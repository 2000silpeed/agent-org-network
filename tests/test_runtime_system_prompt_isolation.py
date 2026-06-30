"""노출 불변식 격리(본 작업) — `--system-prompt`·`--setting-sources` args 조립 검증.

실증(직접 `claude -p`): owner OKF 번들 cwd가 repo 안이면 `claude -p`가 *기본 동작*으로
repo·글로벌 `CLAUDE.md`(개발 규칙)·메모리를 답변 에이전트 컨텍스트로 로드해 그 dev 지침·
과정 narration을 사용자 답변에 흘린다(노출 불변식 위반). `--system-prompt`만으로는 적대적
질문에 *불충분*하고, `--system-prompt`(기본 프롬프트 교체) + `--setting-sources ""`(설정·
메모리 로드 차단) 조합이라야 누출 0 + OKF 접지 유지 + narration 억제다.

여기서는 실 subprocess 없이 args 조립만 검증한다 — 블로킹(`subprocess.run`)·스트리밍
(`subprocess.Popen`) 둘 다 system_prompt가 주어지면 `--system-prompt`·`--setting-sources`를
싣는지(둘 다 누출 가능). 실 claude 호출 0(결정론).
"""

import subprocess
from datetime import date
from pathlib import Path

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.runtime import (
    OKF_ALLOWED_TOOLS,
    OKF_SETTING_SOURCES_ISOLATED,
    ClaudeCodeRuntime,
    build_persona_system,
    build_user_prompt,
)
from agent_org_network.runtime import (
    _stream_claude_headless,  # noqa: E402  # pyright: ignore[reportPrivateUsage]
)


def card(
    agent_id: str = "contract_ops",
    owner: str = "contract_lead",
    knowledge_sources: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary="계약 검토와 표준 조건을 안내합니다.",
        domains=["계약 검토"],
        last_reviewed_at=date(2026, 6, 20),
        knowledge_sources=knowledge_sources or [],
    )


# ── system/user 분리 빌더 단위 ────────────────────────────────────────────────


def test_system에_페르소나와_no_narration_규칙이_들어간다():
    c = card(knowledge_sources=["Notion/표준계약서"])
    system = build_persona_system(c)

    # 페르소나
    assert "contract_lead" in system
    assert "contract_ops" in system
    assert "Notion/표준계약서" in system
    # no-narration·1인칭·격리 규칙
    assert "1인칭" in system
    assert "설명하지 마세요" in system
    assert "노출하지 마세요" in system


def test_user에_질문이_들어가고_페르소나는_안_섞인다():
    c = card(owner="secret_owner", knowledge_sources=["Notion/표준계약서"])
    user = build_user_prompt("표준 계약 위약금 얼마인가요?", c)

    assert "표준 계약 위약금 얼마인가요?" in user
    # 페르소나·정체성·출처는 user에 안 섞인다(system 전용)
    assert "secret_owner" not in user
    assert "Notion/표준계약서" not in user


def test_system과_user는_안_섞인다():
    c = card(owner="x_owner")
    system = build_persona_system(c)
    user = build_user_prompt("질문 본문?", c)
    # 교집합 키워드(owner)는 system에만, 질문은 user에만
    assert "x_owner" in system and "x_owner" not in user
    assert "질문 본문?" in user and "질문 본문?" not in system


# ── 블로킹: ClaudeCodeRuntime.answer가 system_prompt를 runner에 넘긴다 ─────────


class _ArgsCapturingRun:
    """`subprocess.run`을 가로채 args를 잡고 고정 stdout을 돌려주는 대역."""

    def __init__(self, stdout: str = "ok") -> None:
        self._stdout = stdout
        self.args: list[str] | None = None

    def __call__(self, args: list[str], **kwargs: object) -> "subprocess.CompletedProcess[str]":
        self.args = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=self._stdout, stderr="")


def test_answer_블로킹이_system과_setting_sources_플래그를_싣는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 기본 runner(_run_claude_headless)로 실 subprocess.run만 monkeypatch — answer가
    # system_prompt를 배선하면 args에 `--system-prompt`·`--setting-sources`가 실린다.
    fake = _ArgsCapturingRun(stdout="계약 검토 답변입니다.")
    monkeypatch.setattr(subprocess, "run", fake)

    runtime = ClaudeCodeRuntime()  # 기본 runner — 실 subprocess.run만 가짜
    ans = runtime.answer("표준 계약 위약금?", card())

    assert ans.text == "계약 검토 답변입니다."
    assert fake.args is not None
    assert "--system-prompt" in fake.args
    sys_idx = fake.args.index("--system-prompt")
    # system에 페르소나가 실려 있다(질문은 user `-p`에)
    assert "contract_lead" in fake.args[sys_idx + 1]
    assert "--setting-sources" in fake.args
    ss_idx = fake.args.index("--setting-sources")
    assert fake.args[ss_idx + 1] == OKF_SETTING_SOURCES_ISOLATED
    # user `-p` 인자는 질문 중심(페르소나 미포함)
    p_idx = fake.args.index("-p")
    user_arg = fake.args[p_idx + 1]
    assert "표준 계약 위약금?" in user_arg
    assert "contract_lead" not in user_arg


# ── 스트리밍: _stream_claude_headless가 system·setting-sources를 싣는다 ────────


class _EmptyStdout:
    """`for line in proc.stdout`(빈 이터레이터)와 `proc.stdout.close()`를 둘 다 지원하는 대역."""

    def __iter__(self) -> "_EmptyStdout":
        return self

    def __next__(self) -> str:
        raise StopIteration

    def close(self) -> None:
        return None


class _ArgsCapturingPopen:
    """`subprocess.Popen`을 가로채 args를 잡고, stdout으로 빈 이벤트 스트림을 흘린다.

    `_exec_claude_stream`은 줄 단위 JSON을 읽어 `text_delta`만 yield한다 — 여기선 델타 없이
    즉시 종료(빈 stdout)해 args 조립만 본다. returncode 0으로 정상 종료.
    """

    def __init__(self) -> None:
        self.args: list[str] | None = None
        self.stdout = _EmptyStdout()
        self.stderr = None
        self.returncode = 0

    def __call__(self, args: list[str], **kwargs: object) -> "_ArgsCapturingPopen":
        self.args = args
        return self

    def wait(self) -> int:
        return 0


def test_stream_헤드리스가_system과_setting_sources_플래그를_싣는다(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _ArgsCapturingPopen()
    monkeypatch.setattr(subprocess, "Popen", fake)

    list(
        _stream_claude_headless(
            "user 질문", cwd=str(tmp_path), system_prompt="당신은 계약 담당자입니다."
        )
    )

    assert fake.args is not None
    assert "--system-prompt" in fake.args
    sys_idx = fake.args.index("--system-prompt")
    assert fake.args[sys_idx + 1] == "당신은 계약 담당자입니다."
    assert "--setting-sources" in fake.args
    ss_idx = fake.args.index("--setting-sources")
    assert fake.args[ss_idx + 1] == OKF_SETTING_SOURCES_ISOLATED
    # 스트리밍 포맷·접지 유지
    assert "stream-json" in fake.args
    assert "--allowedTools" in fake.args
    assert OKF_ALLOWED_TOOLS in fake.args


def test_stream_헤드리스_system없으면_격리_플래그_없음(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _ArgsCapturingPopen()
    monkeypatch.setattr(subprocess, "Popen", fake)

    list(_stream_claude_headless("user 질문", cwd=str(tmp_path)))

    assert fake.args is not None
    assert "--system-prompt" not in fake.args
    assert "--setting-sources" not in fake.args
