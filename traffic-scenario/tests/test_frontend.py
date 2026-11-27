import unittest
from unittest.mock import Mock

from frontend.app import TrafficWorld, compose, handle_input, ui
from frontend.session import Session


# API 명세 형태의 실제 집계 데이터 준비
def snapshot(run_id='run-1', status='running', queued=0):
    return {'run_id': run_id, 'source_run_id': None, 'status': status, 'telemetry_complete': status == 'completed',
            'agent_phase': 'idle', 'server_counts': {'received': queued, 'queued': queued, 'processing': 0, 'succeeded': 0, 'failed': 0, 'cancelled': 0},
            'client_counts': {'planned': 1000, 'sent': queued, 'success': 0, 'failed': 0, 'pending': queued, 'dropped': 0},
            'metrics': {'avg_response_ms': None, 'p95_response_ms': None, 'failure_rate': None, 'success_rps': None}}


class TrafficFrontendTests(unittest.TestCase):
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
        self.assertEqual(session.departure_position(world, person), (world.gate_x - 4, world.bottom - 5))
        world.update(.5)
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


if __name__ == '__main__':
    unittest.main()
