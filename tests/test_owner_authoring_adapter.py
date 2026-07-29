from hashlib import sha256
import inspect
import json
from pathlib import Path
from datetime import date
from base64 import b64encode
from typing import cast
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr

from agent_org_network.agent_card import AgentCard
from agent_org_network.knowledge_index import ConceptEdge
from agent_org_network.okf_authoring import FakeAuthor, OkfDocumentDraft, RawSource
from agent_org_network.owner_authoring_adapter import (
    OwnerAuthoringDocument,
    OwnerAuthoringRequest,
    OwnerAuthoringUnavailable,
    run_owner_authoring,
)
from agent_org_network.owner_local_authoring_repository import (
    AuthoringArtifactRef,
    OwnerLocalAuthoringKey,
    OwnerLocalAuthoringRepository,
)
from agent_org_network.owner_authoring_web import (
    OwnerAuthoringProductionUnavailable,
    OwnerAuthoringProductionWiring,
    _create_owner_authoring_test_app,  # pyright: ignore[reportPrivateUsage]
    create_owner_authoring_app,
)
from agent_org_network.owner_authoring_operation_store import (
    OwnerAuthoringOperationStore,
)
from agent_org_network.production_authoring_identity import (
    AuthoringIdentitySessionRef,
    AuthoringInvocation,
)
from agent_org_network.sqlite_production_authoring_runs import (
    AwaitingOwnerReviewRun,
    CompleteAuthoringRunResult,
    CompleteAuthoringRunCommand,
    ExtractingRun,
    ReviewedRun,
    ReviewAuthoringRunCommand,
    ReviewAuthoringRunResult,
    StartAuthoringRunResult,
    StartAuthoringRunCommand,
)


class _Keys:
    def current(self) -> OwnerLocalAuthoringKey:
        return OwnerLocalAuthoringKey(key_id="owner-key", key=b"k" * 32)


class _WebVerifier:
    def verify(self, owner_session: str, csrf_proof: str, origin: str) -> bool:
        return (
            owner_session == "paired-session"
            and csrf_proof == "csrf-proof"
            and origin == "https://owner.example"
        )


class _Central:
    def __init__(self) -> None:
        self.started: list[StartAuthoringRunCommand] = []
        self.completed: list[CompleteAuthoringRunCommand] = []
        self.reviewed: list[ReviewAuthoringRunCommand] = []
        self.sessions: list[str] = []
        self.fail_completion = False

    def start(
        self, command: StartAuthoringRunCommand, *, invocation: AuthoringInvocation
    ) -> StartAuthoringRunResult:
        assert invocation.org_id == "acme"
        assert invocation.principal_id == "owner"
        self.sessions.append(invocation.session.value.get_secret_value())
        self.started.append(command)
        return StartAuthoringRunResult(
            run=ExtractingRun(
                org_id=command.org_id,
                run_id="run-1",
                agent_id=command.agent_id,
                owner_id=command.principal_id,
                card_revision=command.expected_card_revision,
                card_digest=command.expected_card_digest,
                source_set_digest="a" * 64,
                source_count=len(command.sources),
                total_bytes=sum(source.byte_size for source in command.sources),
                created_at="2026-07-27T00:00:00.000Z",
            )
        )

    def complete(
        self, command: CompleteAuthoringRunCommand, *, invocation: AuthoringInvocation
    ) -> CompleteAuthoringRunResult:
        assert invocation.org_id == "acme"
        assert invocation.principal_id == "owner"
        self.sessions.append(invocation.session.value.get_secret_value())
        self.completed.append(command)
        if self.fail_completion:
            raise RuntimeError("central unavailable")
        return CompleteAuthoringRunResult(
            run=AwaitingOwnerReviewRun(
                org_id=command.organization_id,
                run_id=command.run_id,
                agent_id="support",
                owner_id=command.principal_id,
                card_revision=command.expected_card_revision,
                card_digest=command.expected_card_digest,
                source_set_digest="a" * 64,
                source_count=1,
                total_bytes=18,
                created_at="2026-07-27T00:00:00.000Z",
                admitted_bundle_digest=command.admitted_bundle_digest,
                document_count=command.document_count,
                edge_count=command.edge_count,
                dropped_count=command.dropped_count,
                author_profile_digest=command.author_profile_digest,
                completed_at="2026-07-27T00:01:00.000Z",
            )
        )

    def review(
        self, command: ReviewAuthoringRunCommand, *, invocation: AuthoringInvocation
    ) -> ReviewAuthoringRunResult:
        assert invocation.org_id == "acme"
        assert invocation.principal_id == "owner"
        self.sessions.append(invocation.session.value.get_secret_value())
        self.reviewed.append(command)
        return ReviewAuthoringRunResult(
            run=ReviewedRun(
                org_id=command.organization_id,
                run_id=command.run_id,
                agent_id="support",
                owner_id=command.principal_id,
                card_revision=command.expected_card_revision,
                card_digest=command.expected_card_digest,
                source_set_digest=command.source_digest,
                source_count=1,
                total_bytes=18,
                created_at="2026-07-27T00:00:00.000Z",
                admitted_bundle_digest=command.draft_digest,
                document_count=1,
                edge_count=1,
                dropped_count=0,
                author_profile_digest="c" * 64,
                completed_at="2026-07-27T00:01:00.000Z",
                outcome=command.outcome,
                reviewed_at="2026-07-27T00:02:00.000Z",
            )
        )


