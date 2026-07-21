# ruff: noqa: E501, E701, E702
"""ADR 0060 fake-only Pending worker: lease/attempt evidence only; Bound is write-zero."""
from __future__ import annotations
import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from collections.abc import Callable
from typing import Protocol, cast
from agent_org_network.trusted_source_binding_authority import SourceBindingAuthorityCapability
from agent_org_network.sqlite_reciprocal_review_source_binding_v7 import validate_sqlite_reciprocal_review_source_binding_v7
from agent_org_network.reciprocal_review import SourceBindingAuthorizationEnvelopeV7
from agent_org_network.source_binding_authorization import SourceBindingAuthorizationDenied, SourceBindingAuthorizationUnavailable

COMPONENT_ID="durable_reciprocal_review_source_binding_worker_v7"
_M="schema_component_manifests"; _L="source_binding_v7_worker_leases"; _A="source_binding_v7_apply_attempts"; _R="source_binding_v7_readbacks"; _O="source_binding_v7_worker_outbox"
_DDL={
 _L:f"CREATE TABLE {_L} (org_id TEXT NOT NULL,intent_id TEXT NOT NULL,lease_epoch INTEGER NOT NULL,worker_id TEXT NOT NULL,token_hash TEXT NOT NULL,expires_at TEXT NOT NULL,PRIMARY KEY(org_id,intent_id))",
 _A:f"CREATE TABLE {_A} (org_id TEXT NOT NULL,intent_id TEXT NOT NULL,attempt_no INTEGER NOT NULL,lease_epoch INTEGER NOT NULL,operation_digest TEXT NOT NULL,classification TEXT NOT NULL CHECK(classification IN ('dispatched','uncertain','observed')),created_at TEXT NOT NULL,PRIMARY KEY(org_id,intent_id,attempt_no))",
 _R:f"CREATE TABLE {_R} (org_id TEXT NOT NULL,intent_id TEXT NOT NULL,attempt_no INTEGER NOT NULL,observation_digest TEXT NOT NULL,classification TEXT NOT NULL CHECK(classification IN ('uncertain','observed')),created_at TEXT NOT NULL,PRIMARY KEY(org_id,intent_id,attempt_no),FOREIGN KEY(org_id,intent_id,attempt_no) REFERENCES {_A}(org_id,intent_id,attempt_no))",
 _O:f"CREATE TABLE {_O} (org_id TEXT NOT NULL,intent_id TEXT NOT NULL,delivery_key TEXT NOT NULL,delivered_at TEXT,PRIMARY KEY(org_id,intent_id,delivery_key))",
}
_T={f"{t}_no_{v}":f"CREATE TRIGGER {t}_no_{v} BEFORE {v.upper()} ON {t} BEGIN SELECT RAISE(ABORT,'immutable source-binding worker v7'); END" for t in (_A,_R) for v in ('update','delete')}
class SourceBindingWorkerError(RuntimeError):pass
_REQUEST_MINT=secrets.token_urlsafe(32)
@dataclass(frozen=True,init=False)
class SourceBindingOperationRequest:
 org_id:str;intent_id:str;source_ref:str;semantic_digest:str;idempotency_key:str;expected_source_revision:str;policy_digest:str;boundary_digest:str;verified_authorization:object
 def __init__(self,token:str,*,org_id:str,intent_id:str,source_ref:str,semantic_digest:str,idempotency_key:str,expected_source_revision:str,policy_digest:str,boundary_digest:str,verified_authorization:object)->None:
  if token!=_REQUEST_MINT:raise TypeError('PendingWorker-only operation request')
  object.__setattr__(self,'org_id',org_id);object.__setattr__(self,'intent_id',intent_id);object.__setattr__(self,'source_ref',source_ref);object.__setattr__(self,'semantic_digest',semantic_digest);object.__setattr__(self,'idempotency_key',idempotency_key);object.__setattr__(self,'expected_source_revision',expected_source_revision);object.__setattr__(self,'policy_digest',policy_digest);object.__setattr__(self,'boundary_digest',boundary_digest);object.__setattr__(self,'verified_authorization',verified_authorization)
