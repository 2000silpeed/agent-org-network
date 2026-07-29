"""Card Owner installation's durable document-to-OKF authoring adapter."""

from __future__ import annotations

from base64 import b64decode, b64encode
from hashlib import sha256
import json
import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from agent_org_network.agent_card import AgentCard
from agent_org_network.okf_authoring import OkfAuthor, RawSource, admit_okf, run_authoring_pipeline
from agent_org_network.owner_local_authoring_repository import (
    AuthoringArtifactRef,
    OwnerLocalAuthoringRepository,
)
from agent_org_network.owner_authoring_operation_store import (
    OwnerAuthoringOperationState,
    OwnerAuthoringOperationStore,
)
from agent_org_network.owner_publish_operation_store import (
    OwnerPublishCommitted,
    OwnerPublishOperationStore,
    OwnerPublishPrepared,
)
from agent_org_network.sqlite_production_authoring_runs import PublishingRun
from agent_org_network.production_authoring_identity import AuthoringInvocation
from agent_org_network.sqlite_production_authoring_runs import (
    AuthoringSourceRef,
    AwaitingOwnerReviewRun,
    CompleteAuthoringRunCommand,
    CompleteAuthoringRunResult,
    ReviewAuthoringRunCommand,
    ReviewAuthoringRunResult,
    MAX_SOURCE_BYTES,
    MAX_SOURCE_COUNT,
    MAX_TOTAL_SOURCE_BYTES,
    StartAuthoringRunCommand,
    StartAuthoringRunResult,
)


class OwnerAuthoringError(Exception):
    pass


class OwnerAuthoringUnavailable(OwnerAuthoringError):
    pass


