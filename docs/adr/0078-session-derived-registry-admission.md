# ADR 0078 — browser session-derived Registry admission과 Central Next onboarding

- 상태: Implemented (RB3.2b.3-A–E 완료; real IdP/TLS 및 Full Gate는 미완료)
- 날짜: 2026-07-31
- 관련: ADR 0004 (중앙 Authority), ADR 0005 (Registry graph), ADR 0034 (Agent Card admission),
  ADR 0064 (Registry User admission), ADR 0075 (세 installation artifact), ADR 0077 (browser session)

## 맥락

ADR 0077의 browser SSO는 이미 등록된 Registry User만 opaque Central session으로 해석한다.
그러나 bootstrap root 뒤 추가 User와 Agent Card를 실제 Central Next에서 register-only로
admit하지 못하면 legacy `admin.html` 기능은 사라진 채 남는다. 기존 `production_onboarding_web.py`는
legacy identity session/OIDC composition의 tested component라 현재 Central API에 mount할 수 없다.

기존 SQLite User/Card stores는 receipt replay, CAS, audit/outbox와 shared
`production_registry_revisions`를 이미 갖는다. 하지만 Central composition의 global Registry store는
의도적으로 read-only authorizer를 사용한다. 이를 mutable request authority로 바꾸면 OIDC identity
resolve/read path까지 registration capability가 섞인다.

## 결정

### 경계와 route

Central Next와 private Central API의 admission mapping은 아래 다섯 개로 고정한다. 이들은 auth
four-route BFF의 확장이나 generic BFF가 아니며, 각 method/path가 하나의 same-semantic upstream만
갖는다.

| Browser → Central Next | private Central API | method | current Authority action | 의미 |
|---|---|---|---|---|
| `/api/onboarding/status` | `/onboarding/status` | GET | `session.read` | current session에 보이는 User→Card→Card Owner Installation handoff 상태 |
| `/api/admin/users` | `/admin/users` | GET | `user.register` | register 가능한 Registry User 목록/현재 revision |
| `/api/admin/users` | `/admin/users` | POST | `user.register` | additional Registry User register-only admission |
| `/api/admin/agent-cards` | `/admin/agent-cards` | GET | `card.register` | register 가능한 Agent Card 목록/현재 revision |
| `/api/admin/agent-cards` | `/admin/agent-cards` | POST | `card.register` | Agent Card register-only admission |

`/onboarding`은 guided User → Agent Card → Card Owner Installation handoff다. knowledge upload,
OKF preview/body, raw source/full draft, Owner credential은 Card Owner Installation에만 남는다.
`/admin`은 ongoing User/Card list와 independent register-only form이다. Card Owner transfer/revoke,
scorecard와 policy edit은 RB3.2b.6에 남긴다.

### onboarding status safe projection

`GET /onboarding/status`의 exact top-level key는 `revision`, `card_capability`, `steps`, `cards`,
`card_owner_installation`이다. 전체 `users` 목록은 넣지 않는다. 그것은 current `user.register`
Authority가 필요한 `GET /admin/users`의 별 projection이다.

```json
{
  "revision": 4,
  "card_capability": "available",
  "steps": [
    {"kind": "user", "label": "Registry User", "state": "complete"},
    {"kind": "card", "label": "Agent Card", "state": "complete"},
    {"kind": "card_owner_installation", "label": "Card Owner Installation", "state": "current"}
  ],
  "cards": [
    {"agent_id": "legal", "owner": "new_user", "team": "Legal", "summary": "Contract questions"}
  ],
  "card_owner_installation": {
    "artifact": "agent-org-owner",
    "href": "/onboarding#card-owner-installation"
  }
}
```

valid browser session은 existing Registry User에 이미 bound되므로 `user.state`는 exact
`complete`다. `card.state`는 current User가 owner인 Card가 있으면 `complete`, 없고 Card
capability가 `available`이면 `current`, capability가 `unavailable`이면 `locked`다.
`card_owner_installation.state`는 owner Card가 있으면 `current`, 아니면 `locked`이며 이 slice는
실제 pair/install 상태를 소유하지 않으므로 `complete`를 만들지 않는다. `card_capability`는
`available|unavailable`이다. `cards`는 current User가 **owner**인 Card의 exact
`{agent_id, owner, team, summary}` safe summary만 포함한다. maintainer-only 또는 다른 User의
Card, full Agent Card, knowledge source, raw/OKF body는 포함하지 않는다.

`card_owner_installation` object는 언제나 exact
`{"artifact":"agent-org-owner","href":"/onboarding#card-owner-installation"}`다. 이는 Central
Next가 번들에 포함한 설치 안내로 가는 fixed relative anchor일 뿐 Owner API URL, runtime endpoint,
profile, pairing token 또는 credential을 담지 않는다. 기존 frontend의 third kind `knowledge`는
폐기하며 wire와 UI 어디에서도 alias로 받지 않는다.

### request, identity, Authority

