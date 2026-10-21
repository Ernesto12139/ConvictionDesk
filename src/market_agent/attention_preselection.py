"""Conservative candidate repost units, never proof of event identity/materiality.

Only unscreened packets are grouped. Original persisted receipts stay untouched;
all members travel to the existing semantic screen and research context.
"""
import json
import re
import unicodedata
from datetime import timedelta

from .attention_groups import publication_precision

VERSION='CANDIDATE_REPOST_V1'
MAX_CLUSTER_RECEIPTS=8
MAX_UNIT_BYTES=8000
MAX_CANDIDATE_BYTES=12000  # Leave room within the existing18k compact context.
MAX_COMPARISONS=20000
STOP=set('a an the of on in at by to for from and with as after before its this that '
         'share shares stock stocks today news update latest why is are was were says said '
         'inc corp corporation plc company group power'.split())


def signature(packet):
    receipt=packet.evidence[0]
    if packet.revision_of or len(packet.evidence)!=1 or publication_precision(receipt)!='INSTANT':
        return None
    title=unicodedata.normalize('NFKC',receipt.title or '').strip()
    title=re.sub(r'^(?:UPDATE\s*\d*|BREAKING|EXCLUSIVE)\s*[-:—]\s*','',title,flags=re.I)
    text=title+' '+(receipt.excerpt or '')
    data=receipt.structured_data or {}
    identity=tuple(data.get(k) if isinstance(data.get(k),str) else None for k in
        ('event_type','event_id','event_date','disclosure_stage'))
    if data.get('new_disclosure') is True or data.get('revision_state') in {'CORRECTION','CONFIRMATION','DENIAL'}:
        return None
    tokens=set(re.findall(r'[a-z][a-z0-9]+',title.casefold()))-STOP
    # Inflection normalization only, not issuer/event-specific semantic keywords.
    tokens={t[:-1] if len(t)>5 and t.endswith('s') and not t.endswith('ss') else t for t in tokens}
    if len(tokens)<4:
        return None
    # Spelled-out changes are numerical too; do not merge three vs ten thousand.
    numeric_words=re.findall(r'\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|'
        r'thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|'
        r'sixty|seventy|eighty|ninety|hundred|thousand|million|billion|first|second|third|fourth)\b',text,re.I)
    numbers=tuple(sorted(set(re.findall(r'\d+(?:[,.]\d+)*%?',text)+[w.casefold() for w in numeric_words])))
    # Without a source case identity, similar solicitation/legal stories are too
    # ambiguous to collapse into one lawsuit. Preserve them individually.
    if re.search(r'\blawsuit|litigation|complaint|\bsue[sd]?\b|class action|诉讼',text,re.I) and not data.get('event_id'):
        return None
    names=set(re.findall(r'\b(?:[A-Z][a-z]+(?:[A-Z][a-z]+)*|[A-Z]+)\b',text))
    names={n.casefold() for n in names if n.casefold() not in STOP and n.casefold() not in {
        'breaking','exclusive','report','reports','analysis','source','sources','safety'}}
    entities=data.get('named_entities',[])
    if isinstance(entities,list) and all(isinstance(n,str) for n in entities):
        names.update(n.casefold() for n in entities)
    stages=[]
    for key,pattern in [('DENIAL',r'\bden(?:y|ies|ied|ial)\b|not in talks|no plans? to|refut'),
        ('RUMOR',r'rumou?r|in talks|considering|people familiar|reportedly|sources? say'),
        ('CONFIRMATION',r'confirm|definitive|official|announc|signs? agreement|agreed'),
        ('REGULATORY_DECISION',r'complete response letter|approval granted|final ruling'),
        ('PANEL',r'advisory panel|panel vote|panel votes'),
        ('REVISION',r'correct(?:ed|ion)|revis(?:ed|ion)')]:
        if re.search(pattern,text,re.I):stages.append(key)
    # Operational/decision reversals must not be swallowed by a high word score.
    for key,pattern in [('NEGATED',r'\b(?:not|no|never|false|untrue)\b|否认|辟谣'),
        ('STOP',r'\b(?:halt\w*|suspend\w*|paus\w*|stopp?\w*|withdraw\w*)\b|on hold'),
        ('RESUME',r'\b(?:resum\w*|restart\w*|reopen\w*|reinstat\w*|lifts?)\b'),
        ('RAISE',r'\b(?:rais\w*|increas\w*|expand\w*)\b'),
        ('LOWER',r'\b(?:cuts?|lower\w*|decreas\w*)\b'),
        ('APPOINT',r'\b(?:appoint\w*|joins?|joining)\b'),
        ('DEPART',r'\b(?:resign\w*|depart\w*|leaves?)\b'),
        ('APPROVE',r'\b(?:approv\w*|grants?|clear\w*)\b'),
        ('REJECT',r'\b(?:reject\w*|refus\w*)\b')]:
        if re.search(pattern,text,re.I):stages.append(key)
    return {'tokens':tokens,'numbers':numbers,'names':names,'stages':tuple(stages),
        'published':receipt.published_at,'symbol':packet.universe.symbol,
        'thesis':(packet.universe.thesis_version_id,_thesis_hash(packet.confirmed_thesis)),
        'source_event_identity':identity}


