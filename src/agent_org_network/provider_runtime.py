"""T9.4(a)(b) — 공급자 런타임 어댑터 (ADR 0027 결정 1·2·3·5)

슬라이스 (a): ProviderTransport(Protocol) · ProviderRequest(값 객체)
              ClaudeApiRuntime(AgentRuntime 포트 · 주입 transport)
              StubProviderTransport(결정론 · 테스트 주입)
              CodexApiRuntime · GeminiApiRuntime (NotImplementedError 자리)

슬라이스 (b): build_provider_request · assemble_stream · map_response_to_answer
              (순수 함수 · SDK/IO 0 · Answer 계약 보존 · 노출 불변식)

A(ii) OKF 접지: read_okf_bundle(순수 헬퍼, stdlib·pathlib만, SDK 0) +
                build_provider_request okf 키워드 + ProviderApiRuntime okf_root 주입.
                ClaudeCodeRuntime의 cwd 접지와 대칭 — 인프로세스 공급자도 OKF 로컬 읽기.

게이트 밖: 실 OAuth·실 공급자 API·실 스트리밍·공급자 SDK (T9.6)
분류기·배치 경로의 claude -p는 잔존 (ADR 0027 결정 3 — 대화 경로만 교체)
"""

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from agent_org_network.agent_card import AgentCard
from agent_org_network.runtime import Answer, AnswerChunk


# ---------------------------------------------------------------------------
# ProviderRequest — 공급자 중립 요청 값 객체 (frozen pydantic)
# ---------------------------------------------------------------------------


class ProviderRequest(BaseModel, frozen=True):
    """공급자 API 요청의 최소 공급자 중립 표현.

    Anthropic API는 system을 top-level 파라미터로 받는다 — messages가 아니라 별 필드.
    model + system(담당자 페르소나) + messages(대화 이력·질문) 3필드.
    """

    model: str
    system: str = ""
    messages: list[dict[str, str]] = []


# ---------------------------------------------------------------------------
# ProviderTransport — 주입 seam Protocol (ADR 0027 결정 2)
# ---------------------------------------------------------------------------


class ProviderTransport(Protocol):
    """인프로세스 공급자 API 스트리밍의 주입 가능 seam.

    ClaudeRunner가 _run_claude_headless를 주입받는 정신과 같다.
    호출 가능(ProviderRequest → Iterable[str] 청크 시퀀스).
    실 구현(Anthropic SDK 등)은 게이트 밖 T9.6. 테스트는 StubProviderTransport.
    """

    def __call__(self, request: ProviderRequest) -> Iterable[str]: ...


# ---------------------------------------------------------------------------
# StubProviderTransport — 결정론 transport (테스트 주입용)
# ---------------------------------------------------------------------------


class StubProviderTransport:
    """고정 청크 시퀀스를 내는 결정론 ProviderTransport.

    실 secrets·네트워크·SDK 0. 단위 테스트 주입 전용.
    """

    _DEFAULT_CHUNKS: tuple[str, ...] = ("stub 응답입니다.",)

    def __init__(self, *, chunks: Iterable[str] | None = None) -> None:
        self._chunks: tuple[str, ...] = (
            tuple(chunks) if chunks is not None else self._DEFAULT_CHUNKS
        )

    def __call__(self, request: ProviderRequest) -> Iterable[str]:
        return iter(self._chunks)


# ---------------------------------------------------------------------------
# A(ii) OKF 접지 — read_okf_bundle (순수 헬퍼, stdlib·pathlib만, SDK 0)
# ---------------------------------------------------------------------------

_OKF_MAX_CHARS = 100_000
_OKF_TRUNCATION_SUFFIX = "\n…(생략)"


def read_okf_bundle(okf_root: str | Path | None, agent_id: str) -> str:
    """owner OKF 번들 디렉터리의 *.md 파일을 읽어 프롬프트 접지용 문자열로 조립한다.

    규약: `okf_root/{agent_id}` 디렉터리가 존재할 때만 읽는다 — ClaudeCodeRuntime.bundle_dir과
    동일한 경로 규약. stdlib(pathlib)만 — 공급자 SDK import 금지(코어 중립 보존).

    반환값:
      - `okf_root is None` → `""`.
      - `bundle.is_dir()` 아니면 → `""`.
      - *.md 없으면 → `""`.
      - 있으면: 파일명 정렬 → 각 파일 `"### {파일명}\\n{내용}"` → `"\\n\\n"` 연결.
      - 방어적 상한: 총 100_000자 초과 시 자르고 `"\\n…(생략)"` 표식.
    """
    if okf_root is None:
        return ""
    bundle = Path(okf_root) / agent_id
    if not bundle.is_dir():
        return ""
    md_files = sorted(bundle.glob("*.md"))
    if not md_files:
        return ""
    sections: list[str] = []
    for md_file in md_files:
        content = md_file.read_text(encoding="utf-8")
        sections.append(f"### {md_file.name}\n{content}")
    result = "\n\n".join(sections)
    if len(result) > _OKF_MAX_CHARS:
        result = result[:_OKF_MAX_CHARS] + _OKF_TRUNCATION_SUFFIX
    return result


