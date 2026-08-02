# ADR 0085 — Owner pair/redeem 재시작은 recovery snapshot과 CAS를 기준으로 한다

- 상태: Accepted (RB3.3a recovery boundary; Central pair/redeem orchestration 미완료)
- 기준일: 2026-08-02
- 관련: ADR 0067, ADR 0068, ADR 0083, ADR 0084

## 맥락

Owner 설치의 device-key bundle 저장과 local SQLite recovery row는 서로 다른 durable 경계다.
네트워크 호출 또는 프로세스 종료가 두 경계 사이에서 발생하면, 메모리에 있던 `updated_at`,
redeem idempotency, issue receipt anchor를 잃고 중복 credential write나 근거 없는 paired
상태를 만들 위험이 있다. pairing code/private key를 재구성하거나 digest를 추정하는 방식은
허용할 수 없다.

## 결정

1. `OwnerPairingRecoveryStore.read_snapshot(profile_id)`를 유일한 재시작 read 경계로 둔다.
   snapshot은 `intent_issued`, `redeem_submitted`, `credential_stored` 상태와 binding/intent/
   issue receipt anchor, redeem command projection, credential/bundle public projection 및
   `created_at`/`updated_at`을 strict immutable DTO로 반환한다.
2. 후속 orchestration은 snapshot의 `state`와 `updated_at`을 CAS expected 값으로 사용한다.
   row 부재, schema/receipt 검증 실패, digest 불일치는 unavailable로 닫으며 paired/ready를
   추측하지 않는다.
3. snapshot은 secret material, pairing code, private key를 반환하지 않는다. `finalize`가
   recovery row를 terminal profile로 이동한 뒤 snapshot이 `None`인 것은 정상이며, terminal
   profile read는 별도 경계로 둔다.
4. Central issue/redeem 응답은 persisted receipt 행에서 계산한 immutable receipt id/digest와
   pairing-intent digest를 반환한다. 다만 이 anchor가 Owner bundle/recovery CAS와 교차 검증되기
   전까지는 이 경계를 pair/redeem 완료로 해석하지 않는다.

## 검증

재시작을 모사하는 focused tests가 snapshot의 state/`updated_at` CAS 재개, malformed profile
거부, 누락 row의 `None`을 검증한다. Central HTTP/strict verifier의 receipt anchor 전달도
검증하지만, cross-store crash reconciliation과 실제 Owner orchestration은 후속 RB3.3a slice다.
