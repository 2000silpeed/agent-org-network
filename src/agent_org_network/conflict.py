import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Annotated, Literal, Protocol, TypeAlias, final

from pydantic import BaseModel, ConfigDict, Field

from agent_org_network.complement import ComplementEdge, EdgeStore

Clock = Callable[[], datetime]


def default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_case_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Resolution:
    intent: str
    primary: str
    rationale: str = ""


@dataclass(frozen=True)
class Precedent:
    """합의 결론(Resolution)의 append-only 기록 — 라우터가 자동 적용한다.

    신선도 신호(ADR 0019 결정 4): `needs_review`·`last_flagged_at`은 변경 전파기
    (`StalenessPropagator`)가 OKF 커밋 변경 이벤트로 *과거 판례*에 stale을 표식하는
    필드다(frozen·하위호환 기본값). **`status: Literal['valid', ...]`는 안 쓴다** —
    'valid'가 admission 어휘("유효하지 않은 카드는 등록되지 않는다")와 충돌·자기모순
    이고, stale은 *재검토 대상*이지 *무효*가 아니므로 boolean이 정확하다. `recorded_at`
    은 불변. **stale ≠ 무효화**: Router lookup은 `needs_review`를 *보지 않으므로*
    (router.py) stale 판례도 계속 라우팅된다(미아 없음 보존). 무효화는 owner 1인칭
    처분(`InvalidatePrecedent`) 후만(ADR 0019 결정 6).

    무효 신호(ADR 0019 결정 6·T8.4(d)): `invalidated`·`invalidated_at`·`invalidated_by`
    는 owner가 `InvalidatePrecedent`로 명시 처분한 *뒤* `PrecedentStore.invalidate`가
    다는 무효 표식이다(frozen·하위호환 기본값·append-only — store 삭제 X). **`needs_review`
    (stale·재검토 대상·라우팅 유지)와 `invalidated`(무효·라우팅 제외)는 독립 축**이다
    (stale ≠ 무효 — 결정 6). Router는 `needs_review`는 안 보지만 `invalidated`면 판례
    경로를 *건너뛰고* 분류기 경로로 폴백한다(판례 단축경로만 끊음·아래 분류기 경로가
    항상 종착이라 미아 없음 보존). 무효화도 append-only라 `list_all`·`find_by_primary`엔
    *그대로 남는다*(운영 면 열람 보존).
    """

    resolution: Resolution
    recorded_at: datetime
    needs_review: bool = False
    last_flagged_at: datetime | None = None
    invalidated: bool = False
    invalidated_at: datetime | None = None
    invalidated_by: str | None = None


class PrecedentStore(Protocol):
    def record(self, resolution: Resolution) -> Precedent: ...

    def lookup(self, intent: str) -> Precedent | None: ...

    def find_by_primary(self, agent_id: str) -> list["Precedent"]:
        """그 agent_id를 `Resolution.primary`로 둔 판례 전부(ADR 0019 결정 2①).

        변경 전파기가 OKF 커밋 영향 식별에 쓴다 — `event.agent_id`를 primary로 둔
        판례가 영향 대상이다(`Resolution.primary`는 agent_id 문자열). 역색인 O(1) 조회.
        """
        ...

    def list_all(self) -> list["Precedent"]:
        """기록된 모든 판례(영향 식별 fallback·운영 면 열람의 원천, ADR 0019 결정 2①)."""
        ...

    def flag_stale(self, intent: str, trigger_sha: str, at: datetime) -> Precedent | None:
        """그 intent의 판례에 stale을 표식한다 — `needs_review=True`·`last_flagged_at=at`.

        ADR 0019 결정 4·6: 무효화가 아니라 *플래그*다(라우팅 불변·미아 없음). 이미
        needs_review면 멱등(다시 안 단다). 판례 없으면 None. append-only 정신 —
        새 인스턴스로 갈아끼우고 history에 남긴다(파괴적 변경 X).
        """
        ...

    def invalidate(self, intent: str, by_owner: str, at: datetime) -> Precedent | None:
        """그 intent의 판례를 무효로 표식한다 — `invalidated=True`·`invalidated_at=at`·
        `invalidated_by=by_owner`(ADR 0019 결정 6·T8.4(d)).

        owner가 `InvalidatePrecedent`로 명시 처분한 *뒤* `ReevalService`가 호출한다
        (Authority 중앙: 무효화 판단만 owner 1인칭). `flag_stale`과 같은 형태:
          - 판례 없으면 None.
          - 이미 invalidated면 멱등(그대로 반환·다시 안 단다).
          - append-only — `_precedents`/`_by_primary`에서 *제거하지 않고* 새 인스턴스로
            갈아끼우고 history에 남긴다(파괴적 변경 X·운영 면 열람[list_all] 보존).

        **stale ≠ 무효** — `needs_review`(라우팅 유지)와 `invalidated`(라우팅 제외)는
        독립 축이라 서로 덮어쓰지 않는다(이미 stale이어도 invalidated만 켠다). 라우팅
        제외는 Router가 `p.invalidated`를 보고 판례 경로를 건너뛰는 것으로 일어난다
        (lookup은 순수 읽기로 그대로 반환 — 안 B).
        """
        ...


