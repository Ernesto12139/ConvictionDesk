"""Inspect an explicitly fictional local journey; never start research or scan.

Only GETs to an identity-checked non-Owner loopback service are permitted. All
metrics name their fixture scope; absent measurements remain NOT_TESTED.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def measure(base, browser=None):
    target=urlsplit(base)
    if target.hostname!='127.0.0.1' or not target.port or target.port==8000 or target.scheme!='http':
        raise ValueError('Explicit isolated synthetic loopback service required')
    with httpx.Client(base_url=base,trust_env=False,timeout=10) as client:
        def get(path):return client.get(path).raise_for_status().json()
        identity=get('/_v09/identity')
        assert identity['scope']=='OFFLINE_SYNTHETIC_ONLY' and identity['source_on_disk_matches']
        assert identity['external_attempts']==identity['paid_model_calls']==0
        journey=identity['journey'];task_id=journey['task_id']
        fast=get(f'/api/research/tasks/{task_id}/revisions/1')
        deep=get(f'/api/research/tasks/{task_id}/revisions/2')
        financial=get(f'/api/research/tasks/{task_id}/valuation?revision=2')
        fast_financial=get(f'/api/research/tasks/{task_id}/valuation?revision=1')
        originals=set(journey['source_ids'])
        selected=lambda t:{r['evidence_id'] for r in t['context_manifest']['selected']}
        assert originals<=selected(fast) and originals<=selected(deep)
        assert fast['snapshot_hash']==deep['snapshot_hash'] and fast['cutoff']==deep['cutoff']
        final=get('/_v09/identity')
        assert identity==final,'Inspection must not submit or alter the fixture'
    report={'status':'VERIFIED_OFFLINE_SCOPED','private_source_sha':identity['head'],
        'source_digest':identity['source_digest'],'run_id':identity['run_id'],
        'scope':'Fictional frozen sources/scripted model only',
        'REAL_MODEL_QUALITY':'REAL_MODEL_QUALITY_NOT_TESTED',
        'time_to_first_useful_answer':{'seconds':journey['time_to_first_useful_answer_seconds'],
            'method':'Fixture seed: manual synthetic scan through completed Fast; excludes human reading and real model latency'},
        'qualified_source_coverage':{'expected_originals':len(originals),
            'fast_originals_selected':len(originals&selected(fast)),
            'deep_originals_selected':len(originals&selected(deep)),
            'qualification':'Typed fictional receipts at fixed cutoff; not live-source coverage'},
        'material_event_discovery_under_fixed_budget':{'fixture_articles':journey['scan_report']['events_considered'],
            'screened_units':journey['scan_report']['screened'],
            'screen_batch_limit':journey['scan_report']['screen_batch_limit'],
            'general_discovery_recall':'NOT_TESTED; no new independent varied-headline evaluation'},
        'false_merge_and_missing_originals':{'missing_originals_in_this_journey':sorted(originals-selected(deep)),
            'general_false_merge_rate':'NOT_MEASURED; scoped counterexamples are separate pytest evidence'},
        'financial_fact_eligibility':{'fast_ttm_revenue':fast_financial['metrics']['ttm_revenue'],
            'deep_ttm_revenue':financial['metrics']['ttm_revenue'],
            'deep_ttm_operating_cash_flow':financial['metrics']['ttm_operating_cash_flow'],
            'live_price_capital_action_eligibility':'NOT_VERIFIED'},
        'fast_vs_deep_incremental_evidence':{'status':deep['investigation']['status'],
            'new_evidence_ids':deep['investigation']['new_evidence_ids'],
            'judgment_change':deep['assessment']['judgment_change'],
            'fast_context_bytes':fast['context_manifest']['context_bytes'],
            'deep_context_bytes':deep['context_manifest']['context_bytes']},
        'action_applicability':{'position_status':deep['action_decision']['position_status'],
            'fast_action':fast['action_decision']['decision_action'],
            'deep_action':deep['action_decision']['decision_action'],
            'status':deep['action_decision']['decision_status'],
            'investment_quality':'NOT_TESTED; direction is scripted'},
        'scenario_vs_actual_isolation':'Covered by tests/test_closure_scenario.py; browser measurement absent',
        'user_visible_reasoning_completeness':{'action':bool(deep.get('action_decision')),
            'thesis_delta':bool(deep['assessment']['action']['thesis_delta']['text']),
            'counterevidence':len(deep['assessment']['counterevidence']),
            'change_conditions':len(deep['assessment']['action_conditions']),
            'browser_visibility':'NOT_TESTED unless matching browser artifact supplied'},
        'provider_and_paid_model_calls':{'real_provider_calls':0,'paid_model_calls':0,
            'actual_product_cost_usd':'0','estimated_product_cost_usd':'0',
            'scripted_model_requests_at_inspection':identity['fake_model_requests']}}
    if browser:
        result=json.loads(Path(browser).read_text(encoding='utf-8-sig'))
        assert result['status']=='PASS'
        assert result['identity']['head']==identity['head'] and result['identity']['source_digest']==identity['source_digest']
        assert result['identity']['run_id']==identity['run_id']
        report['browser_first_useful_answer_seconds']=[r['time_to_first_useful_answer_seconds'] for r in result['journeys']]
        report['user_visible_reasoning_completeness']['browser_visibility']='VERIFIED_SCOPED_DESKTOP_MOBILE'
        report['scenario_vs_actual_isolation']={'browser_checks':True,
            'meaning':'Two user-defined prices; original Action/Task/Deep unchanged, no market-move claim',
            'calculation_receipts':[r['calculation_receipt'] for r in result['journeys']]}
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base',required=True)
    p.add_argument('--browser',type=Path)
    p.add_argument('--output',required=True,type=Path)
    args=p.parse_args()
    if args.output.exists():raise ValueError('Preserve previous scorecard; choose a new output path')
    result=measure(args.base,args.browser)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':result['status'],'REAL_MODEL_QUALITY':result['REAL_MODEL_QUALITY']}))
