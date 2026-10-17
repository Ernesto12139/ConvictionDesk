"""A green suite cannot substitute for journeys, independent QA or cutover proof."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('journey_gate',
    Path(__file__).resolve().parents[1] / 'scripts/check_product_journey.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def packet(tmp_path):
    artifact = tmp_path / 'journeys.json'
    artifact.write_text('{"saved_records_opened":3}', encoding='utf-8')
    proof = {'status': 'PASS', 'path': artifact.name,
             'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}
    return {'run_id': 'synthetic-qa', 'private_source_sha': 'a' * 40,
            'source_lf_sha256': 'b' * 64, 'writer': 'writer',
            'checks': {key: dict(proof) for key in gate.CANDIDATE}}


def independent(tmp_path, report, **changes):
    verdict = {'status': 'ACCEPTED', 'reviewer': 'independent-reviewer',
               'run_id': report['run_id'], 'private_source_sha': report['private_source_sha']}
    verdict.update(changes)
    path = tmp_path / 'independent.json'
    path.write_text(json.dumps(verdict), encoding='utf-8')
    report['independent_qa'] = {'status': 'PASS', 'path': path.name,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def test_pytest_only_never_implies_product_acceptance(tmp_path):
    report = packet(tmp_path)
    report['checks'] = {'canonical': report['checks']['canonical']}
    assert any('historical_three_records' in e for e in gate.check(report, 'CANDIDATE_READY', tmp_path))


def test_candidate_does_not_imply_independent_release_cutover_or_publication(tmp_path):
    report = packet(tmp_path)
    assert gate.check(report, 'CANDIDATE_READY', tmp_path) == []
    for stage in set(gate.STAGES) - {'CANDIDATE_READY'}:
        assert gate.check(report, stage, tmp_path)
    independent(tmp_path, report)
    assert gate.check(report, 'INDEPENDENT_QA_ACCEPTED', tmp_path) == []
    assert gate.check(report, 'OWNER_CUTOVER_VERIFIED', tmp_path)
    assert gate.check(report, 'FINAL_RELEASE_READY', tmp_path)


@pytest.mark.parametrize('change', [dict(status='BLOCKED'), dict(private_source_sha='c'*40),
                                  dict(run_id='old-run'), dict(reviewer='writer')])
def test_independent_block_stale_sha_run_or_self_review_rejects(tmp_path, change):
    report = packet(tmp_path)
    independent(tmp_path, report, **change)
    assert gate.check(report, 'INDEPENDENT_QA_ACCEPTED', tmp_path)


def test_changed_or_absent_artifact_rejects(tmp_path):
    report = packet(tmp_path)
    (tmp_path / 'journeys.json').write_text('different')
    assert any('hash mismatch' in e for e in gate.check(report, 'CANDIDATE_READY', tmp_path))
    (tmp_path / 'journeys.json').unlink()
    assert any('does not exist' in e for e in gate.check(report, 'CANDIDATE_READY', tmp_path))