class InMemoryPrecedentStore:
    def __init__(self, clock: Clock = default_clock) -> None:
        self._clock = clock
        self._precedents: dict[str, Precedent] = {}
        self.history: list[Precedent] = []
        # agent_id → 그 agent를 primary로 둔 판례들(역색인, record 시점에 채움 — ADR 0019 결정 2①).
        self._by_primary: dict[str, list[Precedent]] = {}

    def record(self, resolution: Resolution) -> Precedent:
        precedent = Precedent(resolution=resolution, recorded_at=self._clock())
        self.history.append(precedent)
        self._precedents[resolution.intent] = precedent
        self._by_primary.setdefault(resolution.primary, []).append(precedent)
        return precedent

    def lookup(self, intent: str) -> Precedent | None:
        return self._precedents.get(intent)

    def find_by_primary(self, agent_id: str) -> list[Precedent]:
        return list(self._by_primary.get(agent_id, []))

    def list_all(self) -> list[Precedent]:
        return list(self._precedents.values())

    def flag_stale(self, intent: str, trigger_sha: str, at: datetime) -> Precedent | None:
        existing = self._precedents.get(intent)
        if existing is None:
            return None
        if existing.needs_review:
            return existing
        import dataclasses

        flagged = dataclasses.replace(existing, needs_review=True, last_flagged_at=at)
        self._swap(intent, existing, flagged)
        return flagged

    def invalidate(self, intent: str, by_owner: str, at: datetime) -> Precedent | None:
        existing = self._precedents.get(intent)
        if existing is None:
            return None
        if existing.invalidated:
            return existing
        import dataclasses

        invalid = dataclasses.replace(
            existing, invalidated=True, invalidated_at=at, invalidated_by=by_owner
        )
        self._swap(intent, existing, invalid)
        return invalid

    def _swap(self, intent: str, existing: Precedent, updated: Precedent) -> None:
        """append-only 표식 교체 — `_precedents`·`_by_primary` 동기화 + history append.

        flag_stale·invalidate 공통: 기존 인스턴스를 *삭제하지 않고* 새 인스턴스로
        갈아끼우고(`_precedents[intent]`·`_by_primary` 역색인) history에 남긴다.
        """
        self._precedents[intent] = updated
        self.history.append(updated)
        by_p = self._by_primary.get(existing.resolution.primary, [])
        for i, p in enumerate(by_p):
            if p is existing:
                by_p[i] = updated
                break


# ── 미해소 다툼의 저장 단위: ConflictCase ─────────────────────────────
#
# Contested(후보 ≥2)가 나면 그 다툼을 "미해소 케이스"로 저장한다. 케이스는
# 후보 Owner들의 처리함(Inbox)에 떠서 1인칭 합의의 데이터 원천이 된다.
# 상태(open → resolved)는 frozen 값을 새 인스턴스로 갈아끼워 전이한다
# (불변+새 인스턴스, RoutingDecision의 "타입이 곧 상태" 정신과 정합).


