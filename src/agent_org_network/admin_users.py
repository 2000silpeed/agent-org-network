"""사용자 프로비저닝 도메인 — 관리자 수동 User 등록(register-only·ADR 0064).

이 모듈은 `admin_registry.py`(카드 라이브 등록·오너 변경)의 **User 축 형제**다 — 같은
3층(순수 admission `admit_*` → 라이브 서비스 `register_*` → durable 저널)을 User로
1:1 미러한다(새 축이 아니라 admission-service 패턴의 새 인스턴스·ADR 0064).

핵심 계약(카드와 같은 결):
  - **무효 User 등록 금지**: 신규 등록은 `admit_user`(nonblank id·email 형식/유일/
    필수·manager 실재) 관문을 통과해야 라이브에 들어간다. 우회 등록 경로 없음
    (ADR 0023 계승·`admit_card`와 같은 관문 규율).
  - **email 전역 유일 = 신원 무결성**: email 유일성 읽기와 `register_user` 쓰기를 같은
    임계 구역에 묶는다 — 동시 같은-email 등록이 둘 다 통과하면 `resolve_identity`
    (ADR 0021)가 복수매칭 401로 그 email 사용자 전원을 로그인 불가로 만든다.
  - **전이 ≠ 기록**: 등록(전이)은 `Registry.register_user`(도메인), `UserRegistered`
    감사 이벤트는 기록(append-only). 저널(기계 리플레이 원천) ≠ 감사(사람이 읽는 이력).
  - **register-only**: edit/비활성/삭제/재-parent는 이 모듈이 하지 않는다(ADR 0064
    결정 ⑤ — 참조 무결성 전이 설계는 별도 ADR). 등록은 manager 그래프를 채우기만 한다.

shape(값 객체·시그니처·계약)는 domain-architect가 낸다. `admit_user`·`register_user`의
로직 본체와 red→green 테스트는 tdd-engineer가 잇는다. `SqliteUserJournal`(durable)·
`replay_user_journal`(부팅 복원·user→card 순서)·`user.register` 인가 배선은
mcp-runtime-engineer가 진다(ADR 0064 결정 ⑥⑦).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Protocol, cast

from agent_org_network.user import User

if TYPE_CHECKING:
    from agent_org_network.admin_registry import AdminAuditSink
    from agent_org_network.registry import Registry
    from agent_org_network.sqlite_stores import SqliteUserJournal


Clock = Callable[[], datetime]

# `UserRegistered` 감사 이벤트 kind(append-only·`CardRegistered`와 대칭). `action_record`의
# `action` 인자로 넣어 `AdminAuditSink.record_action`에 남긴다(전이 ≠ 기록).
USER_REGISTERED_ACTION = "UserRegistered"


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class UserCandidate:
    """폼→User 후보 DTO — admission 관문에 넘길 원시 필드(얇은 어댑터 입력).

    `User` 필드를 미러한다(`user_id`→`User.id`·`email`·`manager`). `user_id`는 관리자가
    직접 입력하는 사람이 읽는 id(ADR 0064 결정 ② — email 파생·서버 생성 아님). `CardCandidate`
    (`admin_registry.py`)의 User 판이다 — admission이 이 후보를 `(User|None, errors)`로 판정한다.
    """

    user_id: str
    email: str | None = None
    manager: str | None = None


class DuplicateUserError(Exception):
    """이미 존재하는 user_id로 신규 등록 시도 — 등록 무결성(중복 거부·409 매핑).

    `admit_user`가 아니라 `AdminUserService.register_user`가 던진다(`Registry.register_user`
    의 id 중복 `RegistryError`를 이 타입으로 매핑). `DuplicateCardError`의 User 판.
    """


class UserJournalSink(Protocol):
    """User 라이브 등록을 durable 저널에 남기는 포트(ADR 0064 결정 ⑦).

    `RegistryJournalSink`(카드 저널·`admin_registry.py`)의 User 판이다 — 실 구현은 병렬
    `SqliteUserJournal`(신규 `user_journal` 테이블·`sqlite_stores.py`·tdd/mcp-runtime).
    카드 저널 테이블에 user kind를 얹지 않는다(그 테이블은 카드 필드 shape — ADR 0064 결정 ⑦).
    감사 로그와 다른 축: 저널은 *기계가 재생하는 리플레이 원천*(재기동 시 라이브 User 복원),
    감사는 *사람이 읽는 이력*. 부팅 리플레이 순서는 **user → card**(라이브 카드 owner가
    라이브 User 참조 → User가 먼저 복원돼야 카드 admission의 owner 실재가 통과).
    """

    def append_register(
        self,
        *,
        user_id: str,
        email: str | None,
        manager: str | None,
        by: str,
        at: datetime,
    ) -> None: ...


def admit_user(
    candidate: UserCandidate,
    registry: "Registry",
    *,
    require_email: bool = True,
) -> tuple[User | None, list[str]]:
    """User 후보를 admission 규칙으로 검증해 `(User|None, errors)`를 낸다(순수 함수).

    `admit_card`(`admin_registry.py`)의 User 판 — 통과면 `(user, [])`, 실패면
    `(None, [사유...])`. 라이브 mutation은 *하지 않는다*(판정만 — 반영은 `register_user`).

    관문(ADR 0064 결정 ②③④):
      ① **nonblank user_id** — 빈/공백 id 거부(조기 반환·`admit_card`의 agent_id 결).
         id 중복은 여기서 보지 않는다(카드가 `has_card`를 `register_card`에서 보듯,
         User는 `register_user`가 `DuplicateUserError`로 진다).
      ② **email 필수(정책)** — `require_email`이면 email이 None/공백일 때 거부. 실 프로비저닝
         (`POST /admin/users`)은 기본 True, 시드/테스트는 False로 `email=None` 허용
         (불변식 "제공 시 유일"과 정책 "실 등록 시 필수"를 분리).
      ③ **email 형식** — email이 있으면 최소 형식(nonblank·`@` 포함·양끝 공백 없음) 검사.
         약한 검사다(실 email 검증은 IdP·OIDC의 몫·ADR 0021) — 명백한 쓰레기만 거른다.
      ④ **email 전역 유일** — email이 있으면 기존 User와 겹치면 거부. `resolve_identity`
         (ADR 0021)가 쓰는 것과 **같은 정확 문자열 동등**(그 매칭보다 느슨하면 안 됨 —
         정확 동등이면 "유일 email → resolve_identity ≤1 매칭"이 보장). `email=None`끼리는
         겹침 아님(복수 허용).
      ⑤ **manager 실재** — `manager`가 None 또는 공백이면 루트로 정규화한다(email `.strip()`
         처리와 대칭 — UI를 안 거치는 직접 API 호출이 `manager=""`를 보내도 "미등록 manager: "
         같은 혼란스러운 422 대신 루트로 admit된다). 정규화 후 None이 아니면
         `manager ∈ registry.user_ids()`(`Registry.validate`의 user.manager 실재 불변식을
         앞단 미러). register-only라 순환·자기참조는 원천 불가(신규 id는 아직 미등록·기존
         User manager 미편집) — 별도 순환 검사 불요.

    구현 노트(tdd-engineer): ①은 조기 반환(`admit_card` 결), ②③④⑤는 사유를 모아 반환한다.
    email 유일성 읽기(`registry.all_users()`/`user_ids()`)는 `register_user`가 같은 임계
    구역에서 호출해 쓰기와 원자화한다(email 유일은 Registry가 강제하지 않으므로 admission이
    읽기-쓰기를 직렬화해야 동시 같은-email이 둘 다 통과하지 않는다).
    """
    if not candidate.user_id or not candidate.user_id.strip():
        return None, ["user_id는 비어 있을 수 없습니다."]

    errors: list[str] = []
    email = candidate.email

    if require_email and (email is None or not email.strip()):
        errors.append("email은 비어 있을 수 없습니다.")
    elif email is not None:
        if not email.strip() or "@" not in email or email != email.strip():
            errors.append(f"잘못된 email 형식: {email!r}")
        else:
            for existing in registry.all_users():
                if existing.email == email:
                    errors.append(f"이미 사용 중인 email: {email}")
                    break

    # 공백 manager는 루트로 정규화(email `.strip()` 처리와 대칭 — 직접 API 호출의
    # `manager=""`가 "미등록 manager: " 같은 혼란스러운 422로 새지 않게 한다).
    manager = candidate.manager
    if manager is not None and not manager.strip():
        manager = None
    if manager is not None and manager not in registry.user_ids():
        errors.append(f"미등록 manager: {manager}")

    if errors:
        return None, errors

    return User(id=candidate.user_id, manager=manager, email=email), []


class AdminUserService:
    """라이브 User 등록 서비스(register-only·ADR 0064 결정 ①⑧).

    `AdminRegistryService`(카드)의 **형제**다 — 별도 클래스·별도 lock(확장 아닌 미러).
    카드 서비스는 오너 변경(토큰 revoke·WS 끊기·owner 격리 임계 구역)으로 무겁지만 User
    등록은 그 기계가 없다 → 섞지 않고 대칭 서비스로 둔다(ADR 0064 Consequences). 같은
    `Registry` 인스턴스를 라우터·AskOrg가 라이브로 읽으므로 등록이 반영되면 즉시 그래프·
    escalation·신원 매핑에 잡힌다.

    Depth 무관 동일 코어(ADR 0064 결정 ⑧): Depth A(데모·`_session_identity` 게이트)는 이
    서비스를 직접 호출하고, Depth B(중앙 모드 R1 UoW·ADR 0044)는 operational application이
    UoW 안에서 이 코어를 재사용한다 — 인가·트랜잭션 경계만 승격, 서비스·admission·저널·감사는 불변.
    """

    def __init__(
        self,
        registry: "Registry",
        *,
        audit_sink: "AdminAuditSink | None" = None,
        journal_sink: UserJournalSink | None = None,
        require_email: bool = True,
        clock: Clock = _default_clock,
    ) -> None:
        self._registry = registry
        self._audit_sink = audit_sink
        self._journal_sink = journal_sink
        self._require_email = require_email
        self._clock = clock
        # admission의 email 유일성 읽기와 `register_user` 쓰기를 원자화하는 임계 구역
        # (email 유일은 Registry가 강제하지 않으므로 서비스가 직렬화해야 한다·ADR 0064 근거).
        self._lock = threading.RLock()

    def register_user(self, candidate: UserCandidate, *, by: str) -> User:
        """신규 User를 admission 통과 즉시 라이브 Registry에 반영한다(ADR 0064 결정 ①).

        흐름(카드 `register_card`와 대칭·전이 ≠ 기록):
          1. **임계 구역**: `admit_user`(무효면 `AdmissionError`) → `registry.register_user`
             (id 중복이면 `RegistryError` → `DuplicateUserError`). email 유일성 읽기와
             쓰기를 같은 lock에 묶어 동시 같은-email 등록을 직렬화(ADR 0064 근거).
          2. `journal_sink` 주입 시 durable 저널 append(`append_register` — 재기동 복원 원천).
          3. `UserRegistered` 감사 이벤트 append(`action_record(action="UserRegistered",
             subject_id=user.id, by=by, detail={"email": user.email, "manager": user.manager})`
             → `AdminAuditSink.record_action` — append-only·전이와 분리).
        `by`는 등록을 낸 운영자 신원(감사 who·Depth A=세션 신원·Depth B=grant subject).
        실 프로비저닝이라 `admit_user(require_email=self._require_email)`(기본 True).

        구현은 tdd-engineer(ADR 0064). 감사 기록 헬퍼는 `AdminRegistryService._record_action`
        (미주입 sink면 no-op)을 미러한다.
        """
        from agent_org_network.admin_registry import AdmissionError
        from agent_org_network.registry import RegistryError

        with self._lock:
            user, errors = admit_user(
                candidate, self._registry, require_email=self._require_email
            )
            if user is None:
                raise AdmissionError(errors)
            try:
                self._registry.register_user(user)
            except RegistryError as exc:
                raise DuplicateUserError(str(exc)) from exc

            if self._journal_sink is not None:
                self._journal_sink.append_register(
                    user_id=user.id,
                    email=user.email,
                    manager=user.manager,
                    by=by,
                    at=self._clock(),
                )

            self._record_action(
                action=USER_REGISTERED_ACTION,
                subject_id=user.id,
                by=by,
                detail=user_registered_detail(user),
            )
        return user

    def _record_action(
        self, *, action: str, subject_id: str, by: str, detail: dict[str, Any]
    ) -> int:
        """감사 이벤트를 append하고 그 인덱스를 돌려준다(전이 ≠ 기록).

        `AdminRegistryService._record_action`을 미러한다(미주입 sink면 -1 — 감사 없이
        전이만, 결정론 단위 테스트가 sink 없이 도메인을 본다).
        """
        if self._audit_sink is None:
            return -1
        from agent_org_network.audit import action_record

        record = action_record(
            timestamp=self._clock(),
            action=action,
            subject_id=subject_id,
            by=by,
            detail=detail,
        )
        self._audit_sink.record_action(record)
        records_fn = getattr(self._audit_sink, "records", None)
        if callable(records_fn):
            result: object = records_fn()
            if isinstance(result, list):
                return len(cast("list[object]", result)) - 1
        return -1


def replay_user_journal(journal: "SqliteUserJournal", registry: "Registry") -> None:
    """durable User 저널을 처음부터 순서대로 재생해 라이브 User를 복원한다(ADR 0064 결정 ⑦).

    `replay_registry_journal`(카드)의 User 축 형제다. 중앙 기동 시 호출 순서:
    `registry.load(seed)`(YAML 초기 시드) → `registry.validate()` → **이 함수(User 복원)**
    → `replay_registry_journal`(카드 복원). **user → card 순서 필수** — 라이브 카드의
    owner가 라이브 등록 User를 참조할 수 있으므로 User가 카드보다 먼저 복원돼야 카드
    admission의 owner 실재가 통과한다.

    각 항목을 `admit_user`(admission 관문)에 그대로 태운다 — **우회 없음**("무효 User는
    등록되지 않는다" 불변식이 리플레이에도 그대로 적용된다). 참조 무결성이 깨진 항목
    (미등록 manager 등)·email 충돌·이미 존재하는 id는 조용히 건너뛴다(복원 실패가 부팅을
    막지 않는다 — `replay_registry_journal`과 같은 안전측 스킵).

    **`require_email=False`로 재-admission**한다 — 저널은 등록 시점에 이미 admission을
    통과한 것만 담고 있으므로, "실 등록 시 email 필수"(intake 정책·ADR 0064 결정 ③)를
    리플레이에 다시 강제하지 않고 등록 당시 통과한 값을 그대로 복원한다. 단 email 형식·
    전역 유일·manager 실재 같은 durable 불변식은 admission이 리플레이에서도 그대로 강제한다.
    """
    from agent_org_network.registry import RegistryError

    for entry in journal.entries():
        candidate = UserCandidate(
            user_id=entry.candidate.user_id,
            email=entry.candidate.email,
            manager=entry.candidate.manager,
        )
        user, _errors = admit_user(candidate, registry, require_email=False)
        if user is None:
            continue  # 참조 무결성 붕괴·email 충돌 등 — 안전측 스킵(부팅 중단 없음).
        try:
            registry.register_user(user)
        except RegistryError:
            continue  # 이미 존재하는 id(멱등 방어·중복 재생 무시).


def user_registered_detail(user: User) -> dict[str, Any]:
    """`UserRegistered` 감사 detail 형태(계약 anchor·`action_record`의 detail 인자).

    카드 `CardRegistered`의 `{"owner":..., "team":...}`와 대칭인 User 판. `register_user`
    본체(tdd-engineer)가 이 detail로 `action_record(action=USER_REGISTERED_ACTION,
    subject_id=user.id, by=by, detail=user_registered_detail(user))`를 만들어 감사에 남긴다.
    """
    return {"email": user.email, "manager": user.manager}


__all__ = [
    "UserCandidate",
    "admit_user",
    "DuplicateUserError",
    "UserJournalSink",
    "AdminUserService",
    "USER_REGISTERED_ACTION",
    "user_registered_detail",
    "replay_user_journal",
]
