"""Adoption Metrics — 채택 성공기준 3축 + 선행 사망신호 (Phase 14).

**상태: 구현 완료(green·2026-07-08).**

근거 SSOT:
  - ADR 0039 결정 5 — 채택 성공기준 3축 신설(owner 자발 유지·사람 개입 없는 종결·
    논쟁 판례 종결). 이 ADR이 방향을 박고 수치는 PRD §8이 확정.
  - PRD §8 "채택 기준" — 확정 수치(owner 유지 3명·개입 없는 종결 ≥70%·논쟁 판례
    종결 3건 + 재논쟁 0·선행 사망신호 = N일 이상 안 닫힌 Contested).
  - ADR 0035(Owner Scorecard) — "읽기 파생·라우팅/실행 무변경·Goodhart 방지"
    패턴을 그대로 재사용한다(scorecard.py 형태 정합).

이건 **읽기 파생 투영**이다(전이 ≠ 기록 — 새 전이·새 기록 0). 기존 append-only
기록(감사 로그·`PrecedentStore`·`ConflictCaseStore`·`PresenceLogStore`)을 순수
함수로 조인해 3축 + 선행지표를 낸다. 라우팅·실행·admission·Authority 무변경 —
지표는 조직이 실제로 쓰는가를 *관찰*할 뿐 라우팅을 바꾸지 않는다.

── Goodhart 방지(ADR 0035 정신의 채택판) ──────────────────────────────
  - 축2(개입 없는 종결)는 **사후 정정(`CorrectionEvent`)을 "개입 있는 종결"로
    치지 않는다**. 정정은 이미 발신된(자동 파이프라인이 종결한) 답에 대한 *사후
    감독*이지 종결 시점 개입이 아니다. 정정을 개입으로 세면 정정률이 채택 지표를
    깎아 "고치면 채택 점수 떨어지니 안 고친다"는 역유인이 생긴다 — ADR 0035 결정
    1(정정=가점·품질 벌점은 bad 피드백에서만)의 정확한 재적용. 개입 판정은 **종결
    시점 신호**(HITL 사전검토·escalation·합의)에서만 읽는다.
  - 축1(자발적 유지)은 관찰 도구이지 인사·보상 연동이 아니다(ADR 0035 결정 3 정신).
  - 저물량 왜곡 회피: 축2는 **비율 기본·절대건수 병기**(PRD §8 명시).
  - owner 간 순위표 없음(ADR 0035 결정 2 정신) — 채택은 조직 단위 집계다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from agent_org_network.audit import AuditReader
    from agent_org_network.conflict import ConflictCase, ConflictCaseStore, PrecedentStore
    from agent_org_network.presence import PresenceLogStore


# ── 집계 기간 ────────────────────────────────────────────────────────────


class AdoptionWindow(BaseModel, frozen=True):
    """채택 지표 집계 기간 — rolling 4주(28일) 기본(호출측이 주입 clock으로 시임).

    `ScorecardWindow`(scorecard.py)와 같은 형태지만 병렬 안전을 위해 독립 타입으로
    둔다(공유 모듈 무편집). PRD §8 파일럿 기간(4주)이 기본이나 함수는 임의 기간을
    받는다.
    """

    since: datetime
    until: datetime


# ── 외부결정 6 주입점: "자발적 유지"의 조작적 정의 ────────────────────────
#
# "자발적 유지"의 조작적 정의(로그인 주기? 인박스 처리? 프레즌스 온라인?)는
# **외부결정 6이라 미확정**이다(PRD §8·ADR 0039 결정 5). 임의 확정하지 않고
# 정의를 *주입 가능한 술어*로 둔다 — 사용자가 확정하면 그 정의를 구현해 주입한다.
#
# 후보 정의(애매 목록 — tdd/planner·사용자 확정 대기):
#   (a) 프레즌스 온라인    — `PresenceLogStore`로 그 주에 online 구간이 있었나
#                            (기존 수집 장치 재사용·새 장치 0).
#   (b) 인박스 처리        — 그 주에 `CorrectionEvent`(by_owner) 제출 또는 합의
#                            표(`ConcurOnPrimary`) 참여. 정정은 CorrectionStore로
#                            읽히나 합의 표는 현재 스토어 미영속(_votes in-memory)
#                            이라 축1의 이 축은 새 수집을 요구할 수 있음 — 명시.
#   (c) 지식 갱신          — 그 주에 published `KnowledgeIndex`/카드 synced_at 갱신.
#   (d) 로그인 주기        — 실 SSO 전이라 로그인 이력 원천 없음(약신원) — 미가용.
#
# 시그니처: (owner_id, week_since, week_until) -> "그 주에 활동했나". 함수가 4주를
# 7일 주간으로 슬라이스해 활동 주 수를 센다.
OwnerActivityPredicate = Callable[[str, datetime, datetime], bool]


# ── 축1 — owner 자발 유지 ────────────────────────────────────────────────


class OwnerRetentionMetric(BaseModel, frozen=True):
    """축1 — owner 자발 유지(PRD §8: 3~5명 중 3명이 4주 중 ≥2주 활동·제거 요청 0).

    `retained` 판정 = (활동 주 수 ≥ `min_active_weeks`) AND (제거 요청 없음).
    "활동"의 정의는 `OwnerActivityPredicate`가 주입한다(외부결정 6).

    제거 요청은 전용 스토어가 없다 — 소규모 파일럿(3~5명)에선 수동 관측으로 충분해
    `removal_requested_owner_ids`로 *주입*한다(새 수집 장치 0). 나중에 기록화하려면
    그때 신호를 신설(후속 연기·과설계 회피).
    """

    total_owners: int
    retained_owners: int
    retained_owner_ids: tuple[str, ...]
    active_weeks_by_owner: dict[str, int]
    removal_requested_owner_ids: tuple[str, ...]
    min_active_weeks: int


# ── 축2 — 사람 개입 없는 종결 ─────────────────────────────────────────────


class UnattendedClosureMetric(BaseModel, frozen=True):
    """축2 — 사람 개입 없이 담당 신뢰답으로 종결(PRD §8: **종결된 질문 중** ≥70%).

    분모(`total_questions`) = **종결에 도달한** 질문 총수(감사 로그의 질문 레코드 =
    `decision != None`인 것) 중 in-flight(`awaiting`)를 제외한 것. 분자 = 개입 없는
    종결(routed + delivered + mode=="full"). 비율이 기본, 절대건수 병기(저물량 왜곡
    회피·PRD §8).

    `in_flight`: 아직 종결되지 않은(`awaiting`) 질문 수를 분모와 별도로 계상한다 —
    최근 들어온 질문이 아직 처리 중이라는 이유로 비율을 저평가하지 않게 한다(PRD §8
    확정 — "종결된 질문 중" 문구). `awaiting`과 값이 같지만, `awaiting`은 종결
    시점 개입 분해의 한 항목(참고)이고 `in_flight`는 분모 제외 대상임을 명시하는
    별도 필드다.

    개입 분해(참고 — 위 분모에 포함되는 종결된 질문만 대상): `hitl_reviewed`
    (draft_only 사전검토)·`escalated`(Manager/root escalation)·`contested`(합의
    개입). 이들은 *종결 시점* 개입 신호다. 사후 정정(`CorrectionEvent`)은 개입으로
    세지 않는다(Goodhart 방지·모듈 docstring 참조).
    """

    total_questions: int
    unattended_closed: int
    unattended_rate: float
    hitl_reviewed: int
    escalated: int
    contested: int
    awaiting: int
    in_flight: int


# ── 축3 — 논쟁 판례 종결 + 재논쟁 0 ──────────────────────────────────────


class ContestedResolutionMetric(BaseModel, frozen=True):
    """축3 — 논쟁이 판례로 닫힘 + 그 intent 재논쟁 0(PRD §8: 3건 + 재논쟁 0).

    왕관 보석(논쟁 해소 루프·ADR 0039 결정 2)의 실작동 증거. 종결 = 합의
    (`ConsensusService`) *또는* Manager 백스톱(`manager_queue`)이 `Precedent`를
    record한 사건 — 둘 다 `PrecedentStore`에 남으므로 자연히 함께 집계된다(백스톱도
    유효 종결 경로·ADR 0014). 종결 시각은 `Precedent.recorded_at`.

    재논쟁 0 = 어떤 종결된 intent도 그 판례 record 이후 다시 Contested로 뜨지 않음.
    감사 로그에서 `decision.disposition=="contested"` 且 같은 intent 且 timestamp >
    recorded_at을 찾아 판정한다. (판례가 서면 라우터가 자동 적용하므로 정상 경로에선
    재논쟁이 안 나야 정상 — 재논쟁 발견 = 판례 무효화 후 재발 등 위험 신호.)
    """

    resolved_precedents: int
    resolved_intents: tuple[str, ...]
    re_contested_intents: tuple[str, ...]
    re_contest_free: bool


# ── 선행지표(R1) — N일 이상 안 닫힌 Contested ────────────────────────────


class AgingContested(BaseModel, frozen=True):
    """threshold를 넘긴 미종결 Contested 1건 — 정치 고임 조기 경보의 원소."""

    case_id: str
    intent: str
    opened_at: datetime
    open_days: float


class ContestedDeathSignal(BaseModel, frozen=True):
    """선행 사망신호 — "N일 이상 안 닫힌 Contested"(ADR 0039 결정 5·PRD §8 R1).

    정치 고임(합의가 안 닫히고 쌓임 = 제품 사망 경로·ADR 0039 정직한 리스크)의 조기
    경보. 열린 `ConflictCase`의 경과일 분포를 투영한다. `aging_contested`는 임계
    초과분만(경보 대상), `open_contested_total`·`max_open_days`는 전체 맥락.
    """

    threshold_days: int
    open_contested_total: int
    aging_contested: tuple[AgingContested, ...]
    max_open_days: float | None


# ── 최상위 집계 값 객체 ──────────────────────────────────────────────────


class AdoptionMetrics(BaseModel, frozen=True):
    """채택 성공기준 3축 + 선행지표의 읽기 파생 투영(ADR 0039 결정 5·PRD §8).

    `OwnerScorecard`(scorecard.py)와 형태 정합 — frozen 값 객체 조합 + 약신원 주석
    (ADR 0035 결정 4 정신: 실 SSO 전이라 질문자 신원이 약신원이므로 피드백 파생
    지표는 참고치).
    """

    window: AdoptionWindow
    retention: OwnerRetentionMetric
    unattended_closure: UnattendedClosureMetric
    contested_resolution: ContestedResolutionMetric
    death_signal: ContestedDeathSignal
    weak_identity_note: bool = True


# ── 순수 함수(shape·stub) ────────────────────────────────────────────────


def _slice_weeks(since: datetime, until: datetime) -> list[tuple[datetime, datetime]]:
    """[since, until)을 7일 주간으로 슬라이스한다(마지막 조각은 짧을 수 있음)."""
    week = timedelta(days=7)
    slices: list[tuple[datetime, datetime]] = []
    cursor = since
    while cursor < until:
        end = min(cursor + week, until)
        slices.append((cursor, end))
        cursor = end
    return slices


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


_QuestionCategory = Literal["unattended", "hitl_reviewed", "escalated", "contested", "awaiting"]


def _classify_question(record: dict[str, Any], decision: dict[str, Any]) -> _QuestionCategory:
    """감사 레코드 하나(질문)를 축2 5개 버킷 중 하나로 분류한다(모듈 조인 매핑 참조)."""
    disposition = decision.get("disposition")
    if disposition == "contested":
        return "contested"
    if disposition == "unowned":
        return "escalated"

    dispatch = record.get("dispatch")
    if dispatch is None:
        # routed인데 아직 dispatch가 안 남은 방어적 경로 — 미종결로 취급.
        return "awaiting"
    dispatch_disposition = dispatch.get("disposition")
    if dispatch_disposition == "awaiting_worker":
        return "awaiting"
    if dispatch_disposition == "escalated_to_manager":
        return "escalated"

    # delivered
    answer = record.get("answer")
    mode = answer.get("mode") if answer else "full"
    if mode == "full":
        return "unattended"
    return "hitl_reviewed"


def compute_adoption_metrics(
    *,
    owner_ids: list[str],
    audit_reader: "AuditReader",
    precedent_store: "PrecedentStore",
    conflict_store: "ConflictCaseStore",
    is_owner_active: OwnerActivityPredicate,
    presence_log: "PresenceLogStore | None" = None,
    removal_requested_owner_ids: frozenset[str] = frozenset(),
    window: AdoptionWindow,
    now: datetime,
    threshold_days: int,
    min_active_weeks: int = 2,
) -> AdoptionMetrics:
    """기존 append-only 기록을 조인해 채택 3축 + 선행지표를 낸다(읽기 파생·순수).

    ── 기록 원천 조인 매핑(무엇이 어디서 나오는가) ─────────────────────────
    축1 `retention`:
      - 활동: `is_owner_active`(외부결정 6 주입)를 4주 × 7일 슬라이스로 호출해 활동
        주 수 계산. 후보 구현이 `presence_log`(기존)를 읽으면 새 수집 장치 0.
      - 제거 요청: `removal_requested_owner_ids`(주입·기본 공집합) — 전용 스토어 없음
        (소규모 파일럿 수동 관측).
    축2 `unattended_closure`(전부 `audit_reader.records()` 한 원천 — 조인 키 문제 0):
      - 분모: `decision != None`인 레코드(질문) 중 **종결된 것만**(action 레코드는 제외,
        `awaiting`(in-flight)도 제외 — PRD §8 확정 "종결된 질문 중 ≥70%").
      - 분류: `decision.disposition`(routed/contested/unowned) + `dispatch.disposition`
        (delivered/awaiting_worker/escalated_to_manager) + `answer.mode`(full/draft_only).
      - 개입 없는 종결 = routed + delivered + mode=="full".
      - `awaiting`(dispatch 없음 또는 `awaiting_worker`)은 분모에서 빼고 `in_flight`로
        별도 계상한다(최근 질문이 비율을 저평가하지 않게).
      - 사후 정정은 세지 않음(Goodhart·모듈 docstring).
      - 기간: `datetime.fromisoformat(rec["timestamp"])` ∈ [since, until).
    축3 `contested_resolution`:
      - 종결: `precedent_store.list_all()` 중 `recorded_at` ∈ [since, until). 합의·
        Manager 백스톱 판례 모두 포함(둘 다 record → PrecedentStore).
      - 재논쟁: `audit_reader.records()` 중 contested·같은 intent·timestamp >
        해당 판례 recorded_at.
    선행지표 `death_signal`:
      - 열린 케이스: `{c.case_id: c for o in owner_ids for c in
        conflict_store.open_for_owner(o)}.values()` — 기존 포트만으로 전 open 케이스
        수집(새 read 메서드 0). Contested는 항상 candidates(owner)가 있으므로
        owner_ids가 파일럿 전체면 누락 없음. (파일럿 밖 owner가 후보인 케이스는
        누락될 수 있음 — 그럴 땐 `ConflictCaseStore.open_cases()` 신설 고려·후속.)
      - 경과일: `(now - case.opened_at).days`, threshold_days 초과분이 aging.

    `presence_log`는 외부결정 6이 확정될 때 `is_owner_active` 구현체가 참조할 자리로
    남겨둔다 — 이 함수 자체는 활동 판정을 `is_owner_active` 호출에 전량 위임하므로
    직접 읽지 않는다(주입점 유지·과설계 회피).
    """
    # ── 축1 — owner 자발 유지 ────────────────────────────────────────────
    weeks = _slice_weeks(window.since, window.until)
    active_weeks_by_owner: dict[str, int] = {}
    retained_owner_ids: list[str] = []
    for owner_id in owner_ids:
        active_weeks = sum(
            1 for week_since, week_until in weeks if is_owner_active(owner_id, week_since, week_until)
        )
        active_weeks_by_owner[owner_id] = active_weeks
        if owner_id in removal_requested_owner_ids:
            continue
        if active_weeks >= min_active_weeks:
            retained_owner_ids.append(owner_id)

    retention = OwnerRetentionMetric(
        total_owners=len(owner_ids),
        retained_owners=len(retained_owner_ids),
        retained_owner_ids=tuple(retained_owner_ids),
        active_weeks_by_owner=active_weeks_by_owner,
        removal_requested_owner_ids=tuple(sorted(removal_requested_owner_ids)),
        min_active_weeks=min_active_weeks,
    )

    # ── 축2 — 사람 개입 없는 종결 ─────────────────────────────────────────
    total_questions = 0
    unattended_closed = 0
    hitl_reviewed = 0
    escalated = 0
    contested = 0
    awaiting = 0
    all_records = audit_reader.records()
    for record in all_records:
        decision = record.get("decision")
        if decision is None:
            continue  # action 레코드(전이 없는 처분 절차 기록) — 질문 분모 제외
        timestamp = _parse_timestamp(record["timestamp"])
        if not (window.since <= timestamp < window.until):
            continue

        category = _classify_question(record, decision)
        if category == "awaiting":
            awaiting += 1
            continue  # in-flight — 분모 제외(PRD §8 확정: "종결된 질문 중" ≥70%)

        total_questions += 1
        if category == "unattended":
            unattended_closed += 1
        elif category == "hitl_reviewed":
            hitl_reviewed += 1
        elif category == "escalated":
            escalated += 1
        elif category == "contested":
            contested += 1

    unattended_closure = UnattendedClosureMetric(
        total_questions=total_questions,
        unattended_closed=unattended_closed,
        unattended_rate=(unattended_closed / total_questions) if total_questions else 0.0,
        hitl_reviewed=hitl_reviewed,
        escalated=escalated,
        contested=contested,
        awaiting=awaiting,
        in_flight=awaiting,
    )

    # ── 축3 — 논쟁 판례 종결 + 재논쟁 0 ────────────────────────────────────
    resolved_precedents = [
        precedent
        for precedent in precedent_store.list_all()
        if window.since <= precedent.recorded_at < window.until
    ]
    resolved_intents = tuple(precedent.resolution.intent for precedent in resolved_precedents)

    re_contested_intents: list[str] = []
    for precedent in resolved_precedents:
        intent = precedent.resolution.intent
        recorded_at = precedent.recorded_at
        for record in all_records:
            decision = record.get("decision")
            if decision is None or decision.get("disposition") != "contested":
                continue
            if record.get("intent") != intent:
                continue
            if _parse_timestamp(record["timestamp"]) > recorded_at:
                re_contested_intents.append(intent)
                break

    contested_resolution = ContestedResolutionMetric(
        resolved_precedents=len(resolved_precedents),
        resolved_intents=resolved_intents,
        re_contested_intents=tuple(re_contested_intents),
        re_contest_free=len(re_contested_intents) == 0,
    )

    # ── 선행지표(R1) — N일 이상 안 닫힌 Contested ─────────────────────────
    open_cases: dict[str, "ConflictCase"] = {}
    for owner_id in owner_ids:
        for case in conflict_store.open_for_owner(owner_id):
            open_cases[case.case_id] = case

    aging_contested: list[AgingContested] = []
    max_open_days: float | None = None
    for case in open_cases.values():
        open_days = (now - case.opened_at).total_seconds() / 86400
        if max_open_days is None or open_days > max_open_days:
            max_open_days = open_days
        if open_days > threshold_days:
            aging_contested.append(
                AgingContested(
                    case_id=case.case_id,
                    intent=case.intent,
                    opened_at=case.opened_at,
                    open_days=open_days,
                )
            )

    death_signal = ContestedDeathSignal(
        threshold_days=threshold_days,
        open_contested_total=len(open_cases),
        aging_contested=tuple(aging_contested),
        max_open_days=max_open_days,
    )

    return AdoptionMetrics(
        window=window,
        retention=retention,
        unattended_closure=unattended_closure,
        contested_resolution=contested_resolution,
        death_signal=death_signal,
    )


__all__ = [
    "AdoptionWindow",
    "OwnerActivityPredicate",
    "OwnerRetentionMetric",
    "UnattendedClosureMetric",
    "ContestedResolutionMetric",
    "AgingContested",
    "ContestedDeathSignal",
    "AdoptionMetrics",
    "compute_adoption_metrics",
]
