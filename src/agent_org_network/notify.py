"""실시간 충돌 푸시 통지 — NotificationChannel 포트 + Notification + Notifier (T7.4, ADR 0022).

**이 모듈은 shape(미구현 통과 stub)다 — tdd-engineer가 red→green으로 채운다.**

지금 owner·manager는 처리함·큐를 *조회(pull)*해야 새 일을 안다. T7.4는 "새 일이 생기면
*push*로도 알린다"를 더한다 — **push는 pull을 대체하지 않고 *추가*한다**(ADR 0022 결정 6).
통지 채널이 통째로 실패해도 처리함 pull은 그대로라 미아가 없다(가시성 이중 보장 — 미아 없음은
push가 아니라 pull이 떠받친다). 발화는 *처리함/큐에 항목이 적재되는 사건*이다(다툼 open·백업
답 add·재평가 add·escalation enqueue).

ADR 0022:
- 결정 1: `NotificationChannel` 포트(Protocol) — `send(notification)`. 실 전송은 비결정·외부
  의존(Slack API·SMTP·MCP)이라 `GitGateway`·`OidcProvider`·`AgentRuntime`과 **같은 포트
  패턴**: 결정론 `FakeChannel`(메모리 inbox·전달 로그·게이트 내) + 실 어댑터(`SlackChannel`·
  `EmailChannel`·`McpChannel` — 게이트 밖·`NotImplementedError`·새 의존성은 후속 판단).
  채널 중립 — 어떤 채널도 1급 아님(첫 후보 MCP 알림이나 게이트 밖이라 지금 안 정함).
- 결정 2: `Notification`(frozen 값 객체) — recipient_id·kind·subject_ref·created_at. `kind`는
  `Literal`(필드 구조 동일·분기 없는 라벨이라 sealed sum 아님 — `ReevalItem.subject_kind`와
  같은 판단). 운영 면 신호 — 실 사용자 채팅엔 통지 0(OrgReply는 통지를 모름).
- 결정 3: 구독 = recipient → channel 매핑. MVP는 `Notifier`가 주입 맵을 든다(별 store 불요 —
  정적 매핑이라 전이 없음). 미구독 recipient는 skip(처리함 pull 그대로 — 미아 없음).
- 결정 5: 전달 보장 = 멱등(`(recipient_id, kind, subject_ref)` 중복 발송 안 함) + at-least-once.
  분산 인프라(ADR 0011·0012)는 **코드 재사용 아니라 *패턴*(멱등 `ticket_id` 정신)만** 따른다 —
  통지는 fire-and-forget push라 작업 디스패치(round-trip·claim/submit·큐 상태기계)와 다른 도메인.

결정론 경계(ADR 0003·0022 결정 1): `FakeChannel`은 메모리 inbox·전달 로그라 게이트에서 돈다
(`FakeGitGateway`의 결정 SHA·`FakeOidcProvider`의 in-memory 토큰 맵과 같은 결). 실 어댑터는
실 네트워크·전송이라 게이트 밖(수동 시연).

발화 지점(ADR 0022 결정 4): 각 적재 지점(ConflictCase open·BackupReview add·Reeval add·Manager
enqueue)에 `notifier: Notifier | None = None` 옵셔널 주입 — None이면 기존 동작(하위호환·게이트
보존), 비None이면 적재 직후 `notifier.notify(notification)` 1회. `commit_okf_bundle(...,
propagator=None)`(ADR 0019)과 동형. 단일 발화 추상 = `Notifier.notify` 인터페이스 하나(한 함수
아님 — 발화 맥락이 제각각이라 인터페이스만 통일). store는 순수 보관(전이 ≠ 기록 ≠ 통지).

전이 ≠ 기록 ≠ 통지:
  - store(conflict.py·review.py·reeval.py·manager_queue.py) — 미해소 상태의 도메인 보관소(전이).
  - `AuditLog`(audit.py) — 절차 기록(기록).
  - 통지(이 모듈) — 적재 *뒤*의 외부 부작용(알림). 전달 로그는 audit과 *별 축*.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel

# ── 통지 종류: NotificationKind (Literal — sealed sum 아님) ──────────────────
#
# 어느 처리함/큐 적재가 이 통지를 낳았나. 네 종류가 *필드 구조가 같다*(전부 recipient·
# subject_ref·created_at만 든다) — 종류는 분기 없는 *라벨*이라 Literal이 정확하다(ADR 0022
# 결정 2). sealed sum(RoutingDecision·ReevalOutcome)은 각 변이가 자기 필드를 다르게 들 때·
# *처분*(행위 선택)에 쓴다. 통지는 처분이 아니라 *사건 라벨*이라 Literal — ReevalItem.
# subject_kind 문자열 판별자와 같은 판단. 새 종류는 여기 더하고 발화 지점에서 채운다.

NotificationKind = Literal[
    "conflict_opened",  # ConflictCase open(다툼 적재 — ask_org Contested arm)
    "backup_review_added",  # BackupReviewItem add(백업 답 검토 적재 — dispatcher submit)
    "reeval_flagged",  # ReevalItem add(재평가 적재 — StalenessPropagator, ADR 0019)
    "manager_escalated",  # ManagerItem enqueue(escalation 적재 — ask_org/ConsensusService)
]


# ── 통지 값 객체: Notification ─────────────────────────────────────────────
#
# 통지 한 건. 운영 면(owner/manager) 신호 — "처리할 게 생겼다". 실 사용자 채팅엔 통지 0
# (OrgReply는 통지를 모름·노출 불변식, ADR 0022 결정 2·7). 식별자만 들고 본문 렌더는 어댑터
# 책임(사용자向 비밀 누설 차단 — WorkTicket이 카드 본문 아니라 식별자만 드는 정신).


class Notification(BaseModel, frozen=True):
    """처리함/큐 적재 사건의 한 통지 — 운영 면(owner/manager)으로 가는 push 신호(ADR 0022 결정 2).

    필드:
      - `recipient_id` — owner 또는 manager의 User.id(귀속 키 — 누구에게 가는 통지인가).
      - `kind` — 통지 종류(NotificationKind — 어느 적재가 이 통지를 낳았나).
      - `subject_ref` — 어느 항목인가(case_id / item_id / intent 등 적재된 항목 식별자).
        멱등 키의 일부(결정 5)이자 수신자가 "무엇 때문에"를 잇는 손잡이.
      - `created_at` — 주입 clock(결정론 — `ReevalItem.flagged_at`·`ConflictCase.opened_at`과
        같은 결).

    노출 불변식: 운영 면 신호라 운영 내부값(case_id·item_id·intent)은 OK이되, 사용자向 채팅엔
    통지 0이고 본문에 사용자向 비밀이 새지 않게 식별자만 든다(본문 렌더는 어댑터 책임).
    frozen 값 객체이지 전송 와이어 DTO가 아니다(`OidcClaims`가 전송 DTO 아닌 경계와 동형).
    """

    recipient_id: str
    kind: NotificationKind
    subject_ref: str
    created_at: datetime


# ── 통지 채널 포트: NotificationChannel ────────────────────────────────────
#
# 한 수신자에게 한 통지를 전달하는 최소 포트. 실 전송은 비결정·외부 의존(Slack API·SMTP·MCP)
# 이라 `GitGateway`·`OidcProvider`·`AgentRuntime`과 같은 포트 패턴으로 격리(ADR 0022 결정 1).
# 채널 중립 — 어떤 채널도 1급 아님(포트가 채널 어휘를 안 가정·recipient → 실 주소 변환은 어댑터).


class NotificationChannel(Protocol):
    """한 통지를 한 수신자에게 전달하는 최소 포트(ADR 0022 결정 1).

    `send(notification) -> None` — fire-and-forget(전달 결과를 반환하지 않는다·전송 실패는
    채널 내부 재시도/미전달 처리 자리, 결정 5). 도메인은 "쐈다"까지만 본다. 실 전송은
    *불투명한 외부 부작용*이라 포트로 가린다 — 결정론 `FakeChannel`(메모리 inbox)과 실 어댑터
    (Slack/Email/MCP — 게이트 밖)가 같은 포트의 다른 구현(`GitGateway`↔`FakeGitGateway`와 동형).
    """

    def send(self, notification: Notification) -> None:
        """한 통지를 그 수신자(`notification.recipient_id`)에게 전달한다(fire-and-forget)."""
        ...


class FakeChannel:
    """결정론 in-memory `NotificationChannel` — 게이트(단위 테스트)에서 돈다(ADR 0022 결정 1).

    실 네트워크·시각·랜덤 0 — 메모리 inbox(`recipient_id → list[Notification]`)에 쌓고 전달
    로그(append-only)를 든다. 테스트가 "이 적재 사건이 이 수신자에게 이 통지를 쐈나"를 inbox
    조회로 검증한다(`FakeGitGateway`의 결정 SHA·`FakeOidcProvider`의 토큰 맵과 같은 결).

    **현재는 shape stub(미구현)** — 실 적재·조회 본문은 tdd-engineer가 red→green으로 채운다.
    """

    def __init__(self) -> None:
        # recipient_id → 그 수신자에게 전달된 통지들(메모리 inbox·결정론 검증 원천).
        self.inbox: dict[str, list[Notification]] = {}
        # 전달 로그(append-only — "통지가 나갔나"의 운영 신호, audit과 별 축).
        self.delivered: list[Notification] = []

    def send(self, notification: Notification) -> None:
        if notification.recipient_id not in self.inbox:
            self.inbox[notification.recipient_id] = []
        self.inbox[notification.recipient_id].append(notification)
        self.delivered.append(notification)

    def for_recipient(self, recipient_id: str) -> list[Notification]:
        """그 수신자에게 전달된 통지들(테스트 조회 — inbox 투영)."""
        return self.inbox.get(recipient_id, [])


class SlackChannel:
    """실 Slack 알림 `NotificationChannel` — **게이트 밖 수동 시연**(ADR 0022 결정 1·8).

    Slack API로 recipient(owner/manager)에게 알림을 보낸다(실 네트워크·비결정 — `SubprocessGitGateway`·
    `HttpOidcProvider`와 같은 결). recipient_id → Slack 채널/DM 변환은 이 어댑터 안에서.
    새 무거운 의존성(Slack SDK)을 더할지는 **tdd-engineer/후속이 판단**한다 — 이 shape는
    자리만(`NotImplementedError`). 채널 중립 — Slack은 1급이 아니라 어댑터 중 하나다.

    **현재는 shape stub(미구현)** — 실 본문은 후속(게이트 밖·수동).
    """

    def send(self, notification: Notification) -> None:
        raise NotImplementedError("실 Slack 알림 — 게이트 밖 수동 시연(T7.4 후속)")


class EmailChannel:
    """실 이메일(SMTP) 알림 `NotificationChannel` — **게이트 밖 수동 시연**(ADR 0022 결정 1·8).

    SMTP로 recipient에게 메일을 보낸다(실 네트워크·비결정). recipient_id → 이메일 주소 변환은
    이 어댑터 안에서(User.email 재사용 가능 — ADR 0021). 새 의존성(SMTP 라이브러리)은 후속 판단.

    **현재는 shape stub(미구현)** — 실 본문은 후속(게이트 밖·수동).
    """

    def send(self, notification: Notification) -> None:
        raise NotImplementedError("실 이메일(SMTP) 알림 — 게이트 밖 수동 시연(T7.4 후속)")


class McpChannel:
    """실 MCP 알림 `NotificationChannel` — **게이트 밖 수동 시연**(ADR 0022 결정 1·8).

    MCP 알림으로 recipient에게 보낸다. 제품이 MCP 서버라 외부 의존 0이 강점(첫 실 어댑터 후보)
    이나 게이트 밖이라 지금 안 정한다(ADR 0022 결정 1·8). 새 의존성은 후속 판단.

    **현재는 shape stub(미구현)** — 실 본문은 후속(게이트 밖·수동).
    """

    def send(self, notification: Notification) -> None:
        raise NotImplementedError("실 MCP 알림 — 게이트 밖 수동 시연(T7.4 후속)")


# ── 통지 서비스: Notifier ──────────────────────────────────────────────────
#
# 발화 지점이 `notify(notification)`만 부르고, Notifier가 구독 맵(recipient → channel)으로
# 채널을 찾아 `channel.send`한다(ADR 0022 결정 3). 멱등: 같은 (recipient, kind, subject_ref)는
# 중복 발송 안 함(결정 5 — 분산 `ticket_id` 멱등 정신, 코드 재사용 아닌 *패턴*만). 미구독
# recipient는 skip(처리함 pull 그대로 — 미아 없음·결정 6).


class Notifier:
    """처리함/큐 적재 사건을 받아 구독 채널로 통지를 push하는 도메인 서비스(ADR 0022 결정 3·5).

    발화 지점(각 적재 지점에 `notifier: Notifier | None = None` 옵셔널 주입·결정 4)이 적재
    직후 `notify(notification)`를 1회 부른다 — `commit_okf_bundle(..., propagator=None)`과 동형.

    구독(결정 3): `subscriptions`(recipient_id → channel) 주입 맵. MVP는 별 `SubscriptionStore`
    없이 정적 맵(전이 없는 데이터라 store 불요 — 동적 구독은 후속 승격). 미구독 recipient는
    skip(그 recipient에 채널 없으면 조용히 건너뜀 — 처리함 pull 그대로라 미아 없음, 결정 6).

    멱등(결정 5): 이미 보낸 `(recipient_id, kind, subject_ref)` 키는 다시 안 보낸다(같은 항목에
    발화가 두 번 와도 통지 한 번 — `StalenessPropagator`의 needs_review 가드·answer dedup,
    `ManagerQueueStore.get_by_case` 중복 방지와 동형 멱등). 분산 `ticket_id` 멱등의 *패턴*만
    따른다(코드 재사용 아님 — 통지는 fire-and-forget push라 작업 디스패치와 다른 도메인).
    """

    def __init__(self, subscriptions: dict[str, NotificationChannel] | None = None) -> None:
        # recipient_id → 그 수신자의 통지 채널(정적 구독 맵·결정 3).
        self._subscriptions: dict[str, NotificationChannel] = dict(subscriptions or {})
        # 이미 보낸 멱등 키 (recipient_id, kind, subject_ref) — 중복 발송 차단(결정 5).
        self._sent: set[tuple[str, NotificationKind, str]] = set()

    def notify(self, notification: Notification) -> None:
        """한 적재 사건의 통지를 구독 채널로 push한다(멱등·미구독 skip — ADR 0022 결정 3·5).

        절차: ① 멱등 — `(recipient_id, kind, subject_ref)`가 이미 보냈으면 skip ② 구독 조회 —
        recipient에 채널이 없으면 skip(처리함 pull 그대로·미아 없음) ③ `channel.send` 1회 +
        멱등 키 기록. 전송 실패(채널이 던짐)는 삼키고 전달 로그에 실패로 남긴다(MVP fire-and-
        forget — 실 재시도/dead-letter는 게이트 밖, 결정 5·8).

        **현재는 shape stub(미구현)** — 본문은 tdd-engineer가 red→green으로 채운다.
        """
        key = (notification.recipient_id, notification.kind, notification.subject_ref)
        if key in self._sent:
            return
        channel = self._subscriptions.get(notification.recipient_id)
        if channel is None:
            return
        try:
            channel.send(notification)
        except Exception:
            return
        self._sent.add(key)
