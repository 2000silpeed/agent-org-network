from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TypedDict, cast

import pytest
from pydantic import ValidationError

from agent_org_network.approval_evidence import (
    APPROVAL_ACTION_DIGEST_DOMAIN,
    APPROVAL_CANDIDATE_DIGEST_DOMAIN,
    APPROVAL_EVENT_DIGEST_DOMAIN,
    ApprovalApprovedEvent,
    ApprovalApprovedWithEditEvent,
    ApprovalEvent,
    ApprovalEventJournal,
    ApprovalEventRecorder,
    ApprovalEvidenceDependency,
    ApprovalEvidenceIntegrity,
    ApprovalExpiredEvent,
    ApprovalHumanSubject,
    ApprovalReassignedEvent,
    ApprovalRejectedEvent,
    ApprovalRequestedEvent,
    ApprovalRetentionEligibleEvent,
    ApprovalSystemSubject,
    ApprovalUnavailableEvent,
    InMemoryApprovalEventJournal,
    approval_action_digest,
    approval_candidate_digest,
    approval_event_digest,
    canonical_approval_event,
)

T0 = datetime(2026, 7, 14, 9, tzinfo=UTC)
CANDIDATE_DIGEST = approval_candidate_digest(
    {"mode": "full", "snapshot_sha": "snapshot-1", "sources": ["kb-1"], "text": "답"}
)
ACTION_DIGEST = approval_action_digest({"kind": "approve", "by_approver": "owner-1"})
OTHER_ACTION_DIGEST = approval_action_digest(
    {"kind": "approve_with_edit", "by_approver": "owner-2", "edited_text": "수정"}
)
REASON_DIGEST = approval_action_digest({"reason_code": "insufficient_evidence"})
TERMINAL_EVIDENCE_DIGEST = approval_event_digest(
    {"kind": "answered", "request_id": "request-1", "request_revision": 3}
)
HUMAN = ApprovalHumanSubject(subject_id="owner-1")
OTHER_HUMAN = ApprovalHumanSubject(subject_id="owner-2")
BOUNDARY = ApprovalSystemSubject(system_id="approval_boundary")
EXPIRY = ApprovalSystemSubject(system_id="approval_expiry")
RETENTION = ApprovalSystemSubject(system_id="approval_retention")


class _CommonEventKwargs(TypedDict):
    org_id: str
    request_id: str
    item_id: str
    draft_id: str
    approval_round: int
    subject: ApprovalHumanSubject | ApprovalSystemSubject
    candidate_digest: str
    policy_version: str
    occurred_at: datetime


def _common(
    *,
    item_id: str = "approval-1",
    subject: ApprovalHumanSubject | ApprovalSystemSubject = HUMAN,
    occurred_at: datetime = T0,
    policy_version: str = "approval-v1",
) -> _CommonEventKwargs:
    return {
        "org_id": "org-1",
        "request_id": "request-1",
        "item_id": item_id,
        "draft_id": "draft-1",
        "approval_round": 1,
        "subject": subject,
        "candidate_digest": CANDIDATE_DIGEST,
        "policy_version": policy_version,
        "occurred_at": occurred_at,
    }


def _requested(*, item_id: str = "approval-1") -> ApprovalRequestedEvent:
    return ApprovalRequestedEvent(**_common(item_id=item_id, subject=BOUNDARY))


def _approved() -> ApprovalApprovedEvent:
    return ApprovalApprovedEvent(
        **_common(),
        action_digest=ACTION_DIGEST,
        terminal_record_id="record-1",
    )


def _approved_with_edit() -> ApprovalApprovedWithEditEvent:
    return ApprovalApprovedWithEditEvent(
        **_common(),
        action_digest=OTHER_ACTION_DIGEST,
        terminal_record_id="record-1",
    )


def _rejected() -> ApprovalRejectedEvent:
    return ApprovalRejectedEvent(
        **_common(),
        action_digest=ACTION_DIGEST,
        reason_digest=REASON_DIGEST,
    )


