"""ADR 0060 S1c.2 Pending worker: lease/attempt evidence only; Bound is write-zero."""
from __future__ import annotations
import hashlib
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

COMPONENT_ID="durable_reciprocal_review_source_binding_worker_v7"
_M="schema_component_manifests"; _L="source_binding_v7_worker_leases"; _A="source_binding_v7_apply_attempts"; _R="source_binding_v7_readbacks"
_DDL={
 _L:f"CREATE TABLE {_L} (org_id TEXT NOT NULL,intent_id TEXT NOT NULL,lease_epoch INTEGER NOT NULL,worker_id TEXT NOT NULL,token_hash TEXT NOT NULL,expires_at TEXT NOT NULL,PRIMARY KEY(org_id,intent_id))",
 _A:f"CREATE TABLE {_A} (org_id TEXT NOT NULL,intent_id TEXT NOT NULL,attempt_no INTEGER NOT NULL,lease_epoch INTEGER NOT NULL,operation_digest TEXT NOT NULL,classification TEXT NOT NULL CHECK(classification IN ('dispatched','uncertain','observed')),created_at TEXT NOT NULL,PRIMARY KEY(org_id,intent_id,attempt_no))",
 _R:f"CREATE TABLE {_R} (org_id TEXT NOT NULL,intent_id TEXT NOT NULL,attempt_no INTEGER NOT NULL,observation_digest TEXT NOT NULL,classification TEXT NOT NULL CHECK(classification IN ('uncertain','observed')),created_at TEXT NOT NULL,PRIMARY KEY(org_id,intent_id,attempt_no),FOREIGN KEY(org_id,intent_id,attempt_no) REFERENCES {_A}(org_id,intent_id,attempt_no))",
}
_T={f"{t}_no_{v}":f"CREATE TRIGGER {t}_no_{v} BEFORE {v.upper()} ON {t} BEGIN SELECT RAISE(ABORT,'immutable source-binding worker v7'); END" for t in (_A,_R) for v in ('update','delete')}
class SourceBindingWorkerError(RuntimeError):pass
class SourceBindingExecutor(Protocol):
 def apply(self, request: object, lease_fence: int) -> object: ...
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
class PendingWorker:
 def __init__(self,path:str|Path,executor:SourceBindingExecutor)->None:self.path=Path(path);self.executor=executor
 def claim(self,*,org_id:str,intent_id:str,worker_id:str,lease_for:timedelta=timedelta(minutes=1))->tuple[int,str]:
  c=sqlite3.connect(self.path,isolation_level=None)
  try:
   c.execute('BEGIN IMMEDIATE');now=_now(c);row=c.execute(f"SELECT lease_epoch,expires_at FROM {_L} WHERE org_id=? AND intent_id=?",(org_id,intent_id)).fetchone()
   if row and datetime.fromisoformat(row[1].replace('Z','+00:00'))>now:raise SourceBindingWorkerError('lease unavailable')
   epoch=(row[0]+1 if row else 1);token=secrets.token_urlsafe(32);h=_dig(token);c.execute(f"INSERT INTO {_L} VALUES(?,?,?,?,?,?) ON CONFLICT(org_id,intent_id) DO UPDATE SET lease_epoch=excluded.lease_epoch,worker_id=excluded.worker_id,token_hash=excluded.token_hash,expires_at=excluded.expires_at",(org_id,intent_id,epoch,worker_id,h,_time(now+lease_for)));c.commit();return epoch,token
  except Exception:
   if c.in_transaction:c.rollback()
   raise
  finally:c.close()
