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
    OKF_SETTING_SOURCES_ISOLATED,
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
    """프롬프트와 cwd(키워드, 번들 있을 때만 전달됨)·system_prompt를 기록하는 실 claude 대역.

    `**kwargs`로 받아 cwd 키워드가 *실제로 전달됐는지*를 정확히 감지한다(키워드 기본값이
    아니라 전달 여부 — `cwd` 미전달 시 `cwd_kw_passed=False`). `system_prompt`(노출 격리·본
    작업)도 kwargs로 흡수해 기록한다.
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_prompt: str | None = None
        self.last_cwd: str | None = None
        self.cwd_kw_passed = False
        self.last_system: str | None = None

    def __call__(self, prompt: str, **kwargs: object) -> str:
        self.last_prompt = prompt
        self.cwd_kw_passed = "cwd" in kwargs
        cwd = kwargs.get("cwd")
        self.last_cwd = cwd if isinstance(cwd, str) else None
        system = kwargs.get("system_prompt")
        self.last_system = system if isinstance(system, str) else None
        return self.reply


class _KwargsAbsorbingRunner:
    """cwd·system_prompt 등 선택 키워드를 `**kwargs`로 흡수하는 관대한 runner.

    ClaudeRunner Protocol은 이제 cwd·system_prompt를 선택 키워드로 넘긴다(노출 격리·본
    작업). `**kwargs`로 흡수하는 runner는 번들 유무·격리 유무와 무관하게 *런타임에* 깨지지
    않는다(관대한 호환 — 옛 cwd 흡수 정신의 연장).
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_prompt: str | None = None

    def __call__(self, prompt: str, **kwargs: object) -> str:
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


def test_OKF_읽기_지시는_system에_질문은_user에_들어간다(tmp_path: Path):
    _make_bundle(tmp_path, "cs_ops")
    runner = _CwdRecordingRunner("답")
    runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path)

    runtime.answer("환불 되나요?", card("cs_ops"))

    system = runner.last_system
    user = runner.last_prompt
    assert system is not None
    assert user is not None
    # cwd OKF 번들을 읽고 근거로 답하라는 지시는 system(페르소나)에 녹는다
    assert "작업 디렉터리" in system
    assert "OKF" in system
    # 질문은 user에 — OKF 근거 리마인더(읽는 과정 서술 금지)도 user에 짧게 붙는다
    assert "환불 되나요?" in user
    assert "OKF" in user


def test_OKF_지시는_번들_없어도_동일하게_구성된다():
    # build_system은 cwd와 무관하게 OKF 지시를 싣는다(번들 유무는 cwd 전달에서만 갈림).
    runtime = ClaudeCodeRuntime(runner=_CwdRecordingRunner("x"))
    system = runtime.build_system(card("cs_ops"))
    assert "OKF" in system
    assert "작업 디렉터리" in system
    # 합본 build_prompt(하위호환)에도 여전히 다 녹는다
    legacy = runtime.build_prompt("질문?", card("cs_ops"))
    assert "OKF" in legacy
    assert "질문?" in legacy


# (d) 번들이 없으면 cwd=None(키워드 미전달) — 기존 tempfile 동작·하위호환 ─────────


def test_번들이_없으면_cwd_키워드를_전달하지_않는다(tmp_path: Path):
    # 빈 okf_root → 번들 없음 → cwd 키워드 없이 1-인자 호출(옛 runner와 호환).
    runner = _CwdRecordingRunner("폴백 답")
    runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path)

    ans = runtime.answer("질문?", card("cs_ops"))

    assert runner.cwd_kw_passed is False  # cwd 키워드가 아예 전달되지 않음
    assert runner.last_cwd is None
    assert ans.text == "폴백 답"


def test_번들_없으면_kwargs흡수_runner도_동작한다(tmp_path: Path):
    # 선택 키워드(cwd·system_prompt)를 `**kwargs`로 흡수하는 runner는 번들 없는 경로에서도
    # *런타임에* 깨지지 않는다(관대한 호환 — 노출 격리로 system_prompt가 항상 넘어가도 OK).
    runner = _KwargsAbsorbingRunner("kwargs 흡수 답")
    runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path)

    ans = runtime.answer("질문?", card("cs_ops"))

    assert runner.last_prompt is not None
    assert ans.text == "kwargs 흡수 답"


def test_번들이_있으면_system_prompt가_runner에_전달된다(tmp_path: Path):
    # 노출 격리(본 작업): 번들 cwd 경로에서도 페르소나 system_prompt가 runner로 넘어간다.
    _make_bundle(tmp_path, "cs_ops")
    runner = _CwdRecordingRunner("답")
    runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path)

    runtime.answer("질문?", card("cs_ops"))

    assert runner.last_system is not None
    assert "cs_lead" in runner.last_system


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


