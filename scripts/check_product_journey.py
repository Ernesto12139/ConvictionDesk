"""Check recorded product-journey/release evidence, independently of pytest success.

Evidence files are local, hash-bound artifacts. This checker never runs a scan,
changes a process, publishes, or substitutes for an independent/Owner verdict.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

JOURNEYS = {'historical_three_records', 'complete_decision_reason', 'honest_today',
            'evidence_followup', 'paid_action_confirmation', 'quiet_desktop_mobile'}
CANDIDATE = JOURNEYS | {'focused_tests', 'canonical', 'offline_harness', 'runtime',
                       'evidence_pit', 'database_copy'}
FINAL = {'public_export', 'privacy_license_scan', 'clean_install', 'public_git_identity',
         'readme_assets'}
CUTOVER = {'owner_backup_integrity', 'owner_process_identity', 'owner_history_accounts',
           'owner_no_unconfirmed_writes', 'rollback_available'}
PUBLIC = {'anonymous_clone_install', 'public_exact_head_ci', 'public_refs_authors',
          'release_creator_tag'}
STAGES = {'CANDIDATE_READY': CANDIDATE,
          'INDEPENDENT_QA_ACCEPTED': CANDIDATE,
          'FINAL_RELEASE_READY': CANDIDATE | FINAL,
          'OWNER_CUTOVER_VERIFIED': CANDIDATE | CUTOVER,
          'PUBLIC_RELEASE_VERIFIED': CANDIDATE | FINAL | CUTOVER | PUBLIC}


def evidence_errors(item, root):
    if not isinstance(item, dict) or item.get('status') != 'PASS':
        return ['requires recorded PASS, not a test-count or planned assertion']
    path = item.get('path')
    if not path or not isinstance(path, str):
        return ['missing evidence artifact path']
    artifact = root / path
    if not artifact.is_file():
        return ['evidence artifact does not exist']
    if hashlib.sha256(artifact.read_bytes()).hexdigest() != item.get('sha256'):
        return ['evidence artifact hash mismatch']
    return []


def check(report, stage, root=Path('.')):
    errors = []
    if stage not in STAGES:
        return ['unknown release stage']
    if not report.get('run_id'):
        errors.append('missing run_id')
    if not re.fullmatch(r'[0-9a-f]{40}', report.get('private_source_sha', '')):
        errors.append('full private_source_sha required')
    if not re.fullmatch(r'[0-9a-f]{64}', report.get('source_lf_sha256', '')):
        errors.append('source_lf_sha256 required')
    for key in sorted(STAGES[stage]):
        errors += [f'{key}: {error}' for error in
                   evidence_errors(report.get('checks', {}).get(key), root)]
    if stage != 'CANDIDATE_READY':
        independent = report.get('independent_qa', {})
        failures = evidence_errors(independent, root)
        errors += ['independent_qa: ' + e for e in failures]
        if not failures:
            try:
                verdict = json.loads((root / independent['path']).read_text(encoding='utf-8-sig'))
                if verdict.get('status') != 'ACCEPTED':
                    errors.append('independent_qa: verdict is not ACCEPTED')
                for key in ('run_id', 'private_source_sha'):
                    if verdict.get(key) != report.get(key):
                        errors.append(f'independent_qa: {key} mismatch')
                if not verdict.get('reviewer') or verdict.get('reviewer') == report.get('writer'):
                    errors.append('independent_qa: independent reviewer required')
            except (ValueError, OSError):
                errors.append('independent_qa: invalid verdict artifact')
    return errors


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('report', type=Path)
    p.add_argument('--stage', choices=STAGES, required=True)
    args = p.parse_args()
    problems = check(json.loads(args.report.read_text(encoding='utf-8-sig')),
                     args.stage, args.report.resolve().parent)
    print(json.dumps({'layer': 'PRODUCT_JOURNEY_ACCEPTANCE', 'stage': args.stage,
                      'status': 'BLOCKED' if problems else 'PASS', 'errors': problems}, indent=2))
    raise SystemExit(bool(problems))
