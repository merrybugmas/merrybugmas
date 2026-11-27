import asyncio
import hashlib
import json
import os
import platform
import random
import sqlite3
import sys
import uuid

import httpx

from . import catalog
from .common import ApiError, canonical, digest
from .config import ROOT


class Manager:
    # 실험 실행 관리에 필요한 상태 준비
    def __init__(self, settings, store):
        self.s, self.store = settings, store
        self.lock = asyncio.Lock()
        self.target = None
        self.processes = {}
        self.tasks = set()
        self.restarting = False
        self.client = httpx.AsyncClient(timeout=2, trust_env=False)
        self.target_log = None

    # 검색 서버와 부하 실행기 환경 설정
    def env(self):
        return {**os.environ, "MBM_DATA_DIR": str(self.s.data_dir), "MBM_CONTROL_URL": self.s.control_url,
                "MBM_TARGET_URL": self.s.target_url, "MBM_CONCURRENCY": str(self.s.concurrency),
                "MBM_QUEUE_LIMIT": str(self.s.queue_limit), "MBM_QUEUE_TIMEOUT_MS": str(self.s.queue_timeout_ms),
                "MBM_PRODUCT_COUNT": str(self.s.product_count), "PYTHONUNBUFFERED": "1"}

    # 백그라운드 작업 등록
    def spawn_task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

        return task

    # 검색 서버 준비 상태 확인
    async def health(self):
        try:
            r = await self.client.get(self.s.target_url + "/health")
            r.raise_for_status()

            return r.json()
        except (httpx.HTTPError, ValueError):
            raise ApiError(503, "SERVICE_UNAVAILABLE", "안내 검색 서버가 준비되지 않았습니다.")

    # 검색 서버 프로세스 시작
    async def start_target(self):
        if not self.s.manage_target:
            return

        self.target_log = (self.s.data_dir / "target-process.log").open("a")
        self.target = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "uvicorn", "backend.target:create_app", "--factory", "--host", "127.0.0.1",
            "--port", str(self.s.target_port), "--log-level", "warning", cwd=ROOT, env=self.env(),
            stdout=self.target_log, stderr=asyncio.subprocess.STDOUT)

        for _ in range(100):
            if self.target.returncode is not None:
                raise ApiError(503, "SERVICE_UNAVAILABLE", "검색 서버 프로세스가 종료됐습니다. 로그를 확인하세요.")

            try:
                await self.health()
                # 다른 서버가 사용 중인 포트를 정상 시작으로 오인하지 않도록 확인
                await asyncio.sleep(0.05)

                if self.target.returncode is not None:
                    raise ApiError(503, "SERVICE_UNAVAILABLE", "검색 서버 포트를 사용할 수 없습니다.")

                return
            except ApiError:
                await asyncio.sleep(0.1)

        await self.stop_target()
        raise ApiError(503, "SERVICE_UNAVAILABLE", "검색 서버 시작 시간이 초과됐습니다.")

    # 검색 서버 프로세스 종료
    async def stop_target(self):
        if self.target and self.target.returncode is None:
            self.target.terminate()

            try:
                await asyncio.wait_for(self.target.wait(), 10)
            except TimeoutError:
                self.target.kill()
                await self.target.wait()

        self.target = None

        if self.target_log:
            self.target_log.close()
            self.target_log = None

    # 실험 당시 코드와 DB 조건 기록
    def artifact(self, plan):
        h = hashlib.sha256()

        for path in sorted((ROOT / "backend").glob("*.py")):
            if path.name in {"target.py", "catalog.py"}:
                h.update(path.name.encode() + path.read_bytes())

        return {"code_revision": "sha256:" + h.hexdigest(), "schema_hash": digest(catalog.schema(self.s.catalog)),
                "dataset_hash": catalog.dataset_hash(self.s.catalog), "request_plan_hash": digest(plan),
                "runtime_config_hash": digest({"concurrency": self.s.concurrency, "queue_limit": self.s.queue_limit,
                    "queue_timeout_ms": self.s.queue_timeout_ms, "max_inflight": self.s.max_inflight,
                    "workers": 1, "python": sys.version, "sqlite": sqlite3.sqlite_version}),
                "warmup_policy": "reference-query-warmup-v1"}

    # 요청 계획 생성과 실험 시작
    async def start_run(self, body):
        async with self.lock:
            if self.restarting or self.store.busy():
                raise ApiError(409, "RUN_ALREADY_ACTIVE", "활성 실험 또는 미회수 서버 요청이 있습니다.")

            health = await self.health()

            if health["active"] or health["queued"] or health.get("telemetry_error"):
                raise ApiError(409, "RUN_ALREADY_ACTIVE", "검색 서버의 이전 작업 또는 수집 오류를 확인하세요.")

            source_id = body.source_run_id

            if source_id:
                source = self.store.raw(source_id)

                if source["status"] != "completed":
                    raise ApiError(409, "INVALID_RUN_STATE", "완료된 실험만 재실행할 수 있습니다.")

                config, plan = source["config"], source["plan"]
            else:
                config = body.model_dump(exclude={"source_run_id"})
                rng = random.Random(body.seed)
                plan = [{"customer_id": f"customer-{i+1:04d}",
                         "query": {"category": rng.choice(catalog.CATEGORIES), "max_price": rng.choice([10000, 20000, 30000, 50000])},
                         "scheduled_offset_ms": i * body.arrival_duration_s * 1000 / body.request_count}
                        for i in range(body.request_count)]

            artifact = await asyncio.to_thread(self.artifact, plan)

            if source_id:
                for key in ("dataset_hash", "runtime_config_hash", "warmup_policy"):
                    if artifact[key] != source["artifact"][key]:
                        raise ApiError(409, "INCOMPATIBLE_RUNS", "재실험의 고정 조건이 다릅니다.")

            # 기준 정답 조회로 개선 전후에 같은 워밍업 적용
            # 검색 결과 검증용 정답 저장
            def expected_answers():
                unique = {canonical(p["query"]): p["query"] for p in plan}
                return {key: catalog.search(self.s.catalog, **query) for key, query in unique.items()}

            expected = await asyncio.to_thread(expected_answers)
            run_id = "run-" + uuid.uuid4().hex[:20]
            r = await asyncio.to_thread(self.store.create, run_id, config, plan, artifact, source_id, expected)
            folder = self.s.runs_dir / run_id
            (folder / "schema.sql").write_text(catalog.schema(self.s.catalog))
            (folder / "query-plan.json").write_text(json.dumps(catalog.plan(self.s.catalog), indent=2))
            (folder / "environment.json").write_text(json.dumps({"python": sys.version, "platform": platform.platform(),
                "cpu_count": os.cpu_count(), "settings": {"concurrency": self.s.concurrency, "queue_limit": self.s.queue_limit,
                "queue_timeout_ms": self.s.queue_timeout_ms, "max_inflight": self.s.max_inflight}}, indent=2))
            # 동시에 다른 실험이 시작되지 않도록 먼저 대기 상태 등록
            self.spawn_task(self.launch(run_id))

            return {"run_id": run_id, "status": "pending", "source_run_id": source_id, "created_at": r["created_at"],
                    "config": config, "request_plan_hash": artifact["request_plan_hash"], "status_url": f"/api/runs/{run_id}"}

    # 부하 실행기 시작과 결과 회수
    async def launch(self, run_id):
        log = (self.s.runs_dir / run_id / "loadgen-process.log").open("a")
        proc = None

        try:
            if self.store.raw(run_id)["status"] == "stopping":
                from .models import MeasurementBatch

                count = self.store.raw(run_id)["config"]["request_count"]
                self.store.measurements_ingest(run_id, MeasurementBatch(batch_id="stopped-before-launch", measurements=[], final=True,
                    summary={"planned": count, "sent": 0, "dropped": count, "elapsed_ms": .001, "termination": "stopped"}))
                # 전송 전 중지된 실험도 마지막 서버 이벤트 수집 처리
                await self.client.post(f"{self.s.target_url}/internal/seal/{run_id}")

                return

            self.store.mark(run_id, "running", "기준 요청 계획으로 부하 발생기를 실행합니다.")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "backend.loadgen", "--run-id", run_id, "--folder", str(self.s.runs_dir / run_id),
                "--target", self.s.target_url, "--control", self.s.control_url, "--max-inflight", str(self.s.max_inflight),
                cwd=ROOT, env=self.env(), stdout=log, stderr=asyncio.subprocess.STDOUT)
            self.processes[run_id] = proc

            if self.store.raw(run_id)["status"] == "stopping":
                (self.s.runs_dir / run_id / "stop-requested").touch()

            code = await proc.wait()

            if code != 0 or not self.store.raw(run_id)["measurement_final"]:
                self.store.mark(run_id, "failed", f"발생기가 종료됐습니다(code={code}). 원본과 outbox를 확인하세요.")

            # 고객이 시간 초과돼도 남아 있는 서버 작업은 별도로 확인
            for _ in range(max(100, self.s.queue_timeout_ms // 100 + 100)):
                response = await self.client.post(f"{self.s.target_url}/internal/seal/{run_id}")

                if response.status_code == 200:
                    break

                await asyncio.sleep(0.1)
            else:
                with self.store.tx() as c:
                    self.store.log(c, run_id, "서버 최종 이벤트를 아직 회수하지 못했습니다.", level="WARNING")

            for _ in range(100):
                if self.store.raw(run_id)["telemetry_complete"]:
                    break

                await asyncio.sleep(0.1)

            self.store.export(run_id)
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.terminate()

                try:
                    await asyncio.wait_for(proc.wait(), 10)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()

            raise
        except Exception as exc:
            self.store.mark(run_id, "failed", f"실험 실행 오류: {type(exc).__name__}")
        finally:
            self.processes.pop(run_id, None)
            log.close()

    # 추가 요청 전송 중지
    async def stop(self, run_id):
        response = self.store.stop(run_id)
        (self.s.runs_dir / run_id / "stop-requested").touch()

        return response

    # 검색 서버 재시작 요청 접수
    async def restart(self, body):
        async with self.lock:
            run = self.store.raw(body.run_id)

            if self.restarting:
                raise ApiError(409, "RESTART_IN_PROGRESS", "재시작 중입니다.")

            if run["status"] != "completed" or self.store.busy():
                raise ApiError(409, "ACTIVE_RUN_CONFLICT", "완료된 기준 실험과 서버 요청 회수가 필요합니다.")

            if not self.s.manage_target:
                raise ApiError(503, "SERVICE_UNAVAILABLE", "외부 관리 모드에서는 서버 재시작을 지원하지 않습니다.")

            health = await self.health()

            if health["active"] or health["queued"]:
                raise ApiError(409, "ACTIVE_RUN_CONFLICT", "서버 요청이 남아 있습니다.")

            self.restarting = True
            op = "restart-" + uuid.uuid4().hex[:16]
            self.spawn_task(self._restart(body.run_id, op, body.reason))

            return {"operation_id": op, "status": "restarting"}

    # 검색 서버 재시작과 결과 기록
    async def _restart(self, run_id, op, reason):
        # 서버 재시작 상태 기록
        def emit(type_, status, message):
            with self.store.tx() as c:
                self.store.event(c, run_id, type_, {"operation_id": op, "status": status, "message": message})
                self.store.log(c, run_id, message)

        try:
            emit("service.restarting", "restarting", reason or "안내 검색 서버 재시작")
            await self.stop_target()
            await self.start_target()
            emit("service.restarted", "ready", "안내 검색 서버 재시작 완료")
        except Exception:
            emit("service.restart_failed", "failed", "서버 재시작 실패. 프로세스 로그를 확인하세요.")
        finally:
            self.restarting = False

    # 실험 작업과 자식 프로세스 정리
    async def close(self):
        for task in list(self.tasks):
            task.cancel()

        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        await self.stop_target()
        await self.client.aclose()
