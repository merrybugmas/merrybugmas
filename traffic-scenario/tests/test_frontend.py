import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from frontend.app import TrafficWorld, compose, frame_update, handle_input, saved_run, ui
from frontend.session import Session


# API 명세 형태의 실제 집계 데이터 준비
def snapshot(run_id='run-1', status='running', queued=0):
    return {'run_id': run_id, 'source_run_id': None, 'status': status, 'telemetry_complete': status == 'completed',
            'agent_phase': 'idle', 'server_counts': {'received': queued, 'queued': queued, 'processing': 0, 'succeeded': 0, 'failed': 0, 'cancelled': 0},
            'client_counts': {'planned': 1000, 'sent': queued, 'success': 0, 'failed': 0, 'pending': queued, 'dropped': 0},
            'metrics': {'avg_response_ms': None, 'p95_response_ms': None, 'failure_rate': None, 'success_rps': None}}


class TrafficFrontendTests(unittest.TestCase):
    # 같은 서버에서 실행한 마지막 실험만 다시 연결
    def test_reconnects_saved_run_for_matching_server(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            (runtime / 'state.json').write_text(json.dumps({'experiment': {'server': 'http://127.0.0.1:8000', 'run_id': 'existing', 'run': {'status': 'running'}}}))
            self.assertEqual(saved_run(runtime, 'http://127.0.0.1:8000/'), 'existing')
            self.assertIsNone(saved_run(runtime, 'http://127.0.0.1:9000'))

    # 종료된 실험은 다음 실행에서 자동으로 불러오지 않음
    def test_finished_saved_run_opens_an_empty_market(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            for status in ('completed', 'stopped', 'failed'):
                with self.subTest(status=status):
                    (runtime / 'state.json').write_text(json.dumps({'experiment': {
                        'server': 'http://127.0.0.1:8000', 'run_id': 'old', 'run': {'status': status}}}))
                    self.assertIsNone(saved_run(runtime, 'http://127.0.0.1:8000'))

    # 과거 기록을 직접 열어도 읽는 도중과 읽은 뒤에 고객을 재생하지 않음
    def test_historical_events_do_not_replay_characters(self):
        events = [{'type': 'request.processing', 'request_id': 'old', 'customer_id': 'old-c', 'data': {}},
                  {'type': 'request.succeeded', 'request_id': 'old', 'customer_id': 'old-c', 'data': {}}]

        # 기록 조회용 API 응답 제공
        def api(method, path, body=None):
            if '/events?' in path:
                return {'events': events, 'next_after_seq': 2, 'has_more': False}
            return snapshot('old', 'completed')

        session = Session('http://127.0.0.1:8000', 'old', transport=api)
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply(events)
        world.update(.1)
        self.assertFalse(session.serving)
        self.assertFalse(session.departures)
        session.poll()
        world.update(.1)
        self.assertEqual(session.run['status'], 'completed')
        self.assertIn('old', session.requests)
        self.assertFalse(session.serving)
        self.assertFalse(session.departures)

    # 실행 중 재연결은 현재 대기 고객과 이후 새 이벤트만 연출
    def test_active_resume_suppresses_only_past_completed_customers(self):
        events = [{'type': 'request.succeeded', 'request_id': 'past', 'customer_id': 'past-c', 'data': {}},
                  {'type': 'request.queued', 'request_id': 'waiting', 'customer_id': 'waiting-c', 'data': {}}]

        # 현재 실험과 이전 완료 이벤트를 함께 반환
        def api(method, path, body=None):
            if '/events?' in path:
                return {'events': events, 'next_after_seq': 2, 'has_more': False}
            return snapshot('active', 'running', queued=1)

        session = Session('http://127.0.0.1:8000', 'active', transport=api)
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.poll()
        world.update(.1)
        self.assertEqual([p.request_id for p in world.queue], ['waiting'])
        self.assertNotIn('past', session.departures)
        session.apply([{'type': 'request.succeeded', 'request_id': 'new', 'customer_id': 'new-c', 'data': {}}])
        world.update(.1)
        self.assertIn('new', session.departures)

    # 종료된 실험에 중지 API를 보내지 않고 시작 방법 안내
    def test_stop_on_finished_run_does_not_call_backend(self):
        api = Mock()
        session = Session('http://127.0.0.1:8000', transport=api)
        session.run_id = 'old'
        session.run = snapshot('old', 'completed')
        session.execute(['stop'])
        api.assert_not_called()
        self.assertIn('진행 중인 실험이 없습니다', session.message)

    # 종료된 이전 실험의 시작 허용 여부는 백엔드가 판단
    def test_stopped_run_does_not_block_frontend_start(self):
        session = Session('http://127.0.0.1:8000', 'stopped')
        session.run = snapshot('stopped', 'stopped')
        self.assertFalse(session.active())
        self.assertFalse(session.complete())

    # 기존 버그 화면과 같은 화면 규격 및 패널 출력 유지
    def test_existing_renderer_sizes_and_stream_protocol(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        for width, height in ((60, 22), (82, 30), (100, 30), (140, 45)):
            frame = compose(world, width, height)
            self.assertEqual(len(frame), height)
            self.assertTrue(all(ui.dw(ui.ANSI.sub('', line)) == width for line in frame))
            self.assertTrue(ui.frame_blocks(frame))
            self.assertIn('평소', '\n'.join(frame))

    # 처리량이 높아도 실제 이벤트에 없는 대기 고객을 만들지 않음
    def test_queue_follows_requests_not_rps(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        world.src.rps = Mock(side_effect=AssertionError('mock must not be read'))
        session.run = snapshot(queued=1)
        session.run['metrics']['success_rps'] = 10000
        session.run_id = 'run-1'
        session.apply([{'type': 'request.received', 'request_id': 'r1', 'customer_id': 'c1',
                        'data': {'query': {'category': 'cookie', 'max_price': 20000}}},
                       {'type': 'request.queued', 'request_id': 'r1', 'customer_id': 'c1', 'data': {'queue_size': 1}}])
        compose(world, 140, 45)
        world.update(.1)
        self.assertEqual(len(world.queue), 1)
        self.assertEqual(world.queue[0].request_id, 'r1')
        session.apply([{'type': 'request.failed', 'request_id': 'r1', 'customer_id': 'c1', 'data': {'error_code': 'QUEUE_TIMEOUT'}}])
        world.update(.1)
        self.assertEqual(len(world.queue), 0)
        self.assertEqual(len(world.mice), 0)
        world.src.rps.assert_not_called()

    # 신규 실험과 재실험 및 비교가 원래 백엔드 API를 사용
    def test_start_replay_compare_and_invalid_input(self):
        calls = []

        # 네트워크 대신 API 요청 본문 기록
        def api(method, path, body=None):
            calls.append((method, path, body))
            if path == '/api/runs':
                return {'run_id': 'run-' + str(len(calls))}
            return {'verdict': 'inconclusive', 'metrics': [], 'reasons': ['표본 부족']}

        session = Session('http://127.0.0.1:8000', transport=api)
        with self.assertRaises(ValueError):
            session.execute(['surge', '1001'])
        session.execute(['surge', '1000', '.5'])
        self.assertEqual(calls[0][2]['arrival_duration_s'], .5)
        first = session.run_id
        session.run = snapshot(first, 'completed')
        session.execute(['replay'])
        self.assertEqual(calls[1][2], {'source_run_id': first})
        self.assertEqual(session.baseline, first)
        session.run = snapshot(session.run_id, 'completed')
        session.execute(['compare', 'team-policy'])
        self.assertEqual(calls[-1][2]['criteria_id'], 'team-policy')
        self.assertEqual(session.comparison['verdict'], 'inconclusive')

    # 마우스로 누른 버튼과 채팅이 같은 명령 경로 사용
    def test_mouse_buttons_and_chat_command(self):
        world = TrafficWorld(Session('http://127.0.0.1:8000'))
        compose(world, 140, 45)
        normal = next(hit for hit in world.hits if hit[2] == 2)
        handle_input(world, f'\x1b[<0;{normal[0]};2M', {'quit': False, 'paused': False})
        self.assertEqual(world.experiment.jobs.get_nowait(), ['normal'])
        world.experiment.busy = False
        world.command('experiment surge 1000 1')
        self.assertEqual(world.experiment.jobs.get_nowait(), ['surge', '1000', '1'])

    # 실제 에이전트 이벤트와 마지막 수신 상태 유지
    def test_progress_and_connection_failure_keep_observation(self):
        session = Session('http://127.0.0.1:8000')
        session.run = snapshot()
        session.apply([{'type': 'agent.progress', 'data': {'phase': 'analyzing', 'message': '검색 쿼리 확인 중'}}])
        session.error = 'offline'
        state = session.status()
        self.assertEqual(state['run']['run_id'], 'run-1')
        self.assertIn('검색 쿼리 확인 중', state['agent'])
        self.assertEqual(state['error'], 'offline')

    # 새 완료 고객이 추가돼도 기존 고객의 경로 유지
    def test_departure_keeps_position_when_results_arrive(self):
        session = Session('http://127.0.0.1:8000')
        session.run_id = 'run-1'
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply([{'type': 'request.succeeded', 'request_id': 'r1', 'customer_id': 'c1', 'data': {}}])
        world.update(.1)
        person = session.departures['r1']
        self.assertEqual(session.departure_position(world, person), world.entrance)
        world.update(.6)
        position = session.departure_position(world, person)
        session.apply([{'type': 'request.succeeded', 'request_id': 'r2', 'customer_id': 'c2', 'data': {}}])
        world.update(0)
        self.assertIs(session.departures['r1'], person)
        self.assertEqual(session.departure_position(world, person), position)
        self.assertGreater(session.departures['r2']['born'], person['born'])

    # 렌더링만 반복해도 위치가 바뀌지 않고 퇴장 후 다시 생성되지 않음
    def test_departure_uses_animation_clock_and_exits_once(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply([{'type': 'request.succeeded', 'request_id': 'r1', 'customer_id': 'c1', 'data': {}}])
        world.update(.1)
        person = session.departures['r1']
        position = session.departure_position(world, person)
        compose(world, 140, 45, paused=True)
        compose(world, 140, 45, paused=True)
        self.assertEqual(session.departure_position(world, person), position)
        world.update(100)
        self.assertNotIn('r1', session.departures)
        world.update(.1)
        self.assertNotIn('r1', session.departures)

    # 직원은 처리 고객이 없어도 안내소에 고정 표시
    def test_blue_employee_stays_when_request_status_changes(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        for kind in ('received', 'processing', 'succeeded'):
            session.apply([{'type': 'request.' + kind, 'request_id': 'r1', 'customer_id': 'c1', 'data': {}}])
            world.update(.1)
            canvas = world.draw('traffic')
            self.assertEqual(canvas.get(world.tree_cx - 1, world.guide_top + 6), 33)

    # 짧은 요청도 고객 안내 장면을 유지한 뒤 같은 캐릭터로 퇴장
    def test_processing_visual_holds_without_changing_real_status(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply([{'type': 'request.queued', 'request_id': 'r1', 'customer_id': 'c1', 'data': {}}])
        world.update(10)
        session.apply([{'type': 'request.processing', 'request_id': 'r1', 'customer_id': 'c1', 'data': {}}])
        world.update(.1)
        person = session.serving['r1']
        session.apply([{'type': 'request.succeeded', 'request_id': 'r1', 'customer_id': 'c1', 'data': {}}])
        world.update(.1)
        self.assertIs(session.serving['r1'], person)
        self.assertEqual(session.requests['r1']['server_status'], 'succeeded')
        self.assertNotIn('r1', session.departures)
        world.update(1)
        self.assertNotIn('r1', session.serving)
        self.assertEqual(session.departures['r1']['palette'], person['palette'])
        self.assertEqual(session.departure_position(world, session.departures['r1']),
                         world.guide_front)

    # 성공 고객 퇴장 자리가 가득 차도 실패 고객을 표시
    def test_failure_departures_are_not_hidden_by_successes(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply([{'type': 'request.succeeded', 'request_id': f'ok-{i}', 'customer_id': f'c-{i}', 'data': {}}
                       for i in range(12)])
        world.update(.1)
        self.assertEqual(len(session.departures), 1)
        self.assertEqual(len(session.pending_departures[False]), 11)
        session.apply([{'type': 'request.failed', 'request_id': 'bad', 'customer_id': 'bad-c',
                        'data': {'error_code': 'QUEUE_FULL'}}])
        world.update(.6)
        self.assertIn('bad', session.departures)
        self.assertTrue(session.departures['bad']['failed'])
        self.assertIsInstance(session.departure_position(world, session.departures['bad']), tuple)

    # 대기 고객은 줄 중간에 생성되지 않고 화면 밖에서 입장
    def test_waiting_customer_enters_from_offscreen(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply([{'type': 'request.queued', 'request_id': 'r1', 'customer_id': 'c1', 'data': {}}])
        world.update(0)
        person = world.queue[0]
        initial = person.d
        self.assertGreater(initial, world.qvis)
        world.update(.3)
        self.assertGreater(person.d, 0)
        self.assertLess(person.d, initial)
        world.update(.3)
        self.assertAlmostEqual(initial - person.d, person.entry_speed * .6)
        world.update(10)
        self.assertAlmostEqual(person.d, 0)

    # 대기 고객이 한꺼번에 들어와도 캐릭터끼리 겹치지 않음
    def test_arrival_batch_keeps_customer_spacing(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply([{'type': 'request.queued', 'request_id': f'r-{i}', 'customer_id': f'c-{i}', 'data': {}}
                       for i in range(40)])
        for _ in range(12):
            world.update(.1)
            self.assertTrue(all(b.d - a.d >= ui.Queuer.GAP - .001 for a, b in zip(world.queue, world.queue[1:])))

    # 같은 조회에 완료까지 들어온 요청을 파란 고객으로 재생하지 않음
    def test_completed_batch_does_not_spawn_blue_customers(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply([{'type': kind, 'request_id': 'fast', 'customer_id': 'c', 'data': {}}
                       for kind in ('request.processing', 'request.succeeded')])
        world.update(.1)
        self.assertFalse(session.serving)
        self.assertIn('fast', session.departures)
        world.update(1)
        self.assertFalse(session.serving)
        session.apply([
            {'type': 'request.processing', 'request_id': 'client-done', 'customer_id': 'c2', 'data': {}},
            {'type': 'client.result', 'request_id': 'client-done', 'customer_id': 'c2',
             'data': {'outcome': 'success', 'elapsed_ms': 10, 'query': {'category': 'cookie', 'max_price': 20000}}}
        ])
        world.update(.1)
        self.assertFalse(session.serving)

    # 처리 상태만 처음 관측한 요청은 고정 위치에 갑자기 만들지 않음
    def test_processing_without_visible_queue_does_not_materialize(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply([{'type': 'request.processing', 'request_id': 'r', 'customer_id': 'c', 'data': {}}])
        world.update(.1)
        self.assertFalse(session.serving)
        self.assertEqual(session.requests['r']['server_status'], 'processing')

    # 안내 고객은 퇴장 자리를 확보하고 같은 위치에서 퇴장 시작
    def test_serving_customer_has_reserved_continuous_exit(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply([{'type': 'request.queued', 'request_id': 'r', 'customer_id': 'c', 'data': {}}])
        world.update(10)
        queue_position = world.qpos(world.queue[0].d)
        session.apply([{'type': 'request.processing', 'request_id': 'r', 'customer_id': 'c', 'data': {}}])
        world.update(0)
        person = session.serving['r']
        self.assertEqual(session.service_position(world, person), queue_position)
        session.apply([{'type': 'request.succeeded', 'request_id': f'other-{i}', 'customer_id': f'c-{i}', 'data': {}}
                       for i in range(12)])
        world.update(.4)
        self.assertEqual(len(session.departures), 1)
        self.assertLess(len(session.departures) + len(session.serving), session.departure_capacity(world))
        session.apply([{'type': 'request.succeeded', 'request_id': 'r', 'customer_id': 'c', 'data': {}}])
        world.update(.4)
        position = session.service_position(world, person)
        world.update(.2)
        self.assertNotIn('r', session.serving)
        self.assertIn('r', session.departures)
        self.assertEqual(session.departure_position(world, session.departures['r']), position)

    # 대기 중 실패한 고객은 자기 자리에서 같은 모습으로 이탈
    def test_waiting_failures_leave_from_their_own_positions(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply([{'type': 'request.queued', 'request_id': f'r-{i}', 'customer_id': f'c-{i}', 'data': {}}
                       for i in range(12)])
        world.update(10)
        positions = {p.request_id: world.qpos(p.d) for p in world.queue if world.qpos(p.d)[1] <= world.bottom}
        palettes = {p.request_id: p.pal.copy() for p in world.queue}
        session.apply([{'type': 'request.failed', 'request_id': rid, 'customer_id': rid, 'data': {'error_code': 'QUEUE_TIMEOUT'}}
                       for rid in positions])
        world.update(0)
        self.assertTrue(set(positions).isdisjoint(p.request_id for p in world.queue))
        self.assertEqual(len(session.departures), len(positions))
        for rid, position in positions.items():
            self.assertEqual(session.departure_position(world, session.departures[rid]), position)
            self.assertEqual(session.departures[rid]['palette'], palettes[rid])
            self.assertTrue(session.departures[rid]['failed'])

    # 한 번에 받은 완료 결과도 8명에서 끊기지 않고 순서대로 표시
    def test_completed_results_continue_beyond_eight_customers(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply([{'type': 'request.succeeded', 'request_id': f'r-{i}', 'customer_id': f'c-{i}', 'data': {}}
                       for i in range(24)])
        for _ in range(12):
            world.update(.61)
        self.assertGreater(len(session.departures), 8)
        self.assertTrue(session.pending_departures[False])
        session.run = snapshot(status='completed')
        world.update(.1)
        self.assertFalse(session.pending_departures[False])
        remaining = set(session.departures)
        world.update(1)
        self.assertTrue(set(session.departures).issubset(remaining))

    # 실험이 끝나면 하단 시작 안내를 완료 결과로 변경
    def test_footer_displays_experiment_completion(self):
        completed = snapshot('r', 'completed')
        completed['client_counts'].update(success=14, failed=3)

        # 완료된 실험과 마지막 이벤트 응답 준비
        def api(method, path, body=None):
            if '/events?' in path:
                return {'events': [], 'next_after_seq': 0, 'has_more': False}
            return completed

        session = Session('http://127.0.0.1:8000', transport=api)
        session.run_id = 'r'
        session.run = snapshot('r', 'running')
        session.message = '실제 상품 검색 부하를 시작합니다.'
        world = TrafficWorld(session)
        session.poll()
        frame = compose(world, 140, 45)
        footer = ui.ANSI.sub('', frame[-1])
        self.assertIn('실험 종료', footer)
        self.assertIn('완료 14건', footer)
        self.assertIn('실패 3건', footer)

    # 고정 캐릭터 줄은 다시 쓰지 않고 변경된 줄만 동기 출력
    def test_frame_updates_only_changed_rows(self):
        old = ['fixed blue employee', 'old footer']
        self.assertEqual(frame_update(old, old), '')
        output = frame_update(['fixed blue employee', '실험 종료'], old)
        self.assertNotIn('fixed blue employee', output)
        self.assertIn('\x1b[2;1H실험 종료', output)
        self.assertTrue(output.startswith('\x1b[?2026h'))
        self.assertTrue(output.endswith('\x1b[?2026l'))

    # 같은 고객은 대기와 안내 및 퇴장 중에도 같은 옷과 모자 유지
    def test_customer_identity_and_marks_follow_actual_status(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply([{'type': 'request.queued', 'request_id': 'r1', 'customer_id': 'c1', 'data': {}}])
        world.update(10)
        palette = world.queue[0].pal.copy()
        self.assertEqual(session.customer_mark(session.requests['r1']), ('…', 214))
        session.apply([{'type': 'request.processing', 'request_id': 'r1', 'customer_id': 'c1', 'data': {}}])
        world.update(.1)
        self.assertFalse(world.queue)
        self.assertEqual(session.serving['r1']['palette'], palette)
        self.assertEqual(session.customer_mark(session.requests['r1']), ('?', 33))
        session.apply([{'type': 'request.succeeded', 'request_id': 'r1', 'customer_id': 'c1', 'data': {}}])
        world.update(.1)
        self.assertEqual(session.customer_mark(session.requests['r1']), ('+', 46))
        self.assertEqual(session.serving['r1']['palette'], palette)
        world.update(1)
        self.assertEqual(session.departures['r1']['palette'], palette)

    # 줄 뒤쪽에서 끝난 요청은 안내소로 순간 이동하거나 오래 걷지 않음
    def test_distant_processing_customer_exits_from_current_position(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        session.apply([{'type': 'request.queued', 'request_id': f'r{i}', 'customer_id': f'c{i}', 'data': {}}
                       for i in range(12)])
        world.update(10)
        person = session.characters['r2']
        palette = person.pal.copy()
        session.apply([{'type': 'request.processing', 'request_id': 'r2', 'customer_id': 'c2', 'data': {}}])
        world.update(.1)
        self.assertNotIn('r2', session.serving)
        self.assertIs(session.characters['r2'], person)
        position = world.qpos(person.d)
        session.apply([{'type': 'request.succeeded', 'request_id': 'r2', 'customer_id': 'c2', 'data': {}}])
        world.update(.1)
        self.assertNotIn('r2', session.characters)
        self.assertNotIn('r2', session.serving)
        self.assertEqual(session.departures['r2']['palette'], palette)
        self.assertEqual(session.departure_position(world, session.departures['r2']), position)

    # 병정은 양쪽 노점 끝에 정렬하고 상태 문구는 고객 동선 밖에 표시
    def test_decorations_and_status_stay_outside_customer_routes(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        with patch.object(ui.PixelCanvas, 'blit', autospec=True, side_effect=ui.PixelCanvas.blit) as blit:
            frame = compose(world, 140, 45)
        guards = [call.args[2:4] for call in blit.call_args_list if call.args[1] == ui.SOLDIER]
        self.assertEqual(guards, [(2, world.gpx - 13), (world.w - 9, world.gpx - 13)])
        self.assertTrue(all(x >= 11 and x + 9 <= world.w - 10 for x, name, _ in world.booths))
        self.assertNotIn('마켓 개장 준비', ui.ANSI.sub('', frame[2]))
        scene = ui.ANSI.sub('', '\n'.join(frame[3:-6]))
        self.assertIn('마켓 개장 준비', scene)
        self.assertIn('버그마스 마켓 안내소', scene)
        self.assertEqual(world.draw().get(world.w - 9, 6), 230)
        self.assertEqual(world.qpos(world.qvis), (world.guide_front[0], world.bottom + 1))

    # 상점은 트리 양쪽에 같은 크기로 대칭 배치하고 안내소는 중앙에 위치
    def test_market_layout_is_symmetric_at_supported_widths(self):
        world = TrafficWorld(Session('http://127.0.0.1:8000'))
        for width in (60, 82, 100, 140, 180):
            compose(world, width, 45)
            with self.subTest(width=width):
                self.assertEqual(len(world.booths) % 2, 0)
                for left, right in zip(world.booths[::2], world.booths[1::2]):
                    self.assertEqual(left[0] + right[0] + 9, world.w)
                self.assertLess(abs(world.guide_front[0] - world.w / 2), 3)
                for x, _, _ in world.booths:
                    self.assertTrue(x + 8 < world.tree_cx - 9 or x > world.tree_cx + 9)
                self.assertLess(world.gpx + 14 + 5, world.bottom - 20)
                for category in ('cookie', 'hot_choco', 'gift', 'toy', 'ornament'):
                    target = world.shop_target(category)
                    self.assertGreaterEqual(target[1] - 2, world.gpx + 2)

    # 완료가 먼저 관측돼도 입장과 안내소 및 요청 상품 상점을 차례로 방문
    def test_success_route_visits_guide_then_requested_shop(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        for category in ('cookie', 'hot_choco', 'gift', 'toy', 'ornament'):
            person = {'start': world.entrance, 'via_guide': True,
                      'category': category, 'failed': False, 'born': world.t}
            route = session.departure_route(world, person)
            target = world.shop_target(category)
            self.assertEqual(route[0], world.entrance)
            self.assertEqual(route[1], world.guide_front)
            self.assertIn(target, route[2:])
            self.assertGreater(route.index(target), route.index(world.guide_front))
            distance = sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) / 2
                           for a, b in zip(route[:5], route[1:6]))
            world.t = person['born'] + distance / 14 + .21
            self.assertEqual(session.departure_position(world, person), target)

    # 실패한 고객은 상점을 방문하지 않고 화면 안에서 이탈 경로로 이동
    def test_failed_customer_skips_shop_and_remains_inside_vertical_bounds(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 140, 45)
        person = {'start': world.guide_front, 'via_guide': False,
                  'category': 'cookie', 'failed': True, 'born': 0}
        route = session.departure_route(world, person)
        self.assertNotIn(world.shop_target('cookie'), route)
        self.assertTrue(all(world.gpx + 4 <= y <= world.bottom - 5 for _, y in route))
        self.assertEqual(route[-1][0], -4)

    # 넓은 화면에서 상점 네 개씩을 다섯 칸 간격으로 대칭 배치
    def test_eight_shops_have_spacing_on_both_sides(self):
        world = TrafficWorld(Session('http://127.0.0.1:8000'))
        compose(world, 180, 45)
        self.assertEqual(len(world.booths), 8)
        left = sorted(x for x, _, _ in world.booths if x < world.tree_cx)
        right = sorted(x for x, _, _ in world.booths if x > world.tree_cx)
        self.assertEqual(len(left), 4)
        self.assertEqual(len(right), 4)
        for positions in (left, right):
            self.assertTrue(all(b - a - 9 == 5 for a, b in zip(positions, positions[1:])))
        self.assertGreaterEqual(left[0], 11)
        self.assertLessEqual(right[-1] + 9, world.w - 10)

    # 같은 상품 종류의 상점이 여러 개면 요청별로 방문 상점을 분산
    def test_repeated_shop_categories_receive_customers(self):
        world = TrafficWorld(Session('http://127.0.0.1:8000'))
        compose(world, 180, 45)
        for category in ('gift', 'toy', 'cookie'):
            targets = {world.shop_target(category, f'r-{i}') for i in range(10)}
            self.assertEqual(len(targets), 2)

    # 고객은 안내소 아래에서 수직으로 입장하며 안내 전에는 결과 표시를 숨김
    def test_vertical_entry_hides_results_until_guide_visit(self):
        session = Session('http://127.0.0.1:8000')
        world = TrafficWorld(session)
        compose(world, 180, 45)
        person = {'start': world.entrance, 'via_guide': True, 'category': 'cookie',
                  'failed': False, 'born': world.t}
        distance = (world.entrance[1] - world.guide_front[1]) / 2
        self.assertFalse(session.customer_mark_visible(world, person))
        world.t += distance / 28
        x, y = session.departure_position(world, person)
        self.assertEqual(x, world.guide_front[0])
        self.assertGreater(y, world.guide_front[1])
        self.assertFalse(session.customer_mark_visible(world, person))
        world.t = person['born'] + distance / 14 + .21
        self.assertTrue(session.customer_mark_visible(world, person))
        self.assertEqual(session.departure_route(world, person)[1], world.guide_front)

    # 상점 이름은 글자 폭을 기준으로 간판 가운데에 배치
    def test_shop_names_are_centered_in_signs(self):
        world = TrafficWorld(Session('http://127.0.0.1:8000'))
        compose(world, 180, 45)
        canvas = world.draw()
        row = (world.gpx - 8 + 4) // 2
        for x, name, _ in world.booths:
            start = x + (9 - ui.dw(name)) // 2
            text = ''.join(canvas.text[(start + i, row)][0] for i in range(len(name)))
            self.assertEqual(text, name)
            self.assertLessEqual(abs((start - x) - (9 - ui.dw(name) - (start - x))), 1)

    # 상점 이름은 중복 없이 정확한 가운데 정렬이 가능한 폭으로 표시
    def test_eight_shop_names_are_distinct_and_centered(self):
        world = TrafficWorld(Session('http://127.0.0.1:8000'))
        compose(world, 180, 45)
        names = [name for _, name, _ in world.booths]
        self.assertEqual(len(set(names)), 8)
        self.assertIn('TOY', names)
        self.assertIn('DOLLS', names)
        self.assertTrue(all((9 - ui.dw(name)) % 2 == 0 for name in names))

    # 좌우에 지붕 색 네 가지를 하나씩 배치
    def test_roof_colors_are_evenly_distributed_on_each_side(self):
        world = TrafficWorld(Session('http://127.0.0.1:8000'))
        compose(world, 180, 45)
        for shops in (world.booths[::2], world.booths[1::2]):
            self.assertEqual({stripes for _, _, stripes in shops}, set(ui.BOOTH_STRIPES))

    # 빠르게 사라진 대기도 이벤트에서 최대 인원을 보존하고 다음 실험에서 초기화
    def test_peak_waiting_remains_visible_after_queue_drains(self):
        session = Session('http://127.0.0.1:8000', transport=lambda *args: {'run_id': 'new-run'})
        session.run_id = 'run-1'
        session.run = snapshot(status='completed')
        session.apply([{'type': 'request.queued', 'request_id': 'r', 'customer_id': 'c', 'data': {'queue_size': 7}},
                       {'type': 'request.succeeded', 'request_id': 'r', 'customer_id': 'c', 'data': {}}])
        world = TrafficWorld(session)
        world.update(.1)
        frame = ui.ANSI.sub('', '\n'.join(compose(world, 180, 45)))
        self.assertIn('현재 대기 0명', frame)
        self.assertIn('관측 최대 7명', frame)
        self.assertEqual(session.status()['peak_waiting'], 7)
        session.execute(['surge'])
        self.assertEqual(session.status()['peak_waiting'], 0)


if __name__ == '__main__':
    unittest.main()
