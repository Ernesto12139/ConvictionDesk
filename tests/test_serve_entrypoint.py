"""Native preview binds loopback by default; containers opt in to all interfaces."""
import socket

import pytest

from market_agent import serve


def test_native_preview_defaults_to_loopback():
    assert serve.bind_address({}) == ('127.0.0.1', 8080)


def test_container_opts_in_to_all_interfaces_and_custom_port():
    assert serve.bind_address({'HOST': '0.0.0.0', 'PORT': '9001'}) == ('0.0.0.0', 9001)


@pytest.mark.parametrize('environment', [{'HOST': ' '}, {'PORT': '0'}, {'PORT': '70000'}])
def test_invalid_bind_settings_are_rejected(environment):
    with pytest.raises(ValueError):
        serve.bind_address(environment)


def test_busy_port_fails_with_actionable_message():
    with socket.socket() as taken:
        taken.bind(('127.0.0.1', 0))
        taken.listen()
        port = taken.getsockname()[1]
        with pytest.raises(SystemExit, match='PORT'):
            serve.ensure_port_free('127.0.0.1', port)


def test_main_runs_uvicorn_on_loopback(monkeypatch):
    seen = {}
    monkeypatch.delenv('HOST', raising=False)
    monkeypatch.setenv('MIA_RUNTIME_MODE', 'PUBLIC_DEMO')
    monkeypatch.setenv('PORT', '8080')
    monkeypatch.setattr(serve, 'ensure_port_free', lambda host, port: None)
    monkeypatch.setattr(serve.uvicorn, 'run', lambda app, **kw: seen.update(kw, app=app))
    serve.main()
    assert seen['host'] == '127.0.0.1' and seen['port'] == 8080 and seen['workers'] == 1


def test_owner_mode_is_still_refused(monkeypatch):
    monkeypatch.setenv('MIA_RUNTIME_MODE', 'OWNER')
    with pytest.raises(ValueError):
        serve.main()


def test_help_prints_usage_instead_of_serving(monkeypatch, capsys):
    """`--help` used to be ignored and the command started a server instead."""
    started = []
    monkeypatch.setattr(serve.uvicorn, 'run', lambda *a, **kw: started.append(kw))
    assert serve.main(['--help']) == 0
    assert started == []
    out = capsys.readouterr().out
    assert 'market_agent.public_ivs --serve' in out


def test_unknown_arguments_are_refused(monkeypatch):
    monkeypatch.setattr(serve.uvicorn, 'run', lambda *a, **kw: None)
    with pytest.raises(SystemExit):
        serve.main(['--nonsense'])
