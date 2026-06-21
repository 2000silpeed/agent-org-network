"""OKF 번들 git 저장·빌더 커밋·커밋 스냅샷 실행의 도메인 shape (T7.2, ADR 0018).

**이 모듈은 shape(미구현 통과 stub)다 — tdd-engineer가 red→green으로 채운다.**

ADR 0018:
- 결정 1: 빌더가 커밋하는 본체 = **OKF 번들 마크다운**(카드 YAML 아님 — 분리 유지).
- 결정 3: 커밋 메커니즘을 `GitGateway` 포트로 추상 — 실 git은 `SubprocessGitGateway`
  (subprocess·게이트 밖 수동 시연), 단위 테스트는 `FakeGitGateway`(in-memory·결정론) 주입.
  새 의존성 0(GitPython 등 안 씀 — subprocess만, `ClaudeRunner`·`AgentRuntime`과 같은 결).
- 결정 4: 커밋 스냅샷 실행 = `git archive <sha>` 추출 cwd(working tree 직독 아님 —
  "이 답은 이 커밋 기준" 재현). 답엔 `Answer.snapshot_sha`(runtime.py)로 SHA 감사.
- 결정 5: 커밋 author = owner 신원(세션 신원, T7.1 SSO 전 — `_session_identity`). 편집
  스코프는 기존 Owner 스코프 재사용(세션 신원 ≠ card.owner → 403, web 경계에서).

결정론 경계(ADR 0003·0018 결정 3): `FakeGitGateway`는 in-memory 커밋 로그·결정 SHA라
게이트에서 돈다. `SubprocessGitGateway`는 실 `git` 부작용이라 게이트 밖(수동 시연).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class OkfFile:
    """빌더가 커밋할 OKF 번들 파일 하나 — 번들 내 상대 경로 + 마크다운 본문(프론트매터 포함).

    `path`는 번들 디렉터리(`okf_root/{agent_id}/`) 기준 상대 경로(예: `refund-policy.md`).
    번들 밖(상위·절대)을 가리키면 안 된다(경로 탈출 방지는 구현이 강제 — 결정론 테스트 대상).
    `content`는 마크다운 + YAML 프론트매터 텍스트(OKF 형식, ADR 0013 — type 자유).
    """

    path: str
    content: str


@dataclass(frozen=True)
class CommitRequest:
    """빌더가 owner 대신 한 OKF 번들에 커밋하는 요청(ADR 0018 결정 1·5).

    한 owner의 한 번 편집 = 한 번들(`agent_id`)에 파일 N개 쓰기 + 커밋 1개(가장 작은 닫힌 루프).
    `author`는 커밋 author로 박힐 owner 신원(세션 신원 — ADR 0018 결정 5, T7.1 SSO 전).
    스코프 강제(세션 신원 ≠ card.owner → 403)는 *web 경계*에서 빌더 카드 검증과 같은 규칙으로
    이뤄지고(ADR 0016 재사용), 이 값 객체는 그 통과 후의 커밋 요청만 든다.
    """

    agent_id: str
    files: tuple[OkfFile, ...]
    author: str
    message: str


@dataclass(frozen=True)
class CommitResult:
    """커밋 1개의 결과 — 커밋 SHA(스냅샷 실행·감사 메타의 키, ADR 0018 결정 4·6)."""

    sha: str
    agent_id: str


class GitGateway(Protocol):
    """OKF 번들 git 저장·커밋·커밋 스냅샷 추출의 최소 포트(ADR 0018 결정 3).

    빌더가 쓰는 git 연산만 노출한다 — `add`·`commit`·`rev-parse`·`archive`에 대응.
    실 구현(`SubprocessGitGateway`)은 `git` CLI subprocess(부작용·게이트 밖), 단위 테스트는
    `FakeGitGateway`(in-memory·결정 SHA) 주입. 새 의존성 0(GitPython 안 씀).
    """

    def commit_bundle(self, req: CommitRequest) -> CommitResult:
        """OKF 번들에 파일들을 쓰고 owner author로 커밋 1개를 만든다 → 그 SHA.

        ADR 0018 결정 1·5: 빌더가 owner 대신 커밋(owner는 git 몰라도 됨). author=owner 신원.
        경로 탈출(번들 밖 쓰기)은 거부해야 한다(구현이 강제 — 결정론 테스트 대상).
        """
        ...

    def head_sha(self, agent_id: str) -> str:
        """그 번들 repo의 현재 HEAD 커밋 SHA(ADR 0018 결정 4 — MVP는 HEAD를 스냅샷).

        '중앙 최신 읽기'의 MVP = 로컬 repo HEAD(pull/webhook 캐시는 후속 슬라이스).
        """
        ...

    def extract_snapshot(self, sha: str, agent_id: str, dest: Path) -> Path:
        """그 커밋(`sha`)의 번들 트리를 읽기전용 `dest`로 추출해 그 경로를 돌려준다.

        ADR 0018 결정 4: `git archive <sha>` 추출본 = 그 SHA의 정확한 스냅샷(working tree
        직독 아님 — "이 답은 이 커밋 기준" 재현). 추출된 디렉터리가 `claude -p`의 cwd가 된다.
        여러 답이 다른 커밋을 동시에 읽어도 추출본은 독립이라 충돌 없다(ADR 0017 결정 3 실현).
        """
        ...


class FakeGitGateway:
    """결정론 in-memory `GitGateway` — 게이트(단위 테스트)에서 돈다(ADR 0018 결정 3).

    실 파일·실 git 없이 커밋 로그를 dict로 들고 결정 SHA(카운터 기반)를 낸다.
    SHA는 `{agent_id}:{총커밋수}` 문자열의 16진 해시로 *결정론*을 보장한다 —
    시각·랜덤을 쓰지 않으므로 같은 시퀀스엔 항상 같은 SHA가 나온다.
    """

    def __init__(self) -> None:
        # agent_id → 커밋 SHA 리스트(append-only 커밋 로그). 결정론 — 실 파일 IO 0.
        self._commits: dict[str, list[CommitResult]] = {}
        # sha → 그 커밋이 든 파일들(스냅샷 추출 결정론 재현용).
        self._trees: dict[str, tuple[OkfFile, ...]] = {}
        # agent_id → 커밋 요청 기록(author·message 검증용).
        self._requests: dict[str, list[CommitRequest]] = {}
        # 전역 커밋 카운터 — SHA 유일성 보장.
        self._counter: int = 0

    @staticmethod
    def _validate_paths(files: tuple[OkfFile, ...]) -> None:
        """번들 밖 경로 탈출을 거부한다(결정론 테스트 대상).

        `OkfFile.path`가 빈 문자열, 절대경로, 또는 `..` 구성요소를 포함하면 ValueError.
        """
        for f in files:
            p = f.path
            if not p or not p.strip():
                raise ValueError(f"OkfFile.path가 비어 있습니다: {p!r}")
            from pathlib import PurePosixPath

            parts = PurePosixPath(p).parts
            if parts and parts[0] == "/":
                raise ValueError(f"절대 경로는 허용되지 않습니다: {p!r}")
            if ".." in parts:
                raise ValueError(f"번들 밖 경로 탈출은 허용되지 않습니다: {p!r}")

    def _make_sha(self, agent_id: str) -> str:
        """agent_id + 전역 카운터로 결정 SHA를 만든다(비결정 금지 — 시각·랜덤 0)."""
        import hashlib

        self._counter += 1
        raw = f"{agent_id}:{self._counter}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def commit_bundle(self, req: CommitRequest) -> CommitResult:
        self._validate_paths(req.files)
        sha = self._make_sha(req.agent_id)
        result = CommitResult(sha=sha, agent_id=req.agent_id)
        self._commits.setdefault(req.agent_id, []).append(result)
        self._trees[sha] = req.files
        self._requests.setdefault(req.agent_id, []).append(req)
        return result

    def head_sha(self, agent_id: str) -> str:
        commits = self._commits.get(agent_id)
        if not commits:
            raise ValueError(f"커밋 없음 — agent_id: {agent_id!r}")
        return commits[-1].sha

    def extract_snapshot(self, sha: str, agent_id: str, dest: Path) -> Path:
        files = self._trees.get(sha)
        if files is None:
            raise ValueError(f"알 수 없는 SHA: {sha!r}")
        for f in files:
            target = dest / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8")
        return dest


@dataclass(frozen=True)
class SubprocessGitGateway:
    """실 `git` CLI subprocess `GitGateway` — **게이트 밖 수동 시연**(ADR 0018 결정 3).

    `okf_root`(= OKF 번들들을 담은 git repo 작업 트리 루트, ADR 0018 결정 2)를 대상으로
    `git add`·`git commit --author`·`git rev-parse HEAD`·`git archive <sha> | tar -x`를
    subprocess로 부른다(부작용·비결정 — `ClaudeCodeRuntime`의 `claude` subprocess와 같은 결).
    실 커밋·실 추출이라 단위 게이트에서 돌리지 않는다(수동 시연·eval).

    **현재는 shape stub(미구현)** — 실 subprocess 본문은 후속(mcp-runtime-engineer/수동).
    """

    okf_root: Path

    def commit_bundle(self, req: CommitRequest) -> CommitResult:
        raise NotImplementedError("실 git subprocess — 게이트 밖 수동 시연(T7.2 후속)")

    def head_sha(self, agent_id: str) -> str:
        raise NotImplementedError("실 git subprocess — 게이트 밖 수동 시연(T7.2 후속)")

    def extract_snapshot(self, sha: str, agent_id: str, dest: Path) -> Path:
        raise NotImplementedError("실 git subprocess — 게이트 밖 수동 시연(T7.2 후속)")


@dataclass(frozen=True)
class BuilderCommitRequest:
    """빌더 OKF 편집 면의 커밋 요청 입력(web 경계 → 서비스, ADR 0018 결정 1·5).

    web과 분리한 *순수 입력*(`BuilderValidateRequest`와 같은 결) — 핸들러가 세션 신원으로
    `author`를 채우고(path/body 아님 — ADR 0016 위조 차단), 스코프(세션 신원 ≠ card.owner →
    403)를 web에서 강제한 뒤 이 요청을 서비스에 넘긴다.
    """

    agent_id: str
    owner: str
    files: tuple[OkfFile, ...] = field(default_factory=tuple)
    message: str = ""


def commit_okf_bundle(
    req: BuilderCommitRequest,
    gateway: GitGateway,
) -> CommitResult:
    """빌더 OKF 편집을 owner author로 커밋하는 오케스트레이션(ADR 0018 결정 1·3·5).

    절차: ① 파일 존재 검증(빈 파일 리스트 거부) ② 파일 경로 검증(번들 밖 탈출 거부)
    ③ `CommitRequest`(author=req.owner) 구성 ④ `gateway.commit_bundle` 호출 → `CommitResult`.

    **카드는 건드리지 않는다**(ADR 0018 결정 1 — OKF 번들만 자동 커밋, 카드는 PR 유지).
    스코프 403은 *web 경계*에서(빌더 카드 검증과 같은 규칙) — 이 함수는 통과 후의 커밋만.
    """
    if not req.files:
        raise ValueError("커밋할 파일이 없습니다 — files가 비어 있습니다.")
    commit_req = CommitRequest(
        agent_id=req.agent_id,
        files=req.files,
        author=req.owner,
        message=req.message,
    )
    return gateway.commit_bundle(commit_req)
