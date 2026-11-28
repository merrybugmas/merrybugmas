import copy
from collections import deque
import json
from pathlib import Path
import queue
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PHASES = {'idle': '에이전트 대기', 'analyzing': '원인 조사', 'modifying': '코드 수정',
          'restarting': '서버 재시작', 'testing': '동일 조건 재실험', 'comparing': '전후 비교',
          'finished': '작업 완료', 'failed': '작업 확인 필요', 'celebrating': 'PR 등록 완료'}
STATES = {'received': '접수', 'queued': '대기', 'processing': '안내 중', 'succeeded': '완료',
          'failed': '실패', 'cancelled': '취소', 'success': '완료', 'http_error': '요청 거절',
          'timeout': '시간 초과', 'connection_error': '연결 오류'}
# PR 등록 축하 연출을 보여 주는 시간(초)
CELEBRATION_S = 15
CATEGORIES = {'cookie': '쿠키', 'gift': '선물', 'ornament': '장식', 'hot_choco': '핫초코', 'toy': '장난감'}
LIMITS = Path(__file__).resolve().parents[1] / 'agent_bridge' / 'limits.json'


# 고객 급증의 요청 수와 유입 시간과 seed를 한도 파일 범위에서 무작위로 선택
def surge_condition():
    limits = json.loads(LIMITS.read_text())
    count = random.randint(*limits['request_count'])

    return count, round(count / random.randint(*limits['rate_per_s']), 2), random.randrange(1_000_000)


# 측정되지 않은 값은 빈 값으로 구분
def number(value, unit='ms'):
    return '—' if value is None else f'{value:,.1f}{unit}'


