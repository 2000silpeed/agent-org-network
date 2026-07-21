# ruff: noqa: E401, E501, E701, E702
"""ADR 0059 v7: authority-issued Pending intent only; source apply is write-zero."""
from __future__ import annotations
import hashlib, json, sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from agent_org_network.reciprocal_review import CreateSourceBindingIntentV7, CreatedSourceBindingIntent, IntegrationEnforcementProfileV7, SourceBindingAuthorizationEnvelopeV7
from agent_org_network.source_binding_authorization import SourceBindingAuthorizationAuthority, SourceBindingAuthorizationDenied, SourceBindingAuthorizationUnavailable
from agent_org_network.trusted_source_binding_authority import SourceBindingAuthorityCapability
from agent_org_network.sqlite_reciprocal_review_human_disposition import validate_sqlite_reciprocal_review_human_disposition
from agent_org_network.sqlite_reciprocal_review_ai_mixed_disposition import COMPONENT_ID as V5, validate_sqlite_reciprocal_review_ai_mixed_disposition

COMPONENT_ID='durable_reciprocal_review_source_binding_v7'; _M='schema_component_manifests'; _C='durable_reciprocal_review_source_binding_cycles_v7'; _I='reciprocal_review_v7_binding_intents'; _A='reciprocal_review_v7_binding_audit'; _O='reciprocal_review_v7_binding_outbox'
_DDL={_C:f"CREATE TABLE {_C} (org_id TEXT NOT NULL,cycle_id TEXT NOT NULL,upstream_kind TEXT NOT NULL CHECK(upstream_kind IN ('v2','v5')),upstream_revision INTEGER NOT NULL,revision_id TEXT NOT NULL,state_kind TEXT NOT NULL CHECK(state_kind='binding_pending'),binding_generation INTEGER NOT NULL CHECK(binding_generation=1),created_at TEXT NOT NULL,PRIMARY KEY(org_id,cycle_id))",_I:f"CREATE TABLE {_I} (org_id TEXT NOT NULL,receipt_id TEXT NOT NULL,audit_id TEXT NOT NULL,outbox_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,command_digest TEXT NOT NULL CHECK(length(command_digest)=64),cycle_id TEXT NOT NULL,source_ref TEXT NOT NULL,authorization_json TEXT NOT NULL,authorization_digest TEXT NOT NULL CHECK(length(authorization_digest)=64),integration_profile_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(org_id,receipt_id),UNIQUE(org_id,idempotency_key),UNIQUE(org_id,audit_id),UNIQUE(org_id,outbox_id),FOREIGN KEY(org_id,cycle_id) REFERENCES {_C}(org_id,cycle_id))",_A:f"CREATE TABLE {_A} (org_id TEXT NOT NULL,audit_id TEXT NOT NULL,receipt_id TEXT NOT NULL,event_digest TEXT NOT NULL CHECK(length(event_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,audit_id),FOREIGN KEY(org_id,receipt_id) REFERENCES {_I}(org_id,receipt_id))",_O:f"CREATE TABLE {_O} (org_id TEXT NOT NULL,outbox_id TEXT NOT NULL,receipt_id TEXT NOT NULL,payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,outbox_id),FOREIGN KEY(org_id,receipt_id) REFERENCES {_I}(org_id,receipt_id))"}
_T={f'{t}_no_{v}':f"CREATE TRIGGER {t}_no_{v} BEFORE {v.upper()} ON {t} BEGIN SELECT RAISE(ABORT,'immutable reciprocal review v7'); END" for t in (_I,_A,_O) for v in ('update','delete')};_T[f'{_C}_no_update']=f"CREATE TRIGGER {_C}_no_update BEFORE UPDATE ON {_C} BEGIN SELECT RAISE(ABORT,'immutable reciprocal review v7'); END";_T[f'{_C}_no_delete']=f"CREATE TRIGGER {_C}_no_delete BEFORE DELETE ON {_C} BEGIN SELECT RAISE(ABORT,'immutable reciprocal review v7'); END"
class SqliteReciprocalReviewSourceBindingV7Error(RuntimeError):pass
class SqliteReciprocalReviewSourceBindingV7Conflict(SqliteReciprocalReviewSourceBindingV7Error):pass
def _can(x:object)->str:return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def _dig(x:object)->str:return hashlib.sha256(_can(x).encode()).hexdigest()
def _now(c:sqlite3.Connection)->datetime:return datetime.fromisoformat(c.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0].replace('Z','+00:00'))
def _time(x:datetime)->str:
 if x.tzinfo is None or x.utcoffset()!=UTC.utcoffset(x) or x.microsecond%1000:raise SqliteReciprocalReviewSourceBindingV7Error('DB time must be UTC milliseconds')
 return x.isoformat(timespec='milliseconds').replace('+00:00','Z')
