# ADR 0055 — AI·mixed revision의 사람 Review Run terminal evidence

날짜: 2026-07-20  
상태: Accepted

## 맥락

ADR 0054의 S1b.4는 사람이 만든 revision만 `AwaitingHumanDisposition -> BindingReady`로 전이한다. AI·mixed revision에는 독립된 사람이 실제 검토를 완료했다는 durable Review Run terminal evidence가 아직 없으므로, 해당 provenance에 사람 disposition을 허용하면 검토 기록 없이 binding intent가 생길 수 있다.

또한 v2 ledger trigger와 strict catalog capability를 제자리에서 확장하면 이미 승인된 v1/v2 UoW의 manifest 계약을 깨뜨린다.

## 결정

1. AI·mixed revision의 첫 확장은 **S1b.5a finding-free Human Review Run terminal evidence**로 한정한다. 이 UoW는 `BindingReady`나 source action을 만들지 않는다.
2. `RecordHumanReviewTerminal`은 server-authenticated `HumanPrincipal`, full lease token/epoch, content·rubric·input digest와 finding count 0의 conclusion을 받아, body-free immutable receipt/result/audit/outbox와 `leased -> recorded` run CAS를 하나의 authoritative DB-time transaction에 기록한다.
3. 중앙 `HumanReviewRunAuthorization`은 org·reviewer·revision·cycle·requirement·run·assignment·policy·provenance·contributor-set·independence·content/rubric/input digest와 expiry를 결박한다. 모든 human contributor와 reviewer가 같거나 현재 independence 검증이 실패하면 write 0이다. unknown provenance도 write 0이다.
4. 모든 required human requirement가 verified finding-free terminal evidence로 충족될 때만 같은 transaction에서 `ReviewOpen -> AwaitingHumanDisposition`을 전이한다. finding이 하나라도 있으면 이 slice의 terminal write는 0이다. finding/closure는 후속 slice가 소유한다.
5. v1/v2를 조용히 바꾸지 않고 explicit `durable_reciprocal_review_ledger_v3` migration을 사용한다. v3는 cycle revision을 계승하고 legal transition을 strict manifest/catalog/semantic validation으로 검증한다. v3 cutover 후 v2 disposition writer는 fail-closed하며, registration path는 새 cycle의 v3 mirror를 명시적으로 provision해야 한다.
6. S1b.5b는 별 UoW로 AI/mixed provenance의 verified human terminal evidence와 required AI evidence를 재검증한 뒤에만 `BindingReady`를 쓴다. `BindingPending`, `Bound`, source binding, proposal, evaluation, promotion, rollback, serving state는 S1b.5a/b에서 모두 write 0이다.

## 결과

- “AI가 만든 것은 사람이 검토한다”를 immutable review evidence로 구현하면서, source action·finding closure·promotion 권한을 미리 열지 않는다.
- v2의 strict catalog 계약을 보존하고, cutover/dual-writer split brain을 fail-closed로 처리한다.
- finding-bearing 인간 검토와 source binding은 더 넓은 도메인·권한·장애 모델이 준비된 후속 단계에 남는다.
