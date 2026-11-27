# 실험 제어 서버에서 기록하는 실험 상태 저장소
import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager

from .common import ApiError, canonical, digest, now

ACTIVE = {"pending", "running", "stopping"}
TERMINAL_REQUEST = {"succeeded", "failed", "cancelled"}
TRANSITIONS = {
    None: {"received"}, "received": {"queued", "failed", "cancelled"},
    "queued": {"processing", "failed", "cancelled"},
    "processing": TERMINAL_REQUEST,
    "succeeded": set(), "failed": set(), "cancelled": set(),
}


class Store:
    # 실험 기록용 DB 테이블 준비
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.RLock()

        with self.tx() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, doc TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,type TEXT NOT NULL,request_id TEXT,customer_id TEXT,occurred_at TEXT NOT NULL,data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id,seq);
            CREATE TABLE IF NOT EXISTS requests(run_id TEXT NOT NULL,request_id TEXT NOT NULL,doc TEXT NOT NULL,PRIMARY KEY(run_id,request_id));
            CREATE TABLE IF NOT EXISTS measurements(run_id TEXT NOT NULL,request_id TEXT NOT NULL,customer_id TEXT NOT NULL,doc TEXT NOT NULL,PRIMARY KEY(run_id,request_id),UNIQUE(run_id,customer_id));
            CREATE TABLE IF NOT EXISTS batches(run_id TEXT NOT NULL,source TEXT NOT NULL,batch_id TEXT NOT NULL,hash TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(run_id,source,batch_id));
            CREATE TABLE IF NOT EXISTS source_events(run_id TEXT NOT NULL,event_id TEXT NOT NULL,hash TEXT NOT NULL,PRIMARY KEY(run_id,event_id));
            CREATE TABLE IF NOT EXISTS messages(operation_id TEXT NOT NULL,message_id TEXT NOT NULL,hash TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(operation_id,message_id));
            """)

    # 실험 DB 트랜잭션 처리
    @contextmanager
    def tx(self):
        with self.lock:
            c = sqlite3.connect(self.settings.experiments, timeout=15)
            c.row_factory = sqlite3.Row

            try:
                # 배치 일부가 실패하면 같은 트랜잭션의 변경도 모두 취소
                c.execute("BEGIN IMMEDIATE")
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise
            finally:
                c.close()

    # 실험 정보 DB 조회
    @staticmethod
    def get(c, run_id):
        row = c.execute("SELECT doc FROM runs WHERE id=?", (run_id,)).fetchone()

        if not row:
            raise ApiError(404, "RUN_NOT_FOUND", "실험을 찾을 수 없습니다.")

        return json.loads(row[0])

    # 실험 정보 DB 갱신
    @staticmethod
    def save(c, run):
        c.execute("UPDATE runs SET doc=? WHERE id=?", (canonical(run), run["run_id"]))

    # 마켓 이벤트 DB 저장
    @staticmethod
    def event(c, run_id, type_, data, request_id=None, customer_id=None, occurred_at=None):
        return c.execute("INSERT INTO events(run_id,type,request_id,customer_id,occurred_at,data) VALUES(?,?,?,?,?,?)",
                         (run_id, type_, request_id, customer_id, occurred_at or now(), canonical(data))).lastrowid

    # 진단 로그 DB 저장
    @classmethod
    def log(cls, c, run_id, message, source="control", level="INFO", request_id=None):
        return cls.event(c, run_id, "log", {"source": source, "level": level, "message": message, "request_id": request_id})

    # 진행 중인 실험과 미회수 요청 확인
    def busy(self):
        with self.tx() as c:
            for row in c.execute("SELECT doc FROM runs"):
                run = json.loads(row[0])

                if run["status"] in ACTIVE or (run["target_seen"] and not run["telemetry_complete"] and not run.get("telemetry_interrupted")):
                    return True

            return False

    # 새 실험 생성과 초기 기록 저장
    def create(self, run_id, config, plan, artifact, source_id, expected):
        with self.tx() as c:
            doc = dict(run_id=run_id, status="pending", source_run_id=source_id, created_at=now(),
                       started_at=None, finished_at=None, config=config, artifact=artifact, plan=plan,
                       expected=expected, telemetry_complete=False, target_seen=False,
                       measurement_final=False, summary=None, agent_phase="idle", started_mono=None)
            c.execute("INSERT INTO runs VALUES(?,?)", (run_id, canonical(doc)))
            self.event(c, run_id, "run.pending", {"status": "pending"})
            self.log(c, run_id, "실험이 생성되었습니다.")

        self.export(run_id)

        return doc

    # 실험 원본 정보 조회
    def raw(self, run_id):
        with self.tx() as c:
            return self.get(c, run_id)

    # 실험 상태 변경
    def mark(self, run_id, status, message=None):
        with self.tx() as c:
            r = self.get(c, run_id)

            if r["status"] not in ACTIVE:
                return

            r["status"] = status

            if status == "running":
                r["started_at"], r["started_mono"] = now(), time.monotonic()

            if status in {"failed", "stopped", "completed"}:
                r["finished_at"] = now()

            self.save(c, r)
            self.event(c, run_id, "run." + status, {"status": status})

            if message:
                self.log(c, run_id, message, level="ERROR" if status == "failed" else "INFO")

    # 실험 중지 상태 기록
    def stop(self, run_id):
        with self.tx() as c:
            r = self.get(c, run_id)

            if r["status"] not in ACTIVE:
                raise ApiError(409, "INVALID_RUN_STATE", "이미 종료된 실험입니다.")

            if r["status"] != "stopping":
                r["status"] = "stopping"
                self.save(c, r)
                self.event(c, run_id, "run.stopping", {"status": "stopping"})

            return {"run_id": run_id, "status": "stopping"}

    # 제어 서버 재시작 시 중단된 실험 기록
    def recover(self, release_target=False):
        with self.tx() as c:
            for row in list(c.execute("SELECT doc FROM runs")):
                r = json.loads(row[0])

                if r["status"] in ACTIVE:
                    r["status"], r["finished_at"] = "failed", now()
                    self.save(c, r)
                    self.event(c, r["run_id"], "run.failed", {"status": "failed"})
                    self.log(c, r["run_id"], "제어 프로세스 재시작으로 실험이 중단됐습니다. 원본을 확인하세요.", level="ERROR")

                if release_target and r["target_seen"] and not r["telemetry_complete"] and not r.get("telemetry_interrupted"):
                    # 검색 서버를 새로 시작할 때만 이전 서버의 미회수 요청 잠금 해제
                    r["telemetry_interrupted"] = True
                    self.save(c, r)
                    self.log(c, r["run_id"], "검색 서버 재시작으로 이전 이벤트 수집이 중단됐습니다. 기록을 보존하며 비교 대상에서 제외합니다.", level="WARNING")

    # 수집 배치의 중복 여부 확인
    @staticmethod
    def _batch(c, run_id, source, batch):
        data = batch.model_dump(mode="json")
        h = digest(data)
        row = c.execute("SELECT hash,response FROM batches WHERE run_id=? AND source=? AND batch_id=?", (run_id, source, batch.batch_id)).fetchone()

        if row:
            if row[0] != h:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "같은 배치 ID의 본문이 다릅니다.")

            return h, json.loads(row[1])

        return h, None

    # 배치 처리 결과 저장
    @staticmethod
    def _save_batch(c, run_id, source, batch_id, h, response):
        c.execute("INSERT INTO batches VALUES(?,?,?,?,?)", (run_id, source, batch_id, h, canonical(response)))

    # 검색 서버 이벤트 검증과 저장
    def events_ingest(self, run_id, batch):
        with self.tx() as c:
            r = self.get(c, run_id)
            h, previous = self._batch(c, run_id, "target", batch)

            if previous is not None:
                return previous

            accepted = duplicate = 0
            planned = {p["customer_id"]: p for p in r["plan"]}

            for evt in batch.events:
                value = evt.model_dump(mode="json")
                eh = digest(value)
                old = c.execute("SELECT hash FROM source_events WHERE run_id=? AND event_id=?", (run_id, evt.source_event_id)).fetchone()

                if old:
                    if old[0] != eh:
                        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "같은 이벤트 ID의 본문이 다릅니다.")

                    duplicate += 1
                    continue

                if r["telemetry_complete"]:
                    raise ApiError(409, "INVALID_EVENT_TRANSITION", "최종 수집 이후 신규 이벤트입니다.")

                if evt.customer_id not in planned:
                    raise ApiError(409, "INVALID_EVENT_TRANSITION", "계획에 없는 고객입니다.")

                row = c.execute("SELECT doc FROM requests WHERE run_id=? AND request_id=?", (run_id, evt.request_id)).fetchone()
                request = json.loads(row[0]) if row else {}
                current = request.get("server_status")
                status = evt.type.split(".")[1]

                if status not in TRANSITIONS.get(current, set()):
                    raise ApiError(409, "INVALID_EVENT_TRANSITION", "요청 상태 전환이 올바르지 않습니다.")

                if request and request["customer_id"] != evt.customer_id:
                    raise ApiError(409, "INVALID_EVENT_TRANSITION", "요청의 고객이 다릅니다.")

                if status == "received":
                    other = c.execute("SELECT 1 FROM requests WHERE run_id=? AND json_extract(doc,'$.customer_id')=?", (run_id, evt.customer_id)).fetchone()

                    if other or evt.data["query"] != planned[evt.customer_id]["query"]:
                        raise ApiError(409, "INVALID_EVENT_TRANSITION", "고객 중복 또는 검색 조건 불일치입니다.")

                    request = {"run_id": run_id, "request_id": evt.request_id, "customer_id": evt.customer_id,
                               "query": evt.data["query"], "received_at": value["occurred_at"],
                               "started_at": None, "finished_at": None, "queue_wait_ms": None,
                               "processing_ms": None, "server_total_ms": None, "error_code": None, "items": None}

                request["server_status"] = status

                if status == "queued":
                    request["queued_mono"] = time.monotonic()

                if status == "processing":
                    request["started_at"] = value["occurred_at"]

                if status in TERMINAL_REQUEST:
                    request["finished_at"] = value["occurred_at"]

                for key in ("queue_wait_ms", "processing_ms", "server_total_ms", "error_code", "items"):
                    if key in evt.data:
                        request[key] = evt.data[key]

                c.execute("INSERT INTO requests VALUES(?,?,?) ON CONFLICT(run_id,request_id) DO UPDATE SET doc=excluded.doc", (run_id, evt.request_id, canonical(request)))
                c.execute("INSERT INTO source_events VALUES(?,?,?)", (run_id, evt.source_event_id, eh))
                self.event(c, run_id, evt.type, evt.data, evt.request_id, evt.customer_id, value["occurred_at"])
                self.log(c, run_id, evt.type, "target", request_id=evt.request_id)
                accepted += 1

            if batch.events:
                r["target_seen"] = True

            if batch.final:
                pending = c.execute("SELECT COUNT(*) FROM requests WHERE run_id=? AND json_extract(doc,'$.server_status') IN ('received','queued','processing')", (run_id,)).fetchone()[0]

                if pending:
                    raise ApiError(409, "INVALID_EVENT_TRANSITION", "아직 처리 중인 서버 요청이 있습니다.")

                r["telemetry_complete"] = True

            self.save(c, r)
            last = c.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
            response = {"accepted_count": accepted, "duplicate_count": duplicate, "last_seq": last, "final_received": r["telemetry_complete"]}
            self._save_batch(c, run_id, "target", batch.batch_id, h, response)

        self.save_batch_file(run_id, "target", batch.batch_id, batch.model_dump(mode="json"))

        return response

    # 고객 측정값 검증과 저장
    def measurements_ingest(self, run_id, batch):
        with self.tx() as c:
            r = self.get(c, run_id)
            h, previous = self._batch(c, run_id, "loadgen", batch)

            if previous is not None:
                return previous

            planned = {p["customer_id"]: p for p in r["plan"]}
            accepted = duplicate = 0

            for m in batch.measurements:
                value = m.model_dump(mode="json")
                old = c.execute("SELECT doc FROM measurements WHERE run_id=? AND request_id=?", (run_id, m.request_id)).fetchone()

                if old:
                    if old[0] != canonical(value):
                        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "같은 요청의 최종 결과가 다릅니다.")

                    duplicate += 1
                    continue

                if r["measurement_final"]:
                    raise ApiError(409, "INVALID_MEASUREMENT_STATE", "최종 수집 이후 신규 결과입니다.")

                plan = planned.get(m.customer_id)

                if not plan or plan["query"] != value["query"] or abs(plan["scheduled_offset_ms"] - m.scheduled_offset_ms) > 0.01:
                    raise ApiError(409, "INVALID_MEASUREMENT_STATE", "요청 계획과 측정 결과가 다릅니다.")

                other = c.execute("SELECT 1 FROM measurements WHERE run_id=? AND customer_id=?", (run_id, m.customer_id)).fetchone()

                if other:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "한 고객의 결과가 중복됐습니다.")

                c.execute("INSERT INTO measurements VALUES(?,?,?,?)", (run_id, m.request_id, m.customer_id, canonical(value)))
                self.event(c, run_id, "client.result", value, m.request_id, m.customer_id)
                accepted += 1

            if batch.final:
                s = batch.summary.model_dump()
                count = c.execute("SELECT COUNT(*) FROM measurements WHERE run_id=?", (run_id,)).fetchone()[0]

                if count != s["sent"] or s["planned"] != r["config"]["request_count"] or (r["summary"] is not None and r["summary"] != s):
                    raise ApiError(409, "INVALID_MEASUREMENT_STATE", "최종 집계와 요청 원본이 일치하지 않습니다.")

                r["summary"], r["measurement_final"] = s, True
                r["status"] = s["termination"]
                r["finished_at"] = now()

                if not r["target_seen"] and s["sent"] == 0:
                    r["telemetry_complete"] = True

                self.event(c, run_id, "run." + r["status"], {"status": r["status"]})
                self.log(c, run_id, f"발생기 종료: {s['termination']}, 전송 {s['sent']}, 드롭 {s['dropped']}")

            self.save(c, r)
            response = {"accepted_count": accepted, "duplicate_count": duplicate, "final_received": r["measurement_final"]}
            self._save_batch(c, run_id, "loadgen", batch.batch_id, h, response)

        self.save_batch_file(run_id, "loadgen", batch.batch_id, batch.model_dump(mode="json"))

        if batch.final:
            self.export(run_id)

        return response

    # 마켓에 표시할 현재 실험 상태 집계
    def snapshot(self, run_id):
        with self.tx() as c:
            r = self.get(c, run_id)
            requests = [json.loads(row[0]) for row in c.execute("SELECT doc FROM requests WHERE run_id=?", (run_id,))]
            measurements = [json.loads(row[0]) for row in c.execute("SELECT doc FROM measurements WHERE run_id=?", (run_id,))]
            counts = dict(received=len(requests), queued=0, processing=0, succeeded=0, failed=0, cancelled=0)

            # 접수 직후 상태는 화면 집계에서 대기 요청으로 포함
            for request in requests:
                status = request["server_status"]
                counts["queued" if status == "received" else status] += 1

            summary = r["summary"]
            progress = {}

            try:
                progress = json.loads((self.settings.runs_dir / run_id / "live-progress.json").read_text())
            except (OSError, ValueError):
                pass

            sent = summary["sent"] if summary else max(len(measurements), int(progress.get("sent", 0)))
            success = sum(m["outcome"] == "success" for m in measurements)
            failed = len(measurements) - success
            elapsed = summary["elapsed_ms"] / 1000 if summary else progress.get("elapsed_ms", 0) / 1000
            times = sorted(m["elapsed_ms"] for m in measurements if m["outcome"] == "success")
            metrics = {
                "avg_response_ms": sum(times) / len(times) if times else None,
                "p95_response_ms": times[math.ceil(0.95 * len(times)) - 1] if times else None,
                "failure_rate": failed / sent if sent else None,
                "success_rps": success / elapsed if elapsed > 0 else None,
            }
            last_seq = c.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]

            return {"run_id": run_id, "status": r["status"], "source_run_id": r["source_run_id"],
                    "config": r["config"], "started_at": r["started_at"], "finished_at": r["finished_at"],
                    "last_seq": last_seq, "server_counts": counts,
                    "client_counts": {"planned": r["config"]["request_count"], "sent": sent, "success": success, "failed": failed,
                                      "pending": sent - len(measurements), "dropped": summary["dropped"] if summary else int(progress.get("dropped", 0))},
                    "metrics": metrics, "telemetry_complete": r["telemetry_complete"], "agent_phase": r["agent_phase"], "artifact": r["artifact"]}

    # 고객 요청의 서버 상태와 측정 결과 조회
    def detail(self, run_id, request_id):
        with self.tx() as c:
            self.get(c, run_id)
            row = c.execute("SELECT doc FROM requests WHERE run_id=? AND request_id=?", (run_id, request_id)).fetchone()
            mrow = c.execute("SELECT doc FROM measurements WHERE run_id=? AND request_id=?", (run_id, request_id)).fetchone()

            if not row and not mrow:
                raise ApiError(404, "REQUEST_NOT_FOUND", "요청을 찾을 수 없습니다.")

            r = json.loads(row[0]) if row else {}
            m = json.loads(mrow[0]) if mrow else {}
            result = {k: r.get(k) for k in ("server_status", "received_at", "started_at", "finished_at", "queue_wait_ms", "processing_ms", "server_total_ms", "error_code")}
            result.update(run_id=run_id, request_id=request_id, customer_id=r.get("customer_id", m.get("customer_id")),
                          query=r.get("query", m.get("query")), client_outcome=m.get("outcome"), client_elapsed_ms=m.get("elapsed_ms"),
                          http_status=m.get("http_status"), items=m.get("items", r.get("items")),
                          current_wait_ms=max(0, (time.monotonic() - r["queued_mono"]) * 1000) if r.get("server_status") == "queued" else None)

            return result

    # 마지막 순번 이후 이벤트 조회
    def events(self, run_id, after, limit):
        with self.tx() as c:
            self.get(c, run_id)
            rows = list(c.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?", (run_id, after, limit + 1)))
            values = [{**dict(row), "data": json.loads(row["data"])} for row in rows[:limit]]

            return {"events": values, "next_after_seq": values[-1]["seq"] if values else after, "has_more": len(rows) > limit}

    # 수준에 맞는 진단 로그 조회
    def logs(self, run_id, after, limit, level):
        levels = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}

        with self.tx() as c:
            self.get(c, run_id)
            rows = list(c.execute("SELECT seq,occurred_at,data FROM events WHERE run_id=? AND seq>? AND type='log' ORDER BY seq", (run_id, after)))
            logs, cursor = [], after

            for row in rows:
                payload = json.loads(row["data"])
                cursor = row["seq"]

                if level is None or levels[payload["level"]] >= levels[level]:
                    logs.append({"seq": row["seq"], "occurred_at": row["occurred_at"], **payload})

                if len(logs) == limit:
                    break

            return {"logs": logs, "next_after_seq": cursor, "has_more": bool(rows and rows[-1]["seq"] > cursor)}

    # 에이전트 작업 단계 저장
    def progress(self, body):
        value = body.model_dump(mode="json")
        h = digest(value)

        with self.tx() as c:
            r = self.get(c, body.run_id)

            if body.related_run_id:
                self.get(c, body.related_run_id)

            old = c.execute("SELECT hash,response FROM messages WHERE operation_id=? AND message_id=?", (body.operation_id, body.message_id)).fetchone()

            if old:
                if old[0] != h:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "같은 메시지 ID의 본문이 다릅니다.")

                return {**json.loads(old[1]), "duplicate": True}

            r["agent_phase"] = body.phase
            self.save(c, r)
            seq = self.event(c, body.run_id, "agent.progress", value)
            self.log(c, body.run_id, body.message, "agent")
            response = {"accepted": True, "seq": seq, "duplicate": False}
            c.execute("INSERT INTO messages VALUES(?,?,?,?)", (body.operation_id, body.message_id, h, canonical(response)))

            return response

    # 요청별 측정 원본 조회
    def records(self, run_id):
        with self.tx() as c:
            return [json.loads(row[0]) for row in c.execute("SELECT doc FROM measurements WHERE run_id=? ORDER BY customer_id", (run_id,))]

    # 전체 실험 기록 조회
    def all_runs(self):
        with self.tx() as c:
            return [json.loads(row[0]) for row in c.execute("SELECT doc FROM runs")]

    # 수집 배치 원본 파일 저장
    def save_batch_file(self, run_id, source, batch_id, body):
        folder = self.settings.runs_dir / run_id / source
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{batch_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2))
        tmp.replace(path)

    # 실험 결과 파일 저장
    def export(self, run_id):
        r = self.raw(run_id)
        folder = self.settings.runs_dir / run_id
        folder.mkdir(parents=True, exist_ok=True)

        for name, value in [("config.json", r["config"]), ("request-plan.json", r["plan"]),
                            ("revision.json", r["artifact"]), ("summary.json", self.snapshot(run_id))]:
            path = folder / name
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
            tmp.replace(path)

        rows = self.records(run_id)
        (folder / "measurements.jsonl").write_text("".join(canonical(m) + "\n" for m in rows))
