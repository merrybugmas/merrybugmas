import json
import pytest
from backend.common import ApiError, now
from backend.models import EventBatch, MeasurementBatch, Progress
from conftest import make_run, finish_run


def event(kind, eid="evt-1", **data):
    return dict(source_event_id=eid,type="request."+kind,request_id="req-1",customer_id="customer-0001",occurred_at=now(),data=data)


def test_batch_retries_and_conflict_are_atomic(store):
    make_run(store)
    body = EventBatch(batch_id="b-1", events=[event("received", query={"category":"cookie","max_price":20000})])
    response = store.events_ingest("run-001",body)
    assert store.events_ingest("run-001",body)==response
    assert store.snapshot("run-001")["server_counts"]["received"]==1
    changed=body.model_copy(update={"final":True})
    with pytest.raises(ApiError) as e:store.events_ingest("run-001",changed)
    assert e.value.code=="IDEMPOTENCY_CONFLICT"
    bad=EventBatch(batch_id="b-bad",events=[event("queued","evt-2",queue_size=1),event("queued","evt-3",queue_size=1)])
    with pytest.raises(ApiError):store.events_ingest("run-001",bad)
    assert store.detail("run-001","req-1")["server_status"]=="received"


def test_final_cannot_hide_unfinished_work(store):
    make_run(store)
    store.events_ingest("run-001",EventBatch(batch_id="b-1",events=[event("received",query={"category":"cookie","max_price":20000})]))
    with pytest.raises(ApiError):store.events_ingest("run-001",EventBatch(batch_id="final",events=[],final=True))
    assert not store.raw("run-001")["telemetry_complete"]


def test_measurement_final_totals_and_retry(store):
    make_run(store)
    body=MeasurementBatch(batch_id="bad",measurements=[],final=True,summary={"planned":2,"sent":2,"dropped":0,"elapsed_ms":50,"termination":"completed"})
    with pytest.raises(ApiError):store.measurements_ingest("run-001",body)
    assert store.raw("run-001")["status"]=="pending"
    finish_run(store,"run-001")
    s=store.snapshot("run-001")
    assert s["status"]=="completed" and s["telemetry_complete"]
    assert s["client_counts"]["success"]==2
    assert s["metrics"]["p95_response_ms"]==10
    assert s["metrics"]["success_rps"]==100


def test_server_completion_does_not_override_client_timeout(store):
    make_run(store,count=1)
    finish_run(store,"run-001")
    with store.tx() as c:
        row=c.execute("SELECT request_id,doc FROM measurements").fetchone()
        m=json.loads(row["doc"]);m.update(outcome="timeout",http_status=None,items=None,elapsed_ms=5000)
        from backend.common import canonical
        c.execute("UPDATE measurements SET doc=?",(canonical(m),))
    detail=store.detail("run-001",row["request_id"])
    assert detail["server_status"]=="succeeded" and detail["client_outcome"]=="timeout"
    s=store.snapshot("run-001")
    assert s["metrics"]["avg_response_ms"] is None and s["metrics"]["failure_rate"]==1


def test_events_cursor_and_log_filters(store):
    make_run(store)
    finish_run(store,"run-001")
    cursor=0;seqs=[]
    while True:
        page=store.events("run-001",cursor,3)
        seqs.extend(e["seq"] for e in page["events"])
        cursor=page["next_after_seq"]
        if not page["has_more"]:break
    assert seqs==sorted(set(seqs)) and len(seqs)>8
    assert store.logs("run-001",0,100,"ERROR")["logs"]==[]


def test_progress_idempotent(store):
    make_run(store)
    p=Progress(run_id="run-001",operation_id="op-1",message_id="msg-1",phase="analyzing",message="조회 계획 확인")
    first=store.progress(p);second=store.progress(p)
    assert second["seq"]==first["seq"] and second["duplicate"]
    with pytest.raises(ApiError):store.progress(p.model_copy(update={"message":"다른 본문"}))


def test_unknown_run_and_request(store):
    with pytest.raises(ApiError) as e:store.snapshot("missing")
    assert e.value.status==404
    make_run(store)
    with pytest.raises(ApiError) as e:store.detail("run-001","missing")
    assert e.value.code=="REQUEST_NOT_FOUND"
