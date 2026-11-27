import asyncio
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Response

from . import catalog, models
from .common import install_errors
from .comparison import compare
from .config import Settings
from .manager import Manager
from .store import Store


# 실험 제어 서버 생성
def create_app(settings=None):
    s = settings or Settings()
    store = Store(s)
    manager = Manager(s, store)

    # 검색 서버 시작과 종료 관리
    @asynccontextmanager
    async def lifespan(app):
        try:
            await asyncio.to_thread(catalog.initialize, s.catalog, s.product_count)
            store.recover(release_target=s.manage_target)
            await manager.start_target()
            yield
        finally:
            await manager.close()

    app = FastAPI(title="Merry Bug Mas 실험 제어 서버", version="0.1.0", lifespan=lifespan, responses=models.ERROR_RESPONSES)
    install_errors(app)
    app.state.store, app.state.manager = store, manager

    # 제어 서버 상태 확인 엔드포인트
    @app.get("/health", tags=["운영"])
    async def health():
        return {"status": "ok", "restarting": manager.restarting}

    # 부하 실험 시작 엔드포인트
    @app.post("/api/runs", status_code=202, response_model=models.RunCreated, operation_id="startRun", tags=["API-02"])
    async def start(body: models.StartRun, response: Response):
        result = await manager.start_run(body)
        response.headers["Location"] = result["status_url"]

        return result

    # 부하 실험 중지 엔드포인트
    @app.post("/api/runs/{run_id}/stop", status_code=202, response_model=models.RunStopped, operation_id="stopRun", tags=["API-03"])
    async def stop(run_id: models.ID):
        return await manager.stop(run_id)

    # 실험 상태 조회 엔드포인트
    @app.get("/api/runs/{run_id}", response_model=models.RunSnapshot, operation_id="getRun", tags=["API-04"])
    async def get_run(run_id: models.ID):
        return await asyncio.to_thread(store.snapshot, run_id)

    # 마켓 이벤트 조회 엔드포인트
    @app.get("/api/runs/{run_id}/events", response_model=models.EventPage, operation_id="getEvents", tags=["API-05"])
    async def events(run_id: models.ID, after_seq: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=1000)] = 200):
        return await asyncio.to_thread(store.events, run_id, after_seq, limit)

    # 고객 요청 상세 조회 엔드포인트
    @app.get("/api/runs/{run_id}/requests/{request_id}", response_model=models.RequestDetail, operation_id="getRequest", tags=["API-06"])
    async def detail(run_id: models.ID, request_id: models.ID):
        return await asyncio.to_thread(store.detail, run_id, request_id)

    # 진단 로그 조회 엔드포인트
    @app.get("/api/runs/{run_id}/logs", response_model=models.LogPage, operation_id="getLogs", tags=["API-07"])
    async def logs(run_id: models.ID, after_seq: Annotated[int, Query(ge=0)] = 0,
                   limit: Annotated[int, Query(ge=1, le=500)] = 100,
                   level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] | None = None):
        return await asyncio.to_thread(store.logs, run_id, after_seq, limit, level)

    # 실험 전후 비교 엔드포인트
    @app.post("/api/comparisons", response_model=models.ComparisonResult, operation_id="compareRuns", tags=["API-08"])
    async def comparison(body: models.Compare):
        return await asyncio.to_thread(compare, store, s, body)

    # 에이전트 작업 단계 전달 엔드포인트
    @app.post("/api/agent/progress", response_model=models.ProgressResult, operation_id="agentProgress", tags=["API-09"])
    async def progress(body: models.Progress):
        return await asyncio.to_thread(store.progress, body)

    # 검색 서버 재시작 엔드포인트
    @app.post("/api/service/restart", status_code=202, response_model=models.RestartResult, operation_id="restartService", tags=["API-10"])
    async def restart(body: models.Restart):
        return await manager.restart(body)

    # 서버 이벤트 수집 엔드포인트
    @app.post("/internal/runs/{run_id}/events", response_model=models.EventReceipt, operation_id="ingestServerEvents", tags=["INT-01"])
    async def ingest_events(run_id: models.ID, body: models.EventBatch):
        return await asyncio.to_thread(store.events_ingest, run_id, body)

    # 부하 측정값 수집 엔드포인트
    @app.post("/internal/runs/{run_id}/measurements", response_model=models.MeasurementReceipt, operation_id="ingestMeasurements", tags=["INT-02"])
    async def ingest_measurements(run_id: models.ID, body: models.MeasurementBatch):
        return await asyncio.to_thread(store.measurements_ingest, run_id, body)

    return app
