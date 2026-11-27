# 응답 완료와 별개로 예정된 시각에 요청을 보내는 참조 부하 실행기
import argparse
import asyncio
import json
import signal
import time
import uuid
from pathlib import Path

import httpx

from .models import SearchResponse


# 저장된 요청 계획으로 부하 실행
async def execute(run_id, folder, target_url, control_url, max_inflight):
    plan = json.loads((folder / "request-plan.json").read_text())
    config = json.loads((folder / "config.json").read_text())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    started = time.monotonic()
    tasks = set()
    sent = dropped = 0
    spool = folder / "loadgen-outbox"
    spool.mkdir(exist_ok=True)
    client = httpx.AsyncClient(timeout=config["client_timeout_ms"] / 1000, trust_env=False,
                               limits=httpx.Limits(max_connections=max_inflight, max_keepalive_connections=max_inflight))
    uploader = httpx.AsyncClient(timeout=10, trust_env=False)
    queue = asyncio.Queue()

    # 측정 결과를 제어 서버로 전송
    async def upload():
        while True:
            body = await queue.get()

            try:
                file = spool / f"{body['batch_id']}.json"
                file.write_text(json.dumps(body, ensure_ascii=False))

                for attempt in range(10):
                    try:
                        r = await uploader.post(f"{control_url}/internal/runs/{run_id}/measurements", json=body)
                        r.raise_for_status()
                        file.unlink()
                        break
                    except httpx.HTTPError:
                        if attempt == 9:
                            raise

                        await asyncio.sleep(0.1 * (attempt + 1))
            finally:
                queue.task_done()

    worker = asyncio.create_task(upload())
    buffer = []

    # 현재 부하 진행 상황 저장
    def progress():
        value = {"sent": sent, "dropped": dropped, "elapsed_ms": (time.monotonic() - started) * 1000}
        tmp = folder / "live-progress.tmp"
        tmp.write_text(json.dumps(value))
        tmp.replace(folder / "live-progress.json")

    # 중지 요청과 진행 상황 확인
    async def heartbeat():
        while True:
            if (folder / "stop-requested").exists():
                stop.set()

            progress()
            await asyncio.sleep(.05)

    heartbeat_task = asyncio.create_task(heartbeat())

    # 검색 요청 전송과 응답 시간 측정
    async def send(entry):
        offset = (time.monotonic() - started) * 1000
        t = time.monotonic()
        rid = run_id + "-" + entry["customer_id"]
        result = {"request_id": rid, "customer_id": entry["customer_id"], "query": entry["query"],
                  "scheduled_offset_ms": entry["scheduled_offset_ms"], "sent_offset_ms": offset,
                  "elapsed_ms": 0, "outcome": "connection_error", "http_status": None, "items": None}

        try:
            # 연결과 수신 시간 외에 요청 전체의 마감 시간도 적용
            async with asyncio.timeout(config["client_timeout_ms"] / 1000):
                response = await client.get(f"{target_url}/api/guide/search", params=entry["query"],
                                            headers={"X-Run-ID": run_id, "X-Request-ID": rid, "X-Customer-ID": entry["customer_id"]})

            result["http_status"] = response.status_code

            if response.status_code == 200:
                try:
                    value = SearchResponse.model_validate(response.json())

                    if value.request_id != rid:
                        raise ValueError("Response request ID differs")

                    result.update(outcome="success", items=[x.model_dump() for x in value.items])
                except (ValueError, TypeError):
                    # HTTP 200이어도 응답 형식이 잘못되면 실패로 기록
                    result.update(outcome="connection_error", http_status=None)
            else:
                result["outcome"] = "http_error"
        except (httpx.TimeoutException, TimeoutError):
            result["outcome"] = "timeout"
        except httpx.HTTPError:
            pass
        except asyncio.CancelledError:
            result["outcome"] = "cancelled"
        finally:
            result["elapsed_ms"] = (time.monotonic() - t) * 1000
            buffer.append(result)

            with (folder / "client-raw.jsonl").open("a") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")

            if len(buffer) >= 25:
                queue.put_nowait({"batch_id": "load-" + uuid.uuid4().hex, "measurements": buffer.copy(), "final": False})
                buffer.clear()

    try:
        for entry in plan:
            if (folder / "stop-requested").exists():
                stop.set()

            delay = started + entry["scheduled_offset_ms"] / 1000 - time.monotonic()

            if delay > 0:
                try:
                    await asyncio.wait_for(stop.wait(), delay)
                except TimeoutError:
                    pass

            if stop.is_set():
                dropped += len(plan) - sent - dropped
                break

            if len(tasks) >= max_inflight:
                dropped += 1
                continue

            sent += 1
            task = asyncio.create_task(send(entry))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        # 중지 후 새 요청은 보내지 않고 이미 보낸 요청의 결과는 회수
        if tasks:
            await asyncio.gather(*list(tasks))

        if buffer:
            queue.put_nowait({"batch_id": "load-" + uuid.uuid4().hex, "measurements": buffer.copy(), "final": False})
            buffer.clear()

        summary = {"planned": len(plan), "sent": sent, "dropped": dropped,
                   "elapsed_ms": max((time.monotonic() - started) * 1000, 0.001),
                   "termination": "stopped" if stop.is_set() else "completed"}
        (folder / "loadgen-summary.json").write_text(json.dumps(summary, indent=2))
        progress()
        queue.put_nowait({"batch_id": "load-final-" + uuid.uuid4().hex, "measurements": [], "final": True, "summary": summary})
        done = asyncio.create_task(queue.join())
        complete, _ = await asyncio.wait({done, worker}, return_when=asyncio.FIRST_COMPLETED)

        if worker in complete:
            done.cancel()
            await worker

        await done
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await client.aclose()
        await uploader.aclose()


# 부하 실행 명령 처리
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--folder", required=True, type=Path)
    p.add_argument("--target", required=True)
    p.add_argument("--control", required=True)
    p.add_argument("--max-inflight", type=int, default=1000)
    args = p.parse_args()
    asyncio.run(execute(args.run_id, args.folder, args.target, args.control, args.max_inflight))


if __name__ == "__main__":
    main()