# ---------------------------------------------------------------------------
# 슬라이스 (b) — 순수 함수 (SDK/IO 0)
# ---------------------------------------------------------------------------


_DEFAULT_MODEL = "claude-3-5-haiku-20241022"


def build_provider_request(
    question: str,
    card: AgentCard,
    context: str | None = None,
    *,
    model: str = _DEFAULT_MODEL,
    okf: str = "",
) -> ProviderRequest:
    """공급자 API 요청을 빌드하는 순수 함수 (SDK/IO/네트워크 0).

    context는 옵셔널 — T9.1(b) assemble_context 미완이라 자리만 둔다(기본 None).
    model은 런타임이 자기 공급자 모델을 주입한다 — 기본값은 placeholder(기존 호환).
    okf는 read_okf_bundle이 읽어 온 OKF 번들 내용 — 비면 기존과 100% 동일(무회귀).
      okf가 있으면 system 프롬프트에 OKF 접지 섹션을 덧붙인다(권위 있게).
    """
    system_parts: list[str] = [
        f"당신은 '{card.team}' 팀의 담당자 {card.owner}(담당 영역: {card.agent_id})입니다.",
        f"역할 요약: {card.summary}",
    ]
    if card.domains:
        system_parts.append(f"담당 도메인: {', '.join(card.domains)}")
    if card.can_answer:
        system_parts.append(f"답할 수 있는 것: {', '.join(card.can_answer)}")
    if card.knowledge_sources:
        system_parts.append(f"근거 출처: {', '.join(card.knowledge_sources)}")
    system_parts.append(
        "위 담당자로서 동료의 질문에 한국어로 간결·실무적으로 답하세요. "
        "모르면 추측 말고 모른다고 하세요."
    )
    if okf:
        system_parts.append(
            f"## 지식 베이스(OKF) — 아래 내용에만 근거해 답하라. 여기에 없으면 모른다고 말하라.\n{okf}"
        )

    messages: list[dict[str, str]] = []
    if context is not None:
        messages.append({"role": "user", "content": context})
    messages.append({"role": "user", "content": question})

    return ProviderRequest(
        model=model,
        system="\n".join(system_parts),
        messages=messages,
    )


def assemble_stream(chunks: Iterable[str]) -> str:
    """스트리밍 청크 토막을 순서대로 조립하는 순수 함수 (SDK/IO 0).

    빈 청크("")는 무시하고 나머지를 연결한다.
    """
    return "".join(c for c in chunks if c)


def map_response_to_answer(resp: str, card: AgentCard) -> Answer:
    """공급자 응답(조립된 텍스트) → Answer 매핑 순수 함수 (노출 불변식).

    Answer 계약 보존: text·sources·mode·snapshot_sha 만 — 새 필드 없음.
    sources는 card.knowledge_sources 투영 (출처 레이블, 내부값·비밀 누출 0).
    ADR 0027 결정 3 — serialize_reply·render_mcp_notification과 같은 투영 경계.
    """
    return Answer(
        text=resp,
        sources=tuple(card.knowledge_sources),
        mode="full",
        snapshot_sha=None,
    )


# ---------------------------------------------------------------------------
# 슬라이스 (a) — ProviderApiRuntime 공급자 중립 베이스 (ADR 0027 결정 1·11)
# ---------------------------------------------------------------------------