class Session:
    # 백엔드 연결과 화면 공유 상태 준비
    def __init__(self, url, run_id=None, transport=None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('트래픽 백엔드는 로컬 HTTP 주소를 사용하세요.')
        self.url = url.rstrip('/')
        self.transport = transport or self.request
        self.lock = threading.RLock()
        self.jobs = queue.Queue()
        self.stopped = threading.Event()
        self.run_id = run_id
        self.restoring = bool(run_id)
        self.historical_requests = set()
        self.baseline = None
        self.run = None
        self.requests = {}
        self.cursor = 0
        self.peak_waiting = 0
        self.selected = None
        self.detail = None
        self.comparison = None
        self.message = '평소 방문 또는 고객 급증을 선택하세요.'
        self.error = None
        self.busy = False
        self.agent = '왼쪽 Codex에서 개선을 요청하면 진행 단계를 표시합니다.'
        self.celebrate_until = 0.0
        self.lines = []
        self.line_seq = self.drawn_line = 0
        self.characters = {}
        self.palettes = {}
        self.serving = {}
        self.seen_processing = set()
        self.departures = {}
        self.pending_departures = {False: deque(maxlen=32), True: deque(maxlen=32)}
        self.seen_finished = set()
        self.next_departure_at = 0
        self.last_outcome = True
        self.last_run = None
        self.history_at = 0
        self.guide = False
        self.thread = None

    # 백그라운드 조회 시작
    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.loop, daemon=True, name='traffic-backend')
            self.thread.start()

    # 화면 종료 시 새 조회 중지
    def close(self):
        self.stopped.set()

    # JSON API 호출과 서버 오류 메시지 보존
    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                error = json.load(exc)['error']
                message = f"{error['code']}: {error['message']}"
            except (ValueError, KeyError, TypeError):
                message = f'HTTP {exc.code}'
            raise ValueError(message) from exc

    # 실제 실험 완료와 이벤트 회수 확인
    def complete(self):
        return bool(self.run and self.run['status'] == 'completed' and self.run['telemetry_complete'])

    # 미회수 요청을 포함한 진행 상태 확인
    def active(self):
        return bool(self.run_id and (not self.run or self.run['status'] in ('pending', 'running', 'stopping')))

    # 키보드와 채팅 명령을 같은 작업 큐에 전달
    def command(self, text):
        parts = text.strip().split()
        if not parts:
            return
        action = parts[0].lower()
        parts[0] = action
        if action == 'guide':
            self.guide = not self.guide
            return
        if action not in ('normal', 'surge', 'stop', 'replay', 'compare', 'select'):
            with self.lock:
                self.message = '명령: normal / surge / stop / replay / compare / select 고객ID'
            return
        with self.lock:
            if self.busy and action != 'select':
                return
            if action != 'select':
                self.busy = True
        self.jobs.put(parts)

    # UI 스레드를 막지 않고 명령과 상태 조회 처리
    def loop(self):
        while not self.stopped.is_set():
            try:
                try:
                    parts = self.jobs.get_nowait()
                except queue.Empty:
                    parts = None
                if parts:
                    try:
                        self.execute(parts)
                    finally:
                        with self.lock:
                            self.busy = False
                        self.jobs.task_done()
                self.poll()
                with self.lock:
                    self.error = None
            except (OSError, ValueError, KeyError, TypeError) as exc:
                with self.lock:
                    self.error = '백엔드에 연결할 수 없습니다. 잠시 후 다시 시도합니다.' if isinstance(exc, urllib.error.URLError) else str(exc)
                    self.message = self.error
            self.stopped.wait(0.3)

    # 실제 백엔드에 실험 조작 전달
    def execute(self, parts):
        action, args = parts[0], parts[1:]
        if action in ('normal', 'surge', 'replay'):
            with self.lock:
                if self.active():
                    raise ValueError('실험 중이거나 서버 이벤트를 회수 중입니다.')
                previous = self.run_id
                if action == 'replay':
                    if not self.complete():
                        raise ValueError('완료된 실험만 재실험할 수 있습니다.')
                    body = {'source_run_id': self.run_id}
                else:
                    seed = 42
                    count = int(args[0]) if args else 1000
                    duration = float(args[1]) if len(args) > 1 else (30 if action == 'normal' else 2)
                    # 조건을 지정하지 않은 고객 급증은 부하 실행기가 밀리지 않는 범위에서 매번 다르게 보냄
                    if action == 'surge' and not args:
                        count, duration, seed = surge_condition()
                    if not 1 <= count <= 8000 or not 0 < duration <= 3600:
                        raise ValueError('고객 1~8000명과 유입 시간 0초 초과 3600초 이하를 입력하세요.')
                    body = {'scenario_id': action, 'request_count': count, 'arrival_duration_s': duration,
                            'seed': seed, 'client_timeout_ms': 5000}
            value = self.transport('POST', '/api/runs', body)
            with self.lock:
                self.baseline = (self.baseline or previous) if action == 'replay' else None
                self.run_id, self.run = value['run_id'], None
                self.restoring = False
                self.historical_requests = set()
                self.requests, self.cursor, self.selected, self.detail = {}, 0, None, None
                self.peak_waiting = 0
                self.comparison = None
                self.lines = []
                self.message = '같은 요청 계획으로 재실험합니다.' if action == 'replay' else '실제 상품 검색 부하를 시작합니다.'
                self.agent = '외부 성능 개선 에이전트의 진행 이벤트를 기다립니다.'
        elif action == 'stop':
            if not self.run_id or (self.run and self.run['status'] not in ('pending', 'running', 'stopping')):
                with self.lock:
                    self.message = '진행 중인 실험이 없습니다. n 또는 t로 새 실험을 시작하세요.'
                return
            self.transport('POST', f'/api/runs/{self.run_id}/stop')
            with self.lock:
                self.message = '추가 요청을 중지하고 이미 전송한 요청의 결과를 회수합니다.'
        elif action == 'compare':
            if not self.complete() or not self.baseline:
                raise ValueError('기준 실험과 완료된 재실험이 필요합니다.')
            value = self.transport('POST', '/api/comparisons', {'before_run_id': self.baseline,
                'after_run_id': self.run_id, 'criteria_id': args[0] if args else 'demo-v1'})
            with self.lock:
                self.comparison = value
                self.message = {'improved': '개선 확인', 'regressed': '성능 또는 정확성 악화', 'inconclusive': '판정 보류'}[value['verdict']]
        elif action == 'select':
            if not args or not self.run_id:
                return
            request_id = next((rid for rid, r in self.requests.items() if rid == args[0] or r['customer_id'] == args[0]), None)
            if not request_id:
                raise ValueError('해당 고객의 요청을 찾을 수 없습니다.')
            value = self.transport('GET', f'/api/runs/{self.run_id}/requests/{request_id}')
            with self.lock:
                self.selected, self.detail = request_id, value

    # 커서 이후 이벤트를 중복 없이 반영
    def apply(self, events):
        with self.lock:
            for event in events:
                kind, data = event['type'], event['data']
                if kind.startswith('request.') or kind == 'client.result':
                    rid = event['request_id']
                    request = {**self.requests.get(rid, {}), 'request_id': rid, 'customer_id': event['customer_id']}
                    if kind == 'client.result':
                        request.update(client_outcome=data['outcome'], elapsed_ms=data['elapsed_ms'], query=data['query'])
                    else:
                        request.update(data)
                        request['server_status'] = kind.split('.')[1]
                        if kind == 'request.queued':
                            self.peak_waiting = max(self.peak_waiting, data.get('queue_size', 0))
                        if kind == 'request.processing':
                            request['processing_seen'] = True
                    if kind in ('request.succeeded', 'request.failed', 'request.cancelled', 'client.result'):
                        request.setdefault('finished_mono', time.monotonic())
                    self.requests[rid] = request
                elif kind == 'agent.progress':
                    self.agent = PHASES[data['phase']] + ': ' + data['message']
                    if data['phase'] == 'celebrating':
                        self.celebrate_until = time.monotonic() + CELEBRATION_S
                elif kind == 'log' and data['source'] != 'target':
                    self.line_seq += 1
                    self.lines.append((self.line_seq, data['message']))
                    self.lines = self.lines[-50:]

    # 실험 상태와 선택한 고객 상세 갱신
    def poll(self):
        if not self.run_id:
            self.transport('GET', '/health')
            return
        snapshot = self.transport('GET', f'/api/runs/{self.run_id}')
        for _ in range(20):
            page = self.transport('GET', f'/api/runs/{self.run_id}/events?after_seq={self.cursor}&limit=1000')
            self.apply(page['events'])
            self.cursor = page['next_after_seq']
            if not page['has_more']:
                break
        detail = self.transport('GET', f'/api/runs/{self.run_id}/requests/{self.selected}') if self.selected else None
        with self.lock:
            if self.restoring:
                # 첫 조회는 과거 상태 복원이므로 완료 고객의 연출을 다시 실행하지 않음
                terminal = snapshot['status'] not in ('pending', 'running', 'stopping')
                self.historical_requests = {rid for rid, request in self.requests.items()
                                            if terminal or 'finished_mono' in request}
                self.restoring = False
            previous_status = self.run['status'] if self.run else None
            self.run = snapshot
            self.peak_waiting = max(self.peak_waiting, snapshot['server_counts']['queued'])
            if snapshot['status'] in ('completed', 'stopped', 'failed') and previous_status != snapshot['status']:
                counts = snapshot['client_counts']
                label = {'completed': '실험 종료', 'stopped': '실험 중지 완료', 'failed': '실험 실패'}[snapshot['status']]
                self.message = f"{label} — 완료 {counts['success']}건 / 실패 {counts['failed']}건 / 미전송 {counts['dropped']}건"
            self.baseline = self.baseline or snapshot['source_run_id']
            self.detail = detail

    # PR 등록 축하 연출을 보여 줄 시간인지 확인
    def celebrating(self):
        return time.monotonic() < self.celebrate_until

    # 기존 마켓 상태에 실험 정보 추가
    def status(self):
        with self.lock:
            return {'server': self.url, 'run_id': self.run_id, 'baseline_run_id': self.baseline, 'run': copy.deepcopy(self.run),
                    'busy': self.busy, 'error': self.error, 'message': self.message, 'agent': self.agent,
                    'peak_waiting': self.peak_waiting, 'selected': self.selected, 'detail': copy.deepcopy(self.detail), 'comparison': copy.deepcopy(self.comparison)}

    # 실제 대기 고객을 기존 픽셀 캐릭터로 표현
    def update(self, world, dt, ui):
        state = self.status()
        with self.lock:
            requests = [] if self.restoring else list(self.requests.values())
            logs = list(self.lines)
        if self.last_run != self.run_id:
            self.characters.clear()
            self.palettes.clear()
            self.serving.clear()
            self.seen_processing.clear()
            self.departures.clear()
            for pending in self.pending_departures.values():
                pending.clear()
            self.seen_finished.clear()
            self.next_departure_at = world.t
            self.last_outcome = True
            world.queue, world.passing, world.hist = [], [], []
            self.last_run = self.run_id
            self.history_at = world.t
        previous = self.characters
        waiting = [r for r in requests if r['request_id'] not in self.historical_requests
                   and (r.get('server_status') in ('received', 'queued')
                        or (r.get('server_status') == 'processing' and r['request_id'] in previous))
                   and r['request_id'] not in self.serving and not self.failed(r)]
        world.queue = []
        for i, request in enumerate(waiting):
            rid = request['request_id']
            person = previous.get(rid)
            if person is None:
                goal = world.queue_gap * i
                person = ui.Queuer(max(getattr(world, 'qvis', 0) + 8, goal + 8))
                person.entry_speed = ui.Queuer.SPEED * 2
                person.pal = self.customer_palette(rid, ui).copy()
                person.request_id = rid
            goal = world.queue_gap * i
            before = person.d
            if person.d > goal + .05:
                person.d -= min(person.d - goal, person.entry_speed * dt)
            if world.queue:
                person.d = max(person.d, world.queue[-1].d + world.queue_gap)
            person.moving = person.d < before - .05
            if person.moving:
                person.step += dt
            world.queue.append(person)
        self.characters = {p.request_id: p for p in world.queue}
        if world.w:
            request_map = {r['request_id']: r for r in requests}
            previous_serving = self.serving
            self.serving = {rid: person for rid, person in self.serving.items()
                            if rid in request_map and not self.failed(request_map[rid]) and (request_map[rid].get('server_status') == 'processing'
                                                       or world.t < person['until'])}
            self.departures = {rid: person for rid, person in self.departures.items()
                               if self.departure_position(world, person) != 'exited'}
            # 표시하지 못한 결과는 최근 대표 고객으로 보관해 다음 퇴장과 연결
            capacity = self.departure_capacity(world)
            for request in sorted(requests, key=lambda r: r['request_id'] not in previous_serving):
                rid = request['request_id']
                if rid in self.historical_requests or 'finished_mono' not in request or rid in self.seen_finished or rid in self.serving:
                    continue
                self.seen_finished.add(rid)
                bad = self.failed(request)
                visible_waiter = rid in previous and 0 <= world.qpos(previous[rid].d)[1] <= world.bottom
                palette = self.customer_palette(rid, ui).copy()
                start = world.guide_front
                if rid in previous_serving:
                    start = self.service_position(world, previous_serving[rid])
                elif visible_waiter:
                    start = world.qpos(previous[rid].d)
                person = {'palette': palette, 'failed': bad, 'start': start,
                          'category': request.get('query', {}).get('category'), 'request_id': rid,
                          'via_guide': rid not in previous_serving and not bad, 'guided': rid in previous_serving}
                if rid in previous_serving or visible_waiter:
                    person['born'] = world.t
                    self.departures[rid] = person
                else:
                    person['start'] = world.entrance
                    person['via_guide'] = True
                    self.pending_departures[bad].append((rid, person))
            outcome_counts = {bad: sum(self.failed(request_map[rid]) == bad for rid in self.departures if rid in request_map)
                              for bad in (False, True)}
            for bad in sorted(self.pending_departures, key=lambda outcome: outcome == self.last_outcome):
                pending = self.pending_departures[bad]
                next_at = self.next_departure_at
                if not pending or world.t < next_at or outcome_counts[bad] + len(self.serving) >= capacity:
                    continue
                rid, person = pending.popleft()
                actual_bad = self.failed(request_map[rid])
                if actual_bad != bad:
                    person['failed'] = actual_bad
                    self.pending_departures[actual_bad].append((rid, person))
                    continue
                person['born'] = world.t
                self.departures[rid] = person
                outcome_counts[bad] += 1
                self.last_outcome = bad
                self.next_departure_at = world.t + .6
                break
            if state['run'] and state['run']['status'] in ('completed', 'stopped', 'failed') and state['run']['telemetry_complete']:
                for pending in self.pending_departures.values():
                    pending.clear()
            # 실제 대기줄에 보이던 고객만 안내로 이동시키고 퇴장 자리도 미리 확보
            for request in requests:
                rid = request['request_id']
                if rid in self.historical_requests or request.get('server_status') != 'processing' or rid in self.seen_processing or rid in self.serving or 'finished_mono' in request:
                    continue
                if self.failed(request) or rid not in previous:
                    continue
                start_x, _ = world.qpos(previous[rid].d)
                if start_x < 0 or previous[rid].d > ui.Queuer.GAP * 2 or len(self.serving) >= 1 or any(count + len(self.serving) >= capacity for count in outcome_counts.values()):
                    continue
                self.seen_processing.add(rid)
                duration = max(.3, previous[rid].d / 20)
                self.serving[rid] = {'until': world.t + duration + .6,
                                     'palette': previous[rid].pal.copy(), 'born': world.t,
                                     'duration': duration, 'start_d': previous[rid].d}
            world.queue = [person for person in world.queue if person.request_id not in self.serving]
            self.characters = {person.request_id: person for person in world.queue}
        snapshot = state['run']
        world.rps = (snapshot['metrics']['success_rps'] or 0) if snapshot else 0
        world.admitted = snapshot['client_counts']['success'] if snapshot else 0
        world.peak = max(1, world.rps, max(world.hist or [0]))
        if world.t - self.history_at >= 0.5:
            world.hist = (world.hist + [world.rps])[-60:]
            self.history_at = world.t
        for seq, text in logs:
            if seq > self.drawn_line:
                world.log(text, 183, 'traffic')
                self.drawn_line = seq
        for snow in getattr(world, 'snow', []):
            snow[1] = (snow[1] + snow[2] * dt) % max(1, world.gpx)

    # 고객의 옷과 모자 색은 상태가 바뀌어도 유지
    def customer_palette(self, request_id, ui):
        if request_id not in self.palettes:
            self.palettes[request_id] = ui.visitor_pal()
        return self.palettes[request_id]

    # 실제 요청 상태를 옷 대신 머리 위 표시로 표현
    @staticmethod
    def customer_mark(request):
        if Session.failed(request):
            return '!', 196
        if request.get('client_outcome') == 'success' or request.get('server_status') == 'succeeded':
            return '+', 46
        if request.get('server_status') == 'processing':
            return '?', 33
        return '…', 214

    # 안내소에 도착한 뒤에만 고객 머리 위 상태 표시
    @staticmethod
    def customer_mark_visible(world, person, processing=False):
        age = world.t - person['born']
        if processing:
            return age >= person['duration']
        if person.get('via_guide'):
            start, front = person['start'], world.guide_front
            distance = abs(front[0] - start[0]) + abs(front[1] - start[1]) / 2
            return age * 14 >= distance + 2.8
        return person.get('guided', False)

    # 화면의 출구 경로 길이에 맞춰 연속 퇴장 인원 확보
    @staticmethod
    def departure_capacity(world):
        distance = world.gate_x + abs(world.bottom - world.gpx - 8) / 2
        return max(8, int(distance / 4) + 2)


    # 대기줄에서 안내 위치까지 같은 고객을 연속 이동
    def service_position(self, world, person):
        progress = min(1, max(0, (world.t - person['born']) / person['duration']))
        x, y = world.qpos(person['start_d'] * (1 - progress))
        return x, y

    # 성공 고객은 안내소를 거쳐 요청한 상품 상점으로 이동
    @staticmethod
    def departure_route(world, person):
        start = person['start']
        front = world.guide_front
        points = [start]
        if person.get('via_guide'):
            points.append(front)
        if person['failed']:
            exit_y = front[1] - 7
            detour_x = min(points[-1][0], world.tree_cx - 15)
            points += [(detour_x, points[-1][1]), (detour_x, exit_y), (-4, exit_y)]
        else:
            target_x, target_y = world.shop_target(person.get('category'), person.get('request_id', ''))
            side = -1 if target_x < world.tree_cx else 1
            detour_x = world.tree_cx + side * 15
            points += [(detour_x, points[-1][1]), (detour_x, world.gpx + 14),
                       (target_x, world.gpx + 14), (target_x, target_y),
                       (target_x, world.gpx + 14),
                       (-4 if side < 0 else world.w + 4, world.gpx + 14)]
        return points

    # 이동 연출 시간과 실제 요청 측정 시간은 분리
    def departure_position(self, world, person):
        age = world.t - person['born']
        if age < 0:
            return None
        distance = age * 14
        points = self.departure_route(world, person)
        for start, end in zip(points, points[1:]):
            length = abs(end[0] - start[0]) + abs(end[1] - start[1]) / 2
            if distance <= length and length:
                progress = distance / length
                return start[0] + (end[0] - start[0]) * progress, start[1] + (end[1] - start[1]) * progress
            distance -= length
            hold = 2.8 if end == world.guide_front else 4.2 if end == world.shop_target(person.get('category'), person.get('request_id', '')) and not person['failed'] else 0
            if distance < hold:
                return end
            distance -= hold
        return 'exited'

    # 고객이 받은 최종 실패 결과 우선 확인
    @staticmethod
    def failed(request):
        outcome = request.get('client_outcome')
        if outcome is not None:
            return outcome != 'success'
        return request.get('server_status') in ('failed', 'cancelled')

    # 선택 고객과 비교 결과를 기존 텍스트 형식으로 변환
    def panel(self, state):
        snapshot = state['run']
        if not snapshot:
            return ['버그마스 마켓 안내소', '', '평소 방문 또는 고객 급증을 선택하세요.', '', '머리 위 표시', '… 대기  ? 안내 중', '+ 완료  ! 실패']
        s, c, m = snapshot['server_counts'], snapshot['client_counts'], snapshot['metrics']
        lines = ['마켓 현황', f"요청 {c['sent']}/{c['planned']}  미전송 {c['dropped']}",
                 f"대기 {s['queued']}  안내 중 {s['processing']}", f"완료 {c['success']}  실패 {c['failed']}",
                 f"평균 {number(m['avg_response_ms'])}", f"p95 {number(m['p95_response_ms'])}",
                 f"실패율 {number(None if m['failure_rate'] is None else m['failure_rate'] * 100, '%')}",
                 f"처리량 {number(m['success_rps'], '/s')}", '동선: 입장 → 안내소 → 상점', '표시: … 대기  ? 안내 중', '      + 완료  ! 실패', '초록 안내원: 개선 상태', '파랑 직원: 안내소 담당', '', '성능 개선 에이전트', state['agent'], '', '선택한 고객']
        detail = state['detail']
        if detail:
            query = detail['query']
            lines += [detail['customer_id'], f"질문: {query['max_price']:,}원 이하 {CATEGORIES[query['category']]} 가게는?",
                      f"서버: {STATES.get(detail['server_status'], '—')}", f"고객: {STATES.get(detail['client_outcome'], '—')}",
                      '대기 ' + number(detail['queue_wait_ms']), '현재 대기 추정 ' + number(detail['current_wait_ms']),
                      '처리 ' + number(detail['processing_ms']), '전체 응답 ' + number(detail['client_elapsed_ms'])]
            if detail['error_code']:
                lines.append(detail['error_code'])
            if detail['items'] is not None:
                lines.append(f"검색 결과 {len(detail['items'])}개")
        else:
            lines += ['고객을 클릭하세요.', '[j/k] 선택  [Enter] 상세']
        result = state['comparison']
        if result:
            lines += ['', '전후 비교', state['message'], *result['reasons']]
            for row in result['metrics']:
                label = {'avg_response_ms': '평균', 'p95_response_ms': 'p95', 'failure_rate': '실패율', 'success_rps': '처리량'}[row['name']]
                scale = 100 if row['name'] == 'failure_rate' else 1
                before = None if row['before'] is None else row['before'] * scale
                after = None if row['after'] is None else row['after'] * scale
                lines.append(f"{label}: {number(before, '')} → {number(after, '')}")
        return lines

    # 기존 키 입력에서 트래픽 실험 조작만 처리
    def handle_key(self, ch):
        if ch in ('\x1b', 'm'):
            self.guide = False
            return True
        action = {'n': 'normal', 't': 'surge', 'x': 'stop', 'r': 'replay', 'c': 'compare', 'g': 'guide'}.get(ch)
        if action:
            self.command(action)
            return True
        if ch in ('j', 'k', '\r', '\n'):
            with self.lock:
                keys = list(self.requests)
                if keys:
                    index = keys.index(self.selected) if self.selected in keys else (-1 if ch == 'j' else 0)
                    if ch in ('j', 'k'):
                        index = (index + (1 if ch == 'j' else -1)) % len(keys)
                    self.command('select ' + keys[index])
            return True
        return ch in ('0', '1', '2', '\t', 'b', 'h', 'a')

    # 버그 픽스 화면과 같은 렌더러와 클릭 규칙으로 트래픽 화면 구성
    def render(self, world, width, height, paused, ui):
        state = self.status()
        sidebar = 34 if width >= 110 else 0
        scene_width = width - sidebar
        log_height = 6 if height >= 32 else 4
        info_height = 0 if sidebar else 8 if height >= 32 else 6 if height >= 28 else 5
        scene_height = max(9, height - 3 - log_height - info_height)
        world.layout(scene_width, scene_height)
        frame = [ui.seg_line([('  🎄 MERRY BUGMAS ', 226, True), (' 트래픽 부하 ', 16, True, 223),
                             ('  || 화면 정지' if paused else '  LIVE EXPERIMENT', 159)], width, 52)]
        buttons = [('평소 방문[n]', 'normal'), ('고객 급증[t]', 'surge'), ('중지[x]', 'stop'),
                   ('재실험[r]', 'replay'), ('비교[c]', 'compare'), ('개선 안내[g]', 'guide')]
        segments, col = [], 1
        for label, action in buttons:
            label = ' ' + label + ' '
            segments.append((label, 230, False, 238))
            world.hits.append((col, col + ui.dw(label) - 1, 2, 2, lambda action=action: self.command(action)))
            col += ui.dw(label) + 1
            segments.append((' ', 7))
        frame.append(ui.seg_line(segments, width, 234))
        snapshot = state['run']
        status = '연결 확인 필요: ' + state['error'] if state['error'] else ('백엔드 연결 중' if not snapshot else
                 f"{snapshot['status']}  {state['run_id']}  " + ('수집 완료' if snapshot['telemetry_complete'] else '이벤트 수집 중'))
        cv = world.draw('traffic')
        # 안내소 상태는 간판 위의 전용 공간에 표시
        message = PHASES.get(snapshot['agent_phase'], '작업 대기') if snapshot else '마켓 개장 준비'
        if snapshot and snapshot['agent_phase'] == 'idle':
            message = ':( 안내 지연 중' if snapshot['server_counts']['queued'] else ':) 요청 처리 중' if snapshot['status'] in ('pending', 'running') else ':) 실험 종료'
        frame.append(ui.seg_line([('  ' + status, 203 if state['error'] else 159)], width, 235))
        for x, y in ((2, world.gpx - 13), (max(3, scene_width - 9), world.gpx - 13)):
            cv.blit(ui.SOLDIER, x, y, ui.SOLDIER_PAL)
        with self.lock:
            request_map = dict(self.requests)
        for person in world.queue:
            x, y = world.qpos(person.d)
            if 0 <= y <= world.bottom:
                world.hits.append((int(x) + 1, int(x) + 3, int(y) // 2 + 4, int(y) // 2 + 7,
                                   lambda rid=person.request_id: self.command('select ' + rid)))
        people = [(request_map[rid], *self.service_position(world, person), True, person)
                  for rid, person in self.serving.items() if rid in request_map]
        for rid, person in self.departures.items():
            position = self.departure_position(world, person)
            if isinstance(position, tuple) and rid in request_map:
                people.append((request_map[rid], *position, False, person))
        for request, x, y, processing, person in people:
            palette = person['palette']
            moving = person and (not processing or world.t < person['born'] + person['duration'])
            walking = moving and int((world.t - person['born']) * 6) % 2
            sprite = ui.VISITOR[:4] + (ui.VISITOR_STEP if walking else ui.VISITOR[4:])
            cv.blit(sprite, x, y, palette)
            if self.customer_mark_visible(world, person, processing):
                mark, color = self.customer_mark(request)
                cv.put(int(x) + 1, max(0, int(y) // 2 - 1), mark, color)
            world.hits.append((int(x) + 1, int(x) + 3, int(y) // 2 + 4, int(y) // 2 + 7,
                               lambda rid=request['request_id']: self.command('select ' + rid)))
        cv.put(world.tree_cx - ui.dw(message) // 2, max(0, world.guide_top // 2 - 3),
               message, 16, 230, True)
        if snapshot:
            queued = snapshot['server_counts']['queued']
            badge_x = world.tree_cx + 8
            badge_row = min(world.sh - 2, world.guide_front[1] // 2 + 4)
            cv.put(badge_x, badge_row, f' 현재 대기 {queued}명 ', 231, 160 if queued else 238, True)
            cv.put(badge_x, badge_row + 1, f" 관측 최대 {max(state['peak_waiting'], queued)}명 ", 16, 230, True)
        panel = self.panel(state)
        if len(world.booths) < 8:
            panel.insert(1, '상점 8개: 터미널 폭 180열 이상')
        rendered_panel = [part for line in panel for part in ui.wrap_cols(line, max(20, sidebar - 3), 3)]
        for i, line in enumerate(cv.lines()):
            if sidebar:
                right = ui.seg_line([(' ' + (rendered_panel[i] if i < len(rendered_panel) else ''), 230)], sidebar, 234)
                line += right
            frame.append(line)
        if not sidebar:
            detail = panel[panel.index('선택한 고객'):] if state['detail'] else panel[:info_height]
            for line in detail[:info_height]:
                frame.append(ui.seg_line([('  ' + line, 230)], width, 234))
            while len(frame) < 3 + scene_height + info_height:
                frame.append(ui.seg_line([], width, 234))
        frame += ui.log_lines(world, width, log_height, ('all', 'traffic'), '[n]평소 [t]급증 [x]중지 [r]재실험 [g]안내 [q]종료')
        footer = state['message']
        if snapshot and snapshot['status'] in ('completed', 'stopped', 'failed') and (self.departures or self.serving) and not state['error']:
            footer += '  | 고객 퇴장 연출 중'
        frame[-1] = ui.seg_line([('  ' + footer, 203 if state['error'] else 223)], width, 235)
        if self.guide:
            guide = ['성능 개선 에이전트 연결', '기준 실험: ' + (self.run_id or '먼저 실험을 시작하세요.'),
                     '왼쪽 Codex에 로그와 코드를 분석하고 병목을 수정하도록 요청하세요.',
                     'POST /api/agent/progress로 작업 단계를 전달하면 화면에 반영됩니다.',
                     '수정 후 서버 재시작과 동일 조건 재실험을 진행하세요.',
                     '이 안내는 에이전트를 자동 실행하지 않습니다.', '[g] 또는 개선 안내 클릭으로 닫기']
            for i, line in enumerate(guide[:height - 5]):
                frame[4 + i] = ui.seg_line([('  ' + line, 230)], width, 24)
        return frame[:height]
