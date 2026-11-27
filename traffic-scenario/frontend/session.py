import copy
import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PHASES = {'idle': '에이전트 대기', 'analyzing': '원인 조사', 'modifying': '코드 수정',
          'restarting': '서버 재시작', 'testing': '동일 조건 재실험', 'comparing': '전후 비교',
          'finished': '작업 완료', 'failed': '작업 확인 필요'}
STATES = {'received': '접수', 'queued': '대기', 'processing': '안내 중', 'succeeded': '완료',
          'failed': '실패', 'cancelled': '취소', 'success': '완료', 'http_error': '요청 거절',
          'timeout': '시간 초과', 'connection_error': '연결 오류'}
CATEGORIES = {'cookie': '쿠키', 'gift': '선물', 'ornament': '장식', 'hot_choco': '핫초코', 'toy': '장난감'}


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
        self.baseline = None
        self.run = None
        self.requests = {}
        self.cursor = 0
        self.selected = None
        self.detail = None
        self.comparison = None
        self.message = '평소 방문 또는 고객 급증을 선택하세요.'
        self.error = None
        self.busy = False
        self.agent = '왼쪽 Codex에서 개선을 요청하면 진행 단계를 표시합니다.'
        self.lines = []
        self.line_seq = self.drawn_line = 0
        self.characters = {}
        self.departures = {}
        self.seen_finished = set()
        self.next_departure_at = 0
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
                    count = int(args[0]) if args else 1000
                    duration = float(args[1]) if len(args) > 1 else (30 if action == 'normal' else 2)
                    if not 1 <= count <= 1000 or not 0 < duration <= 3600:
                        raise ValueError('고객 1~1000명과 유입 시간 0초 초과 3600초 이하를 입력하세요.')
                    body = {'scenario_id': action, 'request_count': count, 'arrival_duration_s': duration,
                            'seed': 42, 'client_timeout_ms': 5000}
            value = self.transport('POST', '/api/runs', body)
            with self.lock:
                self.baseline = (self.baseline or previous) if action == 'replay' else None
                self.run_id, self.run = value['run_id'], None
                self.requests, self.cursor, self.selected, self.detail = {}, 0, None, None
                self.comparison = None
                self.lines = []
                self.message = '같은 요청 계획으로 재실험합니다.' if action == 'replay' else '실제 상품 검색 부하를 시작합니다.'
                self.agent = '외부 성능 개선 에이전트의 진행 이벤트를 기다립니다.'
        elif action == 'stop':
            if not self.run_id:
                raise ValueError('먼저 실험을 시작하세요.')
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
                    if kind in ('request.succeeded', 'request.failed', 'request.cancelled', 'client.result'):
                        request.setdefault('finished_mono', time.monotonic())
                    self.requests[rid] = request
                elif kind == 'agent.progress':
                    self.agent = PHASES[data['phase']] + ': ' + data['message']
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
            self.run = snapshot
            self.baseline = self.baseline or snapshot['source_run_id']
            self.detail = detail

    # 기존 마켓 상태에 실험 정보 추가
    def status(self):
        with self.lock:
            return {'server': self.url, 'run_id': self.run_id, 'baseline_run_id': self.baseline, 'run': copy.deepcopy(self.run),
                    'busy': self.busy, 'error': self.error, 'message': self.message, 'agent': self.agent,
                    'selected': self.selected, 'detail': copy.deepcopy(self.detail), 'comparison': copy.deepcopy(self.comparison)}

    # 실제 대기 고객을 기존 픽셀 캐릭터로 표현
    def update(self, world, dt, ui):
        state = self.status()
        with self.lock:
            requests = list(self.requests.values())
            logs = list(self.lines)
        if self.last_run != self.run_id:
            self.characters.clear()
            self.departures.clear()
            self.seen_finished.clear()
            self.next_departure_at = world.t
            world.queue, world.passing, world.hist = [], [], []
            self.last_run = self.run_id
            self.history_at = world.t
        waiting = [r for r in requests if r.get('server_status') in ('received', 'queued')]
        previous = self.characters
        world.queue = []
        for i, request in enumerate(waiting):
            rid = request['request_id']
            person = previous.get(rid)
            if person is None:
                person = ui.Queuer(ui.Queuer.GAP * i + 8)
                person.pal['C'] = 214
                person.request_id = rid
            person.update(dt, ui.Queuer.GAP * i)
            world.queue.append(person)
        self.characters = {p.request_id: p for p in world.queue}
        if world.w:
            self.departures = {rid: person for rid, person in self.departures.items()
                               if self.departure_position(world, person) != 'exited'}
            for request in requests:
                rid = request['request_id']
                if 'finished_mono' not in request or rid in self.seen_finished:
                    continue
                self.seen_finished.add(rid)
                # 혼잡할 때 퇴장 연출은 최대 8명만 표시하고 집계는 실제 전체 요청 사용
                if len(self.departures) >= 8:
                    continue
                born = max(world.t, self.next_departure_at)
                self.next_departure_at = born + .45
                palette = previous[rid].pal.copy() if rid in previous else ui.visitor_pal()
                self.departures[rid] = {'born': born, 'palette': palette}
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

    # 안내소에서 출구까지 고객별 고정 경로 계산
    def departure_position(self, world, person):
        age = world.t - person['born']
        if age < 0:
            return None
        distance = age * 9
        x, start_y, exit_y = world.gate_x - 4, world.bottom - 5, world.gpx + 3
        vertical = max(0, (start_y - exit_y) / 2)
        if distance < vertical:
            return x, start_y - distance * 2
        x -= distance - vertical
        return 'exited' if x < -3 else (x, exit_y)

    # 선택 고객과 비교 결과를 기존 텍스트 형식으로 변환
    def panel(self, state):
        snapshot = state['run']
        if not snapshot:
            return ['버그마스 마켓 안내소', '', '평소 방문 또는 고객 급증을 선택하세요.', '', '노랑: 대기', '파랑: 처리 중', '초록: 완료', '빨강: 실패']
        s, c, m = snapshot['server_counts'], snapshot['client_counts'], snapshot['metrics']
        lines = ['마켓 현황', f"요청 {c['sent']}/{c['planned']}  미전송 {c['dropped']}",
                 f"대기 {s['queued']}  안내 중 {s['processing']}", f"완료 {c['success']}  실패 {c['failed']}",
                 f"평균 {number(m['avg_response_ms'])}", f"p95 {number(m['p95_response_ms'])}",
                 f"실패율 {number(None if m['failure_rate'] is None else m['failure_rate'] * 100, '%')}",
                 f"처리량 {number(m['success_rps'], '/s')}", '', '성능 개선 에이전트', state['agent'], '', '선택한 고객']
        detail = state['detail']
        if detail:
            query = detail['query']
            lines += [detail['customer_id'], f"{CATEGORIES[query['category']]} {query['max_price']:,}원 이하",
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
        frame.append(ui.seg_line([('  ' + status, 203 if state['error'] else 159)], width, 235))
        cv = world.draw('traffic')
        # 외부 에이전트의 실제 진행 단계만 안내원 말풍선으로 표시
        message = PHASES.get(snapshot['agent_phase'], '작업 대기') if snapshot else '마켓 개장 준비'
        if snapshot and snapshot['agent_phase'] == 'idle':
            message = ':( 안내 지연 중' if snapshot['server_counts']['queued'] else ':) 요청 처리 중' if snapshot['status'] in ('pending', 'running') else ':) 실험 종료'
        cv.put(max(0, scene_width - ui.dw(message) - 1), max(0, (world.bottom - 23) // 2), message, 16, 230, True)
        for x, y in ((2, world.gpx - 13), (max(3, scene_width - 20), world.gpx + 2)):
            cv.blit(ui.SOLDIER, x, y, ui.SOLDIER_PAL)
        for person in world.queue:
            x, y = world.qpos(person.d)
            if x >= 0:
                world.hits.append((int(x) + 1, int(x) + 3, int(y) // 2 + 4, int(y) // 2 + 7,
                                   lambda rid=person.request_id: self.command('select ' + rid)))
        with self.lock:
            request_map = dict(self.requests)
        requests = list(request_map.values())
        active = [r for r in requests if r.get('server_status') == 'processing']
        people = [(r, world.gate_x + (i % 2) * 3, world.bottom - 5, True, None)
                  for i, r in enumerate(active[:4]) if r['request_id'] not in self.departures]
        for rid, person in self.departures.items():
            position = self.departure_position(world, person)
            if isinstance(position, tuple) and rid in request_map:
                people.append((request_map[rid], *position, False, person))
        for request, x, y, processing, person in people:
            bad = request.get('client_outcome') not in (None, 'success') or request.get('server_status') in ('failed', 'cancelled')
            palette = person['palette'].copy() if person else {'H': 255, 'S': 223, 'P': 24, 'k': 16}
            palette['C'] = 33 if processing else 196 if bad else 46
            walking = person and int((world.t - person['born']) * 6) % 2
            sprite = ui.VISITOR[:4] + (ui.VISITOR_STEP if walking else ui.VISITOR[4:])
            cv.blit(sprite, x, y, palette)
            world.hits.append((int(x) + 1, int(x) + 3, int(y) // 2 + 4, int(y) // 2 + 7,
                               lambda rid=request['request_id']: self.command('select ' + rid)))
        panel = self.panel(state)
        rendered_panel = [part for line in panel for part in ui.wrap_cols(line, max(20, sidebar - 3), 3)]
        for i, line in enumerate(cv.lines()):
            if sidebar:
                right = ui.seg_line([(' ' + (rendered_panel[i] if i < len(rendered_panel) else ''), 230)], sidebar, 234)
                line += right
            frame.append(line)
        if not sidebar:
            detail = panel[12:] if state['detail'] else panel[:info_height]
            for line in detail[:info_height]:
                frame.append(ui.seg_line([('  ' + line, 230)], width, 234))
            while len(frame) < 3 + scene_height + info_height:
                frame.append(ui.seg_line([], width, 234))
        frame += ui.log_lines(world, width, log_height, ('all', 'traffic'), '[n]평소 [t]급증 [x]중지 [r]재실험 [g]안내 [q]종료')
        frame[-1] = ui.seg_line([('  ' + state['message'], 203 if state['error'] else 223)], width, 235)
        if self.guide:
            guide = ['성능 개선 에이전트 연결', '기준 실험: ' + (self.run_id or '먼저 실험을 시작하세요.'),
                     '왼쪽 Codex에 로그와 코드를 분석하고 병목을 수정하도록 요청하세요.',
                     'POST /api/agent/progress로 작업 단계를 전달하면 화면에 반영됩니다.',
                     '수정 후 서버 재시작과 동일 조건 재실험을 진행하세요.',
                     '이 안내는 에이전트를 자동 실행하지 않습니다.', '[g] 또는 개선 안내 클릭으로 닫기']
            for i, line in enumerate(guide[:height - 5]):
                frame[4 + i] = ui.seg_line([('  ' + line, 230)], width, 24)
        return frame[:height]
