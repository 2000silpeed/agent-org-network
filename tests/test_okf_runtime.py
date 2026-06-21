"""ClaudeCodeRuntime의 owner OKF 번들 cwd 소비 테스트 (T6.7, ADR 0013) — 결정론.

실 `claude -p`·실 파일 탐색은 절대 호출하지 않는다(비결정·느림 — eval/수동 시연 영역).
여기서는 FakeRunner·monkeypatch로 (a) 번들이 있으면 그 디렉터리가 cwd로 `_run_claude_headless`
에 전달되는지, (b) cwd가 주어지면 `--allowedTools "Read,Glob,Grep"` 플래그가 붙는지,
(c) 프롬프트에 "cwd OKF 읽고 근거로 답" 지시가 들어가는지, (d) 번들이 없으면 cwd=None(=cwd
키워드 미전달, 기존 tempfile 동작)인지만 고정 검증한다.
"""

import subprocess
from datetime import date
from pathlib import Path

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.runtime import (
    OKF_ALLOWED_TOOLS,
    Answer,
    ClaudeCodeRuntime,
)

# subprocess args/cwd/플래그를 함수 레벨에서 직접 검증하려고 모듈 내부 함수를 끌어온다
# (b) — ClaudeCodeRuntime 경유로는 runner가 가려 플래그가 안 보인다. 의도된 내부 접근.
from agent_org_network.runtime import _run_claude_headless  # noqa: E402  # pyright: ignore[reportPrivateUsage]


def card(
    agent_id: str = "cs_ops",
    owner: str = "cs_lead",
    knowledge_sources: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary="환불 정책과 처리 절차를 안내합니다.",
        domains=["환불", "보상"],
        last_reviewed_at=date(2026, 6, 20),
        knowledge_sources=knowledge_sources or [],
    )


class _CwdRecordingRunner:
    """프롬프트와 cwd(키워드, 번들 있을 때만 전달됨)를 기록하는 실 claude 대역.

    `**kwargs`로 받아 cwd 키워드가 *실제로 전달됐는지*를 정확히 감지한다(키워드 기본값이
    아니라 전달 여부 — `cwd` 미전달 시 `cwd_kw_passed=False`).
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_prompt: str | None = None
        self.last_cwd: str | None = None
        self.cwd_kw_passed = False

    def __call__(self, prompt: str, **kwargs: object) -> str:
        self.last_prompt = prompt
        self.cwd_kw_passed = "cwd" in kwargs
        cwd = kwargs.get("cwd")
        self.last_cwd = cwd if isinstance(cwd, str) else None
        return self.reply


class _LegacyOneArgRunner:
    """cwd 키워드를 *받지 못하는* 옛 1-인자 runner — 하위호환 검증용(번들 없을 때)."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_prompt: str | None = None

    def __call__(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.reply


def _make_bundle(root: Path, agent_id: str) -> Path:
    bundle = root / agent_id
    bundle.mkdir(parents=True)
    (bundle / "index.md").write_text(
        "---\ntype: index\n---\n# 번들\n", encoding="utf-8"
    )
    return bundle


# (a) 번들이 있으면 그 디렉터리가 cwd로 runner에 전달된다 ──────────────────────


def test_번들이_있으면_cwd가_그_경로로_전달된다(tmp_path: Path):
    bundle = _make_bundle(tmp_path, "cs_ops")
    runner = _CwdRecordingRunner("환불액은 45,000원입니다.")
    runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path)

    ans = runtime.answer("20일 단순변심 환불 얼마?", card("cs_ops"))

    assert runner.cwd_kw_passed is True
    assert runner.last_cwd == str(bundle)
    assert isinstance(ans, Answer)
    assert ans.text == "환불액은 45,000원입니다."


def test_bundle_dir는_규약경로가_존재할때만_돌려준다(tmp_path: Path):
    runtime = ClaudeCodeRuntime(runner=_CwdRecordingRunner("x"), okf_root=tmp_path)
    # 아직 디렉터리 없음 → None
    assert runtime.bundle_dir(card("cs_ops")) is None
    bundle = _make_bundle(tmp_path, "cs_ops")
    # 규약 경로 okf_root/{agent_id} 생성 후 → 그 경로
    assert runtime.bundle_dir(card("cs_ops")) == bundle


def test_번들_경로는_agent_id_규약이다_레이블이_아니라(tmp_path: Path):
    # knowledge_sources(레이블)는 경로로 쓰이지 않는다 — agent_id가 규약으로 경로를 진다.
    bundle = _make_bundle(tmp_path, "cs_ops")
    runner = _CwdRecordingRunner("ok")
    runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path)

    c = card("cs_ops", knowledge_sources=["위키/환불정책", "Notion/없는경로"])
    runtime.answer("질문?", c)

    assert runner.last_cwd == str(bundle)


# (c) 프롬프트에 OKF cwd 소비 지시가 들어간다 ──────────────────────────────────


