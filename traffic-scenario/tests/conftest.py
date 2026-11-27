import json
from pathlib import Path
import pytest
from backend import catalog
from backend.config import Settings
from backend.common import canonical, digest
from backend.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, product_count=500, manage_target=False,
                    policies_path=tmp_path / "policies.json", concurrency=1, queue_limit=1, queue_timeout_ms=30)


@pytest.fixture
def seeded(settings):
    catalog.initialize(settings.catalog, settings.product_count)
    return settings


@pytest.fixture
def store(seeded):
    return Store(seeded)


def make_run(store, run_id="run-001", count=2, artifact=None):
    plan = [{"customer_id": f"customer-{i+1:04d}", "query": {"category": "cookie", "max_price": 20000}, "scheduled_offset_ms": i * 10.0} for i in range(count)]
    cfg = {"scenario_id": "surge", "request_count": count, "arrival_duration_s": count / 100,
           "seed": 42, "client_timeout_ms": 5000}
    artifact = artifact or {"code_revision": "code-a", "schema_hash": "schema-a", "dataset_hash": "data-a",
                           "request_plan_hash": digest(plan), "runtime_config_hash": "runtime-a", "warmup_policy": "warmup-v1"}
    query = plan[0]["query"]
    expected = {canonical(query): catalog.search(store.settings.catalog, **query)}
    return store.create(run_id, cfg, plan, artifact, None, expected)


def finish_run(store, run_id, duration=10):
    from backend.common import now
    from backend.models import EventBatch, MeasurementBatch
    r = store.raw(run_id)
    events, measurements = [], []
    for i, p in enumerate(r["plan"]):
        rid = run_id + "-" + p["customer_id"]
        items = r["expected"][canonical(p["query"])]
        for j, (kind, data) in enumerate([
            ("received", {"query": p["query"]}), ("queued", {"queue_size": 1}),
            ("processing", {"queue_wait_ms": 0}),
            ("succeeded", {"queue_wait_ms": 0, "processing_ms": duration, "server_total_ms": duration, "items": items})
        ]):
            events.append({"source_event_id": f"e-{i}-{j}", "type": "request."+kind, "request_id": rid,
                           "customer_id": p["customer_id"], "occurred_at": now(), "data": data})
        measurements.append({"request_id": rid, "customer_id": p["customer_id"], "query": p["query"],
                             "scheduled_offset_ms": p["scheduled_offset_ms"], "sent_offset_ms": p["scheduled_offset_ms"],
                             "elapsed_ms": duration, "outcome": "success", "http_status": 200, "items": items})
    store.events_ingest(run_id, EventBatch(batch_id="events", events=events, final=True))
    store.measurements_ingest(run_id, MeasurementBatch(batch_id="measurements", measurements=measurements, final=True,
        summary={"planned": len(measurements), "sent": len(measurements), "dropped": 0, "elapsed_ms": duration*len(measurements), "termination": "completed"}))
