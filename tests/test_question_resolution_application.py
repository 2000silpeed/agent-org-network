from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_org_network.agent_card import AgentCard
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorizationGrant as CentralAuthorizationGrant,
    CentralAuthorizer,
    ResourceRef,
)
from agent_org_network.conflict import (
    Candidate,
    ConflictCase,
    InMemoryConflictCaseStore,
    Resolution,
)
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.manager_queue import (
    Dismiss,
    FromUnowned,
    InMemoryManagerQueueStore,
    ManagerItem,
    ManagerResolution,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    FailedRequest,
    InMemoryQuestionRequestStore,
    ReadyToDispatch,
    Received,
)
from agent_org_network.question_resolution import (
    AskQuestion,
    AuthorityGrant,
    InitialRoutingError,
    InvalidInitialRoutingError,
    QuestionAuthorizationDeniedError,
    QuestionAuthorizationUnavailableError,
    QuestionResolutionApplication,
    RequestAnswered,
    RequestFailed,
    RequestNotFound,
    RequestPending,
    RequesterPrincipal,
    RouteAuthorityDeniedError,
)
from agent_org_network.request_correlation import LinkedEntityMismatchError


NOW = datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)


def _card(agent_id: str, owner: str) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="support",
        summary=f"{agent_id} summary",
        domains=["refund"],
        last_reviewed_at=date(2026, 7, 1),
    )


class _DeadlinePolicy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, datetime]] = []

    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        self.calls.append((org_id, state_kind, started_at))
        return started_at + timedelta(hours=1)


class _Authority:
    def __init__(self, grant: AuthorityGrant | None = None) -> None:
        self.grant: AuthorityGrant | None = (
            grant if grant is not None else AuthorityGrant(policy_version="rules-v7")
        )
        self.calls: list[tuple[str, str, str]] = []

    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant | None:
        self.calls.append((org_id, intent, agent_id))
        return self.grant


class _Router:
    def __init__(
        self,
        decision: Routed | Contested | Unowned | Exception,
        *,
        before_route: Callable[[], None] | None = None,
    ) -> None:
        self.decision = decision
        self.before_route = before_route
        self.questions: list[str] = []

    def route(self, question: str) -> Routed | Contested | Unowned:
        if self.before_route is not None:
            self.before_route()
        self.questions.append(question)
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


def _ids(*values: str) -> Callable[[], str]:
    iterator: Iterator[str] = iter(values)
    return lambda: next(iterator)


def _application(
    *,
    router: _Router,
    request_ids: Callable[[], str] | None = None,
    requests: InMemoryQuestionRequestStore | None = None,
    conflicts: InMemoryConflictCaseStore | None = None,
    managers: InMemoryManagerQueueStore | None = None,
    authority: _Authority | None = None,
    deadline_policy: _DeadlinePolicy | None = None,
    clock: Callable[[], datetime] = lambda: NOW,
    central_authorizer: CentralAuthorizer | None = None,
) -> tuple[
    QuestionResolutionApplication,
    InMemoryQuestionRequestStore,
    InMemoryConflictCaseStore,
    InMemoryManagerQueueStore,
    _Authority,
    _DeadlinePolicy,
]:
    request_store = requests or InMemoryQuestionRequestStore()
    conflict_store = conflicts or InMemoryConflictCaseStore()
    manager_store = managers or InMemoryManagerQueueStore()
    route_authority = authority or _Authority()
    deadlines = deadline_policy or _DeadlinePolicy()
    return (
        QuestionResolutionApplication(
            requests=request_store,
            router=router,
            conflicts=conflict_store,
            managers=manager_store,
            route_authority=route_authority,
            deadline_policy=deadlines,
            request_id_factory=request_ids or _ids("req-1"),
            clock=clock,
            central_authorizer=central_authorizer,
        ),
        request_store,
        conflict_store,
        manager_store,
        route_authority,
        deadlines,
    )


def _command(question: str = "환불 규정은?") -> AskQuestion:
    return AskQuestion(
        principal=RequesterPrincipal(org_id="org-1", subject_id="user-1"),
        question=question,
        session_id="session-1",
        context_snapshot="previous turn",
    )


