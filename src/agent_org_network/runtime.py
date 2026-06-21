import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from agent_org_network.agent_card import AgentCard

if TYPE_CHECKING:
    from agent_org_network.git_gateway import GitGateway

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
    # 답이 *어느 OKF 커밋 스냅샷*으로 만들어졌나(ADR 0018 결정 4 — "이 답은 이 커밋 기준"
    # 감사 메타). 커밋 스냅샷 모드에서 `git archive <sha>` 추출본을 cwd로 읽었을 때 그 SHA가
    # 실린다. 기본 None — working tree 직독(T6.7)·스텁/canned 경로엔 SHA 없음(하위호환).
    # `mode`·`sources`와 같은 답에 붙는 신뢰/출처 메타의 연장(노출 불변식: 운영 면 노출 OK).
    snapshot_sha: str | None = None


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

    커밋 스냅샷 모드(ADR 0018 결정 4): `git_gateway` 주입 시 `head_sha` → `extract_snapshot`
    → 추출 디렉터리를 cwd로 runner 호출 → `Answer.snapshot_sha=sha`. `okf_root`만 주면
    기존 working tree 직독(snapshot_sha=None, T6.7 하위호환).
    """

    def __init__(
        self,
        runner: ClaudeRunner = _run_claude_headless,
        okf_root: str | Path | None = None,
        git_gateway: "GitGateway | None" = None,
    ) -> None:
        self._runner = runner
        self._okf_root = Path(okf_root) if okf_root is not None else None
        self._git_gateway = git_gateway

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

        # 커밋 스냅샷 모드(ADR 0018 결정 4): git_gateway 주입 시 HEAD 스냅샷을 추출해
        # 그 디렉터리를 cwd로 runner 호출. snapshot_sha를 Answer에 실어 감사 메타 제공.
        if self._git_gateway is not None:
            try:
                sha = self._git_gateway.head_sha(card.agent_id)
            except (ValueError, KeyError):
                sha = None

            if sha is not None:
                try:
                    with tempfile.TemporaryDirectory() as workdir:
                        snap_dir = self._git_gateway.extract_snapshot(
                            sha, card.agent_id, Path(workdir)
                        )
                        try:
                            raw = self._runner(prompt, cwd=str(snap_dir))
                        except subprocess.TimeoutExpired:
                            return Answer(
                                text=f"[{card.agent_id}] 담당자 응답 생성이 시간 내에 끝나지 않았습니다.",
                                sources=sources,
                                mode="full",
                                snapshot_sha=sha,
                            )
                        text = raw.strip()
                        if not text:
                            return Answer(
                                text=f"[{card.agent_id}] 담당자 응답이 비어 있습니다.",
                                sources=sources,
                                mode="full",
                                snapshot_sha=sha,
                            )
                        return Answer(text=text, sources=sources, mode="full", snapshot_sha=sha)
                except Exception as exc:
                    return Answer(
                        text=f"[{card.agent_id}] 담당자 응답 생성에 실패했습니다: {exc}",
                        sources=sources,
                        mode="full",
                        snapshot_sha=sha,
                    )

        # 기존 working tree 직독 경로(T6.7 하위호환 — git_gateway 없음)
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
