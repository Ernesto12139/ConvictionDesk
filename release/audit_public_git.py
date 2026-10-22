"""Fail closed on non-Owner public authors, trailers, ancestry, refs or release creator."""
import argparse
import json
import re
import subprocess
from pathlib import Path

PUBLIC_ROOT = '555ca4fbc531e61bf0fdc33d5022fec38dece11b'
V081_TAG_OBJECT = '4f5a3f17d2d83536040a2e9737bd15385980141f'
RELEASE_TAG = re.compile(r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)')
# Open development/PR branches as fetched by CI checkout. Only these prefixes, one path
# segment, never tags or other namespaces; every commit they reach is still audited below.
DEV_BRANCH = re.compile(r'refs/remotes/origin/(?:docs|fix|feat|chore|test|codex)/[A-Za-z0-9][A-Za-z0-9._-]{0,99}')
FORBIDDEN_MESSAGE = re.compile(r'^Co-Authored-By:|Generated with (?:Claude Code|Codex)|Claude-Session',re.I|re.M)


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'core.fsmonitor=false', '-C', str(root), *args]).decode('utf-8').strip()


def audit(root, email, *, expected_root=None, release=None, forbidden_ancestors=(),
          release_tag=None, protected_tags=None):
    errors = []
    commits = git(root, 'rev-list', '--all').splitlines()
    roots = git(root, 'rev-list', '--max-parents=0', '--all').splitlines()
    if len(roots) != 1 or expected_root and roots != [expected_root]:
        errors.append('expected exactly one new root commit')
    for sha in commits:
        fields = git(root, 'show', '-s', '--format=%an%x00%ae%x00%cn%x00%ce%x00%B', sha).split('\x00')
        if fields[:4] != ['Ernesto12139', email, 'Ernesto12139', email]:
            errors.append('author/committer identity: '+sha)
        if FORBIDDEN_MESSAGE.search(fields[4]):
            errors.append('forbidden commit trailer/message: '+sha)
    if set(commits) & set(forbidden_ancestors):
        errors.append('inherited forbidden ancestor')
    refs = git(root, 'for-each-ref', '--format=%(refname)').splitlines()
    branch_refs={'refs/heads/main','refs/remotes/origin/main','refs/remotes/origin/HEAD'}
    tags=[ref.removeprefix('refs/tags/') for ref in refs if ref.startswith('refs/tags/')]
    if any(ref not in branch_refs and not DEV_BRANCH.fullmatch(ref) and not (ref.startswith('refs/tags/') and
            RELEASE_TAG.fullmatch(ref.removeprefix('refs/tags/'))) for ref in refs):
        errors.append('unexpected branch/tag/ref')
    ancestry=set(git(root,'rev-list','HEAD').splitlines())
    for tag in tags:
        ref='refs/tags/'+tag
        if git(root,'cat-file','-t',ref)!='tag':
            errors.append('release tag must be annotated: '+tag)
            continue
        raw=git(root,'cat-file','-p',ref)
        tagger=next((line for line in raw.splitlines() if line.startswith('tagger ')), '')
        if not tagger.startswith('tagger Ernesto12139 <'+email+'> '):
            errors.append('tagger identity: '+tag)
        if FORBIDDEN_MESSAGE.search(raw):
            errors.append('forbidden tag trailer/message: '+tag)
        if git(root,'rev-parse',ref+'^{commit}') not in ancestry:
            errors.append('release tag outside main ancestry: '+tag)
    for tag,oid in (protected_tags or {}).items():
        if tag not in tags or git(root,'rev-parse','refs/tags/'+tag)!=oid:
            errors.append('protected release tag changed or missing: '+tag)
    expected_tag=release_tag or (release.get('tag_name') if release else None)
    if expected_tag:
        if not RELEASE_TAG.fullmatch(expected_tag) or expected_tag not in tags:
            errors.append('release tag missing or invalid')
        elif git(root,'rev-parse','refs/tags/'+expected_tag+'^{commit}')!=git(root,'rev-parse','HEAD'):
            errors.append('release tag does not name the audited HEAD')
    if release is not None:
        if release.get('author', {}).get('login') != 'Ernesto12139':
            errors.append('release creator is not Owner')
        if release.get('tag_name') != expected_tag or release.get('draft') or release.get('prerelease'):
            errors.append('wrong or unpublished release/tag')
    return {'status': 'FAIL' if errors else 'PASS', 'errors': errors, 'commits': len(commits),
            'root': roots[0] if len(roots)==1 else None, 'head': git(root, 'rev-parse', 'HEAD'),
            'author': 'Ernesto12139', 'committer': 'Ernesto12139', 'email': email,
            'coauthor_audit': 'REJECT_ALL_COAUTHOR_TRAILERS', 'refs': refs,
            'audited_release_tag':expected_tag,'protected_tags':protected_tags or {},
            'release_creator_verified': release is not None and not errors}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--repo', type=Path, default=Path('.'))
    p.add_argument('--email', required=True)
    p.add_argument('--expected-root',default=PUBLIC_ROOT)
    p.add_argument('--release-tag',help='Current stable version tag; earlier tags may name earlier main commits')
    p.add_argument('--protected-tag',action='append',default=[],metavar='TAG=OBJECT_SHA')
    p.add_argument('--forbidden-ancestor', action='append', default=[])
    p.add_argument('--release-json', type=Path)
    args = p.parse_args()
    pins={}
    if args.expected_root==PUBLIC_ROOT:
        pins['v0.8.1']=V081_TAG_OBJECT
    for item in args.protected_tag:
        tag,separator,oid=item.partition('=')
        if not separator or not RELEASE_TAG.fullmatch(tag) or not re.fullmatch('[0-9a-f]{40}',oid):
            p.error('--protected-tag must be a stable version and full Git object SHA')
        if tag in pins and pins[tag]!=oid:
            p.error('cannot override the protected v0.8.1 tag object')
        pins[tag]=oid
    result = audit(args.repo, args.email, expected_root=args.expected_root,
        forbidden_ancestors=args.forbidden_ancestor,
        release=json.loads(args.release_json.read_text()) if args.release_json else None,
        release_tag=args.release_tag,protected_tags=pins)
    print(json.dumps(result, indent=2))
    raise SystemExit(result['status'] != 'PASS')