def test_run_headless_system_prompt면_system과_setting_sources_격리_플래그(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 노출 격리(본 작업·실증): system_prompt가 주어지면 `--system-prompt`(기본 프롬프트 교체)
    # 와 `--setting-sources ""`(설정·메모리·CLAUDE.md 로드 차단)가 함께 args에 실린다.
    fake = _CapturedRun(stdout="ok")
    monkeypatch.setattr(subprocess, "run", fake)

    out = _run_claude_headless(
        "user 질문", cwd=str(tmp_path), system_prompt="당신은 계약 담당자입니다."
    )

    assert out == "ok"
    assert fake.args is not None
    assert "--system-prompt" in fake.args
    sys_idx = fake.args.index("--system-prompt")
    assert fake.args[sys_idx + 1] == "당신은 계약 담당자입니다."
    assert "--setting-sources" in fake.args
    ss_idx = fake.args.index("--setting-sources")
    assert fake.args[ss_idx + 1] == OKF_SETTING_SOURCES_ISOLATED
    # OKF 접지(읽기 도구)는 그대로 유지된다 — 격리와 접지는 양립한다
    assert "--allowedTools" in fake.args
    assert OKF_ALLOWED_TOOLS in fake.args


def test_run_headless_system_prompt_없으면_격리_플래그_없음(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 하위호환: system_prompt 미전달(기본 None)이면 격리 플래그가 안 붙는다(옛 동작).
    fake = _CapturedRun(stdout="ok")
    monkeypatch.setattr(subprocess, "run", fake)

    _run_claude_headless("prompt", cwd=str(tmp_path))

    assert fake.args is not None
    assert "--system-prompt" not in fake.args
    assert "--setting-sources" not in fake.args


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


# 커밋 스냅샷 모드(ADR 0018 결정 4) — git_gateway 주입 시 head_sha 번들을 cwd로 접지 ──
# 저작→답변 루프 버그 회귀 방지: 게이트웨이에 커밋한 OKF 본문(시드+저작)이 답변 런타임의
# cwd로 닿아야 한다. 실 claude 0(FakeRunner가 cwd만 관측)·실 git 0(FakeGitGateway in-memory).


def _seed_gateway(agent_id: str, owner: str, files: dict[str, str]):
    """FakeGitGateway에 OKF 번들 1커밋을 넣고 게이트웨이를 돌려준다(결정론)."""
    from agent_org_network.git_gateway import (
        BuilderCommitRequest,
        FakeGitGateway,
        OkfFile,
        commit_okf_bundle,
    )

    gw = FakeGitGateway()
    commit_okf_bundle(
        BuilderCommitRequest(
            agent_id=agent_id,
            owner=owner,
            files=tuple(OkfFile(path=p, content=c) for p, c in files.items()),
            message="seed",
        ),
        gw,
    )
    return gw


def test_git_gateway_주입시_커밋_스냅샷을_cwd로_접지한다(tmp_path: Path):
    # 핵심 통합: 게이트웨이에 커밋한 번들이 runner cwd로 추출돼 그 디렉터리에 본문 파일이 있다.
    gw = _seed_gateway(
        "finance_ops",
        "finance_lead",
        {"compensation.md": "정산 오류 보상 처리 기한은 영업일 5일입니다."},
    )

    observed_cwd: dict[str, str | None] = {}

    def runner(prompt: str, **kwargs: object) -> str:
        cwd = kwargs.get("cwd")
        observed_cwd["cwd"] = cwd if isinstance(cwd, str) else None
        # 스냅샷 모드는 답변 *도중*에만 cwd가 존재(임시 디렉터리)하므로 여기서 읽어 확인한다.
        if isinstance(cwd, str):
            observed_cwd["body"] = (Path(cwd) / "compensation.md").read_text(encoding="utf-8")
        return "보상 처리 기한은 영업일 5일입니다."

    runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path, git_gateway=gw)
    ans = runtime.answer("정산 오류 보상 처리 기한?", card("finance_ops", owner="finance_lead"))

    assert observed_cwd["cwd"] is not None
    assert observed_cwd["body"] == "정산 오류 보상 처리 기한은 영업일 5일입니다."
    assert isinstance(ans, Answer)
    assert ans.snapshot_sha == gw.head_sha("finance_ops")


def test_git_gateway가_okf_root보다_우선한다(tmp_path: Path):
    # okf_root 디스크에도 같은 agent 번들이 있지만, 게이트웨이 커밋이 우선이어야 한다(SSOT=게이트웨이).
    disk_bundle = _make_bundle(tmp_path, "finance_ops")
    (disk_bundle / "compensation.md").write_text("디스크 본문(낡음)", encoding="utf-8")
    gw = _seed_gateway(
        "finance_ops", "finance_lead", {"compensation.md": "게이트웨이 본문(최신)"}
    )

    observed: dict[str, str] = {}

    def runner(prompt: str, **kwargs: object) -> str:
        cwd = kwargs.get("cwd")
        assert isinstance(cwd, str)
        observed["body"] = (Path(cwd) / "compensation.md").read_text(encoding="utf-8")
        observed["cwd"] = cwd
        return "ok"

    runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path, git_gateway=gw)
    runtime.answer("질문", card("finance_ops", owner="finance_lead"))

    # 게이트웨이 스냅샷(임시 dir)이 cwd — 디스크 번들 경로가 아니다.
    assert observed["cwd"] != str(disk_bundle)
    assert observed["body"] == "게이트웨이 본문(최신)"


def test_git_gateway에_커밋_없으면_okf_root_직독으로_폴백한다(tmp_path: Path):
    # 게이트웨이는 있지만 그 agent에 커밋이 없으면 head_sha ValueError → 디스크 번들 직독(하위호환).
    from agent_org_network.git_gateway import FakeGitGateway

    bundle = _make_bundle(tmp_path, "cs_ops")
    runner = _CwdRecordingRunner("디스크 폴백 답")
    runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path, git_gateway=FakeGitGateway())

    ans = runtime.answer("질문", card("cs_ops"))

    assert runner.last_cwd == str(bundle)  # 디스크 번들 직독
    assert ans.snapshot_sha is None  # 스냅샷 모드 미발동
    assert ans.text == "디스크 폴백 답"
