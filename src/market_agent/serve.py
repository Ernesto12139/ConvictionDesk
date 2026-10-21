"""Container/native entrypoint: one worker, explicit mode, platform PORT.

Native runs listen on loopback only. Containers opt in to all interfaces with HOST=0.0.0.0
and publish the port to 127.0.0.1 on the host.
"""
import os
import socket

import uvicorn


def bind_address(environment):
    host = environment.get('HOST', '127.0.0.1').strip()
    if not host:
        raise ValueError('HOST must not be empty')
    port = int(environment.get('PORT', '8080'))
    if not 1 <= port <= 65535:
        raise ValueError('PORT must be between 1 and 65535')
    return host, port


def ensure_port_free(host, port):
    with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            raise SystemExit(f'Port {port} on {host} is already in use. Set PORT to a free port, e.g. PORT=8091.') from None


def main():
    mode = os.environ.setdefault('MIA_RUNTIME_MODE', 'PUBLIC_DEMO')
    if mode != 'PUBLIC_DEMO':
        raise ValueError('Public serving entrypoint requires PUBLIC_DEMO; Owner keeps its existing launcher')
    host, port = bind_address(os.environ)
    ensure_port_free(host, port)
    uvicorn.run('market_agent.main:app', host=host, port=port, workers=1,
        access_log=False, proxy_headers=False)


if __name__ == '__main__':
    main()
