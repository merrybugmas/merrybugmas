import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from typing import Annotated

import httpx
from fastapi import FastAPI, Header, Query, Response

from . import catalog
from .common import ApiError, canonical, install_errors, now
from .config import Settings
from .models import Category, ERROR_RESPONSES, ID, SearchQuery, SearchResponse


class Telemetry:
    # 검색 응답과 분리해서 상태 이벤트 전송
    # 서버 이벤트 전송에 필요한 상태 준비
    def __init__(self, settings):
        self.settings = settings
        self.queue = asyncio.Queue()
        self.pending = settings.data_dir / "target-outbox"
        self.pending.mkdir(exist_ok=True)
        self.client = None
        self.task = None
        self.error = None

    # 서버 이벤트 전송 작업 시작
    async def start(self):
        self.client = httpx.AsyncClient(timeout=3, trust_env=False)
        self.task = asyncio.create_task(self.worker())

    # 요청 상태 이벤트를 전송 대기열에 추가
    def emit(self, run_id, event):
        self.queue.put_nowait((run_id, event, False))

    # 실험의 마지막 이벤트 배치 준비
    def seal(self, run_id):
        self.queue.put_nowait((run_id, None, True))

    # 서버 이벤트 묶음 전송과 재시도
    async def worker(self):
        while True:
            files = sorted(self.pending.glob("*.json"))

            if not files:
                run_id, event, final = await self.queue.get()
                entries = [(run_id, event, final)]
                # 검색 요청과 별개로 이벤트를 짧게 모아서 전송
                await asyncio.sleep(0.02)

                while not self.queue.empty() and len(entries) < 200:
                    entries.append(self.queue.get_nowait())

                groups = []

                for rid, evt, end in entries:
                    if not groups or groups[-1][0] != rid or groups[-1][2]:
                        groups.append([rid, [], False])

                    if evt is not None:
                        groups[-1][1].append(evt)

                    groups[-1][2] = end

                for rid, events, end in groups:
                    batch_id = "target-" + uuid.uuid4().hex
                    value = {"run_id": rid, "body": {"batch_id": batch_id, "events": events, "final": end}}
                    path = self.pending / f"{time.time_ns()}-{batch_id}.json"
                    path.write_text(canonical(value))

                for _ in entries:
                    self.queue.task_done()

                files = sorted(self.pending.glob("*.json"))

            for path in files:
                value = json.loads(path.read_text())

                try:
                    response = await self.client.post(f"{self.settings.control_url}/internal/runs/{value['run_id']}/events", json=value["body"])
                    response.raise_for_status()
                    path.unlink()
                    self.error = None
                except (httpx.HTTPError, OSError) as exc:
                    self.error = str(exc)
                    logging.getLogger("mbm.target").warning("Telemetry delivery retained for retry: %s", exc)
                    await asyncio.sleep(0.5)
                    break

    # 서버 이벤트 전송 작업 정리
    async def close(self):
        if self.task:
            try:
                await asyncio.wait_for(self.queue.join(), 5)
            except TimeoutError:
                pass

            # 미전송 배치 파일은 재시작 후 재시도를 위해 보관
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

        if self.client:
            await self.client.aclose()


class Admission:
    # 동시 검색 수와 대기열 제한 준비
    def __init__(self, settings):
        self.limit, self.capacity = settings.queue_limit, settings.concurrency
        self.semaphore = asyncio.Semaphore(settings.concurrency)
        self.waiting = self.active = 0
        self.lock = asyncio.Lock()

    # 검색 처리 대기 자리 확보
    async def reserve(self):
        async with self.lock:
            if self.active + self.waiting >= self.capacity + self.limit:
                raise ApiError(429, "QUEUE_FULL", "안내소 대기열이 가득 찼습니다.")

            self.waiting += 1

            return self.waiting