def _thesis_hash(thesis):
    from .tdb import content_hash
    return content_hash(thesis) if thesis is not None else None


def compatible(a,b):
    if not a or not b or any(a[k]!=b[k] for k in ('symbol','thesis','numbers','names','stages','source_event_identity')):
        return False
    if abs(a['published']-b['published'])>timedelta(hours=36):
        return False
    common=len(a['tokens'] & b['tokens'])
    union=len(a['tokens'] | b['tokens'])
    return common>=4 and (common/union>=.72 or common/min(len(a['tokens']),len(b['tokens']))>=.85)


def public_size(packets):
    from .attention_selection import public_packet
    return len(json.dumps({'events':[public_packet(p) for p in packets]},ensure_ascii=False).encode())


def repost_units(packets):
    """Complete-link clusters avoid transitive topic-chain overmerges.

    Numbers/entities/stages differ -> separate; ambiguity -> separate. A bounded
    comparison budget leaves excess receipts individual rather than discarding.
    """
    signatures={p.event_key:signature(p) for p in packets}
    groups=[];buckets={};comparisons=0;exhausted=False
    for packet in sorted(packets,key=lambda p:(p.evidence[0].published_at,p.event_key)):
        sig=signatures[packet.event_key];found=None
        if sig:
            key=(sig['symbol'],sig['thesis'],sig['numbers'],frozenset(sig['names']),sig['stages'],sig['source_event_identity'])
            for index in buckets.get(key,[]):
                group=groups[index]
                if len(group)>=MAX_CLUSTER_RECEIPTS:continue
                if comparisons+len(group)>MAX_COMPARISONS:
                    exhausted=True;continue
                comparisons+=len(group)
                if not all(compatible(sig,signatures[p.event_key]) for p in group):continue
                combined=group[0].model_copy(update={'evidence':[p.evidence[0] for p in [*group,packet]]})
                if public_size([combined])<=MAX_UNIT_BYTES:
                    found=index;break
        if found is None:
            groups.append([packet])
            if sig:buckets.setdefault(key,[]).append(len(groups)-1)
        else:groups[found].append(packet)
    units=[];membership={}
    for group in groups:
        primary=group[0]
        unit=primary.model_copy(update={'evidence':[p.evidence[0] for p in group]}) if len(group)>1 else primary
        units.append(unit);membership[unit.event_key]=group
    return units,membership,{'version':VERSION,'qualification':'UNVERIFIED_CANDIDATE_REPOSTS',
        'raw_articles':len(packets),'candidate_event_units':len(units),
        'same_round_redundant_slots_avoided':len(packets)-len(units),
        'original_receipts_deleted':0,'comparisons':comparisons,'comparison_budget':MAX_COMPARISONS,
        'comparison_budget_reached':exhausted or comparisons>=MAX_COMPARISONS,
        'max_receipts_per_unit':MAX_CLUSTER_RECEIPTS,
        'meaning':'Conservative same-event hypothesis only; every original goes to semantic screening. New receipts are not auto-accepted from earlier screens.'}


def fit_context(batch):
    result=[];deferred=[]
    for packet in batch:
        if public_size([*result,packet])<=MAX_CANDIDATE_BYTES:result.append(packet)
        else:deferred.append(packet.event_key)
    return result,deferred
