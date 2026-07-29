from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from agent_org_network.owner_authoring_adapter import (
    OwnerAuthoringUnavailable,
    OwnerPublishGitCommit,
    OwnerPublishRequest,
    publish_owner_authoring,
)
from agent_org_network.owner_local_authoring_repository import (
    AuthoringArtifactRef,
    OwnerLocalAuthoringKey,
    OwnerLocalAuthoringRepository,
)
from agent_org_network.owner_publish_operation_store import OwnerPublishOperationStore
from agent_org_network.sqlite_production_authoring_runs import PublishingRun


class Keys:
    def current(self) -> OwnerLocalAuthoringKey:
        return OwnerLocalAuthoringKey(key_id="k", key=b"k" * 32)


class Git:
    def __init__(self) -> None:
        self.commits: dict[str, list[OwnerPublishGitCommit]] = {}
        self.calls = 0

    def find_by_marker(self, marker: str) -> tuple[OwnerPublishGitCommit, ...]:
        return tuple(self.commits.get(marker, []))

    def commit_admitted_bundle(self, *, marker: str, owner_id: str, agent_id: str, admitted_bundle: bytes) -> OwnerPublishGitCommit:
        self.calls += 1
        commit = OwnerPublishGitCommit(sha=sha256((marker + str(self.calls)).encode()).hexdigest(), marker=marker)
        self.commits.setdefault(marker, []).append(commit)
        return commit


class Index:
    def __init__(self) -> None:
        self.calls = 0

    def committed_tree_digest(self, *, commit_sha: str, agent_id: str) -> str:
        self.calls += 1
        return sha256((commit_sha + agent_id).encode()).hexdigest()


def request_and_repo(tmp_path: Path) -> tuple[OwnerPublishRequest, OwnerLocalAuthoringRepository]:
    bundle = b'{"agent_id":"support","documents":[],"edges":[]}'
    digest = sha256(bundle).hexdigest()
    ref = AuthoringArtifactRef(organization_id="acme", agent_id="support", run_id="run-1", revision=1, artifact_kind="full_draft_bundle", artifact_digest=digest)
    repository = OwnerLocalAuthoringRepository(tmp_path / "artifacts", keys=Keys())
    repository.put(ref, bundle)
    run = PublishingRun(org_id="acme", run_id="run-1", agent_id="support", owner_id="owner", card_revision=2, card_digest="a" * 64, source_set_digest="b" * 64, source_count=1, total_bytes=1, created_at="2026-01-01T00:00:00Z", admitted_bundle_digest=digest, document_count=0, edge_count=0, dropped_count=0, author_profile_digest="c" * 64, completed_at="2026-01-01T00:01:00Z", outcome="Approved", reviewed_at="2026-01-01T00:02:00Z", publish_claimed_at="2026-01-01T00:03:00Z")
    return OwnerPublishRequest(publishing=run, publishing_claim_digest="d" * 64, artifact_ref=ref), repository


def test_publish_replay_and_crash_readback_are_one_commit(tmp_path: Path) -> None:
    request, repository = request_and_repo(tmp_path)
    store = OwnerPublishOperationStore(tmp_path / "journal.sqlite", keys=Keys())
    git, index = Git(), Index()
    first = publish_owner_authoring(request, repository=repository, operations=store, git=git, index=index)
    again = publish_owner_authoring(request, repository=repository, operations=store, git=git, index=index)
    assert again == first
    assert git.calls == 1


def test_existing_ambiguous_marker_and_missing_artifact_do_not_commit(tmp_path: Path) -> None:
    request, repository = request_and_repo(tmp_path)
    store = OwnerPublishOperationStore(tmp_path / "journal.sqlite", keys=Keys())
    git, index = Git(), Index()
    # First creates Prepared, then an ambiguous read-back outcome must block.
    prepared = __import__("agent_org_network.owner_authoring_adapter", fromlist=["_publish_prepared"])._publish_prepared(request)
    key = store.operation_key("acme", "support", "run-1")
    store.save(key, prepared, expected_stage=None)
    marker = __import__("agent_org_network.owner_authoring_adapter", fromlist=["_publish_marker"])._publish_marker(prepared, key)
    git.commits[marker] = [OwnerPublishGitCommit(sha="1" * 64, marker=marker), OwnerPublishGitCommit(sha="2" * 64, marker=marker)]
    with pytest.raises(OwnerAuthoringUnavailable):
        publish_owner_authoring(request, repository=repository, operations=store, git=git, index=index)
    assert git.calls == index.calls == 0
    repository.path_for(request.artifact_ref).unlink()
    with pytest.raises(OwnerAuthoringUnavailable):
        publish_owner_authoring(request, repository=repository, operations=store, git=Git(), index=Index())


def test_single_lookup_commit_with_wrong_marker_fails_closed_without_new_commit(
    tmp_path: Path,
) -> None:
    request, repository = request_and_repo(tmp_path)
    store = OwnerPublishOperationStore(tmp_path / "journal.sqlite", keys=Keys())
    git, index = Git(), Index()
    git.commits["unrelated"] = [
        OwnerPublishGitCommit(sha="1" * 64, marker="unrelated")
    ]
    git.find_by_marker = lambda _marker: tuple(git.commits["unrelated"])  # type: ignore[method-assign]

    with pytest.raises(OwnerAuthoringUnavailable):
        publish_owner_authoring(
            request, repository=repository, operations=store, git=git, index=index
        )

    assert git.calls == index.calls == 0


def test_wrong_marker_returned_by_commit_fails_closed_without_persisted_commit(
    tmp_path: Path,
) -> None:
    class WrongMarkerGit(Git):
        def find_by_marker(self, marker: str) -> tuple[OwnerPublishGitCommit, ...]:
            if self.calls == 0:
                return ()
            return (OwnerPublishGitCommit(sha="2" * 64, marker="unrelated"),)

        def commit_admitted_bundle(
            self, *, marker: str, owner_id: str, agent_id: str, admitted_bundle: bytes
        ) -> OwnerPublishGitCommit:
            self.calls += 1
            return OwnerPublishGitCommit(sha="2" * 64, marker="unrelated")

    request, repository = request_and_repo(tmp_path)
    store = OwnerPublishOperationStore(tmp_path / "journal.sqlite", keys=Keys())
    git, index = WrongMarkerGit(), Index()

    with pytest.raises(OwnerAuthoringUnavailable):
        publish_owner_authoring(
            request, repository=repository, operations=store, git=git, index=index
        )

    assert git.commits == {}
    assert index.calls == 0
