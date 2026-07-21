from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from agent_org_network.approval_retention import (
    ApprovalAnsweredTerminalEvidence,
    ApprovalDeclinedTerminalEvidence,
    ApprovalDraftRetained,
    ApprovalDraftRetentionDecision,
    ApprovalDraftRetentionEvaluated,
    ApprovalDraftRetentionPolicy,
    ApprovalDraftRetentionStatus,
    ApprovalDraftTerminalEvidence,
    ApprovalUnavailableTerminalEvidence,
)


T0 = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=7)
DRAFT_DIGEST = "1" * 64
ACTION_DIGEST = "2" * 64
TERMINAL_DIGEST = "3" * 64
REASON_DIGEST = "4" * 64
EVIDENCE_DIGEST = "5" * 64
TerminalFactory = Callable[[], ApprovalDraftTerminalEvidence]


def _answered() -> ApprovalAnsweredTerminalEvidence:
    return ApprovalAnsweredTerminalEvidence(
        org_id="org-1",
        request_id="request-1",
        current_item_id="approval-1",
        draft_id="draft-1",
        approval_round=1,
        request_revision=3,
        record_id="record-1",
        terminal_digest=TERMINAL_DIGEST,
        candidate_digest=DRAFT_DIGEST,
        action_digest=ACTION_DIGEST,
        approval_policy_version="approval-v1",
        terminal_at=T0,
    )


def _declined() -> ApprovalDeclinedTerminalEvidence:
    return ApprovalDeclinedTerminalEvidence(
        org_id="org-1",
        request_id="request-1",
        current_item_id="approval-1",
        draft_id="draft-1",
        approval_round=1,
        request_revision=3,
        reason_digest=REASON_DIGEST,
        action_digest=ACTION_DIGEST,
        approval_policy_version="approval-v1",
        terminal_at=T0,
    )


def _unavailable() -> ApprovalUnavailableTerminalEvidence:
    return ApprovalUnavailableTerminalEvidence(
        org_id="org-1",
        request_id="request-1",
        current_item_id="approval-1",
        draft_id="draft-1",
        approval_round=1,
        request_revision=3,
        error_code="approval_unavailable",
        evidence_digest=EVIDENCE_DIGEST,
        candidate_digest=DRAFT_DIGEST,
        approval_policy_version="approval-v1",
        lifecycle_policy_version="expiry-v1",
        terminal_at=T0,
    )


@pytest.mark.parametrize("evidence", [_answered(), _declined(), _unavailable()])
def test_terminal_evidence_union_strictly_round_trips(
    evidence: ApprovalDraftTerminalEvidence,
) -> None:
    adapter: TypeAdapter[ApprovalDraftTerminalEvidence] = TypeAdapter(ApprovalDraftTerminalEvidence)

    restored = adapter.validate_python(evidence.model_dump(mode="python"), strict=True)

    assert type(restored) is type(evidence)
    assert restored == evidence


@pytest.mark.parametrize("factory", [_answered, _declined, _unavailable])
def test_terminal_evidence_is_frozen(factory: TerminalFactory) -> None:
    evidence = factory()

    with pytest.raises(ValidationError):
        evidence.request_id = "other"  # type: ignore[attr-defined,misc]


def test_terminal_evidence_rejects_extra_fields() -> None:
    payload = _answered().model_dump(mode="python")
    payload["question"] = "본문을 저장하면 안 됩니다."

    with pytest.raises(ValidationError):
        ApprovalAnsweredTerminalEvidence.model_validate(payload, strict=True)


@pytest.mark.parametrize(
    "field",
    ["terminal_digest", "candidate_digest", "action_digest"],
)
@pytest.mark.parametrize("invalid", ["f" * 63, "g" * 64, "A" * 64, " sha256"])
def test_answered_evidence_rejects_noncanonical_digest(field: str, invalid: str) -> None:
    payload = _answered().model_dump(mode="python")
    payload[field] = invalid

    with pytest.raises(ValidationError):
        ApprovalAnsweredTerminalEvidence.model_validate(payload, strict=True)


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (_declined, "reason_digest"),
        (_declined, "action_digest"),
        (_unavailable, "evidence_digest"),
        (_unavailable, "candidate_digest"),
    ],
)
def test_other_terminal_evidence_rejects_bad_digest(
    factory: TerminalFactory,
    field: str,
) -> None:
    evidence = factory()
    payload = evidence.model_dump(mode="python")
    payload[field] = "not-a-digest"

    with pytest.raises(ValidationError):
        TypeAdapter(ApprovalDraftTerminalEvidence).validate_python(payload, strict=True)


