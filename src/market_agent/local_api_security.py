"""Direct-loopback, same-origin and session-CSRF boundary for local secrets."""
import secrets

from fastapi.responses import JSONResponse


def direct_local(request):
    return (request.client is not None and request.client.host in {'127.0.0.1','::1'}
        and request.url.hostname in {'127.0.0.1','localhost','::1'}
        and not any(key in request.headers for key in
            ('forwarded','x-forwarded-for','x-forwarded-host','x-forwarded-proto','x-real-ip')))


def install_local_boundary(app, *, enabled):
    token = secrets.token_urlsafe(32)

    @app.middleware('http')
    async def boundary(request, call_next):
        protected = request.url.path.startswith(('/api/providers','/api/research/egress'))
        # Existing task API is protected too when the local hub is active: a hostile
        # site must not spend money or steal confirmed private research by CSRF.
        protected = protected or enabled and request.url.path.startswith('/api/research/tasks')
        if protected:
            def refuse(code, status=403):
                return JSONResponse({'detail':code}, status_code=status,
                    headers={'Cache-Control':'no-store'})
            if not enabled:
                return refuse('PROVIDER_HUB_DISABLED', 404)
            if not direct_local(request):
                return refuse('DIRECT_LOOPBACK_REQUIRED')
            origin = request.headers.get('origin')
            expected = f'{request.url.scheme}://{request.url.netloc}'
            if origin and origin != expected:
                return refuse('ORIGIN_REJECTED')
            if request.headers.get('sec-fetch-site') in {'cross-site','same-site'}:
                return refuse('ORIGIN_REJECTED')
            if request.method not in {'GET','HEAD','OPTIONS'}:
                if origin != expected or not secrets.compare_digest(request.headers.get('x-provider-csrf',''), token):
                    return refuse('CSRF_REJECTED')
                if request.method != 'DELETE' and request.headers.get('content-type','').split(';')[0] != 'application/json':
                    return refuse('JSON_REQUIRED', 415)
                # Read once through Starlette's cache; no body logging or echo.
                if len(await request.body()) > 16384:
                    return refuse('REQUEST_TOO_LARGE', 413)
        response = await call_next(request)
        if protected:
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Pragma'] = 'no-cache'
        return response

    return token


def install_task_origin_guard(app):
    """Hub-off task API guard (QA P2-02): no session token exists, so refuse what a
    hostile page can send. A cross-site page cannot set a foreign-free Origin, cannot
    claim Sec-Fetch-Site same-origin, and cannot POST application/json without a CORS
    preflight this server never grants. Non-browser loopback clients keep working."""
    @app.middleware('http')
    async def task_origin_guard(request, call_next):
        if (request.url.path.startswith('/api/research/tasks')
                and request.method not in {'GET', 'HEAD', 'OPTIONS'}):
            def refuse(code, status=403):
                return JSONResponse({'detail': code}, status_code=status, headers={'Cache-Control': 'no-store'})
            origin = request.headers.get('origin')
            if origin and origin != f'{request.url.scheme}://{request.url.netloc}':
                return refuse('ORIGIN_REJECTED')
            if request.headers.get('sec-fetch-site') in {'cross-site', 'same-site'}:
                return refuse('ORIGIN_REJECTED')
            if request.headers.get('content-type', '').split(';')[0] != 'application/json':
                return refuse('JSON_REQUIRED', 415)
        return await call_next(request)
