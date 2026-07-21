# ADR 0054 — Reciprocal Review ledger v2와 사람 disposition 경계

날짜: 2026-07-20  
상태: Accepted

## 맥락

Phase 18 S1b.3까지의 strict SQLite ledger v1은 immutable revision·provenance·review requirement, AI reviewer lease와 signed AI advisory batch를 기록한다. v1의 cycle trigger는 `ReviewOpen -> AwaitingHumanDisposition`만 허용한다. 따라서 사람의 `BindingReady` intent를 추가하면서 v1 trigger를 제자리에서 바꾸면 v1 exact-catalog capability와 이미 검증된 UoW의 strict schema 계약을 깨뜨린다.

AI advisory batch는 사람이 만든 revision의 AI requirement만 terminalize한다. AI·mixed revision에 필요한 사람 review-run의 terminal evidence와 source binding read-back은 아직 없다. 이 상태에서 사람 처분을 source action, binding, proposal, evaluation 또는 promotion으로 확대하면 검증되지 않은 권한을 열게 된다.

## 결정

1. S1b.4는 parent ledger의 **명시적 `durable_reciprocal_review_ledger_v2` evolution**으로 진행한다. v2는 monotonic `cycle_revision`과 strict legal transition을 도입하며 v1 DDL/trigger를 조용히 수정하지 않는다. 모든 S1 UoW는 v2 capability를 exact manifest/catalog/row semantic validation으로 확인한다.
2. S1b.4의 유일한 새 write는 인증된 사람의 immutable `HumanDispositionReceipt`와 `AwaitingHumanDisposition -> BindingReady` CAS다. action은 `ApproveRevision | RequestChanges | RejectRevision`이며 source aggregate나 source command를 쓰지 않는다.
3. 입력의 `HumanPrincipal`은 권한 증거가 아니다. 중앙 authority가 org·revision·cycle·action·policy·provenance·independence를 결박한 opaque `HumanDispositionAuthorization`을 issue/verify하고 write 직전에 재검증한다. SQLite adapter의 public API는 authority·verifier·capability injection을 제공하지 않는다. trusted composition root만 public factory에 validated HMAC key registry를 공급하고, factory는 이를 복사해 private concrete UoW와 verifier를 만든다. 같은 trusted Python process의 private-symbol import·monkeypatch를 보안 sandbox 목표로 주장하지 않으며, 지원 API와 deployment secret ownership 경계가 이 결정의 범위다.
4. 이 첫 slice는 **human provenance**만 허용한다. 모든 required AI requirement가 verified signed terminal batch로 완료되고 finding이 0개여야 한다. waiver 속성만으로 requirement를 건너뛰지 않으며 finding disposition/closure가 구현되기 전에는 nonempty finding batch의 action은 write 0이다. AI·mixed·unknown provenance는 사람 review-run terminal evidence가 생길 때까지 write 0이다.
5. receipt/result/audit/outbox와 v2 cycle CAS는 same `BEGIN IMMEDIATE` transaction에서 body-free로 기록한다. receipt/result/audit/outbox는 immutable이고 idempotent replay는 current authority와 eligibility를 다시 통과해야 한다.
6. S1b.4는 `BindingPending`, `Bound`, source binding, `StageReview`, improvement proposal, evaluation, promotion 또는 serving state를 만들거나 수정하지 않는다.

## 결과

- v1 strict capability와 이미 승인된 S1b.1~S1b.3의 의미를 보존한다.
- 사람의 binding intent에 ABA-safe expected cycle revision과 immutable evidence를 제공한다.
- AI·mixed의 사람 검토와 source 적용은 의도적으로 후속 slice에 남는다. 이 제약은 기능 결손이 아니라, terminal evidence가 없는 경로를 fail-closed로 유지하는 안전 경계다.
