"""Private, revocable per-research consent and source-specific outbound projection.

Owner consent grants owned data only. Third-party source rights are an independent
server policy, never a flag in a browser request or a license asserted by a model.
Original evidence/history bytes are not modified by these ephemeral projections.
"""
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import json
import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .llm import LLMError
from .tdb import TDBError, content_hash


CATEGORIES = Literal['OWNED_PORTFOLIO','PUBLIC_FILINGS','THIRD_PARTY_NEWS','MARKET_QUOTES','SYNTHETIC']
active_egress = ContextVar('active_research_egress', default=None)
active_egress_guard = ContextVar('active_research_egress_guard', default=None)


def admit_model_send(provider, model, destination, facts, *, purpose=None,
                     client=None, offline_test=False, admission=None, guard=None):
    """Last pre-dispatch gate, independent of Hub configuration and budget.

    The explicit offline seam can only reach a non-network MockTransport. A
    reservation or mutable context dictionary alone never supplies consent.
    """
    if offline_test:
        import httpx
        transport = client._transport_for_url(httpx.URL(destination)) if client is not None else None
        # Existing offline budget harnesses wrap MockTransport in a transparent
        # reservation transport. Never accept a network-backed leaf.
        wrappers = {'market_agent.call_budget', 'scripts.research_live_budget',
                    'scripts.validate_research_c1_only'}
        for _ in range(4):
            if transport is None or type(transport).__module__ not in wrappers:
                break
            transport = getattr(transport, 'inner', None)
        if not isinstance(transport, httpx.MockTransport):
            raise LLMError('data_rights_test_transport', 'Offline permission requires an explicit MockTransport')
        return
    admission = admission if admission is not None else active_egress.get()
    guard = guard if guard is not None else active_egress_guard.get()
    if not admission or not callable(guard):
        raise LLMError('egress_confirmation_required', 'A live server-side admission is required before model dispatch')
    record = guard()  # Recheck expiry, revocation and source rights immediately before every attempt.
    if (admission.get('target') != provider or admission.get('model') != model
            or admission.get('destination') != destination
            or admission.get('payload_hash') != content_hash(facts)
            or purpose is not None and admission.get('purpose') != purpose):
        raise LLMError('data_rights_payload_mismatch', 'Outbound target or facts differ from the admitted projection')
    if admission.get('purpose') == 'provider.health':
        if (facts != {'synthetic': True, 'required_ok': True}
                or admission.get('owned_data') is not False or record != {'health_probe': True}):
            raise LLMError('data_rights_probe_scope', 'Health admission covers only the fixed synthetic probe')
    elif not isinstance(record, dict) or record.get('id') != admission.get('consent_id'):
        raise LLMError('egress_confirmation_required', 'Admission is not bound to the live consent receipt')
    else:
        binding = record['preview']['binding']
        expected_destination = ('https://api.typesafe.ai/v1/systemone' if provider == 'jev' else binding['destination'])
        if (destination != expected_destination
                or provider == 'jev' and model != record['preview'].get('auxiliary_models',{}).get('jev')
                or provider != 'jev' and (provider != binding['provider'] or model != binding['model'])):
            raise LLMError('data_rights_target_mismatch', 'Transport differs from the consented provider and model')


class PreviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    research: dict
    categories: list[CATEGORIES] = Field(min_length=1,max_length=5)
    auxiliary_targets: list[Literal['jev']] = Field(default_factory=list,max_length=1)


class ConfirmInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_id: str = Field(max_length=96)
    preview_hash: str = Field(max_length=80)
    confirmed: bool


def utc_now():
    return datetime.now(timezone.utc)


