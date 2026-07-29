# ADR 0068 — Owner Installation Credential Envelope v1은 X25519·HKDF-SHA256·AES-256-GCM으로 봉인한다

- 상태: **채택(Accepted, 2026-07-28)**
- 날짜: 2026-07-28
- 계보: ADR 0050(중앙 Authority/RBAC)·ADR 0052(durable credential)·ADR 0067(세 설치 경계와 pairing)을 잇는다.
- 적용 범위: Card Owner Installation pairing이 전달하는 credential envelope의 암호 suite, wire encoding, credential 수명·재전달·회전, client 복호화와 저장.
- 제외 범위: pairing intent 발급·redeem의 사람 권한 판정, session revoke 의미, Agent Card 소유권 판정, credential을 사용하는 개별 command의 중앙 재인가.

## 맥락

[ADR 0067](0067-three-install-product-boundaries-and-pairing.md)은 Card Owner Installation이 device public key를 중앙에 제시하고, 중앙이 raw secret을 저장하지 않은 채 device에 결박된 credential을 전달하도록 정했다. 그러나 공개키 알고리즘, 직렬화, key derivation, authenticated metadata와 암호문 형식은 정하지 않았다.

이 값을 구현마다 임의로 고르면 Python CLI·브라우저·다른 언어 client가 서로 복호화하지 못하고, 이미 배포된 credential envelope를 해석하기 위해 위험한 추정이나 downgrade가 필요해진다. wire crypto는 배포 뒤 되돌리기 어려운 호환성 결정이므로 v1을 별도 ADR로 봉인한다.

pairing issue/redeem의 권한은 이 ADR이 만들지 않는다. 중앙은 [ADR 0050](0050-central-authority-rbac-and-org-isolation.md)과 ADR 0067에 따라 현재 session, Registry User, Agent Card 소유권과 `author.write`를 transaction 안에서 다시 판정한다. 이 ADR은 그 판정이 성공한 뒤 credential을 특정 device에 안전하게 전달하는 형식만 정한다.

## 결정 1 — v1 암호 suite

v1의 suite 식별자는 다음 exact ASCII 문자열이다.

```text
AON-OWNER-PAIR-X25519-HKDF-SHA256-A256GCM-v1
```

구성은 다음과 같다.

- key agreement: X25519
- KDF: HKDF-SHA256
- content encryption: AES-256-GCM
- authenticated metadata serialization: RFC 8785 JSON Canonicalization Scheme(JCS)의 UTF-8 bytes
- binary encoding: RFC 4648 base64url, padding 없음

v1 안에서 알고리즘 협상·fallback·downgrade를 허용하지 않는다. 새 알고리즘은 새 suite와 새 envelope version으로만 도입한다. 지원하지 않는 runtime은 다른 suite를 조용히 선택하지 않고 capability unavailable로 닫는다. 특히 X25519를 지원하지 않는 브라우저는 production에서 임의 JavaScript crypto fallback을 내려받지 않고, 지원 runtime 또는 Owner CLI/desktop pairing을 사용한다.

## 결정 2 — device public key와 thumbprint

Owner Installation은 device마다 X25519 keypair를 생성한다. public key의 wire 형식은 RFC 7517 OKP JWK의 다음 세 필드만 허용한다.

```json
{
  "crv": "X25519",
  "kty": "OKP",
  "x": "<32-byte raw public key의 unpadded base64url>"
}
```

missing·unknown·extra member, 다른 `kty`·`crv`, canonical하지 않은 base64url, 32 bytes가 아닌 `x`는 거부한다. X25519 exchange 결과가 all-zero shared secret이면 low-order public key로 보고 거부한다.

device key의 안정 식별자는 RFC 7638 JWK thumbprint다.

```text
thumbprint_input =
  UTF8('{"crv":"X25519","kty":"OKP","x":"<canonical x>"}')

device_key_thumbprint =
  base64url_without_padding(SHA-256(thumbprint_input))
```

중앙의 pairing intent, redeem, credential binding과 envelope AAD는 이 thumbprint를 exact 값으로 공유한다. private key는 중앙으로 보내지 않는다.

## 결정 3 — server ephemeral key, KDF와 AEAD

성공하는 최초 redeem마다 중앙은 새 X25519 ephemeral keypair, 32-byte random KDF salt와 12-byte random AES-GCM nonce를 CSPRNG로 생성한다.

```text
shared_secret =
  X25519(server_ephemeral_private_key, device_public_key)

aad_digest =
  SHA-256(aad_bytes)

hkdf_info =
  UTF8("agent-org-network/owner-install/credential-envelope/v1\x00")
  || aad_digest

content_encryption_key =
  HKDF-SHA256(
    ikm=shared_secret,
    salt=kdf_salt,
    info=hkdf_info,
    length=32
  )

ciphertext_and_tag =
  AES-256-GCM.encrypt(
    key=content_encryption_key,
    nonce=nonce,
    plaintext=plaintext_bytes,
    associated_data=aad_bytes
  )
```

