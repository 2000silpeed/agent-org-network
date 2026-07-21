"""P17.3c production-style Question Completion 저장소 조립 gate."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from agent_org_network.sqlite_stores import SqliteQuestionRequestStore
from agent_org_network.storage_capability import (
    NonDurableWorkflowCompositionError,
    QuestionCompletionStorageCapabilityError,
    QuestionCompletionStorageIdentityError,
    validate_question_completion_storage,
)


class _ObservedStorage:
    question_completion_storage_capability = "atomic_v1"

    def __init__(self, durability: Literal["ephemeral", "durable"] = "durable") -> None:
        self._durability: Literal["ephemeral", "durable"] = durability
        self.db_path: str | None = None
        self.durability_reads = 0
        self.method_calls: list[str] = []

    @property
    def workflow_durability(self) -> Literal["ephemeral", "durable"]:
        self.durability_reads += 1
        return self._durability

    def create(self, _: object) -> None:
        self.method_calls.append("create")

    def get(self, _: str) -> None:
        self.method_calls.append("get")

    def compare_and_set(self, *_: object) -> bool:
        self.method_calls.append("compare_and_set")
        return False

    def nonterminal(self) -> list[object]:
        self.method_calls.append("nonterminal")
        return []

    def complete(self, _: object) -> None:
        self.method_calls.append("complete")

    def by_request(self, _: str) -> None:
        self.method_calls.append("by_request")

    def by_record(self, _: str) -> None:
        self.method_calls.append("by_record")


class _ReaderProxy:
    def __init__(self, inner: _ObservedStorage) -> None:
        self.inner = inner
        self.calls: list[tuple[str, str]] = []

    def by_request(self, request_id: str) -> None:
        self.calls.append(("request", request_id))

    def by_record(self, record_id: str) -> None:
        self.calls.append(("record", record_id))


class _AlwaysEqualStorage(_ObservedStorage):
    def __init__(self) -> None:
        super().__init__()
        self.equal_calls = 0

    def __eq__(self, _: object) -> bool:
        self.equal_calls += 1
        return True


class _UnknownStorage:
    def __init__(self) -> None:
        self.method_calls: list[str] = []

    def get(self, _: str) -> None:
        self.method_calls.append("get")

    def complete(self, _: object) -> None:
        self.method_calls.append("complete")

    def by_request(self, _: str) -> None:
        self.method_calls.append("by_request")

    def by_record(self, _: str) -> None:
        self.method_calls.append("by_record")


def test_same_durable_object_is_the_only_production_completion_storage() -> None:
    storage = _ObservedStorage()

    assert (
        validate_question_completion_storage(
            requests=storage,
            completion_uow=storage,
            completion_reader=storage,
            require_durable=True,
        )
        == "durable"
    )
    assert storage.durability_reads == 1
    assert storage.method_calls == []


def test_identity_is_rejected_before_durability_or_store_methods_are_touched() -> None:
    requests = _ObservedStorage()
    completion = _ObservedStorage()

    with pytest.raises(QuestionCompletionStorageIdentityError) as caught:
        validate_question_completion_storage(
            requests=requests,
            completion_uow=completion,
            completion_reader=completion,
            require_durable=True,
        )

    assert str(caught.value) == (
        "production-style Question Completion 조립에는 Question Request Store·"
        "Completion UoW·Completion Reader의 동일 객체 인스턴스가 필요합니다."
    )
    assert requests.durability_reads == completion.durability_reads == 0
    assert requests.method_calls == completion.method_calls == []


def test_same_database_label_does_not_make_separate_instances_identical() -> None:
    requests = _ObservedStorage()
    completion = _ObservedStorage()
    requests.db_path = completion.db_path = "same.db"

    with pytest.raises(QuestionCompletionStorageIdentityError):
        validate_question_completion_storage(
            requests=requests,
            completion_uow=completion,
            completion_reader=completion,
            require_durable=True,
        )


def test_legacy_sqlite_request_store_cannot_be_mixed_with_completion_uow(
    tmp_path: Path,
) -> None:
    requests = SqliteQuestionRequestStore(tmp_path / "completion.db")
    completion = _ObservedStorage()
    try:
        with pytest.raises(QuestionCompletionStorageIdentityError):
            validate_question_completion_storage(
                requests=requests,
                completion_uow=completion,
                completion_reader=completion,
                require_durable=True,
            )
    finally:
        requests.close()

    assert completion.durability_reads == 0
    assert completion.method_calls == []


def test_delegating_reader_proxy_is_not_the_completion_uow_identity() -> None:
    storage = _ObservedStorage()
    reader = _ReaderProxy(storage)

    with pytest.raises(QuestionCompletionStorageIdentityError):
        validate_question_completion_storage(
            requests=storage,
            completion_uow=storage,
            completion_reader=reader,
            require_durable=True,
        )

    assert storage.durability_reads == 0
    assert storage.method_calls == []
    assert reader.calls == []


def test_equality_never_substitutes_for_object_identity() -> None:
    requests = _AlwaysEqualStorage()
    completion = _AlwaysEqualStorage()

    with pytest.raises(QuestionCompletionStorageIdentityError):
        validate_question_completion_storage(
            requests=requests,
            completion_uow=completion,
            completion_reader=completion,
            require_durable=True,
        )

    assert requests.equal_calls == completion.equal_calls == 0
    assert requests.durability_reads == completion.durability_reads == 0


def test_same_ephemeral_object_is_rejected_for_production() -> None:
    storage = _ObservedStorage("ephemeral")

    with pytest.raises(NonDurableWorkflowCompositionError):
        validate_question_completion_storage(
            requests=storage,
            completion_uow=storage,
            completion_reader=storage,
            require_durable=True,
        )

    assert storage.durability_reads == 1
    assert storage.method_calls == []


def test_same_unknown_object_fails_before_any_store_method() -> None:
    storage = _UnknownStorage()

    with pytest.raises(QuestionCompletionStorageCapabilityError):
        validate_question_completion_storage(
            requests=storage,
            completion_uow=storage,
            completion_reader=storage,
            require_durable=True,
        )

    assert storage.method_calls == []


def test_same_legacy_sqlite_request_store_fails_capability_even_if_durable(
    tmp_path: Path,
) -> None:
    storage = SqliteQuestionRequestStore(tmp_path / "legacy-requests.db")
    try:
        with pytest.raises(QuestionCompletionStorageCapabilityError):
            validate_question_completion_storage(
                requests=storage,
                completion_uow=storage,
                completion_reader=storage,
                require_durable=True,
            )
    finally:
        storage.close()


@pytest.mark.parametrize("marker", [None, "", "atomic_v0", "durable"])
def test_unknown_completion_capability_marker_fails_before_durability(
    marker: object,
) -> None:
    storage = _ObservedStorage()
    storage.question_completion_storage_capability = marker  # type: ignore[assignment]

    with pytest.raises(QuestionCompletionStorageCapabilityError):
        validate_question_completion_storage(
            requests=storage,
            completion_uow=storage,
            completion_reader=storage,
            require_durable=True,
        )

    assert storage.durability_reads == 0
    assert storage.method_calls == []


@pytest.mark.parametrize(
    "missing",
    ["create", "get", "compare_and_set", "nonterminal", "complete", "by_request", "by_record"],
)
def test_forged_atomic_marker_missing_required_callable_fails_before_durability(
    missing: str,
) -> None:
    storage = _ObservedStorage()
    setattr(storage, missing, None)

    with pytest.raises(QuestionCompletionStorageCapabilityError):
        validate_question_completion_storage(
            requests=storage,
            completion_uow=storage,
            completion_reader=storage,
            require_durable=True,
        )

    assert storage.durability_reads == 0
    assert storage.method_calls == []


def test_identity_failure_does_not_read_completion_marker_or_durability() -> None:
    class Observed(_ObservedStorage):
        def __init__(self) -> None:
            super().__init__()
            self.marker_reads = 0

        @property
        def question_completion_storage_capability(self) -> str:
            self.marker_reads += 1
            return "atomic_v1"

    requests = Observed()
    completion = Observed()

    with pytest.raises(QuestionCompletionStorageIdentityError):
        validate_question_completion_storage(
            requests=requests,
            completion_uow=completion,
            completion_reader=completion,
            require_durable=True,
        )

    assert requests.marker_reads == completion.marker_reads == 0
    assert requests.durability_reads == completion.durability_reads == 0
    assert requests.method_calls == completion.method_calls == []


def test_nonproduction_capability_query_still_requires_one_atomic_identity() -> None:
    storage = _ObservedStorage("ephemeral")

    assert (
        validate_question_completion_storage(
            requests=storage,
            completion_uow=storage,
            completion_reader=storage,
            require_durable=False,
        )
        == "ephemeral"
    )
