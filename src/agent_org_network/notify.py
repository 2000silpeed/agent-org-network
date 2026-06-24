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
  `Literal`(필드 구조 동일·분기 없는 라벨이라 sealed sum 아님 — 분기·페이로드가 종류마다
  갈리는 `ReevalSubject`[대상 축 sealed sum]와 달리 통지 종류는 단순 라벨이다).
  운영 면 신호 — 실 사용자 채팅엔 통지 0(OrgReply는 통지를 모름).
- 결정 3: 구독 = recipient → channel 매핑. MVP는 `Notifier`가 주입 맵을 든다(별 store 불요 —
  정적 매핑이라 전이 없음). 미구독 recipient는 skip(처리함 pull 그대로 — 미아 없음).
- 결정 5: 전달 보장 = 멱등(`(recipient_id, kind, subject_ref)` 중복 발송 안 함) + at-least-once.
  분산 인프라(ADR 0011·0012)는 **코드 재사용 아니라 *패턴*(멱등 `ticket_id` 정신)만** 따른다 —
  통지는 fire-and-forget push라 작업 디스패치(round-trip·claim/submit·큐 상태기계)와 다른 도메인.
- 결정 9(T8.2): 페이로드 렌더 순수 함수 `render_mcp_notification(notification) -> str` — 노출
  불변식의 게이트 내 본체(`mcp_server.reply_to_mcp_text` 정신). `Notification`(식별자만)에서만
  투영해 사용자向 비밀·조직 내부값이 구조적으로 안 샌다. kind(Literal 4종)를 match+assert_never로
  망라(새 종류 누락을 pyright가 잡음). 렌더는 게이트 내·순수, 실 전송은 게이트 밖.
- 결정 10(T8.2): 첫 실 채널 = MCP·fire-and-forget MVP 확정·Slack/Email은 후속 stub. `McpChannel`은
  transport 주입(`send_fn: Callable[[recipient_id, payload], None]`) 실 어댑터 — Fake send_fn 주입
  으로 게이트 내(렌더+호출 인자 검증)·실 MCP transport 주입으로 게이트 밖. send_fn 미주입이면
  NotImplementedError(transport 필요 명시). fire-and-forget은 `Notifier`가 이미 삼킴(결정 5) —
  추가 인프라(재시도·dead-letter·동적 구독) 0(후속). Slack/Email은 "결정 후 보류된 자리".

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

from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol, assert_never

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


# ── 페이로드 렌더 순수 함수: render_mcp_notification ────────────────────────
#
# `Notification`(식별자만) → MCP 알림 페이로드(사람이 읽는 한 줄). 노출 불변식의 게이트 내
# 본체다(ADR 0022 결정 9·T8.2 (a)). `mcp_server.reply_to_mcp_text`의 정신을 그대로 본떴다 —
# 도메인 값에서만 투영하고 내부값/사용자向 비밀을 안 싣는 *순수 함수*라 결정론 단위 테스트가
# 가능하고, 실 전송(McpChannel)은 게이트 밖이다. `reply_to_mcp_text`가 OrgReply에서만 투영하듯
# 이 함수는 Notification에서만 투영한다.
#
# 노출 불변식(두 겹):
#   ① 구조적 — `Notification`은 식별자만 든다(recipient_id·kind·subject_ref·created_at). 사용자向
#      질문 원문·카드 본문·조직 내부값(confidence·candidates·reason 등)은 *필드 자체가 없다*
#      (reply_to_mcp_text가 Answered/Pending에 그 필드가 없어 구조적으로 안전한 것과 동형).
#   ② 렌더 규율 — kind별 *중립 안내 한 줄* + subject_ref *손잡이*만 낸다. subject_ref는 운영 면
#      식별자(case_id·item_id·intent — owner/manager가 "무엇 때문에"를 처리함에서 잇는 손잡이)라
#      운영 통지에 싣는 게 맞다(Inbox·Manager 큐가 내부 식별자를 노출하는 것과 같은 면). 사용자向
#      비밀은 애초에 Notification에 없어 실릴 수 없다(reply_to_mcp_text Pending 중립 안내 정신).
#
# Literal 망라(reply_to_mcp_text의 match+assert_never 정신을 Literal에 적용): `kind`가 Literal
# 4종이라 match로 전부 분기하고 `case _ as never: assert_never(never)`로 *빠짐을 타입 검사 시점에
# 막는다*. NotificationKind에 5번째 종류를 더하면 pyright가 이 match의 assert_never를 도달 가능으로
# 보아 에러를 낸다 — 새 종류가 렌더 누락 없이 강제된다(sealed sum match 망라와 같은 안전망).


