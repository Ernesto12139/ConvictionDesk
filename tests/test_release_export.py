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


def test_manifest_version_is_taken_from_the_fixed_exported_pyproject(tmp_path):
    root,_=repo(tmp_path)
    (root/'pyproject.toml').write_text('[project]\nversion = "0.9.0.dev0"\n')
    entries=json.loads((root/'release/public-files.json').read_text())
    entries.append({'source':'pyproject.toml','path':'pyproject.toml'})
    (root/'release/public-files.json').write_text(json.dumps(entries))
    git(root,'add','pyproject.toml','release/public-files.json')
    git(root,'commit','-m','test: synthetic development version')
    sha=git(root,'rev-parse','HEAD')
    (root/'pyproject.toml').write_text('[project]\nversion = "9.9.9"\n')
    manifest=exporter.export(root,sha,tmp_path/'fixed-version')
    assert manifest['source_version']=='0.9.0.dev0'
    assert manifest['upstream_source_sha']==sha


def test_secrets_private_paths_and_active_svg_are_rejected():
    assert exporter.scan('app.py', ('token="'+'sk-'+'a'*32+'"').encode())
    assert exporter.scan('scripts/bridge.ps1', ('token="'+'sk-'+'a'*32+'"').encode())
    assert exporter.scan('scripts/bridge.ps1', b'param([string]$Operation)') == []
    assert exporter.scan('settings.txt', ('/home/'+'synthetic-owner/private').encode())
    assert exporter.scan('.env', b'EXAMPLE=')
    assert exporter.scan('asset.svg', b'<svg><script>bad()</script></svg>')
    assert exporter.scan('asset.svg', b'<svg><image href="https://example.com/x"/></svg>')
    assert exporter.scan('settings.txt', b'private-synthetic-marker', ['private-synthetic-marker'])


def test_default_scan_rejects_private_distribution_name():
    private_name = 'market-' + 'intelligence-' + 'agent'
    assert 'private project identifier' in exporter.scan(
        'pyproject.toml', ('name = "' + private_name + '"').encode())
    assert exporter.scan('pyproject.toml', b'name = "convictiondesk"') == []


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


def test_next_release_extends_same_root_and_preserves_old_annotated_tag(tmp_path):
    root,sha=repo(tmp_path)
    git(root,'tag','-a','v0.8.1','-m','Synthetic stable release')
    pinned=git(root,'rev-parse','v0.8.1')
    git(root,'commit','--allow-empty','-m','test: next fixed source snapshot')
    git(root,'tag','-a','v0.9.0','-m','Synthetic next release')
    release={'author':{'login':'Ernesto12139'},'tag_name':'v0.9.0','draft':False,'prerelease':False}
    result=auditor.audit(root,EMAIL,expected_root=sha,release=release,protected_tags={'v0.8.1':pinned})
    assert result['status']=='PASS',result['errors']
    assert result['commits']==2 and result['root']==sha and result['release_creator_verified']
    assert git(root,'rev-parse','v0.8.1^{commit}')==sha
    assert auditor.audit(root,EMAIL,expected_root='f'*40)['status']=='FAIL'


def test_lightweight_and_nonowner_tags_are_rejected(tmp_path):
    root,_=repo(tmp_path)
    git(root,'tag','v0.9.0')
    assert auditor.audit(root,EMAIL)['status']=='FAIL'
    git(root,'-c','user.name=Not Owner','tag','-a','v1.0.0','-m','Synthetic wrong tagger')
    assert any('tagger identity' in e for e in auditor.audit(root,EMAIL)['errors'])


def test_same_commit_retag_still_violates_protected_annotation(tmp_path):
    root,_=repo(tmp_path)
    git(root,'tag','-a','v0.8.1','-m','Original synthetic annotation')
    pinned=git(root,'rev-parse','v0.8.1')
    git(root,'tag','-f','-a','v0.8.1','-m','Changed synthetic annotation')
    result=auditor.audit(root,EMAIL,protected_tags={'v0.8.1':pinned})
    assert 'protected release tag changed or missing: v0.8.1' in result['errors']


def png(*chunks):
    import zlib
    body = b''.join(len(d).to_bytes(4, 'big')+k+d+zlib.crc32(k+d).to_bytes(4, 'big') for k, d in chunks)
    return bytes.fromhex('89504e470d0a1a0a')+body


