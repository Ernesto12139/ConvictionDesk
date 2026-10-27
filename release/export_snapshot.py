"""Export exact allowlisted blobs from one commit into a NEW empty release tree.

No history, Git configuration, refs, user data or working-tree files are copied.
This tool never deletes files, changes remotes or publishes anything.
"""
import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET
import zlib

TEXT_TYPES = {'.py', '.ps1', '.js', '.cjs', '.css', '.html', '.svg', '.json', '.toml', '.md', '.txt',
              '.yml', '.yaml', '.example', ''}
PNG_SIGNATURE = bytes.fromhex('89504e470d0a1a0a')
PNG_CHUNKS = {b'IHDR', b'PLTE', b'IDAT', b'IEND', b'sRGB', b'gAMA', b'pHYs'}
PNG_MAX_BYTES = 1_000_000
# Compose the forbidden identifier so the scanner can itself be exported.
PRIVATE_IDENTIFIERS = ('market-' + 'intelligence-' + 'agent',)
RULES = {
    'private_key': r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
    'api_key': r'\bsk-[A-Za-z0-9_-]{20,}',
    'github_token': r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})',
    'aws_key': r'\bAKIA[0-9A-Z]{16}\b',
    'assigned_secret': r'''(?i)\b(?:[A-Z_]*API_KEY|ACCESS_TOKEN|SECRET|PASSWORD)\b\s*[:=]\s*["'][A-Za-z0-9_-]{24,}["']''',
    'private_windows_path': r'(?i)\b[A-Z]:[\\/]+(?:Users|Codex|Documents and Settings)[\\/]',
    'private_home': r'/(?:home|Users)/[A-Za-z0-9_-]+/',
    'nonexample_email': r'\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net)\b|users\.noreply\.github\.com\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
}


def git(source, *args):
    return subprocess.check_output(['git', '-c', 'core.fsmonitor=false', '-C', str(source), *args])


def safe_path(name):
    path = PurePosixPath(name)
    return (isinstance(name, str) and name == path.as_posix() and not path.is_absolute()
            and '..' not in path.parts and ':' not in name and '\\' not in name
            and not any(p.lower() == '.git' for p in path.parts))


def scan_png(name, data):
    """Accept only metadata-free PNG screenshots under docs/assets/."""
    if not name.startswith('docs/assets/'):
        return ['PNG outside docs/assets']
    if len(data) > PNG_MAX_BYTES or not data.startswith(PNG_SIGNATURE):
        return ['oversized or invalid PNG']
    at, kinds = len(PNG_SIGNATURE), []
    while at < len(data):
        if at + 12 > len(data):
            return ['truncated PNG chunk']
        size = int.from_bytes(data[at:at+4], 'big')
        kind, body = data[at+4:at+8], data[at+8:at+8+size]
        crc = data[at+8+size:at+12+size]
        if len(body) != size or len(crc) != 4 or zlib.crc32(kind+body).to_bytes(4, 'big') != crc:
            return ['corrupt PNG chunk']
        if kind not in PNG_CHUNKS:
            return ['PNG metadata or unapproved chunk']
        if kind == b'IHDR':
            if size != 13:
                return ['invalid PNG header']
            width,height=int.from_bytes(body[:4],'big'),int.from_bytes(body[4:8],'big')
            if not (0<width<=4096 and 0<height<=4096 and width*height<=8_000_000):
                return ['PNG dimensions exceed screenshot bounds']
            if body[8] != 8 or body[9] not in {2,6} or body[10:12] != b'\0\0' or body[12] not in {0,1}:
                return ['unsupported screenshot PNG encoding']
        if kind == b'IEND' and size != 0:
            return ['invalid PNG end']
        kinds.append(kind)
        at += 12 + size
    if not kinds or kinds[0] != b'IHDR' or kinds.count(b'IHDR') != 1 or b'IDAT' not in kinds or kinds[-1] != b'IEND' or kinds.count(b'IEND') != 1:
        return ['malformed PNG structure']
    return []