def _card() -> AgentCard:
    return AgentCard(
        agent_id="support",
        owner="owner",
        team="support",
        summary="support",
        domains=["support"],
        last_reviewed_at=date(2026, 7, 27),
    )


def _author() -> FakeAuthor:
    document = OkfDocumentDraft(
        concept_id="refund",
        title="Refund",
        body="Private derived policy",
        core_question="How do refunds work?",
        domain="support",
    )
    return FakeAuthor(
        split_result=(document,),
        derive_result=(document,),
        link_result=(ConceptEdge(from_id="refund", to_id="refund", relation="self"),),
    )


def _request() -> OwnerAuthoringRequest:
    return OwnerAuthoringRequest(
        organization_id="acme",
        principal_id="owner",
        idempotency_key="author-1",
        card=_card(),
        card_revision=2,
        card_digest="b" * 64,
        author_profile_digest="c" * 64,
        documents=(
            OwnerAuthoringDocument(
                source_id="policy.md",
                media_type="text/markdown",
                content=b"PRIVATE RAW POLICY",
            ),
        ),
    )


def _invocation(session: str = "s" * 32) -> AuthoringInvocation:
    return AuthoringInvocation(
        session=AuthoringIdentitySessionRef(value=SecretStr(session)),
        org_id="acme",
        principal_id="owner",
        identity_provider="corp",
    )


def _operations(tmp_path: Path) -> OwnerAuthoringOperationStore:
    return OwnerAuthoringOperationStore(tmp_path / "operations.sqlite", keys=_Keys())


def test_public_owner_factory는_arbitrary_injection을_route등록전에거부한다() -> None:
    import agent_org_network.owner_authoring_web as module

    with pytest.raises(OwnerAuthoringProductionUnavailable):
        create_owner_authoring_app(object())  # type: ignore[arg-type]
    with pytest.raises(OwnerAuthoringProductionUnavailable):
        OwnerAuthoringProductionWiring(
            central=_Central(),  # type: ignore[arg-type]
            repository=object(),  # type: ignore[arg-type]
            operations=object(),  # type: ignore[arg-type]
            author=_author(),  # type: ignore[arg-type]
        )
    signature = inspect.signature(create_owner_authoring_app)
    assert tuple(signature.parameters) == ("wiring",)
    assert signature.parameters["wiring"].default is inspect.Parameter.empty
    assert "_create_owner_authoring_test_app" not in module.__all__
    assert "OwnerRequestBinding" not in module.__all__
    assert "OwnerWebRequestVerifier" not in module.__all__