def _expired(*, action_digest: str = ACTION_DIGEST) -> ApprovalExpiredEvent:
    return ApprovalExpiredEvent(
        **_common(subject=EXPIRY),
        action_digest=action_digest,
    )


def _reassigned(*, item_id: str = "approval-2") -> ApprovalReassignedEvent:
    common = _common(item_id=item_id, subject=EXPIRY)
    common["approval_round"] = 2
    return ApprovalReassignedEvent(
        **common,
        predecessor_item_id="approval-1",
        action_digest=ACTION_DIGEST,
    )


def _unavailable() -> ApprovalUnavailableEvent:
    return ApprovalUnavailableEvent(
        **_common(subject=EXPIRY),
        action_digest=ACTION_DIGEST,
        error_ref="approval_unavailable",
    )


def _retention_eligible() -> ApprovalRetentionEligibleEvent:
    retain_until = T0 + timedelta(days=30)
    return ApprovalRetentionEligibleEvent(
        **_common(
            subject=RETENTION,
            policy_version="retention-v1",
            occurred_at=retain_until,
        ),
        terminal_kind="answered",
        request_revision=3,
        terminal_at=T0,
        terminal_evidence_digest=TERMINAL_EVIDENCE_DIGEST,
        retain_until=retain_until,
    )


def _all_events() -> tuple[ApprovalEvent, ...]:
    return (
        _requested(),
        _approved(),
        _approved_with_edit(),
        _rejected(),
        _reassigned(),
        _expired(),
        _unavailable(),
        _retention_eligible(),
    )


def test_여덟_사건_arm은_정확한_이름과_결정론_event_id를_가진다() -> None:
    events = _all_events()

    assert tuple(event.kind for event in events) == (
        "requested",
        "approved",
        "approved_with_edit",
        "rejected",
        "reassigned",
        "expired",
        "unavailable",
        "retention_eligible",
    )
    assert all(event.event_id.startswith("approval-event-") for event in events)
    assert all(canonical_approval_event(event) == event for event in events)


def test_사건_schema에는_본문_field가_없다() -> None:
    forbidden = {
        "question",
        "text",
        "candidate",
        "edited_text",
        "reason",
        "reason_code",
        "source",
        "sources",
    }

    for event in _all_events():
        assert forbidden.isdisjoint(type(event).model_fields)
        assert forbidden.isdisjoint(event.model_dump(mode="python"))


def test_digest는_canonical_json과_domain_tag로_분리된다() -> None:
    first = {"한글": [1, True, None], "nested": {"b": 2, "a": 1}}
    reordered = {"nested": {"a": 1, "b": 2}, "한글": [1, True, None]}

    assert approval_candidate_digest(first) == approval_candidate_digest(reordered)
    assert approval_action_digest(first) == approval_action_digest(reordered)
    assert approval_event_digest(first) == approval_event_digest(reordered)
    assert (
        len(
            {
                approval_candidate_digest(first),
                approval_action_digest(first),
                approval_event_digest(first),
            }
        )
        == 3
    )
    assert (
        APPROVAL_CANDIDATE_DIGEST_DOMAIN,
        APPROVAL_ACTION_DIGEST_DOMAIN,
        APPROVAL_EVENT_DIGEST_DOMAIN,
    ) == (
        "aon.approval.candidate.v1",
        "aon.approval.action.v1",
        "aon.approval.event.v1",
    )


