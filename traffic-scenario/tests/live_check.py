# 실행 중인 로컬 서버로 부하 연동 확인
import argparse
import asyncio
import json
import time
from pathlib import Path
import httpx


async def wait_run(client, base, rid, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        response = await client.get(f"{base}/api/runs/{rid}")
        response.raise_for_status()
        data = response.json()
        if data["status"] in {"completed", "stopped", "failed"} and data["telemetry_complete"]:
            return data
        await asyncio.sleep(.2)
    raise AssertionError(f"Run did not finish: {data}")


async def main(base, output, count, duration):
    async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
        start = await c.post(base + "/api/runs", json={"scenario_id": "surge", "request_count": count,
            "arrival_duration_s": duration, "seed": 42, "client_timeout_ms": 5000})
        assert start.status_code == 202, start.text
        rid = start.json()["run_id"]
        active = await c.post(base + "/api/runs", json={"source_run_id": rid})
        assert active.status_code == 409, active.text
        result = await wait_run(c, base, rid)
        assert result["status"] == "completed", result
        assert result["client_counts"]["sent"] == count, result
        assert result["client_counts"]["dropped"] == 0, result
        # A whole-request timeout can happen before the server accepts the connection.
        assert result["client_counts"]["success"] <= result["server_counts"]["received"] <= count, result
        assert sum(result["server_counts"][k] for k in ("succeeded","failed","cancelled")) == result["server_counts"]["received"]
        cursor = 0
        all_events = []
        while True:
            r = await c.get(f"{base}/api/runs/{rid}/events", params={"after_seq":cursor,"limit":1000})
            r.raise_for_status()
            page = r.json()
            all_events.extend(page["events"])
            cursor = page["next_after_seq"]
            if not page["has_more"]:break
        assert len({e['seq'] for e in all_events}) == len(all_events)
        first = next(e for e in all_events if e["request_id"])
        detail = await c.get(f"{base}/api/runs/{rid}/requests/{first['request_id']}")
        assert detail.status_code == 200
        assert detail.json()["client_outcome"] is not None
        progress = {"run_id":rid,"operation_id":"live-check","message_id":f"message-{rid}","phase":"analyzing","message":"실제 부하 실험 로그를 확인합니다."}
        p = await c.post(base + '/api/agent/progress',json=progress)
        assert p.status_code == 200
        retry = await c.post(base + '/api/agent/progress',json=progress)
        assert retry.json()['duplicate'] is True
        logs = await c.get(f'{base}/api/runs/{rid}/logs?limit=10')
        assert logs.status_code == 200 and logs.json()['logs']
        restart = await c.post(base + '/api/service/restart', json={"run_id":rid,"reason":"실제 서버 재시작 연동 검증"})
        assert restart.status_code == 202, restart.text
        op = restart.json()['operation_id']
        for _ in range(100):
            r = await c.get(f'{base}/api/runs/{rid}/events',params={'after_seq':cursor,'limit':1000})
            page = r.json();cursor=page['next_after_seq']
            matches = [e for e in page['events'] if e['type']=='service.restarted' and e['data']['operation_id']==op]
            assert not any(e['type']=='service.restart_failed' for e in page['events']), page
            if matches:break
            await asyncio.sleep(.1)
        else:raise AssertionError('Restart event missing')
        replay = await c.post(base + '/api/runs',json={'source_run_id':rid})
        assert replay.status_code == 202,replay.text
        after = await wait_run(c,base,replay.json()['run_id'])
        assert after['artifact']['request_plan_hash']==result['artifact']['request_plan_hash']
        assert after['config']==result['config']
        comparison = await c.post(base+'/api/comparisons',json={'before_run_id':rid,'after_run_id':after['run_id'],'criteria_id':'demo-v1'})
        assert comparison.status_code==409 and comparison.json()['error']['code']=='POLICY_NOT_READY',comparison.text
        evidence={'before':result,'replay':after,'event_count':len(all_events),'detail':detail.json(),
                  'restart_verified':True,'unconfigured_policy_rejected':True}
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
        print(json.dumps({'before':rid,'replay':after['run_id'],'client_counts':result['client_counts'],
                          'metrics':result['metrics'],'events':len(all_events),'evidence':str(output)},ensure_ascii=False))


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--base',default='http://127.0.0.1:8000')
    p.add_argument('--output',type=Path,default=Path('data/live-check.json'))
    p.add_argument('--count',type=int,default=1000)
    p.add_argument('--duration',type=float,default=2)
    a=p.parse_args()
    asyncio.run(main(a.base,a.output,a.count,a.duration))
