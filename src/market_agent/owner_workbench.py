"""Trusted local composition of the existing Console and approved Owner value slice.

No default Owner path, provider fetch, synthetic mark substitution or model send.
The operator supplies one source-qualified local input bundle; HTTP cannot replace it.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from .domain import FrozenModel
from .owner_value_slice import (SecEvent, SecFilingBody, PriceObservationInput,
    ThesisRecord, WhatIfRequest, build_owner_value_slice)
from .owner_value_view import to_ui_view
from .tdb import content_hash


class OwnerInputs(FrozenModel):
    snapshot_id: str
    snapshot_hash: str
    account_id: str
    symbol: str
    event: SecEvent
    filing_body: SecFilingBody | None = None
    price: PriceObservationInput | None = None
    thesis: ThesisRecord | None = None


def checked_runtime_root(root):
    import os
    requested = Path(root).absolute()
    for entry in (requested, *requested.parents):
        if entry.is_symlink() or getattr(entry, 'is_junction', lambda: False)():
            raise ValueError('OWNER_RUNTIME_LINK_REFUSED')
    root = requested.resolve()
    marker = root / 'OWNER_WORKBENCH_LOCAL.json'
    identity = {'kind': 'OWNER_LOCAL_WITH_ISOLATED_KEYLESS_SAMPLE', 'version': 1}
    if root.exists():
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                entry = Path(directory) / name
                if entry.is_symlink() or getattr(entry, 'is_junction', lambda: False)():
                    raise ValueError('OWNER_RUNTIME_LINK_REFUSED')
        if any(root.iterdir()) and (not marker.is_file() or json.loads(marker.read_text()) != identity):
            raise ValueError('OWNER_RUNTIME_MARKER_REQUIRED')
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(identity), encoding='utf-8')
    return root


def build_owner_workbench(root, database, inputs, environment, *, code=None, clock=None):
    from .unified_workbench import _main_factory, build_app, Scope
    from .run_trace import TraceRecorder, export_trace, SourceRef
    from .workbench_trace import install_workbench_trace, workbench_code_identity
    root = checked_runtime_root(root)
    database = Path(database).resolve(strict=True)
    if database.is_relative_to(root / 'sample'):
        raise ValueError('OWNER_DATABASE_IN_SAMPLE_SCOPE_REFUSED')
    if environment.get('MIA_PROVIDER_HUB') != '1' or environment.get('MIA_RUNTIME_MODE') != 'OWNER':
        raise ValueError('EXPLICIT_OWNER_HUB_REQUIRED')
    inputs = OwnerInputs.model_validate(inputs)
    clock = clock or (lambda: datetime.now(timezone.utc))
    code = code or workbench_code_identity()
    factory = _main_factory(root)
    # Saved data-vendor credentials are not a source-rights admission. Keep the
    # real Vault available to the Hub, but do not let background Console GETs
    # dispatch unqualified news/quote requests on the strength of a saved key.
    from .runtime_provider_hub import ProviderHub
    hub = ProviderHub(environment)
    source_environment = dict(environment)
    for provider, variable in [('Finnhub','FINNHUB_API_KEY'), ('Massive','MASSIVE_API_KEY'),
                               ('Longbridge','LONGBRIDGE_MCP_ACCESS_TOKEN')]:
        source_environment.pop(variable, None)
        source_environment['MIA_SOURCE_POLICY_' + provider.upper()] = 'RIGHTS_BLOCKED'
    source_environment.pop('MIA_RESEARCH_MCP', None)
    source_environment.pop('MIA_LONGBRIDGE_CREDENTIALS', None)
    owner = factory(database, environment=source_environment, provider_hub=hub)
    store = owner.state.research_service.store
    sink = install_workbench_trace(owner, root / 'owner-traces', code, 'OWNER_LOCAL')

    def evaluate(whatif=None):
        now = clock()
        recorder = TraceRecorder(run_id=str(uuid4()), execution_mode='OWNER_LOCAL',
                                 as_of=now, pit_cutoff=now, code=code)
        with recorder.record_tool('portfolio.local_snapshot', input_class={'scope': 'OWNER_LOCAL'}) as span:
            snapshot = store.get_snapshot()
            span.output_count = int(snapshot is not None)
        if (snapshot is None or snapshot.snapshot_id != inputs.snapshot_id
                or content_hash(snapshot.model_dump(mode='json')) != inputs.snapshot_hash):
            recorder.record_gate('CONTRACT', decision='DENY', code='OWNER_INPUT_SNAPSHOT_CHANGED')
            trace_id = sink(export_trace(recorder.close(status='BLOCKED', error_code='OWNER_INPUT_SNAPSHOT_CHANGED')))
            raise HTTPException(409, detail={'code': 'OWNER_INPUT_SNAPSHOT_CHANGED', 'trace_id': trace_id})
        with recorder.record_tool('owner_value.qualify_and_compute', input_class={'scope': 'OWNER_LOCAL'}) as span:
            result = build_owner_value_slice(snapshot=snapshot, account_id=inputs.account_id,
                symbol=inputs.symbol, evaluation_time=now, event=inputs.event,
                filing_body=inputs.filing_body, price=inputs.price, thesis=inputs.thesis,
                whatif=whatif, redaction='LOCAL_FULL')
            span.output_count = 1
            span.sources = [SourceRef(source_id=inputs.event.accession, kind='PUBLIC_SEC_CAPTURED',
                accepted_at=inputs.event.accepted_at, captured_at=inputs.event.captured_at,
                available_at=max(inputs.event.accepted_at, inputs.event.captured_at),
                content_hash=inputs.event.source_hash, rights=inputs.event.rights,
                pit_ok=result.event.status == 'ELIGIBLE')]
            span.notes = ['QUALIFIED_PRICE' if result.price.valuation_eligible else 'PRICE_UNAVAILABLE',
                          'HYPOTHETICAL_RECEIPT_ONLY' if whatif else 'READ_ONLY_LOCAL_INPUTS']
        recorder.record_gate('PIT', decision='ALLOW' if result.event.status == 'ELIGIBLE' else 'DENY',
                             code=result.event.status)
        recorder.record_gate('RIGHTS', decision='ALLOW' if result.price.valuation_eligible else 'DENY',
                             code=result.price.status)
        trace_id = sink(export_trace(recorder.close(status='COMPLETE')))
        return {'scope': 'OWNER_LOCAL', 'source_mode': 'PUBLIC_SEC_CAPTURED',
            'live_fetch_performed': False, 'model_calls': 0,
            'view': to_ui_view(result).model_dump(mode='json'),
            'qualification': {'event': result.event.status, 'body': result.filing_body.status,
                'price': result.price.status, 'thesis': result.thesis_check.status},
            'trace_id': trace_id, 'research_dispatch': 'REQUIRES_SCOPED_CONSENT_RIGHTS_AND_BUDGET',
            'slice_hash': content_hash(result.model_dump(mode='json'))}

    @owner.get('/api/owner/value')
    def value():
        return evaluate()

    @owner.post('/api/owner/whatif')
    def whatif(body: WhatIfRequest):
        return evaluate(body)

    app = build_app(root / 'sample', owner_app=owner, default_scope=Scope.OWNER_LOCAL, trace_code=code)
    app.state.owner_inputs = inputs
    return app


def main():
    import argparse
    import os
    import socket
    import sqlite3
    from .owner_provider_recovery import recovery_environment
    from .provider_vault import ProviderVault
    from .workbench_trace import workbench_code_identity
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--inputs', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8797)
    parser.add_argument('--production-admission', type=Path)
    parser.add_argument('--budget-config', type=Path)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit('INVALID_PORT')
    code = workbench_code_identity()
    if args.port == 8000:
        if args.production_admission is None or code.dirty is not False:
            raise SystemExit('PRODUCTION_ADMISSION_REQUIRED')
        admission = json.loads(args.production_admission.read_text(encoding='utf-8'))
        if (admission.get('candidate_sha') != code.commit or admission.get('git_tree') != code.tree
                or admission.get('security_go') != 'ACCEPTED' or admission.get('product_go') != 'ACCEPTED'
                or admission.get('private_ci') != 'SUCCESS' or admission.get('backup_restore') != 'PASS'):
            raise SystemExit('PRODUCTION_ADMISSION_MISMATCH')
        # This local receipt indexes externally verified gates; the sole operator
        # must verify original reviewer/CI receipts, not manufacture their verdicts.
    database, backup = args.database.resolve(strict=True), args.backup.resolve(strict=True)
    if os.path.samefile(database, backup):
        raise SystemExit('SEPARATE_VERIFIED_BACKUP_REQUIRED')
    for path in (database, backup):
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
            connection.execute('PRAGMA query_only=ON')
            if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise SystemExit('DATABASE_INTEGRITY_REJECTED')
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', args.port)) == 0:
            raise SystemExit('PRESERVE_EXISTING_LISTENER')
    environment = recovery_environment(os.environ, ProviderVault(os.environ))
    environment['MIA_RUNTIME_OPERATION_CEILING_USD'] = '5'
    if args.budget_config:
        budget = json.loads(args.budget_config.read_text(encoding='utf-8'))
        allowed = {'MIA_RUNTIME_BUDGET_LEDGER','MIA_RUNTIME_RESERVATION_ID','MIA_RUNTIME_BUDGET_DIR',
                   'MIA_RUNTIME_DATA_RIGHTS'}
        if set(budget) - allowed or any(not isinstance(v,str) for v in budget.values()):
            raise SystemExit('INVALID_EXPLICIT_BUDGET_CONFIGURATION')
        environment.update(budget)
    inputs = OwnerInputs.model_validate_json(args.inputs.read_bytes())
    app = build_owner_workbench(args.root, database, inputs, environment, code=code)
    for scope_app in app.state.scopes.values():
        scope_app.state.local_acceptance = {'pid':os.getpid(), 'head':code.commit,
            'source_sha256':code.build, 'status':'ENGINEERING_CANDIDATE'}
    def identity():
        return {'pid':os.getpid(), 'port':args.port, 'code':code.model_dump(mode='json'),
                'scope':'OWNER_LOCAL_WITH_ISOLATED_KEYLESS_SAMPLE',
                'startup_model_calls':0}
    # The outer app mounts its scoped Console at '/'. Register on each child so
    # that mount dispatch cannot consume this route before it is reached.
    for scope_app in app.state.scopes.values():
        scope_app.add_api_route('/api/workbench/identity', identity, methods=['GET'])
    record = {'pid':os.getpid(), 'port':args.port,'code':code.model_dump(mode='json'),
              'scope':'OWNER_LOCAL_WITH_ISOLATED_KEYLESS_SAMPLE'}
    (Path(args.root)/'PROCESS.json').write_text(json.dumps(record,indent=2))
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=args.port,access_log=False)


if __name__ == '__main__':
    main()