@pytest.mark.parametrize("factory", [_answered, _declined, _unavailable])
def test_terminal_evidence_rejects_naive_time(factory: TerminalFactory) -> None:
    evidence = factory()
    payload = evidence.model_dump(mode="python")
    payload["terminal_at"] = datetime(2026, 7, 14, 9, 0)

    with pytest.raises(ValidationError):
        TypeAdapter(ApprovalDraftTerminalEvidence).validate_python(payload, strict=True)


def test_unavailable_evidence_accepts_only_safe_error_code() -> None:
    payload = _unavailable().model_dump(mode="python")
    payload["error_code"] = "secret backend failure"

    with pytest.raises(ValidationError):
        ApprovalUnavailableTerminalEvidence.model_validate(payload, strict=True)


def test_terminal_evidence_rejects_bool_round() -> None:
    payload = _answered().model_dump(mode="python")
    payload["approval_round"] = True

    with pytest.raises(ValidationError):
        ApprovalAnsweredTerminalEvidence.model_validate(payload, strict=True)


def test_terminal_evidence_requires_exact_request_revision() -> None:
    payload = _answered().model_dump(mode="python")
    payload["request_revision"] = True
    with pytest.raises(ValidationError):
        ApprovalAnsweredTerminalEvidence.model_validate(payload, strict=True)


def test_datetime_subclass_cannot_bypass_terminal_or_policy_bounds() -> None:
    class HostileDatetime(datetime):
        def __lt__(self, other: object) -> bool:
            del other
            return False

    hostile = HostileDatetime(2025, 1, 1, tzinfo=timezone.utc)
    terminal_payload = _answered().model_dump(mode="python")
    terminal_payload["terminal_at"] = hostile
    with pytest.raises(ValidationError):
        ApprovalAnsweredTerminalEvidence.model_validate(terminal_payload, strict=True)

    for field in ("evaluated_at", "retain_until"):
        decision_payload: dict[str, object] = {
            "terminal": _answered(),
            "evaluated_at": T1,
            "policy_version": "retention-v1",
            "retain_until": T1,
            "purge_eligible": True,
        }
        decision_payload[field] = hostile
        with pytest.raises(ValidationError):
            ApprovalDraftRetentionDecision.model_validate(decision_payload, strict=True)


def test_retention_decision_binds_terminal_and_policy_result() -> None:
    terminal = _answered()

    decision = ApprovalDraftRetentionDecision(
        terminal=terminal,
        evaluated_at=T0,
        policy_version="retention-v1",
        retain_until=T1,
        purge_eligible=False,
    )

    assert decision.terminal == terminal
    assert decision.evaluated_at == T0
    assert decision.retain_until == T1


def test_retention_decision_allows_true_at_exact_retain_until() -> None:
    decision = ApprovalDraftRetentionDecision(
        terminal=_declined(),
        evaluated_at=T1,
        policy_version="retention-v1",
        retain_until=T1,
        purge_eligible=True,
    )

    assert decision.purge_eligible is True


def test_retention_decision_allows_false_after_retain_until() -> None:
    decision = ApprovalDraftRetentionDecision(
        terminal=_unavailable(),
        evaluated_at=T1 + timedelta(days=1),
        policy_version="retention-v1",
        retain_until=T1,
        purge_eligible=False,
    )

    assert decision.purge_eligible is False


@pytest.mark.parametrize(
    ("evaluated_at", "retain_until", "purge_eligible"),
    [
        (T0 - timedelta(microseconds=1), T1, False),
        (T0, T0 - timedelta(microseconds=1), False),
        (T1 - timedelta(microseconds=1), T1, True),
    ],
)
def test_retention_decision_rejects_invalid_time_boundaries(
    evaluated_at: datetime,
    retain_until: datetime,
    purge_eligible: bool,
) -> None:
    with pytest.raises(ValidationError):
        ApprovalDraftRetentionDecision(
            terminal=_answered(),
            evaluated_at=evaluated_at,
            policy_version="retention-v1",
            retain_until=retain_until,
            purge_eligible=purge_eligible,
        )


