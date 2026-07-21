from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_org_network.conflict import InMemoryConflictCaseStore
from agent_org_network.decision import RoutingDecision
from agent_org_network.manager_queue import InMemoryManagerQueueStore
from agent_org_network.question_request import (
    InMemoryQuestionRequestStore,
    QuestionRequest,
)
from agent_org_network.question_resolution import (
    AuthorityGrant,
    QuestionResolutionApplication,
)
from agent_org_network.sqlite_stores import SqliteQuestionRequestStore
from agent_org_network.storage_capability import (
    MixedWorkflowDurabilityError,
    NonDurableWorkflowCompositionError,
    UnknownWorkflowDurabilityError,
    validate_workflow_composition,
    workflow_durability_of,
)


def test_inmemory_stores_declare_ephemeral_and_file_sqlite_declares_durable(
    tmp_path: Path,
) -> None:
    sqlite = SqliteQuestionRequestStore(tmp_path / "requests.db")
    try:
        assert workflow_durability_of(InMemoryQuestionRequestStore()) == "ephemeral"
        assert workflow_durability_of(InMemoryConflictCaseStore()) == "ephemeral"
        assert workflow_durability_of(InMemoryManagerQueueStore()) == "ephemeral"
        assert workflow_durability_of(sqlite) == "durable"
    finally:
        sqlite.close()


def test_sqlite_memory_is_ephemeral_not_durable() -> None:
    sqlite = SqliteQuestionRequestStore(":memory:")
    try:
        assert workflow_durability_of(sqlite) == "ephemeral"
    finally:
        sqlite.close()


def test_sqlite_empty_temporary_database_path_is_ephemeral() -> None:
    sqlite = SqliteQuestionRequestStore("")
    try:
        assert workflow_durability_of(sqlite) == "ephemeral"
    finally:
        sqlite.close()


def test_production_gate_rejects_file_sqlite_mixed_with_inmemory_linked_writers(
    tmp_path: Path,
) -> None:
    sqlite = SqliteQuestionRequestStore(tmp_path / "requests.db")
    try:
        with pytest.raises(MixedWorkflowDurabilityError):
            validate_workflow_composition(
                requests=sqlite,
                conflicts=InMemoryConflictCaseStore(),
                managers=InMemoryManagerQueueStore(),
                require_durable=True,
            )
    finally:
        sqlite.close()


def test_production_gate_rejects_all_ephemeral_including_sqlite_memory() -> None:
    sqlite = SqliteQuestionRequestStore(":memory:")
    try:
        with pytest.raises(NonDurableWorkflowCompositionError):
            validate_workflow_composition(
                requests=sqlite,
                conflicts=InMemoryConflictCaseStore(),
                managers=InMemoryManagerQueueStore(),
                require_durable=True,
            )
    finally:
        sqlite.close()


def test_production_gate_rejects_unknown_capability_before_using_store() -> None:
    class UnknownRequestStore:
        def create(self, request: QuestionRequest) -> QuestionRequest:
            raise AssertionError("must not be used")

        def get(self, request_id: str) -> QuestionRequest | None:
            raise AssertionError("must not be used")

        def compare_and_set(
            self,
            request_id: str,
            expected_revision: int,
            current: QuestionRequest,
            updated: QuestionRequest,
        ) -> bool:
            raise AssertionError("must not be used")

        def nonterminal(self) -> list[QuestionRequest]:
            raise AssertionError("must not be used")

    with pytest.raises(UnknownWorkflowDurabilityError):
        validate_workflow_composition(
            requests=UnknownRequestStore(),
            conflicts=InMemoryConflictCaseStore(),
            managers=InMemoryManagerQueueStore(),
            require_durable=True,
        )


def test_nonproduction_application_allows_ephemeral_but_production_style_rejects_it() -> None:
    class NeverRouter:
        def route(self, question: str) -> RoutingDecision:
            raise AssertionError("must not be used")

    class NeverAuthority:
        def authorize(
            self,
            org_id: str,
            intent: str,
            agent_id: str,
        ) -> AuthorityGrant | None:
            raise AssertionError("must not be used")

    class Deadline:
        def deadline_for(
            self,
            org_id: str,
            state_kind: str,
            started_at: datetime,
        ) -> datetime:
            return started_at + timedelta(hours=1)

    def build(*, production_style: bool = False) -> QuestionResolutionApplication:
        return QuestionResolutionApplication(
            requests=InMemoryQuestionRequestStore(),
            router=NeverRouter(),
            conflicts=InMemoryConflictCaseStore(),
            managers=InMemoryManagerQueueStore(),
            route_authority=NeverAuthority(),
            deadline_policy=Deadline(),
            request_id_factory=lambda: "req-1",
            clock=lambda: datetime(2026, 7, 12, tzinfo=timezone.utc),
            production_style=production_style,
        )

    build()
    with pytest.raises(NonDurableWorkflowCompositionError):
        build(production_style=True)


def test_all_declared_durable_passes_capability_gate_but_does_not_claim_transaction() -> None:
    class DeclaredDurableRequestStore(InMemoryQuestionRequestStore):
        workflow_durability = "durable"

    class DeclaredDurableConflictStore(InMemoryConflictCaseStore):
        workflow_durability = "durable"

    class DeclaredDurableManagerStore(InMemoryManagerQueueStore):
        workflow_durability = "durable"

    assert (
        validate_workflow_composition(
            requests=DeclaredDurableRequestStore(),
            conflicts=DeclaredDurableConflictStore(),
            managers=DeclaredDurableManagerStore(),
            require_durable=True,
        )
        == "durable"
    )