def test_owner_vertical은_raw와_full_draft를_local암호화하고_central에는_metadata만보낸다(
    tmp_path: Path,
) -> None:
    central = _Central()
    local = OwnerLocalAuthoringRepository(tmp_path / "owner", keys=_Keys())
    result = run_owner_authoring(
        _request(), invocation=_invocation(), central=central, repository=local,
        operations=_operations(tmp_path), author=_author()
    )
    assert result.stage == "AwaitingOwnerReview"
    assert result.revision == 1
    assert len(central.started) == len(central.completed) == 1
    central_wire = json.dumps(
        [
            central.started[0].model_dump(mode="json"),
            central.completed[0].model_dump(mode="json"),
        ]
    )
    assert "PRIVATE RAW POLICY" not in central_wire
    assert "Private derived policy" not in central_wire
    assert "policy.md" not in central_wire
    assert central.started[0].sources[0].source_digest == sha256(
        b"PRIVATE RAW POLICY"
    ).hexdigest()
    assert central.completed[0].document_count == 1
    disk = b"".join(path.read_bytes() for path in (tmp_path / "owner").iterdir())
    assert b"PRIVATE RAW POLICY" not in disk
    assert b"Private derived policy" not in disk
    operation_disk = (tmp_path / "operations.sqlite").read_bytes()
    assert b"PRIVATE RAW POLICY" not in operation_disk
    assert b"Private derived policy" not in operation_disk


def test_completion실패는_완료로표시하지않고_same_key_retry가안정적이다(
    tmp_path: Path,
) -> None:
    central = _Central()
    central.fail_completion = True
    local = OwnerLocalAuthoringRepository(tmp_path / "owner", keys=_Keys())
    operations = _operations(tmp_path)
    with pytest.raises(OwnerAuthoringUnavailable):
        run_owner_authoring(
            _request(), invocation=_invocation(), central=central, repository=local,
            operations=operations, author=_author()
        )
    central.fail_completion = False
    class _MustNotRun:
        def split(
            self, sources: Sequence[RawSource], allowed_domains: Sequence[str]
        ) -> tuple[OkfDocumentDraft, ...]:
            raise AssertionError("LlmAuthor must not rerun")
        def derive_core_questions(
            self, drafts: Sequence[OkfDocumentDraft]
        ) -> tuple[OkfDocumentDraft, ...]:
            raise AssertionError("LlmAuthor must not rerun")
        def link(
            self, drafts: Sequence[OkfDocumentDraft]
        ) -> tuple[ConceptEdge, ...]:
            raise AssertionError("LlmAuthor must not rerun")

    result = run_owner_authoring(
        _request(), invocation=_invocation("t" * 32), central=central,
        repository=local, operations=operations, author=_MustNotRun()
    )
    assert result.stage == "AwaitingOwnerReview"
    assert len(central.completed) == 2
    assert central.completed[0] == central.completed[1]
    assert central.sessions == ["s" * 32, "s" * 32, "t" * 32]
    operation_bytes = (tmp_path / "operations.sqlite").read_bytes()
    assert ("s" * 32).encode() not in operation_bytes
    assert ("t" * 32).encode() not in operation_bytes

    # completed local cache도 권한/graph drift를 우회하지 않는다.
    central.fail_completion = True
    with pytest.raises(OwnerAuthoringUnavailable):
        run_owner_authoring(
            _request(),
            invocation=_invocation("u" * 32),
            central=central,
            repository=local,
            operations=operations,
            author=_MustNotRun(),
        )
    central.fail_completion = False
    draft_ref = AuthoringArtifactRef(
        organization_id="acme",
        agent_id="support",
        run_id="run-1",
        revision=1,
        artifact_kind="full_draft_bundle",
        artifact_digest=central.completed[0].admitted_bundle_digest,
    )
    assert local.delete(draft_ref)
    with pytest.raises(OwnerAuthoringUnavailable):
        run_owner_authoring(
            _request(),
            invocation=_invocation("v" * 32),
            central=central,
            repository=local,
            operations=operations,
            author=_MustNotRun(),
        )


