"""Scope-local immutable execution receipts for the existing research service."""
import json
import hashlib
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from .research_execution_trace import ResearchExecutionTrace, install_execution_trace


def workbench_code_identity():
    from .run_trace import CodeIdentity
    package = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(package.rglob('*')):
        if path.is_file() and path.suffix in {'.py','.js','.css','.html','.json','.svg'}:
            digest.update(path.relative_to(package).as_posix().encode())
            digest.update(b'\0')
            digest.update(path.read_bytes().replace(b'\r\n', b'\n'))
    build = 'runtime-sha256:' + digest.hexdigest()
    root = package.parents[1]
    try:
        def git(*args):
            return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL,
                                           text=True,encoding='utf-8',timeout=10).strip()
        if Path(git('rev-parse','--show-toplevel')).resolve() != root.resolve():
            raise OSError('Runtime package does not belong to this Git root')
        return CodeIdentity(commit=git('rev-parse','HEAD'), tree=git('rev-parse','HEAD^{tree}'),
            build=build, dirty=bool(git('status','--porcelain','--','src','scripts','tests','evals')), source='GIT')
    except (OSError, subprocess.SubprocessError):
        return CodeIdentity(commit='UNKNOWN',build=build,source='UNKNOWN')


def install_workbench_trace(app, directory, code, execution_mode):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    def sink(document):
        identifier = str(uuid4())
        with (directory / (identifier + '.json')).open('x', encoding='utf-8') as stream:
            json.dump(document, stream, ensure_ascii=False)
        return identifier

    install_execution_trace(app, ResearchExecutionTrace(code=code, sink=sink,
                                                       execution_mode=execution_mode))

    @app.get('/api/execution-traces/{trace_id}')
    def read(trace_id: UUID):
        path = directory / (str(trace_id) + '.json')
        if not path.is_file():
            raise HTTPException(404, detail='TRACE_NOT_FOUND')
        return json.loads(path.read_text(encoding='utf-8'))

    @app.get('/api/research/tasks/{task_id}/execution-traces')
    def task_traces(task_id: UUID):
        # The task must belong to this scope's actual database.
        task = app.state.research_service.tasks.get(str(task_id))
        if task is None:
            raise HTTPException(404, detail='RESEARCH_TASK_NOT_FOUND')
        rows = []
        for path in sorted(directory.glob('*.json')):
            value = json.loads(path.read_text(encoding='utf-8'))
            if value.get('task_id') == str(task_id):
                rows.append({'trace_id': path.stem, 'trace': value})
        return {'execution_mode': execution_mode, 'receipts': rows}
    return sink
