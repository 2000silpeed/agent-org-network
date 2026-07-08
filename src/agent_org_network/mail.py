"""메일 질문 입출력 채널 — Phase 15 A축(채널). 웹챗·MCP와 형제 어댑터.

**게이트 내(A1/A2/A3)는 tdd-engineer가 red→green으로 구현 완료**(`tests/test_mail.py`).
실 어댑터(`ImapMailChannel` — A4/A5)만 `NotImplementedError` 자리로 남는다(게이트 밖·
owner 동의·스코프 선결). 테스트 더블(`FakeMailChannel`)은 in-memory 배관만 제공한다
(notify.py `FakeChannel` 정신 — 배관은 도메인 판단이 아님).

메일은 `ask_org`를 다른 클라이언트에서 재노출하는 세 번째 채널이다(PRD §5 "어느
클라이언트에서든 `ask_org`"의 연장 — 웹챗[web.py]·MCP[mcp_server.py]와 형제·충돌 아님).
`AskOrg.handle`은 **무변경**이다 — 메일은 그 앞뒤에 붙는 어댑터일 뿐이다:

    수신 메일 payload  ──A1──▶  질문 텍스트  ──▶  AskOrg.handle  ──▶  OrgReply
                                                                        │
    발신 메일  ◀──(MailChannel.send)──  MailReply(제목+본문)  ◀──A2─────┘

세 슬라이스(게이트 내):
  - A1 `extract_mail_question` — 메일 payload dict → 질문 텍스트(순수 함수·비질문 필터).
    `ConfluenceIngestor`(confluence_ingestor.py)와 동형인 관대한 순수 추출기다 —
    특정 메일 벤더 wire shape에 하드코딩하지 않고 폴백 사슬로 흡수하며, 스레드·인용
    답장·서명을 관대하게 벗긴다. 임의 처리 금지 — 애매 케이스는 `AmbiguousMail`로
    *데이터로 노출*한다(RoutingDecision sealed sum 정신).
  - A2 `project_reply_to_mail` — OrgReply → 메일 회신(제목+본문). `reply_to_mcp_text`
    (mcp_server.py)를 본문으로 **재사용**하는 얇은 래퍼다. 노출 불변식이 핵심 —
    OrgReply(Answered | Pending)에서만 투영하므로 내부값(confidence·candidates·
    manager_id·ticket_id·reason·intent 등)이 구조적으로 새지 않는다.
  - A3 `MailChannel` 포트(수신 poll + 송신 send) + `FakeMailChannel`. `NotificationChannel`
    (notify.py, ADR 0022 결정 1)의 채널 중립 정신 — 실 전송은 비결정·외부 의존이라 포트로
    가린다. 결정론 `FakeMailChannel`(게이트 내) + 실 어댑터(IMAP/Graph/webhook — 게이트
    밖·`NotImplementedError` 자리).

신원·프라이버시 경계(ADR 0040): 메일 발신자(`InboundMail.sender`)는 **약신원**이다 —
검증된 SSO 세션(ADR 0021·`email_verified` 가드)과도, 웹 익명 guest(ADR 0009)와도 구별되는
3번째 신원 출처이고, From 헤더는 스푸핑 가능하다. 그래서 발신자 주소는 *회신 목적지*
(반송 봉투)로만 쓰고, 라우팅 신원/Authority 부여에는 쓰지 않는다(발신자→User 매핑은
게이트 밖·검증 경유·ADR 0040). 실 메일 접근(A4/A5)은 게이트 밖이고 owner 동의·스코프가
선결이다 — 게이트 내 픽스처는 전부 합성(실 PII 0).

4대 불변식:
  - 노출: 메일 회신도 A2 투영만(내부값 미노출·제목·본문 둘 다·핵심).
  - 미아 없음: 메일 질문도 0매칭이면 handle이 Pending(unowned)→escalation으로 종착 —
    채널은 답 전달일 뿐 라우팅/드롭을 하지 않는다(구조적 보존).
  - Authority 중앙: 채널은 답 전달이지 권한 선언이 아니다(발신자가 담당권을 주장 불가).
  - 전이 ≠ 기록: 채널 송수신 로그는 감사(audit)와 별 축이다(FakeChannel.delivered 정신).
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from agent_org_network.ask_org import OrgReply
from agent_org_network.mcp_server import reply_to_mcp_text

# ── A1: 메일 payload → 질문 추출 결과 (MailExtraction sealed sum) ───────────────
#
# 메일 하나를 읽은 결과를 sealed sum으로 낸다("타입이 곧 상태" — RoutingDecision·
# OrgReply 정신). 세 갈래로 *망라*하고 임의 처리를 금지한다:
#   - MailQuestion   → 질문으로 판정. text를 AskOrg.handle에 넘긴다.
#   - NotAQuestion   → 비질문으로 필터(FYI·공지·자동응답·빈 본문). 조용히 드롭하지 않고
#                      reason으로 *경계를 드러낸다*(ConfluenceIngestor가 빈 본문을 스킵
#                      대신 명시 실패로 드러내는 정신).
#   - AmbiguousMail  → 질문인지 애매(다주제·불명확). 임의로 질문/비질문 처리하지 않고
#                      *데이터로 노출*해 사람/다운스트림이 판단하게 한다.
# 소비자가 match+assert_never로 망라하게 설계된 sealed sum이다(새 갈래 추가 시 컴파일 강제
# — 단, 이 갈래를 실제로 exhaustive match하는 소비자는 게이트 내에 아직 없다).


@dataclass(frozen=True)
class MailQuestion:
    """질문으로 판정된 메일 — `text`를 `AskOrg.handle`에 넘긴다.

    `text`는 스레드·인용 답장·서명을 관대하게 벗긴 정규화 본문이다(A1 추출 산물).
    발신자·제목 등 엔벌로프는 여기 싣지 않는다 — 이 값은 *질문 텍스트*만 담는다
    (신원/회신 주소는 `InboundMail` 엔벌로프 소관·A3·ADR 0040 신원 경계).
    """

    text: str


NonQuestionReason = Literal[
    "empty",  # 본문 추출 결과가 빈 문자열/공백(질문 아님·명시 실패)
    "fyi",  # 정보 공유/공지(FYI·no reply needed) — 답을 구하지 않음
    "scheduling",  # 일정 조율/캘린더 초대 — 라우팅 대상 질문 아님
    "auto_reply",  # 자동 응답(부재중·수신확인·메일링리스트) — 사람 질문 아님
]


@dataclass(frozen=True)
class NotAQuestion:
    """비질문으로 필터된 메일 — 조용히 드롭하지 않고 `reason`으로 경계를 드러낸다.

    `reason`(`NonQuestionReason`)은 필터 판정 근거다(운영 튜닝·감사용). 채널은 이걸
    받으면 `ask_org`로 넘기지 않는다(비질문은 라우팅 대상이 아님) — 미아 없음 불변식과
    무관하다(애초에 질문이 아니므로 라우팅 자체가 없다).
    """

    reason: NonQuestionReason


AmbiguousReason = Literal[
    "multi_topic",  # 한 메일에 여러 질문/주제 — 어느 걸 라우팅할지 애매
    "unclear",  # 질문인지 진술인지 불명확(관대 파싱 한계)
]


@dataclass(frozen=True)
class AmbiguousMail:
    """질문인지 애매한 메일 — 임의 처리 금지, 데이터로 노출(사람/다운스트림 판단).

    관대 추출의 한계 지점을 *삼키지 않고 드러낸다*(ConfluenceIngestor가 애매를 스킵
    대신 명시하는 정신). `text`는 추출된 후보 본문(사람이 볼 수 있게 보존), `reason`은
    왜 애매한지. 게이트 내에서 자동 라우팅하지 않는다 — 실 정밀 파싱/분기(다주제
    분해·되묻기)는 후속(게이트 밖·A4). 임의 규칙으로 질문/비질문을 확정하면 조용한
    오분류가 되므로 이 갈래로 노출만 한다.
    """

    text: str
    reason: AmbiguousReason


MailExtraction = MailQuestion | NotAQuestion | AmbiguousMail


# ── A1 구현 헬퍼(관대 폴백 사슬 + 스레드/서명 벗기기 + 정규화) ─────────────────
#
# `ConfluenceIngestor`(confluence_ingestor.py)의 관대한 순수 추출기 정신을 메일
# payload에 맞춰 재구현한다(모듈 결합을 늘리지 않으려 별도 구현 — 둘 다 stdlib
# 폴백 사슬 + HTML 정규화라는 *같은 정신*을 공유할 뿐, 코드 재사용은 하지 않는다).

_TAG_RE: re.Pattern[str] = re.compile(r"<[^>]+>")
_WHITESPACE_RE: re.Pattern[str] = re.compile(r"\s+")

# 스레드 인용 헤더(관대 매칭) — 이 줄부터 끝까지는 인용된 원 메일로 간주해 버린다.
# "On ... wrote:"(영문 클라이언트) · "…님이 …작성:"(한글 클라이언트) ·
# "-----Original Message-----"(구식 포워드 구분선).
_QUOTE_HEADER_RE: re.Pattern[str] = re.compile(
    r"^(On .+ wrote:|.+님이\s.*작성(?:했습니다)?:|-{3,}\s*Original Message\s*-{3,})\s*$",
    re.IGNORECASE,
)

# 서명 구분선 — RFC 3676이 정확히 규정하는 건 "-- "(대시 두 개 + 공백 하나, 원문 라인
# 그대로) 뿐이다. strip 후 비교하면 trailing space가 사라져 이 값이 매치 불가(dead)가
# 되고, 동시에 순수 "--"(마크다운 수평선)를 서명으로 오인해 본문을 잘못 절단한다 —
# 그래서 원문 라인(strip 전) 기준 정확 일치로만 판정한다.
_SIGNATURE_MARKER = "-- "


def _extract_raw_mail_body(payload: Mapping[str, object]) -> str:
    """메일 payload → 정규화 전 raw 본문 문자열(관대 폴백 사슬·벤더 wire shape 흡수)."""
    text_plain: object = payload.get("text/plain")
    if isinstance(text_plain, str):
        return text_plain

    body: object = payload.get("body")
    if isinstance(body, Mapping):
        body_mapping = cast("Mapping[str, object]", body)
        for key in ("text", "plain", "content"):
            value: object = body_mapping.get(key)
            if isinstance(value, str):
                return value
    elif isinstance(body, str):
        return body

    content: object = payload.get("content")
    if isinstance(content, str):
        return content

    text: object = payload.get("text")
    if isinstance(text, str):
        return text

    return ""


def _strip_thread_and_signature(raw: str) -> str:
    """인용 답장(스레드 헤더+`>` 접두)과 서명(`-- ` 구분자)을 관대하게 벗긴다."""
    kept: list[str] = []
    for line in raw.splitlines():
        stripped_line = line.strip()
        if _QUOTE_HEADER_RE.match(stripped_line):
            break
        if line == _SIGNATURE_MARKER:
            break
        if stripped_line.startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _normalize_mail_text(raw: str) -> str:
    """거친 HTML 정규화 — 태그를 공백으로 치환·엔티티 디코드·공백 정리(stdlib만)."""
    without_tags = _TAG_RE.sub(" ", raw)
    decoded = html.unescape(without_tags)
    collapsed = _WHITESPACE_RE.sub(" ", decoded)
    return collapsed.strip()


# 비질문 필터 키워드(관대·소문자 부분일치). 순서는 NonQuestionReason 문서 순서를 따른다.
#
# fyi류는 강/약 두 그룹으로 나눈다(정책 확정 — 실 질문 드롭이 채널 레벨 미아 위험이라
# 질문을 우선한다): 약신호(단순 정보공유 어투)는 질문 마커가 *없을 때만* 필터하고,
# 강신호(명시적으로 회신 불필요를 선언)는 질문 마커가 있어도 필터한다("FYI, 환불 절차
# 어떻게 되나요?"는 MailQuestion이어야 하지만 "회신 불필요합니다. ...?"는 필터).
_WEAK_FYI_KEYWORDS = (
    "fyi",
    "for your information",
    "참고 바랍니다",
    "참고용으로 공유",
)
_STRONG_FYI_KEYWORDS = (
    "회신 불필요",
    "no reply needed",
)
_SCHEDULING_KEYWORDS = (
    "calendar invite",
    "invite:",
    "회의 일정",
    "일정 조율",
    "캘린더 초대",
    "미팅 초대",
)
_AUTO_REPLY_KEYWORDS = (
    "out of office",
    "automatic reply",
    "부재중",
    "부재 중",
    "자동 응답",
    "자동-회신",
    "수신확인",
)

# 질문 신호 키워드(관대) — "?"/fullwidth "？" 외에 흔한 요청/의문 표현. 이 중 하나도
# 없으면 unclear. fullwidth 물음표(U+FF1F)는 한글 메일 클라이언트·IME에서 흔히 쓰여
# ("환불？") ASCII "?"만 보면 마커 없는 짧은 CJK 질문이 새므로 양쪽 다 인식한다.
_QUESTION_MARKERS = (
    "?",
    "？",
    "궁금",
    "알려주세요",
    "부탁",
    "문의",
    "가능한가요",
    "가능할까요",
    "해주세요",
    "인가요",
    "될까요",
    "어떻게",
    "왜",
)

# 다주제(multi_topic) 판정용 물음표 카운트 보정:
#   - URL 유사 토큰(`http(s)://...`·`www...`·쿼리스트링 `?key=value`)에 박힌 "?"는
#     실제 질문이 아니라 오탐 원인이라 카운트에서 제외한다.
#   - 연속 물음표("??"·"？？"·혼합)는 강조 표현("정말요??")이지 별개 질문이 아니라
#     1개로 collapse한다. 완벽 문장분해는 하지 않는다 — 흔한 오탐만 제거한다.
_URL_LIKE_RE: re.Pattern[str] = re.compile(
    r"(?:https?://\S+|www\.\S+|\S+\?[A-Za-z0-9_]+=\S*)"
)
_QUESTION_MARK_RUN_RE: re.Pattern[str] = re.compile(r"[?？]+")


def _matches_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _looks_like_question(text: str) -> bool:
    return any(marker in text for marker in _QUESTION_MARKERS)


def _count_question_marks(text: str) -> int:
    """다주제 판정용 물음표 개수 — URL 유사 토큰 제외 + 연속 물음표 1개로 collapse."""
    masked = _URL_LIKE_RE.sub(" ", text)
    return len(_QUESTION_MARK_RUN_RE.findall(masked))


def extract_mail_question(payload: Mapping[str, object]) -> MailExtraction:
    """메일 payload → `MailExtraction`(질문/비질문/애매) — 순수 함수(ConfluenceIngestor 동형).

    구현 계약(tdd-engineer red→green 완료):
      1. 본문 추출은 특정 벤더 wire shape에 하드코딩하지 않고 *관대한 폴백 사슬*로
         흡수한다(`ConfluenceIngestor._extract_body_text` 정신) — 예: text/plain →
         body → content → text 순. IMAP/Graph/webhook 어느 shape든 픽스처 교체만으로
         흡수(A4가 payload를 이 함수에 먹인다).
      2. 스레드·인용 답장(`>` 접두·"On ... wrote:" 헤더)과 서명(`-- ` 구분자·흔한
         패턴)을 *관대하게* 벗긴다 — 완벽 파싱이 아니라 라우팅에 쓸 질문 몸통을 남긴다
         (실 정밀화는 후속).
      3. 정규화(HTML 태그 제거·엔티티 디코드·공백 정리)는 stdlib만 쓴다
         (`ConfluenceIngestor._normalize_extracted_text` 재사용 후보 — B축 무관·순수).
      4. 비질문(FYI/일정/자동응답/빈 본문)은 `NotAQuestion(reason=...)`으로, 애매
         (다주제/불명확)는 `AmbiguousMail(text, reason)`로 낸다 — 임의로 질문 확정 금지.
      5. 발신자·회신 주소는 이 함수가 다루지 않는다(신원/엔벌로프는 A3·ADR 0040).

    순수 함수라 결정론 단위 테스트가 가능하다(합성 메일 dict → 고정 판정).
    """
    raw = _extract_raw_mail_body(payload)
    stripped = _strip_thread_and_signature(raw)
    text = _normalize_mail_text(stripped)

    if not text:
        return NotAQuestion(reason="empty")
    if _matches_any(text, _STRONG_FYI_KEYWORDS):
        return NotAQuestion(reason="fyi")
    if _matches_any(text, _SCHEDULING_KEYWORDS):
        return NotAQuestion(reason="scheduling")
    if _matches_any(text, _AUTO_REPLY_KEYWORDS):
        return NotAQuestion(reason="auto_reply")
    is_question = _looks_like_question(text)
    if _matches_any(text, _WEAK_FYI_KEYWORDS) and not is_question:
        return NotAQuestion(reason="fyi")
    if _count_question_marks(text) >= 2:
        return AmbiguousMail(text=text, reason="multi_topic")
    if not is_question:
        return AmbiguousMail(text=text, reason="unclear")
    return MailQuestion(text=text)


# ── A2: OrgReply → 메일 회신 투영 (MailReply) ──────────────────────────────────
#
# 노출 불변식의 게이트 내 본체. `reply_to_mcp_text`(mcp_server.py)를 본문으로 재사용하는
# 얇은 래퍼다 — 그 함수가 이미 OrgReply(Answered | Pending)에서만 투영하므로 조직 내부값
# (confidence·candidates·manager_id·ticket_id·reason·intent·escalated_to)이 *구조적으로*
# 새지 않는다(Answered/Pending에 그 필드 자체가 없음). 메일이 새로 여는 노출 표면은
# **제목(subject)** 하나뿐이라, 제목은 내부 상태에 분기하지 않고 사용자 자신의 원 제목을
# 되비추거나(에코) 중립 고정 문자열만 쓴다(새 노출 표면 0).


@dataclass(frozen=True)
class MailReply:
    """메일 회신 투영 — 제목 + 본문. OrgReply에서만 파생(내부값 0·노출 불변식).

    `body`는 `reply_to_mcp_text(reply)` 재사용 산물(담당·신뢰 상태[mode]·출처가 박힌
    사람이 읽는 텍스트 — MCP 텍스트 투영과 같은 SSOT). `subject`는 원 제목 에코 또는
    중립 고정 문자열로, 조직 내부 상태를 인코딩하지 않는다.
    """

    subject: str
    body: str


# 원 제목이 없을 때 쓰는 중립 고정 제목(조직 내부값 0). Answered/Pending을 분기해
# 제목을 바꾸지 않는다 — coarse 상태조차 제목에 새기지 않아 노출 표면을 최소화한다.
_NEUTRAL_SUBJECT = "[조직에 묻기] 답변"


def project_reply_to_mail(
    reply: OrgReply, *, original_subject: str | None = None
) -> MailReply:
    """OrgReply → `MailReply`(제목+본문) — 노출 투영만(`reply_to_mcp_text` 재사용·순수).

    구현 계약(tdd-engineer red→green 완료):
      1. 본문 = `reply_to_mcp_text(reply)`를 **그대로 재사용**한다(노출 SSOT — 웹/MCP/메일이
         같은 투영 규율을 공유해 채널마다 노출 불변식이 갈리지 않게). 새 본문 조립 로직을
         만들지 않는다.
      2. 제목 = `original_subject`가 있으면 `"Re: {original_subject}"`(사용자 자신의 입력
         에코 — 조직 내부값 아님), 없으면 `_NEUTRAL_SUBJECT`. **내부 상태에 분기 금지**
         (Answered/Pending·kind에 따라 제목을 바꾸지 않는다 — 제목에 상태를 새기면 새
         노출 표면이 생김).
      3. 결과 `MailReply`의 subject·body **둘 다** leaky 단언 대상이다(테스트가 두 문자열
         모두에 confidence·candidates·manager_id·ticket_id·reason·intent·escalated_to·
         primary 부분문자열이 없음을 단언). owner·agent_id·mode·record_id·tracking은
         `reply_to_mcp_text`가 이미 노출하는 *정당한* 값이라 leaky 아님(그대로 통과).

    순수 함수라 결정론 단위 테스트가 가능하다(고정 OrgReply → 고정 MailReply).
    """
    body = reply_to_mcp_text(reply)
    subject = f"Re: {original_subject}" if original_subject else _NEUTRAL_SUBJECT
    return MailReply(subject=subject, body=body)


# ── A3: MailChannel 포트(수신+송신) + FakeMailChannel ──────────────────────────
#
# `NotificationChannel`(notify.py, ADR 0022 결정 1)의 채널 중립 정신 — 실 전송은 비결정·
# 외부 의존(IMAP/Graph/SMTP/webhook)이라 포트로 가린다. NotificationChannel은 송신 전용
# (fire-and-forget)이지만 메일 채널은 *양방향*(질문 수신 + 답 송신)이라 poll+send 두 메서드다.
# 결정론 `FakeMailChannel`(게이트 내) + 실 어댑터(게이트 밖·NotImplementedError).


@dataclass(frozen=True)
class InboundMail:
    """수신 메일 엔벌로프 — 발신자·제목·raw payload. A1이 payload에서 질문을 추출한다.

    `sender`는 발신자 이메일 주소다 — **약신원**(ADR 0040): From 헤더는 스푸핑 가능하고
    검증된 SSO 세션(ADR 0021)이 아니다. 그래서 `sender`는 *회신 목적지*(반송 봉투)로만
    쓰고 라우팅 신원/Authority 부여엔 쓰지 않는다(발신자→User 매핑은 게이트 밖·검증 경유).
    `payload`는 메일 원본 dict(정확한 wire shape는 A1의 관대 폴백 사슬이 흡수 —
    `ConfluenceIngestor`가 페이지 payload를 흡수하는 정신). `message_id`는 멱등 키
    (같은 메일 중복 수신 방지 자리·`Notification` 멱등 정신) — 미제공이면 None.
    """

    sender: str
    subject: str
    payload: Mapping[str, object]
    message_id: str | None = None


@dataclass(frozen=True)
class OutboundMail:
    """송신 메일 — 수신자·회신 본문. A2 산출(`MailReply`)을 발신한다.

    `recipient`는 회신 목적지(보통 원 발신자 `InboundMail.sender` — 반송 봉투). `reply`는
    A2가 낸 `MailReply`(제목+본문·투영만). `in_reply_to`는 스레드 연결용 원 메일 식별자
    (`InboundMail.message_id` 에코 — 메일 스레딩·조직 내부값 아님). 노출 불변식은 A2가
    이미 강제한다(이 엔벌로프는 전송만 담당).
    """

    recipient: str
    reply: MailReply
    in_reply_to: str | None = None


class MailChannel(Protocol):
    """메일 질문 수신 + 답 송신의 최소 포트(ADR 0022 채널 중립 정신 확장).

    `poll`/`send` 두 메서드 — 실 전송은 *불투명한 외부 부작용*이라 포트로 가린다
    (`NotificationChannel`·`GitGateway`·`OidcProvider`·`AgentRuntime`과 같은 포트 패턴).
    결정론 `FakeMailChannel`과 실 어댑터(IMAP/Graph/webhook — 게이트 밖)가 같은 포트의
    다른 구현이다. 이 포트는 채널 어휘(IMAP·Graph 등)를 가정하지 않는다 — 수신 방식
    (poll vs webhook)·인증은 어댑터 내부 관심사다.
    """

    def poll(self) -> tuple[InboundMail, ...]:
        """대기 중인 수신 메일들을 가져온다(수신). 없으면 빈 튜플."""
        ...

    def send(self, mail: OutboundMail) -> None:
        """회신 메일 한 통을 발신한다(송신·fire-and-forget)."""
        ...


class FakeMailChannel:
    """결정론 in-memory `MailChannel` — 게이트(단위 테스트)에서 돈다(ADR 0022 FakeChannel 정신).

    실 네트워크·시각·랜덤 0. 생성 시 주입한 수신 메일(합성·실 PII 0)을 `poll`이 배출하고,
    `send`한 메일을 `sent` 로그(append-only)에 쌓는다(`FakeChannel.inbox`/`delivered`와
    같은 결). 테스트가 "이 수신 메일이 이 회신을 낳았나"를 `sent` 조회로 검증한다.

    배관만 제공한다(도메인 판단 0) — 라우팅·투영은 A1/A2·`AskOrg.handle`이 진다.
    """

    def __init__(self, inbound: Sequence[InboundMail] = ()) -> None:
        # 아직 poll되지 않은 수신 메일(주입 — 결정론). poll이 배출하며 비운다.
        self._inbound: list[InboundMail] = list(inbound)
        # 발신 로그(append-only — "회신이 나갔나"의 검증 원천, audit과 별 축·전이≠기록).
        self.sent: list[OutboundMail] = []

    def poll(self) -> tuple[InboundMail, ...]:
        drained = tuple(self._inbound)
        self._inbound.clear()
        return drained

    def send(self, mail: OutboundMail) -> None:
        self.sent.append(mail)


# ── 실 어댑터 자리(게이트 밖·수동 시연) ────────────────────────────────────────
#
# 실 IMAP/Graph 수신·SMTP/Graph 송신은 실 네트워크·인증·PII라 게이트 밖이다(A4/A5 —
# `worker.py`의 실 WS 셸·`SlackChannel`/`EmailChannel`[notify.py]과 같은 경계). 실 메일
# 접근은 owner 동의·스코프가 선결이고(ADR 0040), 발신자→User 신원 매핑은 검증 경유다.
# 여기선 자리만 잡는다(주입 transport 미제공 시 NotImplementedError).


class ImapMailChannel:
    """실 IMAP/SMTP `MailChannel` 어댑터 — **게이트 밖·미구현 자리**(A4/A5).

    실 메일함 poll·실 발신은 실 네트워크·인증·PII라 게이트 밖이다. transport 주입 설계는
    `McpChannel`(notify.py 결정 10)의 `send_fn` 주입 정신을 따른다(후속 판단). 실 접근은
    owner 동의·스코프·발신자 검증(ADR 0040)이 선결이다.
    """

    def poll(self) -> tuple[InboundMail, ...]:
        raise NotImplementedError("A4: 실 IMAP/Graph 수신은 게이트 밖(owner 동의·스코프 선결)")

    def send(self, mail: OutboundMail) -> None:
        raise NotImplementedError("A5: 실 SMTP/Graph 송신은 게이트 밖(발신 검수 선결)")
