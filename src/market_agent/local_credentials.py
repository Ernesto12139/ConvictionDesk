"""Windows current-user DPAPI storage for explicitly supplied MCP credentials.

Secrets travel over a child process pipe, never command arguments or log text.
No plaintext file or environment-variable persistence is used.
"""
import json
import os
from pathlib import Path
import subprocess


def _dpapi(value, *, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('This credential store requires Windows current-user DPAPI')
    command = (
        "$ErrorActionPreference='Stop';$v=[Console]::In.ReadToEnd();"
        "$s=ConvertTo-SecureString $v;"
        "$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s);"
        "try{[Console]::Out.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($p))}"
        "finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}"
    ) if decrypt else (
        "$ErrorActionPreference='Stop';$v=[Console]::In.ReadToEnd();"
        "$s=ConvertTo-SecureString $v -AsPlainText -Force;"
        "[Console]::Out.Write((ConvertFrom-SecureString $s))"
    )
    try:
        result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', command],
            input=value, text=True, encoding='utf-8', capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Local credential protection timed out; secret output suppressed') from None
    if result.returncode:
        raise RuntimeError('Local credential protection failed; secret output suppressed')
    return result.stdout


def save_credentials(path, credentials):
    path = Path(path).resolve()
    if path.suffix != '.dpapi':
        raise ValueError('Protected credential filename must end in .dpapi')
    if path.exists():
        raise ValueError('Existing credentials are preserved; use a new explicit filename')
    if not isinstance(credentials, dict) or not isinstance(credentials.get('access_token'), str) or not credentials['access_token']:
        raise ValueError('MCP credential shape invalid')
    # Official auth message includes token examples; it has no storage value.
    retained = {key: credentials[key] for key in ('access_token', 'refresh_token',
        'token_type', 'expires_in', 'scopes', 'account_channel', 'status') if key in credentials}
    protected = _dpapi(json.dumps(retained))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='ascii') as stream:
        stream.write(protected)


def load_credentials(path):
    path = Path(path)
    if path.suffix != '.dpapi':
        raise ValueError('Only DPAPI-protected credential files are supported')
    value = json.loads(_dpapi(path.read_text(encoding='ascii'), decrypt=True))
    if not isinstance(value, dict) or not isinstance(value.get('access_token'), str) or not value['access_token']:
        raise ValueError('Protected MCP credential shape invalid')
    return value
