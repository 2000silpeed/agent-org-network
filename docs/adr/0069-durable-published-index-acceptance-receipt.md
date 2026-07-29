# ADR 0069 — Durable Published Index Acceptance Receipt

## 결정

P17.15 O5c는 `SqlitePublishedIndexAcceptance`가 `KnowledgeIndex` 목차 payload와 immutable acceptance receipt를 소유한다. receipt의 command digest·receipt digest·durable schema는 O5b `OwnerPublishCommitted.commit_sha`와 `committed_tree_index_digest`를 semantic key·payload digest와 함께 결박한다. `AuthoringRun` control·audit·outbox에는 receipt ID/digest·count·opaque metadata만 남기고 index 본문·raw source·full draft·OKF·patch를 복제하지 않는다.

초기 acceptance는 current Card Owner·Card snapshot·`author.publish` grant를 transaction 시작과 precommit에 재검증한다. 하나의 `BEGIN IMMEDIATE` transaction이 payload/receipt/latest event와 `Publishing(3) → Published(4)`, terminal receipt/audit/outbox를 함께 확정한다. semantic key는 `(org_id, agent_id, run_id, review_revision=2)`이며 exact replay만 허용한다. stale/equal index와 다른 semantic payload는 receipt·terminal write 0이다.

Card transfer/revoke 뒤 immutable receipt만으로 terminalize하는 historical-proof 예외는 O5d reconciliation에만 허용한다. O5c 최초 acceptance/replay는 current authorization을 우회하지 않는다.

O5d reconciliation은 exact historical receipt graph가 이미 durable한 `Publishing(3)`의 control projection만 `Published(4)`로 CAS할 수 있다. historical graph를 읽기 전에 acceptance의 canonical SQLite contract(세 table의 column/PK/UNIQUE/FK, immutable update/delete trigger, `production_authoring_runs`의 Published binding trigger)를 validate-only로 exact 대조한다. schema drift는 graph read/CAS보다 먼저 repair 없이 unavailable·write 0이다. 이 경로는 payload·acceptance receipt·latest event·control receipt·audit/outbox·grant를 새로 만들지 않으며 current Owner/Card/`author.publish`를 호출하지 않는다. graph가 하나라도 누락·변조되면 repair하지 않고 unavailable으로 닫는다.

## 결과

기존 in-memory `PublishedIndexStore`는 durable receipt·semantic binding·shared transaction이 없어 O5 terminal proof로 쓰지 않는다. distributed exactly-once, response-loss reconciliation, transport는 O5d 이후 책임이다.
