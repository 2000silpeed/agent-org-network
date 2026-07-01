"""T7.2 슬라이스 1·2·3 — GitGateway / commit_okf_bundle / 커밋 스냅샷 모드 결정론 테스트.

FakeGitGateway·FakeRunner·tmp_path 만 쓴다. 실 git·실 claude·실 네트워크 0.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.git_gateway import (
    BuilderCommitRequest,
    CommitRequest,
    CommitResult,
    FakeGitGateway,
    OkfFile,
    commit_okf_bundle,
)
from agent_org_network.runtime import ClaudeCodeRuntime


def _card(agent_id: str = "cs_ops", owner: str = "cs_lead") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary="환불 안내",
        domains=["환불"],
        last_reviewed_at=date(2026, 6, 20),
    )


class _FakeRunner:
    """cwd를 기록하고 고정 답을 돌려주는 FakeRunner."""

    def __init__(self, reply: str = "스냅샷 답") -> None:
        self.reply = reply
        self.last_cwd: str | None = None
        self.cwd_passed = False

    def __call__(self, prompt: str, **kwargs: object) -> str:
        self.cwd_passed = "cwd" in kwargs
        cwd = kwargs.get("cwd")
        self.last_cwd = cwd if isinstance(cwd, str) else None
        return self.reply


# ═══════════════════════════════════════════════════════════════════════════
# 슬라이스 1 — FakeGitGateway
# ═══════════════════════════════════════════════════════════════════════════


class TestFakeGitGateway커밋성립:
    def _req(
        self,
        agent_id: str = "cs_ops",
        files: tuple[OkfFile, ...] | None = None,
        author: str = "cs_lead",
        message: str = "초기 커밋",
    ) -> CommitRequest:
        if files is None:
            files = (OkfFile(path="index.md", content="# 안내\n"),)
        return CommitRequest(
            agent_id=agent_id,
            files=files,
            author=author,
            message=message,
        )

    def test_커밋_성립_CommitResult_반환(self) -> None:
        gw = FakeGitGateway()
        result = gw.commit_bundle(self._req())
        assert isinstance(result, CommitResult)
        assert result.agent_id == "cs_ops"
        assert result.sha != ""

    def test_author_보존(self) -> None:
        gw = FakeGitGateway()
        gw.commit_bundle(self._req(author="cs_lead"))
        req = gw._requests["cs_ops"][0]  # pyright: ignore[reportPrivateUsage]
        assert req.author == "cs_lead"

    def test_message_보존(self) -> None:
        gw = FakeGitGateway()
        gw.commit_bundle(self._req(message="환불 정책 갱신"))
        req = gw._requests["cs_ops"][0]  # pyright: ignore[reportPrivateUsage]
        assert req.message == "환불 정책 갱신"

    def test_결정_SHA_같은_시퀀스_항상_동일(self) -> None:
        gw1 = FakeGitGateway()
        gw2 = FakeGitGateway()
        sha1 = gw1.commit_bundle(self._req()).sha
        sha2 = gw2.commit_bundle(self._req()).sha
        assert sha1 == sha2

    def test_순차_커밋_SHA_구분된다(self) -> None:
        gw = FakeGitGateway()
        sha_a = gw.commit_bundle(self._req(message="1차")).sha
        sha_b = gw.commit_bundle(self._req(message="2차")).sha
        assert sha_a != sha_b

    def test_다른_agent_id_SHA_구분된다(self) -> None:
        gw = FakeGitGateway()
        sha_cs = gw.commit_bundle(self._req(agent_id="cs_ops")).sha
        sha_legal = gw.commit_bundle(self._req(agent_id="legal_ops")).sha
        assert sha_cs != sha_legal


class TestFakeGitGateway경로탈출거부:
    def _commit(self, path: str) -> None:
        gw = FakeGitGateway()
        req = CommitRequest(
            agent_id="cs_ops",
            files=(OkfFile(path=path, content="내용"),),
            author="cs_lead",
            message="테스트",
        )
        gw.commit_bundle(req)

    def test_빈_경로_거부(self) -> None:
        with pytest.raises(ValueError, match="비어"):
            self._commit("")

    def test_공백만_경로_거부(self) -> None:
        with pytest.raises(ValueError, match="비어"):
            self._commit("   ")

    def test_절대경로_거부(self) -> None:
        with pytest.raises(ValueError, match="절대 경로"):
            self._commit("/etc/passwd")

    def test_상위_탈출_거부(self) -> None:
        with pytest.raises(ValueError, match="탈출"):
            self._commit("../outside.md")

    def test_중간_상위_탈출_거부(self) -> None:
        with pytest.raises(ValueError, match="탈출"):
            self._commit("sub/../../outside.md")

    def test_정상_상대경로_통과(self) -> None:
        gw = FakeGitGateway()
        req = CommitRequest(
            agent_id="cs_ops",
            files=(OkfFile(path="sub/policy.md", content="내용"),),
            author="cs_lead",
            message="정상",
        )
        result = gw.commit_bundle(req)
        assert result.sha != ""


class TestFakeGitGatewayAgentId탈출거부:
    """B1: agent_id 경로 탈출 차단 — commit_bundle·extract_snapshot 양쪽(계약 일치).

    in-memory라 실 파일 피해는 없지만, SubprocessGitGateway와 *같은 ValueError*를 내야
    Protocol 계약이 일관된다(안전 경계 단일화 — validate_agent_id 공유).
    """

    def _commit(self, agent_id: str) -> None:
        gw = FakeGitGateway()
        req = CommitRequest(
            agent_id=agent_id,
            files=(OkfFile(path="policy.md", content="내용"),),
            author="cs_lead",
            message="테스트",
        )
        gw.commit_bundle(req)

    def test_commit_상위_탈출_agent_id_거부(self) -> None:
        with pytest.raises(ValueError):
            self._commit("../evil")

    def test_commit_단일_점점_agent_id_거부(self) -> None:
        with pytest.raises(ValueError):
            self._commit("..")

    def test_commit_절대경로_agent_id_거부(self) -> None:
        with pytest.raises(ValueError):
            self._commit("/etc")

    def test_commit_경로구분자_agent_id_거부(self) -> None:
        with pytest.raises(ValueError):
            self._commit("a/b")

    def test_commit_빈_agent_id_거부(self) -> None:
        with pytest.raises(ValueError):
            self._commit("")

    def test_commit_공백만_agent_id_거부(self) -> None:
        with pytest.raises(ValueError):
            self._commit("   ")

    def test_extract_상위_탈출_agent_id_거부(self, tmp_path: Path) -> None:
        gw = FakeGitGateway()
        with pytest.raises(ValueError):
            gw.extract_snapshot("deadbeef" * 5, "../evil", tmp_path)

    def test_extract_절대경로_agent_id_거부(self, tmp_path: Path) -> None:
        gw = FakeGitGateway()
        with pytest.raises(ValueError):
            gw.extract_snapshot("deadbeef" * 5, "/etc", tmp_path)

    def test_extract_경로구분자_agent_id_거부(self, tmp_path: Path) -> None:
        gw = FakeGitGateway()
        with pytest.raises(ValueError):
            gw.extract_snapshot("deadbeef" * 5, "a/b", tmp_path)


class TestFakeGitGatewayHEAD:
    def test_HEAD_sha_마지막_커밋(self) -> None:
        gw = FakeGitGateway()
        req = CommitRequest(
            agent_id="cs_ops",
            files=(OkfFile(path="a.md", content="a"),),
            author="cs_lead",
            message="첫",
        )
        first = gw.commit_bundle(req)
        second = gw.commit_bundle(req)
        assert gw.head_sha("cs_ops") == second.sha
        assert gw.head_sha("cs_ops") != first.sha

    def test_커밋_없는_HEAD_ValueError(self) -> None:
        gw = FakeGitGateway()
        with pytest.raises(ValueError, match="커밋 없음"):
            gw.head_sha("no_such_agent")


# ═══════════════════════════════════════════════════════════════════════════
# 슬라이스 2 — commit_okf_bundle 오케스트레이션
# ═══════════════════════════════════════════════════════════════════════════


class TestCommitOkfBundle:
    def _make_req(
        self,
        agent_id: str = "cs_ops",
        owner: str = "cs_lead",
        files: tuple[OkfFile, ...] | None = None,
        message: str = "정책 갱신",
    ) -> BuilderCommitRequest:
        if files is None:
            files = (OkfFile(path="policy.md", content="# 환불\n"),)
        return BuilderCommitRequest(
            agent_id=agent_id,
            owner=owner,
            files=files,
            message=message,
        )

    def test_커밋_성립_SHA_반환(self) -> None:
        gw = FakeGitGateway()
        result = commit_okf_bundle(self._make_req(), gw)
        assert isinstance(result, CommitResult)
        assert result.sha != ""
        assert result.agent_id == "cs_ops"

    def test_author는_owner로_설정된다(self) -> None:
        gw = FakeGitGateway()
        commit_okf_bundle(self._make_req(owner="cs_lead"), gw)
        req = gw._requests["cs_ops"][0]  # pyright: ignore[reportPrivateUsage]
        assert req.author == "cs_lead"

    def test_다른_owner_author_구분(self) -> None:
        gw = FakeGitGateway()
        commit_okf_bundle(self._make_req(agent_id="cs_ops", owner="cs_lead"), gw)
        commit_okf_bundle(
            BuilderCommitRequest(
                agent_id="legal_ops",
                owner="legal_lead",
                files=(OkfFile(path="contract.md", content="계약"),),
                message="계약 갱신",
            ),
            gw,
        )
        assert gw._requests["cs_ops"][0].author == "cs_lead"  # pyright: ignore[reportPrivateUsage]
        assert gw._requests["legal_ops"][0].author == "legal_lead"  # pyright: ignore[reportPrivateUsage]

    def test_경로_탈출_거부(self) -> None:
        gw = FakeGitGateway()
        bad_req = BuilderCommitRequest(
            agent_id="cs_ops",
            owner="cs_lead",
            files=(OkfFile(path="../escape.md", content="탈출"),),
            message="위험",
        )
        with pytest.raises(ValueError, match="탈출"):
            commit_okf_bundle(bad_req, gw)

    def test_gateway_commit_bundle_호출됨(self) -> None:
        gw = FakeGitGateway()
        commit_okf_bundle(self._make_req(), gw)
        assert len(gw._commits.get("cs_ops", [])) == 1  # pyright: ignore[reportPrivateUsage]

    def test_빈_파일_리스트_거부(self) -> None:
        """파일 없는 커밋 요청은 ValueError."""
        gw = FakeGitGateway()
        req = BuilderCommitRequest(
            agent_id="cs_ops",
            owner="cs_lead",
            files=(),
            message="빈 커밋",
        )
        with pytest.raises(ValueError, match="파일"):
            commit_okf_bundle(req, gw)


# ═══════════════════════════════════════════════════════════════════════════
# 슬라이스 3 — FakeGitGateway.extract_snapshot
# ═══════════════════════════════════════════════════════════════════════════


class TestFakeGitGatewayExtractSnapshot:
    def _commit_files(
        self,
        gw: FakeGitGateway,
        files: tuple[OkfFile, ...],
        agent_id: str = "cs_ops",
    ) -> str:
        req = CommitRequest(
            agent_id=agent_id,
            files=files,
            author="cs_lead",
            message="스냅샷 테스트",
        )
        return gw.commit_bundle(req).sha

    def test_extract_파일_실제_생성(self, tmp_path: Path) -> None:
        gw = FakeGitGateway()
        sha = self._commit_files(
            gw,
            (OkfFile(path="policy.md", content="# 환불\n내용"),),
        )
        result = gw.extract_snapshot(sha, "cs_ops", tmp_path)
        assert result == tmp_path
        assert (tmp_path / "policy.md").exists()
        assert "환불" in (tmp_path / "policy.md").read_text(encoding="utf-8")

    def test_extract_하위디렉터리_생성(self, tmp_path: Path) -> None:
        gw = FakeGitGateway()
        sha = self._commit_files(
            gw,
            (OkfFile(path="sub/detail.md", content="세부"),),
        )
        gw.extract_snapshot(sha, "cs_ops", tmp_path)
        assert (tmp_path / "sub" / "detail.md").exists()

    def test_extract_다수_파일(self, tmp_path: Path) -> None:
        gw = FakeGitGateway()
        sha = self._commit_files(
            gw,
            (
                OkfFile(path="a.md", content="A"),
                OkfFile(path="b.md", content="B"),
            ),
        )
        gw.extract_snapshot(sha, "cs_ops", tmp_path)
        assert (tmp_path / "a.md").read_text() == "A"
        assert (tmp_path / "b.md").read_text() == "B"

    def test_알수없는_SHA_ValueError(self, tmp_path: Path) -> None:
        gw = FakeGitGateway()
        with pytest.raises(ValueError, match="알 수 없는 SHA"):
            gw.extract_snapshot("deadbeef" * 5, "cs_ops", tmp_path)

    def test_dest_경로_반환(self, tmp_path: Path) -> None:
        gw = FakeGitGateway()
        sha = self._commit_files(
            gw,
            (OkfFile(path="x.md", content="x"),),
        )
        returned = gw.extract_snapshot(sha, "cs_ops", tmp_path)
        assert returned == tmp_path


# ═══════════════════════════════════════════════════════════════════════════
# removed_paths — OKF 개념 삭제 커밋 (ADR 0032 OQ-3)
# ═══════════════════════════════════════════════════════════════════════════


class TestFakeGitGatewayRemovedPaths:
    """ADR 0032 OQ-3 — `CommitRequest.removed_paths`로 working tree에서 파일 제거.

    삭제 커밋: working tree에서 removed_paths를 빼고 files를 적용한 새 스냅샷을 만든다.
    extract_snapshot으로 삭제 반영(빠진 파일)·다른 파일 보존·없는 path 무시(idempotent)를 단언.
    """

    def _commit(
        self,
        gw: FakeGitGateway,
        files: tuple[OkfFile, ...] = (),
        removed_paths: tuple[str, ...] = (),
        agent_id: str = "cs_ops",
    ) -> str:
        req = CommitRequest(
            agent_id=agent_id,
            files=files,
            author="cs_lead",
            message="삭제 테스트",
            removed_paths=removed_paths,
        )
        return gw.commit_bundle(req).sha

    def test_removed_paths_기본값_빈튜플(self) -> None:
        """removed_paths 미지정 시 기본 빈 튜플(하위호환 — 기존 커밋 무영향)."""
        req = CommitRequest(
            agent_id="cs_ops",
            files=(OkfFile(path="a.md", content="A"),),
            author="cs_lead",
            message="기본",
        )
        assert req.removed_paths == ()

    def test_삭제된_파일은_스냅샷에서_빠진다(self, tmp_path: Path) -> None:
        gw = FakeGitGateway()
        self._commit(
            gw,
            files=(OkfFile(path="a.md", content="A"), OkfFile(path="b.md", content="B")),
        )
        sha = self._commit(gw, removed_paths=("a.md",))
        gw.extract_snapshot(sha, "cs_ops", tmp_path)
        assert not (tmp_path / "a.md").exists()

    def test_다른_파일은_삭제_커밋_후에도_보존된다(self, tmp_path: Path) -> None:
        gw = FakeGitGateway()
        self._commit(
            gw,
            files=(OkfFile(path="a.md", content="A"), OkfFile(path="b.md", content="B")),
        )
        sha = self._commit(gw, removed_paths=("a.md",))
        gw.extract_snapshot(sha, "cs_ops", tmp_path)
        assert (tmp_path / "b.md").read_text(encoding="utf-8") == "B"

    def test_없는_path_삭제는_무시_idempotent(self, tmp_path: Path) -> None:
        """removed_paths에 있는데 working tree에 없는 path는 무시(idempotent·에러 없음)."""
        gw = FakeGitGateway()
        self._commit(gw, files=(OkfFile(path="a.md", content="A"),))
        sha = self._commit(gw, removed_paths=("nonexistent.md",))
        gw.extract_snapshot(sha, "cs_ops", tmp_path)
        assert (tmp_path / "a.md").read_text(encoding="utf-8") == "A"

    def test_files와_removed_paths_동시_적용(self, tmp_path: Path) -> None:
        """삭제+추가 한 커밋 — removed_paths 제거 후 files 적용."""
        gw = FakeGitGateway()
        self._commit(gw, files=(OkfFile(path="a.md", content="A"),))
        sha = self._commit(
            gw,
            files=(OkfFile(path="b.md", content="B"),),
            removed_paths=("a.md",),
        )
        gw.extract_snapshot(sha, "cs_ops", tmp_path)
        assert not (tmp_path / "a.md").exists()
        assert (tmp_path / "b.md").read_text(encoding="utf-8") == "B"

    def test_마지막_파일_삭제하면_빈_스냅샷(self, tmp_path: Path) -> None:
        gw = FakeGitGateway()
        self._commit(gw, files=(OkfFile(path="only.md", content="X"),))
        sha = self._commit(gw, removed_paths=("only.md",))
        gw.extract_snapshot(sha, "cs_ops", tmp_path)
        assert not (tmp_path / "only.md").exists()
        assert list(tmp_path.glob("*.md")) == []

    def test_removed_paths_경로탈출_거부(self) -> None:
        """removed_paths도 files와 같은 traversal 방어(절대경로·`..`·빈 값 거부)."""
        gw = FakeGitGateway()
        for bad in ("/etc/passwd", "../outside.md", "sub/../../evil.md", ""):
            with pytest.raises(ValueError):
                self._commit(gw, removed_paths=(bad,))


# ═══════════════════════════════════════════════════════════════════════════
# 슬라이스 3 — ClaudeCodeRuntime 커밋 스냅샷 모드
# ═══════════════════════════════════════════════════════════════════════════


class TestClaudeCodeRuntimeSnapshot:
    """GitGateway 주입 시 커밋 스냅샷 cwd + Answer.snapshot_sha 검증."""

    def _make_gw_with_commit(self) -> tuple[FakeGitGateway, str]:
        gw = FakeGitGateway()
        req = CommitRequest(
            agent_id="cs_ops",
            files=(OkfFile(path="policy.md", content="# 환불\n"),),
            author="cs_lead",
            message="정책 갱신",
        )
        result = gw.commit_bundle(req)
        return gw, result.sha

    def test_스냅샷_모드_cwd가_추출경로로_전달된다(self) -> None:
        gw, _ = self._make_gw_with_commit()
        runner = _FakeRunner("스냅샷 답")
        runtime = ClaudeCodeRuntime(runner=runner, git_gateway=gw)

        runtime.answer("환불 금액?", _card())

        assert runner.cwd_passed is True
        assert runner.last_cwd is not None

    def test_스냅샷_모드_Answer_snapshot_sha_설정(self) -> None:
        gw, sha = self._make_gw_with_commit()
        runner = _FakeRunner("스냅샷 답")
        runtime = ClaudeCodeRuntime(runner=runner, git_gateway=gw)

        ans = runtime.answer("환불 금액?", _card())

        assert ans.snapshot_sha == sha

    def test_스냅샷_모드_답_텍스트_보존(self) -> None:
        gw, _ = self._make_gw_with_commit()
        runner = _FakeRunner("스냅샷에서 나온 답")
        runtime = ClaudeCodeRuntime(runner=runner, git_gateway=gw)

        ans = runtime.answer("질문?", _card())

        assert ans.text == "스냅샷에서 나온 답"

    def test_하위호환_gateway_없으면_snapshot_sha_None(self, tmp_path: Path) -> None:
        bundle = tmp_path / "cs_ops"
        bundle.mkdir()
        (bundle / "index.md").write_text("# 번들\n")

        runner = _FakeRunner("워킹트리 답")
        runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path)

        ans = runtime.answer("질문?", _card())

        assert ans.snapshot_sha is None
        assert ans.text == "워킹트리 답"

    def test_하위호환_cwd가_번들경로로_전달된다(self, tmp_path: Path) -> None:
        bundle = tmp_path / "cs_ops"
        bundle.mkdir()
        (bundle / "index.md").write_text("# 번들\n")

        runner = _FakeRunner("답")
        runtime = ClaudeCodeRuntime(runner=runner, okf_root=tmp_path)

        runtime.answer("질문?", _card())

        assert runner.cwd_passed is True
        assert runner.last_cwd == str(bundle)

    def test_스냅샷_모드_커밋_없으면_폴백(self, tmp_path: Path) -> None:
        """커밋 없는 gateway 주입 시 head_sha ValueError → 폴백(snapshot_sha=None)."""
        gw = FakeGitGateway()  # 커밋 0개
        runner = _FakeRunner("폴백 답")
        runtime = ClaudeCodeRuntime(runner=runner, git_gateway=gw)

        ans = runtime.answer("질문?", _card())

        assert ans.snapshot_sha is None
        assert ans.text == "폴백 답"  # n1: 폴백 시 답 텍스트 보존


# ── m2: 예외 fallback 분기 snapshot_sha 일관성 ────────────────────────────────


class _ErrorRunner:
    """extract_snapshot 후 runner 자체가 예외를 던지는 Fake."""

    def __call__(self, prompt: str, **kwargs: object) -> str:
        raise RuntimeError("runner 내부 오류")


class _ErrorExtractGateway(FakeGitGateway):
    """extract_snapshot이 예외를 던지는 Fake — except Exception 경로 유도."""

    def extract_snapshot(self, sha: str, agent_id: str, dest: "Path") -> "Path":
        raise RuntimeError("extract 실패")


class TestClaudeCodeRuntimeSnapshotExceptionFallback:
    """m2: 스냅샷 모드 except Exception fallback도 snapshot_sha=sha를 싣는다."""

    def _make_gw_with_commit(self) -> tuple[FakeGitGateway, str]:
        gw = FakeGitGateway()
        req = CommitRequest(
            agent_id="cs_ops",
            files=(OkfFile(path="policy.md", content="# 환불\n"),),
            author="cs_lead",
            message="정책 갱신",
        )
        result = gw.commit_bundle(req)
        return gw, result.sha

    def test_extract_예외_fallback_snapshot_sha_실린다(self) -> None:
        """extract_snapshot에서 RuntimeError 발생 시 fallback Answer에도 snapshot_sha가 있다."""
        # FakeGitGateway에 extract 오류 주입
        bad_gw = _ErrorExtractGateway()
        # head_sha는 정상 반환되어야 하므로 gw에서 sha를 미리 얻고 bad_gw에 커밋 삽입
        bad_gw.commit_bundle(
            CommitRequest(
                agent_id="cs_ops",
                files=(OkfFile(path="policy.md", content="# 환불\n"),),
                author="cs_lead",
                message="정책 갱신",
            )
        )
        real_sha = bad_gw.head_sha("cs_ops")

        runner = _FakeRunner("이쪽 안 불림")
        runtime = ClaudeCodeRuntime(runner=runner, git_gateway=bad_gw)

        ans = runtime.answer("환불?", _card())

        assert ans.snapshot_sha == real_sha

    def test_runner_예외_fallback_snapshot_sha_실린다(self) -> None:
        """runner 자체가 예외를 던질 때 fallback Answer에도 snapshot_sha가 있다."""
        gw = FakeGitGateway()
        gw.commit_bundle(
            CommitRequest(
                agent_id="cs_ops",
                files=(OkfFile(path="policy.md", content="# 환불\n"),),
                author="cs_lead",
                message="정책 갱신",
            )
        )
        sha = gw.head_sha("cs_ops")

        runtime = ClaudeCodeRuntime(runner=_ErrorRunner(), git_gateway=gw)

        ans = runtime.answer("환불?", _card())

        assert ans.snapshot_sha == sha
