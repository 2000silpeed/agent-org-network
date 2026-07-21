# ruff: noqa: E501, E701, E702, F841
"""P18 S1b.5a-v4 assignment-scoped finding-free human terminal evidence.

This is deliberately an additive capability.  It never infers assignments for a
legacy cycle and it never writes a binding/source/proposal state.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_org_network.reciprocal_review import HumanPrincipal, HumanReviewConclusion
from agent_org_network.sqlite_reciprocal_review_human_terminal import (
    COMPONENT_ID as V3_COMPONENT_ID,
    validate_sqlite_reciprocal_review_human_terminal,
)

COMPONENT_ID = "durable_reciprocal_review_ledger_v4"
_MANIFEST = "schema_component_manifests"
_CYCLE = "durable_reciprocal_review_cycles_v4"
_OWNERSHIP = "reciprocal_review_v4_cycle_ownership"
_ASSIGN = "reciprocal_review_v4_reviewer_assignments"
_RUN = "reciprocal_review_v4_assignment_runs"
_RECEIPT = "reciprocal_review_v4_human_terminal_receipts"
_RESULT = "reciprocal_review_v4_human_terminal_results"
_AUDIT = "reciprocal_review_v4_human_terminal_audit"
_OUTBOX = "reciprocal_review_v4_human_terminal_outbox"
_TABLES = (_CYCLE, _OWNERSHIP, _ASSIGN, _RUN, _RECEIPT, _RESULT, _AUDIT, _OUTBOX)

_DDLS = {
 _CYCLE: "CREATE TABLE durable_reciprocal_review_cycles_v4 (org_id TEXT NOT NULL,cycle_id TEXT NOT NULL,revision_id TEXT NOT NULL,cycle_no INTEGER NOT NULL,state_kind TEXT NOT NULL CHECK(state_kind IN ('review_open','awaiting_human_disposition','binding_ready','binding_pending','bound','superseded')),active INTEGER NOT NULL CHECK(active IN (0,1)),provenance_digest TEXT NOT NULL CHECK(length(provenance_digest)=64),policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),created_at TEXT NOT NULL,cycle_revision INTEGER NOT NULL CHECK(cycle_revision>=1),PRIMARY KEY(org_id,cycle_id))",
 _OWNERSHIP: "CREATE TABLE reciprocal_review_v4_cycle_ownership (org_id TEXT NOT NULL,cycle_id TEXT NOT NULL,owner TEXT NOT NULL CHECK(owner='v4'),created_at TEXT NOT NULL,PRIMARY KEY(org_id,cycle_id),FOREIGN KEY(org_id,cycle_id) REFERENCES durable_reciprocal_review_cycles_v4(org_id,cycle_id))",
 _ASSIGN: "CREATE TABLE reciprocal_review_v4_reviewer_assignments (org_id TEXT NOT NULL,assignment_id TEXT NOT NULL,cycle_id TEXT NOT NULL,requirement_id TEXT NOT NULL,reviewer_ref TEXT NOT NULL,ordinal INTEGER NOT NULL CHECK(ordinal>=1),assignment_digest TEXT NOT NULL CHECK(length(assignment_digest)=64),contributor_digest TEXT NOT NULL CHECK(length(contributor_digest)=64),policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),provenance_digest TEXT NOT NULL CHECK(length(provenance_digest)=64),content_digest TEXT NOT NULL CHECK(length(content_digest)=64),rubric_digest TEXT NOT NULL CHECK(length(rubric_digest)=64),input_digest TEXT NOT NULL CHECK(length(input_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,assignment_id),UNIQUE(org_id,requirement_id,reviewer_ref),UNIQUE(org_id,cycle_id,reviewer_ref),FOREIGN KEY(org_id,cycle_id) REFERENCES durable_reciprocal_review_cycles_v4(org_id,cycle_id))",
 _RUN: "CREATE TABLE reciprocal_review_v4_assignment_runs (org_id TEXT NOT NULL,assignment_run_id TEXT NOT NULL,assignment_id TEXT NOT NULL,attempt INTEGER NOT NULL CHECK(attempt>=1),lease_epoch INTEGER NOT NULL CHECK(lease_epoch>=1),token_hash TEXT,expires_at TEXT,state TEXT NOT NULL CHECK(state IN ('queued','leased','recorded','expired')),PRIMARY KEY(org_id,assignment_run_id),UNIQUE(org_id,assignment_id,attempt),FOREIGN KEY(org_id,assignment_id) REFERENCES reciprocal_review_v4_reviewer_assignments(org_id,assignment_id))",
 _RECEIPT: "CREATE TABLE reciprocal_review_v4_human_terminal_receipts (org_id TEXT NOT NULL,receipt_id TEXT NOT NULL,audit_id TEXT NOT NULL,outbox_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,command_digest TEXT NOT NULL CHECK(length(command_digest)=64),cycle_id TEXT NOT NULL,requirement_id TEXT NOT NULL,assignment_id TEXT NOT NULL,assignment_run_id TEXT NOT NULL,lease_epoch INTEGER NOT NULL,subject_id TEXT NOT NULL,content_digest TEXT NOT NULL CHECK(length(content_digest)=64),rubric_digest TEXT NOT NULL CHECK(length(rubric_digest)=64),input_digest TEXT NOT NULL CHECK(length(input_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,receipt_id),UNIQUE(org_id,idempotency_key),UNIQUE(org_id,audit_id),UNIQUE(org_id,outbox_id),UNIQUE(org_id,assignment_id))",
 _RESULT: "CREATE TABLE reciprocal_review_v4_human_terminal_results (org_id TEXT NOT NULL,receipt_id TEXT NOT NULL,cycle_id TEXT NOT NULL,assignment_id TEXT NOT NULL,command_digest TEXT NOT NULL CHECK(length(command_digest)=64),cycle_revision INTEGER NOT NULL,cycle_state TEXT NOT NULL CHECK(cycle_state IN ('review_open','awaiting_human_disposition')),created_at TEXT NOT NULL,PRIMARY KEY(org_id,receipt_id))",
 _AUDIT: "CREATE TABLE reciprocal_review_v4_human_terminal_audit (org_id TEXT NOT NULL,audit_id TEXT NOT NULL,receipt_id TEXT NOT NULL,event_digest TEXT NOT NULL CHECK(length(event_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,audit_id),UNIQUE(org_id,receipt_id))",
 _OUTBOX: "CREATE TABLE reciprocal_review_v4_human_terminal_outbox (org_id TEXT NOT NULL,outbox_id TEXT NOT NULL,receipt_id TEXT NOT NULL,payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,outbox_id),UNIQUE(org_id,receipt_id))",
}
_TRIGGERS = {f"{t}_no_{v}": f"CREATE TRIGGER {t}_no_{v} BEFORE {v.upper()} ON {t} BEGIN SELECT RAISE(ABORT,'immutable reciprocal review v4'); END" for t in (_OWNERSHIP,_ASSIGN,_RECEIPT,_RESULT,_AUDIT,_OUTBOX) for v in ('update','delete')}
_TRIGGERS[f"{_CYCLE}_legal_update"] = f"CREATE TRIGGER {_CYCLE}_legal_update BEFORE UPDATE ON {_CYCLE} WHEN NOT (OLD.state_kind='review_open' AND NEW.state_kind='awaiting_human_disposition' AND NEW.cycle_revision=OLD.cycle_revision+1 AND OLD.org_id=NEW.org_id AND OLD.cycle_id=NEW.cycle_id AND OLD.revision_id=NEW.revision_id AND OLD.cycle_no=NEW.cycle_no AND OLD.active=NEW.active AND OLD.provenance_digest=NEW.provenance_digest AND OLD.policy_digest=NEW.policy_digest AND OLD.created_at=NEW.created_at) BEGIN SELECT RAISE(ABORT,'illegal reciprocal review v4 transition'); END"
_TRIGGERS[f"{_CYCLE}_no_delete"] = f"CREATE TRIGGER {_CYCLE}_no_delete BEFORE DELETE ON {_CYCLE} BEGIN SELECT RAISE(ABORT,'immutable reciprocal review v4'); END"

class SqliteReciprocalReviewAssignmentTerminalError(RuntimeError): pass
class SqliteReciprocalReviewAssignmentTerminalConflict(SqliteReciprocalReviewAssignmentTerminalError): pass

def _canonical(x: object) -> str: return json.dumps(x, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
def _digest(x: object) -> str: return hashlib.sha256(_canonical(x).encode()).hexdigest()
def _time(x: datetime) -> str:
 if x.tzinfo is None or x.utcoffset()!=UTC.utcoffset(x) or x.microsecond%1000: raise SqliteReciprocalReviewAssignmentTerminalError('DB time은 canonical UTC milliseconds여야 합니다.')
 return x.isoformat(timespec='milliseconds').replace('+00:00','Z')
def sqlite_db_now(c: sqlite3.Connection) -> datetime:
 """Read the transaction's authoritative clock, never a caller-supplied one."""
 value=c.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
 return datetime.fromisoformat(value.replace('Z','+00:00'))