GET은 `__Host-aon-central-session` cookie만 받는다. POST는 exact `Origin ==
central_public_origin`, `Sec-Fetch-Site: same-origin`, `Sec-Fetch-Mode: cors`,
`Sec-Fetch-Dest: empty`, session cookie, readable `__Host-aon-central-csrf` cookie와 exact
`X-AON-CSRF`, bounded `Idempotency-Key`, JSON content type를 모두 요구한다. missing, `null`,
cross-site/same-site, extra query, malformed/oversize body, forwarded/authorization or actor/org/role/
permission/session self-claim header는 fail-close한다. Next standalone entrypoint는 raw HTTP request에서
`forwarded`, 모든 `x-forwarded-*`, 그리고 internal provenance marker의 caller-supplied presence를 먼저
fail-close marker로 분류하고 제거한 뒤, Next가 만든 transport facts에만 clean marker를 붙인다. route
handler의 URL/loopback 값과의 일치는 provenance가 아니므로 clean marker 없는 exact-match 값도 거부한다.
그 외 일반 browser header는 self-claim이 아니며 거부 기준이 아니다. Next는 `Cookie`, 위 Origin/Fetch/CSRF,
`Idempotency-Key`, `Content-Type`만 upstream에 forward하고 request host가 upstream이나 redirect를
결정하지 못한다.

Central API는 Browser Session Principal을 먼저 resolve한다. request마다 session active/expiry,
existing Registry User/org binding, 해당 action의 current file Authority를 확인한다. body/header/
query의 actor/org/role/session은 principal 또는 ResourceRef의 근거가 아니다.

global `SqliteProductionRegistryUsers(authorize=_ReadOnlyRegistrationAuthorizer())`는 immutable하게
유지한다. `Session-Derived Registry Registration Application`은 request마다 session digest와 action을
닫아 둔 transaction-current authorizer 및 scoped User/Card store를 factory로 만든다. store의
`current()`과 `verify_precommit()`은 같은 `BEGIN IMMEDIATE` DB transaction 안에서 다음을 다시 읽는다.

1. durable browser session row의 digest, active/expiry/end state와 Registry User/org binding
2. canonical Registry schema, binding fingerprint와 shared Registry revision
3. reload한 current Authority의 exact `user.register` 또는 `card.register` grant

한 단계라도 drift, deny 또는 unavailable이면 receipt/audit/outbox/Registry write 없이 닫는다.
기존 stores의 idempotent receipt replay, expected revision CAS, admission, audit/outbox와 UoW는
그대로 재사용한다. Central migration은 Agent Card canonical four table capability를 먼저 mount하고
marker-last로 Central marker v4→v5를 쓴다.

User와 Card의 receipt replay는 동일한 의미를 가진다. same org, session-derived actor,
`Idempotency-Key`, canonical command digest가 모두 exact이면 original safe result와
`replayed=true`만 반환하며 revision/receipt/audit/outbox를 추가하지 않는다. replay라도
`current()`/`verify_precommit()`의 active session, Registry binding, current action Authority
re-read를 생략하지 않는다. key의 다른 digest는 conflict이고 current session/Authority deny 또는
unavailable은 replay 여부와 무관하게 deny/unavailable이다. 이미 성공한 command의 stale
`expected_revision`은 exact replay를 새 CAS conflict로 바꾸지 않지만, 새 command는 항상 shared
revision CAS를 통과해야 한다.

### owner 지정과 DTO

`card.register`는 `user.register`와 마찬가지로 static central action이다. Authority가 current actor에게
grant한 경우 actor는 existing Registry User를 Card Owner로 지정할 수 있다. self-registration은
`owner == actor`인 같은 command일 뿐 별 action이 아니다. 따라서 admin/operator delegation은
`routing_rules.yaml`의 `card.register` permission만이 허용하며, manager 관계, body owner, client role은
delegation 근거가 아니다. existing Card의 owner를 바꾸거나 없애는 transfer/revoke는 이 decision에
포함하지 않는다.

strict JSON input은 다음뿐이다. unknown field와 actor/org/role/session/authority/last-reviewed self
claim은 거부한다.

```json
POST /admin/users
{"expected_revision": 3, "user_id": "new_user", "email": "new@example.test", "manager": "root"}

POST /admin/agent-cards
{"expected_revision": 4, "agent_id": "legal", "owner": "new_user", "team": "Legal", "summary": "...",
 "domains": ["contract"], "maintainer": null, "can_answer": [], "cannot_answer": [],
 "approval_when": [], "collaborate_when": [], "knowledge_sources": [], "trust_labels": []}
```

`expected_revision`은 nonnegative integer, `manager`/`maintainer`만 nullable이며 Agent Card의
`last_reviewed_at`은 Central clock으로만 만든다. success projection은 safe User/Card projection,
shared `revision`, `replayed`만 포함한다. list/status는 raw cookie/session/OIDC claim, authority
evidence/policy digest, receipt/audit/outbox 내부값, Owner raw/OKF 본문을 반환하지 않는다.

오류 body는 언제나 `{ "error": code }`이다. session 부재/만료는 `401
browser_session_unauthenticated`, session/action deny는 `403 browser_session_forbidden` 또는
`registry_registration_forbidden`, CSRF failure는 `403 browser_csrf_forbidden`, revision/semantic
conflict는 `409 registry_revision_conflict` 또는 `registry_registration_conflict`, DTO/idempotency
failure는 `422 invalid_registration_request`, profile/schema/session/policy/dependency failure는
`503 registry_registration_unavailable`이다. error는 다른 session/User/Card 존재나 내부 evidence를
구분해 누설하지 않는다.

## 결과

Central browser는 legacy HTML을 되살리지 않고 session-derived, current-Authority Registry admission을
얻는다. User/Card graph mutation은 기존 canonical UoW에만 남고, Card Owner local knowledge boundary는
Central BFF와 분리된다. 이 ADR은 real IdP/TLS, JIT/invitation, Card ownership lifecycle, support
승격 또는 Full Gate를 완료로 주장하지 않는다.