`ciphertext_and_tag`는 ciphertext 뒤에 16-byte GCM tag가 붙은 단일 byte string이다. tag를 별도 필드로 나누지 않는다.

server ephemeral private key, shared secret과 content encryption key는 exchange/encryption 직후 폐기하고 DB·receipt·audit·outbox·log에 저장하지 않는다. ephemeral public key만 device JWK와 같은 OKP JWK 형식으로 envelope에 싣는다. ephemeral key, salt 또는 nonce를 다른 credential 암호화에 재사용하지 않는다.

## 결정 4 — canonical AAD

AAD는 다음 exact JSON object를 RFC 8785 JCS로 canonicalize한 UTF-8 bytes다.

```json
{
  "agent_card_id": "<Agent Card ID>",
  "audience": "owner-install",
  "credential_generation": 1,
  "credential_id": "<opaque credential ID>",
  "device_key_thumbprint": "<RFC 7638 thumbprint>",
  "envelope_version": 1,
  "expires_at": "2026-08-27T00:00:00Z",
  "issued_at": "2026-07-28T00:00:00Z",
  "org_id": "<organization ID>",
  "owner_user_id": "<Registry User ID>",
  "scope": ["author.read", "author.write"]
}
```

필드 계약은 다음과 같다.

- `envelope_version`은 정수 `1`이다.
- `audience`는 exact 문자열 `owner-install`이다.
- `credential_generation`은 양의 정수다.
- `issued_at`·`expires_at`은 UTC, 초 정밀도, `Z` suffix의 canonical RFC 3339 instant다.
- `scope`는 중앙 action manifest의 ASCII action을 bytewise 오름차순으로 정렬한 중복 없는 배열이다.
- 모든 ID는 중앙이 발급하거나 Registry에서 읽은 canonical ID다.
- unknown·missing field는 거부한다.

pairing code, browser session ID와 policy proof는 AAD에 넣지 않는다. 이들은 issuance authority proof이지 발급된 credential의 wire identity가 아니다. 중앙 receipt는 pairing intent, issue/redeem authority proof, AAD SHA-256 digest를 연결한다.

## 결정 5 — envelope와 credential plaintext

wire envelope의 exact shape은 다음과 같다.

```json
{
  "aad": {
    "agent_card_id": "<Agent Card ID>",
    "audience": "owner-install",
    "credential_generation": 1,
    "credential_id": "<opaque credential ID>",
    "device_key_thumbprint": "<RFC 7638 thumbprint>",
    "envelope_version": 1,
    "expires_at": "2026-08-27T00:00:00Z",
    "issued_at": "2026-07-28T00:00:00Z",
    "org_id": "<organization ID>",
    "owner_user_id": "<Registry User ID>",
    "scope": ["author.read", "author.write"]
  },
  "ciphertext": "<ciphertext || 16-byte tag의 unpadded base64url>",
  "ephemeral_public_key": {
    "crv": "X25519",
    "kty": "OKP",
    "x": "<32-byte raw public key의 unpadded base64url>"
  },
  "kdf_salt": "<32 random bytes의 unpadded base64url>",
  "nonce": "<12 random bytes의 unpadded base64url>",
  "suite": "AON-OWNER-PAIR-X25519-HKDF-SHA256-A256GCM-v1"
}
```

응답과 replay 저장은 envelope 전체를 JCS로 serialize한다. parser는 먼저 bounded JSON depth/size 안에서 `suite` allowlist를 확인하고, v1 suite이면 `aad.envelope_version == 1`과 exact schema를 확인한다. 모든 base64url 값은 `=` padding을 금지하고 decode 뒤 re-encode한 값이 입력과 exact 일치해야 한다.

암호화할 credential plaintext의 exact shape은 다음과 같다.

```json
{
  "credential_secret": "<32 CSPRNG bytes의 unpadded base64url>",
  "credential_type": "bearer",
  "envelope_version": 1
}
```

이를 JCS UTF-8 bytes로 만든 뒤 암호화한다. org, Owner, Agent Card, generation, scope와 lifetime은 인증된 AAD와 중앙 Durable Credential Registry가 권위이므로 plaintext에 중복하지 않는다.

중앙은 raw `credential_secret`을 commit 전에 검증용 digest로 바꾸고 원문을 저장하지 않는다. digest는 별 server-side pepper/key와 key ID를 사용하는 HMAC-SHA256처럼 offline DB 탈취에 raw bearer 검증을 허용하지 않는 방식이어야 한다. credential 비교는 constant-time이다. raw secret은 receipt·audit·outbox·log에 절대 싣지 않는다.