def _catalog()->dict[str,object]:return {'tables':[{'name':n,'sql':' '.join(s.split())}for n,s in _DDL.items()],'triggers':[{'name':n,'sql':' '.join(s.split())}for n,s in _T.items()]}
def _intent_preimage(e:SourceBindingAuthorizationEnvelopeV7,u:tuple[str,str,int,str],p:IntegrationEnforcementProfileV7)->dict[str,object]:return {'org_id':u[0],'source_resource':e.source_resource.model_dump(mode='json'),'expected_source_revision':e.expected_source_revision,'revision_id':u[3],'content_digest':e.content_digest,'data_classification':e.data_classification,'boundary_snapshot_ref':e.boundary_snapshot_ref,'boundary_digest':e.boundary_digest,'declassification_receipt_id':e.declassification_receipt_id,'policy_digest':e.policy_digest,'principal_grant_digest':e.principal_grant_digest,'integration_profile':p.model_dump(mode='json'),'every_read_mode':e.every_read_mode,'drift_action':e.drift_action,'drift_action_digest':e.drift_action_digest,'drift_expires_at':e.drift_expires_at.isoformat(timespec='milliseconds').replace('+00:00','Z')}
def migrate_sqlite_reciprocal_review_source_binding_v7(c:sqlite3.Connection)->None:
 try:
  c.execute('BEGIN IMMEDIATE');validate_sqlite_reciprocal_review_human_disposition(c)
  if c.execute(f'SELECT 1 FROM {_M} WHERE component_id=?',(COMPONENT_ID,)).fetchone() is None:
   for s in _DDL.values():c.execute(s)
   for s in _T.values():c.execute(s)
   m=_can({'component_id':COMPONENT_ID,'schema_version':7,'catalog':_catalog()});c.execute(f'INSERT INTO {_M} VALUES(?,?,?,?)',(COMPONENT_ID,7,m,hashlib.sha256(m.encode()).hexdigest()))
  validate_sqlite_reciprocal_review_source_binding_v7(c);c.commit()
 except Exception:
  if c.in_transaction:c.rollback()
  raise