class _QuestionCentralAuthorizer:
    def __init__(
        self,
        *,
        actions: frozenset[str] = frozenset(
            {"question.create", "question.read", "question.stream"}
        ),
        org_id: str = "org-1",
        unavailable_actions: frozenset[str] = frozenset(),
        authorize_error_actions: frozenset[str] = frozenset(),
        verify_error: bool = False,
    ) -> None:
        self.actions = actions
        self.org_id = org_id
        self.unavailable_actions = unavailable_actions
        self.authorize_error_actions = authorize_error_actions
        self.verify_error = verify_error
        self.calls: list[tuple[AuthenticatedPrincipal, object, ResourceRef]] = []
        self.verified: list[tuple[CentralAuthorizationGrant, object, ResourceRef]] = []
        self._issued: set[int] = set()

    def authorize(
        self,
        principal: AuthenticatedPrincipal,
        action: object,
        resource: ResourceRef,
    ) -> CentralAuthorizationGrant | AuthorizationDenied:
        self.calls.append((principal, action, resource))
        if action in self.authorize_error_actions:
            raise RuntimeError("secret-authorize-token")
        if action in self.unavailable_actions:
            return AuthorizationDenied(kind="policy_unavailable")
        if (
            action not in self.actions
            or principal.org_id != self.org_id
            or resource.org_id != self.org_id
            or (
                resource.owner_subject_id is not None
                and resource.owner_subject_id != principal.subject_id
            )
        ):
            return AuthorizationDenied(kind="not_found_or_denied")
        grant = CentralAuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,  # type: ignore[arg-type]
            resource=resource,
            roles=("requester",),
            policy_version="question-policy-v1",
            policy_digest="a" * 64,
        )
        self._issued.add(id(grant))
        return grant

    def verify(
        self,
        grant: CentralAuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: object,
        resource: ResourceRef,
    ) -> bool:
        self.verified.append((grant, action, resource))
        if self.verify_error:
            raise RuntimeError("secret-verify-token")
        return bool(
            id(grant) in self._issued
            and grant.subject_id == principal.subject_id
            and grant.action == action
            and grant.resource == resource
        )


def _authenticated_command(question: str = "환불 규정은?") -> AskQuestion:
    return AskQuestion(
        principal=AuthenticatedPrincipal(
            org_id="org-1",
            subject_id="user-1",
            identity_provider="company-oidc",
            identity_session_id="session-1",
        ),
        question=question,
    )


def test_central_ask_deny는_ID_clock_deadline_store_router보다_먼저_write_0이다() -> None:
    calls: list[str] = []
    requests = InMemoryQuestionRequestStore()
    router = _Router(Unowned(escalated_to="root-user"))
    central = _QuestionCentralAuthorizer(actions=frozenset())
    app, _, _, _, _, deadlines = _application(
        router=router,
        requests=requests,
        request_ids=lambda: calls.append("id") or "req-1",
        clock=lambda: calls.append("clock") or NOW,
        central_authorizer=central,
    )

    with pytest.raises(QuestionAuthorizationDeniedError) as denied:
        app.ask(_authenticated_command(), result_action="question.read")

    assert str(denied.value) == "질문 권한이 거부되었습니다."
    assert denied.value.__dict__ == {}
    assert calls == []
    assert deadlines.calls == []
    assert requests.nonterminal() == []
    assert router.questions == []
    assert central.calls[0][1:] == (
        "question.create",
        ResourceRef(org_id="org-1", kind="question"),
    )


@pytest.mark.parametrize(
    "principal",
    [
        RequesterPrincipal(org_id="org-1", subject_id="user-1"),
        {"org_id": "org-1", "subject_id": "user-1"},
        AuthenticatedPrincipal(
            org_id="other-org",
            subject_id="user-1",
            identity_provider="company-oidc",
            identity_session_id="session-1",
        ),
    ],
)
def test_central_ask는_weak_duck_cross_org_principal을_deny한다(principal: object) -> None:
    app, requests, _, _, _, _ = _application(
        router=_Router(Unowned(escalated_to="root-user")),
        central_authorizer=_QuestionCentralAuthorizer(),
    )
    command = AskQuestion.model_construct(principal=principal, question="질문")

    with pytest.raises(QuestionAuthorizationDeniedError):
        app.ask(command, result_action="question.read")

    assert requests.nonterminal() == []


