# ruff: noqa: E501, E701, E702
"""P18 S1c v6: source-binding intent/Pending companion (ADR 0058).

No source mutation is performed here.  The adapter is queried only for its
current capability and body-free plan/authorization; external apply belongs to
the later fenced worker slice.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Protocol, cast

from agent_org_network.reciprocal_review import (
    CreateSourceBindingIntent, CreatedSourceBindingIntent, SourceBindingAttestation, SourceBindingAuthorityReceipt,
)
from agent_org_network.sqlite_reciprocal_review_human_disposition import (
    COMPONENT_ID as V2_COMPONENT_ID, validate_sqlite_reciprocal_review_human_disposition,
)
from agent_org_network.sqlite_reciprocal_review_ai_mixed_disposition import (
    COMPONENT_ID as V5_COMPONENT_ID, validate_sqlite_reciprocal_review_ai_mixed_disposition,
)

COMPONENT_ID = "durable_reciprocal_review_source_binding_v6"
_MANIFEST = "schema_component_manifests"
_CYCLE = "durable_reciprocal_review_source_binding_cycles_v6"
_INTENT = "reciprocal_review_v6_binding_intents"
_AUDIT = "reciprocal_review_v6_binding_audit"
_OUTBOX = "reciprocal_review_v6_binding_outbox"
_TABLES = (_CYCLE, _INTENT, _AUDIT, _OUTBOX)
_DDLS = {
 _CYCLE: "CREATE TABLE durable_reciprocal_review_source_binding_cycles_v6 (org_id TEXT NOT NULL,cycle_id TEXT NOT NULL,upstream_kind TEXT NOT NULL CHECK(upstream_kind IN ('v2','v5')),upstream_revision INTEGER NOT NULL CHECK(upstream_revision>=1),revision_id TEXT NOT NULL,state_kind TEXT NOT NULL CHECK(state_kind IN ('binding_ready','binding_pending','bound','superseded')),binding_generation INTEGER NOT NULL CHECK(binding_generation>=1),created_at TEXT NOT NULL,PRIMARY KEY(org_id,cycle_id),UNIQUE(org_id,cycle_id,binding_generation))",
 _INTENT: "CREATE TABLE reciprocal_review_v6_binding_intents (org_id TEXT NOT NULL,receipt_id TEXT NOT NULL,audit_id TEXT NOT NULL,outbox_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,command_digest TEXT NOT NULL CHECK(length(command_digest)=64),cycle_id TEXT NOT NULL,source_ref TEXT NOT NULL,expected_source_revision TEXT NOT NULL,authority_json TEXT NOT NULL,authority_digest TEXT NOT NULL CHECK(length(authority_digest)=64),plan_json TEXT NOT NULL,plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64),drift_authorization_json TEXT NOT NULL,drift_authorization_digest TEXT NOT NULL CHECK(length(drift_authorization_digest)=64),capability_json TEXT NOT NULL,attestation_key_id TEXT NOT NULL,attestation_issued_at TEXT NOT NULL,attestation_expires_at TEXT NOT NULL,attestation_digest TEXT NOT NULL CHECK(length(attestation_digest)=64),boundary_digest TEXT NOT NULL CHECK(length(boundary_digest)=64),content_digest TEXT NOT NULL CHECK(length(content_digest)=64),binding_generation INTEGER NOT NULL CHECK(binding_generation>=1),created_at TEXT NOT NULL,PRIMARY KEY(org_id,receipt_id),UNIQUE(org_id,idempotency_key),UNIQUE(org_id,audit_id),UNIQUE(org_id,outbox_id),UNIQUE(org_id,cycle_id,binding_generation),FOREIGN KEY(org_id,cycle_id) REFERENCES durable_reciprocal_review_source_binding_cycles_v6(org_id,cycle_id))",
 _AUDIT: "CREATE TABLE reciprocal_review_v6_binding_audit (org_id TEXT NOT NULL,audit_id TEXT NOT NULL,receipt_id TEXT NOT NULL,event_digest TEXT NOT NULL CHECK(length(event_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,audit_id),UNIQUE(org_id,receipt_id),FOREIGN KEY(org_id,receipt_id) REFERENCES reciprocal_review_v6_binding_intents(org_id,receipt_id))",
 _OUTBOX: "CREATE TABLE reciprocal_review_v6_binding_outbox (org_id TEXT NOT NULL,outbox_id TEXT NOT NULL,receipt_id TEXT NOT NULL,payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,outbox_id),UNIQUE(org_id,receipt_id),FOREIGN KEY(org_id,receipt_id) REFERENCES reciprocal_review_v6_binding_intents(org_id,receipt_id))",
}
_TRIGGERS = {f"{t}_no_{v}": f"CREATE TRIGGER {t}_no_{v} BEFORE {v.upper()} ON {t} BEGIN SELECT RAISE(ABORT,'immutable reciprocal review v6'); END" for t in _TABLES[1:] for v in ('update','delete')}
_TRIGGERS[f"{_CYCLE}_legal_update"] = f"CREATE TRIGGER {_CYCLE}_legal_update BEFORE UPDATE ON {_CYCLE} FOR EACH ROW WHEN NOT (OLD.state_kind='binding_ready' AND NEW.state_kind='binding_pending' AND NEW.binding_generation=OLD.binding_generation+1 AND OLD.org_id=NEW.org_id AND OLD.cycle_id=NEW.cycle_id AND OLD.upstream_kind=NEW.upstream_kind AND OLD.upstream_revision=NEW.upstream_revision AND OLD.revision_id=NEW.revision_id AND OLD.created_at=NEW.created_at) BEGIN SELECT RAISE(ABORT,'illegal reciprocal review v6 transition'); END"
_TRIGGERS[f"{_CYCLE}_no_delete"] = f"CREATE TRIGGER {_CYCLE}_no_delete BEFORE DELETE ON {_CYCLE} BEGIN SELECT RAISE(ABORT,'immutable reciprocal review v6'); END"

class SqliteReciprocalReviewSourceBindingError(RuntimeError): pass
class SqliteReciprocalReviewSourceBindingConflict(SqliteReciprocalReviewSourceBindingError): pass

class SourceBindingAdapter(Protocol):
 def attest_binding_intent(self, *, org_id: str, source_ref: str, revision_id: str, content_digest: str, boundary_digest: str, now: datetime) -> SourceBindingAttestation: ...

def _canonical(x: object) -> str: return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
def _digest(x: object) -> str: return hashlib.sha256(_canonical(x).encode()).hexdigest()
def _time(x: datetime) -> str:
 if x.tzinfo is None or x.utcoffset()!=UTC.utcoffset(x) or x.microsecond%1000: raise SqliteReciprocalReviewSourceBindingError('DB time은 canonical UTC milliseconds여야 합니다.')
 return x.isoformat(timespec='milliseconds').replace('+00:00','Z')
def _now(c: sqlite3.Connection) -> datetime: return datetime.fromisoformat(c.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0].replace('Z','+00:00'))
def _same(a: object,b: str)->bool:return isinstance(a,str) and ' '.join(a.split())==' '.join(b.split())
def _catalog()->dict[str,object]: return {'tables':[{'name':n,'sql':' '.join(s.split())} for n,s in _DDLS.items()],'triggers':[{'name':n,'sql':' '.join(s.split())} for n,s in _TRIGGERS.items()]}

def validate_sqlite_reciprocal_review_source_binding(c: sqlite3.Connection) -> None:
 validate_sqlite_reciprocal_review_human_disposition(c)
 m=_canonical({'component_id':COMPONENT_ID,'schema_version':6,'catalog':_catalog()})
 if c.execute(f'SELECT schema_version,manifest_json,manifest_sha256 FROM {_MANIFEST} WHERE component_id=?',(COMPONENT_ID,)).fetchone() != (6,m,hashlib.sha256(m.encode()).hexdigest()): raise SqliteReciprocalReviewSourceBindingError('reciprocal review v6 manifest가 canonical하지 않습니다.')
 actual={x[0] for x in c.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'durable_reciprocal_review_source_binding_cycles_v6%' OR name LIKE 'reciprocal_review_v6_%'")}
 if actual != set(_TABLES)|set(_TRIGGERS) or any(not _same(c.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",(n,)).fetchone()[0],s) for n,s in _DDLS.items()) or any(not _same(c.execute("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",(n,)).fetchone()[0],s) for n,s in _TRIGGERS.items()): raise SqliteReciprocalReviewSourceBindingError('reciprocal review v6 catalog가 canonical하지 않습니다.')
 receipts=set(c.execute(f'SELECT org_id,receipt_id FROM {_INTENT}'))
 if set(c.execute(f'SELECT org_id,receipt_id FROM {_AUDIT}'))!=receipts or set(c.execute(f'SELECT org_id,receipt_id FROM {_OUTBOX}'))!=receipts: raise SqliteReciprocalReviewSourceBindingError('v6 intent evidence graph가 bijection이 아닙니다.')
 # v6 owns only the Pending intent.  A row in any other state would imply an
 # external apply/terminal slice that this component deliberately does not
 # implement, so it must not be accepted as a valid ledger.
 for org, cycle, kind, upstream_rev, revision, state, generation, created in c.execute(f'SELECT org_id,cycle_id,upstream_kind,upstream_revision,revision_id,state_kind,binding_generation,created_at FROM {_CYCLE}'):
  if state != 'binding_pending' or generation != 2:
   raise SqliteReciprocalReviewSourceBindingError('v6은 BindingPending intent만 소유합니다.')
  try: _time(datetime.fromisoformat(created.replace('Z','+00:00')))
  except (TypeError, ValueError, SqliteReciprocalReviewSourceBindingError) as exc: raise SqliteReciprocalReviewSourceBindingError('v6 timestamp가 canonical하지 않습니다.') from exc
  if kind == 'v2':
   upstream = c.execute("SELECT revision_id FROM durable_reciprocal_review_cycles_v2 WHERE org_id=? AND cycle_id=? AND state_kind='binding_ready' AND cycle_revision=?",(org,cycle,upstream_rev)).fetchone()
  else:
   upstream = c.execute("SELECT revision_id FROM durable_reciprocal_review_cycles_v5 WHERE org_id=? AND cycle_id=? AND state_kind='binding_ready' AND result_revision=?",(org,cycle,upstream_rev)).fetchone()
  if upstream != (revision,): raise SqliteReciprocalReviewSourceBindingError('v6 upstream BindingReady가 current/canonical하지 않습니다.')
 for org, receipt, audit, outbox, digest, cycle, _source, _expected_source, plan, drift, boundary, content, generation, created in c.execute(f'SELECT org_id,receipt_id,audit_id,outbox_id,command_digest,cycle_id,source_ref,expected_source_revision,plan_digest,drift_authorization_digest,boundary_digest,content_digest,binding_generation,created_at FROM {_INTENT}'):
  cycle_row = c.execute(f'SELECT state_kind,binding_generation FROM {_CYCLE} WHERE org_id=? AND cycle_id=?',(org,cycle)).fetchone()
  audit_row = c.execute(f'SELECT receipt_id,event_digest,created_at FROM {_AUDIT} WHERE org_id=? AND audit_id=?',(org,audit)).fetchone()
  outbox_row = c.execute(f'SELECT receipt_id,payload_digest,created_at FROM {_OUTBOX} WHERE org_id=? AND outbox_id=?',(org,outbox)).fetchone()
  if cycle_row != ('binding_pending',generation) or generation != 2 or audit_row != (receipt,_digest(('v6_binding_audit',digest)),created) or outbox_row != (receipt,_digest(('v6_binding_outbox',digest)),created):
   raise SqliteReciprocalReviewSourceBindingError('v6 intent/evidence semantic graph가 canonical하지 않습니다.')
  if any(type(value) is not str or len(value) != 64 for value in (digest,plan,drift,boundary,content)):
   raise SqliteReciprocalReviewSourceBindingError('v6 digest가 canonical하지 않습니다.')
  try: _time(datetime.fromisoformat(created.replace('Z','+00:00')))
  except (TypeError, ValueError, SqliteReciprocalReviewSourceBindingError) as exc: raise SqliteReciprocalReviewSourceBindingError('v6 timestamp가 canonical하지 않습니다.') from exc
 if c.execute('PRAGMA foreign_key_check').fetchone() is not None: raise SqliteReciprocalReviewSourceBindingError('v6 FK가 canonical하지 않습니다.')

def migrate_sqlite_reciprocal_review_source_binding_v6(c: sqlite3.Connection, *, fault_injector: Callable[[str],None]|None=None)->None:
 try:
  c.execute('BEGIN IMMEDIATE')
  if c.execute(f'SELECT 1 FROM {_MANIFEST} WHERE component_id=?',(V2_COMPONENT_ID,)).fetchone() is None: raise SqliteReciprocalReviewSourceBindingError('v6에는 explicit v2 snapshot이 필요합니다.')
  if c.execute(f'SELECT 1 FROM {_MANIFEST} WHERE component_id=?',(COMPONENT_ID,)).fetchone() is None:
   for n,s in _DDLS.items():
    c.execute(s)
    if fault_injector:
     fault_injector(f'after_{n}')
   for s in _TRIGGERS.values(): c.execute(s)
   m=_canonical({'component_id':COMPONENT_ID,'schema_version':6,'catalog':_catalog()});c.execute(f'INSERT INTO {_MANIFEST} VALUES(?,?,?,?)',(COMPONENT_ID,6,m,hashlib.sha256(m.encode()).hexdigest()))
  validate_sqlite_reciprocal_review_source_binding(c);c.commit()
 except Exception:
  if c.in_transaction:c.rollback()
  raise

class SourceBindingIntentUnitOfWork(Protocol):
 def create(self, command: CreateSourceBindingIntent) -> CreatedSourceBindingIntent: ...

class _Uow:  # pyright: ignore[reportUnusedClass]
 def __init__(self,path: str|Path,adapter: SourceBindingAdapter,adapter_keys: Mapping[str, bytes],authority_keys: Mapping[str, bytes],fault: Callable[[str],None]|None)->None:self.path=Path(path);self.adapter=adapter;self.adapter_keys=dict(adapter_keys);self.authority_keys=dict(authority_keys);self.fault=fault
 def create(self,command: CreateSourceBindingIntent)->CreatedSourceBindingIntent:
  raise SqliteReciprocalReviewSourceBindingError('v6 source binding intent는 legacy closed boundary입니다.')
  c=sqlite3.connect(self.path,timeout=30,isolation_level=None)
  try:
   c.execute('PRAGMA foreign_keys=ON');c.execute('BEGIN IMMEDIATE');validate_sqlite_reciprocal_review_source_binding(c);now=_now(c)
   upstream=self._upstream(c,command.cycle_id,command.expected_upstream_revision)
   rev=c.execute("SELECT content_sha256,data_classification,boundary_snapshot_ref,boundary_digest,declassification_receipt_id FROM durable_reciprocal_review_artifact_revisions WHERE org_id=? AND revision_id=?",(upstream[0],upstream[3])).fetchone()
   if rev is None: raise SqliteReciprocalReviewSourceBindingError('revision evidence가 없습니다.')
   authority=command.authority_receipt
   if authority is None:raise SqliteReciprocalReviewSourceBindingError('v6 source binding intent는 authority receipt 없이 재개할 수 없습니다.')
   self._verify_authority(authority, upstream, command.source_ref, rev, now)
   attestation=self.adapter.attest_binding_intent(org_id=upstream[0],source_ref=command.source_ref,revision_id=upstream[3],content_digest=rev[0],boundary_digest=rev[3],now=now)
   self._verify_attestation(attestation, upstream, command.source_ref, rev, now)
   cap,plan,drift=attestation.capability,attestation.plan,attestation.drift_authorization
   semantic={'command':command.model_dump(mode='json'),'upstream':upstream,'attestation':attestation.model_dump(mode='json')};digest=_digest(semantic)
   old=c.execute(f'SELECT command_digest FROM {_INTENT} WHERE org_id=? AND idempotency_key=?',(upstream[0],command.idempotency_key)).fetchone()
   if old:
    if old[0]!=digest:raise SqliteReciprocalReviewSourceBindingConflict('idempotency semantic command가 다릅니다.')
    out=self._result(c,upstream[0],command.receipt_id,digest);c.commit();return out
   c.execute(f'INSERT INTO {_CYCLE} VALUES(?,?,?,?,?,?,?,?)',(upstream[0],command.cycle_id,upstream[1],upstream[2],upstream[3],'binding_ready',1,_time(now)))
   if c.execute(f"UPDATE {_CYCLE} SET state_kind='binding_pending',binding_generation=2 WHERE org_id=? AND cycle_id=? AND state_kind='binding_ready' AND binding_generation=1",(upstream[0],command.cycle_id)).rowcount!=1:raise SqliteReciprocalReviewSourceBindingConflict('Ready->Pending CAS가 stale입니다.')
   self._hit('after_pending_cas')
   c.execute(f'INSERT INTO {_INTENT} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(upstream[0],command.receipt_id,command.audit_id,command.outbox_id,command.idempotency_key,digest,command.cycle_id,command.source_ref,plan.expected_source_revision,_canonical(authority.model_dump(mode='json')),_digest(authority.model_dump(mode='json')),_canonical(plan.model_dump(mode='json')),_digest(plan.model_dump(mode='json')),_canonical(drift.model_dump(mode='json')),_digest(drift.model_dump(mode='json')),_canonical(cap.model_dump(mode='json')),attestation.key_id,_time(attestation.issued_at),_time(attestation.expires_at),_digest(attestation.model_dump(mode='json')),rev[3],rev[0],2,_time(now)))
   c.execute(f'INSERT INTO {_AUDIT} VALUES(?,?,?,?,?)',(upstream[0],command.audit_id,command.receipt_id,_digest(('v6_binding_audit',digest)),_time(now)));c.execute(f'INSERT INTO {_OUTBOX} VALUES(?,?,?,?,?)',(upstream[0],command.outbox_id,command.receipt_id,_digest(('v6_binding_outbox',digest)),_time(now)));self._hit('after_outbox');validate_sqlite_reciprocal_review_source_binding(c);c.commit();return self._result(c,upstream[0],command.receipt_id,digest)
  except Exception:
   if c.in_transaction:c.rollback()
   raise
  finally:c.close()
 def _hit(self,x:str)->None:
  if self.fault:self.fault(x)
 def _verify_authority(self,a: SourceBindingAuthorityReceipt, upstream: tuple[str,str,int,str], source: str, rev: tuple[object,...], now: datetime)->None:
  key=self.authority_keys.get(a.key_id)
  payload=a.model_dump(mode='json',exclude={'signature'})
  if key is None or now<a.issued_at or now>a.expires_at or not hmac.compare_digest(a.signature,hmac.new(key,_canonical(payload).encode(),hashlib.sha256).hexdigest()) or (a.org_id,a.source_ref,a.revision_id,a.content_digest,a.data_classification,a.boundary_snapshot_ref,a.boundary_digest,a.declassification_receipt_id)!=(upstream[0],source,upstream[3],rev[0],rev[1],rev[2],rev[3],rev[4]): raise SqliteReciprocalReviewSourceBindingError('current central source-binding authority가 없습니다.')
 def _verify_attestation(self,a: SourceBindingAttestation, upstream: tuple[str,str,int,str], source: str, rev: tuple[object,...], now: datetime)->None:
  key=self.adapter_keys.get(a.key_id)
  payload=a.model_dump(mode='json',exclude={'signature'})
  plan,drift=a.plan,a.drift_authorization
  if key is None or now<a.issued_at or now>a.expires_at or not hmac.compare_digest(a.signature,hmac.new(key,_canonical(payload).encode(),hashlib.sha256).hexdigest()) or plan.source_ref!=source or plan.revision_id!=upstream[3] or plan.content_digest!=rev[0] or plan.boundary_digest!=rev[3] or plan.expected_source_revision!=drift.expected_source_revision or plan.expires_at is not None and plan.expires_at<=now or drift.source_ref!=source or drift.boundary_digest!=rev[3] or drift.expires_at<=now: raise SqliteReciprocalReviewSourceBindingError('trusted source adapter attestation이 없습니다.')
 def _upstream(self,c:sqlite3.Connection,cycle:str,expected:int)->tuple[str,str,int,str]:
  # v5 owns AI/mixed; v2 owns the human lane.  Ambiguity is fail-closed.
  rows: list[tuple[str, str, int, str]]=[]
  r=c.execute("SELECT org_id,'v2',cycle_revision,revision_id FROM durable_reciprocal_review_cycles_v2 WHERE cycle_id=? AND state_kind='binding_ready' AND cycle_revision=?",(cycle,expected)).fetchall();rows += [cast(tuple[str, str, int, str], x) for x in r]
  if c.execute(f'SELECT 1 FROM {_MANIFEST} WHERE component_id=?',(V5_COMPONENT_ID,)).fetchone():
   validate_sqlite_reciprocal_review_ai_mixed_disposition(c);rows += [cast(tuple[str, str, int, str], x) for x in c.execute("SELECT org_id,'v5',result_revision,revision_id FROM durable_reciprocal_review_cycles_v5 WHERE cycle_id=? AND state_kind='binding_ready' AND result_revision=?",(cycle,expected)).fetchall()]
  if len(rows)!=1:raise SqliteReciprocalReviewSourceBindingConflict('exact BindingReady upstream이 없습니다.')
  return rows[0]
 def _result(self,c:sqlite3.Connection,org:str,receipt:str,digest:str)->CreatedSourceBindingIntent:
  r=c.execute(f'SELECT cycle_id,command_digest,binding_generation,created_at FROM {_INTENT} WHERE org_id=? AND receipt_id=?',(org,receipt)).fetchone()
  if r is None or r[1]!=digest:raise SqliteReciprocalReviewSourceBindingError('immutable v6 result가 없습니다.')
  return CreatedSourceBindingIntent(org_id=org,cycle_id=r[0],receipt_id=receipt,command_digest=r[1],binding_generation=r[2],created_at=datetime.fromisoformat(r[3].replace('Z','+00:00')))

def create_sqlite_reciprocal_review_source_binding_intent_uow(path: str|Path, *, adapter: object, fault_injector: Callable[[str],None]|None=None)->SourceBindingIntentUnitOfWork:
 """v6 draft is legacy-unverifiable and intentionally closed (ADR 0059 follow-up)."""
 raise SqliteReciprocalReviewSourceBindingError('v6 source binding intent는 legacy closed boundary입니다.')
