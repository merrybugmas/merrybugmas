import asyncio
import time
from unittest.mock import AsyncMock, Mock
import httpx
import pytest
from backend.control import create_app
from backend.target import create_app as target_app
from backend.models import StartRun
from backend import catalog
from backend import target
from conftest import make_run


class Sink:
    error=None
    def __init__(self):self.events=[]
    async def start(self):pass
    async def close(self):pass
    def emit(self,run,event):self.events.append(event)
    def seal(self,run):pass


async def test_control_validation_and_missing(seeded):
    app=create_app(seeded)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as c:
            r=await c.post('/api/runs',json={"scenario_id":"surge"})
            assert r.status_code==422 and r.json()["error"]["code"]=="VALIDATION_ERROR"
            assert (await c.get('/api/runs/missing')).status_code==404
            assert (await c.get('/api/runs/missing/events?limit=0')).status_code==422
            assert (await c.post('/api/agent/progress',json={"bad":1})).status_code==422
            r=await c.get('/health');assert r.headers['cache-control']=='no-store'


async def test_start_and_stop_contract(monkeypatch,seeded):
    app=create_app(seeded);m=app.state.manager
    async def health():return {"active":0,"queued":0}
    async def no_launch(run_id):pass
    monkeypatch.setattr(m,"health",health);monkeypatch.setattr(m,"launch",no_launch)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as c:
            r=await c.post('/api/runs',json={"scenario_id":"surge","request_count":10,"arrival_duration_s":1,"client_timeout_ms":5000})
            assert r.status_code==202
            rid=r.json()['run_id'];assert r.headers['location']==f'/api/runs/{rid}'
            assert (await c.post('/api/runs',json={"source_run_id":rid})).status_code==409
            stop=await c.post(f'/api/runs/{rid}/stop');assert stop.json()['status']=='stopping'
            assert (await c.post(f'/api/runs/{rid}/stop')).status_code==202


async def test_actual_search_and_headers(seeded):
    sink=Sink();app=target_app(seeded,sink)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as c:
            assert (await c.get('/api/guide/search?category=cookie&max_price=20000')).status_code==422
            r=await c.get('/api/guide/search?category=cookie&max_price=20000',headers={"X-Run-ID":"r","X-Customer-ID":"c","X-Request-ID":"q"})
            assert r.status_code==200 and r.headers['x-request-id']=='q'
            expected=catalog.search(seeded.catalog,'cookie',20000)
            assert r.json()['items']==expected
            assert [e['type'] for e in sink.events]==['request.received','request.queued','request.processing','request.succeeded']
            assert sink.events[-1]['data']['processing_ms']>=0


async def test_queue_timeout_full_and_slot_return(monkeypatch,seeded):
    original=catalog.search
    def slow(*args):
        time.sleep(.15) # Only test fixture: production search has no artificial sleep.
        return original(*args)
    monkeypatch.setattr(catalog,'search',slow)
    sink=Sink();app=target_app(seeded,sink)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as c:
            async def get(i):
                return await c.get('/api/guide/search?category=cookie&max_price=20000',headers={'X-Run-ID':'r','X-Customer-ID':f'c{i}','X-Request-ID':f'q{i}'})
            first=asyncio.create_task(get(1));await asyncio.sleep(.01)
            second=asyncio.create_task(get(2));await asyncio.sleep(.01)
            third=await get(3)
            assert third.status_code==429
            assert (await second).status_code==504
            assert (await first).status_code==200
            assert app.state.admission.active==0 and app.state.admission.waiting==0
            assert (await get(4)).status_code==200


async def test_cancelled_work_releases_slot_after_sql_exit(monkeypatch,seeded):
    original=catalog.search
    def slow(*args):time.sleep(.08);return original(*args)
    monkeypatch.setattr(catalog,'search',slow)
    app=target_app(seeded,Sink())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as c:
            task=asyncio.create_task(c.get('/api/guide/search?category=cookie&max_price=20000',headers={'X-Run-ID':'r','X-Customer-ID':'c','X-Request-ID':'q'}))
            await asyncio.sleep(.02);task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            assert app.state.admission.active==0 and app.state.admission.waiting==0


# 초기화와 실행 및 종료 중 오류가 발생해도 검색 서버 자원 정리
@pytest.mark.parametrize('stage', ['initialize', 'start', 'body', 'close'])
async def test_target_cleanup_on_failure(monkeypatch, seeded, stage):
    pool = Mock()
    monkeypatch.setattr(target, 'ThreadPoolExecutor', Mock(return_value=pool))
    sink = Sink()
    sink.start = AsyncMock(side_effect=RuntimeError(stage) if stage == 'start' else None)
    sink.close = AsyncMock(side_effect=RuntimeError(stage) if stage == 'close' else None)

    if stage == 'initialize':
        monkeypatch.setattr(catalog, 'initialize', Mock(side_effect=RuntimeError(stage)))

    app = target_app(seeded, sink)

    with pytest.raises(RuntimeError, match=stage):
        async with app.router.lifespan_context(app):
            if stage == 'body':
                raise RuntimeError(stage)

    sink.close.assert_awaited_once()
    pool.shutdown.assert_called_once_with(wait=True)


# 상품 초기화 실패 시 제어 서버의 HTTP 연결 정리
async def test_control_cleanup_on_initialization_failure(monkeypatch, seeded):
    app = create_app(seeded)
    manager = app.state.manager
    close = AsyncMock(wraps=manager.close)
    monkeypatch.setattr(manager, 'close', close)
    monkeypatch.setattr(catalog, 'initialize', Mock(side_effect=RuntimeError('initialize')))

    with pytest.raises(RuntimeError, match='initialize'):
        async with app.router.lifespan_context(app):
            pytest.fail('Initialization should have failed')

    close.assert_awaited_once()
    assert manager.client.is_closed


# 잘못된 검색 응답을 성공으로 기록하지 않고 처리 자리 반환
@pytest.mark.parametrize('items', [[{'product_id': -1}], [{}] * 11])
async def test_invalid_search_result_records_failure(monkeypatch, seeded, items):
    monkeypatch.setattr(catalog, 'search', Mock(return_value=items))
    sink = Sink()
    app = target_app(seeded, sink)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            response = await client.get('/api/guide/search?category=cookie&max_price=20000',
                headers={'X-Run-ID': 'r', 'X-Customer-ID': 'c', 'X-Request-ID': 'q'})

    assert response.status_code == 500
    assert response.json()['error']['code'] == 'INTERNAL_ERROR'
    assert sink.events[-1]['type'] == 'request.failed'
    assert not any(event['type'] == 'request.succeeded' for event in sink.events)
    assert app.state.admission.active == 0
    assert app.state.admission.waiting == 0