@pytest.mark.parametrize("field", ["evaluated_at", "retain_until"])
def test_retention_decision_rejects_naive_times(field: str) -> None:
    values: dict[str, object] = {
        "terminal": _answered(),
        "evaluated_at": T0,
        "policy_version": "retention-v1",
        "retain_until": T1,
        "purge_eligible": False,
    }
    values[field] = datetime(2026, 7, 14, 9, 0)

    with pytest.raises(ValidationError):
        ApprovalDraftRetentionDecision.model_validate(values, strict=True)


def test_retention_decision_rejects_terminal_subclass() -> None:
    class AnsweredAlias(ApprovalAnsweredTerminalEvidence):
        pass

    alias = AnsweredAlias.model_validate(_answered().model_dump(mode="python"), strict=True)

    with pytest.raises(ValidationError):
        ApprovalDraftRetentionDecision(
            terminal=alias,
            evaluated_at=T0,
            policy_version="retention-v1",
            retain_until=T1,
            purge_eligible=False,
        )


def test_retention_policy_protocol_has_keyword_only_contract() -> None:
    class FakePolicy:
        def evaluate(
            self,
            *,
            terminal: ApprovalDraftTerminalEvidence,
            evaluated_at: datetime,
        ) -> ApprovalDraftRetentionDecision:
            return ApprovalDraftRetentionDecision(
                terminal=terminal,
                evaluated_at=evaluated_at,
                policy_version="retention-v1",
                retain_until=T1,
                purge_eligible=False,
            )

    policy: ApprovalDraftRetentionPolicy = FakePolicy()

    assert policy.evaluate(terminal=_answered(), evaluated_at=T0).policy_version == "retention-v1"


@pytest.mark.parametrize(
    "reason",
    ["active_assignment", "finalization_pending", "terminalization_pending"],
)
def test_retained_status_never_claims_purge(reason: str) -> None:
    status = ApprovalDraftRetained(reason=reason)  # type: ignore[arg-type]

    assert status.purge_eligible is False
    assert status.retain_until is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"purge_eligible": True},
        {"retain_until": T1},
        {"deleted": True},
        {"reason": "unknown"},
    ],
)
def test_retained_status_rejects_overclaims(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {"reason": "active_assignment", **overrides}

    with pytest.raises(ValidationError):
        ApprovalDraftRetained.model_validate(values, strict=True)


def test_evaluated_status_reports_policy_result_without_deletion_claim() -> None:
    status = ApprovalDraftRetentionEvaluated(
        retain_until=T1,
        purge_eligible=True,
        policy_version="retention-v1",
    )

    assert status.retain_until == T1
    assert status.purge_eligible is True
    assert "purged" not in type(status).model_fields
    assert "deleted" not in type(status).model_fields
    assert "redacted" not in type(status).model_fields


def test_evaluated_status_rejects_naive_retain_until_and_extra_claim() -> None:
    with pytest.raises(ValidationError):
        ApprovalDraftRetentionEvaluated(
            retain_until=datetime(2026, 7, 14, 9, 0),
            purge_eligible=False,
            policy_version="retention-v1",
        )
    with pytest.raises(ValidationError):
        ApprovalDraftRetentionEvaluated.model_validate(
            {
                "retain_until": T1,
                "purge_eligible": False,
                "policy_version": "retention-v1",
                "purged": True,
            },
            strict=True,
        )


@pytest.mark.parametrize(
    "status",
    [
        ApprovalDraftRetained(reason="active_assignment"),
        ApprovalDraftRetentionEvaluated(
            retain_until=T1,
            purge_eligible=False,
            policy_version="retention-v1",
        ),
    ],
)
def test_retention_status_union_round_trips(status: ApprovalDraftRetentionStatus) -> None:
    adapter: TypeAdapter[ApprovalDraftRetentionStatus] = TypeAdapter(ApprovalDraftRetentionStatus)
    restored = adapter.validate_python(
        status.model_dump(mode="python"),
        strict=True,
    )

    assert type(restored) is type(status)
    assert restored == status


def test_retention_primitives_have_no_plaintext_body_fields() -> None:
    forbidden = {
        "question",
        "draft_text",
        "edited_text",
        "candidate_text",
        "sources",
        "reason_code",
        "evidence_ref",
        "purged",
        "deleted",
        "redacted",
    }
    models = (
        ApprovalAnsweredTerminalEvidence,
        ApprovalDeclinedTerminalEvidence,
        ApprovalUnavailableTerminalEvidence,
        ApprovalDraftRetentionDecision,
        ApprovalDraftRetained,
        ApprovalDraftRetentionEvaluated,
    )

    for model in models:
        assert forbidden.isdisjoint(model.model_fields)