def render_mcp_notification(notification: Notification) -> str:
    """`Notification`을 MCP 알림 페이로드(사람이 읽는 한 줄)로 투영한다(노출 불변식·순수 함수).

    `mcp_server.reply_to_mcp_text`와 같은 경계: `Notification`에서만 투영하므로 조직 내부값·
    사용자向 비밀은 *구조적으로* 새지 않는다(Notification에 그 필드 자체가 없다). 다른 점은
    면(reply_to_mcp_text는 사용자向 답, 이건 운영 면 알림)과 종류 축(OrgReply sealed sum 대신
    NotificationKind Literal). SDK·IO 0이라 결정론 단위 테스트한다.

    kind별 *중립 안내* + `subject_ref` *손잡이*만 낸다 — owner/manager가 처리함에서 그 항목을
    찾도록. 실 전송(McpChannel.send)은 이 텍스트를 그대로 실어 보낸다(렌더와 전송 분리).

    match+assert_never로 NotificationKind(Literal 4종)를 망라한다 — 5번째 종류를 더하면 pyright가
    누락을 잡는다(sealed sum match 망라와 같은 안전망·`reply_to_mcp_text` 정신을 Literal에 적용).
    """
    ref = notification.subject_ref
    match notification.kind:
        case "conflict_opened":
            head = "담당 영역에 다툼이 열렸습니다 — 처리함에서 확인하세요"
        case "backup_review_added":
            head = "백업 답이 검토 대기로 적재됐습니다 — 처리함에서 확인하세요"
        case "reeval_flagged":
            head = "판례 재평가가 필요한 항목이 적재됐습니다 — 처리함에서 확인하세요"
        case "manager_escalated":
            head = "에스컬레이션이 매니저 큐로 올라왔습니다 — 큐에서 확인하세요"
        case _ as never:
            assert_never(never)
    return f"{head} (대상: {ref})"


class SlackChannel:
    """실 Slack 알림 `NotificationChannel` — **후속 자리(stub)**, 게이트 밖(ADR 0022 결정 1·8·10).

    **결정됨(2026-06-24, T8.2): 첫 실 채널은 MCP. 이 채널은 *후속 자리*다(미정 아님).** MVP는 MCP
    한 채널부터 닫는다(과도 엔지니어링·다중 채널 fan-out 회피 — ADR 0022 결정 10·결정 8 다중
    채널 fan-out open question). Slack 실 전송 본체(Slack SDK·새 무거운 의존성·외부 계정)는
    **착수하지 않은 후속**이다 — 채널 중립이라 같은 `NotificationChannel` 포트에 언제든 붙는다.
    recipient_id → Slack 채널/DM 변환은 이 어댑터 안에서(채널 중립 — 포트는 채널 어휘 안 가정).

    **stub 유지(미구현 — 결정 후 보류된 자리)** — 실 본문은 후속(게이트 밖·수동·외부 결정 후).
    """

    def send(self, notification: Notification) -> None:
        raise NotImplementedError(
            "Slack 알림은 후속 자리 — 결정됨(2026-06-24): 첫 실 채널은 MCP, Slack은 후속(T8.2 (d))"
        )