class SourceBindingExecutor(Protocol):
 def apply(self, request: SourceBindingOperationRequest, lease_fence: int) -> object: ...
class FakeSourceBindingExecutor:
 """Deterministic test executor: CAS, semantic idempotency, and fence only."""
 def __init__(self,*,source_revision:str='source-revision-1')->None:self.source_revision=source_revision;self._operations:dict[str,object]={};self._fence=0;self.timeout=False;self.late:object|None=None;self.calls=0;self.last_request:SourceBindingOperationRequest|None=None
 def apply(self,request:object,lease_fence:int)->object:
  self.calls+=1
  if type(request) is not SourceBindingOperationRequest:raise RuntimeError('source_cas_conflict')
  self.last_request=request
  if request.expected_source_revision!=self.source_revision:raise RuntimeError('source_cas_conflict')
  if lease_fence<self._fence:raise RuntimeError('worker_fence_conflict')
  self._fence=lease_fence;key=request.idempotency_key
  if self.timeout:
   if self.late is not None:self._operations[key]=self.late
   raise TimeoutError('controlled_timeout')
  return self._operations.setdefault(key,{'source_revision':self.source_revision,'idempotency_key':key})
def _can(x:object)->str: import json;return json.dumps(x,sort_keys=True,separators=(',',':'),default=str)
def _dig(x:object)->str:return hashlib.sha256(_can(x).encode()).hexdigest()
def _now(c:sqlite3.Connection)->datetime:return datetime.fromisoformat(c.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0].replace('Z','+00:00'))
def _time(x:datetime)->str:return x.isoformat(timespec='milliseconds').replace('+00:00','Z')
def migrate_sqlite_source_binding_worker_v7(c:sqlite3.Connection)->None:
 try:
  c.execute('BEGIN IMMEDIATE')
  if c.execute(f"SELECT 1 FROM {_M} WHERE component_id='durable_reciprocal_review_source_binding_v7'").fetchone() is None:raise SourceBindingWorkerError('v7 Pending capability required')
  if c.execute(f"SELECT 1 FROM {_M} WHERE component_id=?",(COMPONENT_ID,)).fetchone() is None:
   for s in _DDL.values():c.execute(s)
   for s in _T.values():c.execute(s)
   catalog={"tables":_DDL,"triggers":_T};raw=_can(catalog);c.execute(f"INSERT INTO {_M} VALUES(?,?,?,?)",(COMPONENT_ID,1,raw,_dig(catalog)))
  c.commit()
 except Exception:
  if c.in_transaction:c.rollback()
  raise