def assignment_human_authority_payload(
 *, org_id:str, reviewer:str, authenticated_at:datetime, expires_at:datetime,
 revision_id:str, cycle_id:str, requirement_id:str, assignment_id:str,
 assignment_run_id:str, assignment_digest:str, policy_digest:str,
 provenance_digest:str, contributor_digest:str, content_digest:str,
 rubric_digest:str, input_digest:str, completion_rule:str, required_count:int,
 candidate_reviewers:tuple[str,...], contributors:tuple[str,...],
) -> dict[str, object]:
 plan={'requirement_id':requirement_id,'completion_rule':completion_rule,'required_count':required_count,'candidate_reviewers':candidate_reviewers}
 independence_rule='reviewer_not_contributor_and_unique_requirement_candidate'
 independence_result=reviewer not in contributors and candidate_reviewers.count(reviewer)==1
 independence_digest=_digest({'rule':independence_rule,'result':independence_result,'reviewer':reviewer,'contributors':contributors,'candidate_reviewers':candidate_reviewers,'plan_digest':_digest(plan)})
 return {'org_id':org_id,'reviewer':reviewer,'authenticated_at':_time(authenticated_at),'expires_at':_time(expires_at),'revision_id':revision_id,'cycle_id':cycle_id,'requirement_id':requirement_id,'assignment_id':assignment_id,'assignment_run_id':assignment_run_id,'assignment_digest':assignment_digest,'assignment_plan':plan,'assignment_plan_digest':_digest(plan),'assignment_candidate_set':candidate_reviewers,'independence_rule':independence_rule,'independence_result':independence_result,'independence_digest':independence_digest,'policy_digest':policy_digest,'provenance_digest':provenance_digest,'contributor_digest':contributor_digest,'content_digest':content_digest,'rubric_digest':rubric_digest,'input_digest':input_digest}
