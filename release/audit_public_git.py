"""Fail closed on non-Owner public authors, trailers, ancestry, refs or release creator."""
import argparse
import json
import re
import subprocess
from pathlib import Path


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'core.fsmonitor=false', '-C', str(root), *args]).decode('utf-8').strip()


def audit(root, email, *, expected_root=None, release=None, forbidden_ancestors=()):
    errors = []
    commits = git(root, 'rev-list', '--all').splitlines()
    roots = git(root, 'rev-list', '--max-parents=0', '--all').splitlines()
    if len(roots) != 1 or expected_root and roots != [expected_root]:
        errors.append('expected exactly one new root commit')
    for sha in commits:
        fields = git(root, 'show', '-s', '--format=%an%x00%ae%x00%cn%x00%ce%x00%B', sha).split('\x00')
        if fields[:4] != ['Ernesto12139', email, 'Ernesto12139', email]:
            errors.append('author/committer identity: '+sha)
        if re.search(r'(?im)^Co-Authored-By:|Generated with (?:Claude Code|Codex)|Claude-Session', fields[4]):
            errors.append('forbidden commit trailer/message: '+sha)
    if set(commits) & set(forbidden_ancestors):
        errors.append('inherited forbidden ancestor')
    refs = git(root, 'for-each-ref', '--format=%(refname)').splitlines()
    if any(ref not in {'refs/heads/main', 'refs/tags/v0.8.1', 'refs/remotes/origin/main',
                       'refs/remotes/origin/HEAD'} for ref in refs):
        errors.append('unexpected branch/tag/ref')
    if 'refs/tags/v0.8.1' in refs and git(root, 'rev-parse', 'v0.8.1^{commit}') != git(root, 'rev-parse', 'HEAD'):
        errors.append('release tag does not name the audited HEAD')
    if release is not None:
        if release.get('author', {}).get('login') != 'Ernesto12139':
            errors.append('release creator is not Owner')
        if release.get('tag_name') != 'v0.8.1' or release.get('draft') or release.get('prerelease'):
            errors.append('wrong or unpublished release/tag')
        if 'refs/tags/v0.8.1' not in refs:
            errors.append('release tag missing locally')
    return {'status': 'FAIL' if errors else 'PASS', 'errors': errors, 'commits': len(commits),
            'root': roots[0] if len(roots)==1 else None, 'head': git(root, 'rev-parse', 'HEAD'),
            'author': 'Ernesto12139', 'committer': 'Ernesto12139', 'email': email,
            'coauthor_audit': 'REJECT_ALL_COAUTHOR_TRAILERS', 'refs': refs,
            'release_creator_verified': release is not None and not errors}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--repo', type=Path, default=Path('.'))
    p.add_argument('--email', required=True)
    p.add_argument('--expected-root')
    p.add_argument('--forbidden-ancestor', action='append', default=[])
    p.add_argument('--release-json', type=Path)
    args = p.parse_args()
    result = audit(args.repo, args.email, expected_root=args.expected_root,
        forbidden_ancestors=args.forbidden_ancestor,
        release=json.loads(args.release_json.read_text()) if args.release_json else None)
    print(json.dumps(result, indent=2))
    raise SystemExit(result['status'] != 'PASS')
