"""Persistent research jobs, with polling and bounded in-process execution."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import Query, Request

from .research_task import ResearchRequest, ResearchService, ResearchTask
from .dated_valuation import UserPriceRequest, dated_valuation
from .portfolio_whatif import WhatIfRequest, WhatIfUnavailable, scope, simulate
from .technical_zones import TechnicalRequest, technical_zones


def register_research(app, agent, *, reference_provider=None, decision_component=None,model_profiles=None,
                      local_capital_reference=None):
    service = ResearchService(agent,reference_provider=reference_provider,decision_component=decision_component,model_profiles=model_profiles)
    service.tasks.recover(service.now())
    running = set()
    app.state.research_service = service
    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with existing_lifespan(application):
            try:
                yield
            finally:
                for task in running:
                    task.cancel()
                await asyncio.gather(*list(running), return_exceptions=True)
    app.router.lifespan_context = lifespan

    @app.get('/api/research/model-profiles')
    def profiles():
        return {'profiles':model_profiles.describe() if model_profiles else []}

    @app.post('/api/research/tasks', status_code=202, response_model=ResearchTask)
    async def research(request: ResearchRequest):
        task, previous, snapshot = service.begin(request)
        job = asyncio.create_task(service.execute(task, previous, snapshot))
        running.add(job)
        def done(completed):
            running.discard(completed)
            if not completed.cancelled():
                completed.exception()  # Observe; no provider/private exception prose.
        job.add_done_callback(done)
        return task

    @app.get('/api/research/tasks', response_model=list[ResearchTask])
    def recent():
        return service.tasks.recent()

    @app.get('/api/research/tasks/{task_id}', response_model=ResearchTask)
    def read(task_id: str):
        return service.tasks.get(task_id)

    @app.get('/api/research/tasks/{task_id}/revisions')
    def revisions(task_id: str):
        return service.tasks.revisions(task_id)

    @app.get('/api/research/tasks/{task_id}/valuation')
    def valuation(task_id: str, request: Request, revision: int | None = Query(default=None,ge=1)):
        from .valuation import valuation_report
        # Frozen computation stays separate from the optional local display.
        # Neither projection fetches, saves or enters ResearchService/model facts.
        task=service.tasks.revision(task_id,revision) if revision else service.tasks.get(task_id)
        report=valuation_report(task).model_dump(mode='json')
        if local_capital_reference is not None:
            # Explicit personal display must not become an exposed server or
            # reverse-proxy redistribution endpoint. No forwarded identity trust.
            local=(request.client and request.client.host in {'127.0.0.1','::1'}
                and request.url.hostname in {'127.0.0.1','localhost','::1'}
                and not any(key in request.headers for key in ('forwarded','x-forwarded-for','x-real-ip')))
            report['local_capital_reference']=(local_capital_reference.read(task.focus_symbol,task.cutoff)
                if local else {'status':'UNAVAILABLE','reason':'PERSONAL_DISPLAY_REQUIRES_DIRECT_LOOPBACK'})
        return report

    @app.get('/api/research/tasks/{task_id}/revisions/{revision}', response_model=ResearchTask)
    def revision(task_id: str, revision: int):
        return service.tasks.revision(task_id, revision)

    @app.post('/api/research/tasks/{task_id}/dated-valuation')
    def evaluate_dated_price(task_id: str, request: UserPriceRequest):
        # Explicit local arithmetic, no ResearchService.begin/execute, provider,
        # task update, model input or saved-history rewrite. Revision is pinned.
        task = service.tasks.revision(task_id, request.revision)
        return dated_valuation(task, request, service.now())

    def whatif_snapshot(task):
        from .domain import PortfolioSnapshot
        with service.store._connect() as db:
            row = db.execute('SELECT payload FROM portfolio_snapshots WHERE snapshot_id=?',
                             (task.snapshot_id,)).fetchone()
        return PortfolioSnapshot.model_validate_json(row[0]) if row else None

    @app.get('/api/research/tasks/{task_id}/what-if')
    def whatif_scope(task_id: str, revision: int = Query(ge=1)):
        from fastapi import HTTPException
        task = service.tasks.revision(task_id, revision)
        try:
            return scope(task, whatif_snapshot(task))
        except WhatIfUnavailable as error:
            raise HTTPException(422, str(error)) from None

    @app.post('/api/research/tasks/{task_id}/what-if')
    def account_whatif(task_id: str, request: WhatIfRequest):
        from fastapi import HTTPException
        task = service.tasks.revision(task_id, request.revision)
        try:
            return simulate(task, whatif_snapshot(task), request)
        except WhatIfUnavailable as error:
            raise HTTPException(422, str(error)) from None

    @app.post('/api/research/tasks/{task_id}/technical-zones')
    def compute_technical_zones(task_id: str, request: TechnicalRequest):
        task=service.tasks.revision(task_id,request.revision)
        return technical_zones(task,request)

    @app.get('/api/research/tasks/{task_id}/technical-example')
    def technical_example(task_id: str, revision: int = Query(ge=1)):
        import json
        from importlib.resources import files
        task=service.tasks.revision(task_id,revision)
        data=json.loads(files('market_agent').joinpath('demo_assets/technical-example-v1.json').read_text(encoding='utf-8'))
        data['symbol']=task.focus_symbol
        return {'revision':revision,'series':data}