class EmailChannel:
    """실 이메일(SMTP) 알림 `NotificationChannel` — **후속 자리(stub)**, 게이트 밖(ADR 0022 결정 1·8·10).

    **결정됨(2026-06-24, T8.2): 첫 실 채널은 MCP. 이 채널은 *후속 자리*다(미정 아님).** SMTP 실
    전송 본체(SMTP 라이브러리·외부 메일 서버)는 **착수하지 않은 후속**이다. recipient_id →
    이메일 주소 변환은 이 어댑터 안에서(User.email 재사용 가능 — ADR 0021). 채널 중립이라 같은
    포트에 언제든 붙는다.

    **stub 유지(미구현 — 결정 후 보류된 자리)** — 실 본문은 후속(게이트 밖·수동·외부 결정 후).
    """

    def send(self, notification: Notification) -> None:
        raise NotImplementedError(
            "이메일(SMTP) 알림은 후속 자리 — 결정됨(2026-06-24): 첫 실 채널은 MCP, 메일은 후속(T8.2 (d))"
        )


class McpChannel:
    """실 MCP 알림 `NotificationChannel` — 첫 실 어댑터(transport 주입·ADR 0022 결정 1·9·10).

    **결정됨(2026-06-24, T8.2): 첫 실 채널 = MCP.** 제품이 MCP 서버라 외부 의존 0이 강점이라
    먼저 닫는다. `send`는 두 일을 한다: ① `render_mcp_notification`으로 페이로드(텍스트)를 렌더
    (게이트 내·순수·노출 불변식) ② 주입된 transport(`send_fn`)로 실 전송(게이트 밖·비결정).
    **렌더는 게이트 내·전송은 게이트 밖**으로 깔끔히 갈린다 — `GitGateway`↔`FakeGitGateway`·
    `ClaudeRunner` 주입과 동형(transport를 포트화하지 않고 `send_fn` 함수 주입으로 가볍게).

    transport 주입(`send_fn: Callable[[recipient_id, payload], None]`):
      - **Fake send_fn 주입 → 게이트 내 테스트** — 렌더 결과·호출 인자(recipient_id·payload)를
        결정론으로 검증(실 MCP 0). `FakeChannel`이 메모리 inbox로 검증되듯, McpChannel은 Fake
        transport로 "무엇을 어디로 보냈나"를 검증한다.
      - **실 MCP transport 주입 → 게이트 밖 수동** — server-initiated notification 클라 push.
        recipient_id(User.id) → MCP 세션/엔드포인트 변환은 *transport 안에서*(채널 중립 — 포트도
        McpChannel도 MCP 세션 어휘를 안 가정·mcp-runtime-engineer가 채움).
      - **send_fn 미주입(None) → NotImplementedError** — "실 MCP transport 필요"를 명시(자리만인
        Slack/Email stub과 달리, McpChannel은 *결정된 실 채널*이라 transport만 주면 실제로 돈다).

    fire-and-forget(ADR 0022 결정 5): `send_fn`이 던지면 `Notifier`가 이미 삼킨다(결정 5) — 이
    어댑터는 재시도/dead-letter를 안 둔다(MVP·게이트 밖 인프라). 채널 중립 — MCP는 1급이 아니라
    어댑터 중 하나(첫 채널일 뿐).
    """

    def __init__(self, send_fn: Callable[[str, str], None] | None = None) -> None:
        # recipient_id·payload(텍스트)를 받아 실 MCP 알림을 쏘는 transport. None이면 실 transport
        # 미주입 — send 시 NotImplementedError(결정된 실 채널이라 transport만 채우면 돈다).
        # Fake 주입 → 게이트 내, 실 MCP 클라 주입 → 게이트 밖(mcp-runtime-engineer).
        self._send_fn = send_fn

    def send(self, notification: Notification) -> None:
        """한 통지를 MCP 알림으로 전달한다 — 렌더(게이트 내)+transport(게이트 밖).

        ① `render_mcp_notification`으로 노출 불변식 페이로드 렌더(순수) ② 주입 transport로 전송.
        send_fn 미주입이면 NotImplementedError(실 MCP transport 필요 명시). 전송 실패(send_fn이
        던짐)는 여기서 안 삼킨다 — `Notifier`가 fire-and-forget로 삼킨다(결정 5·계층 책임 분리).
        """
        if self._send_fn is None:
            raise NotImplementedError(
                "실 MCP 알림 — send_fn(실 MCP transport) 주입 필요(게이트 밖 수동·T8.2 (a) 본체)"
            )
        payload = render_mcp_notification(notification)
        self._send_fn(notification.recipient_id, payload)


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
