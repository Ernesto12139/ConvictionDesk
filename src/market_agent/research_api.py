"""Persistent research jobs, with polling and bounded in-process execution."""
import asyncio
from contextlib import asynccontextmanager

from .research_task import ResearchRequest, ResearchService, ResearchTask


def register_research(app, agent, *, reference_provider=None, decision_component=None,model_profiles=None):
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

    @app.get('/api/research/tasks/{task_id}/revisions/{revision}', response_model=ResearchTask)
    def revision(task_id: str, revision: int):
        return service.tasks.revision(task_id, revision)
