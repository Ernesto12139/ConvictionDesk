"""Clean export and public identities fail closed without real network access."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'release'/(name+'.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


exporter = module('export_snapshot')
auditor = module('audit_public_git')
EMAIL = '198560242+Ernesto12139@users.noreply.github.com'


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args]).decode().strip()


def repo(tmp_path):
    root = tmp_path/'synthetic-source'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Ernesto12139')
    git(root, 'config', 'user.email', EMAIL)
    (root/'release').mkdir()
    (root/'release/public-files.json').write_text(json.dumps([{'source':'app.py','path':'app.py'}]))
    (root/'app.py').write_bytes(b'print("fixed source")\n')
    (root/'not-public.txt').write_text('Synthetic private material, excluded by default')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'test: synthetic fixed checkpoint')
    return root, git(root, 'rev-parse', 'HEAD')


def test_export_reads_exact_commit_not_worktree_and_never_copies_git(tmp_path):
    root, sha = repo(tmp_path)
    (root/'app.py').write_text('changed working file')
    target = tmp_path/'export'
    manifest = exporter.export(root, sha, target)
    assert (target/'app.py').read_bytes() == b'print("fixed source")\n'
    assert not (target/'.git').exists() and not (target/'not-public.txt').exists()
    assert manifest['files'][0]['sha256'] == hashlib.sha256((target/'app.py').read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='new or empty'):
        exporter.export(root, sha, target)


@pytest.mark.parametrize('path', ['../leak.py', '/absolute.py', '.git/config', 'C:/private.py'])
def test_path_escape_or_git_metadata_is_never_exportable(path):
    assert not exporter.safe_path(path)


def test_secrets_private_paths_and_active_svg_are_rejected():
    assert exporter.scan('app.py', ('token="'+'sk-'+'a'*32+'"').encode())
    assert exporter.scan('settings.txt', ('/home/'+'synthetic-owner/private').encode())
    assert exporter.scan('.env', b'EXAMPLE=')
    assert exporter.scan('asset.svg', b'<svg><script>bad()</script></svg>')
    assert exporter.scan('asset.svg', b'<svg><image href="https://example.com/x"/></svg>')
    assert exporter.scan('settings.txt', b'private-synthetic-marker', ['private-synthetic-marker'])


def test_owner_identity_does_not_prove_release_and_coauthor_or_refs_reject(tmp_path):
    root, sha = repo(tmp_path)
    result = auditor.audit(root, EMAIL, expected_root=sha)
    assert result['status'] == 'PASS' and not result['release_creator_verified']
    git(root, 'commit', '--allow-empty', '-m', 'test: synthetic trailer\n\nCo-Authored-By: automated assistant')
    assert auditor.audit(root, EMAIL)['status'] == 'FAIL'
    git(root, 'branch', 'unapproved-history-ref')
    assert 'unexpected branch/tag/ref' in auditor.audit(root, EMAIL)['errors']


def test_nonowner_author_wrong_ancestry_and_release_creator_reject(tmp_path):
    root, sha = repo(tmp_path)
    git(root, 'config', 'user.name', 'Not Owner')
    git(root, 'commit', '--allow-empty', '-m', 'test: synthetic wrong identity')
    assert auditor.audit(root, EMAIL)['status'] == 'FAIL'
    assert auditor.audit(root, EMAIL, forbidden_ancestors=[sha])['status'] == 'FAIL'
    release = {'author': {'login':'not-owner'}, 'tag_name':'v0.8.1', 'draft':False}
    assert 'release creator is not Owner' in auditor.audit(root, EMAIL, release=release)['errors']
