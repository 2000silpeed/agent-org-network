"""T8.1 (a)(b) — SubprocessGitGateway 실 git CLI 어댑터 tmp repo 통합 테스트.

이 환경에 git 2.52가 있어 *게이트 내*(통합 테스트)로 들인다. 단 실 SHA 값은 시각/환경
의존이라 **행위 단언만** 한다(SHA 값 단언 금지) — 커밋이 생겼나·파일이 들어갔나·author가
박혔나·서브트리만 풀렸나. tmp_path·행위 단언이라 환경 독립이다.

git이 없는 CI/타 환경에선 모듈 전체를 skip한다.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_org_network.git_gateway import (
    CommitRequest,
    CommitResult,
    OkfFile,
    SubprocessGitGateway,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="실 git 바이너리가 없어 통합 테스트를 건너뜁니다.",
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _git(repo: Path, *args: str) -> str:
    """테스트 검증용 git 호출(어댑터 밖 — 어댑터 결과를 독립 확인)."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """git init한 빈 repo 작업 트리 루트(= okf_root). committer는 어댑터가 -c로 처리."""
    subprocess.run(
        ["git", "-C", str(tmp_path), "init"],
        capture_output=True,
        text=True,
        check=True,
    )
    return tmp_path


@pytest.fixture
def gateway(repo: Path) -> SubprocessGitGateway:
    return SubprocessGitGateway(okf_root=repo)


def _req(
    agent_id: str = "cs_ops",
    files: tuple[OkfFile, ...] | None = None,
    author: str = "cs_lead",
    message: str = "초기 커밋",
) -> CommitRequest:
    if files is None:
        files = (OkfFile(path="policy.md", content="# 환불\n금액 안내"),)
    return CommitRequest(agent_id=agent_id, files=files, author=author, message=message)


# ═══════════════════════════════════════════════════════════════════════════
# (a) commit_bundle
# ═══════════════════════════════════════════════════════════════════════════


class TestCommitBundle:
    def test_커밋_성립_CommitResult_반환(self, gateway: SubprocessGitGateway) -> None:
        result = gateway.commit_bundle(_req())
        assert isinstance(result, CommitResult)
        assert result.agent_id == "cs_ops"

    def test_반환_sha가_40hex(self, gateway: SubprocessGitGateway) -> None:
        result = gateway.commit_bundle(_req())
        assert _SHA_RE.match(result.sha), f"40-hex SHA가 아님: {result.sha!r}"

    def test_반환_sha가_rev_parse_HEAD와_일치(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        result = gateway.commit_bundle(_req())
        assert result.sha == _git(repo, "rev-parse", "HEAD")

    def test_파일이_번들_경로에_실제_생성된다(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        gateway.commit_bundle(_req(agent_id="cs_ops", files=(OkfFile("policy.md", "본문"),)))
        written = repo / "cs_ops" / "policy.md"
        assert written.exists()
        assert written.read_text(encoding="utf-8") == "본문"

    def test_하위디렉터리_파일_생성된다(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        gateway.commit_bundle(_req(files=(OkfFile("sub/detail.md", "세부"),)))
        assert (repo / "cs_ops" / "sub" / "detail.md").exists()

    def test_커밋이_정확히_1개_생긴다(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        gateway.commit_bundle(_req())
        log = _git(repo, "log", "--oneline")
        assert len(log.splitlines()) == 1

    def test_author에_owner가_박힌다(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        gateway.commit_bundle(_req(author="cs_lead"))
        assert _git(repo, "log", "-1", "--format=%an") == "cs_lead"

    def test_author_email에_owner가_박힌다(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        gateway.commit_bundle(_req(author="cs_lead"))
        assert "cs_lead" in _git(repo, "log", "-1", "--format=%ae")

    def test_committer는_빌더봇_고정(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        """committer는 환경 의존 아닌 빌더 봇 고정(-c 처리 — ADR 0018 결정 5)."""
        gateway.commit_bundle(_req(author="cs_lead"))
        assert _git(repo, "log", "-1", "--format=%cn") == "agent-org-builder"

    def test_commit_message_보존(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        gateway.commit_bundle(_req(message="환불 정책 갱신"))
        assert _git(repo, "log", "-1", "--format=%s") == "환불 정책 갱신"

    def test_두번_커밋하면_커밋_2개(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        gateway.commit_bundle(_req(files=(OkfFile("policy.md", "v1"),), message="1차"))
        gateway.commit_bundle(_req(files=(OkfFile("policy.md", "v2"),), message="2차"))
        log = _git(repo, "log", "--oneline")
        assert len(log.splitlines()) == 2


class TestCommitBundle경로탈출거부:
    def _commit_bad(self, gateway: SubprocessGitGateway, path: str) -> None:
        gateway.commit_bundle(_req(files=(OkfFile(path=path, content="위험"),)))

    def test_빈_경로_거부(self, gateway: SubprocessGitGateway) -> None:
        with pytest.raises(ValueError, match="비어"):
            self._commit_bad(gateway, "")

    def test_공백만_경로_거부(self, gateway: SubprocessGitGateway) -> None:
        with pytest.raises(ValueError, match="비어"):
            self._commit_bad(gateway, "   ")

    def test_절대경로_거부(self, gateway: SubprocessGitGateway) -> None:
        with pytest.raises(ValueError, match="절대 경로"):
            self._commit_bad(gateway, "/etc/passwd")

    def test_상위_탈출_거부(self, gateway: SubprocessGitGateway) -> None:
        with pytest.raises(ValueError, match="탈출"):
            self._commit_bad(gateway, "../outside.md")

    def test_중간_상위_탈출_거부(self, gateway: SubprocessGitGateway) -> None:
        with pytest.raises(ValueError, match="탈출"):
            self._commit_bad(gateway, "sub/../../outside.md")

    def test_탈출_거부시_파일_안써지고_커밋_안생긴다(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        with pytest.raises(ValueError):
            self._commit_bad(gateway, "../escape.md")
        # 번들 밖 파일이 안 써졌나
        assert not (repo.parent / "escape.md").exists()
        # 커밋이 0개(빈 repo HEAD 없음)
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert head.returncode != 0


class TestCommitBundleRemovedPaths:
    """ADR 0032 OQ-3 — removed_paths로 실 git에서 파일 제거(`git rm`) 통합 단언."""

    def _req_rm(
        self,
        files: tuple[OkfFile, ...] = (),
        removed_paths: tuple[str, ...] = (),
        message: str = "삭제 커밋",
    ) -> CommitRequest:
        return CommitRequest(
            agent_id="cs_ops",
            files=files,
            author="cs_lead",
            message=message,
            removed_paths=removed_paths,
        )

    def test_삭제된_파일이_working_tree에서_사라진다(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        gateway.commit_bundle(
            self._req_rm(
                files=(OkfFile("a.md", "A"), OkfFile("b.md", "B")), message="초기"
            )
        )
        gateway.commit_bundle(self._req_rm(removed_paths=("a.md",), message="a 삭제"))
        assert not (repo / "cs_ops" / "a.md").exists()
        assert (repo / "cs_ops" / "b.md").read_text(encoding="utf-8") == "B"

    def test_삭제_전용_커밋이_생긴다(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        gateway.commit_bundle(self._req_rm(files=(OkfFile("a.md", "A"),), message="초기"))
        result = gateway.commit_bundle(
            self._req_rm(removed_paths=("a.md",), message="a 삭제")
        )
        assert _SHA_RE.match(result.sha)
        log = _git(repo, "log", "--oneline")
        assert len(log.splitlines()) == 2

    def test_없는_path_삭제는_무시되고_커밋_성립(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        """working tree에 없는 path 삭제 + 새 파일 추가 → 에러 없이 커밋(idempotent)."""
        gateway.commit_bundle(self._req_rm(files=(OkfFile("a.md", "A"),), message="초기"))
        result = gateway.commit_bundle(
            self._req_rm(
                files=(OkfFile("b.md", "B"),),
                removed_paths=("nonexistent.md",),
                message="없는 것 삭제 + b 추가",
            )
        )
        assert _SHA_RE.match(result.sha)
        assert (repo / "cs_ops" / "b.md").exists()
        assert (repo / "cs_ops" / "a.md").exists()

    def test_removed_paths_경로탈출_거부(self, gateway: SubprocessGitGateway) -> None:
        for bad in ("/etc/passwd", "../outside.md", "sub/../../evil.md", ""):
            with pytest.raises(ValueError):
                gateway.commit_bundle(self._req_rm(removed_paths=(bad,)))


class TestCommitBundleAgentId탈출거부:
    """B1: agent_id 경로 탈출 차단 — okf_root 밖에 실 파일이 안 써진다(실 파일시스템 확인)."""

    def _commit_bad_agent(self, gateway: SubprocessGitGateway, agent_id: str) -> None:
        gateway.commit_bundle(_req(agent_id=agent_id))

    def test_상위_탈출_agent_id_거부(self, gateway: SubprocessGitGateway) -> None:
        # `../evil`은 경로구분자(`/`)를 품으므로 단일 디렉터리명 규약 위반으로 먼저 거부된다.
        with pytest.raises(ValueError, match="구분자"):
            self._commit_bad_agent(gateway, "../evil")

    def test_단일_점점_agent_id_거부(self, gateway: SubprocessGitGateway) -> None:
        with pytest.raises(ValueError, match="탈출"):
            self._commit_bad_agent(gateway, "..")

    def test_절대경로_agent_id_거부(self, gateway: SubprocessGitGateway) -> None:
        # `/etc`도 경로구분자를 품으므로 구분자 위반으로 거부(절대경로 검사 도달 전).
        with pytest.raises(ValueError, match="구분자"):
            self._commit_bad_agent(gateway, "/etc")

    def test_경로구분자_agent_id_거부(self, gateway: SubprocessGitGateway) -> None:
        with pytest.raises(ValueError, match="구분자"):
            self._commit_bad_agent(gateway, "a/b")

    def test_빈_agent_id_거부(self, gateway: SubprocessGitGateway) -> None:
        with pytest.raises(ValueError, match="비어"):
            self._commit_bad_agent(gateway, "")

    def test_공백만_agent_id_거부(self, gateway: SubprocessGitGateway) -> None:
        with pytest.raises(ValueError, match="비어"):
            self._commit_bad_agent(gateway, "   ")

    def test_탈출_agent_id_거부시_okf_root_밖에_파일_안써진다(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        with pytest.raises(ValueError):
            self._commit_bad_agent(gateway, "../evil")
        # okf_root 밖(상위)에 evil 디렉터리·파일이 안 생긴다
        assert not (repo.parent / "evil").exists()
        # 커밋도 0개(빈 repo HEAD 없음)
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert head.returncode != 0


# ═══════════════════════════════════════════════════════════════════════════
# (b) head_sha
# ═══════════════════════════════════════════════════════════════════════════


class TestHeadSha:
    def test_커밋후_rev_parse_HEAD와_일치(
        self, gateway: SubprocessGitGateway, repo: Path
    ) -> None:
        gateway.commit_bundle(_req())
        assert gateway.head_sha("cs_ops") == _git(repo, "rev-parse", "HEAD")

    def test_빈_repo_ValueError(self, gateway: SubprocessGitGateway) -> None:
        with pytest.raises(ValueError, match="커밋 없음"):
            gateway.head_sha("cs_ops")

    def test_모노repo_HEAD는_전역_agent_id_무관(
        self, gateway: SubprocessGitGateway
    ) -> None:
        """모노repo라 HEAD는 전역 — 다른 agent_id로 물어도 같은 repo HEAD."""
        gateway.commit_bundle(_req(agent_id="cs_ops"))
        assert gateway.head_sha("cs_ops") == gateway.head_sha("legal_ops")


# ═══════════════════════════════════════════════════════════════════════════
# (b) extract_snapshot
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractSnapshot:
    def test_추출본_그_SHA_시점_내용_재현(
        self, gateway: SubprocessGitGateway, tmp_path: Path
    ) -> None:
        """두 번 커밋(같은 파일 다른 내용) 후, 첫 sha 추출본이 *첫 시점* 내용인지.

        working tree 직독이 아니라 그 SHA 트리 재현임을 확인(ADR 0018 결정 4).
        """
        first = gateway.commit_bundle(
            _req(files=(OkfFile("policy.md", "첫 시점 내용"),), message="1차")
        )
        gateway.commit_bundle(
            _req(files=(OkfFile("policy.md", "둘째 시점 내용"),), message="2차")
        )

        dest = tmp_path / "snapshot"
        gateway.extract_snapshot(first.sha, "cs_ops", dest)

        extracted = dest / "policy.md"
        assert extracted.exists()
        # working tree는 "둘째 시점 내용"이지만 추출본은 첫 SHA 트리여야
        assert extracted.read_text(encoding="utf-8") == "첫 시점 내용"

    def test_서브트리만_dest_직하에_풀린다(
        self, gateway: SubprocessGitGateway, tmp_path: Path
    ) -> None:
        """번들 서브트리만 dest 직하에 — 다른 agent_id 파일은 안 섞인다."""
        # 두 agent_id를 같은 repo에 커밋
        gateway.commit_bundle(
            _req(agent_id="cs_ops", files=(OkfFile("cs.md", "cs 내용"),), message="cs")
        )
        legal = gateway.commit_bundle(
            _req(
                agent_id="legal_ops",
                files=(OkfFile("legal.md", "legal 내용"),),
                message="legal",
            )
        )

        dest = tmp_path / "snapshot"
        gateway.extract_snapshot(legal.sha, "legal_ops", dest)

        # legal 번들 파일은 dest 직하에 있고
        assert (dest / "legal.md").exists()
        assert (dest / "legal.md").read_text(encoding="utf-8") == "legal 내용"
        # cs 번들 파일·cs_ops 디렉터리는 안 섞인다
        assert not (dest / "cs.md").exists()
        assert not (dest / "cs_ops").exists()

    def test_하위디렉터리_보존된다(
        self, gateway: SubprocessGitGateway, tmp_path: Path
    ) -> None:
        result = gateway.commit_bundle(
            _req(files=(OkfFile("sub/detail.md", "세부"),), message="중첩")
        )
        dest = tmp_path / "snapshot"
        gateway.extract_snapshot(result.sha, "cs_ops", dest)
        assert (dest / "sub" / "detail.md").read_text(encoding="utf-8") == "세부"

    def test_dest_경로_반환(
        self, gateway: SubprocessGitGateway, tmp_path: Path
    ) -> None:
        result = gateway.commit_bundle(_req())
        dest = tmp_path / "snapshot"
        returned = gateway.extract_snapshot(result.sha, "cs_ops", dest)
        assert returned == dest

    def test_알수없는_sha_ValueError(
        self, gateway: SubprocessGitGateway, tmp_path: Path
    ) -> None:
        gateway.commit_bundle(_req())
        dest = tmp_path / "snapshot"
        with pytest.raises(ValueError):
            gateway.extract_snapshot("deadbeef" * 5, "cs_ops", dest)

    def test_알수없는_agent_id_ValueError(
        self, gateway: SubprocessGitGateway, tmp_path: Path
    ) -> None:
        result = gateway.commit_bundle(_req(agent_id="cs_ops"))
        dest = tmp_path / "snapshot"
        with pytest.raises(ValueError):
            gateway.extract_snapshot(result.sha, "no_such_bundle", dest)

    def test_임시_tar_파일_안남는다(
        self, gateway: SubprocessGitGateway, tmp_path: Path
    ) -> None:
        result = gateway.commit_bundle(_req())
        dest = tmp_path / "snapshot"
        gateway.extract_snapshot(result.sha, "cs_ops", dest)
        assert not (dest / ".okf-archive.tar").exists()


class TestExtractSnapshotShaGuard:
    """M1: extract_snapshot sha 옵션 주입 경계 — 빈/`-`시작 sha 거부(tree-ish 주입 차단)."""

    def test_빈_sha_거부(
        self, gateway: SubprocessGitGateway, tmp_path: Path
    ) -> None:
        gateway.commit_bundle(_req())
        with pytest.raises(ValueError):
            gateway.extract_snapshot("", "cs_ops", tmp_path / "snapshot")

    def test_대시_시작_sha_거부(
        self, gateway: SubprocessGitGateway, tmp_path: Path
    ) -> None:
        gateway.commit_bundle(_req())
        with pytest.raises(ValueError):
            gateway.extract_snapshot("-foo", "cs_ops", tmp_path / "snapshot")


class TestExtractSnapshotAgentIdGuard:
    """B1: extract_snapshot agent_id 탈출 차단 — archive *전에* 거부(tree-ish 주입 차단)."""

    def test_상위_탈출_agent_id_거부(
        self, gateway: SubprocessGitGateway, tmp_path: Path
    ) -> None:
        result = gateway.commit_bundle(_req())
        with pytest.raises(ValueError, match="구분자"):
            gateway.extract_snapshot(result.sha, "../evil", tmp_path / "snapshot")

    def test_절대경로_agent_id_거부(
        self, gateway: SubprocessGitGateway, tmp_path: Path
    ) -> None:
        result = gateway.commit_bundle(_req())
        with pytest.raises(ValueError, match="구분자"):
            gateway.extract_snapshot(result.sha, "/etc", tmp_path / "snapshot")

    def test_경로구분자_agent_id_거부(
        self, gateway: SubprocessGitGateway, tmp_path: Path
    ) -> None:
        result = gateway.commit_bundle(_req())
        with pytest.raises(ValueError, match="구분자"):
            gateway.extract_snapshot(result.sha, "a/b", tmp_path / "snapshot")


class TestExtractSnapshotTempTar정리:
    """m1·m2: 임시 tar는 dest 바깥에 쓰이고, 추출 성공·실패 무관 항상 정리된다."""

    def test_dest에_tar_확장자_잔존물_없다(
        self, gateway: SubprocessGitGateway, tmp_path: Path
    ) -> None:
        """m2: 임시 tar가 dest(답 cwd) 직하를 오염하지 않는다 — 번들 파일만 남는다."""
        gateway.commit_bundle(_req(files=(OkfFile("policy.md", "본문"),)))
        result = gateway.head_sha("cs_ops")
        dest = tmp_path / "snapshot"
        gateway.extract_snapshot(result, "cs_ops", dest)
        leftovers = [p.name for p in dest.iterdir() if p.suffix == ".tar"]
        assert leftovers == []
        # 번들 파일만 dest 직하에
        assert {p.name for p in dest.iterdir()} == {"policy.md"}

    def test_추출_예외시에도_임시_tar_정리된다(
        self, gateway: SubprocessGitGateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """m1: tarfile.extractall이 예외여도 임시 tar가 남지 않는다(try/finally 보장)."""
        import tarfile as _tarfile

        gateway.commit_bundle(_req())
        sha = gateway.head_sha("cs_ops")
        dest = tmp_path / "snapshot"

        class _BoomTar:
            def __enter__(self) -> "_BoomTar":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def extractall(self, *args: object, **kwargs: object) -> None:
                raise RuntimeError("손상 tar")

        def _fake_open(*args: object, **kwargs: object) -> _BoomTar:
            return _BoomTar()

        monkeypatch.setattr(_tarfile, "open", _fake_open)

        with pytest.raises(RuntimeError, match="손상 tar"):
            gateway.extract_snapshot(sha, "cs_ops", dest)

        # 임시 tar가 어디에도 잔존하지 않는다(dest 직하·시스템 temp 정리)
        if dest.exists():
            assert [p for p in dest.iterdir() if p.suffix == ".tar"] == []
