"""Small public-source projection and honest scan outcome; no semantic verdicts."""
import re
from datetime import timedelta

from .market_evidence import NEWS_MAX_AGE, market_identity


def current_candidate(packet, cutoff):
    """Candidate window, not a claim of verified event time/source importance."""
    receipt = packet.evidence[0]
    return bool(receipt.published_at and receipt.retrieved_at >= receipt.published_at
                and 0 <= (cutoff-receipt.published_at).total_seconds() <= NEWS_MAX_AGE.total_seconds())


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


def pending_lifecycle(packets, cutoff):
    """Observed admission eligibility distinguishes expiry from pre-existing history."""
    result={'current':0,'expired_unscreened':0,'historical_other':0,'by_symbol':{}}
    for packet in packets:
        if current_candidate(packet,cutoff):
            state='current'
        elif current_candidate(packet,packet.cutoff) and packet.evidence[0].published_at<cutoff:
            state='expired_unscreened'
        else:
            state='historical_other'
        result[state]+=1
        row=result['by_symbol'].setdefault(packet.universe.symbol,
            {'current':0,'expired_unscreened':0,'historical_other':0})
        row[state]+=1
    return result


def candidate_signal(packet):
    """Replayable triage clues only; never a materiality or truth verdict."""
    receipt=packet.evidence[0]
    text=' '.join((e.title or '')+' '+(e.excerpt or '') for e in packet.evidence).casefold()
    if re.search(r'\b(?:bankrupt\w*|chapter\s+(?:11|7)|restatement|delist\w*|trading halt|'
                 r'shares?\s+(?:halted|suspended)|subpoena|(?:regulator\w*|sec|doj).{0,40}probe|complete response letter|deficiency notice|'
                 r'short seller.{0,40}(?:alleg\w*|report)|accounting fraud|'
                 r'(?:plant|factory|facility).{0,30}(?:explosion|fire|halt\w* production)|'
                 r'(?:cuts?|lowers?|withdraws?|suspends?)\s+(?:[\w-]+\s+){0,2}(?:outlook|forecast|guidance)|'
                 r'(?:ceo|cfo|auditor|chief executive).{0,50}(?:resign\w*|depart\w*)|'
                 r'terminat\w*.{0,30}(?:contract|agreement)|(?:contract|agreement).{0,30}terminat\w*|'
                 r'guidance withdrawn|withdraw\w* guidance|regulator\w*.*(?:suspend\w*|halt\w*)|'
                 r'(?:fda|nrc|sec|doj).*?(?:reject\w*|suspend\w*|halt\w*|paus\w*|charg\w*))\b|'
                 r'破产|重述|退市|停牌|(?:撤回|下调).{0,4}(?:指引|预期|展望)|'
                 r'(?:首席执行官|审计师|财务总监).{0,8}(?:离职|辞职)|合同终止|'
                 r'监管.{0,8}(?:暂停|禁止|立案)',text):
        return 2
    routine_date=bool(re.search(r'\b(?:earnings (?:date|call date)|sets? earnings|announces? earnings date)\b',text))
    if ((receipt.provider=='sec_edgar' and receipt.evidence_type=='sec_filing')
            or re.search(r'\b(?:guidance|investigation|acquir\w*|merger|recall)\b|'
            r'盈利预警|业绩预告|正式调查|收购|合并|召回',text)):
        return 1
    if not routine_date and re.search(r'\b(?:earnings|misses\s+(?:q[1-4]\s+)?(?:estimates|expectations))\b',text):
        return 1
    return 0


def select_candidates(packets, cutoff, limit, *, holding_symbols=None):
    """Four bounded lanes prevent one priority rule from consuming all capacity.

    Holding -> fresh signal/new arrival -> deadline -> nonholding, then fill.
    A clue promotes investigation, never declares materiality. Every unselected
    receipt remains pending; finite capacity cannot promise complete coverage.
    """
    if limit<=0:
        return []
    current=[p for p in packets if current_candidate(p,cutoff)]
    holding=lambda p:(p.universe.symbol in holding_symbols if holding_symbols is not None
                      else p.universe.state=='HOLDING')
    deadline=lambda p:(p.evidence[0].published_at,p.event_key)
    signal=lambda p:(-candidate_signal(p),*deadline(p))
    fresh=lambda p:(-candidate_signal(p),candidate_key(p,cutoff))
    selected=[]
    keys=set();symbols=set()
    def take(pool,key):
        if len(selected)>=limit:
            return
        remaining=[p for p in pool if p.event_key not in keys]
        distinct=[p for p in remaining if p.universe.symbol not in symbols]
        # One issuer cannot take a second slot while an unoffered issuer waits.
        if distinct:
            remaining=distinct
        elif any(p.event_key not in keys and p.universe.symbol not in symbols for p in current):
            return
        if remaining:
            packet=min(remaining,key=key)
            selected.append(packet);keys.add(packet.event_key);symbols.add(packet.universe.symbol)
    take([p for p in current if holding(p)],signal)
    take([p for p in current if cutoff-p.evidence[0].published_at<=timedelta(hours=24)],fresh)
    take(current,deadline)
    take([p for p in current if not holding(p)],signal)
    while len(selected)<min(limit,len(current)):
        previous=len(selected)
        take(current,signal)
        if len(selected)==previous:
            break
    return selected


def selection_projection(packets, batch, cutoff, limit):
    """Counts describe candidate triage, never model success or actual importance."""
    current=[p for p in packets if current_candidate(p,cutoff)]
    return {'policy':'HOLDING_FRESH_DEADLINE_NONHOLDING_V1',
        'materiality_status':'UNVERIFIED_UNTIL_SEMANTIC_SCREEN',
        'current_candidates':len(current),'offered_candidates':len(batch),
        'deferred_current':len(current)-len(batch),'request_limit':1,'event_limit':limit,
        'candidate_reduction_ratio':str(1-len(batch)/len(current)) if current else '0',
        'reduction_meaning':'Deferred this round, not proven noise or safely discarded',
        'offered':[{'event_key':p.event_key,'evidence_id':p.evidence[0].evidence_id,
            'signal_hint':candidate_signal(p),'publication_to_offer_seconds':
                int((cutoff-p.evidence[0].published_at).total_seconds())} for p in batch]}


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
