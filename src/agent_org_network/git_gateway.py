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

import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    pass

Clock = Callable[[], datetime]


def validate_okf_paths(files: tuple[OkfFile, ...]) -> None:
    """번들 밖 경로 탈출을 거부하는 *공유* 검증(안전 경계 — 등록 무결성).

    `OkfFile.path`가 빈 문자열, 공백뿐, 절대경로, 또는 `..` 구성요소를 포함하면 ValueError.
    `FakeGitGateway`와 `SubprocessGitGateway`가 *같은 규칙*을 쓰도록 모듈 레벨로 뺐다 —
    안전 경계라 두 구현의 행동이 동일해야 한다(번들 밖 쓰기 차단의 단일 진실 원천).
    """
    for f in files:
        p = f.path
        if not p or not p.strip():
            raise ValueError(f"OkfFile.path가 비어 있습니다: {p!r}")
        parts = PurePosixPath(p).parts
        if parts and parts[0] == "/":
            raise ValueError(f"절대 경로는 허용되지 않습니다: {p!r}")
        if ".." in parts:
            raise ValueError(f"번들 밖 경로 탈출은 허용되지 않습니다: {p!r}")


def validate_agent_id(agent_id: str) -> None:
    """agent_id 경로 탈출을 거부하는 *공유* 검증(안전 경계 — 등록 무결성).

    `agent_id`는 `okf/{agent_id}/` 규약의 단일 디렉터리명이어야 한다. 비어 있거나 공백뿐,
    절대경로, `..` 구성요소, 또는 경로구분자(`/`·`\\`)를 포함하면 ValueError. 어댑터에서
    `okf_root / agent_id`(파일 쓰기)와 `{sha}:{agent_id}`(archive tree-ish)에 쓰이므로
    okf_root 밖 쓰기·엉뚱한 트리 지목을 원천 차단한다. `FakeGitGateway`와
    `SubprocessGitGateway`가 *같은 규칙*을 쓰도록 모듈 레벨로 둔다(계약 일치).
    """
    if not agent_id or not agent_id.strip():
        raise ValueError(f"agent_id가 비어 있습니다: {agent_id!r}")
    if "/" in agent_id or "\\" in agent_id:
        raise ValueError(f"agent_id에 경로구분자는 허용되지 않습니다: {agent_id!r}")
    parts = PurePosixPath(agent_id).parts
    if parts and parts[0] == "/":
        raise ValueError(f"절대 경로 agent_id는 허용되지 않습니다: {agent_id!r}")
    if ".." in parts:
        raise ValueError(f"번들 밖 경로 탈출 agent_id는 허용되지 않습니다: {agent_id!r}")


class ChangeEventListener(Protocol):
    """OKF 커밋 변경 이벤트를 받는 최소 포트(ADR 0019 결정 1).

    `StalenessPropagator`를 구체 타입으로 참조하면 순환 임포트 + 타입 주입 제약이 생기므로
    `commit_okf_bundle`은 이 Protocol에 의존한다 — duck typing으로 Fake 주입이 가능하다.
    """

    def on_okf_committed(self, event: Any) -> None: ...


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


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


@dataclass(frozen=True)
class OkfChangeEvent:
    """OKF 번들이 방금 커밋으로 바뀌었다는 변경 사건(ADR 0019 결정 1).

    "OKF 커밋 = 변경 이벤트 소스"(ADR 0018 결정 6)의 본체. `commit_okf_bundle`이 커밋
    성공 직후 1회 발화해 `StalenessPropagator`로만 흐른다 — `CommitResult`·web 응답
    (`{sha, agent_id}`)엔 끼어들지 않는다(노출 불변식·직렬화 계약 보존).

    필드:
      - `agent_id` — 어느 번들이 바뀌었나(영향 식별의 거친 매칭 키, 결정 2).
      - `new_sha` — 방금 만든 커밋 SHA(`CommitResult.sha`).
      - `parent_sha` — 커밋 *직전* HEAD(최초 커밋이면 None). 반드시 커밋 *전에*
        `head_sha`를 읽어 얻는다(커밋 후엔 새 SHA가 나오므로).
      - `committed_at` — 주입 clock(결정론).

    **죽은 필드(MVP)**: `changed_paths`(req.files의 path)·`parent_sha`는 이벤트에 *싣되*
    MVP 영향 식별엔 **쓰지 않는다**(결정 2 — agent_id 단위 거친 매칭). 미래 정밀화
    (파일 단위 교차·ordering)의 *자리*다. `sources`(자유 레이블) ↔ `changed_paths`
    (파일 경로) 교차는 타입 불일치 과소검출이라 MVP 매칭에서 기각(ADR 0019 결정 2 기각).
    """

    agent_id: str
    new_sha: str
    parent_sha: str | None
    changed_paths: tuple[str, ...]
    author: str
    committed_at: datetime


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
        """공유 검증 `validate_okf_paths`로 위임한다(중복 제거·계약 일치).

        `SubprocessGitGateway`와 *같은 규칙*을 쓰도록 모듈 함수를 부른다(안전 경계 단일화).
        """
        validate_okf_paths(files)

    def _make_sha(self, agent_id: str) -> str:
        """agent_id + 전역 카운터로 결정 SHA를 만든다(비결정 금지 — 시각·랜덤 0)."""
        import hashlib

        self._counter += 1
        raw = f"{agent_id}:{self._counter}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def commit_bundle(self, req: CommitRequest) -> CommitResult:
        validate_agent_id(req.agent_id)
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
        validate_agent_id(agent_id)
        files = self._trees.get(sha)
        if files is None:
            raise ValueError(f"알 수 없는 SHA: {sha!r}")
        for f in files:
            target = dest / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8")
        return dest