_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class OwnerAuthoringDocument(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: str
    media_type: Literal["text/plain", "text/markdown"]
    content: bytes

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        if not value or not value.strip() or len(value) > 512:
            raise ValueError("bounded owner-local source id required")
        return value

    @field_validator("content")
    @classmethod
    def _content(cls, value: bytes) -> bytes:
        if not 1 <= len(value) <= MAX_SOURCE_BYTES:
            raise ValueError("bounded nonempty source required")
        return value


class OwnerAuthoringRequest(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True)

    organization_id: str
    principal_id: str
    idempotency_key: str
    card: AgentCard
    card_revision: int
    card_digest: str
    author_profile_digest: str
    documents: tuple[OwnerAuthoringDocument, ...]

    @field_validator("organization_id", "principal_id", "idempotency_key")
    @classmethod
    def _opaque(cls, value: str) -> str:
        if _OPAQUE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("card_revision")
    @classmethod
    def _revision(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("positive card revision required")
        return value

    @field_validator("card_digest", "author_profile_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value

    @field_validator("documents")
    @classmethod
    def _documents(
        cls, value: tuple[OwnerAuthoringDocument, ...]
    ) -> tuple[OwnerAuthoringDocument, ...]:
        if not 1 <= len(value) <= MAX_SOURCE_COUNT:
            raise ValueError("bounded document count required")
        if sum(len(document.content) for document in value) > MAX_TOTAL_SOURCE_BYTES:
            raise ValueError("bounded aggregate source bytes required")
        digests = [sha256(document.content).hexdigest() for document in value]
        if len(digests) != len(set(digests)):
            raise ValueError("source payloads must be unique")
        return value

    @model_validator(mode="after")
    def _owner(self) -> "OwnerAuthoringRequest":
        if self.card.owner != self.principal_id:
            raise ValueError("current Card Owner required")
        return self


class CentralAuthoringRuns(Protocol):
    def start(
        self, command: StartAuthoringRunCommand, *, invocation: AuthoringInvocation
    ) -> StartAuthoringRunResult: ...

    def complete(
        self, command: CompleteAuthoringRunCommand, *, invocation: AuthoringInvocation
    ) -> CompleteAuthoringRunResult: ...

    def review(
        self, command: ReviewAuthoringRunCommand, *, invocation: AuthoringInvocation
    ) -> ReviewAuthoringRunResult: ...


class OwnerPublishGitCommit(BaseModel, frozen=True):
    """Read-back evidence for the deterministic O5b git marker."""

    model_config = ConfigDict(extra="forbid", strict=True)
    sha: str
    marker: str

    @field_validator("sha")
    @classmethod
    def _sha(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


class OwnerPublishGit(Protocol):
    def find_by_marker(self, marker: str) -> tuple[OwnerPublishGitCommit, ...]: ...

    def commit_admitted_bundle(
        self, *, marker: str, owner_id: str, agent_id: str, admitted_bundle: bytes
    ) -> OwnerPublishGitCommit: ...


class OwnerPublishIndex(Protocol):
    def committed_tree_digest(self, *, commit_sha: str, agent_id: str) -> str: ...


class OwnerPublishRequest(BaseModel, frozen=True):
    """Owner-local O5b input; the central claim is evidence, not a call seam."""

    model_config = ConfigDict(extra="forbid", strict=True)
    publishing: PublishingRun
    publishing_claim_digest: str
    artifact_ref: AuthoringArtifactRef

    @field_validator("publishing_claim_digest")
    @classmethod
    def _claim(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value

    @model_validator(mode="after")
    def _artifact(self) -> "OwnerPublishRequest":
        run = self.publishing
        ref = self.artifact_ref
        if (ref.organization_id, ref.agent_id, ref.run_id, ref.revision, ref.artifact_kind, ref.artifact_digest) != (run.org_id, run.agent_id, run.run_id, 1, "full_draft_bundle", run.admitted_bundle_digest):
            raise ValueError("exact admitted bundle reference required")
        return self


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _draft_bundle(admitted: object) -> bytes:
    if not isinstance(admitted, BaseModel):
        raise OwnerAuthoringUnavailable()
    return _canonical_json(admitted.model_dump(mode="json"))


def run_owner_authoring(
    request: OwnerAuthoringRequest,
    *,
    invocation: AuthoringInvocation,
    central: CentralAuthoringRuns,
    repository: OwnerLocalAuthoringRepository,
    operations: OwnerAuthoringOperationStore,
    author: OkfAuthor,
) -> AwaitingOwnerReviewRun:
    """Create a local encrypted draft and expose only bounded metadata centrally."""
    if (
        type(request) is not OwnerAuthoringRequest
        or type(invocation) is not AuthoringInvocation
        or invocation.org_id != request.organization_id
        or invocation.principal_id != request.principal_id
    ):
        raise OwnerAuthoringUnavailable()
    refs = tuple(
        sorted(
            (
                AuthoringSourceRef(
                    source_digest=sha256(document.content).hexdigest(),
                    byte_size=len(document.content),
                    media_type=document.media_type,
                )
                for document in request.documents
            ),
            key=lambda ref: ref.source_digest,
        )
    )
    start_command = StartAuthoringRunCommand(
        org_id=request.organization_id,
        principal_id=request.principal_id,
        idempotency_key=request.idempotency_key,
        agent_id=request.card.agent_id,
        expected_card_revision=request.card_revision,
        expected_card_digest=request.card_digest,
        sources=refs,
    )
    identity_digest = sha256(
        _canonical_json(
            {
                "start": start_command.model_dump(mode="json"),
                "author_profile_digest": request.author_profile_digest,
            }
        )
    ).hexdigest()
    operation_key = operations.operation_key(
        request.organization_id, request.card.agent_id, request.idempotency_key
    )
    try:
        state = operations.load(operation_key, identity_digest)
        if state is not None and state.stage in {"draft_ready", "completed"}:
            complete_command = CompleteAuthoringRunCommand.model_validate(
                state.payload["complete_command"]
            )
            draft_ref = AuthoringArtifactRef.model_validate(state.payload["draft_ref"])
            durable_bundle = b64decode(
                str(state.payload["draft_bundle_base64"]), validate=True
            )
            if state.stage == "draft_ready":
                repository.put(draft_ref, durable_bundle)
            if repository.read(draft_ref) != durable_bundle:
                raise OwnerAuthoringUnavailable()
            completed = central.complete(complete_command, invocation=invocation)
            if state.stage == "completed":
                return completed.run
            operations.save(
                operation_key,
                OwnerAuthoringOperationState(
                    identity_digest=identity_digest,
                    stage="completed",
                    payload={
                        **state.payload,
                        "result": completed.run.model_dump(mode="json"),
                    },
                ),
                expected_stage="draft_ready",
            )
            return completed.run
        if state is None:
            started = central.start(start_command, invocation=invocation)
            run = started.run
            operations.save(
                operation_key,
                OwnerAuthoringOperationState(
                    identity_digest=identity_digest,
                    stage="started",
                    payload={
                        "start_command": start_command.model_dump(mode="json"),
                        "run": run.model_dump(mode="json"),
                    },
                ),
                expected_stage=None,
            )
        elif state.stage == "started":
            if state.payload.get("start_command") != start_command.model_dump(mode="json"):
                raise OwnerAuthoringUnavailable()
            from agent_org_network.sqlite_production_authoring_runs import ExtractingRun

            run = ExtractingRun.model_validate(state.payload["run"])
        else:
            raise OwnerAuthoringUnavailable()
        by_digest = {
            sha256(document.content).hexdigest(): document
            for document in request.documents
        }
        raw_sources: list[RawSource] = []
        for ref in refs:
            document = by_digest[ref.source_digest]
            repository.put(
                AuthoringArtifactRef(
                    organization_id=request.organization_id,
                    agent_id=request.card.agent_id,
                    run_id=run.run_id,
                    revision=0,
                    artifact_kind="raw_source",
                    artifact_digest=ref.source_digest,
                ),
                document.content,
            )
            raw_sources.append(
                RawSource(
                    source_id=document.source_id,
                    content=document.content.decode("utf-8"),
                )
            )
        authored = run_authoring_pipeline(
            request.card.agent_id,
            raw_sources,
            author,
            allowed_domains=request.card.domains,
        )
        admission = admit_okf(authored.draft, request.card)
        if admission.violations:
            raise OwnerAuthoringUnavailable()
        bundle = _draft_bundle(admission.admitted)
        bundle_digest = sha256(bundle).hexdigest()
        complete_command = CompleteAuthoringRunCommand(
            organization_id=request.organization_id,
            principal_id=request.principal_id,
            idempotency_key=f"{request.idempotency_key}:complete",
            run_id=run.run_id,
            expected_revision=0,
            expected_card_revision=request.card_revision,
            expected_card_digest=request.card_digest,
            admitted_bundle_digest=bundle_digest,
            document_count=len(admission.admitted.documents),
            edge_count=len(admission.admitted.edges),
            dropped_count=(
                len(admission.dropped_concepts) + len(admission.dropped_edges)
            ),
            author_profile_digest=request.author_profile_digest,
        )
        draft_ref = AuthoringArtifactRef(
            organization_id=request.organization_id,
            agent_id=request.card.agent_id,
            run_id=run.run_id,
            revision=1,
            artifact_kind="full_draft_bundle",
            artifact_digest=bundle_digest,
        )
        operations.save(
            operation_key,
            OwnerAuthoringOperationState(
                identity_digest=identity_digest,
                stage="draft_ready",
                payload={
                    "start_command": start_command.model_dump(mode="json"),
                    "run": run.model_dump(mode="json"),
                    "draft_ref": draft_ref.model_dump(mode="json"),
                    "draft_bundle_base64": b64encode(bundle).decode(),
                    "complete_command": complete_command.model_dump(mode="json"),
                },
            ),
            expected_stage="started",
        )
        repository.put(draft_ref, bundle)
        completed = central.complete(complete_command, invocation=invocation)
        operations.save(
            operation_key,
            OwnerAuthoringOperationState(
                identity_digest=identity_digest,
                stage="completed",
                payload={
                    "start_command": start_command.model_dump(mode="json"),
                    "run": run.model_dump(mode="json"),
                    "draft_ref": draft_ref.model_dump(mode="json"),
                    "draft_bundle_base64": b64encode(bundle).decode(),
                    "complete_command": complete_command.model_dump(mode="json"),
                    "result": completed.run.model_dump(mode="json"),
                },
            ),
            expected_stage="draft_ready",
        )
        return completed.run
    except OwnerAuthoringUnavailable:
        raise
    except Exception as error:
        raise OwnerAuthoringUnavailable() from error


def review_owner_authoring(
    command: ReviewAuthoringRunCommand,
    *,
    invocation: AuthoringInvocation,
    draft_ref: AuthoringArtifactRef,
    repository: OwnerLocalAuthoringRepository,
    central: CentralAuthoringRuns,
) -> object:
    """Publish only a disposition after locally decrypting and checking the O3 draft.

    Edited deliberately has no edited body channel: a subsequent authoring run is
    required before any new publish can be considered.
    """
    if (
        type(command) is not ReviewAuthoringRunCommand
        or type(invocation) is not AuthoringInvocation
        or type(draft_ref) is not AuthoringArtifactRef
        or invocation.org_id != command.organization_id
        or invocation.principal_id != command.principal_id
        or draft_ref.organization_id != command.organization_id
        or draft_ref.run_id != command.run_id
        or draft_ref.revision != 1
        or draft_ref.artifact_kind != "full_draft_bundle"
        or draft_ref.artifact_digest != command.draft_digest
    ):
        raise OwnerAuthoringUnavailable()
    try:
        bundle = repository.read(draft_ref)
        if sha256(bundle).hexdigest() != command.draft_digest:
            raise OwnerAuthoringUnavailable()
        return central.review(command, invocation=invocation).run
    except OwnerAuthoringUnavailable:
        raise
    except Exception as error:
        raise OwnerAuthoringUnavailable() from error


def _publish_prepared(request: OwnerPublishRequest) -> OwnerPublishPrepared:
    run = request.publishing
    draft = OwnerPublishPrepared.model_construct(
        org_id=run.org_id,
        agent_id=run.agent_id,
        run_id=run.run_id,
        review_revision=2,
        publishing_claim_digest=request.publishing_claim_digest,
        card_revision=run.card_revision,
        card_digest=run.card_digest,
        source_set_digest=run.source_set_digest,
        admitted_bundle_digest=run.admitted_bundle_digest,
        artifact_ref=request.artifact_ref,
        operation_digest="0" * 64,
    )
    return OwnerPublishPrepared.model_validate(
        draft.model_dump(mode="python")
        | {"operation_digest": OwnerPublishPrepared.digest_for(draft)}
    )


def _publish_marker(prepared: OwnerPublishPrepared, operation_key: str) -> str:
    # A trailer-safe deterministic marker.  It binds the semantic key and the
    # exact admitted bundle, so a same-key different body cannot be mistaken
    # for a replay after a crash.
    return f"AON-Publish: {operation_key} {prepared.admitted_bundle_digest}"


def publish_owner_authoring(
    request: OwnerPublishRequest,
    *,
    repository: OwnerLocalAuthoringRepository,
    operations: OwnerPublishOperationStore,
    git: OwnerPublishGit,
    index: OwnerPublishIndex,
) -> OwnerPublishCommitted:
    """Commit a claimed local admitted bundle once and durably record its SHA.

    The central claim is supplied as immutable evidence.  This function makes
    no central acceptance call; a missing/corrupt local artifact and any
    unreadable or ambiguous marker outcome fail closed before a new commit.
    """
    if type(request) is not OwnerPublishRequest:
        raise OwnerAuthoringUnavailable()
    try:
        prepared = _publish_prepared(request)
        key = operations.operation_key(prepared.org_id, prepared.agent_id, prepared.run_id)
        current = operations.load(key, prepared.operation_digest)
        if type(current) is OwnerPublishCommitted:
            return current
        if current is None:
            operations.save(key, prepared, expected_stage=None)
            current = prepared
        if type(current) is not OwnerPublishPrepared:
            raise OwnerAuthoringUnavailable()

        # Local bytes are the authority for the body.  Do this before *all*
        # git/index actions, including marker lookup.
        bundle = repository.read(current.artifact_ref)
        if sha256(bundle).hexdigest() != current.admitted_bundle_digest:
            raise OwnerAuthoringUnavailable()
        marker = _publish_marker(current, key)
        found = git.find_by_marker(marker)
        if len(found) > 1 or any(commit.marker != marker for commit in found):
            raise OwnerAuthoringUnavailable()
        if not found:
            made = git.commit_admitted_bundle(
                marker=marker,
                owner_id=request.publishing.owner_id,
                agent_id=current.agent_id,
                admitted_bundle=bundle,
            )
            # Crash-safe idempotency depends on read-back, never on a merely
            # returned SHA from an effect whose durable outcome is unknown.
            found = git.find_by_marker(marker)
            if (
                made.marker != marker
                or len(found) != 1
                or found[0].marker != marker
                or found[0].sha != made.sha
            ):
                raise OwnerAuthoringUnavailable()
        commit = found[0]
        tree_digest = index.committed_tree_digest(
            commit_sha=commit.sha, agent_id=current.agent_id
        )
        if _DIGEST.fullmatch(tree_digest) is None:
            raise OwnerAuthoringUnavailable()
        committed = OwnerPublishCommitted.model_validate(
            current.model_dump(mode="python")
            | {"commit_sha": commit.sha, "committed_tree_index_digest": tree_digest}
        )
        operations.save(key, committed, expected_stage="prepared")
        return committed
    except OwnerAuthoringUnavailable:
        raise
    except Exception as error:
        raise OwnerAuthoringUnavailable() from error


__all__ = [
    "CentralAuthoringRuns",
    "OwnerAuthoringDocument",
    "OwnerAuthoringError",
    "OwnerAuthoringRequest",
    "OwnerAuthoringUnavailable",
    "OwnerPublishGit",
    "OwnerPublishGitCommit",
    "OwnerPublishIndex",
    "OwnerPublishRequest",
    "publish_owner_authoring",
    "run_owner_authoring",
    "review_owner_authoring",
]
