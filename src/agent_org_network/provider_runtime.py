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

Phase 12 S2(ADR 0033 결정 1): 지식 접지 원천이 디스크(`read_okf_bundle`)→`KnowledgeStore`로
이동한다. `resolve_knowledge_text`가 그 전환 지점(스토어 우선, 부재 시 디스크 폴백) —
`ProviderApiRuntime`에 `knowledge_store` 선택 주입을 더했을 뿐 `AgentRuntime` 포트
시그니처(`answer(question, card, context) -> Answer`)·`Answer` 계약은 무변경이다.

게이트 밖: 실 OAuth·실 공급자 API·실 스트리밍·공급자 SDK (T9.6)
분류기·배치 경로의 claude -p는 잔존 (ADR 0027 결정 3 — 대화 경로만 교체)
"""

from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeVar

from pydantic import BaseModel

from agent_org_network.agent_card import AgentCard
from agent_org_network.knowledge_store import knowledge_stale_seconds
from agent_org_network.provider_retry import (
    DEFAULT_RETRY_POLICY,
    RetryPolicy,
    Sleeper,
    run_with_retry,
)
from agent_org_network.runtime import Answer, AnswerChunk

if TYPE_CHECKING:
    from agent_org_network.knowledge_store import KnowledgeStore

_T = TypeVar("_T")

Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# ProviderRequest — 공급자 중립 요청 값 객체 (frozen pydantic)
# ---------------------------------------------------------------------------


class ProviderRequest(BaseModel, frozen=True):
    """공급자 API 요청의 최소 공급자 중립 표현.

    Anthropic API는 system을 top-level 파라미터로 받는다 — messages가 아니라 별 필드.
    model + system(담당자 페르소나) + messages(대화 이력·질문) 3필드.

    `agent_id`(Phase 12·ADR 0033 결정 2·옵셔널·비용 태깅): 중앙 조직 키 1개로 부를 때
    담당자별 비용을 사후 집계할 수 있게 하는 *식별자 태그*(키를 담당자별로 쪼개지 않는다).
    실 transport(`AnthropicSdkTransport`)가 SDK `metadata`로 실어 보낸다. None이면 태깅
    없음(하위호환 — 기존 요청 형태 무회귀). 키가 아니라 *식별자*라 노출 불변식 무관.
    """

    model: str
    system: str = ""
    messages: list[dict[str, str]] = []
    agent_id: str | None = None


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
# Phase 12 S2 — resolve_knowledge_text (스토어 우선, 디스크 폴백 전환 지점)
# ---------------------------------------------------------------------------


def _bundle_text_from_documents(documents: "Iterable[object]") -> str:
    """`KnowledgeBundleContent.documents`(KnowledgeDoc들)를 read_okf_bundle과 같은
    꼴("### {path}\\n{body}"를 "\\n\\n" 연결)로 조립한다 — 접지 프롬프트 포맷 일관성."""
    sections: list[str] = []
    for doc in documents:
        path = getattr(doc, "path", "")
        body = getattr(doc, "body", "")
        sections.append(f"### {path}\n{body}")
    return "\n\n".join(sections)


def resolve_knowledge_text(
    knowledge_store: "KnowledgeStore | None",
    okf_root: str | Path | None,
    agent_id: str,
    *,
    now: datetime,
    threshold_s: int,
) -> tuple[str, bool]:
    """지식 접지 텍스트를 조회하는 전환 지점(ADR 0033 결정 1) — 스토어 우선, 디스크 폴백.

    `read_okf_bundle`(디스크)의 *입력 원천*을 `KnowledgeStore`로 옮기되, 매핑 함수·
    포트는 무변경으로 두는 최소 수술. **폴백 정책(결정 — ADR/plan에 구체 지침 없어
    tasks에 메모)**: 스토어가 주입됐고 그 agent_id 본문이 있으면 스토어를 쓴다.
    스토어가 없거나(None) 그 agent_id 본문이 없으면 기존 디스크(`read_okf_bundle`)
    경로로 폴백한다 — 회귀 0(스토어 미배선 기존 호출부는 100% 기존 동작).

    반환값: `(접지 텍스트, is_stale)`. 스토어 히트일 때만 `is_stale`을
    `store.is_stale(agent_id, now=now, threshold_s=threshold_s)`로 판정한다(디스크
    폴백·완전 부재 시엔 신선도를 논할 스토어 본문 자체가 없으므로 `False` — 디스크
    직독은 원래 신선도 개념이 없던 기존 동작 보존).
    """
    if knowledge_store is not None:
        content = knowledge_store.get(agent_id)
        if content is not None:
            text = _bundle_text_from_documents(content.documents)
            stale = knowledge_store.is_stale(agent_id, now=now, threshold_s=threshold_s)
            return text, stale
    return read_okf_bundle(okf_root, agent_id), False


# ---------------------------------------------------------------------------
# 슬라이스 (b) — 순수 함수 (SDK/IO 0)
# ---------------------------------------------------------------------------


_DEFAULT_MODEL = "claude-haiku-4-5"


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
        agent_id=card.agent_id,  # 비용 태깅(ADR 0033 결정 2) — 식별자만, 키 아님.
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

    Phase 12 S2(ADR 0033 결정 1): `knowledge_store` 주입 시 지식 접지 원천이 중앙
    `KnowledgeStore`로 전환된다(`resolve_knowledge_text` — 스토어 우선, 그 agent_id
    본문 부재 시 기존 디스크 `okf_root` 경로로 폴백). `knowledge_store` 미주입(기본
    None)이면 기존 디스크 전용 동작 100% 보존(회귀 0). `last_knowledge_stale`은
    직전 answer/answer_stream 호출의 stale 판정 결과를 담는 관측 seam이다 — stale이어도
    답 자체는 차단하지 않는다(미아 없음 보존, 표식은 신뢰 하향 신호일 뿐).

    일시 장애 재시도(provider_retry): transport 호출을 `run_with_retry`로 감싼다 — 429/5xx/
    네트워크 일시 실패는 지수 백오프로 재시도하고, 401/403은 `ProviderAuthError`로 승격,
    재시도 소진/비일시 실패는 원 예외를 그대로 재던져 기존 escalation·폴백 경로를 보존한다.
    `retry_policy`/`sleeper`는 주입 seam — 테스트는 fake sleeper로 실 sleep 0·결정론. 기본값은
    프로덕션 정책(`DEFAULT_RETRY_POLICY`)·실 `time.sleep`. StubProviderTransport는 예외를 던지지
    않으므로 재시도 경로가 무영향(무회귀 — 첫 시도에서 성공).
    """

    def __init__(
        self,
        transport: ProviderTransport,
        *,
        model: str,
        okf_root: str | Path | None = None,
        knowledge_store: "KnowledgeStore | None" = None,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        sleeper: Sleeper | None = None,
        clock: Clock = _default_clock,
    ) -> None:
        self._transport = transport
        self._model = model
        self._okf_root = okf_root
        self._knowledge_store = knowledge_store
        self._retry_policy = retry_policy
        self._sleeper = sleeper
        self._clock = clock
        # 관측 seam(ADR 0033 결정 3) — 직전 answer/answer_stream 호출의 stale 판정.
        # Answer 계약(text·sources·mode·snapshot_sha)엔 안 싣는다(노출 불변식 보존).
        self.last_knowledge_stale: bool = False

    def _with_retry(self, fn: "Callable[[], _T]") -> "_T":
        """transport 호출을 재시도 정책으로 감싼다(sleeper 주입 seam·값/부작용 분리).

        sleeper 미주입(None)이면 `run_with_retry` 기본(실 `time.sleep`). 주입 시 그 fake로
        실 sleep 0·백오프 순서 결정론 단언. 401/403은 ProviderAuthError 승격, 소진/비일시는 재던짐.
        """
        if self._sleeper is None:
            return run_with_retry(fn, policy=self._retry_policy)
        return run_with_retry(fn, policy=self._retry_policy, sleeper=self._sleeper)

    def _resolve_okf(self, agent_id: str) -> str:
        """지식 접지 텍스트를 얻고 stale 관측 seam을 갱신한다(스토어 우선, 디스크 폴백)."""
        okf, stale = resolve_knowledge_text(
            self._knowledge_store,
            self._okf_root,
            agent_id,
            now=self._clock(),
            threshold_s=knowledge_stale_seconds(),
        )
        self.last_knowledge_stale = stale
        return okf

    def answer(self, question: str, card: AgentCard, context: str | None = None) -> Answer:
        okf = self._resolve_okf(card.agent_id)
        request = build_provider_request(question, card, context=context, model=self._model, okf=okf)

        # transport 호출 + 스트림 소진을 한 시도 단위로 재시도한다 — assemble_stream이 청크를
        # 모두 끌어당기므로(제너레이터 transport의 실 실패는 소진 시점에 난다) 호출+소진을 함께
        # 감싸야 429/5xx/네트워크 실패를 잡는다. 블로킹 answer는 부분 출력 개념이 없어 전체 재시도 안전.
        def _call() -> str:
            chunks = self._transport(request)
            return assemble_stream(chunks)

        text = self._with_retry(_call)
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

        일시 장애 재시도(스트림 시작 전만): transport 호출 + *첫 청크 도달까지*만 재시도한다.
        첫 델타가 이미 흐른 뒤의 중간 실패는 **재시도하지 않는다** — 부분 출력이 소비자에게 이미
        나갔으므로 재시도하면 델타가 중복/뒤엉킨다. 중간 실패는 기존 ErrorEvent 경로(상위
        dispatch_stream이 소비 중 예외로 처리)를 그대로 탄다. 시작 전 실패(429/5xx/네트워크)만
        `run_with_retry`로 잡아 지수 백오프 재시도하고, 401/403은 `ProviderAuthError`로 승격한다.
        """
        okf = self._resolve_okf(card.agent_id)
        request = build_provider_request(question, card, context=context, model=self._model, okf=okf)

        # 스트림 시작 = transport 호출 + 첫 청크 pull. 이 지점까지의 실패만 재시도한다(부분 출력
        # 전이라 안전). 재시도로 얻은 iterator와 이미 pull한 첫 청크를 함께 돌려받아, 이후 델타는
        # 재시도 없이 그대로 흘린다(중간 실패는 소비자에게 전파 — ErrorEvent 경로 보존).
        def _start() -> tuple[Iterator[str], str | None]:
            it = iter(self._transport(request))
            first = next(it, None)  # StopIteration(빈 스트림)은 재시도 대상 아님 → None.
            return it, first

        it, first = self._with_retry(_start)
        if first is not None and first:
            yield AnswerChunk(text_delta=first)
        # 첫 청크 이후 델타 — 재시도 없이 그대로 흘린다(중간 실패는 그대로 전파).
        for chunk in it:
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
    knowledge_store 주입 시 Phase 12 S2 전환 활성(ADR 0033 결정 1 — 스토어 우선·디스크 폴백).
    """

    _DEFAULT_CLAUDE_MODEL = "claude-sonnet-5"  # adaptive thinking 지원(답변 실 LLM)·저작(LlmAuthor)과 통일

    def __init__(
        self,
        transport: ProviderTransport,
        *,
        okf_root: str | Path | None = None,
        knowledge_store: "KnowledgeStore | None" = None,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        sleeper: Sleeper | None = None,
        clock: Clock = _default_clock,
    ) -> None:
        super().__init__(
            transport,
            model=self._DEFAULT_CLAUDE_MODEL,
            okf_root=okf_root,
            knowledge_store=knowledge_store,
            retry_policy=retry_policy,
            sleeper=sleeper,
            clock=clock,
        )


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
    knowledge_store 주입 시 Phase 12 S2 전환 활성(ClaudeApiRuntime 대칭).
    """

    _DEFAULT_CODEX_MODEL = "gpt-5.5"

    def __init__(
        self,
        transport: ProviderTransport,
        *,
        okf_root: str | Path | None = None,
        knowledge_store: "KnowledgeStore | None" = None,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        sleeper: Sleeper | None = None,
        clock: Clock = _default_clock,
    ) -> None:
        super().__init__(
            transport,
            model=self._DEFAULT_CODEX_MODEL,
            okf_root=okf_root,
            knowledge_store=knowledge_store,
            retry_policy=retry_policy,
            sleeper=sleeper,
            clock=clock,
        )


class GeminiApiRuntime:
    """Google Gemini API 공급자 어댑터 자리 — 후속 구현 (ADR 0027 결정 5).

    ClaudeApiRuntime·CodexApiRuntime 입증 후 같은 포트·다른 transport로 추가.
    """

    def answer(self, question: str, card: AgentCard, context: str | None = None) -> Answer:
        raise NotImplementedError("GeminiApiRuntime은 후속 공급자 슬라이스(T9.6+)에서 구현한다.")