def _catalog() -> dict[str, object]: return {'tables':[{'name':n,'sql':' '.join(s.split())} for n,s in _DDLS.items()],'triggers':[{'name':n,'sql':' '.join(s.split())} for n,s in _TRIGGERS.items()]}
def _same(a: object,b:str)->bool: return isinstance(a,str) and ' '.join(a.split())==' '.join(b.split())

def validate_sqlite_reciprocal_review_assignment_terminal(c: sqlite3.Connection) -> None:
 validate_sqlite_reciprocal_review_human_terminal(c)
 manifest=_canonical({'component_id':COMPONENT_ID,'schema_version':4,'catalog':_catalog()})
 if c.execute(f'SELECT schema_version,manifest_json,manifest_sha256 FROM {_MANIFEST} WHERE component_id=?',(COMPONENT_ID,)).fetchone() != (4,manifest,hashlib.sha256(manifest.encode()).hexdigest()): raise SqliteReciprocalReviewAssignmentTerminalError('reciprocal review v4 manifest가 canonical하지 않습니다.')
 actual={r[0] for r in c.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'durable_reciprocal_review_cycles_v4%' OR name LIKE 'reciprocal_review_v4_%'")}
 if actual != set(_TABLES)|set(_TRIGGERS) or any(not _same(c.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",(n,)).fetchone()[0],s) for n,s in _DDLS.items()) or any(not _same(c.execute("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",(n,)).fetchone()[0],s) for n,s in _TRIGGERS.items()): raise SqliteReciprocalReviewAssignmentTerminalError('reciprocal review v4 catalog가 canonical하지 않습니다.')
 for org,cycle,revision,state,rev in c.execute(f'SELECT org_id,cycle_id,revision_id,state_kind,cycle_revision FROM {_CYCLE}'):
  v3=c.execute('SELECT revision_id,cycle_no,active,provenance_digest,policy_digest,created_at FROM durable_reciprocal_review_cycles_v3 WHERE org_id=? AND cycle_id=?',(org,cycle)).fetchone()
  if v3 is None or v3[0]!=revision or state not in {'review_open','awaiting_human_disposition'} or type(rev) is not int or rev<1: raise SqliteReciprocalReviewAssignmentTerminalError('forged reciprocal review v4 cycle row가 있습니다.')
  if c.execute(f'SELECT owner FROM {_OWNERSHIP} WHERE org_id=? AND cycle_id=?',(org,cycle)).fetchone()!=('v4',): raise SqliteReciprocalReviewAssignmentTerminalError('v4 cycle ownership가 없습니다.')
 for org,_assignment,cycle,req,reviewer,ordinal,*_ in c.execute(f'SELECT * FROM {_ASSIGN}'):
  if c.execute(f'SELECT 1 FROM {_CYCLE} WHERE org_id=? AND cycle_id=?',(org,cycle)).fetchone() is None or c.execute('SELECT 1 FROM durable_reciprocal_review_requirements WHERE org_id=? AND requirement_id=? AND cycle_id=? AND reviewer_kind=\'human\'',(org,req,cycle)).fetchone() is None or not reviewer or type(ordinal) is not int: raise SqliteReciprocalReviewAssignmentTerminalError('forged reviewer assignment이 있습니다.')
 # Each immutable assignment owns exactly its initial run.  v4 has no run
 # spawning operation, so an additional attempt is forged rather than retry evidence.
 assignments={(org,aid) for org,aid in c.execute(f'SELECT org_id,assignment_id FROM {_ASSIGN}')}
 run_rows=list(c.execute(f'SELECT org_id,assignment_run_id,assignment_id,attempt,lease_epoch,token_hash,expires_at,state FROM {_RUN}'))
 runs={(org,aid): (run,attempt,epoch,token,expires,state) for org,run,aid,attempt,epoch,token,expires,state in run_rows}
 if len(run_rows)!=len(assignments) or set(runs)!=assignments: raise SqliteReciprocalReviewAssignmentTerminalError('assignment run chain에 orphan 또는 missing run이 있습니다.')
 for (org,aid),(run,attempt,epoch,token,expires,state) in runs.items():
  if run!=f'run:{aid}:1' or attempt!=1 or type(epoch) is not int or epoch<1: raise SqliteReciprocalReviewAssignmentTerminalError('assignment initial run/attempt chain이 canonical하지 않습니다.')
  if state=='queued':
   legal=epoch==1 and token is None and expires is None
  else:
   legal=token is not None and expires is not None
   if legal:
    try: legal=isinstance(expires,str) and _time(datetime.fromisoformat(expires.replace('Z','+00:00')))==expires
    except (TypeError, ValueError): legal=False
  if not legal: raise SqliteReciprocalReviewAssignmentTerminalError('assignment run epoch/state가 canonical하지 않습니다.')
 receipts=set(c.execute(f'SELECT org_id,receipt_id FROM {_RECEIPT}'))
 if any(set(c.execute(q)) != receipts for q in (f'SELECT org_id,receipt_id FROM {_RESULT}',f'SELECT org_id,receipt_id FROM {_AUDIT}',f'SELECT org_id,receipt_id FROM {_OUTBOX}')): raise SqliteReciprocalReviewAssignmentTerminalError('v4 terminal evidence graph가 bijection이 아닙니다.')
 for org,receipt,audit,outbox,digest,cycle,req,aid,run,epoch,subject,content,rubric,input_digest,created in c.execute(f'SELECT org_id,receipt_id,audit_id,outbox_id,command_digest,cycle_id,requirement_id,assignment_id,assignment_run_id,lease_epoch,subject_id,content_digest,rubric_digest,input_digest,created_at FROM {_RECEIPT}'):
  result=c.execute(f'SELECT cycle_id,assignment_id,command_digest,created_at FROM {_RESULT} WHERE org_id=? AND receipt_id=?',(org,receipt)).fetchone()
  arow=c.execute(f'SELECT cycle_id,requirement_id,reviewer_ref,content_digest,rubric_digest,input_digest FROM {_ASSIGN} WHERE org_id=? AND assignment_id=?',(org,aid)).fetchone()
  rrow=c.execute(f'SELECT assignment_id,lease_epoch,state FROM {_RUN} WHERE org_id=? AND assignment_run_id=?',(org,run)).fetchone()
  auditrow=c.execute(f'SELECT receipt_id,event_digest,created_at FROM {_AUDIT} WHERE org_id=? AND audit_id=?',(org,audit)).fetchone()
  outboxrow=c.execute(f'SELECT receipt_id,payload_digest,created_at FROM {_OUTBOX} WHERE org_id=? AND outbox_id=?',(org,outbox)).fetchone()
  if result != (cycle,aid,digest,created) or arow != (cycle,req,subject,content,rubric,input_digest) or rrow != (aid,epoch,'recorded') or auditrow != (receipt,_digest(('v4_terminal_audit',digest)),created) or outboxrow != (receipt,_digest(('v4_terminal_outbox',digest)),created): raise SqliteReciprocalReviewAssignmentTerminalError('forged v4 terminal evidence graph가 있습니다.')
 if c.execute('PRAGMA foreign_key_check').fetchone() is not None: raise SqliteReciprocalReviewAssignmentTerminalError('reciprocal review v4 FK가 canonical하지 않습니다.')

def migrate_sqlite_reciprocal_review_assignment_terminal_v4(c: sqlite3.Connection, *, fault_injector: Callable[[str],None]|None=None) -> None:
 try:
  c.execute('BEGIN IMMEDIATE')
  if c.execute(f'SELECT 1 FROM {_MANIFEST} WHERE component_id=?',(V3_COMPONENT_ID,)).fetchone() is None: raise SqliteReciprocalReviewAssignmentTerminalError('v4에는 explicit v3 snapshot이 필요합니다.')
  if c.execute(f'SELECT 1 FROM {_MANIFEST} WHERE component_id=?',(COMPONENT_ID,)).fetchone() is None:
   for n,s in _DDLS.items():
    c.execute(s)
    if fault_injector is not None: fault_injector(f'after_{n}')
   for s in _TRIGGERS.values(): c.execute(s)
   manifest=_canonical({'component_id':COMPONENT_ID,'schema_version':4,'catalog':_catalog()}); c.execute(f'INSERT INTO {_MANIFEST} VALUES(?,?,?,?)',(COMPONENT_ID,4,manifest,hashlib.sha256(manifest.encode()).hexdigest()))
  validate_sqlite_reciprocal_review_assignment_terminal(c); c.commit()
 except Exception:
  if c.in_transaction: c.rollback()
  raise

def provision_sqlite_reciprocal_review_v4_cycle(c: sqlite3.Connection, *, org_id:str, cycle_id:str, assignments: Sequence[tuple[str, tuple[str,...], str, str, str]] = ()) -> None:
 """Registration-only provision. tuple=(requirement, reviewers, rubric_digest, input_digest, content_digest)."""
 if c.execute(f'SELECT 1 FROM {_MANIFEST} WHERE component_id=?',(COMPONENT_ID,)).fetchone() is None: return
 if c.execute(f'SELECT 1 FROM {_CYCLE} WHERE org_id=? AND cycle_id=?',(org_id,cycle_id)).fetchone() is not None: return
 c.execute('SAVEPOINT v4_provision')
 try:
  parent=c.execute('SELECT revision_id,cycle_no,state_kind,active,provenance_digest,policy_digest,created_at,cycle_revision FROM durable_reciprocal_review_cycles_v3 WHERE org_id=? AND cycle_id=?',(org_id,cycle_id)).fetchone()
  if parent is None: raise SqliteReciprocalReviewAssignmentTerminalError('v4 mirror source cycle이 없습니다.')
  provenance_kind=c.execute('SELECT provenance_kind FROM durable_reciprocal_review_artifact_revisions WHERE org_id=? AND revision_id=?',(org_id,parent[0])).fetchone()
  if provenance_kind is None or provenance_kind[0] not in {'ai','mixed'}: raise SqliteReciprocalReviewAssignmentTerminalError('v4 assignment mirror는 ai/mixed provenance에만 허용됩니다.')
  if not assignments: raise SqliteReciprocalReviewAssignmentTerminalError('v4 cycle에는 explicit human assignments가 필요합니다.')
  c.execute(f'INSERT INTO {_CYCLE} VALUES(?,?,?,?,?,?,?,?,?,?)',(org_id,cycle_id,*parent)); c.execute(f'INSERT INTO {_OWNERSHIP} VALUES(?,?,?,?)',(org_id,cycle_id,'v4',parent[6]))
  contributors=tuple(x[0] for x in c.execute("SELECT principal_ref FROM durable_reciprocal_review_provenance_events WHERE org_id=? AND revision_id=? AND principal_kind='human' ORDER BY principal_ref",(org_id,parent[0])))
  for requirement,reviewers,rubric,input_digest,content in assignments:
   req=c.execute('SELECT completion_rule,required_count FROM durable_reciprocal_review_requirements WHERE org_id=? AND requirement_id=? AND cycle_id=? AND reviewer_kind=\'human\'',(org_id,requirement,cycle_id)).fetchone()
   if req is None or len(set(reviewers)) != len(reviewers) or any(x in contributors for x in reviewers): raise SqliteReciprocalReviewAssignmentTerminalError('v4 reviewer assignment이 independent하지 않습니다.')
   n=len(reviewers); rule,k=req
   if n<1 or (rule=='all' and k!=n) or (rule=='any' and k!=1) or (rule=='quorum' and not 1<=k<=n): raise SqliteReciprocalReviewAssignmentTerminalError('v4 completion policy가 assignment count와 일치하지 않습니다.')
   for ordinal,reviewer in enumerate(reviewers,1):
    aid=f'assignment:{cycle_id}:{requirement}:{ordinal}'; digest=_digest({'requirement':requirement,'reviewer':reviewer,'ordinal':ordinal,'contributors':contributors,'policy':parent[5],'provenance':parent[4],'content':content,'rubric':rubric,'input':input_digest})
    c.execute(f'INSERT INTO {_ASSIGN} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(org_id,aid,cycle_id,requirement,reviewer,ordinal,digest,_digest(contributors),parent[5],parent[4],content,rubric,input_digest,parent[6]))
    c.execute(f'INSERT INTO {_RUN} VALUES(?,?,?,?,?,?,?,?)',(org_id,f'run:{aid}:1',aid,1,1,None,None,'queued'))
  c.execute('RELEASE v4_provision')
 except Exception:
  c.execute('ROLLBACK TO v4_provision'); c.execute('RELEASE v4_provision')
  raise

class AssignmentReviewLease:
 def __init__(self,path: str|Path, clock: Callable[[],datetime], key:bytes): self.path,self.clock,self.key=Path(path),clock,key
 def claim(self, *, org_id:str, assignment_run_id:str, principal:HumanPrincipal, lease_for:timedelta) -> tuple[int,str,datetime]:
  if lease_for <= timedelta(0): raise SqliteReciprocalReviewAssignmentTerminalError('assignment lease duration은 양수여야 합니다.')
  c=sqlite3.connect(self.path,timeout=30,isolation_level=None)
  try:
   c.execute('BEGIN IMMEDIATE'); validate_sqlite_reciprocal_review_assignment_terminal(c); now=sqlite_db_now(c)
   row=c.execute(f'SELECT a.reviewer_ref,r.lease_epoch,r.state FROM {_RUN} r JOIN {_ASSIGN} a ON a.org_id=r.org_id AND a.assignment_id=r.assignment_id WHERE r.org_id=? AND r.assignment_run_id=?',(org_id,assignment_run_id)).fetchone()
   if row is None or row[0]!=principal.subject_id or row[2] not in {'queued','expired'}: raise SqliteReciprocalReviewAssignmentTerminalConflict('assignment lease가 stale입니다.')
   token=secrets.token_urlsafe(32); exp=now+lease_for; epoch=row[1]+(1 if row[2]=='expired' else 0); changed=c.execute(f"UPDATE {_RUN} SET lease_epoch=?,token_hash=?,expires_at=?,state='leased' WHERE org_id=? AND assignment_run_id=? AND state=?",(epoch,hmac.new(self.key,token.encode(),hashlib.sha256).hexdigest(),_time(exp),org_id,assignment_run_id,row[2])).rowcount
   if changed!=1: raise SqliteReciprocalReviewAssignmentTerminalConflict('assignment lease CAS가 stale입니다.')
   c.commit(); return epoch,token,exp
  except Exception:
   if c.in_transaction:c.rollback()
   raise
  finally:c.close()
 def renew(self, *, org_id:str, assignment_run_id:str, principal:HumanPrincipal, lease_epoch:int, lease_token:str, lease_for:timedelta) -> tuple[int,str,datetime]:
  if lease_for <= timedelta(0): raise SqliteReciprocalReviewAssignmentTerminalError('assignment lease duration은 양수여야 합니다.')
  c=sqlite3.connect(self.path,timeout=30,isolation_level=None)
  try:
   c.execute('BEGIN IMMEDIATE'); validate_sqlite_reciprocal_review_assignment_terminal(c); now=sqlite_db_now(c); now_s=_time(now)
   row=c.execute(f'SELECT a.reviewer_ref,r.state,r.lease_epoch,r.token_hash,r.expires_at FROM {_RUN} r JOIN {_ASSIGN} a ON a.org_id=r.org_id AND a.assignment_id=r.assignment_id WHERE r.org_id=? AND r.assignment_run_id=?',(org_id,assignment_run_id)).fetchone()
   if row is None or row[0]!=principal.subject_id or row[1]!='leased' or row[2]!=lease_epoch or row[4]<=now_s or row[3]!=hmac.new(self.key,lease_token.encode(),hashlib.sha256).hexdigest(): raise SqliteReciprocalReviewAssignmentTerminalConflict('assignment renew가 stale입니다.')
   token=secrets.token_urlsafe(32); exp=now+lease_for
   if c.execute(f"UPDATE {_RUN} SET token_hash=?,expires_at=? WHERE org_id=? AND assignment_run_id=? AND state='leased' AND lease_epoch=? AND token_hash=?",(hmac.new(self.key,token.encode(),hashlib.sha256).hexdigest(),_time(exp),org_id,assignment_run_id,lease_epoch,row[3])).rowcount!=1: raise SqliteReciprocalReviewAssignmentTerminalConflict('assignment renew CAS가 stale입니다.')
   c.commit(); return lease_epoch,token,exp
  except Exception:
   if c.in_transaction:c.rollback()
   raise
  finally:c.close()
 def reclaim(self, *, org_id:str, assignment_run_id:str, principal:HumanPrincipal, lease_for:timedelta) -> tuple[int,str,datetime]:
  if lease_for <= timedelta(0): raise SqliteReciprocalReviewAssignmentTerminalError('assignment lease duration은 양수여야 합니다.')
  c=sqlite3.connect(self.path,timeout=30,isolation_level=None)
  try:
   c.execute('BEGIN IMMEDIATE'); validate_sqlite_reciprocal_review_assignment_terminal(c); now=sqlite_db_now(c); now_s=_time(now)
   row=c.execute(f'SELECT a.reviewer_ref,r.state,r.lease_epoch,r.expires_at FROM {_RUN} r JOIN {_ASSIGN} a ON a.org_id=r.org_id AND a.assignment_id=r.assignment_id WHERE r.org_id=? AND r.assignment_run_id=?',(org_id,assignment_run_id)).fetchone()
   if row is None or row[0]!=principal.subject_id or row[1]!='leased' or row[3]>now_s: raise SqliteReciprocalReviewAssignmentTerminalConflict('assignment reclaim가 stale입니다.')
   token=secrets.token_urlsafe(32); exp=now+lease_for; epoch=row[2]+1
   if c.execute(f"UPDATE {_RUN} SET lease_epoch=?,token_hash=?,expires_at=?,state='leased' WHERE org_id=? AND assignment_run_id=? AND state='leased' AND lease_epoch=? AND expires_at=?",(epoch,hmac.new(self.key,token.encode(),hashlib.sha256).hexdigest(),_time(exp),org_id,assignment_run_id,row[2],row[3])).rowcount!=1: raise SqliteReciprocalReviewAssignmentTerminalConflict('assignment reclaim CAS가 stale입니다.')
   c.commit(); return epoch,token,exp
  except Exception:
   if c.in_transaction:c.rollback()
   raise
  finally:c.close()

class AssignmentHumanTerminalUnitOfWork:
 def __init__(self,path:str|Path, keys:Mapping[str,bytes], lease_key:bytes, clock:Callable[[],datetime], fault:Callable[[str],None]|None=None): self.path,self.keys,self.lease_key,self.clock,self.fault=Path(path),dict(keys),lease_key,clock,fault
 def record(self, *, principal:HumanPrincipal, receipt_id:str,audit_id:str,outbox_id:str,idempotency_key:str,assignment_run_id:str,lease_epoch:int,lease_token:str,conclusion:HumanReviewConclusion) -> tuple[str,int]:
  c=sqlite3.connect(self.path,timeout=30,isolation_level=None)
  try:
   c.execute('BEGIN IMMEDIATE'); validate_sqlite_reciprocal_review_assignment_terminal(c); now=sqlite_db_now(c); now_s=_time(now)
   row=c.execute(f'SELECT a.cycle_id,a.requirement_id,a.assignment_id,a.reviewer_ref,a.assignment_digest,a.contributor_digest,a.policy_digest,a.provenance_digest,a.content_digest,a.rubric_digest,a.input_digest,r.lease_epoch,r.token_hash,r.expires_at,r.state,v.revision_id,v.state_kind,v.cycle_revision,d.provenance_kind FROM {_RUN} r JOIN {_ASSIGN} a ON a.org_id=r.org_id AND a.assignment_id=r.assignment_id JOIN {_CYCLE} v ON v.org_id=a.org_id AND v.cycle_id=a.cycle_id JOIN durable_reciprocal_review_artifact_revisions d ON d.org_id=v.org_id AND d.revision_id=v.revision_id WHERE r.org_id=? AND r.assignment_run_id=?',(principal.org_id,assignment_run_id)).fetchone()
   if row is None: raise SqliteReciprocalReviewAssignmentTerminalError('v4 assignment run이 없습니다.')
   cycle,req,aid,reviewer,ad,contributors,policy,prov,content,rubric,input_digest,epoch,token_hash,expires,state,revision,cycle_state,cycle_rev,provenance_kind=row
   semantic={'principal':principal.model_dump(mode='json'),'ids':[receipt_id,audit_id,outbox_id,idempotency_key,assignment_run_id],'assignment':ad,'epoch':lease_epoch,'conclusion':conclusion.model_dump(mode='json')}; digest=_digest(semantic)
   old=c.execute(f'SELECT command_digest FROM {_RECEIPT} WHERE org_id=? AND idempotency_key=?',(principal.org_id,idempotency_key)).fetchone()
   if old is not None:
    if old[0]!=digest: raise SqliteReciprocalReviewAssignmentTerminalConflict('idempotency semantic command가 다릅니다.')
    out=c.execute(f'SELECT cycle_state,cycle_revision FROM {_RESULT} WHERE org_id=? AND receipt_id=?',(principal.org_id,receipt_id)).fetchone(); c.commit(); return out[0],out[1]
   plan_row=c.execute("SELECT completion_rule,required_count FROM durable_reciprocal_review_requirements WHERE org_id=? AND requirement_id=? AND cycle_id=? AND reviewer_kind='human'",(principal.org_id,req,cycle)).fetchone()
   candidates=tuple(x[0] for x in c.execute(f'SELECT reviewer_ref FROM {_ASSIGN} WHERE org_id=? AND cycle_id=? AND requirement_id=? ORDER BY ordinal',(principal.org_id,cycle,req)))
   contributor_refs=tuple(x[0] for x in c.execute("SELECT principal_ref FROM durable_reciprocal_review_provenance_events WHERE org_id=? AND revision_id=? AND principal_kind='human' ORDER BY principal_ref",(principal.org_id,revision)))
   expires_at=principal.authenticated_at+timedelta(minutes=5)
   authority=assignment_human_authority_payload(org_id=principal.org_id,reviewer=principal.subject_id,authenticated_at=principal.authenticated_at,expires_at=expires_at,revision_id=revision,cycle_id=cycle,requirement_id=req,assignment_id=aid,assignment_run_id=assignment_run_id,assignment_digest=ad,policy_digest=policy,provenance_digest=prov,contributor_digest=contributors,content_digest=content,rubric_digest=rubric,input_digest=input_digest,completion_rule=plan_row[0] if plan_row else '',required_count=plan_row[1] if plan_row else 0,candidate_reviewers=candidates,contributors=contributor_refs)
   key=self.keys.get(principal.subject_id)
   if key is None or plan_row is None or provenance_kind not in {'ai','mixed'} or principal.subject_id!=reviewer or not authority['independence_result'] or now>expires_at or now<principal.authenticated_at or not hmac.compare_digest(principal.authn_context_digest,hmac.new(key,_canonical(authority).encode(),hashlib.sha256).hexdigest()) or conclusion.finding_count!=0 or conclusion.content_digest!=content or conclusion.rubric_digest!=rubric or conclusion.input_digest!=input_digest or state!='leased' or epoch!=lease_epoch or expires<=now_s or token_hash!=hmac.new(self.lease_key,lease_token.encode(),hashlib.sha256).hexdigest(): raise SqliteReciprocalReviewAssignmentTerminalError('assignment terminal authority 또는 lease가 stale입니다.')
   if c.execute(f"UPDATE {_RUN} SET state='recorded' WHERE org_id=? AND assignment_run_id=? AND state='leased' AND lease_epoch=?",(principal.org_id,assignment_run_id,epoch)).rowcount!=1: raise SqliteReciprocalReviewAssignmentTerminalConflict('assignment run CAS가 stale입니다.')
   if self.fault: self.fault('after_run_cas')
   reqs=c.execute("SELECT requirement_id,completion_rule,required_count FROM durable_reciprocal_review_requirements WHERE org_id=? AND cycle_id=? AND reviewer_kind='human'",(principal.org_id,cycle)).fetchall()
   complete=bool(reqs) and all(c.execute(f'SELECT count(*) FROM {_RECEIPT} WHERE org_id=? AND cycle_id=? AND requirement_id=?',(principal.org_id,cycle,rid)).fetchone()[0]+(1 if rid==req else 0) >= k for rid,_rule,k in reqs)
   next_state='awaiting_human_disposition' if cycle_state=='review_open' and complete else 'review_open'; next_rev=cycle_rev+(next_state!=cycle_state)
   if next_state!=cycle_state and c.execute(f"UPDATE {_CYCLE} SET state_kind=?,cycle_revision=? WHERE org_id=? AND cycle_id=? AND state_kind='review_open' AND cycle_revision=?",(next_state,next_rev,principal.org_id,cycle,cycle_rev)).rowcount!=1: raise SqliteReciprocalReviewAssignmentTerminalConflict('v4 cycle CAS가 stale입니다.')
   if self.fault: self.fault('after_cycle_cas')
   c.execute(f'INSERT INTO {_RECEIPT} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(principal.org_id,receipt_id,audit_id,outbox_id,idempotency_key,digest,cycle,req,aid,assignment_run_id,epoch,principal.subject_id,content,rubric,input_digest,now_s))
   if self.fault: self.fault('after_receipt')
   c.execute(f'INSERT INTO {_RESULT} VALUES(?,?,?,?,?,?,?,?)',(principal.org_id,receipt_id,cycle,aid,digest,next_rev,next_state,now_s)); c.execute(f'INSERT INTO {_AUDIT} VALUES(?,?,?,?,?)',(principal.org_id,audit_id,receipt_id,_digest(('v4_terminal_audit',digest)),now_s)); c.execute(f'INSERT INTO {_OUTBOX} VALUES(?,?,?,?,?)',(principal.org_id,outbox_id,receipt_id,_digest(('v4_terminal_outbox',digest)),now_s)); validate_sqlite_reciprocal_review_assignment_terminal(c); c.commit(); return next_state,next_rev
  except Exception:
   if c.in_transaction:c.rollback()
   raise
  finally:c.close()

def create_sqlite_reciprocal_review_assignment_lease(path:str|Path, *, clock:Callable[[],datetime], token_key:bytes) -> AssignmentReviewLease:
 if not token_key: raise ValueError('lease token key가 필요합니다.')
 return AssignmentReviewLease(path,clock,token_key)
def create_sqlite_reciprocal_review_assignment_human_terminal_uow(path:str|Path, *, trusted_human_assignment_authority_keys:Mapping[str,bytes], trusted_lease_token_key:bytes, clock:Callable[[],datetime], fault_injector:Callable[[str],None]|None=None) -> AssignmentHumanTerminalUnitOfWork:
 if not trusted_human_assignment_authority_keys or not trusted_lease_token_key: raise ValueError('trusted authority key가 필요합니다.')
 return AssignmentHumanTerminalUnitOfWork(path,trusted_human_assignment_authority_keys,trusted_lease_token_key,clock,fault_injector)