def owned_projection(snapshot, account_id):
    if snapshot is None:
        return None
    accounts = [a for a in snapshot.accounts if not account_id or a.account_id == account_id]
    positions = [p for p in snapshot.positions if not account_id or p.account_id == account_id]
    # Only owned cash, cost and quantity, not provider-derived marks/weights.
    return {'snapshot_id':snapshot.snapshot_id,'snapshot_hash':snapshot.content_hash,
        'as_of':snapshot.as_of.isoformat(),
        'accounts':[a.model_dump(mode='json') for a in accounts],
        'positions':[p.model_dump(mode='json') for p in positions]}


class ResearchEgress:
    def __init__(self, store, profiles, environment, *, clock=utc_now):
        self.store, self.profiles, self.clock = store, profiles, clock
        self.source_policies = json.loads(environment.get('MIA_EGRESS_SOURCE_POLICIES','{}'))
        self.auxiliary_models = {'jev':environment.get('JEV_MODEL','jev-latest')}
        # Non-secret policies are server-owned: {provider:{category,fields,
        # citation_scope, license_reference, expires_at, destinations:[...]}}.
        with store._connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS research_egress_consents '
                '(id TEXT PRIMARY KEY,payload TEXT NOT NULL);'
                'CREATE TABLE IF NOT EXISTS research_egress_attempts '
                '(id INTEGER PRIMARY KEY,task_id TEXT,payload TEXT NOT NULL);')

    def save(self, record):
        with self.store._connect() as db:
            db.execute('INSERT OR REPLACE INTO research_egress_consents VALUES (?,?)',
                (record['id'],json.dumps(record,ensure_ascii=False)))

    @staticmethod
    def decode(identifier, row):
        if not row:
            raise TDBError('egress_confirmation_required',409)
        try:
            record = json.loads(row[0])
            if record['id'] != identifier or content_hash(record['preview']) != record['preview_hash']:
                raise ValueError('integrity')
            return record
        except (ValueError,KeyError,TypeError):
            raise TDBError('egress_confirmation_integrity',409) from None

    def get(self, identifier):
        with self.store._connect() as db:
            row = db.execute('SELECT payload FROM research_egress_consents WHERE id=?',(identifier,)).fetchone()
        return self.decode(identifier,row)

    def mutate(self, identifier, change):
        # Confirm/revoke must serialize. A delayed confirmation cannot overwrite
        # a concurrent revocation with the stale pre-revocation record.
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM research_egress_consents WHERE id=?',(identifier,)).fetchone()
            record = self.decode(identifier,row)
            change(record)
            db.execute('UPDATE research_egress_consents SET payload=? WHERE id=?',
                (json.dumps(record,ensure_ascii=False),identifier))
        return record

    def binding(self, request, snapshot):
        from .research_task import ResearchTasks
        previous = ResearchTasks(self.store).get(request.task_id) if request.task_id else None
        mode = request.mode or (previous.mode if previous else None)
        profile_id = request.model_profile_id or (previous.model_profile_id if previous and (not request.mode or request.mode==previous.mode) else None)
        profile = self.profiles.resolve(profile_id,mode)
        account = previous.account_id if previous else request.account_id
        destination = {'openai':'https://api.openai.com/v1/responses',
            'deepseek':'https://api.deepseek.com/chat/completions',
            'compatible':self.profiles.environment.get('LLM_BASE_URL','')+'/chat/completions'}[profile.provider]
        return {'request':request.model_dump(mode='json',exclude={'egress_consent_id'}),
            'profile':profile.profile_id,'provider':profile.provider,'model':profile.model,'mode':profile.mode,
            'destination':destination,
            'snapshot_hash':snapshot.content_hash if snapshot else None,'account_id':account}

    def snapshot(self, request):
        from .domain import PortfolioSnapshot
        from .research_task import ResearchTasks
        previous = ResearchTasks(self.store).get(request.task_id) if request.task_id else None
        snapshot_id = previous.snapshot_id if previous else request.snapshot_id
        if not snapshot_id:
            return self.store.get_snapshot_known_at(self.clock())
        with self.store._connect() as db:
            row = db.execute('SELECT payload FROM portfolio_snapshots WHERE snapshot_id=?',(snapshot_id,)).fetchone()
        if not row:
            raise TDBError('RESEARCH_SNAPSHOT_UNAVAILABLE',409)
        snapshot = PortfolioSnapshot.model_validate_json(row[0])
        if snapshot.as_of > self.clock() or snapshot.source.imported_at > self.clock():
            raise TDBError('RESEARCH_SNAPSHOT_UNAVAILABLE',409)
        return snapshot

    def preview(self, body):
        from .research_task import ResearchRequest
        request = ResearchRequest.model_validate(body.research)
        snapshot = self.snapshot(request)
        binding = self.binding(request,snapshot)
        if snapshot and 'OWNED_PORTFOLIO' not in body.categories:
            raise TDBError('egress_owned_portfolio_confirmation_required',409)
        # Questions and hypotheses are owned even without an imported portfolio.
        if 'OWNED_PORTFOLIO' not in body.categories and 'SYNTHETIC' not in body.categories:
            raise TDBError('egress_owned_question_confirmation_required',409)
        now = self.clock()
        payload = {'binding':binding,'categories':sorted(set(body.categories)),
            'auxiliary_targets':body.auxiliary_targets,
            'auxiliary_models':{'jev':self.auxiliary_models['jev']} if body.auxiliary_targets else {},
            'auxiliary_destinations':{'jev':'https://api.typesafe.ai/v1/systemone'} if body.auxiliary_targets else {},
            'owned_payload':{'question':request.question,
                'portfolio':owned_projection(snapshot,binding['account_id'])},
            'source_policy':self.source_policies,
            'allowed_owned_fields':['question','investment_hypotheses','account_id','cash','symbol','quantity','cost'],
            'citation_scope':'PRIVATE_RESEARCH_ONLY',
            'notice':'Only server-licensed source projections may accompany owned data. No public logs/evaluation export.',
            'created_at':now.isoformat(),'expires_at':(now+timedelta(minutes=15)).isoformat()}
        digest = content_hash(payload)
        record = {'id':secrets.token_urlsafe(24),'preview_hash':digest,'preview':payload,
            'confirmed_at':None,'revoked':False}
        self.save(record)
        return {'preview_id':record['id'],'preview_hash':digest,**payload}

    def confirm(self, body):
        def change(record):
            if not body.confirmed or body.preview_hash != record['preview_hash'] or record['revoked']:
                raise TDBError('egress_confirmation_mismatch',409)
            if self.clock() >= datetime.fromisoformat(record['preview']['expires_at']):
                raise TDBError('egress_confirmation_expired',409)
            record['confirmed_at'] = record['confirmed_at'] or self.clock().isoformat()
        record = self.mutate(body.preview_id,change)
        return {'consent_id':record['id'],'confirmed_at':record['confirmed_at'],
            'expires_at':record['preview']['expires_at']}

    def authorize(self, request, snapshot):
        record = self.get(request.egress_consent_id or '')
        if (record['revoked'] or not record['confirmed_at']
                or self.clock() >= datetime.fromisoformat(record['preview']['expires_at'])
                or record['preview']['binding'] != self.binding(request,snapshot)):
            raise TDBError('egress_confirmation_mismatch',409)
        return record['id']

    def revoke(self, identifier):
        self.mutate(identifier,lambda record:record.update(revoked=True))
        return {'status':'REVOKED'}

    def checked(self, task, provider):
        try:
            record = self.get(task.egress_consent_id or '')
            binding = record['preview']['binding']
            if (record['revoked'] or not record['confirmed_at']
                    or self.source_policies != record['preview']['source_policy']
                    or self.clock() >= datetime.fromisoformat(record['preview']['expires_at'])
                    or binding['profile'] != task.model_profile_id or binding['snapshot_hash'] != task.snapshot_hash
                    or binding['account_id'] != task.account_id
                    or provider == 'jev' and record['preview'].get('auxiliary_models',{}).get('jev') != self.auxiliary_models['jev']
                    or provider not in [binding['provider'],*record['preview'].get('auxiliary_targets',[])]
                    or binding['request']['question'] != task.question):
                raise ValueError('invalid grant')
            return record
        except (TDBError,ValueError,KeyError):
            raise LLMError('egress_confirmation_required','Local confirmation is absent, changed, expired or revoked') from None

    def source_policy(self, source, provider, categories, cutoff):
        evidence = source.evidence
        kind = evidence.evidence_type
        category = ('PUBLIC_FILINGS' if evidence.provider=='python_decimal' and evidence.structured_data.get('calculation')=='sec_quarterly_v1' else
            'OWNED_PORTFOLIO' if kind in {'portfolio','portfolio_snapshot','thesis','decision','semantic_judgment'}
            or evidence.provider in {'python_decimal','research_task'} else
            'PUBLIC_FILINGS' if kind in {'sec_filing','sec_fact'} and evidence.provider == 'sec_edgar' else
            'THIRD_PARTY_NEWS' if kind == 'news' else 'MARKET_QUOTES' if kind in {'market_data','market_price','quote','market_observation'} else None)
        policy = self.source_policies.get(evidence.provider,{})
        if policy.get('category') == 'SYNTHETIC':
            category = 'SYNTHETIC'
        if category not in categories:
            return None
        if category in {'OWNED_PORTFOLIO','PUBLIC_FILINGS'}:
            return {'category':category,'fields':['title','excerpt','structured_data'],
                'citation_scope':'PRIVATE_RESEARCH_ONLY','license_reference':'OWNED_CONSENT' if category=='OWNED_PORTFOLIO' else 'PUBLIC_SEC_FILINGS'}
        try:
            if (policy.get('category') != category or provider not in policy['destinations']
                    or not policy['license_reference'] or policy['citation_scope'] != 'PRIVATE_RESEARCH_ONLY'
                    or datetime.fromisoformat(policy['expires_at']) <= self.clock()):
                return None
            allowed = set(policy['fields'])
            if not allowed or allowed-{'title','excerpt','structured_data','facts_summary'}:
                return None
            return policy
        except (KeyError,ValueError,TypeError):
            return None

    def project(self, task, provider):
        record = self.checked(task,provider)
        categories = record['preview']['categories']
        sources, manifest = [], []
        for source in task.sources:
            evidence = source.evidence
            if source.identity in {'AI_DRAFT','HISTORICAL_DECISION'}:
                continue  # Generated history may contain unlicensed third-party prose.
            policy = self.source_policy(source,provider,categories,task.cutoff)
            if (policy is None or evidence.published_at and evidence.published_at > task.cutoff
                    or evidence.as_of and evidence.as_of > task.cutoff):
                continue
            fields = policy['fields']
            title = evidence.title if 'title' in fields else 'Authorized facts'
            excerpt = evidence.excerpt if 'excerpt' in fields else ''
            data = evidence.structured_data if 'structured_data' in fields else {}
            if 'facts_summary' in fields:
                # A provider/server-authored, already-qualified summary; never use
                # original news text as a fallback when rights allow only facts.
                summary = evidence.structured_data.get('egress_facts_summary')
                if not isinstance(summary,str) or not summary.strip():
                    continue
                excerpt, data = summary[:600], {}
            if policy['category'] == 'OWNED_PORTFOLIO' and evidence.provider == 'python_decimal':
                # Derived market values carry underlying quote rights. The minimal
                # owned path forwards only independently owned cost/cash/quantity.
                keep = {'cash','total_cost','position','symbol','quantity','average_cost','cost_basis','accounts','account_id'}
                def owned(value):
                    if isinstance(value,dict):
                        return {k:owned(v) for k,v in value.items() if k in keep}
                    if isinstance(value,list):
                        return [owned(v) for v in value]
                    return value
                data = owned(data)
                excerpt, title = '', 'Owned portfolio facts (quote-derived metrics excluded)'
            projected = evidence.model_copy(update={'title':title,'excerpt':excerpt,'structured_data':data})
            sources.append(source.model_copy(update={'evidence':projected}))
            manifest.append({'source':evidence.provider,'evidence_id':evidence.evidence_id,
                'source_hash':evidence.content_hash,'category':policy['category'],'allowed_fields':fields,
                'citation_scope':policy['citation_scope'],'license_reference':policy['license_reference'],
                'projection_hash':content_hash(projected.model_dump(mode='json'))})
        # Omit summaries/history that may have copied licensed third-party prose.
        own = task.portfolio_context or {}
        portfolio = {key:own[key] for key in ('snapshot_id','account_id','cash','total_cost','position_status') if key in own}
        if isinstance(own.get('position'),dict):
            portfolio['position'] = {k:v for k,v in own['position'].items()
                if k in {'symbol','quantity','average_cost','cost_basis'}}
        premise = {k:v for k,v in task.premise.items() if k in {'user_price','identity','verification'}}
        projected_task = task.model_copy(update={'sources':sources,'portfolio_context':portfolio,
            'premise':premise,'findings_summary':None,'unresolved_question':None})
        return projected_task,manifest

    def validate_send(self, task, provider, admission):
        record = self.checked(task,provider)
        _, manifest = self.project(task,provider)
        allowed = {row['evidence_id']:row for row in manifest}
        if any(allowed.get(row['evidence_id']) != row for row in admission['sources']):
            raise LLMError('data_rights_source_expired','Source permission changed or expired before dispatch')
        return record

    def record_send(self, task, provider, purpose, facts, sources, *, model=None):
        record = self.checked(task,provider)
        if provider == 'jev' and model != record['preview']['auxiliary_models']['jev']:
            raise LLMError('data_rights_target_mismatch','Auxiliary model differs from the confirmed preview')
        projected,manifest = self.project(task,provider)
        allowed = {s.evidence.evidence_id:s.evidence.model_dump(mode='json') for s in projected.sources}
        if any(allowed.get(s.evidence.evidence_id) != s.evidence.model_dump(mode='json') for s in sources):
            raise LLMError('data_rights_unprojected_source','Source bytes differ from the authorized projection')
        ids = {s.evidence.evidence_id for s in sources}
        if ids-{m['evidence_id'] for m in manifest}:
            raise LLMError('data_rights_unprojected_source','An unqualified source reached the send boundary')
        result = {'target':provider,'model':model or record['preview']['binding']['model'],
            'destination':record['preview']['binding']['destination'] if provider!='jev' else 'https://api.typesafe.ai/v1/systemone',
            'purpose':purpose,'cutoff':task.cutoff.isoformat(),'confirmed_at':record['confirmed_at'],
            'consent_id':record['id'],'payload_hash':content_hash(facts),
            'hash_scope':'MODEL_FACTS_PROJECTION',
            'sources':[m for m in manifest if m['evidence_id'] in ids],
            'owned_fields':record['preview']['allowed_owned_fields'],'status':'AUTHORIZED_BEFORE_SEND',
            'at':self.clock().isoformat()}
        with self.store._connect() as db:
            db.execute('INSERT INTO research_egress_attempts(task_id,payload) VALUES (?,?)',
                (task.task_id,json.dumps(result)))
        return result


def register_egress(app, service, egress):
    @app.post('/api/research/egress/preview')
    def preview(body: PreviewInput):
        return egress.preview(body)

    @app.post('/api/research/egress/confirm')
    def confirm(body: ConfirmInput):
        return egress.confirm(body)

    @app.delete('/api/research/egress/{consent_id}')
    def revoke(consent_id: str):
        return egress.revoke(consent_id)