def test_프롬프트에_OKF_읽기_지시가_들어간다(tmp_path: Path):
    _make_bundle(tmp_path, "cs_ops")
    runner = _CwdRecordingRunner("답")
    runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path)

    runtime.answer("환불 되나요?", card("cs_ops"))

    prompt = runner.last_prompt
    assert prompt is not None
    # cwd OKF 번들을 먼저 읽고 근거로 답하라는 지시(PoC 프롬프트 정신)
    assert "작업 디렉터리" in prompt
    assert "OKF" in prompt
    assert "먼저 읽" in prompt
    # 번들 없는 호출에서도 프롬프트는 같게 구성된다(지시는 카드 기반, 번들 유무 무관)
    assert "환불 되나요?" in prompt


def test_프롬프트_지시는_번들_없어도_동일하게_구성된다():
    # build_prompt는 cwd와 무관하게 OKF 지시를 싣는다(번들 유무는 cwd 전달에서만 갈림).
    runtime = ClaudeCodeRuntime(runner=_CwdRecordingRunner("x"))
    prompt = runtime.build_prompt("질문?", card("cs_ops"))
    assert "OKF" in prompt
    assert "먼저 읽" in prompt


# (d) 번들이 없으면 cwd=None(키워드 미전달) — 기존 tempfile 동작·하위호환 ─────────


def test_번들이_없으면_cwd_키워드를_전달하지_않는다(tmp_path: Path):
    # 빈 okf_root → 번들 없음 → cwd 키워드 없이 1-인자 호출(옛 runner와 호환).
    runner = _CwdRecordingRunner("폴백 답")
    runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path)

    ans = runtime.answer("질문?", card("cs_ops"))

    assert runner.cwd_kw_passed is False  # cwd 키워드가 아예 전달되지 않음
    assert runner.last_cwd is None
    assert ans.text == "폴백 답"


def test_번들_없으면_옛_1인자_runner도_동작한다(tmp_path: Path):
    # cwd 키워드를 받지 못하는 옛 runner도 번들 없는 경로에선 *런타임에* 깨지지 않는다
    # (하위호환 핵심 — answer가 번들 없으면 cwd를 안 넘긴다). 타입상 ClaudeRunner Protocol
    # (cwd 선택 키워드)과는 불일치하므로 의도적으로 ignore해 런타임 호환만 증명한다.
    runner = _LegacyOneArgRunner("옛 runner 답")
    runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path)  # pyright: ignore[reportArgumentType]

    ans = runtime.answer("질문?", card("cs_ops"))

    assert runner.last_prompt is not None
    assert ans.text == "옛 runner 답"


def test_sources는_번들_유무와_무관하게_knowledge_sources를_보존한다(tmp_path: Path):
    _make_bundle(tmp_path, "cs_ops")
    runner = _CwdRecordingRunner("답")
    runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path)

    ans = runtime.answer("질문?", card("cs_ops", knowledge_sources=["위키/환불정책"]))

    assert ans.sources == ("위키/환불정책",)
    assert ans.mode == "full"


# (b) cwd가 주어지면 --allowedTools 읽기전용 플래그가 붙는다(subprocess 레벨) ──────


class _CapturedRun:
    """`subprocess.run`을 가로채 args·cwd를 기록하고 고정 결과를 돌려주는 대역."""

    def __init__(self, returncode: int = 0, stdout: str = "ok", stderr: str = "") -> None:
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.args: list[str] | None = None
        self.cwd: str | None = None

    def __call__(
        self, args: list[str], **kwargs: object
    ) -> "subprocess.CompletedProcess[str]":
        self.args = args
        cwd = kwargs.get("cwd")
        self.cwd = cwd if isinstance(cwd, str) else None
        return subprocess.CompletedProcess(
            args=args,
            returncode=self._returncode,
            stdout=self._stdout,
            stderr=self._stderr,
        )


def test_run_headless_cwd면_allowedTools_읽기전용_플래그(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _CapturedRun(stdout="ok")
    monkeypatch.setattr(subprocess, "run", fake)

    out = _run_claude_headless("prompt", cwd=str(tmp_path))

    assert out == "ok"
    assert fake.args is not None
    assert "--allowedTools" in fake.args
    assert OKF_ALLOWED_TOOLS in fake.args
    # cwd가 그 번들 디렉터리로 실행된다(tempfile이 아니라)
    assert fake.cwd == str(tmp_path)


def test_run_headless_cwd_None이면_도구플래그_없고_tempfile_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _CapturedRun(stdout="ok")
    monkeypatch.setattr(subprocess, "run", fake)

    out = _run_claude_headless("prompt")

    assert out == "ok"
    assert fake.args is not None
    # 도구 플래그 없음(기존 동작 — 텍스트 답만)
    assert "--allowedTools" not in fake.args
    # tempfile cwd(임시 디렉터리) — 호출 시점엔 존재했고 컨텍스트 종료로 사라진다.
    assert fake.cwd is not None
    assert fake.cwd != ""


def test_run_headless_비정상_종료면_RuntimeError(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _CapturedRun(returncode=1, stdout="", stderr="boom")
    monkeypatch.setattr(subprocess, "run", fake)

    with pytest.raises(RuntimeError, match="boom"):
        _run_claude_headless("prompt", cwd=str(tmp_path))