def test_only_metadata_free_pngs_under_docs_assets_are_exportable():
    ihdr = (b'IHDR', (1).to_bytes(4, 'big')*2+bytes([8, 2, 0, 0, 0]))
    idat, iend = (b'IDAT', b'x\x9cc\xf8\x0f\x00\x01\x01\x01\x00'), (b'IEND', b'')
    clean = png(ihdr, idat, iend)
    assert exporter.scan('docs/assets/shot.png', clean) == []
    assert exporter.scan('src/shot.png', clean)
    assert exporter.scan('docs/assets/shot.png', png(ihdr, (b'tEXt', b'Author\x00x'), idat, iend))
    assert exporter.scan('docs/assets/shot.png', png(ihdr, (b'eXIf', b'MM'), idat, iend))
    assert exporter.scan('docs/assets/shot.png', clean[:-1]+bytes([clean[-1] ^ 1]))
    assert exporter.scan('docs/assets/shot.png', clean+b'\0'*exporter.PNG_MAX_BYTES)
    assert exporter.scan('docs/assets/shot.jpg', b'\xff\xd8\xff')


def test_open_development_branches_are_allowed_but_still_audited(tmp_path):
    root, sha = repo(tmp_path)
    git(root, 'update-ref', 'refs/remotes/origin/docs/presentation-002', 'HEAD')
    git(root, 'update-ref', 'refs/remotes/origin/codex/fix-annotated-release-ci', 'HEAD')
    assert auditor.audit(root, EMAIL, expected_root=sha)['status'] == 'PASS'
    # Commits reachable only from a development branch keep the identity/trailer audit.
    git(root, 'checkout', '-q', '-b', 'scratch')
    git(root, 'commit', '--allow-empty', '-m', 'docs: synthetic' + chr(10) * 2 + 'Co-Authored-By: automated assistant')
    git(root, 'update-ref', 'refs/remotes/origin/docs/bad-trailer', 'HEAD')
    git(root, 'checkout', '-q', '-')
    git(root, 'branch', '-D', 'scratch')
    assert any('forbidden commit trailer' in e for e in auditor.audit(root, EMAIL)['errors'])


def test_unknown_branch_namespaces_and_shapes_still_reject(tmp_path):
    root, sha = repo(tmp_path)
    for ref in ('refs/remotes/origin/release/v0.9.0', 'refs/remotes/origin/private-history',
                'refs/remotes/origin/docs/a/b', 'refs/remotes/upstream/docs/x', 'refs/heads/docs/x',
                'refs/remotes/origin/docs/'):
        if ref.endswith('/'):
            continue
        git(root, 'update-ref', ref, 'HEAD')
        assert 'unexpected branch/tag/ref' in auditor.audit(root, EMAIL)['errors'], ref
        git(root, 'update-ref', '-d', ref)


SERVICE_SHA = 'c83cbecd8b0a5d0a622ac95fed1b13453ed24bfc'
def published_service_identities():
    return git(ROOT,'show','-s','--format=%an%x00%ae%x00%cn%x00%ce',SERVICE_SHA).split('\x00')


def require_real_service_object():
    result = subprocess.run(['git','cat-file','-e',SERVICE_SHA+'^{commit}'],cwd=ROOT,capture_output=True)
    if result.returncode:
        pytest.skip('Published signature object absent from an exported non-Git snapshot')


def test_exact_published_signed_squash_has_a_source_bound_witness():
    require_real_service_object()
    assert auditor.pinned_signed_service_merge(ROOT,SERVICE_SHA,published_service_identities())


@pytest.mark.parametrize('field,value', [('tree','0'*40),('signature_sha256','0'*64)])
def test_altered_witness_cannot_accept_the_published_object(tmp_path,monkeypatch,field,value):
    require_real_service_object()
    records=json.loads((ROOT/'release/verified_service_merges.json').read_text())
    records[SERVICE_SHA][field]=value
    (tmp_path/'verified_service_merges.json').write_text(json.dumps(records))
    monkeypatch.setattr(auditor,'__file__',str(tmp_path/'audit_public_git.py'))
    assert not auditor.pinned_signed_service_merge(ROOT,SERVICE_SHA,published_service_identities())


def test_copying_github_committer_name_does_not_bypass_owner_identity(tmp_path):
    require_real_service_object()
    root,first=repo(tmp_path)
    git(root,'-c','user.name=GitHub','-c','user.email='+published_service_identities()[3],'commit',
        '--allow-empty','--author=Ernesto12139 <'+EMAIL+'>','-m','test: synthetic forged service identity')
    forged=git(root,'rev-parse','HEAD')
    assert forged != SERVICE_SHA
    result=auditor.audit(root,EMAIL,expected_root=first)
    assert 'author/committer identity: '+forged in result['errors']
    assert result['witnessed_service_commits']==[]