# 커밋 author 이메일 도메인(환경 의존 회피 — 실 메일주소 아님, owner 신원 식별만).
_OWNER_EMAIL_DOMAIN = "agent-org.local"
# committer 신원(ADR 0018 결정 5 — author=owner, committer는 빌더 봇 고정).
# git이 committer identity를 요구하므로 환경(전역 user.name/email)에 의존하지 않게 -c로 박는다.
_COMMITTER_NAME = "agent-org-builder"
_COMMITTER_EMAIL = f"agent-org-builder@{_OWNER_EMAIL_DOMAIN}"


@dataclass(frozen=True)
class SubprocessGitGateway:
    """실 `git` CLI subprocess `GitGateway` — T8.1 (a)(b) 실 어댑터(ADR 0018 결정 3).

    `okf_root`(= OKF 번들들을 담은 git repo 작업 트리 루트, ADR 0018 결정 2)를 대상으로
    `git add`·`git commit --author`·`git rev-parse HEAD`·`git archive <sha>`를 subprocess로
    부른다(부작용·비결정 — `ClaudeCodeRuntime`의 `claude` subprocess와 같은 결). 새 의존성 0
    (표준 라이브러리 subprocess·tarfile만 — GitPython 안 씀, ADR 0018 결정 3).

    검증 경계: 실 git 부작용이라 결정론 단위 게이트엔 못 들어가지만, 이 환경에 git이 있어
    tmp repo *통합 테스트*(행위 단언·SHA 값 비의존)로 게이트에 들인다(T8.1 (a)(b)).
    """

    okf_root: Path

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """`git -C {okf_root} {args}`를 부른다 — capture·text·check=False(returncode 직접 검사).

        명령 인젝션 회피: shell=False(인자 리스트)·`--` 구분자로 옵션/경로 경계를 못 박는다.
        """
        return subprocess.run(
            ["git", "-C", str(self.okf_root), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def commit_bundle(self, req: CommitRequest) -> CommitResult:
        # ① 경로 탈출 거부 — FakeGitGateway와 *같은 규칙*(공유 함수·안전 경계).
        #    파일을 쓰기 *전에* 검증해 번들 밖 쓰기를 원천 차단(등록 무결성).
        #    agent_id가 okf_root/{agent_id} 경로에 박히므로 agent_id 탈출도 쓰기 전 거부.
        validate_agent_id(req.agent_id)
        validate_okf_paths(req.files)

        # ② okf_root/{agent_id}/{file.path}에 각 파일 쓰기(부모 디렉터리 mkdir).
        bundle_dir = self.okf_root / req.agent_id
        written: list[str] = []
        for f in req.files:
            target = bundle_dir / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8")
            written.append(str(target))

        # ③ git add — 방금 쓴 파일들만(번들 밖 변경 끌어들이지 않게 -- 구분자·명시 경로).
        add = self._run_git("add", "--", *written)
        if add.returncode != 0:
            raise RuntimeError(f"git add 실패: {add.stderr.strip()}")

        # ④ git commit — author=owner 신원(ADR 0018 결정 5). committer identity는 환경에
        #    의존하지 않게 -c user.name/email로 빌더 봇 고정(환경 의존 회피).
        author_email = f"{req.author}@{_OWNER_EMAIL_DOMAIN}"
        commit = self._run_git(
            "-c",
            f"user.name={_COMMITTER_NAME}",
            "-c",
            f"user.email={_COMMITTER_EMAIL}",
            "commit",
            "-m",
            req.message,
            "--author",
            f"{req.author} <{author_email}>",
        )
        if commit.returncode != 0:
            raise RuntimeError(f"git commit 실패: {commit.stderr.strip()}")

        # ⑤ rev-parse HEAD → 방금 만든 커밋 SHA.
        sha = self._rev_parse_head()
        return CommitResult(sha=sha, agent_id=req.agent_id)

    def head_sha(self, agent_id: str) -> str:
        # 모노repo 판단(ADR 0018 결정 2): okf_root는 단일 git repo라 HEAD는 *전역*이다 —
        # agent_id별로 다르지 않다. MVP는 repo HEAD를 반환한다(빌더가 방금 커밋한 그 HEAD,
        # ADR 0018 결정 4). agent_id는 미래 owner별 repo(결정 2 후속 옵션)를 위해 시그니처에
        # 두되 지금은 미사용 — 모노repo에선 의미가 같다. 커밋 0개면 ValueError(Fake와 같은 계약).
        return self._rev_parse_head()

    def extract_snapshot(self, sha: str, agent_id: str, dest: Path) -> Path:
        # ADR 0018 결정 4: 그 커밋의 *번들 서브트리만* dest로 추출(working tree 직독 아님 —
        # "이 답은 이 커밋 기준" 재현). `git archive {sha}:{agent_id}`는 그 SHA 트리의
        # agent_id 서브트리를 tar로 낸다 → dest *직하*에 번들 파일들이 풀린다(다른 agent_id
        # 안 섞임). 파이프 대신 임시 tar 파일 후 tarfile로 풀기(결정적·에러 처리 쉬움).
        #
        # 안전 경계(B1·M1): tree-ish `{sha}:{agent_id}` 주입 차단. sha가 `-`로 시작하면
        # 옵션으로, agent_id가 탈출 구성이면 엉뚱한 트리로 해석될 수 있으므로 archive *전에*
        # 거부한다(인자 순서에 안 기댄다).
        if not sha or sha.startswith("-"):
            raise ValueError(f"유효하지 않은 sha입니다(빈 값·옵션 형식 거부): {sha!r}")
        validate_agent_id(agent_id)

        dest.mkdir(parents=True, exist_ok=True)
        # m2: 임시 tar는 dest(답 cwd) 바깥 시스템 temp에 둔다(결과 디렉터리 오염 방지).
        tmp_dir = tempfile.mkdtemp(prefix="okf-archive-")
        tar_path = Path(tmp_dir) / "archive.tar"
        try:
            archive = self._run_git(
                "archive",
                "--format=tar",
                f"--output={tar_path}",
                f"{sha}:{agent_id}",
            )
            if archive.returncode != 0:
                raise ValueError(
                    f"스냅샷 추출 실패 — 알 수 없는 sha/agent_id일 수 있습니다 "
                    f"(sha={sha!r}, agent_id={agent_id!r}): {archive.stderr.strip()}"
                )
            with tarfile.open(tar_path, "r") as tf:
                # filter="data": 절대경로·.. 엔트리 거부(안전 추출, Python 3.12+ 권장 기본).
                tf.extractall(dest, filter="data")
        finally:
            # m1: 추출 성공·실패 무관 임시 tar·temp 디렉터리 정리(잔존 방지).
            tar_path.unlink(missing_ok=True)
            Path(tmp_dir).rmdir()
        return dest

    def _rev_parse_head(self) -> str:
        """`git rev-parse HEAD` → SHA. 커밋 0개(빈 repo)면 ValueError(Fake와 같은 "커밋 없음")."""
        result = self._run_git("rev-parse", "HEAD")
        if result.returncode != 0:
            raise ValueError(
                f"커밋 없음 — HEAD를 읽을 수 없습니다(빈 repo): {result.stderr.strip()}"
            )
        return result.stdout.strip()


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
    propagator: ChangeEventListener | None = None,
    clock: Clock = _default_clock,
) -> CommitResult:
    """빌더 OKF 편집을 owner author로 커밋하는 오케스트레이션(ADR 0018 결정 1·3·5).

    절차: ① 파일 존재 검증(빈 파일 리스트 거부) ② 파일 경로 검증(번들 밖 탈출 거부)
    ③ `CommitRequest`(author=req.owner) 구성 ④ `gateway.commit_bundle` 호출 → `CommitResult`.

    **카드는 건드리지 않는다**(ADR 0018 결정 1 — OKF 번들만 자동 커밋, 카드는 PR 유지).
    스코프 403은 *web 경계*에서(빌더 카드 검증과 같은 규칙) — 이 함수는 통과 후의 커밋만.

    변경 전파 발화(ADR 0019 결정 1): `propagator`가 주입되면(비None) 커밋 성공 직후
    `OkfChangeEvent`를 구성해 `propagator.on_okf_committed(event)`를 1회 호출한다 — 그
    정책에 기댄 과거 Precedent·답이 stale 플래그·재평가 큐로 간다. `propagator=None`이면
    *기존 동작 그대로*(하위호환 — 기존 호출 무영향·노출 불변식). `parent_sha`는 반드시
    커밋 *전에* `head_sha`를 읽어 얻는다(커밋 후엔 새 SHA가 나오므로).
    """
    if not req.files:
        raise ValueError("커밋할 파일이 없습니다 — files가 비어 있습니다.")
    commit_req = CommitRequest(
        agent_id=req.agent_id,
        files=req.files,
        author=req.owner,
        message=req.message,
    )
    if propagator is not None:
        try:
            parent_sha: str | None = gateway.head_sha(req.agent_id)
        except (ValueError, KeyError):
            parent_sha = None
        result = gateway.commit_bundle(commit_req)
        event = OkfChangeEvent(
            agent_id=req.agent_id,
            new_sha=result.sha,
            parent_sha=parent_sha,
            changed_paths=tuple(f.path for f in req.files),
            author=req.owner,
            committed_at=clock(),
        )
        propagator.on_okf_committed(event)
        return result
    return gateway.commit_bundle(commit_req)