CaseStatus = Literal["open", "escalated", "resolved", "declined"]


class _ConflictFrozenDto(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


@final
class DivergentVotes(_ConflictFrozenDto):
    kind: Literal["divergent_votes"] = "divergent_votes"
    round: int = Field(ge=1)


@final
class CandidateRegistryChanged(_ConflictFrozenDto):
    kind: Literal["candidate_registry_changed"] = "candidate_registry_changed"
    round: int = Field(ge=1)
    reason_code: Literal[
        "candidate_missing",
        "owner_missing",
        "owner_changed",
        "under_claim_changed",
    ]


ConflictEscalationCause: TypeAlias = Annotated[
    DivergentVotes | CandidateRegistryChanged,
    Field(discriminator="kind"),
]


@dataclass(frozen=True)
class Candidate:
    """다툼에 걸린 후보 한 명 — agent_id와 그 Owner(처리함 귀속 키).

    Owner별 처리함 조회를 위해 owner를 함께 들고 있는다. AgentCard 전체가
    아니라 식별자만 보관 — 케이스는 라우터向(intent)·처리함向(owner) 색인이
    핵심이고, 카드 본문은 Registry가 출처(stale 회피)다.
    """

    agent_id: str
    owner: str


@dataclass(frozen=True)
class ConflictCase:
    """미해소 다툼의 저장 단위.

    intent(어떤 분류 라벨의 다툼인지) + 후보들 + 원문 question + 상태 + 생성
    시각(주입 clock 결정론). question 원문은 Owner가 처리함에서 "무엇을 두고
    다투는지" 맥락을 보고 1인칭 판단을 내리기 위해 보관한다.

    open → resolved 전이는 `resolve()`가 새 인스턴스를 돌려준다(파괴적 변경 X).
    resolution은 resolved일 때만 채워지는 합의 결론(intent→primary).
    """

    intent: str
    question: str
    candidates: tuple[Candidate, ...]
    opened_at: datetime
    case_id: str = field(default_factory=_new_case_id)
    status: CaseStatus = "open"
    resolution: Resolution | None = None
    request_id: str | None = None
    concurrence_round: int = 1
    manager_item_id: str | None = None
    decline_reason: Literal["manager_declined"] | None = None

    def __post_init__(self) -> None:
        from agent_org_network.request_correlation import validate_optional_request_id

        validate_optional_request_id(self.request_id)
        if self.concurrence_round < 1:
            raise ValueError("ConflictCase.concurrence_round는 1 이상이어야 합니다.")
        if self.manager_item_id is not None and not self.manager_item_id.strip():
            raise ValueError("ConflictCase.manager_item_id는 nonblank여야 합니다.")
        if self.status == "open":
            if (
                self.resolution is not None
                or self.manager_item_id is not None
                or self.decline_reason is not None
            ):
                raise ValueError("open ConflictCase에는 종결 필드를 둘 수 없습니다.")
        elif self.status == "escalated":
            if (
                self.resolution is not None
                or self.manager_item_id is None
                or self.decline_reason is not None
            ):
                raise ValueError("escalated ConflictCase에는 manager_item_id만 필요합니다.")
        elif self.status == "resolved":
            if self.resolution is None or self.decline_reason is not None:
                raise ValueError("resolved ConflictCase에는 Resolution만 필요합니다.")
        elif self.status == "declined":
            if (
                self.resolution is not None
                or self.manager_item_id is None
                or self.decline_reason != "manager_declined"
            ):
                raise ValueError(
                    "declined ConflictCase에는 manager_item_id와 manager_declined가 필요합니다."
                )
        else:
            raise ValueError(f"알 수 없는 ConflictCase.status입니다: {self.status!r}")

    @classmethod
    def for_request(
        cls,
        *,
        request_id: str,
        intent: str,
        question: str,
        candidates: tuple[Candidate, ...],
        opened_at: datetime,
        case_id: str | None = None,
    ) -> "ConflictCase":
        """Question Request 한 건에 귀속되는 open Case 생성 관문."""
        from agent_org_network.request_correlation import require_request_id

        correlated_request_id = require_request_id(request_id)
        if case_id is None:
            return cls(
                intent=intent,
                question=question,
                candidates=candidates,
                opened_at=opened_at,
                request_id=correlated_request_id,
            )
        return cls(
            intent=intent,
            question=question,
            candidates=candidates,
            opened_at=opened_at,
            case_id=case_id,
            request_id=correlated_request_id,
        )

    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(c.agent_id for c in self.candidates)

    def involves_owner(self, owner_id: str) -> bool:
        return any(c.owner == owner_id for c in self.candidates)

    def resolve(self, resolution: Resolution) -> "ConflictCase":
        """합의 결론을 안은 resolved 케이스를 새로 만든다(case_id·후보 보존)."""
        return replace(
            self,
            status="resolved",
            resolution=resolution,
            manager_item_id=self.manager_item_id if self.status == "escalated" else None,
            decline_reason=None,
        )

    def resolve_for_request(self, primary: str, rationale: str = "") -> "ConflictCase":
        if self.request_id is None:
            raise ValueError("resolve_for_request는 request-aware ConflictCase 전용입니다.")
        if self.status not in ("open", "escalated"):
            raise ValueError("open 또는 escalated ConflictCase만 resolved로 전이할 수 있습니다.")
        if primary not in self.candidate_ids():
            raise ValueError("request-aware Resolution primary는 원 후보여야 합니다.")
        return self.resolve(Resolution(intent=self.intent, primary=primary, rationale=rationale))

    def advance_concurrence_round(self) -> "ConflictCase":
        if self.status != "open":
            raise ValueError("open ConflictCase만 다음 concurrence round로 갈 수 있습니다.")
        return replace(self, concurrence_round=self.concurrence_round + 1)

    def escalate(self, manager_item_id: str) -> "ConflictCase":
        if self.status != "open":
            raise ValueError("open ConflictCase만 escalated로 전이할 수 있습니다.")
        return replace(self, status="escalated", manager_item_id=manager_item_id)

    def decline(self) -> "ConflictCase":
        if self.status != "escalated":
            raise ValueError("escalated ConflictCase만 declined로 전이할 수 있습니다.")
        return replace(self, status="declined", decline_reason="manager_declined")


# ── 1인칭 합의 액션: ConcurOnPrimary ──────────────────────────────────
#
# "1인칭"의 핵심: 각 후보 Owner가 자기 화면에서 자기 입장을 낸다. MVP 최소
# 단순화안 — 후보 중 한 명을 primary로 지목하는 한 표(Concur)를 후보 Owner가
# 던진다. claim("내가 맡는다" = 자기 카드를 지목)도 concede("쟤가 맡아" = 남을
# 지목)도 모두 "primary는 누구"라는 같은 한 축의 표로 환원된다. 전원이 같은
# agent_id를 지목하면 합의 성립 → Resolution. (찬반 2축·라운드·코멘트 스레드는
# 후순위; 지금 필요한 건 "전원이 한 명을 가리켰나"뿐.)


# concede stance(ADR 0038 결정 3) — 진 후보 owner가 자기 지식의 상보 관련성을
# 자기보고하는 신호. "withdraw"(기본·엣지 없음)와 "keep_as_complement"(양성 신호·
# ComplementEdge 방출) 두 값. 개념상 concede 표(자기 카드가 아닌 남을 지목한 표)를
# 겨냥하지만, **코드는 claim/concede를 구분해 가드하지 않는다** — `_emit_complement_
# edges`는 Agreed 불변식(전원이 같은 agent_id를 지목)에 기대어 *진 후보 카드마다*
# 그 카드 owner가 이 케이스에 던진 단일 표의 stance를 그대로 적용한다(owner가 이
# 다툼에서 primary 카드와 진 카드를 동시에 소유해도 같은 규칙 — 그 owner의 단일
# 표가 두 카드 관계를 스스로 선언하는 셈이라 무해, `test_owner가_같은_다툼에서_
# primary_카드와_진_카드를_모두_소유하면_stance로_엣지방출된다`가 이 경계를 고정).
# 기본값이 withdraw라 기존 생성처는 100% 무영향(회귀 0).
ConcessionStance = Literal["withdraw", "keep_as_complement"]


@dataclass(frozen=True)
class ConcurOnPrimary:
    """후보 Owner 한 명의 1인칭 합의 표.

    `by_owner`(표를 던진 Owner User.id) 가 `on_agent`(primary로 지목한 카드의
    agent_id) 를 담당으로 지목한다. rationale은 합의 근거(선택).
    by_owner는 그 케이스의 후보 Owner여야 유효(해소 서비스가 강제).

    `stance`(ADR 0038 결정 3): 진 후보 owner가 자기 카드의 상보 관련성을
    자기보고하는 신호. 개념상 concede 표(자기 카드가 아닌 다른 primary를 지목한
    표)를 겨냥하지만, **코드가 claim/concede를 구분해 가드하지는 않는다** —
    `ConsensusService._emit_complement_edges`가 Agreed 성립 후 *진 후보 카드마다*
    그 카드 owner가 이 케이스에 던진 단일 표의 stance를 그대로 읽는다(owner가
    이 다툼에서 primary 카드와 진 카드를 동시에 소유해도 동일 규칙). 기본
    `"withdraw"` = 상보 엣지 없음(안전 기본). `"keep_as_complement"` = "내 관점은
    계속 필요"라는 양성 선언 — 이때만 `ComplementEdge`가 방출된다(Option A: 진
    owner 단독 선언, 이긴 front 수락 불요).
    """

    by_owner: str
    on_agent: str
    rationale: str = ""
    stance: ConcessionStance = "withdraw"


class ConflictCaseStore(Protocol):
    """open ConflictCase 보관·조회 포트 — 처리함의 데이터 원천.

    audit(`AuditLog`)·판례(`PrecedentStore`)와 같은 포트 패턴(Protocol +
    InMemory 구현). `open_for_owner`가 Owner별 처리함(자기 카드가 후보로 걸린
    open 케이스) 조회. 전이 ≠ 기록 — 여긴 미해소 도메인 상태를 보관하는 곳이지
    절차 기록(AuditLog)이 아니다.
    """

    def open_case(self, case: ConflictCase) -> None: ...

    def get(self, case_id: str) -> ConflictCase | None: ...

    def open_for_owner(self, owner_id: str) -> list[ConflictCase]: ...

    def open_for_intent(self, intent: str) -> ConflictCase | None: ...

    def mark_resolved(self, case: ConflictCase) -> None: ...


class RequestAwareConflictCaseStore(Protocol):
    """Request-first 경로가 쓰는 요청별 원자 생성·조회 보조 포트.

    기존 ``ConflictCaseStore``의 intent 단위 legacy 계약을 넓히지 않는다.
    ``get_by_request``는 open/resolved 여부와 무관하게 마지막 저장본을 반환한다.
    """

    def create_or_get_for_request(
        self,
        case: ConflictCase,
    ) -> tuple[ConflictCase, bool]: ...

    def get_by_request(self, request_id: str) -> ConflictCase | None: ...

    def get_request_case(self, case_id: str) -> ConflictCase | None: ...


def _conflict_request_fingerprint(
    case: ConflictCase,
) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
    """생성 ID·시각·상태를 제외한 request-derived Case payload."""
    from agent_org_network.request_correlation import require_request_id

    request_id = require_request_id(case.request_id)
    candidates = tuple((candidate.agent_id, candidate.owner) for candidate in case.candidates)
    return request_id, case.intent, case.question, candidates


class InMemoryConflictCaseStore:
    """append-only 정신의 in-memory 처리함 저장소.

    open 케이스는 `_open`(case_id 색인)에 둔다. resolved되면 `_open`에서 빼
    `history`(append-only)에 결말을 남긴다 — 처리함 목록은 open만, 이력은 전부.
    동일 intent의 중복 open 방지를 위해 `open_for_intent`로 먼저 조회한다.
    """

    workflow_durability: Literal["ephemeral", "durable"] = "ephemeral"

    def __init__(self) -> None:
        self._open: dict[str, ConflictCase] = {}
        self._by_request: dict[str, ConflictCase] = {}
        self._request_by_case: dict[str, str] = {}
        self._history: list[ConflictCase] = []
        self._lock = RLock()

    @property
    def history(self) -> list[ConflictCase]:
        with self._lock:
            return deepcopy(self._history)

    def open_case(self, case: ConflictCase) -> None:
        canonical = deepcopy(case)
        if canonical.request_id is not None:
            raise ValueError(
                "request-aware ConflictCase는 create_or_get_for_request로만 생성합니다."
            )
        with self._lock:
            existing_request = self._request_by_case.get(canonical.case_id)
            if existing_request is not None:
                raise ValueError("ConflictCase.case_id가 request-aware Case와 충돌합니다.")
            self._open[canonical.case_id] = deepcopy(canonical)
            self._history.append(deepcopy(canonical))

    def get(self, case_id: str) -> ConflictCase | None:
        with self._lock:
            case = self._open.get(case_id)
            return None if case is None else deepcopy(case)

    def open_for_owner(self, owner_id: str) -> list[ConflictCase]:
        with self._lock:
            return [deepcopy(c) for c in self._open.values() if c.involves_owner(owner_id)]

    def open_for_intent(self, intent: str) -> ConflictCase | None:
        with self._lock:
            for c in self._open.values():
                if c.request_id is None and c.intent == intent:
                    return deepcopy(c)
        return None

    def mark_resolved(self, case: ConflictCase) -> None:
        canonical = deepcopy(case)
        if canonical.request_id is not None:
            raise ValueError(
                "request-aware ConflictCase는 generation-bound claim으로만 전이합니다."
            )
        if canonical.status != "resolved" or canonical.resolution is None:
            raise ValueError("legacy mark_resolved는 resolved ConflictCase만 받습니다.")
        with self._lock:
            current = self._open.get(canonical.case_id)
            if current is not None and current.request_id is not None:
                raise ValueError(
                    "request-aware ConflictCase는 generation-bound claim으로만 전이합니다."
                )
            self._open.pop(canonical.case_id, None)
            self._history.append(deepcopy(canonical))

    def create_or_get_for_request(
        self,
        case: ConflictCase,
    ) -> tuple[ConflictCase, bool]:
        """요청별 Case를 한 번만 만들고 semantic 재시도만 멱등 수용한다."""
        from agent_org_network.request_correlation import LinkedEntityMismatchError

        canonical = deepcopy(case)
        fingerprint = _conflict_request_fingerprint(canonical)
        request_id = fingerprint[0]
        candidate_pairs = fingerprint[3]
        if len(candidate_pairs) != len(set(candidate_pairs)):
            raise ValueError("request-aware ConflictCase 후보는 중복될 수 없습니다.")
        with self._lock:
            existing = self._by_request.get(request_id)
            if existing is not None:
                if _conflict_request_fingerprint(existing) != fingerprint:
                    raise LinkedEntityMismatchError(
                        f"Question Request {request_id!r}의 ConflictCase payload가 다릅니다."
                    )
                return deepcopy(existing), False
            if (
                canonical.status != "open"
                or canonical.resolution is not None
                or canonical.concurrence_round != 1
            ):
                raise ValueError("request-aware ConflictCase 생성은 open 원형만 받습니다.")
            owner_request = self._request_by_case.get(canonical.case_id)
            if owner_request is not None and owner_request != request_id:
                raise LinkedEntityMismatchError(
                    f"ConflictCase.case_id가 다른 Request와 충돌합니다: {canonical.case_id!r}"
                )
            if any(
                stored.case_id == canonical.case_id and stored.request_id != request_id
                for stored in self._history
            ):
                raise LinkedEntityMismatchError(
                    f"ConflictCase.case_id가 다른 Request와 충돌합니다: {canonical.case_id!r}"
                )
            self._open[canonical.case_id] = deepcopy(canonical)
            self._by_request[request_id] = deepcopy(canonical)
            self._request_by_case[canonical.case_id] = request_id
            self._history.append(deepcopy(canonical))
            return deepcopy(canonical), True

    def get_by_request(self, request_id: str) -> ConflictCase | None:
        from agent_org_network.request_correlation import require_request_id

        correlated_request_id = require_request_id(request_id)
        with self._lock:
            case = self._by_request.get(correlated_request_id)
            return None if case is None else deepcopy(case)

    def get_request_case(self, case_id: str) -> ConflictCase | None:
        with self._lock:
            request_id = self._request_by_case.get(case_id)
            if request_id is None:
                return None
            case = self._by_request.get(request_id)
            return None if case is None else deepcopy(case)

    def _current_request_case_unlocked(self, case_id: str) -> ConflictCase | None:
        request_id = self._request_by_case.get(case_id)
        if request_id is None:
            return None
        return self._by_request.get(request_id)

    def _replace_request_case_unlocked(self, target: ConflictCase) -> ConflictCase:
        request_id = target.request_id
        if request_id is None:
            raise ValueError("request-aware ConflictCase 전이에 request_id가 필요합니다.")
        current = self._current_request_case_unlocked(target.case_id)
        if current is None:
            raise ValueError("request-aware ConflictCase를 찾을 수 없습니다.")
        if (
            current.request_id != request_id
            or current.intent != target.intent
            or current.question != target.question
            or current.candidates != target.candidates
            or current.opened_at != target.opened_at
        ):
            raise ValueError("ConflictCase 전이 target이 저장 원형과 다릅니다.")
        canonical = deepcopy(target)
        self._by_request[request_id] = canonical
        if canonical.status == "open":
            self._open[canonical.case_id] = canonical
        else:
            self._open.pop(canonical.case_id, None)
        self._history.append(deepcopy(canonical))
        return deepcopy(canonical)


# ── 합의 시도의 결과: ConsensusOutcome ────────────────────────────────
#
# 후보 Owner들의 표(ConcurOnPrimary)를 모아 합의를 시도한 결과. "타입이 곧
# 상태"(RoutingDecision·OrgReply 정신) — 세 결말 중 하나다.
#   - Agreed:     전원이 같은 agent_id 지목 → Resolution 산출, 케이스 closed.
#   - StillOpen:  아직 표가 덜 모였다(미완) → 케이스 open 유지, 처리함에 남음.
#   - Deadlocked: 표가 갈렸다(교착) → 합의 실패 자리. Manager escalation은
#                 T5.2(Manager 큐) 영역이라 여기선 *상태만* 남기고 처리는 미룬다.


@dataclass(frozen=True)
class Agreed:
    resolution: Resolution
    precedent: Precedent


@dataclass(frozen=True)
class StillOpen:
    case: ConflictCase
    pending_owners: tuple[str, ...]  # 아직 표를 안 던진 후보 Owner들


@dataclass(frozen=True)
class Deadlocked:
    case: ConflictCase
    reason: str = ""  # T5.2에서 Manager 큐로 넘길 때 근거로 쓴다


ConsensusOutcome = Agreed | StillOpen | Deadlocked


class ConsensusService:
    """후보 Owner들의 표(ConcurOnPrimary)를 모아 합의를 시도하는 도메인 서비스.

    Authority는 중앙(표→Resolution→Precedent). 카드 자기보고 금지.
    표 누적은 서비스 내부 상태(_votes)에 둔다 — ConflictCase는 단순 유지.

    `edge_store`(ADR 0038 결정 2, 옵셔널 주입): 주입되면 `Agreed` 분기에서
    `Precedent`(라우팅 학습) 곁에 `ComplementEdge`(접지 학습)도 방출한다(진 후보
    owner가 `stance="keep_as_complement"`로 명시 선언한 경우만 — 결정 3). 기본
    `None`이면 방출 로직 자체가 안 돈다(회귀 0 — 기존 생성처·테스트 100% 무영향).
    """

    def __init__(
        self,
        case_store: ConflictCaseStore,
        precedents: PrecedentStore,
        edge_store: EdgeStore | None = None,
    ) -> None:
        self._case_store = case_store
        self._precedents = precedents
        self._edge_store = edge_store
        self._votes: dict[str, dict[str, ConcurOnPrimary]] = {}

    def concur(self, case_id: str, vote: ConcurOnPrimary) -> ConsensusOutcome:
        case = self._case_store.get(case_id)
        if case is None:
            raise ValueError(f"미존재 case: {case_id!r}")
        if case.request_id is not None:
            raise ValueError(
                "request-aware ConflictCase는 P17ConflictDispositionApplication으로 처리합니다."
            )
        if not case.involves_owner(vote.by_owner):
            raise ValueError(f"후보 owner 아님: {vote.by_owner!r}")

        if case_id not in self._votes:
            self._votes[case_id] = {}
        self._votes[case_id][vote.by_owner] = vote

        candidate_owners = tuple(dict.fromkeys(c.owner for c in case.candidates))
        current_votes = self._votes[case_id]
        pending = tuple(o for o in candidate_owners if o not in current_votes)

        if pending:
            return StillOpen(case=case, pending_owners=pending)

        targets = set(v.on_agent for v in current_votes.values())
        if len(targets) > 1:
            return Deadlocked(case=case, reason=f"표 갈림: {targets}")

        primary = next(iter(targets))
        rationale = "; ".join(f"{o}→{v.on_agent}" for o, v in current_votes.items())
        resolution = Resolution(intent=case.intent, primary=primary, rationale=rationale)
        precedent = self._precedents.record(resolution)
        resolved_case = case.resolve(resolution)
        self._case_store.mark_resolved(resolved_case)
        if self._edge_store is not None:
            self._emit_complement_edges(case, resolution, current_votes)
        return Agreed(resolution=resolution, precedent=precedent)

    def _emit_complement_edges(
        self,
        case: ConflictCase,
        resolution: Resolution,
        votes: dict[str, ConcurOnPrimary],
    ) -> None:
        """`Agreed` 직후 진 후보 카드마다 그 카드 owner가 던진 표의 stance를 보고
        상보 엣지를 방출한다(ADR 0038 결정 3 — `precedents.record` 바로 곁의 방출
        지점).

        primary 카드는 건너뛴다(자기 자신에겐 안 감). **claim/concede를 코드가
        구분해 가드하지 않는다** — Agreed 전제(전원이 같은 agent_id를 지목)에
        기대어, 각 진 후보 카드의 owner가 이 케이스에 던진 *단일* 표(`votes[owner]`,
        Agreed 전제상 반드시 존재)의 `stance`가 `"keep_as_complement"`일 때만
        `ComplementEdge(primary→supporting)`를 `edge_store`에 record한다. 한 owner가
        이 다툼에서 primary 카드와 진 카드를 동시에 소유해도 같은 규칙이 적용된다
        (그 owner의 단일 표가 두 카드 관계를 스스로 선언하는 셈 — 무해·의도된 경계
        동작, `test_owner가_같은_다툼에서_primary_카드와_진_카드를_모두_소유하면_
        stance로_엣지방출된다`가 고정). 기본 `"withdraw"`면 방출 안 함.
        """
        assert self._edge_store is not None
        for candidate in case.candidates:
            if candidate.agent_id == resolution.primary:
                continue
            vote = votes.get(candidate.owner)
            if vote is None or vote.stance != "keep_as_complement":
                continue
            self._edge_store.record(
                ComplementEdge(
                    intent=case.intent,
                    primary_id=resolution.primary,
                    supporting_id=candidate.agent_id,
                )
            )