def test_raw는_utf8문서만받고_review_publish를열지않는다(tmp_path: Path) -> None:
    request = _request().model_copy(
        update={
            "documents": (
                OwnerAuthoringDocument(
                    source_id="binary.pdf",
                    media_type="text/plain",
                    content=b"\xff\x00",
                ),
            )
        }
    )
    with pytest.raises(OwnerAuthoringUnavailable):
        run_owner_authoring(
            request,
            invocation=_invocation(),
            central=_Central(),
            repository=OwnerLocalAuthoringRepository(
                tmp_path / "owner", keys=_Keys()
            ),
            operations=_operations(tmp_path),
            author=_author(),
        )


def test_owner_http는_local_binding에서만_raw를풀고_review_publish_route가없다(
    tmp_path: Path,
) -> None:
    central = _Central()
    seen: list[tuple[OwnerAuthoringDocument, ...]] = []

    def bind(
        owner_session: str,
        agent_id: str,
        idempotency_key: str,
        documents: tuple[OwnerAuthoringDocument, ...],
    ) -> tuple[OwnerAuthoringRequest, AuthoringInvocation]:
        assert owner_session == "paired-session"
        assert agent_id == "support"
        seen.append(documents)
        return (
            _request().model_copy(
                update={"idempotency_key": idempotency_key, "documents": documents}
            ),
            _invocation(),
        )

    app = _create_owner_authoring_test_app(
        bind_request=bind,
        central=central,
        repository=OwnerLocalAuthoringRepository(
            tmp_path / "owner", keys=_Keys()
        ),
        operations=_operations(tmp_path),
        author=_author(),
        request_verifier=_WebVerifier(),
    )
    client = TestClient(app)
    assert client.post(  # pyright: ignore[reportUnknownMemberType]
        "/authoring/runs",
        headers={"idempotency-key": "author-1"},
        json={"agent_id": "support", "documents": []},
    ).status_code == 401
    response = cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
        "/authoring/runs",
        headers={
            "idempotency-key": "author-1",
            "cookie": "aon_owner_session=paired-session",
            "x-owner-csrf": "csrf-proof",
            "origin": "https://owner.example",
        },
        json={
            "agent_id": "support",
            "documents": [
                {
                    "source_id": "policy.md",
                    "media_type": "text/markdown",
                    "content_base64": b64encode(b"PRIVATE RAW POLICY").decode(),
                }
            ],
        },
    ))
    assert response.status_code == 200
    assert response.json()["stage"] == "AwaitingOwnerReview"
    assert seen[0][0].content == b"PRIVATE RAW POLICY"
    assert client.post("/authoring/review").status_code == 404  # pyright: ignore[reportUnknownMemberType]
    assert client.post("/authoring/publish").status_code == 404  # pyright: ignore[reportUnknownMemberType]


def test_owner_http는_csrf_origin_count와_unexpected_error를_safe하게닫는다(
    tmp_path: Path,
) -> None:
    def explode(
        owner_session: str,
        agent_id: str,
        idempotency_key: str,
        documents: tuple[OwnerAuthoringDocument, ...],
    ) -> tuple[OwnerAuthoringRequest, AuthoringInvocation]:
        raise RuntimeError("PRIVATE-SECRET")

    app = _create_owner_authoring_test_app(
        bind_request=explode,
        central=_Central(),
        repository=OwnerLocalAuthoringRepository(tmp_path / "owner", keys=_Keys()),
        operations=_operations(tmp_path),
        author=_author(),
        request_verifier=_WebVerifier(),
    )
    client = TestClient(app)
    body = {
        "agent_id": "support",
        "documents": [
            {
                "source_id": "policy.md",
                "media_type": "text/markdown",
                "content_base64": b64encode(b"x").decode(),
            }
        ],
    }
    common = {
        "idempotency-key": "author-1",
        "cookie": "aon_owner_session=paired-session",
        "origin": "https://owner.example",
    }
    denied = cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
        "/authoring/runs",
        headers={**common, "x-owner-csrf": "wrong"},
        json=body,
    ))
    assert denied.status_code == 401
    failed = cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
        "/authoring/runs",
        headers={**common, "x-owner-csrf": "csrf-proof"},
        json=body,
    ))
    assert failed.status_code == 503
    assert "PRIVATE-SECRET" not in failed.text
