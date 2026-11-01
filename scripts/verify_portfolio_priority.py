"""Real-process HTTP + restart acceptance with a process-wide outbound guard."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx


def child(root, port):
    # Proven before starting the app; asyncio self-pipe and loopback remain usable.
    real_connect, real_connect_ex, real_dns = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
    attempts = []
    def record(host):
        attempts.append(str(host))
        (root.parent / 'outbound-guard.json').write_text(json.dumps({
            'guard_proven': True, 'application_outbound_attempts': attempts}), encoding='utf-8')
    def guard(sock, address):
        if isinstance(address, tuple) and address[0] not in {'127.0.0.1', 'localhost', '::1'}:
            record(address); raise OSError('OUTBOUND_DISABLED')
        return real_connect(sock, address)
    def connect_ex(sock, address):
        if isinstance(address, tuple) and address[0] not in {'127.0.0.1', 'localhost', '::1'}:
            record(address); raise OSError('OUTBOUND_DISABLED')
        return real_connect_ex(sock, address)
    def dns(host, *args, **kwargs):
        if host not in {'127.0.0.1', 'localhost', '::1', None}:
            record(host); raise OSError('OUTBOUND_DNS_DISABLED')
        return real_dns(host, *args, **kwargs)
    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = guard, connect_ex, dns
    try:
        socket.getaddrinfo('proof.invalid', 443)
    except OSError:
        assert attempts == ['proof.invalid']
    attempts.clear()
    (root.parent / 'outbound-guard.json').write_text(json.dumps({
        'guard_proven': True, 'application_outbound_attempts': []}), encoding='utf-8')
    from market_agent.portfolio_priority_journey import build_app
    import uvicorn
    try:
        uvicorn.run(build_app(root), host='127.0.0.1', port=port, access_log=False)
    finally:
        (root.parent / 'outbound-guard.json').write_text(json.dumps({
            'guard_proven': True, 'application_outbound_attempts': attempts}), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if args.child:
        if args.port == 8000: raise SystemExit('OWNER_PORT_REFUSED')
        child(args.output / 'state', args.port); return
    folder = args.output.resolve()
    if folder.exists() and any(folder.iterdir()):
        raise SystemExit('Use a fresh verification output directory; previous evidence is preserved')
    folder.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
    assert port != 8000
    base = f'http://127.0.0.1:{port}'
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    def start():
        stream = (folder / 'server.log').open('a', encoding='utf-8')
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child',
            '--output', str(folder), '--port', str(port)], env=env, stdout=stream, stderr=stream)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stream.close(); raise AssertionError('CHILD_BOOT_FAILED: see server.log')
            try:
                if httpx.get(base + '/health', timeout=1).status_code == 200: return process, stream
            except httpx.HTTPError: pass
            time.sleep(.1)
        process.terminate(); process.wait(10); stream.close(); raise AssertionError('CHILD_BOOT_DEADLINE')
    def stop(process, stream):
        # Terminate only the process this script owns; Windows termination is abrupt,
        # deliberately exercising SQLite/history recovery rather than a fake reload.
        process.terminate(); process.wait(15); stream.close()
    process, stream = start()
    try:
        with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
            from evals.portfolio_priority_eval import run
            result = run(client)
            tid = result['task_id']
            frozen = client.get(f'/api/research/tasks/{tid}/revisions/1').content
            overview = client.get('/api/portfolio-priority').content
        stop(process, stream)
        process, stream = start()
        with httpx.Client(base_url=base, timeout=20, trust_env=False) as client:
            assert client.get(f'/api/research/tasks/{tid}/revisions/1').content == frozen
            assert client.get(f'/api/research/tasks/{tid}').json()['revision'] == 2
            assert client.get('/api/portfolio-priority').content == overview
            result['restart_recovery'] = 'PASS_BYTE_IDENTICAL_HISTORY_AND_SNAPSHOT'
            result['http_base'] = base
        (folder / 'HTTP_EVAL.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
        print(json.dumps({'verdict': 'PASS', 'checks': len(result['checks']),
            'restart': result['restart_recovery'], 'report': str(folder / 'HTTP_EVAL.json')}))
    finally:
        if process.poll() is None: stop(process, stream)


if __name__ == '__main__':
    main()