def validate_sqlite_source_binding_worker_v7(c:sqlite3.Connection)->None:
 validate_sqlite_reciprocal_review_source_binding_v7(c)
 raw=_can({"tables":_DDL,"triggers":_T})
 if c.execute(f"SELECT schema_version,manifest_json,manifest_sha256 FROM {_M} WHERE component_id=?",(COMPONENT_ID,)).fetchone() != (1,raw,_dig({"tables":_DDL,"triggers":_T})):raise SourceBindingWorkerError('worker catalog unavailable')
 actual={x[0] for x in c.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'source_binding_v7_%'")}
 if actual!=set(_DDL)|set(_T) or any(' '.join(c.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",(n,)).fetchone()[0].split())!=' '.join(sql.split()) for n,sql in _DDL.items()) or any(' '.join(c.execute("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",(n,)).fetchone()[0].split())!=' '.join(sql.split()) for n,sql in _T.items()):raise SourceBindingWorkerError('worker catalog tamper')
 for org,intent,epoch,_worker,token_hash,expires in c.execute(f"SELECT org_id,intent_id,lease_epoch,worker_id,token_hash,expires_at FROM {_L}").fetchall():
  pending=c.execute("SELECT 1 FROM reciprocal_review_v7_binding_intents i JOIN durable_reciprocal_review_source_binding_cycles_v7 c ON c.org_id=i.org_id AND c.cycle_id=i.cycle_id WHERE i.org_id=? AND i.receipt_id=? AND c.state_kind='binding_pending'",(org,intent)).fetchone()
  try: expiry=datetime.fromisoformat(expires.replace('Z','+00:00'))
  except (AttributeError,ValueError): expiry=None
  if pending is None or type(epoch) is not int or epoch<1 or type(token_hash) is not str or len(token_hash)!=64 or expiry is None:raise SourceBindingWorkerError('worker semantic graph tamper')
 for org,intent,attempt,_epoch,operation,classification,_created in c.execute(f"SELECT org_id,intent_id,attempt_no,lease_epoch,operation_digest,classification,created_at FROM {_A}").fetchall():
  pending=c.execute("SELECT 1 FROM reciprocal_review_v7_binding_intents i JOIN durable_reciprocal_review_source_binding_cycles_v7 c ON c.org_id=i.org_id AND c.cycle_id=i.cycle_id WHERE i.org_id=? AND i.receipt_id=? AND c.state_kind='binding_pending'",(org,intent)).fetchone()
  observation=c.execute(f"SELECT classification FROM {_R} WHERE org_id=? AND intent_id=? AND attempt_no=?",(org,intent,attempt)).fetchone()
  delivery=c.execute(f"SELECT 1 FROM {_O} WHERE org_id=? AND intent_id=? AND delivery_key=?",(org,intent,operation)).fetchone()
  expected=c.execute("SELECT command_digest FROM reciprocal_review_v7_binding_intents WHERE org_id=? AND receipt_id=?",(org,intent)).fetchone()
  observation_digest=c.execute(f"SELECT observation_digest FROM {_R} WHERE org_id=? AND intent_id=? AND attempt_no=?",(org,intent,attempt)).fetchone()
  if pending is None or delivery is None or expected is None or operation!=_dig({'intent':intent,'semantic':expected[0]}) or classification!='dispatched' or (observation is not None and observation[0] not in ('uncertain','observed')) or (observation_digest is not None and (type(observation_digest[0]) is not str or len(observation_digest[0])!=64)):raise SourceBindingWorkerError('worker semantic graph tamper')
 for org,intent,operation,delivered in c.execute(f"SELECT org_id,intent_id,delivery_key,delivered_at FROM {_O}").fetchall():
  expected=c.execute("SELECT command_digest FROM reciprocal_review_v7_binding_intents WHERE org_id=? AND receipt_id=?",(org,intent)).fetchone()
  if expected is None or operation!=_dig({'intent':intent,'semantic':expected[0]}):raise SourceBindingWorkerError('worker semantic graph tamper')
  if delivered is not None:
   try: datetime.fromisoformat(delivered.replace('Z','+00:00'))
   except (AttributeError,ValueError):raise SourceBindingWorkerError('worker semantic graph tamper') from None
class PendingWorker:
 def __init__(self,path:str|Path,executor:SourceBindingExecutor,authority_capability:SourceBindingAuthorityCapability,*,fault_injector:Callable[[str],None]|None=None,before_apply:Callable[[],None]|None=None)->None:
  self.path=Path(path)
  if type(authority_capability) is not SourceBindingAuthorityCapability or not authority_capability.is_live() or not authority_capability.matches_sqlite_target(self.path):raise ValueError('live bootstrap source-binding capability required')
  # ADR 0061 admits only the registry-created deterministic signed fake as an
  # additional Pending observation path.  A caller-supplied adapter remains closed.
  from agent_org_network.trusted_source_integration import TrustedPendingExecutor
  if type(executor) not in (FakeSourceBindingExecutor,TrustedPendingExecutor):raise ValueError('S1c.2 requires sealed FakeSourceBindingExecutor or trusted Pending executor')
  self.executor=executor;self.authority_capability=authority_capability;self.fault_injector=fault_injector;self.before_apply=before_apply
 def _hit(self,point:str)->None:
  if self.fault_injector is not None:self.fault_injector(point)
 def _request(self,*,c:sqlite3.Connection,org_id:str,intent_id:str,operation:str)->SourceBindingOperationRequest:
  intent=c.execute("SELECT command_digest,source_ref,authorization_json FROM reciprocal_review_v7_binding_intents WHERE org_id=? AND receipt_id=?",(org_id,intent_id)).fetchone()
  if intent is None:raise SourceBindingWorkerError('pending intent unavailable')
  try: receipt=SourceBindingAuthorizationEnvelopeV7.model_validate_json(intent[2])
  except ValueError as exc:raise SourceBindingWorkerError('authorization receipt unavailable') from exc
  if receipt.org_id!=org_id or receipt.source_ref!=intent[1] or not self.authority_capability.matches_sqlite_target(self.path) or not self.authority_capability.matches_source_ref(receipt.source_ref):raise SourceBindingWorkerError('authority capability unavailable')
  resolution=self.authority_capability.verify_current(receipt=receipt,purpose='SourceApply',readback=None,db_now=_now(c))
  if isinstance(resolution,(SourceBindingAuthorizationDenied,SourceBindingAuthorizationUnavailable)):raise SourceBindingWorkerError('source apply authorization denied')
  return SourceBindingOperationRequest(_REQUEST_MINT,org_id=org_id,intent_id=intent_id,source_ref=receipt.source_ref,semantic_digest=intent[0],idempotency_key=operation,expected_source_revision=receipt.expected_source_revision,policy_digest=receipt.policy_digest,boundary_digest=receipt.boundary_digest,verified_authorization=resolution)
 def claim(self,*,org_id:str,intent_id:str,worker_id:str,lease_for:timedelta=timedelta(minutes=1))->tuple[int,str]:
  c=sqlite3.connect(self.path,isolation_level=None)
  try:
   c.execute('BEGIN IMMEDIATE');validate_sqlite_source_binding_worker_v7(c);self._request(c=c,org_id=org_id,intent_id=intent_id,operation=_dig({'intent':intent_id,'semantic':c.execute("SELECT command_digest FROM reciprocal_review_v7_binding_intents WHERE org_id=? AND receipt_id=?",(org_id,intent_id)).fetchone()[0]}));now=_now(c);row=c.execute(f"SELECT lease_epoch,expires_at FROM {_L} WHERE org_id=? AND intent_id=?",(org_id,intent_id)).fetchone()
   if row and datetime.fromisoformat(row[1].replace('Z','+00:00'))>now:raise SourceBindingWorkerError('lease unavailable')
   epoch=(row[0]+1 if row else 1);token=secrets.token_urlsafe(32);h=_dig(token);c.execute(f"INSERT INTO {_L} VALUES(?,?,?,?,?,?) ON CONFLICT(org_id,intent_id) DO UPDATE SET lease_epoch=excluded.lease_epoch,worker_id=excluded.worker_id,token_hash=excluded.token_hash,expires_at=excluded.expires_at",(org_id,intent_id,epoch,worker_id,h,_time(now+lease_for)));self._hit('after_lease');c.commit();return epoch,token
  except Exception:
   if c.in_transaction:c.rollback()
   raise
  finally:c.close()
 def execute(self,*,org_id:str,intent_id:str,worker_id:str,epoch:int,token:str)->str:
  """Dispatch evidence commits before the external call; terminal state remains Pending."""
  if not self.authority_capability.matches_sqlite_target(self.path):raise SourceBindingWorkerError('authority capability unavailable')
  c=sqlite3.connect(self.path,isolation_level=None)
  try:
   c.execute('BEGIN IMMEDIATE');validate_sqlite_source_binding_worker_v7(c);now=_now(c);lease=c.execute(f"SELECT worker_id,token_hash,expires_at FROM {_L} WHERE org_id=? AND intent_id=? AND lease_epoch=?",(org_id,intent_id,epoch)).fetchone()
   intent=c.execute("SELECT command_digest,source_ref,authorization_json FROM reciprocal_review_v7_binding_intents WHERE org_id=? AND receipt_id=?",(org_id,intent_id)).fetchone()
   if lease is None or intent is None or lease[0]!=worker_id or lease[1]!=_dig(token) or datetime.fromisoformat(lease[2].replace('Z','+00:00'))<=now:raise SourceBindingWorkerError('stale lease or pending intent')
   previous=c.execute(f"SELECT 1 FROM {_A} WHERE org_id=? AND intent_id=? AND lease_epoch=?",(org_id,intent_id,epoch)).fetchone()
   if previous is not None:raise SourceBindingWorkerError('lease delivery already dispatched')
   attempt=c.execute(f"SELECT COALESCE(MAX(attempt_no),0)+1 FROM {_A} WHERE org_id=? AND intent_id=?",(org_id,intent_id)).fetchone()[0];operation=_dig({'intent':intent_id,'semantic':intent[0]})
   c.execute(f"INSERT INTO {_A} VALUES(?,?,?,?,?,?,?)",(org_id,intent_id,attempt,epoch,operation,'dispatched',_time(now)));self._hit('after_attempt');c.execute(f"INSERT OR IGNORE INTO {_O} VALUES(?,?,?,NULL)",(org_id,intent_id,operation));self._hit('after_outbox');c.commit()
  except Exception:
   if c.in_transaction:c.rollback()
   raise
  finally:c.close()
  # Deliberately outside the SQLite transaction/lock, after a final fence check.
  if self.before_apply is not None:self.before_apply()
  try:
   c=sqlite3.connect(self.path,isolation_level=None);c.execute('BEGIN IMMEDIATE');validate_sqlite_source_binding_worker_v7(c);now=_now(c);lease=c.execute(f"SELECT worker_id,token_hash,expires_at FROM {_L} WHERE org_id=? AND intent_id=? AND lease_epoch=?",(org_id,intent_id,epoch)).fetchone()
   if lease is None or lease[0]!=worker_id or lease[1]!=_dig(token) or datetime.fromisoformat(lease[2].replace('Z','+00:00'))<=now:raise SourceBindingWorkerError('stale lease before source apply')
   request=self._request(c=c,org_id=org_id,intent_id=intent_id,operation=operation);c.commit();c.close()
  except Exception as exc:
   if 'c' in locals() and c.in_transaction:c.rollback()
   if 'c' in locals():c.close()
   observed={'uncertain':type(exc).__name__};classification='uncertain'
  else:
   try:
    observed=self.executor.apply(request,epoch);classification='observed'
   except Exception as exc:
    observed={'uncertain':type(exc).__name__};classification='uncertain'
  c=sqlite3.connect(self.path,isolation_level=None)
  try:
   c.execute('BEGIN IMMEDIATE');c.execute(f"INSERT INTO {_R} VALUES(?,?,?,?,?,?)",(org_id,intent_id,attempt,_dig(observed),classification,_time(_now(c))));self._hit('after_observation')
   if classification=='observed':c.execute(f"UPDATE {_O} SET delivered_at=? WHERE org_id=? AND intent_id=? AND delivery_key=?",(_time(_now(c)),org_id,intent_id,operation));self._hit('after_delivery')
   c.commit();return classification
  except Exception:
   if c.in_transaction:c.rollback()
   raise
  finally:c.close()
 def recover(self,*,worker_id:str)->tuple[tuple[str,str],...]:
  """At-least-once redelivery: expired/absent leases are reclaimed with same operation key."""
  c=sqlite3.connect(self.path);rows=tuple(cast(tuple[str,str],x) for x in c.execute(f"SELECT org_id,intent_id FROM {_O} WHERE delivered_at IS NULL"));c.close();done:list[tuple[str,str]]=[]
  for org,intent in rows:
   try:
    epoch,token=self.claim(org_id=org,intent_id=intent,worker_id=worker_id);self.execute(org_id=org,intent_id=intent,worker_id=worker_id,epoch=epoch,token=token);done.append((org,intent))
   except SourceBindingWorkerError:pass
  return tuple(done)