# 안내 검색 서버 생성
def create_app(settings=None, telemetry=None):
    s = settings or Settings()
    sink = telemetry or Telemetry(s)
    admission = Admission(s)
    pool = ThreadPoolExecutor(max_workers=s.concurrency, thread_name_prefix="guide-search")
    active_by_run = {}
    request_ids = set()
    sealed = set()

    # 상품 DB와 검색 작업 자원 관리
    @asynccontextmanager
    async def lifespan(app):
        try:
            await asyncio.to_thread(catalog.initialize, s.catalog, s.product_count)
            await sink.start()
            yield
        finally:
            try:
                await sink.close()
            finally:
                pool.shutdown(wait=True)

    app = FastAPI(title="Merry Bug Mas 안내 검색 서버", version="0.1.0", lifespan=lifespan, responses=ERROR_RESPONSES)
    install_errors(app)
    app.state.admission, app.state.telemetry = admission, sink

    # 검색 서버 상태 확인 엔드포인트
    @app.get("/health", tags=["운영"])
    async def health():
        return {"status": "ok", "active": admission.active, "queued": admission.waiting,
                "runs_active": active_by_run, "telemetry_error": getattr(sink, "error", None)}

    # 서버 이벤트 수집 종료 엔드포인트
    @app.post("/internal/seal/{run_id}", tags=["운영"])
    async def seal(run_id: ID):
        if active_by_run.get(run_id, 0):
            raise ApiError(409, "ACTIVE_RUN_CONFLICT", "서버 요청이 남아 있습니다.")

        if run_id not in sealed:
            sink.seal(run_id)
            sealed.add(run_id)

        return {"accepted": True}

    # 상품 안내 검색 엔드포인트
    @app.get("/api/guide/search", response_model=SearchResponse, operation_id="searchGuide", tags=["API-01"])
    async def search(response: Response, category: Category, max_price: Annotated[int, Query(ge=0, le=1000000)],
                     run_id: Annotated[ID, Header(alias="X-Run-ID")],
                     customer_id: Annotated[ID, Header(alias="X-Customer-ID")],
                     request_id: Annotated[ID, Header(alias="X-Request-ID")]):
        if (run_id, request_id) in request_ids or run_id in sealed:
            raise ApiError(422, "VALIDATION_ERROR", "요청 ID 중복 또는 종료된 실험입니다.", request_id)

        request_ids.add((run_id, request_id))
        active_by_run[run_id] = active_by_run.get(run_id, 0) + 1
        received = time.monotonic()
        started = None
        reserved = acquired = False

        # 현재 요청의 상태 이벤트 생성
        def emit(type_, data):
            sink.emit(run_id, {"source_event_id": "event-" + uuid.uuid4().hex, "type": "request." + type_,
                               "request_id": request_id, "customer_id": customer_id, "occurred_at": now(), "data": data})

        # 실패 요청의 처리 시간 계산
        def failed(code):
            return {"error_code": code, "queue_wait_ms": (started - received) * 1000 if started else None,
                    "processing_ms": (time.monotonic() - started) * 1000 if started else None,
                    "server_total_ms": (time.monotonic() - received) * 1000}

        emit("received", {"query": SearchQuery(category=category, max_price=max_price).model_dump()})

        try:
            size = await admission.reserve()
            reserved = True
            emit("queued", {"queue_size": size})

            try:
                await asyncio.wait_for(admission.semaphore.acquire(), s.queue_timeout_ms / 1000)
            except TimeoutError:
                raise ApiError(504, "QUEUE_TIMEOUT", "처리 대기 시간이 초과되었습니다.", request_id)

            acquired = True

            async with admission.lock:
                admission.waiting -= 1
                reserved = False
                admission.active += 1

            started = time.monotonic()
            emit("processing", {"queue_wait_ms": (started - received) * 1000})
            # DB 조회 중에도 다른 API가 응답하도록 별도 스레드에서 실행
            future = asyncio.get_running_loop().run_in_executor(pool, partial(catalog.search, s.catalog, category, max_price))

            try:
                items = await asyncio.shield(future)
            except asyncio.CancelledError:
                # 요청이 취소돼도 실행 중인 DB 조회가 끝날 때까지 처리 자리 유지
                await asyncio.shield(future)
                raise

            # 응답 검증을 통과한 결과만 성공 이벤트로 기록
            result = SearchResponse(request_id=request_id, items=items)
            finished = time.monotonic()
            emit("succeeded", {"queue_wait_ms": (started - received) * 1000,
                               "processing_ms": (finished - started) * 1000,
                               "server_total_ms": (finished - received) * 1000, "items": result.model_dump()["items"]})
            response.headers["X-Request-ID"] = request_id

            return result
        except ApiError as exc:
            emit("failed", failed(exc.code))
            raise
        except asyncio.CancelledError:
            emit("cancelled", failed("CANCELLED"))
            raise
        except Exception:
            emit("failed", failed("INTERNAL_ERROR"))
            raise
        finally:
            async with admission.lock:
                if reserved:
                    admission.waiting -= 1

                if acquired:
                    admission.active -= 1
                    admission.semaphore.release()

            active_by_run[run_id] -= 1

            if not active_by_run[run_id]:
                del active_by_run[run_id]

    return app
