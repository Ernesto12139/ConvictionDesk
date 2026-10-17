"""Small public-source projection and honest scan outcome; no semantic verdicts."""
import re

from .market_evidence import market_identity


def public_packet(packet):
    return {'event_key': packet.event_key, 'focus_symbol': packet.universe.symbol,
        'confirmed_thesis': None, 'revision_of': packet.revision_of,
        'cutoff': packet.cutoff.isoformat(), 'evidence': [
            {key: value for key, value in receipt.model_dump(mode='json').items()
             if key in {'evidence_id', 'evidence_type', 'provider', 'symbol', 'title',
                        'excerpt', 'published_at', 'source_uri'}} for receipt in packet.evidence]}


def candidate_key(packet, cutoff):
    receipt = packet.evidence[0]
    age = {'TODAY': 0, 'RECENT': 1, 'HISTORICAL': 2, 'UNKNOWN': 2, 'INELIGIBLE': 3}
    text = ((receipt.title or '')+' '+(receipt.excerpt or '')).lower()
    solicitation = bool(re.search(r'lead plaintiff|contact.*law firm|investor.*deadline|'
                                  r'shareholder alert|class action.*reminder', text))
    # This only orders candidates. No legal receipt is dismissed or labelled
    # immaterial without screening; new court/risk evidence remains available.
    return (age[market_identity(receipt, cutoff).freshness_status], int(solicitation),
            -(receipt.published_at or receipt.retrieved_at).timestamp(), packet.event_key)


def select_candidates(packets, cutoff, limit):
    ordered = sorted(packets, key=lambda packet:candidate_key(packet, cutoff))
    selected = []
    # First give each symbol with eligible recent evidence one candidate. A
    # high-volume issuer must not consume another issuer's entire screen budget.
    for recent in (True, False):
        groups = {}
        for packet in ordered:
            if (candidate_key(packet, cutoff)[0] <= 1) == recent:
                groups.setdefault(packet.universe.symbol, []).append(packet)
        while groups and len(selected) < limit:
            for symbol in sorted(groups, key=lambda key:candidate_key(groups[key][0], cutoff)):
                selected.append(groups[symbol].pop(0))
                if not groups[symbol]:
                    del groups[symbol]
                if len(selected) == limit:
                    break
    return selected


def scan_outcome(report):
    if not report:
        return 'NOT_RUN'
    if report.get('status') in {'FAILED', 'INTERRUPTED'}:
        return 'FAILED'
    errors = report.get('errors', [])
    source_errors = {row['symbol'] for row in errors if 'symbol' in row
                     and row.get('code') != 'SOURCE_COVERAGE_LIMIT'}
    if report.get('symbols', 0) and len(source_errors) >= report['symbols'] and not report.get('screened'):
        return 'PROVIDER_UNAVAILABLE'
    if report.get('coverage_incomplete') or report.get('pending_events'):
        return 'PARTIAL'
    return 'COMPLETED_WITH_EVENTS' if report.get('queued') else 'COMPLETED_NO_MATERIAL_EVENTS'