class ProviderApiRuntime:
    """공급자 중립 AgentRuntime 포트 베이스 (ADR 0027 결정 1·11).

    어떤 공급자도 1급 아님 — claude·codex·gemini는 model+transport만 다른 같은 어댑터.
    파이프라인: read_okf_bundle(okf_root, agent_id) → build_provider_request(model, okf) →
               transport → assemble_stream → map_response_to_answer.

    okf_root 주입 시 A(ii) OKF 접지 활성: 각 answer() 호출마다 자기 번들을 로컬 파일 I/O로
    읽어 system 프롬프트에 접지 — ClaudeCodeRuntime cwd 접지와 대칭(중앙 토큰 0·격리 보존).
    """

    def __init__(
        self,
        transport: ProviderTransport,
        *,
        model: str,
        okf_root: str | Path | None = None,
    ) -> None:
        self._transport = transport
        self._model = model
        self._okf_root = okf_root

    def answer(self, question: str, card: AgentCard, context: str | None = None) -> Answer:
        okf = read_okf_bundle(self._okf_root, card.agent_id)
        request = build_provider_request(question, card, context=context, model=self._model, okf=okf)
        chunks = self._transport(request)
        text = assemble_stream(chunks)
        return map_response_to_answer(text, card)

    def answer_stream(
        self, question: str, card: AgentCard, context: str | None = None
    ) -> Iterator[AnswerChunk]:
        """`answer`의 스트리밍 형제 — 청크를 *모으지 않고* `AnswerChunk` 델타로 흘린다 (ADR 0031).

        `answer`와 같은 파이프라인(read_okf_bundle → build_provider_request → transport)이되
        `assemble_stream`으로 합치는 대신 transport 청크를 그대로 yield한다. 이로써
        `ProviderApiRuntime`(따라서 `ClaudeApiRuntime`·`CodexApiRuntime`)이 `StreamingRuntime`을
        만족 → `dispatch_stream`이 실 SDK 토큰 델타를 점진 전달한다(블로킹 1델타 폴백 탈출).
        빈 청크("")는 스킵한다(assemble_stream 정신·빈 TokenEvent 방지). 코어 `answer` 무변경.
        """
        okf = read_okf_bundle(self._okf_root, card.agent_id)
        request = build_provider_request(question, card, context=context, model=self._model, okf=okf)
        for chunk in self._transport(request):
            if chunk:
                yield AnswerChunk(text_delta=chunk)


# ---------------------------------------------------------------------------
# 슬라이스 (a) — ClaudeApiRuntime (AgentRuntime 포트 · 첫 공급자)
# ---------------------------------------------------------------------------


class ClaudeApiRuntime(ProviderApiRuntime):
    """Anthropic API + owner OAuth 구독의 AgentRuntime 포트 구현 (ADR 0027 결정 1·5).

    StubRuntime·ClaudeCodeRuntime과 같은 포트(answer(question, card) -> Answer).
    ProviderApiRuntime 베이스 상속 — model 기본값은 기존 placeholder(무회귀).
    실 OAuth·실 API 스트리밍은 게이트 밖 T9.6.
    okf_root 주입 시 A(ii) OKF 접지 활성(ClaudeCodeRuntime cwd 접지 대칭).
    """

    _DEFAULT_CLAUDE_MODEL = _DEFAULT_MODEL  # 기존 placeholder 유지(기존 테스트 무회귀)

    def __init__(
        self,
        transport: ProviderTransport,
        *,
        okf_root: str | Path | None = None,
    ) -> None:
        super().__init__(transport, model=self._DEFAULT_CLAUDE_MODEL, okf_root=okf_root)


# ---------------------------------------------------------------------------
# 슬라이스 1 — CodexApiRuntime (OpenAI Codex · 대칭 공급자 어댑터)
# ---------------------------------------------------------------------------


class CodexApiRuntime(ProviderApiRuntime):
    """OpenAI Codex API 공급자 어댑터 (ADR 0027 결정 1·11).

    ClaudeApiRuntime과 대칭 — model+transport만 다른 같은 ProviderApiRuntime 베이스.
    기본 모델: gpt-5.5 (ADR 0027 결정 10 · 설정 override 가능 · 실 시연 검증값 —
    ChatGPT 구독 codex 백엔드가 gpt-5.2-codex는 미지원, gpt-5.5·gpt-5.4 등 지원).
    실 OAuth·openai SDK·실 네트워크는 게이트 밖 슬라이스 2.
    okf_root 주입 시 A(ii) OKF 접지 활성(ClaudeApiRuntime 대칭).
    """

    _DEFAULT_CODEX_MODEL = "gpt-5.5"

    def __init__(
        self,
        transport: ProviderTransport,
        *,
        okf_root: str | Path | None = None,
    ) -> None:
        super().__init__(transport, model=self._DEFAULT_CODEX_MODEL, okf_root=okf_root)


class GeminiApiRuntime:
    """Google Gemini API 공급자 어댑터 자리 — 후속 구현 (ADR 0027 결정 5).

    ClaudeApiRuntime·CodexApiRuntime 입증 후 같은 포트·다른 transport로 추가.
    """

    def answer(self, question: str, card: AgentCard, context: str | None = None) -> Answer:
        raise NotImplementedError("GeminiApiRuntime은 후속 공급자 슬라이스(T9.6+)에서 구현한다.")