def test_event_id는_같은_논리_slot의_가변_payload를_제외한다() -> None:
    requested = _requested()
    changed_requested_payload = _common(
        subject=BOUNDARY,
        occurred_at=T0 + timedelta(minutes=5),
        policy_version="approval-v2",
    )
    changed_requested_payload["candidate_digest"] = "f" * 64
    changed_requested = ApprovalRequestedEvent(**changed_requested_payload)
    assert changed_requested.event_id == requested.event_id

    assert _approved().event_id == _approved_with_edit().event_id == _rejected().event_id
    changed_reassigned_payload = _common(item_id="approval-2", subject=OTHER_HUMAN)
    changed_reassigned_payload["approval_round"] = 3
    changed_reassigned_payload["candidate_digest"] = "e" * 64
    changed_reassigned = ApprovalReassignedEvent(
        **changed_reassigned_payload,
        predecessor_item_id="different-predecessor",
        action_digest=OTHER_ACTION_DIGEST,
    )
    assert _reassigned().event_id == changed_reassigned.event_id

    first_retention = _retention_eligible()
    retain_until = T0 + timedelta(days=30)
    other_policy = ApprovalRetentionEligibleEvent(
        **_common(
            subject=RETENTION,
            policy_version="retention-v2",
            occurred_at=retain_until,
        ),
        terminal_kind="answered",
        request_revision=3,
        terminal_at=T0,
        terminal_evidence_digest=TERMINAL_EVIDENCE_DIGEST,
        retain_until=retain_until,
    )
    assert first_retention.event_id != other_policy.event_id


def test_호출자가_불일치_event_id를_주입할_수_없다() -> None:
    with pytest.raises(ValidationError):
        ApprovalRequestedEvent(
            **_common(subject=BOUNDARY),
            event_id="approval-event-" + "0" * 64,
        )


@pytest.mark.parametrize("digest", ["a" * 63, "A" * 64, "g" * 64, ""])
def test_digest_field는_lowercase_sha256만_받는다(digest: str) -> None:
    payload: dict[str, object] = dict(_common(subject=BOUNDARY))
    payload["candidate_digest"] = digest
    with pytest.raises(ValidationError):
        ApprovalRequestedEvent.model_validate(payload, strict=True)


def test_시간과_extra와_subject는_strict하게_검증된다() -> None:
    with pytest.raises(ValidationError):
        ApprovalRequestedEvent(
            **_common(subject=BOUNDARY, occurred_at=datetime(2026, 7, 14, 9)),
        )
    with pytest.raises(ValidationError):
        ApprovalRequestedEvent.model_validate(
            {**_common(subject=BOUNDARY), "question": "본문 유출"},
            strict=True,
        )
    with pytest.raises(ValidationError):
        ApprovalApprovedEvent(
            **_common(subject=EXPIRY),
            action_digest=ACTION_DIGEST,
            terminal_record_id="record-1",
        )
    with pytest.raises(ValidationError):
        ApprovalRequestedEvent(**_common(subject=EXPIRY))


def test_datetime_subclass는_사건_시각_불변식을_우회할_수_없다() -> None:
    class HostileDatetime(datetime):
        def __lt__(self, other: object) -> bool:
            del other
            return False

    hostile = HostileDatetime(2026, 7, 14, 9, tzinfo=UTC)
    with pytest.raises(ValidationError):
        ApprovalRequestedEvent(**_common(subject=BOUNDARY, occurred_at=hostile))

    payload = _retention_eligible().model_dump(mode="python")
    for field in ("occurred_at", "terminal_at", "retain_until"):
        changed = dict(payload)
        changed[field] = hostile
        with pytest.raises(ValidationError):
            ApprovalRetentionEligibleEvent.model_validate(changed, strict=True)


def test_retention_event는_exact_terminal_revision과_digest에_결박된다() -> None:
    first = _retention_eligible()
    changed_payload = first.model_dump(mode="python")
    changed_payload["request_revision"] = 4
    changed_payload["terminal_evidence_digest"] = "f" * 64
    changed = ApprovalRetentionEligibleEvent.model_validate(changed_payload, strict=True)

    assert changed.event_id == first.event_id
    journal = InMemoryApprovalEventJournal()
    journal.append_once(first)
    with pytest.raises(ApprovalEvidenceIntegrity):
        journal.append_once(changed)


