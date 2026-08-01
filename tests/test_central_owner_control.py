"""Deterministic contract tests for the RB3.3b owner-control ports."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agent_org_network.central_owner_control import (
    OwnerControlAction,
    OwnerControlAnswer,
    OwnerControlApplication,
    OwnerControlBinding,
    OwnerControlCorrection,
    OwnerControlCorrectionCommand,
    OwnerControlForbidden,
    OwnerControlPresence,
    OwnerControlScorecard,
    OwnerControlStale,
    OwnerControlUnavailable,
)


NOW = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)


def _binding() -> OwnerControlBinding:
    return OwnerControlBinding(
        org_id="acme", owner_user_id="owner", agent_card_id="support",
        card_revision=4, assignment_generation=2, assignment_revision=1,
        card_digest="a" * 64, credential_ref="cred-1", credential_generation=3,
        device_thumbprint="b" * 64, central_origin="https://central.example.test",
    )


class _Auth:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.bind_calls = 0
        self.authorize_calls: list[tuple[str, str]] = []

    def bind(self) -> OwnerControlBinding:
        self.bind_calls += 1
        return _binding()

    def authorize(self, binding: OwnerControlBinding, action: str) -> bool:
        assert binding.owner_user_id == "owner"
        assert type(action) is str
        self.authorize_calls.append((binding.owner_user_id, action))
        return self.allowed

    def reauthorize(self, binding: OwnerControlBinding, action: str) -> bool:
        assert binding.owner_user_id == "owner"
        assert type(action) is str
        self.authorize_calls.append((binding.owner_user_id, action))
        return self.allowed


class _Reads:
    def answers(
        self, binding: OwnerControlBinding, *, needs_review: bool | None = None,
    ) -> tuple[OwnerControlAnswer, ...]:
        _ = needs_review
        return (
            OwnerControlAnswer(
                org_id=binding.org_id, record_id="answer-1", request_id="request-1", card_id=binding.card_id,
                card_revision=binding.card_revision, assignment_revision=binding.assignment_revision,
                card_digest=binding.card_digest,
                owner_user_id=binding.owner_user_id, assignment_generation=binding.assignment_generation,
                mode="full", question="질문", answer_text="답변", answered_at=NOW,
                review_status="not_required", needs_review=False,
            ),
        )

    def presence(self, binding: OwnerControlBinding) -> OwnerControlPresence:
        return OwnerControlPresence(
            org_id=binding.org_id, owner_user_id=binding.owner_user_id, card_id=binding.card_id,
            card_revision=binding.card_revision, assignment_revision=binding.assignment_revision,
            card_digest=binding.card_digest,
            assignment_generation=binding.assignment_generation,
            status="online", observed_at=NOW,
        )

    def corrections(self, binding: OwnerControlBinding, *, record_id: str) -> tuple[OwnerControlCorrection, ...]:
        _ = binding
        assert record_id == "answer-1"
        return ()

    def scorecard(self, binding: OwnerControlBinding) -> OwnerControlScorecard:
        return OwnerControlScorecard(
            org_id=binding.org_id, owner_user_id=binding.owner_user_id, card_id=binding.card_id,
            card_revision=binding.card_revision, assignment_revision=binding.assignment_revision,
            card_digest=binding.card_digest,
            assignment_generation=binding.assignment_generation,
            window_since=NOW, window_until=NOW, total_answers=1,
            bad_feedback_answers=0, corrected_count=0,
        )


class _Writes:
    def correct(
        self, binding: OwnerControlBinding, command: OwnerControlCorrectionCommand,
    ) -> OwnerControlCorrection:
        assert command.kind == "correct"
        return OwnerControlCorrection(
            org_id=binding.org_id, correction_record_id="correction-1", record_id="answer-1",
            card_id=binding.card_id, card_revision=binding.card_revision,
            assignment_revision=binding.assignment_revision, card_digest=binding.card_digest,
            owner_user_id=binding.owner_user_id,
            assignment_generation=binding.assignment_generation,
            corrected_text=command.corrected_text, corrected_at=NOW,
            rationale=command.rationale,
            review_id=command.review_id, idempotency_key=command.idempotency_key,
            expected_revision=command.expected_revision,
        )


def test_owner_control_is_self_bound_and_reads_are_safe() -> None:
    auth = _Auth()
    app = OwnerControlApplication(auth=auth, reads=_Reads(), writes=_Writes())

    answer = app.answers()[0]
    assert answer.owner_user_id == "owner"
    assert app.answers(needs_review=True) == ()
    assert app.presence().status == "online"
    assert app.corrections(record_id="answer-1") == ()
    assert app.scorecard().total_answers == 1
    assert [action for _owner, action in auth.authorize_calls] == [
        OwnerControlAction.SUPERVISION_READ,
        OwnerControlAction.SUPERVISION_READ,
        OwnerControlAction.SUPERVISION_READ,
        OwnerControlAction.SUPERVISION_READ,
        OwnerControlAction.SUPERVISION_READ,
        OwnerControlAction.SUPERVISION_READ,
        OwnerControlAction.SUPERVISION_READ,
        OwnerControlAction.SUPERVISION_READ,
        OwnerControlAction.SCORECARD_READ,
        OwnerControlAction.SCORECARD_READ,
    ]


def test_owner_control_missing_capability_and_denial_fail_closed() -> None:
    with pytest.raises(OwnerControlUnavailable):
        OwnerControlApplication(auth=None, reads=_Reads()).answers()
    with pytest.raises(OwnerControlUnavailable):
        OwnerControlApplication(auth=_Auth(), reads=None).answers()
    with pytest.raises(OwnerControlForbidden):
        OwnerControlApplication(auth=_Auth(allowed=False), reads=_Reads()).answers()

    class StaleAuth(_Auth):
        def authorize(self, binding: OwnerControlBinding, action: str) -> bool:
            _ = binding, action
            raise OwnerControlStale("generation changed")

        def reauthorize(self, binding: OwnerControlBinding, action: str) -> bool:
            _ = binding, action
            raise OwnerControlStale("generation changed")

    with pytest.raises(OwnerControlStale):
        OwnerControlApplication(auth=StaleAuth(), reads=_Reads()).answers()


def test_owner_control_reauthorizes_before_projection_and_write() -> None:
    class ReauthDenied(_Auth):
        def reauthorize(self, binding: OwnerControlBinding, action: str) -> bool:
            _ = binding, action
            return False

    auth = ReauthDenied()
    app = OwnerControlApplication(auth=auth, reads=_Reads(), writes=_Writes())
    with pytest.raises(OwnerControlForbidden):
        app.answers()
    writes = _CountingWrites()
    app = OwnerControlApplication(auth=auth, reads=_Reads(), writes=writes)
    with pytest.raises(OwnerControlForbidden):
        app.correct(OwnerControlCorrectionCommand(
            kind="correct", review_id="review-1", idempotency_key="key-1",
            corrected_text="수정", rationale="이유", expected_revision=1,
        ))
    assert writes.calls == 0


class _CountingWrites(_Writes):
    def __init__(self) -> None:
        self.calls = 0

    def correct(
        self, binding: OwnerControlBinding, command: OwnerControlCorrectionCommand,
    ) -> OwnerControlCorrection:
        self.calls += 1
        return super().correct(binding, command)


def test_owner_control_rejects_foreign_projection_after_read() -> None:
    class ForeignReads(_Reads):
        def answers(
            self, binding: OwnerControlBinding, *, needs_review: bool | None = None,
        ) -> tuple[OwnerControlAnswer, ...]:
            _ = needs_review
            return (
                OwnerControlAnswer(
                    org_id=binding.org_id, record_id="answer-foreign", request_id="request-1",
                    card_id=binding.card_id, card_revision=binding.card_revision,
                    assignment_revision=binding.assignment_revision, card_digest=binding.card_digest,
                    owner_user_id="other-owner",
                    assignment_generation=binding.assignment_generation, mode="full",
                    question="질문", answer_text="답변", answered_at=NOW,
                    review_status="not_required", needs_review=False,
                ),
            )

    with pytest.raises(OwnerControlForbidden):
        OwnerControlApplication(auth=_Auth(), reads=ForeignReads()).answers()


def test_owner_control_maps_stale_projection_to_stale_error() -> None:
    class StaleReads(_Reads):
        def answers(
            self, binding: OwnerControlBinding, *, needs_review: bool | None = None,
        ) -> tuple[OwnerControlAnswer, ...]:
            _ = needs_review
            return (
                OwnerControlAnswer(
                    org_id=binding.org_id, record_id="answer-stale", request_id="request-1",
                    card_id=binding.card_id, card_revision=binding.card_revision,
                    assignment_revision=binding.assignment_revision,
                    card_digest=binding.card_digest,
                    owner_user_id=binding.owner_user_id,
                    assignment_generation=binding.assignment_generation + 1,
                    mode="full", question="질문", answer_text="답변", answered_at=NOW,
                    review_status="not_required", needs_review=False,
                ),
            )

    with pytest.raises(OwnerControlStale):
        OwnerControlApplication(auth=_Auth(), reads=StaleReads()).answers()


def test_owner_control_flattens_malformed_read_source_to_unavailable() -> None:
    class BrokenReads(_Reads):
        def answers(
            self, binding: OwnerControlBinding, *, needs_review: bool | None = None,
        ) -> tuple[OwnerControlAnswer, ...]:
            _ = binding, needs_review
            raise TypeError("malformed source")

    with pytest.raises(OwnerControlUnavailable):
        OwnerControlApplication(auth=_Auth(), reads=BrokenReads()).answers()


def test_owner_control_rejects_foreign_card_correction() -> None:
    class ForeignCorrectionReads(_Reads):
        def corrections(
            self, binding: OwnerControlBinding, *, record_id: str,
        ) -> tuple[OwnerControlCorrection, ...]:
            return (
                OwnerControlCorrection(
                    org_id=binding.org_id, correction_record_id="correction-foreign",
                    record_id=record_id, card_id="other-card", card_revision=binding.card_revision,
                    assignment_revision=binding.assignment_revision, card_digest=binding.card_digest,
                    owner_user_id=binding.owner_user_id,
                    assignment_generation=binding.assignment_generation, corrected_text="수정",
                    corrected_at=NOW, rationale="이유", review_id="review-1",
                    idempotency_key="key-1", expected_revision=1,
                ),
            )

    with pytest.raises(OwnerControlForbidden):
        OwnerControlApplication(auth=_Auth(), reads=ForeignCorrectionReads()).corrections(
            record_id="answer-1"
        )


def test_owner_control_never_returns_unknown_presence_as_success() -> None:
    class UnknownReads(_Reads):
        def presence(self, binding: OwnerControlBinding) -> OwnerControlPresence:
            return OwnerControlPresence(
                org_id=binding.org_id, owner_user_id=binding.owner_user_id,
                card_id=binding.card_id, card_revision=binding.card_revision,
                assignment_revision=binding.assignment_revision, card_digest=binding.card_digest,
                assignment_generation=binding.assignment_generation,
                status="unknown",
            )

    with pytest.raises(OwnerControlUnavailable):
        OwnerControlApplication(auth=_Auth(), reads=UnknownReads()).presence()


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "correct", "review_id": "review-1", "idempotency_key": "key-1", "corrected_text": "수정", "rationale": "이유", "expected_revision": 1, "token": "secret"},
        {"kind": "correct", "review_id": "review-1", "idempotency_key": "key-1", "corrected_text": "수정", "rationale": "이유", "expected_revision": 1, "owner_user_id": "owner"},
        {"kind": "correct", "review_id": "review-1", "idempotency_key": "key-1", "corrected_text": "\ud800", "rationale": "이유", "expected_revision": 1},
    ],
)
def test_correction_command_rejects_secrets_claims_and_lone_surrogates(payload: dict[str, object]) -> None:
    with pytest.raises((ValidationError, UnicodeEncodeError)):
        OwnerControlCorrectionCommand.model_validate(payload, strict=True)


def test_correction_command_exact_dto_and_append_only_write() -> None:
    command = OwnerControlCorrectionCommand(
        kind="correct", review_id="review-1", idempotency_key="key-1",
        corrected_text="  보존할 공백  ", rationale="근거", expected_revision=2,
    )
    app = OwnerControlApplication(auth=_Auth(), reads=_Reads(), writes=_Writes())
    result = app.correct(command)
    assert result.corrected_text == "  보존할 공백  "
    assert result.source_record_id == "answer-1"


def test_correction_receipt_must_echo_command_binding() -> None:
    class MismatchedWrites(_Writes):
        def correct(
            self, binding: OwnerControlBinding, command: OwnerControlCorrectionCommand,
        ) -> OwnerControlCorrection:
            result = super().correct(binding, command)
            return result.model_copy(update={"idempotency_key": "different-key"})

    command = OwnerControlCorrectionCommand(
        kind="correct", review_id="review-1", idempotency_key="key-1",
        corrected_text="수정", rationale="이유", expected_revision=1,
    )
    with pytest.raises(OwnerControlStale):
        OwnerControlApplication(auth=_Auth(), reads=_Reads(), writes=MismatchedWrites()).correct(command)


def test_scorecard_rejects_partial_metric_projection() -> None:
    with pytest.raises(ValidationError):
        OwnerControlScorecard(
            org_id="acme", owner_user_id="owner", card_id="support",
            card_revision=4, assignment_revision=1, card_digest="a" * 64,
            assignment_generation=2, window_since=NOW, window_until=NOW,
            total_answers=1, bad_feedback_answers=0, corrected_count=0,
            handled_rate=1.0,
        )