## 결정 6 — lifetime, replay와 저장

Owner Installation credential의 v1 lifetime은 발급 시각부터 정확히 30일이다. `issued_at`·`expires_at`은 중앙 transaction의 clock이 정한다. client clock은 권위가 아니다.

pairing code는 single-use다. 다만 성공 commit 뒤 응답이 유실된 경우에 한해 같은 code와 같은 device key의 짧은 replay window를 허용한다. replay는 다음 조건을 모두 만족해야 한다.

- 같은 pairing/redeem command와 같은 canonical digest다.
- 같은 `device_key_thumbprint`다.
- 현재 session·Agent Card 소유권·중앙 policy가 여전히 redeem을 허용한다.
- replay window가 지나지 않았다.

이때 새 credential, ephemeral key, salt 또는 nonce를 만들지 않고 저장된 byte-identical envelope를 반환한다. 다른 code/device/payload는 conflict다. 재전달을 위한 envelope ciphertext 저장은 이 짧은 replay window 동안만 허용하고 만료 즉시 삭제한다. ciphertext 저장소는 audit/outbox가 아니며 접근과 retention을 별도로 제한한다.

session revoke는 성공적으로 활성화된 credential을 자동 revoke하지 않는다. 그러나 pending code의 최초 redeem과 envelope replay/redelivery는 막는다. 활성 credential의 폐기는 unpair, Card ownership transfer, 관리자 revoke 또는 generation 교체가 담당한다.

## 결정 7 — rotation과 generation

재-pair 또는 device key rotation은 새 generation, 새 credential secret과 새 envelope를 발급한다. 기존 envelope를 새 key로 재암호화하지 않는다.

Owner client는 만료 7일 전부터 authenticated rotation을 요청하고 만료 24시간 전부터 강한 경고를 표시한다. 자동 무중단 rotation은 다음 두 단계가 durable하게 구현될 때만 연다.

```text
issued_pending_activation
→ client decrypt + keychain atomic store + ack
→ active
```

ack 뒤 이전 generation은 최대 10분 overlap 후 revoke한다. ack 전에는 새 generation을 active로 간주하거나 기존 active generation을 폐기하지 않는다.

이 2단계 lifecycle이 아직 없는 MVP의 명시적 re-pair는 기존 계약대로 새 generation을 즉시 활성화하고 구 generation을 즉시 revoke한다. 이때 응답 유실 복구는 §6의 byte-identical envelope replay가 보장해야 한다. generation을 추정하거나 과거 secret을 다시 활성화하지 않는다.

Card ownership transfer, unpair와 관리자 revoke는 현재 Credential Registry 상태를 즉시 deny로 바꾼다. ciphertext가 남았거나 client가 decrypt한 secret을 보유해도 Registry의 status·generation이 권위다.

## 결정 8 — client 복호화와 private key 저장

Owner client는 다음 순서를 지킨다.

1. suite/version/schema/size와 canonical base64url 길이를 검증한다.
2. AAD의 org, Owner, Agent Card, device thumbprint, generation과 pairing 결과를 exact 비교한다.
3. AAD를 RFC 8785 JCS UTF-8 bytes로 다시 만든다.
4. device private key와 server ephemeral public key로 X25519/HKDF/AES-GCM 복호화한다.
5. plaintext의 exact schema, `credential_type`, version과 32-byte secret을 검증한다.
6. credential secret과 private key를 OS keychain/secret store에 atomic하게 저장한다.
7. 2단계 rotation을 지원하는 경우에만 저장 성공 뒤 중앙에 activation ack를 보낸다.

private key는 가능한 runtime에서 non-extractable native key handle로 보관한다. 그렇지 못한 CLI는 PKCS#8 DER을 OS keychain/secret store의 secret value로 저장할 수 있다. private key나 credential secret을 평문 config, workspace DB, stdout/stderr, browser local/session storage에 두지 않는다.

local 평문 profile에는 central URL과 credential ID, generation, expiry, device thumbprint 같은 공개 binding metadata만 둘 수 있다. decrypt나 keychain 저장이 실패하면 secret을 화면·파일로 fallback하지 않고 pairing을 미완료로 닫는다.

## 결정 9 — fail-closed와 migration

다음 입력은 복구 추정 없이 거부한다.

- unknown suite/version/field 또는 v1 필드 누락
- canonical하지 않은 JWK·JCS 대상 값·base64url
- 잘못된 길이의 key/salt/nonce/tag/secret
- all-zero X25519 shared secret
- AAD의 org/Owner/Card/device/generation/scope/audience 불일치
- AEAD tag 실패
- expired·revoked credential 또는 stale generation
- 지원하지 않는 runtime crypto capability