def test_journal은_subclass를_거부하고_canonical_deep_copy를_보관한다() -> None:
    class HostileRequested(ApprovalRequestedEvent):  # pyright: ignore[reportGeneralTypeIssues]
        pass

    journal = InMemoryApprovalEventJournal()
    event = _requested()
    hostile = HostileRequested.model_validate(event.model_dump(mode="python"))
    with pytest.raises(ApprovalEvidenceIntegrity):
        journal.append_once(hostile)

    stored = journal.append_once(event)
    object.__setattr__(event.subject, "system_id", "approval_retention")

    reread = journal.get(stored.event_id)
    assert reread == stored
    assert reread is not stored
    assert reread is not None
    assert reread.subject is not stored.subject


def test_journal은_같은_id와_payload를_noop하고_다른_payload는_거부한다() -> None:
    journal = InMemoryApprovalEventJournal()
    first = _requested()
    same = canonical_approval_event(first)
    collision_payload = _common(subject=BOUNDARY)
    collision_payload["candidate_digest"] = "f" * 64
    collision = ApprovalRequestedEvent(**collision_payload)

    assert journal.append_once(first) == first
    assert journal.append_once(same) == first
    with pytest.raises(ApprovalEvidenceIntegrity) as caught:
        journal.append_once(collision)
    assert str(caught.value) == "Approval 증거 무결성을 확인할 수 없습니다."
    assert journal.for_request("org-1", "request-1") == (first,)


def test_batch는_입력_순서를_지키며_충돌시_전체를_쓰지_않는다() -> None:
    journal = InMemoryApprovalEventJournal()
    expired = _expired()
    reassigned = _reassigned()
    assert journal.append_batch_once((expired, reassigned)) == (expired, reassigned)
    assert journal.for_request("org-1", "request-1") == (expired, reassigned)

    collision = _expired(action_digest=OTHER_ACTION_DIGEST)
    unavailable = _unavailable()
    with pytest.raises(ApprovalEvidenceIntegrity):
        journal.append_batch_once((collision, unavailable))
    assert journal.get(unavailable.event_id) is None
    assert journal.for_request("org-1", "request-1") == (expired, reassigned)


class _FaultJournal:
    def __init__(self, *, fail_after_append: bool) -> None:
        self.inner = InMemoryApprovalEventJournal()
        self.fail_after_append = fail_after_append

    def append_batch_once(self, events: tuple[ApprovalEvent, ...]) -> tuple[ApprovalEvent, ...]:
        if not self.fail_after_append:
            raise OSError("secret-before")
        self.inner.append_batch_once(events)
        raise OSError("secret-after")

    def append_once(self, event: ApprovalEvent) -> ApprovalEvent:
        return self.append_batch_once((event,))[0]

    def get(self, event_id: str) -> ApprovalEvent | None:
        return self.inner.get(event_id)

    def for_request(self, org_id: str, request_id: str) -> tuple[ApprovalEvent, ...]:
        return self.inner.for_request(org_id, request_id)


def test_recorder는_append_응답_유실을_exact_get으로_복구한다() -> None:
    event = _requested()
    journal = _FaultJournal(fail_after_append=True)

    assert ApprovalEventRecorder(journal).record(event) == event
    assert journal.inner.for_request("org-1", "request-1") == (event,)


def test_recorder는_append전_의존성_실패를_field_free로_분류한다() -> None:
    recorder = ApprovalEventRecorder(_FaultJournal(fail_after_append=False))

    with pytest.raises(ApprovalEvidenceDependency) as caught:
        recorder.record(_requested())
    assert caught.value.retryable is True
    assert str(caught.value) == "Approval 증거 의존성을 확인할 수 없습니다."
    assert "secret-before" not in str(caught.value)


