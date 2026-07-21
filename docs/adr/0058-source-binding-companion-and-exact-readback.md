# ADR 0058 — Source Binding v6 companion과 exact read-back

날짜: 2026-07-20  
상태: Accepted

## 결정

1. source binding lifecycle은 additive `durable_reciprocal_review_source_binding_v6`만 소유한다. v2 human BindingReady와 v5 AI/mixed BindingReady(+v4 upstream)는 immutable source-free evidence로 보존한다.
2. v6은 `Ready -> Pending -> Bound | Superseded` 상태와 immutable intent/receipt/failure/audit/outbox/worker lease graph를 가진다. Pending은 하나의 logical cycle에 하나만 허용한다.
3. `SourceBindingAdapter`가 expected-source-revision CAS, semantic idempotency, worker fencing, stable exact read-back, continuous gateway/native enforcement 및 every-read attestation을 증명하지 못하면 intent/Pending write는 0이다.
4. intent transaction은 upstream BindingReady·current authorization·boundary/declassification·adapter capability를 재검증하고 boundary plan·drift action authorization·Pending/outbox를 원자화한다. external call은 DB lock 밖 worker만 수행한다.
5. Bound는 source apply 응답이 아니라 fenced stable exact read-back, source boundary enforcement receipt, authorization/lease/intent 재검증 뒤에만 확정한다. 불확실한 failure/late mutation은 Bound resurrection 없이 pending settle, superseded evidence, escalation 및 human reconciliation으로 처리한다.
6. proposal/evaluation/promotion/serving state는 v6에서 write 0이다.