def test_central_create와_own_read는_allow하고_read_stream_permission을_분리한다() -> None:
    central = _QuestionCentralAuthorizer(actions=frozenset({"question.create", "question.read"}))
    app, _, _, _, _, _ = _application(
        router=_Router(Unowned(escalated_to="root-user")),
        central_authorizer=central,
    )
    command = _authenticated_command()
    asked = app.ask(command, result_action="question.read")

    own = app.retrieve("req-1", command.principal, action="question.read")
    stream = app.retrieve("req-1", command.principal, action="question.stream")
    other = app.retrieve(
        "req-1",
        AuthenticatedPrincipal(
            org_id="org-1",
            subject_id="user-2",
            identity_provider="company-oidc",
            identity_session_id="session-2",
        ),
        action="question.read",
    )

    assert isinstance(asked, RequestPending)
    assert own == asked
    assert stream == RequestNotFound()
    assert other == RequestNotFound()
    assert central.calls[-3][1:] == (
        "question.read",
        ResourceRef(
            org_id="org-1",
            kind="question",
            resource_id="req-1",
            owner_subject_id="user-1",
        ),
    )


@pytest.mark.parametrize("result_action", ["question.read", "question.stream"])
def test_central_intake는_create와_결과_action을_ID_store전에_모두_요구한다(
    result_action: str,
) -> None:
    side_effects: list[str] = []
    requests = InMemoryQuestionRequestStore()
    central = _QuestionCentralAuthorizer(actions=frozenset({"question.create"}))
    app, _, _, _, _, _ = _application(
        router=_Router(Unowned(escalated_to="root-user")),
        requests=requests,
        request_ids=lambda: side_effects.append("id") or "req-1",
        clock=lambda: side_effects.append("clock") or NOW,
        central_authorizer=central,
    )

    with pytest.raises(QuestionAuthorizationDeniedError):
        app.ask(
            _authenticated_command(),
            result_action=result_action,  # type: ignore[arg-type]
        )

    assert side_effects == []
    assert requests.nonterminal() == []
    assert [call[1] for call in central.calls] == ["question.create", result_action]


@pytest.mark.parametrize("result_action", [None, "question.destroy"])
def test_central_ask는_missing_invalid_result_action을_create보다_먼저_deny한다(
    result_action: object,
) -> None:
    effects: list[str] = []
    central = _QuestionCentralAuthorizer()
    app, requests, _, _, _, _ = _application(
        router=_Router(Unowned(escalated_to="root-user")),
        request_ids=lambda: effects.append("id") or "req-1",
        clock=lambda: effects.append("clock") or NOW,
        central_authorizer=central,
    )

    with pytest.raises(QuestionAuthorizationDeniedError):
        app.ask(
            _authenticated_command(),
            result_action=result_action,  # type: ignore[arg-type]
        )

    assert effects == []
    assert requests.nonterminal() == []
    assert central.calls == []


class _ForgedGrantAuthorizer:
    def authorize(
        self,
        principal: AuthenticatedPrincipal,
        action: object,
        resource: ResourceRef,
    ) -> CentralAuthorizationGrant:
        return CentralAuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,  # type: ignore[arg-type]
            resource=resource,
            roles=("requester",),
            policy_version="forged-v1",
            policy_digest="f" * 64,
        )

    def verify(
        self,
        grant: CentralAuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: object,
        resource: ResourceRef,
    ) -> bool:
        del grant, principal, action, resource
        return False


def test_seal없는_forged_grant는_verify_false로_Request_write_0이다() -> None:
    app, requests, _, _, _, _ = _application(
        router=_Router(Unowned(escalated_to="root-user")),
        central_authorizer=_ForgedGrantAuthorizer(),
    )

    with pytest.raises(QuestionAuthorizationDeniedError):
        app.ask(_authenticated_command(), result_action="question.read")

    assert requests.nonterminal() == []


@pytest.mark.parametrize("failure", ["denied", "authorize_error", "verify_error"])
def test_central_dependency_unavailable은_typed_field_free_error이고_write_0이다(
    failure: str,
) -> None:
    central = _QuestionCentralAuthorizer(
        unavailable_actions=frozenset({"question.create"}) if failure == "denied" else frozenset(),
        authorize_error_actions=(
            frozenset({"question.create"}) if failure == "authorize_error" else frozenset()
        ),
        verify_error=failure == "verify_error",
    )
    app, requests, _, _, _, _ = _application(
        router=_Router(Unowned(escalated_to="root-user")),
        central_authorizer=central,
    )

    with pytest.raises(QuestionAuthorizationUnavailableError) as raised:
        app.ask(_authenticated_command(), result_action="question.read")

    assert raised.value.__dict__ == {}
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "secret" not in str(raised.value)
    assert requests.nonterminal() == []


