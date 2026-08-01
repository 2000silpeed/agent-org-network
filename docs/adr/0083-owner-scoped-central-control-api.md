# ADR 0083 — Owner-scoped Central control API를 별도 versioned boundary로 둔다

- 상태: Accepted (RB3.3b contract foundation; route/composition acceptance 미완료)
- 기준일: 2026-08-02
- 관련: ADR 0050, ADR 0074, ADR 0075, ADR 0081

## 맥락

Card Owner Installation은 자신의 Card에 대한 AnswerRecord supervision, Presence, correction과
self scorecard가 필요하다. 기존 Browser Question route는 requester-owned Question만 반환하고,
`web.py`의 `/supervision/*`·`CorrectionService`는 legacy in-process DB와 composition에 묶여 있어
Owner API가 재사용할 수 없다. Central admin scorecard와 Card Owner self scorecard도 권한·projection
범위가 다르다.

## 결정

1. Owner API 전용 namespace를 versioned private `/v1/owner/*`로 둔다. `/v1/admin/*`, Browser
   `/v1/questions/*`, legacy `/supervision/*`를 Owner API contract로 재사용하지 않는다.
2. 매 read/write는 paired Owner Installation binding에서 유도한 `org_id`, `owner_user_id`,
   `agent_card_id`, Card revision/digest, assignment generation과 credential generation을
   검증한 뒤 Central current Assignment/Authority를 다시 읽는다. owner/card/role/organization을
   body·query에서 자기보고할 수 없다.
3. 최소 surface는 다음 다섯 가지다.

   - `GET /v1/owner/answers`
   - `GET /v1/owner/presence`
   - `GET /v1/owner/scorecard`
   - `GET /v1/owner/answers/{record_id}/corrections`
   - `POST /v1/owner/corrections`

4. correction write는 D2 durable `BackupReviewDispositionApplication`의 `correct` 경로만
   호출한다. DTO는 `review_id`, `expected_revision`, `corrected_text`, `rationale`,
   `idempotency_key`로 고정하며, AnswerRecord 직접 수정·Answer submit·transfer/revoke/publish는
   이 API에서 금지한다. corrected text와 rationale은 bounded input이고 raw source/full draft/
   credential/remote payload는 받지 않는다.
5. Presence와 scorecard source capability가 조립되지 않았거나 Assignment generation이 stale하면
   503으로 닫는다. 부분 값이나 process-local 추정값으로 성공 응답을 만들지 않는다. 다른 Owner/Card,
   foreign org, revoked binding은 403 또는 hidden 404로 평탄화한다.
6. `OwnerControlBinding`, Card revision/digest까지 포함한 strict safe projections와
   `OwnerControlAuthPort`/read/write ports는
   Central composition에서 주입한다. 기본 composition은 capability를 주입하지 않으므로
   unavailable이며, deterministic Fake만 Contract test에서 성공 경로를 제공한다.

## 금지 경계

- Central raw evidence relay, Owner credential/OOB secret, caller-chosen upstream와 A2A proxy 없음
- legacy `server.py`/`web.py`/demo session/Fake Runtime import 없음
- Owner API는 Central DB/migration/Authority policy를 직접 소유하지 않음
- Central admin organization scorecard를 Card Owner self scorecard로 재사용하지 않음

## 검증

Contract foundation은 frozen binding/strict DTO, self-only filter, stale generation, cross-owner
403, unavailable fail-close, correction idempotency key 보존·receipt binding·pre-commit write 0과
secret/raw-field non-egress를 결정론 Fake로 검증한다. D2 durable adapter가 current
Assignment/Authority CAS와 replay/conflict receipt를 추가로 증명해야 하며, 실제 Owner pair/redeem,
OS keychain, Central route wiring, separate process와 A2A egress는 RB3.3a/RB3.4/RB3.5/RB3.7의
후속 gate다.
