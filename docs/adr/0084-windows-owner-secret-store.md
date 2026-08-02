# ADR 0084 — Windows Owner secret bundle은 current-user DPAPI로 보호한다

- 상태: Accepted (RB3.3a key-storage slice; pair/redeem orchestration 미완료)
- 기준일: 2026-08-02
- 관련: ADR 0068, ADR 0082, ADR 0083

## 맥락

회사 기본 실행 환경은 Docker 없는 Windows다. 기존 `ProductionOwnerDeviceKeyStore`는
macOS/SecretService keyring만 허용하고 Windows를 즉시 unavailable로 닫아, 암호화된 Owner
credential/device bundle을 저장할 수 없었다. 평문 JSON/profile fallback은 credential·private key
경계를 깨므로 허용하지 않는다.

## 결정

1. Windows native에서는 Windows Data Protection API(DPAPI)의 current-user scope로 Owner
   secret bundle을 암호화한다. 디스크에는 DPAPI ciphertext의 bounded base64 transport만 둔다.
2. bundle 파일은 profile별 lock 파일과 atomic temp-write/replace를 사용한다. plaintext
   credential, private key, pairing code를 파일·SQLite·stdout에 저장하지 않는다.
3. macOS/Linux는 기존 명시적 native keyring allowlist를 유지한다. keyring backend override,
   미검증 backend, DPAPI 실패 시 임의 파일 fallback 없이 unavailable로 닫는다.
4. DPAPI 저장은 pairing/redeem의 Central receipt·generation·recovery 검증을 대체하지 않는다.
   실제 pair/redeem orchestration과 Windows manual acceptance는 RB3.3a 후속 gate다.

## 검증

Windows backend 선택이 keyring import 없이 이루어지는 결정론 테스트와 기존 bundle/CAS·secret
redaction 회귀를 통과한다. 실제 Windows DPAPI와 clean-install pair/redeem은 Manual Acceptance로
분리한다.