MERGE_SHA = '7bdaf6aef1a9e351a36c78d3ed37488081df2f08'
MERGE_PARENTS = ['5950bae3a695fb2a9ac2745f8213cbfadacb678a',
                 '3ca2dc7b2e2b58455663a5ce0ab9e43dfa2eae45']
MERGE_WITNESS = {
    'verification_status': 'GITHUB_API_VERIFIED',
    'parents': MERGE_PARENTS,
    'tree': 'e2c688aa8629c370df2389f81d825e9a3e01d4e5',
    'identities_sha256': '91a5cb7e65f66dcde1025d01cef2a415ab5e98bed64923184826a29c75cf46e7',
    'signature_sha256': 'a2e70da00188f143f756e2d5ee4b8643049106073767617bf33e72f501dc9d25',
}


def require_merge_object():
    result = subprocess.run(['git', 'cat-file', '-e', MERGE_SHA+'^{commit}'], cwd=ROOT, capture_output=True)
    if result.returncode:
        pytest.skip('Published signed merge absent from an exported non-Git snapshot')


def merge_identities():
    return git(ROOT, 'show', '-s', '--format=%an%x00%ae%x00%cn%x00%ce', MERGE_SHA).split('\x00')


def install_merge_witness(tmp_path, monkeypatch, changes=None, *, listed=True):
    records = json.loads((ROOT/'release/verified_service_merges.json').read_text())
    records.pop(MERGE_SHA, None)
    if listed:
        records[MERGE_SHA] = {**MERGE_WITNESS, **(changes or {})}
    (tmp_path/'verified_service_merges.json').write_text(json.dumps(records))
    monkeypatch.setattr(auditor, '__file__', str(tmp_path/'audit_public_git.py'))


def test_exact_signed_two_parent_merge_requires_its_ordered_source_witness(tmp_path, monkeypatch):
    require_merge_object()
    assert git(ROOT, 'show', '-s', '--format=%P', MERGE_SHA).split() == MERGE_PARENTS
    install_merge_witness(tmp_path, monkeypatch)
    assert auditor.pinned_signed_service_merge(ROOT, MERGE_SHA, merge_identities())


def test_published_two_parent_witness_is_bound_in_source():
    require_merge_object()
    assert auditor.pinned_signed_service_merge(ROOT, MERGE_SHA, merge_identities())


@pytest.mark.parametrize('changes', [
    {'parents': list(reversed(MERGE_PARENTS))},
    {'parents': ['0'*40, MERGE_PARENTS[1]]},
    {'parents': MERGE_PARENTS[:1]},
    {'parents': [*MERGE_PARENTS, '0'*40]},
    {'parents': [MERGE_PARENTS[0]]*2},
    {'parents': ' '.join(MERGE_PARENTS)},
    {'parents': [None, MERGE_PARENTS[1]]},
    {'parent': MERGE_PARENTS[0]},
    {'tree': '0'*40},
    {'identities_sha256': '0'*64},
    {'signature_sha256': '0'*64},
    {'verification_status': 'UNVERIFIED'},
])
def test_two_parent_witness_tampering_fails_closed(tmp_path, monkeypatch, changes):
    require_merge_object()
    install_merge_witness(tmp_path, monkeypatch, changes)
    assert not auditor.pinned_signed_service_merge(ROOT, MERGE_SHA, merge_identities())


@pytest.mark.parametrize('field,value', [(0, 'Not Owner'), (1, 'different@example.com'),
    (2, 'Impersonator'), (3, 'different@example.com')])
def test_exact_signed_merge_does_not_admit_changed_identity(tmp_path, monkeypatch, field, value):
    require_merge_object()
    install_merge_witness(tmp_path, monkeypatch)
    identities = merge_identities(); identities[field] = value
    assert not auditor.pinned_signed_service_merge(ROOT, MERGE_SHA, identities)


def test_unlisted_real_signed_merge_does_not_get_a_service_name_exemption(tmp_path, monkeypatch):
    require_merge_object()
    install_merge_witness(tmp_path, monkeypatch, listed=False)
    assert merge_identities()[:2] == ['Ernesto12139', EMAIL]
    assert merge_identities()[2:] == ['GitHub', 'noreply@github.com']
    assert not auditor.pinned_signed_service_merge(ROOT, MERGE_SHA, merge_identities())