ADR 0068 이전에 생성되어 suite, key binding, AAD 또는 credential generation을 의미적으로 증명할 수 없는 row/envelope는 `legacy-unverifiable`이다. 값을 추정해 v1으로 backfill하거나 shadow promotion하지 않고 재-pair를 요구한다.

향후 v2는 새 suite와 version, 새 credential generation으로만 발급한다. 이행은 dual-reader/single-writer 기간을 두되 writer는 새 version만 만들고, client 전환 뒤 v1 credential을 명시 revoke한다. 과거에 server ephemeral private key나 raw credential이 저장된 사실을 발견하면 migration 대상이 아니라 보안 incident와 전면 credential rotation 대상으로 취급한다.

## 보안 및 운영 결과

- credential secret은 device public key 소유자만 복호화하며 AAD 변조는 AEAD tag로 거부된다.
- 새 server ephemeral key는 발급 간 key separation을 제공한다. 다만 중앙이나 device가 발급 시점에 침해된 경우까지 보호한다고 주장하지 않는다.
- 중앙은 raw bearer secret을 durable하게 보유하지 않는다. 짧게 저장되는 envelope ciphertext는 재전달 가능성만 주며 device private key 없이 복호화할 수 없다.
- custom 조합을 쓰는 부담을 exact suite, encoding, JCS, golden vector와 downgrade 금지로 제한한다. 구현은 직접 primitive를 작성하지 않고 `cryptography` 49 등 검증된 라이브러리를 사용한다.
- 30일 credential은 장기 offline bearer의 노출 시간을 제한하지만 rotation 장애가 제품 가용성에 직접 영향을 준다. 만료 경고와 2단계 rotation fault recovery가 필요하다.
- 이 ADR은 bearer credential을 mTLS나 hardware-attested credential이라고 과장하지 않는다. 그런 강한 device identity가 필요하면 새 suite/credential type과 별 ADR이 선행돼야 한다.

## RED handoff

구현은 최소 다음 RED를 독립적으로 고정한다.

### 암호와 상호운용

- Python `cryptography` 49가 만든 fixed-input golden vector를 독립 Node/WebCrypto 또는 다른 언어 구현이 복호화하고, 반대 방향도 성공한다.
- fixed device/server key, salt, nonce, AAD, plaintext에 대해 shared secret, HKDF key, JCS bytes와 ciphertext가 byte-exact다.
- JWK JSON member 순서가 달라도 RFC 7638 thumbprint는 같고, `x`가 달라지면 다르다.
- extra/missing JWK member, padding, wrong `kty`/`crv`, wrong `x` length와 all-zero exchange는 거부된다.

### authenticated binding

- AAD JSON field order가 달라도 canonical AAD는 같아 복호화된다.
- org, Owner, Agent Card, device thumbprint, credential ID, generation, scope, audience, issued/expiry 중 하나라도 바뀌면 사전 binding 검증 또는 GCM tag에서 거부된다.
- unsorted/duplicate scope, noncanonical timestamp, unknown AAD/envelope/plaintext field는 거부된다.
- wrong device private key, swapped envelope, truncated ciphertext와 tag는 거부된다.

### 길이·비밀·재사용

- salt 32 bytes, nonce 12 bytes, tag 16 bytes, credential secret 32 bytes를 exact 검사한다.
- 발급마다 ephemeral public key, salt, nonce와 secret이 달라진다.
- DB dump, receipt, audit, outbox와 log 전수 검사에서 pairing code, private/shared/content key와 raw credential secret이 0건이다.
- replay store 외 durable 위치에 ciphertext가 없고 replay TTL 뒤 envelope가 삭제된다.

### 경쟁·장애·lifecycle

- 같은 code+device의 동시 redeem은 한 winner와 byte-identical replay만 만든다.
- 같은 code의 다른 device 또는 다른 payload는 conflict이며 credential write가 0이다.
- credential registry commit 전후, envelope 저장 전후, response 전송 전후, client decrypt/keychain 저장/ack 전후 fault injection에서 secret 중복 발급·generation 부활·영구 lockout이 없다.
- expired/revoked/transferred/stale-generation credential은 central command write가 0이다.
- rotation ack 전에는 old active generation이 유지되고, ack 뒤 overlap 상한을 지나면 old generation이 거부된다.
- unknown suite/version과 crypto capability 부재는 fallback 없이 fail-closed한다.

## 후속 경계

이 ADR의 구현은 wire codec만으로 완료되지 않는다. Durable Credential Registry의 secret verifier, replay envelope retention, generation CAS, keychain adapter와 cross-language golden vector가 함께 닫혀야 한다. 반대로 redeem authority와 session revoke 판정을 이 codec 안으로 끌어들이지 않는다. 그것은 ADR 0050·0067의 중앙 권한 경계가 계속 소유한다.