def validate_sqlite_reciprocal_review_source_binding_v7(c:sqlite3.Connection)->None:
 validate_sqlite_reciprocal_review_human_disposition(c);m=_can({'component_id':COMPONENT_ID,'schema_version':7,'catalog':_catalog()})
 if c.execute(f'SELECT schema_version,manifest_json,manifest_sha256 FROM {_M} WHERE component_id=?',(COMPONENT_ID,)).fetchone()!=(7,m,hashlib.sha256(m.encode()).hexdigest()):raise SqliteReciprocalReviewSourceBindingV7Error('v7 catalog unavailable')
 actual={x[0]for x in c.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'durable_reciprocal_review_source_binding_cycles_v7%' OR name LIKE 'reciprocal_review_v7_%'")}
 if actual!=set(_DDL)|set(_T) or any(' '.join(c.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",(n,)).fetchone()[0].split())!=' '.join(s.split()) for n,s in _DDL.items()) or any(' '.join(c.execute("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",(n,)).fetchone()[0].split())!=' '.join(s.split()) for n,s in _T.items()):raise SqliteReciprocalReviewSourceBindingV7Error('v7 catalog tamper')
 if c.execute(f'SELECT count(*) FROM {_C}').fetchone()!=c.execute(f'SELECT count(*) FROM {_I}').fetchone() or c.execute(f'SELECT count(*) FROM {_I}').fetchone()!=c.execute(f'SELECT count(*) FROM {_A}').fetchone() or c.execute(f'SELECT count(*) FROM {_I}').fetchone()!=c.execute(f'SELECT count(*) FROM {_O}').fetchone():raise SqliteReciprocalReviewSourceBindingV7Error('v7 graph not bijective')
 for org,receipt,audit,outbox,digest,cycle,source,raw,raw_digest,profile,created in c.execute(f'SELECT org_id,receipt_id,audit_id,outbox_id,command_digest,cycle_id,source_ref,authorization_json,authorization_digest,integration_profile_json,created_at FROM {_I}'):
  try:
   decoded=json.loads(raw);decoded_profile=json.loads(profile);env=SourceBindingAuthorizationEnvelopeV7.model_validate_json(raw);IntegrationEnforcementProfileV7.model_validate_json(profile);at=datetime.fromisoformat(created.replace('Z','+00:00'))
  except (TypeError,ValueError,json.JSONDecodeError) as exc:raise SqliteReciprocalReviewSourceBindingV7Error('source_binding_receipt_semantic_tamper') from exc
  audit_row=c.execute(f'SELECT receipt_id,event_digest,created_at FROM {_A} WHERE org_id=? AND audit_id=?',(org,audit)).fetchone();outbox_row=c.execute(f'SELECT receipt_id,payload_digest,created_at FROM {_O} WHERE org_id=? AND outbox_id=?',(org,outbox)).fetchone();cycle_row=c.execute(f'SELECT upstream_kind,upstream_revision,revision_id,state_kind FROM {_C} WHERE org_id=? AND cycle_id=?',(org,cycle)).fetchone()
  if cycle_row is None:raise SqliteReciprocalReviewSourceBindingV7Error('source_binding_receipt_semantic_tamper')
  expected=cycle_row[1]
  upstream_row=(c.execute("SELECT revision_id FROM durable_reciprocal_review_cycles_v2 WHERE org_id=? AND cycle_id=? AND state_kind='binding_ready' AND cycle_revision=?",(org,cycle,expected)).fetchone() if cycle_row[0]=='v2' else c.execute("SELECT revision_id FROM durable_reciprocal_review_cycles_v5 WHERE org_id=? AND cycle_id=? AND state_kind='binding_ready' AND result_revision=?",(org,cycle,expected)).fetchone())
  if upstream_row is None:raise SqliteReciprocalReviewSourceBindingV7Error('source_binding_receipt_semantic_tamper')
  authoritative=(org,cycle_row[0],expected,upstream_row[0]);cmd=CreateSourceBindingIntentV7(receipt_id=receipt,audit_id=audit,outbox_id=outbox,idempotency_key=c.execute(f'SELECT idempotency_key FROM {_I} WHERE org_id=? AND receipt_id=?',(org,receipt)).fetchone()[0],cycle_id=cycle,expected_upstream_revision=expected,source_ref=source);semantic=_dig({'command':cmd.model_dump(mode='json'),'upstream':authoritative,'authorization':env.model_dump(mode='json'),'profile':IntegrationEnforcementProfileV7.model_validate_json(profile).model_dump(mode='json')})
  if _can(decoded)!=raw or _can(decoded_profile)!=profile or _dig(env.model_dump(mode='json'))!=raw_digest or env.org_id!=org or env.source_ref!=source or cycle_row[3]!='binding_pending' or authoritative[3]!=cycle_row[2] or digest!=semantic or audit_row!=(receipt,_dig(('v7_binding_audit',digest)),created) or outbox_row!=(receipt,_dig(('v7_binding_outbox',digest)),created):raise SqliteReciprocalReviewSourceBindingV7Error('source_binding_receipt_semantic_tamper')
  _time(at)
class _Uow:
 def __init__(self,path:Path,authority:SourceBindingAuthorizationAuthority,authority_capability:SourceBindingAuthorityCapability,profiles:Mapping[str,IntegrationEnforcementProfileV7],fault:Callable[[str],None]|None)->None:self.path=path;self.authority=authority;self.authority_capability=authority_capability;self.profiles=dict(profiles);self.fault=fault
 def create(self,cmd:CreateSourceBindingIntentV7)->CreatedSourceBindingIntent:
  if not self.authority_capability.matches_sqlite_target(self.path) or not self.authority_capability.matches_source_ref(cmd.source_ref):raise SqliteReciprocalReviewSourceBindingV7Error('production source-binding target/wiring unavailable')
  c=sqlite3.connect(self.path,isolation_level=None,timeout=30)
  try:
   c.execute('PRAGMA foreign_keys=ON');c.execute('BEGIN IMMEDIATE');validate_sqlite_reciprocal_review_source_binding_v7(c)
   if not self.authority_capability.is_live():raise SqliteReciprocalReviewSourceBindingV7Error('production source-binding capability unavailable')
   self._validate_stored(c);now=_now(c);up=self._up(c,cmd.cycle_id,cmd.expected_upstream_revision);r=c.execute("SELECT content_sha256,data_classification,boundary_snapshot_ref,boundary_digest,declassification_receipt_id FROM durable_reciprocal_review_artifact_revisions WHERE org_id=? AND revision_id=?",(up[0],up[3])).fetchone()
   if r is None:raise SqliteReciprocalReviewSourceBindingV7Error('revision absent')
   old=c.execute(f'SELECT receipt_id,audit_id,outbox_id,command_digest,authorization_json,authorization_digest,integration_profile_json,created_at FROM {_I} WHERE org_id=? AND idempotency_key=?',(up[0],cmd.idempotency_key)).fetchone()
   if old:
    env,profile=self._stored(old,cmd,up,r,now)
    d=self._semantic(cmd,up,env,profile)
    if old[0]!=cmd.receipt_id or old[1]!=cmd.audit_id or old[2]!=cmd.outbox_id or old[3]!=d:raise SqliteReciprocalReviewSourceBindingV7Conflict('idempotency_payload_conflict')
    c.commit();return CreatedSourceBindingIntent(org_id=up[0],cycle_id=cmd.cycle_id,receipt_id=cmd.receipt_id,command_digest=d,binding_generation=1,created_at=datetime.fromisoformat(old[7].replace('Z','+00:00')))
   issued=self.authority.issue_intent(cycle_id=cmd.cycle_id,source_ref=cmd.source_ref,db_now=now)
   if not isinstance(issued, SourceBindingAuthorizationEnvelopeV7):raise SqliteReciprocalReviewSourceBindingV7Error('current central authorization unavailable')
   env=issued;profile=self._verify(env,up,r,cmd.source_ref,now);self._current(env,now,'intent_create');d=self._semantic(cmd,up,env,profile)
   c.execute(f'INSERT INTO {_C} VALUES(?,?,?,?,?,?,?,?)',(up[0],cmd.cycle_id,up[1],up[2],up[3],'binding_pending',1,_time(now)));self._hit('after_pending')
   c.execute(f'INSERT INTO {_I} VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(up[0],cmd.receipt_id,cmd.audit_id,cmd.outbox_id,cmd.idempotency_key,d,cmd.cycle_id,cmd.source_ref,_can(env.model_dump(mode="json")),_dig(env.model_dump(mode="json")),_can(profile.model_dump(mode="json")),_time(now)));self._hit('after_intent')
   c.execute(f'INSERT INTO {_A} VALUES(?,?,?,?,?)',(up[0],cmd.audit_id,cmd.receipt_id,_dig(("v7_binding_audit",d)),_time(now)));self._hit('after_audit')
   c.execute(f'INSERT INTO {_O} VALUES(?,?,?,?,?)',(up[0],cmd.outbox_id,cmd.receipt_id,_dig(("v7_binding_outbox",d)),_time(now)));self._hit('after_outbox');validate_sqlite_reciprocal_review_source_binding_v7(c);c.commit();return CreatedSourceBindingIntent(org_id=up[0],cycle_id=cmd.cycle_id,receipt_id=cmd.receipt_id,command_digest=d,binding_generation=1,created_at=now)
  except Exception:
   if c.in_transaction:c.rollback()
   raise
  finally:c.close()
 def _hit(self,point:str)->None:
  if self.fault:self.fault(point)
 def _validate_stored(self,c:sqlite3.Connection)->None:
  for org,cycle,source,raw,profile in c.execute(f'SELECT org_id,cycle_id,source_ref,authorization_json,integration_profile_json FROM {_I}'):
   try: env=SourceBindingAuthorizationEnvelopeV7.model_validate_json(raw);stored=IntegrationEnforcementProfileV7.model_validate_json(profile);up=self._up(c,cycle,c.execute(f'SELECT upstream_revision FROM {_C} WHERE org_id=? AND cycle_id=?',(org,cycle)).fetchone()[0]);revision=c.execute("SELECT content_sha256,data_classification,boundary_snapshot_ref,boundary_digest,declassification_receipt_id FROM durable_reciprocal_review_artifact_revisions WHERE org_id=? AND revision_id=?",(org,up[3])).fetchone()
   except (TypeError,ValueError,json.JSONDecodeError,IndexError) as exc:raise SqliteReciprocalReviewSourceBindingV7Error('source_binding_receipt_semantic_tamper') from exc
   if revision is None:raise SqliteReciprocalReviewSourceBindingV7Error('source_binding_receipt_semantic_tamper')
   self._verify(env,up,revision,source,_now(c),stored)
   self._current(env,_now(c),'intent_open')
 def _current(self,env:SourceBindingAuthorizationEnvelopeV7,now:datetime,purpose:str)->None:
  result=self.authority_capability.verify_current(receipt=env,purpose=purpose,db_now=now)
  if isinstance(result,SourceBindingAuthorizationUnavailable):raise SqliteReciprocalReviewSourceBindingV7Error('source_binding_authority_unavailable')
  if isinstance(result,SourceBindingAuthorizationDenied):raise SqliteReciprocalReviewSourceBindingV7Error('current central authorization unavailable')
 def _semantic(self,cmd:CreateSourceBindingIntentV7,up:tuple[str,str,int,str],env:SourceBindingAuthorizationEnvelopeV7,profile:IntegrationEnforcementProfileV7)->str:return _dig({'command':cmd.model_dump(mode='json'),'upstream':up,'authorization':env.model_dump(mode='json'),'profile':profile.model_dump(mode='json')})
 def _stored(self,row:tuple[object,...],cmd:CreateSourceBindingIntentV7,up:tuple[str,str,int,str],r:tuple[object,...],now:datetime)->tuple[SourceBindingAuthorizationEnvelopeV7,IntegrationEnforcementProfileV7]:
  try:
   raw_env=json.loads(cast(str,row[4]));raw_profile=json.loads(cast(str,row[6]));env=SourceBindingAuthorizationEnvelopeV7.model_validate_json(cast(str,row[4]));profile=IntegrationEnforcementProfileV7.model_validate_json(cast(str,row[6]))
  except (TypeError,ValueError,json.JSONDecodeError) as exc:raise SqliteReciprocalReviewSourceBindingV7Error('source_binding_receipt_semantic_tamper') from exc
  if _can(raw_env)!=row[4] or _can(raw_profile)!=row[6] or _dig(env.model_dump(mode='json'))!=row[5]:raise SqliteReciprocalReviewSourceBindingV7Error('source_binding_receipt_semantic_tamper')
  # A replay never trusts a DB DTO: it re-verifies its signature, current bindings,
  # and (when implemented by the Authority port) current revocation/policy state.
  self._verify(env,up,r,cmd.source_ref,now,profile)
  self._current(env,now,'intent_replay')
  return env,profile
 def _up(self,c:sqlite3.Connection,cycle:str,expected:int)->tuple[str,str,int,str]:
  rows=[cast(tuple[str,str,int,str],x)for x in c.execute("SELECT org_id,'v2',cycle_revision,revision_id FROM durable_reciprocal_review_cycles_v2 WHERE cycle_id=? AND state_kind='binding_ready' AND cycle_revision=?",(cycle,expected)).fetchall()]
  if c.execute(f'SELECT 1 FROM {_M} WHERE component_id=?',(V5,)).fetchone():validate_sqlite_reciprocal_review_ai_mixed_disposition(c);rows += [cast(tuple[str,str,int,str],x)for x in c.execute("SELECT org_id,'v5',result_revision,revision_id FROM durable_reciprocal_review_cycles_v5 WHERE cycle_id=? AND state_kind='binding_ready' AND result_revision=?",(cycle,expected)).fetchall()]
  if len(rows)!=1:raise SqliteReciprocalReviewSourceBindingV7Conflict('exact BindingReady unavailable')
  return rows[0]
 def _verify(self,e:SourceBindingAuthorizationEnvelopeV7,u:tuple[str,str,int,str],r:tuple[object,...],source:str,now:datetime,stored:IntegrationEnforcementProfileV7|None=None)->IntegrationEnforcementProfileV7:
  p=self.profiles.get(e.integration_id)
  if p is None or e.payload_digest!=_dig(e.model_dump(mode='json',exclude={'signature','payload_digest'})) or now<e.issued_at or now>=e.expires_at or now>=e.drift_expires_at or (e.source_resource.org_id,e.source_resource.resource_kind,e.source_resource.resource_id)!=(u[0],'source',source) or (p.profile_version,p.profile_digest,p.enforcement_plan_digest)!=(e.integration_profile_version,e.integration_profile_digest,e.enforcement_plan_digest) or (e.org_id,e.source_ref,e.revision_id,e.content_digest,e.data_classification,e.boundary_snapshot_ref,e.boundary_digest,e.declassification_receipt_id)!=(u[0],source,u[3],r[0],r[1],r[2],r[3],r[4]) or e.intent_semantic_digest!=_dig(_intent_preimage(e,u,p)):raise SqliteReciprocalReviewSourceBindingV7Error('current central authorization unavailable')
  if stored is not None and stored != p:raise SqliteReciprocalReviewSourceBindingV7Error('source_binding_receipt_semantic_tamper')
  return p
def create_sqlite_reciprocal_review_source_binding_v7_uow(path:str|Path,*,authority:SourceBindingAuthorizationAuthority,authority_capability:SourceBindingAuthorityCapability,trusted_integration_profiles:Mapping[str,IntegrationEnforcementProfileV7],fault_injector:Callable[[str],None]|None=None):
 if type(authority_capability) is not SourceBindingAuthorityCapability or not authority_capability.matches_sqlite_target(path) or not trusted_integration_profiles or not callable(getattr(authority,'issue_intent',None)):raise ValueError('production authority capability/registry required')
 return _Uow(Path(path),authority,authority_capability,trusted_integration_profiles,fault_injector)
