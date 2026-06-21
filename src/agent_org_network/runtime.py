import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from agent_org_network.agent_card import AgentCard

# 신뢰 상태(CONTEXT Answer 절, ADR 0012 결정 4):
#   full:        owner 실시간 답, 그대로 사용자에게
#   draft_only:  Approval 게이트 — 사람 승인 전까지 초안
#   backup:      owner 위임 백업 워커의 스냅샷 기반 답(owner 미검토) — 신뢰 하향
AnswerMode = Literal["draft_only", "full", "backup"]


@dataclass(frozen=True)
class Answer:
    text: str
    sources: tuple[str, ...] = field(default_factory=tuple)
    mode: AnswerMode = "full"


class AgentRuntime(Protocol):
    def answer(self, question: str, card: AgentCard) -> Answer: ...


class ClaudeRunner(Protocol):
    """`claude -p`를 실제로 돌리는 호출 가능 객체의 모양 — 테스트는 FakeRunner로 대체한다.

    `cwd`는 *선택 키워드*다 — `ClaudeCodeRuntime`은 owner OKF 번들이 있을 때만 `cwd`를
    넘기고(번들 cwd 소비), 없으면 넘기지 않는다. 기본값을 둬 1-인자 호출(번들 없음)과
    cwd 호출(번들 있음)을 한 시그니처로 받는다 — 옛 1-인자 FakeRunner도 `**kwargs`로
    흡수하면 호환된다.
    """

    def __call__(self, prompt: str, /, *, cwd: str | None = None) -> str: ...


class StubRuntime:
    def answer(self, question: str, card: AgentCard) -> Answer:
        return Answer(
            text=f"[{card.agent_id}] {card.summary}",
            sources=tuple(card.knowledge_sources),
            mode="full",
        )


# `claude -p` 헤드리스 호출 기본값. 응답 외 잡음(상태/로그)이 stdout을 오염하거나
# 프로젝트 CLAUDE.md·MCP가 끼어드는 걸 막으려 임시 디렉터리(cwd)에서 1회성으로 돈다.
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 120

# OKF 소비 시 claude에게 여는 도구 — 읽기 전용으로 좁힌다(ADR 0013 결정 3·6, 파일 접근
# 격리). owner OKF 번들의 마크다운을 *읽기만* 하게 하고 쓰기·실행은 막는다.
OKF_ALLOWED_TOOLS = "Read,Glob,Grep"


def _run_claude_headless(
    prompt: str,
    /,
    *,
    cwd: str | None = None,
    timeout: int = DEFAULT_CLAUDE_TIMEOUT_SECONDS,
) -> str:
    """`claude -p`를 한 번 돌려 text 응답(stdout)을 돌려준다.

    `cwd`가 주어지면(owner OKF 번들 디렉터리) **그 디렉터리를 cwd로** 두고 claude에
    `--allowedTools "Read,Glob,Grep"`(읽기 전용)을 더해 claude가 번들 마크다운을 *읽어*
    답하게 한다(ADR 0013 결정 3, PoC 입증). `cwd=None`이면 응답 잡음·프로젝트 CLAUDE.md
    간섭을 막으려 **임시 디렉터리(빈 cwd)에서** 1회성으로 돈다(기존 동작·하위호환 — 도구
    없이 텍스트 답만).
    """
    if cwd is not None:
        return _exec_claude(prompt, cwd=cwd, allowed_tools=OKF_ALLOWED_TOOLS, timeout=timeout)
    with tempfile.TemporaryDirectory() as workdir:
        return _exec_claude(prompt, cwd=workdir, allowed_tools=None, timeout=timeout)


def _exec_claude(
    prompt: str,
    *,
    cwd: str,
    allowed_tools: str | None,
    timeout: int,
) -> str:
    args = ["claude", "-p", prompt, "--output-format", "text"]
    if allowed_tools is not None:
        args += ["--allowedTools", allowed_tools]
    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"claude -p exited with {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout


def _build_persona_prompt(question: str, card: AgentCard) -> str:
    """카드로 '담당자 페르소나'를 구성해 그 사람으로서 답하게 하는 프롬프트.

    cwd에 owner의 OKF 번들(마크다운+프론트매터)이 있을 수 있다 — claude가 *먼저 읽고*
    그 내용을 근거로 답하게 지시한다(ADR 0013 결정 3, PoC 프롬프트 정신). 번들이 없는
    호출(tempfile cwd)에선 읽을 게 없으므로 카드 맥락만으로 답한다.
    """
    lines: list[str] = [
        f"당신은 '{card.team}' 팀의 담당자 {card.owner}(담당 영역 ID: {card.agent_id})입니다.",
        f"역할 요약: {card.summary}",
    ]
    if card.domains:
        lines.append(f"담당 도메인: {', '.join(card.domains)}")
    if card.can_answer:
        lines.append(f"답할 수 있는 것: {', '.join(card.can_answer)}")
    if card.knowledge_sources:
        lines.append(f"근거로 삼을 출처: {', '.join(card.knowledge_sources)}")
    lines.append("")
    lines.append(
        "현재 작업 디렉터리에 당신의 지식 문서(OKF 번들 — index.md나 마크다운 파일)가 "
        "있으면 *먼저 읽고* 그 내용을 근거로 답하세요. 그런 문서가 없으면 추측하지 말고 "
        "모른다고 하세요."
    )
    lines.append("")
    lines.append(
        "위 담당자로서, 회사 동료의 다음 질문에 한국어로 간결하고 실무적으로 답하세요. "
        "모르면 추측하지 말고 모른다고 하세요. 메타 설명 없이 답변 본문만 출력하세요."
    )
    lines.append("")
    lines.append(f"질문: {question}")
    return "\n".join(lines)


