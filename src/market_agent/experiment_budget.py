"""Single designated experiment writer, layered over durable per-send CallBudget."""
import json
import os
import tempfile
from uuid import uuid4
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .call_budget import CallBudget
from .llm import LLMError


@contextmanager
def edit_ledger(path):
    path=Path(path)
    lock=path.with_name('API_BUDGET_LEDGER.writer.lock')
    descriptor=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    os.close(descriptor)
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        yield data
        data['updated_at_utc']=datetime.now(timezone.utc).isoformat()
        descriptor,temporary=tempfile.mkstemp(prefix='API_BUDGET_LEDGER.',suffix='.tmp',dir=path.parent)
        with os.fdopen(descriptor,'w',encoding='utf-8',newline='\n') as stream:
            json.dump(data,stream,indent=2);stream.flush();os.fsync(stream.fileno())
        json.loads(Path(temporary).read_text(encoding='utf-8'))
        os.replace(temporary,path)
        json.loads(path.read_text(encoding='utf-8'))
    finally:
        lock.unlink()


class SharedExperimentBudget(CallBudget):
    def __init__(self,path,*,ledger,reservation_id,provider):
        self.ledger=Path(ledger);self.reservation_id=reservation_id;self.provider=provider
        # Local request ids restart at 1 per run (and request hashes can repeat), so every
        # shared row is keyed by this run as well; otherwise a later run settles an earlier row.
        self.run_id=uuid4().hex
        data=json.loads(self.ledger.read_text(encoding='utf-8'))
        group=next((row for row in data['reservations'] if row['reservation_id']==reservation_id),None)
        if not group or group['status']!='RESERVED_BEFORE_SEND' or group.get('stopped') or data.get('round_reconciled') is not True:
            raise LLMError('api_budget_authorization','A reconciled live reservation is required')
        super().__init__(path,authorization=reservation_id+':'+provider,
            ceiling=group['provider_limits_usd'][provider],max_requests=32,max_models=30)
        self.assert_settled(group)

    def assert_settled(self,group):
        requests=[*group.get('requests',[]),*self.summary()['requests']]
        if any(row['status']!='RETURNED_USAGE_KNOWN' for row in requests):
            raise LLMError('api_budget_pending','Unsettled shared or local request requires explicit reconciliation')

    def reserve(self,kind,model,amount,request_hash):
        with edit_ledger(self.ledger) as data:
            group=next(row for row in data['reservations'] if row['reservation_id']==self.reservation_id)
            if group['status']!='RESERVED_BEFORE_SEND' or group.get('stopped'):
                raise LLMError('api_budget_stopped','Shared experiment is closed or stopped')
            self.assert_settled(group)
            requests=group.setdefault('requests',[])
            spent=sum((Decimal(row['upper_usd']) for row in requests if row['provider']==self.provider),Decimal(0))
            if (spent+Decimal(amount)>Decimal(group['provider_limits_usd'][self.provider])
                    or len(requests)>=group['max_requests']):
                raise LLMError('api_budget_cost','Shared provider or request allowance exhausted')
            request_id=super().reserve(kind,model,amount,request_hash)
            requests.append({'reservation_id':self.reservation_id,'request_id':request_id,'run_id':self.run_id,
                'provider':self.provider,'model':model,'task_id':group.get('task_id','LOOP-002'),
                'timestamp_utc':datetime.now(timezone.utc).isoformat(),'upper_usd':str(amount),
                'request_hash':request_hash,'status':'RESERVED_BEFORE_SEND',
                'estimated_cost_usd':None,'actual_usd':None,'billing_confirmed':False})
        return request_id

    def record(self,request_id,status,usage=None):
        super().record(request_id,status,usage)
        with edit_ledger(self.ledger) as data:
            group=next(row for row in data['reservations'] if row['reservation_id']==self.reservation_id)
            request=next(row for row in group['requests'] if row['provider']==self.provider
                and row['request_id']==request_id and row.get('run_id')==self.run_id)
            request.update(status=status,usage=usage,
                estimated_cost_usd=(usage or {}).get('standard_rate_estimate_usd'))
            if status!='RETURNED_USAGE_KNOWN' or (usage or {}).get('http_status',200)!=200:
                group['stopped']=True  # Any uncertain usage stops this entire experiment.
