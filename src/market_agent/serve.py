"""Container/native entrypoint: one worker, explicit mode, platform PORT."""
import os

import uvicorn


def main():
    mode = os.environ.setdefault('MIA_RUNTIME_MODE', 'PUBLIC_DEMO')
    if mode != 'PUBLIC_DEMO':
        raise ValueError('Public serving entrypoint requires PUBLIC_DEMO; Owner keeps its existing launcher')
    port = int(os.environ.get('PORT', '8080'))
    if not 1 <= port <= 65535:
        raise ValueError('PORT must be between 1 and 65535')
    uvicorn.run('market_agent.main:app', host='0.0.0.0', port=port, workers=1,
        access_log=False, proxy_headers=False)


if __name__ == '__main__':
    main()