class ClaudeCodeRuntime:
    """헤드리스 `claude -p` subprocess로 담당자 답을 실제 생성하는 AgentRuntime 포트.

    owner 지식 소비(ADR 0013): card가 가리키는 owner OKF 번들 디렉터리가 존재하면 그
    디렉터리를 cwd로 두고 `claude -p`를 `--allowedTools "Read,Glob,Grep"`와 함께 돌려
    claude가 번들 마크다운을 *읽어* 답하게 한다(벡터DB·RAG 0). 번들이 없으면 기존
    동작(tempfile cwd, 도구 없음)으로 카드 맥락만으로 답한다(하위호환).

    번들 경로 규약(ADR 0013 결정 2 안 B): `okf_root/{agent_id}`. `knowledge_sources`는
    카드 스키마 무변경으로 그 OKF 번들을 가리키는 *참조*(레이블)이며 `Answer.sources`로
    그대로 흐른다 — 레이블 문자열을 경로로 쓰지 않고 `agent_id` 규약으로 디렉터리를
    해석한다(기존 레이블 보존). `okf_root`는 **명시 주입**이다 — owner 환경(데모는 repo
    `okf/`)이 자기 번들 루트를 넘긴다(번들 cwd 격리, 분산 T6.3 정합). `okf_root=None`(기본)
    이면 번들 해석을 아예 하지 않아 항상 기존 동작(tempfile cwd) — 중앙 무지식·하위호환:
    암묵 cwd-상대 경로로 owner 지식을 *추정*하지 않는다(owner 환경이 명시 제공해야 소비).

    실제 호출은 비결정·느리므로 `runner`를 주입 가능하게 둬 단위테스트는 FakeRunner로
    고정한다. `runner`는 프롬프트(위치 인자)와 `cwd`(키워드, 번들 있을 때만 전달)를 받는다 —
    번들이 없으면 `cwd`를 *넘기지 않아* 1-인자 runner와도 호환된다.
    """

    def __init__(
        self,
        runner: ClaudeRunner = _run_claude_headless,
        okf_root: str | Path | None = None,
    ) -> None:
        self._runner = runner
        self._okf_root = Path(okf_root) if okf_root is not None else None

    def build_prompt(self, question: str, card: AgentCard) -> str:
        return _build_persona_prompt(question, card)

    def bundle_dir(self, card: AgentCard) -> Path | None:
        """card가 가리키는 owner OKF 번들 디렉터리(존재할 때만), 없으면 None.

        규약: `okf_root/{agent_id}`. `okf_root`가 주입되고 그 규약 경로 디렉터리가 실제로
        있어야 cwd로 쓴다 — `okf_root=None`이거나 디렉터리가 없으면 None(기존 tempfile
        동작으로 폴백·하위호환). `knowledge_sources`가 비어 있어도 규약 경로가 존재하면
        그 번들을 읽는다(번들 참조의 의미는 `agent_id`가 규약으로 진다).
        """
        if self._okf_root is None:
            return None
        candidate = self._okf_root / card.agent_id
        return candidate if candidate.is_dir() else None

    def answer(self, question: str, card: AgentCard) -> Answer:
        prompt = self.build_prompt(question, card)
        sources = tuple(card.knowledge_sources)
        bundle = self.bundle_dir(card)
        try:
            if bundle is not None:
                raw = self._runner(prompt, cwd=str(bundle))
            else:
                raw = self._runner(prompt)
        except subprocess.TimeoutExpired:
            return Answer(
                text=f"[{card.agent_id}] 담당자 응답 생성이 시간 내에 끝나지 않았습니다.",
                sources=sources,
                mode="full",
            )
        except Exception as exc:
            return Answer(
                text=f"[{card.agent_id}] 담당자 응답 생성에 실패했습니다: {exc}",
                sources=sources,
                mode="full",
            )
        text = raw.strip()
        if not text:
            return Answer(
                text=f"[{card.agent_id}] 담당자 응답이 비어 있습니다.",
                sources=sources,
                mode="full",
            )
        return Answer(text=text, sources=sources, mode="full")
