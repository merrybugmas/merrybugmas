import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from backend.control import create_app
from backend.target import create_app as target_app
from backend.models import Measurement, StartRun, TargetEvent
from conftest import make_run,finish_run


def test_all_spreadsheet_endpoints_have_response_schemas(seeded):
    contract=json.loads((Path(__file__).parents[1]/'docs/api-contract.json').read_text())
    control=create_app(seeded).openapi()
    target=target_app(seeded).openapi()
    assert len(contract['endpoints'])==12
    for e in contract['endpoints']:
        doc=target if e['id']=='API-01' else control
        operation=doc['paths'][e['path']][e['method']]
        response=operation['responses'][str(e['success'])]
        assert '$ref' in response['content']['application/json']['schema'],e


@pytest.mark.parametrize('data',[
 {'source_run_id':'run','seed':42},
 {'scenario_id':'surge','arrival_duration_s':0,'client_timeout_ms':5000},
 {'scenario_id':'surge','arrival_duration_s':1,'client_timeout_ms':5000,'request_count':8001},
 {'scenario_id':'surge','arrival_duration_s':1,'client_timeout_ms':5000,'arbitrary_command':'rm'},
])
def test_start_rejects_invalid_contract_inputs(data):
    with pytest.raises(ValidationError):StartRun.model_validate(data)


def test_no_false_success_for_missing_response():
    with pytest.raises(ValidationError):
        Measurement(request_id='r',customer_id='c',query={'category':'cookie','max_price':20000},
                    scheduled_offset_ms=0,sent_offset_ms=0,elapsed_ms=1,outcome='success',http_status=200,items=None)


def test_event_payload_rejects_nan():
    with pytest.raises(ValidationError):
        TargetEvent(source_event_id='e',type='request.processing',request_id='r',customer_id='c',
                    occurred_at='2026-11-27T06:00:00Z',data={'queue_wait_ms':float('nan')})


def test_client_drop_and_timeout_are_separate(store):
    from backend.models import MeasurementBatch
    make_run(store,count=2)
    body=MeasurementBatch(batch_id='m',measurements=[{'request_id':'r','customer_id':'customer-0001',
      'query':{'category':'cookie','max_price':20000},'scheduled_offset_ms':0,'sent_offset_ms':5,
      'elapsed_ms':5000,'outcome':'timeout','http_status':None,'items':None}],final=True,
      summary={'planned':2,'sent':1,'dropped':1,'elapsed_ms':5000,'termination':'completed'})
    store.measurements_ingest('run-001',body)
    result=store.snapshot('run-001')
    assert result['client_counts']['dropped']==1
    assert result['metrics']['failure_rate']==1
    assert result['metrics']['p95_response_ms'] is None


def test_tampered_measurement_plan_is_rejected(store):
    from backend.models import MeasurementBatch
    from backend.common import ApiError
    make_run(store,count=1)
    batch=MeasurementBatch(batch_id='m',measurements=[{'request_id':'r','customer_id':'customer-0001',
      'query':{'category':'toy','max_price':20000},'scheduled_offset_ms':0,'sent_offset_ms':5,
      'elapsed_ms':5,'outcome':'success','http_status':200,'items':[]}])
    with pytest.raises(ApiError):store.measurements_ingest('run-001',batch)
    assert store.records('run-001')==[]
