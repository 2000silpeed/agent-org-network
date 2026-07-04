"""Knowledge Sync 채널 결정론 코어 — 값 객체·민감정보 필터·admission·WS 프레임 (Phase 12 S3, ADR 0033).

범위: 결정론(주입 격리)만 — 실 WS·실 git·크로스머신은 밖(mcp-runtime-engineer 몫).

  - 지정 밖 경로 거부(외부 결정 ①)
  - 민감정보 패턴별 차단(주민등록번호·API 키·비밀번호) + 우회 시도 케이스(외부 결정 ②)
  - 정상 수용
  - 프레임 왕복(직렬화↔역직렬화)
  - 거부 사유 보존
  - 멱등(같은 문서 재동기화)
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_org_network.agent_card import AgentCard
from agent_org_network.knowledge_sync import (
    Admitted,
    Blocked,
    Clean,
    KnowledgeBundleContent,
    KnowledgeDoc,
    KnowledgeSyncAck,
    KnowledgeSyncSpec,
    Rejected,
    SyncKnowledge,
    accept_knowledge_sync,
    admit_knowledge,
    filter_sensitive,
)

_T0 = datetime(2026, 7, 4, 9, 0, 0, tzinfo=timezone.utc)
_REVIEWED = _T0.date()


def _card(agent_id: str = "refund-bot", owner: str = "alice") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary=f"{agent_id} 요약",
        domains=["환불"],
        last_reviewed_at=_REVIEWED,
    )


def _spec(agent_id: str = "refund-bot", paths: tuple[str, ...] = ("docs/",)) -> KnowledgeSyncSpec:
    return KnowledgeSyncSpec(agent_id=agent_id, paths=paths)


def _content(
    agent_id: str = "refund-bot",
    documents: tuple[KnowledgeDoc, ...] = (),
    version: str = "sync-1",
    synced_at: datetime = _T0,
) -> KnowledgeBundleContent:
    return KnowledgeBundleContent(
        agent_id=agent_id, documents=documents, version=version, synced_at=synced_at
    )


# ── filter_sensitive — 민감정보 패턴별 차단 ──────────────────────────────────


def test_민감정보_없는_본문은_Clean이다() -> None:
    verdict = filter_sensitive("환불 정책: 구매 후 7일 이내 가능합니다.")
    assert isinstance(verdict, Clean)


def test_주민등록번호_패턴은_Blocked이다() -> None:
    verdict = filter_sensitive("담당자 주민번호: 901231-1234567")
    assert isinstance(verdict, Blocked)
    assert "resident_registration_number" in verdict.patterns


def test_API_키_패턴은_Blocked이다() -> None:
    verdict = filter_sensitive("설정: sk-abcdEFGH12345678ijklmnop")
    assert isinstance(verdict, Blocked)
    assert "api_key" in verdict.patterns


def test_AWS_키_패턴도_API_키로_Blocked이다() -> None:
    verdict = filter_sensitive("AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP")
    assert isinstance(verdict, Blocked)
    assert "api_key" in verdict.patterns


def test_비밀번호_패턴은_Blocked이다() -> None:
    verdict = filter_sensitive("db password: hunter2")
    assert isinstance(verdict, Blocked)
    assert "password" in verdict.patterns


def test_한글_비밀번호_라벨도_Blocked이다() -> None:
    verdict = filter_sensitive("비밀번호: swordfish123")
    assert isinstance(verdict, Blocked)
    assert "password" in verdict.patterns


def test_우회_시도_대소문자_섞기도_Blocked이다() -> None:
    verdict = filter_sensitive("PaSsWoRd = topsecret!")
    assert isinstance(verdict, Blocked)
    assert "password" in verdict.patterns


def test_우회_시도_주민번호_공백삽입도_Blocked이다() -> None:
    verdict = filter_sensitive("901231 1234567 참고")
    assert isinstance(verdict, Blocked)
    assert "resident_registration_number" in verdict.patterns


def test_여러_패턴_동시_검출시_전부_담긴다() -> None:
    verdict = filter_sensitive("password: abc123 그리고 주민번호 901231-1234567")
    assert isinstance(verdict, Blocked)
    assert "password" in verdict.patterns
    assert "resident_registration_number" in verdict.patterns


def test_우회_시도_주민번호_외국인_세기코드_5_8도_Blocked이다() -> None:
    """뒷자리 성별/세기 코드는 1~4(내국인)뿐 아니라 5~8(외국인/2000년대 이후)도 있다.
    `_RRN_RE`가 [1-4]만 잡으면 `901231-5234567` 같은 외국인 등록번호가 그대로 샌다."""
    verdict = filter_sensitive("외국인 등록번호: 901231-5234567")
    assert isinstance(verdict, Blocked)
    assert "resident_registration_number" in verdict.patterns


def test_우회_시도_OpenAI_신형_키_하이픈_포함도_Blocked이다() -> None:
    """OpenAI 신형 키는 `sk-proj-...`처럼 프리픽스 뒤에 하이픈이 섞인다.
    `sk-[A-Za-z0-9]{16,}`는 하이픈에서 매치가 끊겨 `sk-proj-ABCDEFGH12345678`을 놓친다."""
    verdict = filter_sensitive("설정: sk-proj-ABCDEFGH12345678")
    assert isinstance(verdict, Blocked)
    assert "api_key" in verdict.patterns


def test_우회_시도_AWS_키_소문자도_Blocked이다() -> None:
    """`AKIA[0-9A-Z]{16}`은 대문자만 잡아 `akia...`(소문자)가 그대로 샌다."""
    verdict = filter_sensitive("aws_access_key_id=akiaabcdefghijklmnop")
    assert isinstance(verdict, Blocked)
    assert "api_key" in verdict.patterns


def test_하이픈_들어간_일반_단어는_Clean이다() -> None:
    """`sk-[A-Za-z0-9_-]{16,}` 같은 하이픈 허용 확장이 일반 하이픈 복합어까지
    오탐하면 안 된다 — API 키 프리픽스(sk-/ghp_ 등) 없는 순수 텍스트는 Clean 유지."""
    verdict = filter_sensitive("문서 파일명: risk-assessment-checklist-v2-final.md")
    assert isinstance(verdict, Clean)


def test_sk_프리픽스_짧은_토큰_16자_미만은_Clean_유지가_의도된_한계이다() -> None:
    """`sk-abc123`처럼 16자 미만인 짧은 문자열은 여전히 Clean이다 — `{16,}` 하한은
    "sk-"로 시작하는 흔한 일반 식별자(짧은 변수명·슬러그 등)를 오탐하지 않기 위한
    의도된 경계다(리뷰 판단: 정당한 미탐, 필터 규칙 변경 불필요). 실 API 키는
    통상 20자 이상이라 이 하한 아래로는 내려가지 않는다."""
    verdict = filter_sensitive("설정 키 이름 예시: sk-abc123")
    assert isinstance(verdict, Clean)


# ── admit_knowledge — 명시 지정 + 민감 필터 admission ────────────────────────


def test_지정_경로_안_정상_문서는_Admitted이다() -> None:
    content = _content(documents=(KnowledgeDoc(path="docs/refund.md", body="7일 이내 환불"),))
    result = admit_knowledge(content, _card(), _spec())
    assert isinstance(result, Admitted)
    assert result.content == content


def test_지정_밖_경로는_Rejected이다() -> None:
    content = _content(documents=(KnowledgeDoc(path="secrets/keys.md", body="내용"),))
    result = admit_knowledge(content, _card(), _spec())
    assert isinstance(result, Rejected)
    assert "secrets/keys.md" in result.reason


def test_지정_경로_정확일치도_통과한다() -> None:
    spec = _spec(paths=("docs/refund.md",))
    content = _content(documents=(KnowledgeDoc(path="docs/refund.md", body="내용"),))
    result = admit_knowledge(content, _card(), spec)
    assert isinstance(result, Admitted)


def test_민감정보_포함_문서는_지정_안이어도_Rejected이다() -> None:
    content = _content(
        documents=(KnowledgeDoc(path="docs/staff.md", body="주민번호: 901231-1234567"),)
    )
    result = admit_knowledge(content, _card(), _spec())
    assert isinstance(result, Rejected)
    assert "resident_registration_number" in result.reason


def test_agent_id_불일치_spec은_Rejected이다() -> None:
    content = _content(agent_id="other-bot")
    result = admit_knowledge(content, _card(), _spec(agent_id="refund-bot"))
    assert isinstance(result, Rejected)


def test_agent_id_불일치_card는_Rejected이다() -> None:
    content = _content(agent_id="refund-bot")
    other_card = _card(agent_id="other-bot")
    result = admit_knowledge(content, other_card, _spec(agent_id="refund-bot"))
    assert isinstance(result, Rejected)


def test_문서_하나라도_위반이면_번들_전체가_거부된다() -> None:
    content = _content(
        documents=(
            KnowledgeDoc(path="docs/ok.md", body="정상 내용"),
            KnowledgeDoc(path="secrets/bad.md", body="정상 내용"),
        )
    )
    result = admit_knowledge(content, _card(), _spec())
    assert isinstance(result, Rejected)


def test_빈_documents는_Admitted이다() -> None:
    content = _content(documents=())
    result = admit_knowledge(content, _card(), _spec())
    assert isinstance(result, Admitted)


# ── WS 프레임 — 직렬화/역직렬화 왕복 ──────────────────────────────────────────


def test_SyncKnowledge_프레임_왕복() -> None:
    content = _content(documents=(KnowledgeDoc(path="docs/refund.md", body="7일 이내"),))
    frame = SyncKnowledge(content=content)
    wire = frame.model_dump(mode="json")
    restored = SyncKnowledge.model_validate(wire)
    assert restored == frame
    assert restored.content.documents[0].path == "docs/refund.md"


def test_KnowledgeSyncAck_프레임_왕복() -> None:
    ack = KnowledgeSyncAck(agent_id="refund-bot", accepted=False, reason="지정 밖 경로")
    wire = ack.model_dump(mode="json")
    restored = KnowledgeSyncAck.model_validate(wire)
    assert restored == ack


def test_SyncKnowledge_프레임은_미지_필드를_거부한다() -> None:
    content = _content()
    frame = SyncKnowledge(content=content)
    wire = frame.model_dump(mode="json")
    wire["unexpected_field"] = "x"
    try:
        SyncKnowledge.model_validate(wire)
        raised = False
    except Exception:
        raised = True
    assert raised


# ── accept_knowledge_sync — 프레임 수신→admission→응답 결정론 코어 ──────────


def test_정상_동기화는_accepted_True_응답을_받는다() -> None:
    content = _content(documents=(KnowledgeDoc(path="docs/refund.md", body="정상"),))
    frame = SyncKnowledge(content=content)
    ack = accept_knowledge_sync("alice", frame, _card(owner="alice"), _spec())
    assert ack.accepted is True
    assert ack.reason == ""
    assert ack.agent_id == "refund-bot"


def test_타_owner_사칭은_거부되고_사유가_보존된다() -> None:
    content = _content(documents=(KnowledgeDoc(path="docs/refund.md", body="정상"),))
    frame = SyncKnowledge(content=content)
    ack = accept_knowledge_sync("mallory", frame, _card(owner="alice"), _spec())
    assert ack.accepted is False
    assert "스코핑" in ack.reason


def test_지정_밖_경로_동기화_거부_사유가_응답에_보존된다() -> None:
    content = _content(documents=(KnowledgeDoc(path="secrets/keys.md", body="정상"),))
    frame = SyncKnowledge(content=content)
    ack = accept_knowledge_sync("alice", frame, _card(owner="alice"), _spec())
    assert ack.accepted is False
    assert "secrets/keys.md" in ack.reason


def test_민감정보_동기화_거부_사유가_응답에_보존된다() -> None:
    content = _content(
        documents=(KnowledgeDoc(path="docs/staff.md", body="password: hunter2"),)
    )
    frame = SyncKnowledge(content=content)
    ack = accept_knowledge_sync("alice", frame, _card(owner="alice"), _spec())
    assert ack.accepted is False
    assert "password" in ack.reason


def test_같은_문서_재동기화는_멱등하게_수용된다() -> None:
    """같은 KnowledgeBundleContent를 두 번 보내도 매번 결정론적으로 같은 admission 결과."""
    content = _content(documents=(KnowledgeDoc(path="docs/refund.md", body="정상"),))
    frame = SyncKnowledge(content=content)
    ack1 = accept_knowledge_sync("alice", frame, _card(owner="alice"), _spec())
    ack2 = accept_knowledge_sync("alice", frame, _card(owner="alice"), _spec())
    assert ack1 == ack2
    assert ack1.accepted is True and ack2.accepted is True