class _IntegrityFaultJournal(_FaultJournal):
    def append_batch_once(
        self,
        events: tuple[ApprovalEvent, ...],
    ) -> tuple[ApprovalEvent, ...]:
        if self.fail_after_append:
            self.inner.append_batch_once(events)
        raise ApprovalEvidenceIntegrity()


def test_recorder는_integrity_응답_유실도_exact_get으로_복구한다() -> None:
    event = _requested()
    journal = _IntegrityFaultJournal(fail_after_append=True)

    assert ApprovalEventRecorder(journal).record(event) == event


def test_recorder는_write전_integrity를_dependency로_완화하지_않는다() -> None:
    recorder = ApprovalEventRecorder(_IntegrityFaultJournal(fail_after_append=False))

    with pytest.raises(ApprovalEvidenceIntegrity) as caught:
        recorder.record(_requested())
    assert caught.value.retryable is False


class _HostileJournal:
    def __init__(self, mode: str) -> None:
        self.inner = InMemoryApprovalEventJournal()
        self.mode = mode
        self.expected: ApprovalEvent | None = None

    def append_batch_once(self, events: tuple[ApprovalEvent, ...]) -> tuple[ApprovalEvent, ...]:
        self.expected = events[0]
        stored = self.inner.append_batch_once(events)
        if self.mode == "list_result":
            return cast(tuple[ApprovalEvent, ...], list(stored))
        if self.mode == "wrong_result":
            return (cast(ApprovalEvent, _requested(item_id="other-item")),)
        return stored

    def append_once(self, event: ApprovalEvent) -> ApprovalEvent:
        return self.append_batch_once((event,))[0]

    def get(self, event_id: str) -> ApprovalEvent | None:
        if self.mode == "missing_readback":
            return None
        if self.mode == "wrong_readback":
            return _requested(item_id="other-item")
        return self.inner.get(event_id)

    def for_request(self, org_id: str, request_id: str) -> tuple[ApprovalEvent, ...]:
        return self.inner.for_request(org_id, request_id)


@pytest.mark.parametrize(
    "mode",
    ["list_result", "wrong_result", "missing_readback", "wrong_readback"],
)
def test_recorder는_변조된_return과_readback을_integrity로_거부한다(mode: str) -> None:
    recorder = ApprovalEventRecorder(_HostileJournal(mode))

    with pytest.raises(ApprovalEvidenceIntegrity) as caught:
        recorder.record(_requested())
    assert caught.value.retryable is False
    assert str(caught.value) == "Approval 증거 무결성을 확인할 수 없습니다."


def test_동일_key_32way는_한_사건으로_수렴한다() -> None:
    journal = InMemoryApprovalEventJournal()
    recorder = ApprovalEventRecorder(journal)
    event = _requested()

    def record(_: int) -> ApprovalEvent:
        return recorder.record(event)

    with ThreadPoolExecutor(max_workers=32) as executor:
        results = tuple(executor.map(record, range(32)))

    assert results == (event,) * 32
    assert journal.for_request("org-1", "request-1") == (event,)


def test_서로_다른_key_32way는_유실없이_기록된다() -> None:
    journal = InMemoryApprovalEventJournal()
    recorder = ApprovalEventRecorder(journal)
    events = tuple(_requested(item_id=f"approval-{index}") for index in range(32))

    with ThreadPoolExecutor(max_workers=32) as executor:
        results = tuple(executor.map(recorder.record, events))

    assert set(result.event_id for result in results) == set(event.event_id for event in events)
    assert len(journal.for_request("org-1", "request-1")) == 32


def test_expiry_두_사건은_recorder_batch에서도_원자적이고_순서가_고정된다() -> None:
    journal: ApprovalEventJournal = InMemoryApprovalEventJournal()
    recorder = ApprovalEventRecorder(journal)
    expired = _expired()
    unavailable = _unavailable()

    assert recorder.record_batch((expired, unavailable)) == (expired, unavailable)
    assert journal.for_request("org-1", "request-1") == (expired, unavailable)