@pytest.mark.parametrize("action", ["question.read", "question.stream"])
def test_central_read_stream_dependency_unavailable도_typed_error로_분리한다(
    action: str,
) -> None:
    central = _QuestionCentralAuthorizer()
    app, _, _, _, _, _ = _application(
        router=_Router(Unowned(escalated_to="root-user")),
        central_authorizer=central,
    )
    command = _authenticated_command()
    app.ask(command, result_action="question.read")
    central.unavailable_actions = frozenset({action})

    with pytest.raises(QuestionAuthorizationUnavailableError) as raised:
        app.retrieve(
            "req-1",
            command.principal,
            action=action,  # type: ignore[arg-type]
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_ask_persists_received_before_router_then_records_authorized_route() -> None:
    requests = InMemoryQuestionRequestStore()

    def assert_received_exists() -> None:
        current = requests.get("req-1")
        assert current is not None
        assert isinstance(current.state, Received)
        assert current.revision == 0

    router = _Router(
        Routed(
            primary=_card("refund-owner", "owner-1"),
            intent="refund",
            requires_approval=True,
        ),
        before_route=assert_received_exists,
    )
    app, _, conflicts, managers, authority, deadlines = _application(
        router=router,
        requests=requests,
    )

    outcome = app.ask(_command())

    assert outcome == RequestPending(
        request_id="req-1",
        state="ready_to_dispatch",
        retryable=True,
        message="질문을 처리하고 있습니다.",
    )
    stored = requests.get("req-1")
    assert stored is not None
    assert stored.revision == 1
    assert stored.org_id == "org-1"
    assert stored.requester_id == "user-1"
    assert stored.session_id == "session-1"
    assert stored.context_snapshot == "previous turn"
    assert isinstance(stored.state, ReadyToDispatch)
    assert stored.state.attempt == 1
    assert stored.state.trigger_key == "request-dispatch:req-1:1"
    assert stored.state.handling.ref == stored.state.trigger_key
    assert stored.state.route.agent_id == "refund-owner"
    assert stored.state.route.intent == "refund"
    assert stored.state.route.requires_approval is True
    assert stored.state.route.authority_version == "rules-v7"
    assert authority.calls == [("org-1", "refund", "refund-owner")]
    assert router.questions == ["환불 규정은?"]
    assert conflicts.history == []
    assert managers.history == []
    assert deadlines.calls == [
        ("org-1", "received", NOW),
        ("org-1", "ready_to_dispatch", NOW),
    ]


def test_contested_creates_canonical_request_scoped_case_before_request_cas() -> None:
    router = _Router(
        Contested(
            candidates=(
                _card("z-owner", "owner-z"),
                _card("a-owner", "owner-a"),
                _card("a-owner", "owner-a"),
            ),
            intent="refund",
        )
    )
    app, requests, conflicts, _, _, _ = _application(router=router)

    outcome = app.ask(_command())

    assert isinstance(outcome, RequestPending)
    assert outcome.state == "awaiting_conflict"
    stored = requests.get("req-1")
    assert stored is not None
    assert stored.intent == "refund"
    assert stored.initial_disposition == "contested"
    case = conflicts.get_by_request("req-1")
    assert case is not None
    assert case.question == "환불 규정은?"
    assert case.request_id == "req-1"
    assert case.candidates == (
        Candidate(agent_id="a-owner", owner="owner-a"),
        Candidate(agent_id="z-owner", owner="owner-z"),
    )
    assert stored.state.kind == "awaiting_conflict"
    assert stored.state.handling.ref == case.case_id


@pytest.mark.parametrize("intent", ["", "   "])
def test_unowned_normalizes_blank_intent_and_creates_request_scoped_item(intent: str) -> None:
    router = _Router(Unowned(escalated_to="root-user", reason="no owner", intent=intent))
    app, requests, _, managers, _, _ = _application(router=router)

    outcome = app.ask(_command())

    assert isinstance(outcome, RequestPending)
    assert outcome.state == "awaiting_manager"
    stored = requests.get("req-1")
    assert stored is not None
    assert stored.intent is None
    assert stored.initial_disposition == "unowned"
    item = managers.get_by_request("req-1")
    assert item is not None
    assert item.manager_id == "root-user"
    assert item.request_id == "req-1"
    assert isinstance(item.source, FromUnowned)
    assert item.source.question == "환불 규정은?"
    assert stored.state.kind == "awaiting_manager"
    assert stored.state.handling.ref == item.item_id


def test_unowned_preserves_nonblank_intent() -> None:
    router = _Router(Unowned(escalated_to="root-user", intent="unknown-policy"))
    app, requests, _, _, _, _ = _application(router=router)

    app.ask(_command())

    stored = requests.get("req-1")
    assert stored is not None
    assert stored.intent == "unknown-policy"


def test_same_intent_in_different_requests_creates_distinct_cases() -> None:
    router = _Router(
        Contested(
            candidates=(
                _card("refund-owner", "owner-1"),
                _card("refund-backup", "owner-2"),
            ),
            intent="refund",
        )
    )
    app, _, conflicts, _, _, _ = _application(
        router=router,
        request_ids=_ids("req-1", "req-2"),
    )

    app.ask(_command("first"))
    app.ask(_command("second"))

    first = conflicts.get_by_request("req-1")
    second = conflicts.get_by_request("req-2")
    assert first is not None and second is not None
    assert first.case_id != second.case_id
    assert first.question == "first"
    assert second.question == "second"


def test_retrieve_hides_missing_org_and_requester_with_same_field_free_value() -> None:
    router = _Router(Unowned(escalated_to="root-user"))
    app, _, _, _, _, _ = _application(router=router)
    app.ask(_command())

    missing = app.retrieve(
        "does-not-exist",
        RequesterPrincipal(org_id="org-1", subject_id="user-1"),
    )
    wrong_org = app.retrieve(
        "req-1",
        RequesterPrincipal(org_id="org-2", subject_id="user-1"),
    )
    wrong_subject = app.retrieve(
        "req-1",
        RequesterPrincipal(org_id="org-1", subject_id="user-2"),
    )

    assert missing == wrong_org == wrong_subject == RequestNotFound()
    assert missing.model_dump() == {}
    with pytest.raises(ValidationError):
        RequestNotFound.model_validate({"request_id": "leak"})


def test_router_error_returns_retryable_pending_and_leaves_received() -> None:
    app, requests, _, _, _, _ = _application(router=_Router(RuntimeError("router unavailable")))

    outcome = app.ask(_command())

    assert outcome == RequestPending(
        request_id="req-1",
        state="received",
        retryable=True,
        message="질문을 처리하고 있습니다.",
    )
    stored = requests.get("req-1")
    assert stored is not None
    assert isinstance(stored.state, Received)
    assert stored.revision == 0
    with pytest.raises(InitialRoutingError) as error:
        app.advance("req-1", expected_revision=0)
    assert error.value.request_id == "req-1"
    assert error.value.retryable is True


@pytest.mark.parametrize("expected_revision", [0.0, "0", None])
def test_advance_rejects_non_integer_revision_before_router(
    expected_revision: object,
) -> None:
    router = _Router(RuntimeError("router unavailable"))
    app, requests, _, _, _, _ = _application(router=router)
    app.ask(_command())
    assert router.questions == ["환불 규정은?"]

    with pytest.raises(InitialRoutingError) as error:
        app.advance("req-1", expected_revision=expected_revision)

    assert error.value.code == "initial_routing_conflict"
    assert router.questions == ["환불 규정은?"]
    stored = requests.get("req-1")
    assert stored is not None and isinstance(stored.state, Received)


def test_received_is_still_persisted_when_intake_deadline_policy_fails() -> None:
    class BrokenDeadline(_DeadlinePolicy):
        def deadline_for(
            self,
            org_id: str,
            state_kind: str,
            started_at: datetime,
        ) -> datetime:
            raise RuntimeError("deadline backend unavailable")

    router = _Router(Unowned(escalated_to="root-user"))
    app, requests, _, _, _, _ = _application(
        router=router,
        deadline_policy=BrokenDeadline(),
    )

    outcome = app.ask(_command())

    assert isinstance(outcome, RequestPending)
    assert outcome.state == "received"
    stored = requests.get("req-1")
    assert stored is not None
    assert isinstance(stored.state, Received)
    assert stored.state.handling.due_at == NOW
    assert router.questions == []


def test_backward_transition_clock_fails_before_linked_entity_write() -> None:
    times = iter((NOW, NOW - timedelta(seconds=1)))
    router = _Router(
        Contested(
            candidates=(
                _card("refund-owner", "owner-1"),
                _card("refund-backup", "owner-2"),
            ),
            intent="refund",
        )
    )
    app, requests, conflicts, _, _, _ = _application(
        router=router,
        clock=lambda: next(times),
    )

    outcome = app.ask(_command())

    assert isinstance(outcome, RequestPending)
    assert outcome.state == "received"
    stored = requests.get("req-1")
    assert stored is not None and isinstance(stored.state, Received)
    assert conflicts.history == []


@pytest.mark.parametrize(
    "decision",
    [
        Routed(primary=_card("refund-owner", "owner-1"), intent=""),
        Contested(
            candidates=(
                _card("refund-owner", "owner-1"),
                _card("refund-backup", "owner-2"),
            ),
            intent=" ",
        ),
        Contested(
            candidates=(_card("refund-owner", "owner-1"),),
            intent="refund",
        ),
        Unowned(escalated_to=" ", intent="refund"),
    ],
)
def test_invalid_decision_fails_before_linked_write_and_leaves_received(
    decision: Routed | Contested | Unowned,
) -> None:
    app, requests, conflicts, managers, _, _ = _application(router=_Router(decision))

    with pytest.raises(InvalidInitialRoutingError):
        app.ask(_command())

    stored = requests.get("req-1")
    assert stored is not None
    assert isinstance(stored.state, Received)
    assert conflicts.history == []
    assert managers.history == []


def test_authority_deny_is_fail_closed_and_leaves_received() -> None:
    authority = _Authority()
    authority.grant = None
    app, requests, _, _, _, _ = _application(
        router=_Router(Routed(primary=_card("refund-owner", "owner-1"), intent="refund")),
        authority=authority,
    )

    with pytest.raises(RouteAuthorityDeniedError):
        app.ask(_command())

    stored = requests.get("req-1")
    assert stored is not None
    assert isinstance(stored.state, Received)


def test_blank_authority_policy_version_is_rejected_before_ready_state() -> None:
    authority = _Authority()
    authority.grant = AuthorityGrant.model_construct(policy_version=" ")
    app, requests, _, _, _, _ = _application(
        router=_Router(Routed(primary=_card("refund-owner", "owner-1"), intent="refund")),
        authority=authority,
    )

    with pytest.raises(InvalidInitialRoutingError):
        app.ask(_command())

    stored = requests.get("req-1")
    assert stored is not None and isinstance(stored.state, Received)


def test_terminal_projection_returns_only_stable_ids_and_codes() -> None:
    router = _Router(Routed(primary=_card("refund-owner", "owner-1"), intent="refund"))
    app, requests, _, _, _, _ = _application(router=router)
    app.ask(_command())
    current = requests.get("req-1")
    assert current is not None
    answered = current.transition(AnsweredRequest(record_id="answer-1"), clock=lambda: NOW)
    assert requests.compare_and_set("req-1", 1, current, answered)

    outcome = app.retrieve(
        "req-1",
        RequesterPrincipal(org_id="org-1", subject_id="user-1"),
    )

    assert outcome == RequestAnswered(request_id="req-1", record_id="answer-1")
    assert outcome.model_dump() == {"request_id": "req-1", "record_id": "answer-1"}

    router2 = _Router(RuntimeError("never called"))
    app2, requests2, _, _, _, _ = _application(router=router2, request_ids=_ids("req-2"))
    pending = app2.ask(_command())
    assert isinstance(pending, RequestPending)
    received = requests2.get("req-2")
    assert received is not None
    failed = received.transition(FailedRequest(error_code="routing_exhausted"), clock=lambda: NOW)
    assert requests2.compare_and_set("req-2", 0, received, failed)
    assert app2.retrieve(
        "req-2", RequesterPrincipal(org_id="org-1", subject_id="user-1")
    ) == RequestFailed(
        request_id="req-2",
        error_code="routing_exhausted",
        message="질문을 처리하지 못했습니다.",
    )


def test_conflict_request_index_is_order_sensitive_idempotent_and_survives_resolution() -> None:
    class CorruptibleConflictStore(InMemoryConflictCaseStore):
        def force_transition_for_corruption_test(self, case: ConflictCase) -> None:
            with self._lock:
                self._replace_request_case_unlocked(case)

    store = CorruptibleConflictStore()
    original = ConflictCase.for_request(
        request_id="req-1",
        intent="refund",
        question="refund?",
        candidates=(
            Candidate(agent_id="z", owner="oz"),
            Candidate(agent_id="a", owner="oa"),
        ),
        opened_at=NOW,
        case_id="case-1",
    )
    semantic_retry = ConflictCase.for_request(
        request_id="req-1",
        intent="refund",
        question="refund?",
        candidates=original.candidates,
        opened_at=NOW + timedelta(minutes=1),
        case_id="case-2",
    )
    reversed_retry = ConflictCase.for_request(
        request_id="req-1",
        intent="refund",
        question="refund?",
        candidates=(
            Candidate(agent_id="a", owner="oa"),
            Candidate(agent_id="z", owner="oz"),
        ),
        opened_at=NOW + timedelta(minutes=1),
        case_id="case-2",
    )

    assert store.create_or_get_for_request(original) == (original, True)
    assert store.create_or_get_for_request(semantic_retry) == (original, False)
    with pytest.raises(LinkedEntityMismatchError):
        store.create_or_get_for_request(reversed_retry)
    assert len(store.history) == 1
    resolved = original.resolve(Resolution(intent="refund", primary="a"))
    store.force_transition_for_corruption_test(resolved)
    assert store.get_by_request("req-1") == resolved

    changed = ConflictCase.for_request(
        request_id="req-1",
        intent="refund",
        question="different",
        candidates=original.candidates,
        opened_at=NOW,
    )
    with pytest.raises(LinkedEntityMismatchError):
        store.create_or_get_for_request(changed)

    id_collision = ConflictCase.for_request(
        request_id="req-2",
        intent="refund",
        question="another request",
        candidates=original.candidates,
        opened_at=NOW,
        case_id="case-1",
    )
    with pytest.raises(LinkedEntityMismatchError):
        store.create_or_get_for_request(id_collision)


def test_manager_request_index_is_idempotent_and_legacy_resolution을_거부한다() -> None:
    store = InMemoryManagerQueueStore()
    decision = Unowned(escalated_to="root", reason="none", intent="refund")
    original = ManagerItem.for_request(
        request_id="req-1",
        manager_id="root",
        source=FromUnowned(decision=decision, question="refund?"),
        created_at=NOW,
        item_id="item-1",
    )
    retry = ManagerItem.for_request(
        request_id="req-1",
        manager_id="root",
        source=FromUnowned(decision=decision, question="refund?"),
        created_at=NOW + timedelta(minutes=1),
        item_id="item-2",
    )

    assert store.create_or_get_for_request(original) == (original, True)
    assert store.create_or_get_for_request(retry) == (original, False)
    assert len(store.history) == 1
    assert store.get_by_request("req-1") == original
    resolved = original.resolve(
        ManagerResolution(action=Dismiss(by_manager="root", rationale="done"))
    )
    with pytest.raises(ValueError, match="generation-bound claim"):
        store.mark_resolved(resolved)
    assert store.get_by_request("req-1") == original

    changed = ManagerItem.for_request(
        request_id="req-1",
        manager_id="other",
        source=FromUnowned(decision=decision, question="refund?"),
        created_at=NOW,
    )
    with pytest.raises(LinkedEntityMismatchError):
        store.create_or_get_for_request(changed)


def test_question_resolution_module_has_no_legacy_execution_or_delivery_imports() -> None:
    path = Path(__file__).parents[1] / "src/agent_org_network/question_resolution.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {
        "ask_org",
        "runtime",
        "answer_record",
        "session",
        "audit",
        "web",
        "mcp",
        "console",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.rsplit(".", maxsplit=1)[-1])
        elif isinstance(node, ast.Import):
            imported.update(alias.name.rsplit(".", maxsplit=1)[-1] for alias in node.names)
    assert imported.isdisjoint(forbidden)