def scan(name, data, private_terms=()):
    errors = []
    if safe_path(name) and PurePosixPath(name).suffix.lower() == '.png':
        return scan_png(name, data)
    if not safe_path(name) or PurePosixPath(name).suffix.lower() not in TEXT_TYPES:
        return ['unapproved path or file type']
    if any(p.startswith('.env') and p != '.env.example' for p in PurePosixPath(name).parts):
        return ['private environment file']
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return ['not UTF-8 text']
    for label, pattern in RULES.items():
        if re.search(pattern, text):
            errors.append(label)
    if any(term.casefold() in text.casefold() for term in PRIVATE_IDENTIFIERS):
        errors.append('private project identifier')
    if any(term and term.casefold() in text.casefold() for term in private_terms):
        errors.append('owner-specified private term')
    if name.endswith('.svg'):
        try:
            root = ET.fromstring(text)
            for node in root.iter():
                if node.tag.rsplit('}', 1)[-1] in {'script', 'foreignObject', 'image'}:
                    errors.append('active or embedded SVG content')
                if any(k.startswith('on') or k.endswith('href') and not v.startswith('#')
                       for k, v in node.attrib.items()):
                    errors.append('external or active SVG attribute')
        except ET.ParseError:
            errors.append('invalid SVG')
    return errors


def export(source, sha, target, private_terms=()):
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('Full explicit commit SHA required')
    if git(source, 'rev-parse', sha+'^{commit}').decode().strip() != sha:
        raise ValueError('Commit identity mismatch')
    if target.exists() and any(target.iterdir()):
        raise ValueError('Release directory must be new or empty; preserve existing files')
    entries = json.loads(git(source, 'show', sha+':release/public-files.json'))
    if len({e['path'].casefold() for e in entries}) != len(entries):
        raise ValueError('Duplicate release path')
    files, findings = [], {}
    for entry in entries:
        origin, name = entry['source'], entry['path']
        if not safe_path(origin) or not safe_path(name):
            raise ValueError('Unsafe allowlist path')
        tree = git(source, 'ls-tree', sha, '--', origin).decode().strip()
        meta, actual = tree.split('\t', 1)
        mode, kind, oid = meta.split()
        if actual != origin or kind != 'blob' or mode not in {'100644', '100755'}:
            raise ValueError('Only explicitly named regular blobs may be exported')
        data = git(source, 'cat-file', 'blob', oid)
        errors = scan(name, data, private_terms)
        if errors:
            findings[name] = errors
        files.append((name, data, {'path': name, 'source': origin, 'git_blob': oid,
                                  'sha256': hashlib.sha256(data).hexdigest()}))
    if findings:
        raise ValueError('SECURITY_SCAN_FAILED: '+json.dumps(findings))
    target.mkdir(parents=True, exist_ok=True)
    for name, data, _ in files:
        path = target/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    version_entry=next((entry for entry in files if entry[0]=='pyproject.toml'),None)
    version=tomllib.loads(version_entry[1].decode()).get('project',{}).get('version') if version_entry else None
    manifest = {'upstream_source_sha': sha,'source_version':version,'allowlist': 'explicit paths and mappings',
                'files': [row for _, _, row in files], 'privacy_scan': 'PASS',
                'history': 'No Git history/configuration/refs exported',
                'verification': 'Local source candidate; publication and remote CI are separate gates'}
    (target/'release').mkdir(exist_ok=True)
    (target/'release/RELEASE_MANIFEST.json').write_text(json.dumps(manifest,indent=2)+'\n', encoding='utf-8')
    return manifest


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--sha', required=True)
    p.add_argument('--target', type=Path, required=True)
    p.add_argument('--private-term', action='append', default=[])
    args = p.parse_args()
    result = export(args.source, args.sha, args.target, args.private_term)
    print(json.dumps({'status': 'PASS', 'source_sha': args.sha, 'files': len(result['files'])}))
