# ADR 0057 — AI·mixed Human Disposition v5 companion

날짜: 2026-07-20  
상태: Accepted

## 맥락

v4는 AI·mixed revision의 assignment-scoped, finding-free human terminal evidence와 `ReviewOpen -> AwaitingHumanDisposition`을 소유한다. v4 catalog/trigger를 disposition까지 확장하면 ADR 0056의 strict capability 계약을 깨뜨린다. AI/mixed는 AI signed evidence와 모든 required human evidence가 모두 충족된 뒤에만 사람이 BindingReady action을 남길 수 있어야 한다.

## 결정

1. AI/mixed disposition은 explicit `durable_reciprocal_review_ledger_v5` companion이 소유한다. v4는 immutable upstream evidence/state로 보존하며 v5는 `BindingReady` result/state와 receipt/result/audit/outbox만 기록한다.
2. 대상은 v4 ownership이 있고 `ai|mixed` provenance인 cycle뿐이다. human provenance는 v2/v3 lane, unknown·legacy/v4 없는 cycle은 write 0이며 backfill/추정은 없다.
3. command의 expected upstream revision은 v4 Awaiting revision과 같아야 하고 v5 result revision은 +1이다. ownership/state/receipt graph는 exact 1:1이고 하나의 cycle/action winner만 허용한다.
4. eligibility는 DB transaction에서 모든 immutable AI/human requirement를 재구성한다. AI는 verified signed finding-free terminal batch threshold, human은 v4 distinct assignment finding-free terminal threshold를 모두 충족해야 한다. finding/waiver/unknown/missing/malformed evidence는 deny다.
5. central disposition authority는 DB-time issued/expiry, org/principal/action/revision/cycle/policy/provenance/v4 snapshot, AI/human/overall eligibility digest와 disposition independence를 결박하고 시작·replay·write 직전에 재검증한다.
6. S1b.5b는 BindingReady까지만 쓴다. BindingPending/Bound/source binding, finding closure, proposal/evaluation/promotion/serving write는 0이다.

## 결과

AI·mixed의 사람 disposition은 실제 검토 evidence를 재검증한 immutable source-free intent가 된다. v4 evidence 계약과 human-provenance lane을 변경하지 않는다.
