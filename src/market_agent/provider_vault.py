"""Reuse the existing SecureString/DPAPI store; never persist plaintext keys."""
import json
import os
from pathlib import Path
import subprocess
import threading

from .tdb import TDBError


VARIABLES = {'OpenAI':'OPENAI_API_KEY', 'DeepSeek':'DEEPSEEK_API_KEY',
    'Compatible':'LLM_API_KEY', 'Jev':'JEV_API_KEY', 'Massive':'MASSIVE_API_KEY',
    'Finnhub':'FINNHUB_API_KEY', 'Longbridge':'LONGBRIDGE_MCP_ACCESS_TOKEN', 'SEC':'SEC_USER_AGENT'}


class ProviderVault:
    def __init__(self, environment):
        self.environment = dict(environment)
        self.overrides = {}
        self.lock = threading.RLock()

    def bridge(self, operation, provider, secret=None):
        if os.name != 'nt' or provider == 'SEC':
            raise TDBError('VAULT_UNAVAILABLE', 409)
        script = Path(__file__).resolve().parents[2] / 'scripts' / 'ProviderHubBridge.ps1'
        try:
            result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive',
                '-ExecutionPolicy','Bypass','-File',str(script),'-Operation',operation,'-Provider',provider],
                input=json.dumps({'secret':secret}) if secret is not None else '',
                text=True, encoding='utf-8',capture_output=True, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise ValueError('bridge failed')
            return json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise TDBError('VAULT_OPERATION_FAILED', 409) from None

    def get(self, provider):
        with self.lock:
            if provider in self.overrides:
                return self.overrides[provider]
            if self.environment.get(VARIABLES[provider]):
                return self.environment[VARIABLES[provider]]
            if os.name != 'nt' or provider == 'SEC':
                return ''
            result = self.bridge('read', provider)
            # The service already needs keys in memory. Cache this process's
            # configuration; Hub changes replace it. External wizard edits need
            # restart, just as environment edits do. Never repeatedly spawn a
            # PowerShell process on the fast routing path.
            self.overrides[provider] = result['secret'] if result else ''
            return self.overrides[provider]

    def set(self, provider, secret):
        with self.lock:
            if provider == 'SEC':
                # Public identifying contact, not a credential; memory only.
                self.overrides[provider] = secret
            else:
                self.bridge('set', provider, secret)
                self.overrides[provider] = secret

    def remove(self, provider):
        with self.lock:
            if provider != 'SEC':
                self.bridge('remove', provider)
            self.overrides[provider] = ''

    def configured(self, provider):
        try:
            return bool(self.get(provider)), None
        except TDBError as exc:
            return False, exc.code
