"""Optional, same-source continuation window for an already approved SEC packet.

No new refs, sources, numerical permissions, transport, consent or budget. Apply
before native record_send so its final facts hash covers both exact source slices.
The original memo packet/compiler remain unchanged and retain numeric veto power.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import re
from urllib.parse import urlsplit

from .domain import utc
from .evidence_action import MAX_EXCERPT, contract_request
from .llm import LLMError
from .tdb import content_hash


@dataclass(frozen=True)
class SourceWindow:
    parent_ref: str
    parent_evidence_id: str
    source_hash: str
    source_uri: str
    accession_number: str
    source_group_id: str
    start: int
    end: int
    text: str
    slice_sha256: str

    def metadata(self):
        # The model cites the existing S ref, never a new child/E/window ref.
        return {key:getattr(self,key) for key in ('parent_ref','source_hash','source_uri',
            'accession_number','source_group_id','start','end','slice_sha256')}


@dataclass(frozen=True)
class WindowedContract:
    request: dict
    windows: tuple[SourceWindow, ...]
    base_packet_hash: str
    final_facts_hash: str
    source_group_count: int
    added_characters: int


def _reject(code):
    raise LLMError('retrieval_window_'+code, 'Bounded source window binding failed')


def extend_source_windows(packet, request, approved_source, source_manifest) -> WindowedContract:
    """Add at most one <=600-char continuation to one existing <=600-char excerpt.

    `approved_source` and its matching manifest entry must come from the current
    native egress.project. This local binding is not execution Admission; normal
    record_send/validate_send/terminal payload-hash gates must still run afterward.
    Only public SEC filings qualify. Apply immediately after contract_request,
    before unrelated optional facts augmentation, to avoid ambiguous base binding.
    """
    if MAX_EXCERPT != 600 or request != contract_request(packet):
        _reject('base_contract_mismatch')
    evidence = approved_source.evidence
    row = next((row for row in packet.refs if row.evidence_id==evidence.evidence_id),None)
    if row is None or (evidence.provider!='sec_edgar' or evidence.evidence_type!='sec_filing'
            or approved_source.identity!='VERIFIED_FACT'
            or row.source_hash!=evidence.content_hash or row.excerpt is None
            or row.identity!=approved_source.identity or row.freshness!=approved_source.freshness
            or row.published_at!=evidence.published_at or row.source_uri!=evidence.source_uri
            or row.as_of!=evidence.as_of or row.retrieved_at!=evidence.retrieved_at
            or row.symbol!=evidence.symbol or evidence.symbol!=packet.symbol
            or row.evidence_type!=evidence.evidence_type or row.structured_data!=evidence.structured_data):
        _reject('source_mismatch')
    if (evidence.published_at is None or utc(evidence.published_at)>utc(packet.cutoff)
            or evidence.as_of is not None and utc(evidence.as_of)>utc(packet.cutoff)):
        _reject('pit')
    uri = urlsplit(evidence.source_uri or '')
    metadata = evidence.structured_data or {}
    accession, cik = metadata.get('accession_number'),metadata.get('cik')
    if (uri.scheme!='https' or uri.hostname not in {'sec.gov','www.sec.gov'}
            or not uri.path.startswith('/Archives/edgar/data/')
            or not isinstance(accession,str) or not re.fullmatch(r'\d{10}-\d{2}-\d{6}',accession)
            or not isinstance(cik,str) or not re.fullmatch(r'\d{10}',cik)):
        _reject('public_sec_identity')
    if not isinstance(source_manifest,dict) or not isinstance(source_manifest.get('allowed_fields'),list):
        _reject('rights_projection')
    if (source_manifest.get('evidence_id')!=evidence.evidence_id
            or source_manifest.get('source')!='sec_edgar'
            or source_manifest.get('source_hash')!=evidence.content_hash
            or source_manifest.get('category')!='PUBLIC_FILINGS'
            or 'excerpt' not in source_manifest.get('allowed_fields',[])
            or source_manifest.get('license_reference')!='PUBLIC_SEC_FILINGS'
            or source_manifest.get('citation_scope')!='PRIVATE_RESEARCH_ONLY'
            or source_manifest.get('projection_hash')!=content_hash(evidence.model_dump(mode='json'))):
        _reject('rights_projection')
    text = evidence.excerpt or ''
    if not text or not 0<len(row.excerpt)<=600:
        _reject('empty_or_oversize_excerpt')
    audit = [item for item in packet.omitted if item.get('evidence_id')==evidence.evidence_id
        and item.get('reason')=='excerpt-window']
    if len(audit)>1:
        _reject('ambiguous_offset')
    start = audit[0]['excerpt_start'] if audit else 0
    if type(start) is not int or start<0 or text[start:start+len(row.excerpt)]!=row.excerpt:
        _reject('slice_mismatch')
    end = start+len(row.excerpt)
    group = content_hash({'provider':'sec_edgar','cik':cik,'accession_number':accession})
    def window(left,right):
        selected = text[left:right]
        return SourceWindow(row.ref,evidence.evidence_id,evidence.content_hash,
            evidence.source_uri,accession,group,left,right,selected,
            hashlib.sha256(selected.encode('utf-8')).hexdigest())
    windows = [window(start,end)]
    # Adjacent non-overlapping slices only. Preserve the existing first excerpt
    # exactly; choose the side exposing more unseen text, with stable earlier tie.
    options = [(max(0,start-600),start),(end,min(len(text),end+600))]
    options = [(left,right) for left,right in options if right>left
        and text[left:right]!=row.excerpt]
    if options:
        left,right = max(options,key=lambda pair:(pair[1]-pair[0],-pair[0]))
        windows.append(window(left,right))
    facts_request = deepcopy(request)
    target = next(item for item in facts_request['facts']['evidence'] if item['ref']==row.ref)
    target['excerpt_window'] = windows[0].metadata()
    if len(windows)>1:
        # The first window's text is already in `excerpt`: never duplicate its
        # bytes and quietly turn a 1200-char allowance into an 1800-char payload.
        target['continuation_window'] = {**windows[1].metadata(),'text':windows[1].text}
    facts_request['facts']['source_window_extension'] = {
        'version':'bounded-same-source-windows-v1','base_packet_hash':packet.packet_hash,
        'parent_ref':row.ref,'source_group_id':group,'source_group_count':1,
        'source_text_characters':sum(len(item.text) for item in windows),
        'window_count':len(windows),'citation_policy':'SAME_PARENT_REF_NOT_INDEPENDENT_CORROBORATION'}
    facts_request['system_instruction'] += (
        '\n可选 continuation_window 是同一 parent_ref 原文的另一窗口，与 excerpt '
        '共享同一来源、申报 accession 和权利，不是独立交叉佐证。仅引用原 S 编号，'
        '不得创造子引用。numeric_receipts 与原确定性数值权限不变；新增窗口出现的数字'
        '若未获原 excerpt/receipt 支持，不得输出。窗口 metadata 是追踪信息，不是财务事实。')
    return WindowedContract(facts_request,tuple(windows),packet.packet_hash,
        content_hash(facts_request['facts']),1,len(windows[1].text) if len(windows)>1 else 0)
