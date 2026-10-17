"""Bounded real-HTTP public replay journey; refuses Owner8000/non-demo targets."""
import argparse
import http.cookiejar
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPCookieProcessor


def journey(base, expected_revision=None):
    url=urlparse(base)
    if url.hostname!='127.0.0.1' or url.port==8000 or url.path not in {'','/'}:
        raise ValueError('Only an explicitly isolated localhost Demo, never Owner8000')
    clients=[build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar())) for _ in range(2)]
    checks=[]
    def request(path, *, guest=0, method='GET', payload=None):
        data=json.dumps(payload).encode() if payload is not None else None
        req=Request(base.rstrip('/')+path,data=data,method=method,
            headers={'Content-Type':'application/json','User-Agent':f'MIA-demo-smoke-guest-{guest}'})
        try:
            response=clients[guest].open(req,timeout=2)
        except HTTPError as exc:
            response=exc
        with response:
            raw=response.read()
            return response.status,json.loads(raw) if 'json' in response.headers.get('Content-Type','') else raw.decode()
    deadline=time.monotonic()+20
    while True:
        try:
            code,manifest=request('/api/public-demo')
            break
        except (URLError,TimeoutError,ConnectionError):
            if time.monotonic()>=deadline:raise
            time.sleep(.2)
    assert code==200 and manifest['runtime_mode']=='PUBLIC_DEMO' and manifest['synthetic'] is True
    assert manifest['access']=='SHARED_READ_ONLY' and manifest['execution_mode']=='OFFLINE_REPLAY'
    assert manifest['model_budget_usd']=='0' and manifest['live_research_enabled'] is False
    if expected_revision:assert manifest['build_revision']==expected_revision
    checks.append('explicit_synthetic_shared_readonly_zero_live_budget')
    assert request('/health')==(200,{'status':'ok'})
    assert 'data-runtime-mode="PUBLIC_DEMO"' in request('/')[1]
    examples=manifest['examples'];task_id=examples[0]['task_id']
    code,queue=request('/api/attention/queue')
    assert code==200 and queue['scan_result_status']=='COMPLETED_WITH_EVENTS'
    assert queue['freshness']['market_timezone']=='America/New_York'
    items={row['item']['item_id']:row for row in queue['items']}
    assert items and all(row['freshness']['freshness_status']=='TODAY' for row in items.values())
    checks.append('today_queue_replayed_scan_ny_publication_date')
    tasks={}
    for example in examples:
        code,task=request(f"/api/research/tasks/{example['task_id']}/revisions/{example['revision']}")
        assert code==200 and task['status']=='COMPLETE' and task['attention_item_id']==example['attention_item_id']
        assert example['attention_item_id'] in items
        assert request('/api/attention/items/'+example['attention_item_id'])[0]==200
        sources={s['evidence']['evidence_id'] for s in task['sources']}
        assert sources and set(task['assessment']['core_judgment']['evidence_ids'])<=sources
        assert set(task['action_decision']['source_ids'])<=sources
        assert all(event['model'].startswith('scripted-oracle') for event in task['model_events'])
        tasks[(example['symbol'],example['mode'])]=task
    checks.append('today_item_to_research_task_with_resolvable_typed_evidence')
    fast,deep=tasks[('XMPL','FAST')],tasks[('XMPL','DEEP')]
    assert fast['context_manifest']['max_context_bytes']<deep['context_manifest']['max_context_bytes']
    assert fast['investigation']['status']=='NOT_NEEDED' and deep['investigation']['status']=='NEW_EVIDENCE'
    assert sum(1 for s in deep['sources'] if s['evidence']['structured_data'].get('calculation')=='sec_quarterly_v1')==4
    assert fast['action_decision']['decision_action']=='HOLD' and fast['action_decision']['position_status']=='HELD'
    unheld=tasks[('XGRD','FAST')]['action_decision']
    assert unheld['decision_action'] is None and unheld['decision_status']=='UNDETERMINED'
    checks.append('fast_deep_distinct_budgets_gap_check_four_quarters_and_action_applicability')
    before=request('/api/research/tasks')[1]
    for guest in range(2):
        assert request('/api/research/tasks',guest=guest)[1]==before
        for method,path in [('POST','/api/research/tasks'),('POST','/api/attention/scan'),
            ('PUT','/api/research-universe'),('POST','/api/portfolio'),('DELETE',f'/api/research/tasks/{task_id}'),
            ('POST','/api/trade')]:
            status,error=request(path,guest=guest,method=method,payload={'question':'APP','task_id':task_id})
            assert status==403 and error=={'detail':'PUBLIC_DEMO_READ_ONLY'}
        assert request('/api/research/tasks/not-a-guest-task',guest=guest)[0]==404
    assert request('/api/research/tasks')[1]==before
    checks.append('two_independent_guests_all_mutations_denied_no_private_task_creation')
    assert request('/api/portfolio/current')[1]['portfolio_id']=='public-demo-synthetic'
    assert request('/api/console/portfolio-overview')[0]==200
    for path in ('/qa/runtime/metrics','/docs','/openapi.json','/api/runs/private','/api/attention/scans/x','/api/attention/deep/x'):
        assert request(path)[0]==404
    checks.append('synthetic_portfolio_only_and_nonpublic_routes_denied')
    return {'status':'PASS','timestamp':datetime.now(timezone.utc).isoformat(),'base':base,
        'build_revision':manifest['build_revision'],'dataset_hash':manifest['dataset_hash'],
        'checks':checks,'guest_scope':'INTENTIONALLY_SHARED_SYNTHETIC_READ_ONLY',
        'live_model_calls':0,'owner_port_8000_used':False}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base',default='http://127.0.0.1:8080')
    parser.add_argument('--expected-revision');parser.add_argument('--output',type=Path)
    args=parser.parse_args();result=journey(args.base,args.expected_revision)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result))


if __name__=='__main__':main()
