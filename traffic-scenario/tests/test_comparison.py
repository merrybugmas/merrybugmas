import json
import pytest
from backend.common import ApiError
from backend.models import Compare
from backend.comparison import compare
from conftest import make_run,finish_run


def policy(settings):
    settings.policies_path.write_text(json.dumps({'demo-v1':{'min_repeats':1,'min_sent':1,'min_relative_improvement':.05,'allowed_relative_regression':.02,'max_failure_rate_increase':0}}))


def test_unconfigured_policy_and_not_ready(store,settings):
    make_run(store,'before');make_run(store,'after')
    b=Compare(before_run_id='before',after_run_id='after',criteria_id='demo-v1')
    with pytest.raises(ApiError) as e:compare(store,settings,b)
    assert e.value.code=='POLICY_NOT_READY'
    policy(settings)
    with pytest.raises(ApiError) as e:compare(store,settings,b)
    assert e.value.code=='INVALID_RUN_STATE'


def test_improvement_and_regression(store,settings):
    policy(settings)
    before=make_run(store,'before');finish_run(store,'before',100)
    artifact={**before['artifact'],'schema_hash':'indexed'}
    make_run(store,'after',artifact=artifact);finish_run(store,'after',20)
    result=compare(store,settings,Compare(before_run_id='before',after_run_id='after',criteria_id='demo-v1'))
    assert result['verdict']=='improved' and result['correctness_passed']
    reverse=compare(store,settings,Compare(before_run_id='after',after_run_id='before',criteria_id='demo-v1'))
    assert reverse['verdict']=='regressed'


def test_fixed_condition_mismatch(store,settings):
    policy(settings)
    before=make_run(store,'before');finish_run(store,'before')
    make_run(store,'after',artifact={**before['artifact'],'dataset_hash':'different'});finish_run(store,'after')
    with pytest.raises(ApiError) as e:compare(store,settings,Compare(before_run_id='before',after_run_id='after',criteria_id='demo-v1'))
    assert e.value.code=='INCOMPATIBLE_RUNS'


def test_index_keeps_search_results(seeded):
    from backend import catalog
    before=catalog.search(seeded.catalog,'cookie',20000)
    with catalog.connect(seeded.catalog) as c:c.execute(catalog.INDEX_SQL)
    assert catalog.search(seeded.catalog,'cookie',20000)==before
    assert any('INDEX' in r['detail'] for r in catalog.plan(seeded.catalog))


# 후보 코드가 잘못된 정답을 저장해도 개선 판정에서 제외
def test_candidate_cannot_replace_baseline_answers(store, settings):
    policy(settings)
    before = make_run(store, 'before')
    finish_run(store, 'before', 100)
    assert any(before['expected'].values())

    make_run(store, 'after', artifact={**before['artifact'], 'code_revision': 'wrong-search'})

    with store.tx() as con:
        candidate = store.get(con, 'after')
        candidate['expected'] = {query: [] for query in candidate['expected']}
        store.save(con, candidate)

    finish_run(store, 'after', 20)
    body = Compare(before_run_id='before', after_run_id='after', criteria_id='demo-v1')
    result = compare(store, settings, body)

    assert result['verdict'] == 'regressed'
    assert result['correctness_passed'] is False
