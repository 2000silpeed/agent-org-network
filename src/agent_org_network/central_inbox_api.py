"""Exact private Central inbox HTTP boundary (ADR 0080, RB3.2b.5-E1)."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import re
from typing import Literal, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
    SnapshotCentralAuthorizer,
    load_authority_policy_yaml,
)
from agent_org_network.central_browser_auth import (
    constant_time_digest_matches,
    opaque_browser_handle_digest,
)
from agent_org_network.central_browser_auth_sqlite import BrowserSessionCurrentOutcome
from agent_org_network.central_composition import CentralComposition
from agent_org_network.central_inbox_approval import (
    ApprovalDispositionInboxCommand,
    ApprovalDispositionInboxResult,
    ApprovalInboxNotFound,
    ApprovalInboxStaleOrConflict,
    ApprovalInboxUnavailable,
    ApprovalItemDetail,
    ApprovalItemSummary,
    ApprovalReadCommand,
    ApprovalReassignmentCommand,
    ApprovalReassignmentResult,
    ApprovalSessionUnauthenticated,
)
from agent_org_network.central_inbox_conflict import (
    ConflictConcurrenceCommand,
    ConflictConcurrenceResult,
    ConflictCaseDetail,
    ConflictCaseSummary,
    ConflictNotFound,
    ConflictReadCommand,
    ConflictSessionUnauthenticated,
    ConflictStaleOrConflict,
    ConflictUnavailable,
)
from agent_org_network.central_inbox_review import (
    BackupReviewDispositionCommand,
    BackupReviewDispositionResult,
    BackupReviewDetail,
    BackupReviewSummary,
    ReevaluationDetail,
    ReevaluationDispositionCommand,
    ReevaluationDispositionResult,
    ReevaluationSummary,
    ReviewInboxConflict,
    ReviewInboxNotFound,
    ReviewInboxUnavailable,
    ReviewReadCommand,
    ReviewSessionUnauthenticated,
)


_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_IDEMPOTENCY_KEY = _REFERENCE
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_SELF_CLAIM_HEADERS = frozenset(
    {
        "authorization",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-aon-user",
        "x-aon-org",
        "x-aon-owner",
        "x-aon-role",
        "x-aon-permission",
        "x-aon-token",
        "x-aon-token-claim",
        "x-aon-session",
        "x-aon-actor",
        "x-aon-authority",
    }
)


def create_central_inbox_router(composition: CentralComposition) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/inbox/conflicts")
    async def list_conflicts(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        principal = await _read_principal(composition, request, "conflicts")
        if isinstance(principal, Response):
            return principal
        application = composition.conflict_inbox
        if application is None:
            return _inbox_error(503, "unavailable")
        try:
            items = application.list(_conflict_read(principal))
            return _success({"items": [_wire(item) for item in items]})
        except Exception as error:
            return _mapped_error(error)

    @router.get("/v1/inbox/conflicts/{case_id}")
    async def conflict_detail(  # pyright: ignore[reportUnusedFunction]
        request: Request, case_id: str
    ) -> Response:
        principal = await _read_principal(composition, request, "conflicts", case_id)
        if isinstance(principal, Response):
            return principal
        application = composition.conflict_inbox
        if application is None:
            return _inbox_error(503, "unavailable")
        try:
            detail = application.detail(_conflict_read(principal), case_id)
            return (
                _inbox_error(404, "not_found_or_denied")
                if detail is None
                else _success(_wire(detail))
            )
        except Exception as error:
            return _mapped_error(error)

    @router.post("/v1/inbox/conflicts/{case_id}/concurrences")
    async def concur(  # pyright: ignore[reportUnusedFunction]
        request: Request, case_id: str
    ) -> Response:
        principal = await _write_principal(
            composition, request, "conflicts", case_id, "concurrences"
        )
        if isinstance(principal, Response):
            return principal
        body = await _body(request)
        expected = {
            "on_candidate_card_id",
            "stance",
            "rationale",
            "expected_case_revision",
            "expected_request_revision",
            "expected_round",
        }
        if (
            body is None
            or set(body) != expected
            or not _reference(body["on_candidate_card_id"])
            or body["stance"] not in {"keep_as_complement", "withdraw"}
            or not _text(body["rationale"], empty=False)
            or not _positive(body["expected_case_revision"])
            or not _nonnegative(body["expected_request_revision"])
            or not _positive(body["expected_round"])
        ):
            return _inbox_error(422, "invalid_input")
        application = composition.conflict_concurrence
        key = _idempotency_key(request)
        if application is None:
            return _inbox_error(503, "unavailable")
        if key is None:
            return _inbox_error(422, "invalid_input")
        try:
            result = application.concur(
                ConflictConcurrenceCommand(
                    case_id=case_id,
                    identity_session_id=principal.identity_session_id,
                    expected_org_id=principal.org_id,
                    expected_actor_id=principal.subject_id,
                    on_candidate_card_id=cast(str, body["on_candidate_card_id"]),
                    stance=cast(
                        Literal["keep_as_complement", "withdraw"], body["stance"]
                    ),
                    rationale=cast(str, body["rationale"]),
                    expected_case_revision=cast(int, body["expected_case_revision"]),
                    expected_request_revision=cast(
                        int, body["expected_request_revision"]
                    ),
                    expected_round=cast(int, body["expected_round"]),
                    idempotency_key=key,
                )
            )
            return _success(_wire(result))
        except Exception as error:
            return _mapped_error(error)

    @router.get("/v1/inbox/backup-reviews")
    async def list_backup_reviews(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> Response:
        principal = await _read_principal(composition, request, "backup-reviews")
        if isinstance(principal, Response):
            return principal
        application = composition.review_inbox
        if application is None:
            return _inbox_error(503, "unavailable")
        try:
            return _success(
                {
                    "items": [
                        _wire(item)
                        for item in application.list_backup_reviews(
                            _review_read(principal)
                        )
                    ]
                }
            )
        except Exception as error:
            return _mapped_error(error)

    @router.get("/v1/inbox/backup-reviews/{review_id}")
    async def backup_review_detail(  # pyright: ignore[reportUnusedFunction]
        request: Request, review_id: str
    ) -> Response:
        principal = await _read_principal(
            composition, request, "backup-reviews", review_id
        )
        if isinstance(principal, Response):
            return principal
        application = composition.review_inbox
        if application is None:
            return _inbox_error(503, "unavailable")
        try:
            detail = application.backup_review_detail(
                _review_read(principal), review_id
            )
            return (
                _inbox_error(404, "not_found_or_denied")
                if detail is None
                else _success(_wire(detail))
            )
        except Exception as error:
            return _mapped_error(error)

    @router.post("/v1/inbox/backup-reviews/{review_id}/dispositions")
    async def dispose_backup_review(  # pyright: ignore[reportUnusedFunction]
        request: Request, review_id: str
    ) -> Response:
        principal = await _write_principal(
            composition, request, "backup-reviews", review_id, "dispositions"
        )
        if isinstance(principal, Response):
            return principal
        body = await _body(request)
        if not _backup_disposition(body):
            return _inbox_error(422, "invalid_input")
        assert body is not None
        key = _idempotency_key(request)
        application = composition.backup_review_disposition
        if key is None:
            return _inbox_error(422, "invalid_input")
        if application is None:
            return _inbox_error(503, "unavailable")
        try:
            result = application.dispose(
                BackupReviewDispositionCommand(
                    review_id=review_id,
                    identity_session_id=principal.identity_session_id,
                    expected_org_id=principal.org_id,
                    expected_actor_id=principal.subject_id,
                    kind=cast(
                        Literal["approve", "dismiss", "correct"], body["kind"]
                    ),
                    rationale=cast(str, body["rationale"]),
                    corrected_text=cast(str | None, body.get("corrected_text")),
                    expected_revision=cast(int, body["expected_revision"]),
                    idempotency_key=key,
                )
            )
            return _success(_wire(result))
        except Exception as error:
            return _mapped_error(error)

    @router.get("/v1/inbox/reevaluations")
    async def list_reevaluations(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> Response:
        principal = await _read_principal(composition, request, "reevaluations")
        if isinstance(principal, Response):
            return principal
        application = composition.review_inbox
        if application is None:
            return _inbox_error(503, "unavailable")
        try:
            return _success(
                {
                    "items": [
                        _wire(item)
                        for item in application.list_reevaluations(
                            _review_read(principal)
                        )
                    ]
                }
            )
        except Exception as error:
            return _mapped_error(error)

    @router.get("/v1/inbox/reevaluations/{reevaluation_id}")
    async def reevaluation_detail(  # pyright: ignore[reportUnusedFunction]
        request: Request, reevaluation_id: str
    ) -> Response:
        principal = await _read_principal(
            composition, request, "reevaluations", reevaluation_id
        )
        if isinstance(principal, Response):
            return principal
        application = composition.review_inbox
        if application is None:
            return _inbox_error(503, "unavailable")
        try:
            detail = application.reevaluation_detail(
                _review_read(principal), reevaluation_id
            )
            return (
                _inbox_error(404, "not_found_or_denied")
                if detail is None
                else _success(_wire(detail))
            )
        except Exception as error:
            return _mapped_error(error)

    @router.post("/v1/inbox/reevaluations/{reevaluation_id}/dispositions")
    async def dispose_reevaluation(  # pyright: ignore[reportUnusedFunction]
        request: Request, reevaluation_id: str
    ) -> Response:
        principal = await _write_principal(
            composition, request, "reevaluations", reevaluation_id, "dispositions"
        )
        if isinstance(principal, Response):
            return principal
        body = await _body(request)
        if (
            body is None
            or set(body) != {"kind", "rationale", "expected_revision"}
            or body["kind"] not in {"acknowledge", "request_reanswer"}
            or not _text(body["rationale"], empty=False)
            or not _positive(body["expected_revision"])
        ):
            return _inbox_error(422, "invalid_input")
        key = _idempotency_key(request)
        application = composition.reevaluation_disposition
        if key is None:
            return _inbox_error(422, "invalid_input")
        if application is None:
            return _inbox_error(503, "unavailable")
        try:
            result = application.dispose(
                ReevaluationDispositionCommand(
                    reevaluation_id=reevaluation_id,
                    identity_session_id=principal.identity_session_id,
                    expected_org_id=principal.org_id,
                    expected_actor_id=principal.subject_id,
                    kind=cast(
                        Literal["acknowledge", "request_reanswer"], body["kind"]
                    ),
                    rationale=cast(str, body["rationale"]),
                    expected_revision=cast(int, body["expected_revision"]),
                    idempotency_key=key,
                )
            )
            return _success(_wire(result))
        except Exception as error:
            return _mapped_error(error)

    @router.get("/v1/inbox/approvals")
    async def list_approvals(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> Response:
        principal = await _read_principal(composition, request, "approvals")
        if isinstance(principal, Response):
            return principal
        application = composition.approval_inbox
        if application is None:
            return _inbox_error(503, "unavailable")
        try:
            return _success(
                {
                    "items": [
                        _wire(item)
                        for item in application.list(_approval_read(principal))
                    ]
                }
            )
        except Exception as error:
            return _mapped_error(error)

    @router.get("/v1/inbox/approvals/{approval_item_id}")
    async def approval_detail(  # pyright: ignore[reportUnusedFunction]
        request: Request, approval_item_id: str
    ) -> Response:
        principal = await _read_principal(
            composition, request, "approvals", approval_item_id
        )
        if isinstance(principal, Response):
            return principal
        application = composition.approval_inbox
        if application is None:
            return _inbox_error(503, "unavailable")
        try:
            detail = application.detail(_approval_read(principal), approval_item_id)
            return (
                _inbox_error(404, "not_found_or_denied")
                if detail is None
                else _success(_wire(detail))
            )
        except Exception as error:
            return _mapped_error(error)

    @router.post("/v1/inbox/approvals/{approval_item_id}/dispositions")
    async def dispose_approval(  # pyright: ignore[reportUnusedFunction]
        request: Request, approval_item_id: str
    ) -> Response:
        principal = await _write_principal(
            composition, request, "approvals", approval_item_id, "dispositions"
        )
        if isinstance(principal, Response):
            return principal
        body = await _body(request)
        if not _approval_disposition(body):
            return _inbox_error(422, "invalid_input")
        assert body is not None
        key = _idempotency_key(request)
        application = composition.approval_disposition
        if key is None:
            return _inbox_error(422, "invalid_input")
        if application is None:
            return _inbox_error(503, "unavailable")
        try:
            result = application.dispose(
                ApprovalDispositionInboxCommand(
                    approval_item_id=approval_item_id,
                    identity_session_id=principal.identity_session_id,
                    expected_org_id=principal.org_id,
                    expected_actor_id=principal.subject_id,
                    kind=cast(
                        Literal["approve", "approve_with_edit", "reject"],
                        body["kind"],
                    ),
                    expected_approval_item_revision=cast(
                        int, body["expected_approval_item_revision"]
                    ),
                    expected_request_revision=cast(
                        int, body["expected_request_revision"]
                    ),
                    idempotency_key=key,
                    edited_text=cast(str | None, body.get("edited_text")),
                    reason_code=cast(str | None, body.get("reason_code")),
                )
            )
            return _success(_wire(result))
        except Exception as error:
            return _mapped_error(error)

    @router.post("/v1/inbox/approvals/{approval_item_id}/reassignments")
    async def reassign_approval(  # pyright: ignore[reportUnusedFunction]
        request: Request, approval_item_id: str
    ) -> Response:
        principal = await _write_principal(
            composition, request, "approvals", approval_item_id, "reassignments"
        )
        if isinstance(principal, Response):
            return principal
        body = await _body(request)
        if (
            body is None
            or set(body)
            != {
                "target_approver_user_id",
                "target_approval_card_id",
                "expected_approval_item_revision",
                "expected_request_revision",
            }
            or not _reference(body["target_approver_user_id"])
            or not _reference(body["target_approval_card_id"])
            or not _positive(body["expected_approval_item_revision"])
            or not _nonnegative(body["expected_request_revision"])
        ):
            return _inbox_error(422, "invalid_input")
        key = _idempotency_key(request)
        application = composition.approval_reassignment
        if key is None:
            return _inbox_error(422, "invalid_input")
        if application is None:
            return _inbox_error(503, "unavailable")
        try:
            result = application.reassign(
                ApprovalReassignmentCommand(
                    approval_item_id=approval_item_id,
                    identity_session_id=principal.identity_session_id,
                    expected_org_id=principal.org_id,
                    expected_actor_id=principal.subject_id,
                    target_approver_user_id=cast(
                        str, body["target_approver_user_id"]
                    ),
                    target_approval_card_id=cast(
                        str, body["target_approval_card_id"]
                    ),
                    expected_approval_item_revision=cast(
                        int, body["expected_approval_item_revision"]
                    ),
                    expected_request_revision=cast(
                        int, body["expected_request_revision"]
                    ),
                    idempotency_key=key,
                )
            )
            return _success(_wire(result))
        except Exception as error:
            return _mapped_error(error)

    return router


async def _read_principal(
    composition: CentralComposition,
    request: Request,
    *path_parts: str,
) -> AuthenticatedPrincipal | Response:
    if not await _strict_get(request, path_parts):
        return _inbox_error(422, "invalid_input")
    return _current_principal(composition, request)


async def _write_principal(
    composition: CentralComposition,
    request: Request,
    *path_parts: str,
) -> AuthenticatedPrincipal | Response:
    if not _valid_path(request, path_parts):
        return _inbox_error(422, "invalid_input")
    if not _strict_post_envelope(
        request, composition.config.central_public_origin
    ):
        return _inbox_error(422, "invalid_input")
    principal = _current_principal(composition, request)
    if isinstance(principal, Response):
        return principal
    if not _csrf_matches(composition, request, principal):
        return _inbox_error(422, "invalid_input")
    return principal


def _current_principal(
    composition: CentralComposition, request: Request
) -> AuthenticatedPrincipal | Response:
    handle = request.cookies.get("__Host-aon-central-session")
    store = composition.browser_auth
    clock = composition.browser_clock
    if type(handle) is not str or not handle:
        return _inbox_error(401, "session_unavailable")
    if store is None or clock is None:
        return _inbox_error(503, "unavailable")
    try:
        now = clock()
        if now.tzinfo is None or now.utcoffset() is None:
            return _inbox_error(503, "unavailable")
        outcome, session = store.read_current_session(
            opaque_browser_handle_digest(handle), now=now
        )
        if outcome is BrowserSessionCurrentOutcome.UNAUTHENTICATED or session is None:
            return _inbox_error(401, "session_unavailable")
        if outcome is not BrowserSessionCurrentOutcome.ACTIVE:
            return _inbox_error(503, "unavailable")
        principal = AuthenticatedPrincipal(
            org_id=session.org_id,
            subject_id=session.registry_user_id,
            identity_provider=composition.config.oidc_provider_id,
            identity_session_id=session.session_digest,
        )
        authorizer = SnapshotCentralAuthorizer(
            load_authority_policy_yaml(
                composition.config.authority_snapshot_path.read_text(
                    encoding="utf-8"
                ),
                expected_org_id=composition.config.org_id,
            )
        )
        resource = ResourceRef(
            org_id=principal.org_id,
            kind="browser_session",
            resource_id=principal.identity_session_id,
            owner_subject_id=principal.subject_id,
        )
        if (
            type(authorizer.authorize(principal, "session.read", resource))
            is not AuthorizationGrant
        ):
            return _inbox_error(503, "unavailable")
        return principal
    except Exception:
        return _inbox_error(503, "unavailable")


def _csrf_matches(
    composition: CentralComposition,
    request: Request,
    principal: AuthenticatedPrincipal,
) -> bool:
    try:
        handle = request.cookies.get("__Host-aon-central-session")
        cookie = request.cookies.get("__Host-aon-central-csrf")
        header = request.headers.get("x-aon-csrf")
        if not all(type(value) is str and value for value in (handle, cookie, header)):
            return False
        store = composition.browser_auth
        if store is None:
            return False
        session = store.get_session(
            opaque_browser_handle_digest(cast(str, handle))
        )
        return (
            session is not None
            and session.session_digest == principal.identity_session_id
            and constant_time_digest_matches(cast(str, cookie), session.csrf_digest)
            and constant_time_digest_matches(cast(str, header), session.csrf_digest)
        )
    except Exception:
        return False


async def _strict_get(request: Request, path_parts: tuple[str, ...]) -> bool:
    try:
        if not _valid_path(request, path_parts):
            return False
        if (
            tuple(request.query_params.multi_items())
            or request.headers.get("content-length") not in {None, "0"}
            or request.headers.get("content-type") is not None
            or _has_self_claim(request)
        ):
            return False
        async for chunk in request.stream():
            if chunk:
                return False
        return True
    except Exception:
        return False


def _strict_post_envelope(request: Request, origin: str) -> bool:
    try:
        if tuple(request.query_params.multi_items()) or _has_self_claim(request):
            return False
        headers = request.headers
        if (
            headers.get("origin") != origin
            or headers.get("sec-fetch-site") != "same-origin"
            or headers.get("sec-fetch-mode") != "cors"
            or headers.get("sec-fetch-dest") != "empty"
            or headers.get("content-type", "").split(";", 1)[0].strip()
            != "application/json"
        ):
            return False
        length = headers.get("content-length")
        return length is None or (
            length.isascii()
            and length.isdecimal()
            and int(length) <= _MAX_REQUEST_BYTES
        )
    except Exception:
        return False


def _has_self_claim(request: Request) -> bool:
    try:
        return any(
            name in _SELF_CLAIM_HEADERS
            or (name.startswith("x-forwarded-"))
            or (
                name.startswith("x-aon-")
                and name not in {"x-aon-csrf"}
            )
            for name in request.headers.keys()
        )
    except Exception:
        return True


def _valid_path(request: Request, path_parts: tuple[str, ...]) -> bool:
    raw_path = request.scope.get("raw_path", b"")
    if not isinstance(raw_path, bytes) or b"%2f" in raw_path.lower() or b"%5c" in raw_path.lower():
        return False
    if any(not _reference(part) for part in path_parts):
        return False
    suffix = "/".join(path_parts)
    expected = "/v1/inbox" + ("/" + suffix if suffix else "")
    return request.url.path == expected


async def _body(request: Request) -> dict[str, object] | None:
    try:
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > _MAX_REQUEST_BYTES:
                return None
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not 2 <= len(raw) <= _MAX_REQUEST_BYTES:
            return None
        text = raw.decode("utf-8", errors="strict")
        payload: object = json.loads(
            text,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if type(payload) is not dict:
            return None
        body = cast(dict[str, object], payload)
        if any(not _utf8(value) for value in body.values() if type(value) is str):
            return None
        return body
    except Exception:
        return None


def _backup_disposition(body: dict[str, object] | None) -> bool:
    if body is None:
        return False
    kind = body.get("kind")
    keys = (
        {"kind", "corrected_text", "rationale", "expected_revision"}
        if kind == "correct"
        else {"kind", "rationale", "expected_revision"}
    )
    return (
        set(body) == keys
        and kind in {"approve", "dismiss", "correct"}
        and _text(body.get("rationale"), empty=False)
        and _positive(body.get("expected_revision"))
        and (
            kind != "correct"
            or _text(body.get("corrected_text"), empty=False)
        )
    )


def _approval_disposition(body: dict[str, object] | None) -> bool:
    if body is None:
        return False
    kind = body.get("kind")
    common = {
        "kind",
        "expected_approval_item_revision",
        "expected_request_revision",
    }
    keys = (
        common | {"edited_text"}
        if kind == "approve_with_edit"
        else common | {"reason_code"}
        if kind == "reject"
        else common
    )
    return (
        set(body) == keys
        and kind in {"approve", "approve_with_edit", "reject"}
        and _positive(body.get("expected_approval_item_revision"))
        and _nonnegative(body.get("expected_request_revision"))
        and (
            kind != "approve_with_edit"
            or _text(body.get("edited_text"), empty=False)
        )
        and (kind != "reject" or _text(body.get("reason_code"), empty=False))
    )


def _conflict_read(principal: AuthenticatedPrincipal) -> ConflictReadCommand:
    return ConflictReadCommand(
        identity_session_id=principal.identity_session_id,
        expected_org_id=principal.org_id,
        expected_actor_id=principal.subject_id,
    )


def _review_read(principal: AuthenticatedPrincipal) -> ReviewReadCommand:
    return ReviewReadCommand(
        identity_session_id=principal.identity_session_id,
        expected_org_id=principal.org_id,
        expected_actor_id=principal.subject_id,
    )


def _approval_read(principal: AuthenticatedPrincipal) -> ApprovalReadCommand:
    return ApprovalReadCommand(
        identity_session_id=principal.identity_session_id,
        expected_org_id=principal.org_id,
        expected_actor_id=principal.subject_id,
    )


def _mapped_error(error: Exception) -> Response:
    if isinstance(
        error,
        (
            ConflictSessionUnauthenticated,
            ApprovalSessionUnauthenticated,
            ReviewSessionUnauthenticated,
        ),
    ):
        return _inbox_error(401, "session_unavailable")
    if isinstance(
        error, (ConflictNotFound, ApprovalInboxNotFound, ReviewInboxNotFound)
    ):
        return _inbox_error(404, "not_found_or_denied")
    if isinstance(
        error,
        (
            ConflictStaleOrConflict,
            ApprovalInboxStaleOrConflict,
            ReviewInboxConflict,
        ),
    ):
        return _inbox_error(409, "stale_or_conflict")
    if isinstance(
        error,
        (ConflictUnavailable, ApprovalInboxUnavailable, ReviewInboxUnavailable),
    ):
        return _inbox_error(503, "unavailable")
    return _inbox_error(503, "unavailable")


def _wire(value: object) -> dict[str, object]:
    """Project an explicit finite DTO; future domain fields cannot leak."""
    if isinstance(value, ConflictCaseDetail):
        return _conflict_summary_wire(value) | {
            "expected_case_revision": value.expected_case_revision,
            "expected_request_revision": value.expected_request_revision,
            "expected_round": value.expected_round,
            "question": value.question,
            "candidates": [
                {
                    "card_id": item.card_id,
                    "card_revision": item.card_revision,
                    "card_digest": item.card_digest,
                    "owner_user_id": item.owner_user_id,
                    "concept_ref": item.concept_ref,
                    "coverage_digest": item.coverage_digest,
                }
                for item in value.candidates
            ],
            "own_concurrence": (
                None
                if value.own_concurrence is None
                else {
                    "on_candidate_card_id": value.own_concurrence.on_candidate_card_id,
                    "stance": value.own_concurrence.stance,
                    "rationale": value.own_concurrence.rationale,
                    "round": value.own_concurrence.round,
                }
            ),
            "evidence_grants": [
                {
                    "grant_id": item.grant_id,
                    "candidate_card_id": item.candidate_card_id,
                    "candidate_card_revision": item.candidate_card_revision,
                    "concept_ref": item.concept_ref,
                    "expires_at": _timestamp(item.expires_at),
                    "single_use": item.single_use,
                    "status": item.status,
                }
                for item in value.evidence_grants
            ],
        }
    if isinstance(value, ConflictCaseSummary):
        return _conflict_summary_wire(value)
    if isinstance(value, ConflictConcurrenceResult):
        return {
            "receipt_id": value.receipt_id,
            "concurrence_command_digest": value.concurrence_command_digest,
            "case_id": value.case_id,
            "case_revision": value.case_revision,
            "request_id": value.request_id,
            "request_revision": value.request_revision,
            "state": value.state,
            "outcome": value.outcome,
            "replayed": value.replayed,
        }
    if isinstance(value, BackupReviewDetail):
        return _backup_summary_wire(value) | {
            "question": value.question,
            "backup_answer_text": value.backup_answer_text,
            "answering_card_id": value.answering_card_id,
            "answering_card_revision": value.answering_card_revision,
            "owner_user_id": value.owner_user_id,
            "answered_at": _timestamp(value.answered_at),
        }
    if isinstance(value, BackupReviewSummary):
        return _backup_summary_wire(value)
    if isinstance(value, BackupReviewDispositionResult):
        return {
            "receipt_id": value.receipt_id,
            "review_id": value.review_id,
            "revision": value.revision,
            "state": value.state,
            "correction_record_id": value.correction_record_id,
            "replayed": value.replayed,
        }
    if isinstance(value, ReevaluationDetail):
        return _reevaluation_summary_wire(value) | {
            "question": value.question,
            "answer_text": value.answer_text,
            "feedback_verdict": value.feedback_verdict,
            "feedback_comment": value.feedback_comment,
            "answering_card_id": value.answering_card_id,
            "answering_card_revision": value.answering_card_revision,
            "owner_user_id": value.owner_user_id,
            "flagged_at": _timestamp(value.flagged_at),
        }
    if isinstance(value, ReevaluationSummary):
        return _reevaluation_summary_wire(value)
    if isinstance(value, ReevaluationDispositionResult):
        return {
            "receipt_id": value.receipt_id,
            "reevaluation_id": value.reevaluation_id,
            "revision": value.revision,
            "state": value.state,
            "reanswer_requested_id": value.reanswer_requested_id,
            "replayed": value.replayed,
        }
    if isinstance(value, ApprovalItemDetail):
        return _approval_summary_wire(value) | {
            "question": value.question,
            "candidate_text": value.candidate_text,
            "candidate_digest": value.candidate_digest,
            "policy_digest": value.policy_digest,
            "binding_version": value.binding_version,
            "assigned_approver_user_id": value.assigned_approver_user_id,
            "assigned_approval_card_id": value.assigned_approval_card_id,
        }
    if isinstance(value, ApprovalItemSummary):
        return _approval_summary_wire(value)
    if isinstance(value, ApprovalDispositionInboxResult):
        return {
            "receipt_id": value.receipt_id,
            "approval_item_id": value.approval_item_id,
            "approval_item_revision": value.approval_item_revision,
            "request_id": value.request_id,
            "request_revision": value.request_revision,
            "state": value.state,
            "replayed": value.replayed,
        }
    if isinstance(value, ApprovalReassignmentResult):
        return {
            "receipt_id": value.receipt_id,
            "superseded_approval_item_id": value.superseded_approval_item_id,
            "successor_approval_item_id": value.successor_approval_item_id,
            "successor_approval_item_revision": value.successor_approval_item_revision,
            "request_id": value.request_id,
            "request_revision": value.request_revision,
            "state": value.state,
            "replayed": value.replayed,
        }
    raise TypeError("unsupported inbox projection")


def _conflict_summary_wire(
    value: ConflictCaseSummary | ConflictCaseDetail,
) -> dict[str, object]:
    return {
        "case_id": value.case_id,
        "request_id": value.request_id,
        "request_revision": value.request_revision,
        "state": value.state,
        "round": value.round,
        "revision": value.revision,
        "candidate_card_ids": list(value.candidate_card_ids),
        "opened_at": _timestamp(value.opened_at),
    }


def _backup_summary_wire(value: BackupReviewSummary) -> dict[str, object]:
    return {
        "review_id": value.review_id,
        "request_id": value.request_id,
        "source_answer_record_id": value.source_answer_record_id,
        "revision": value.revision,
        "state": value.state,
        "created_at": _timestamp(value.created_at),
    }


def _reevaluation_summary_wire(value: ReevaluationSummary) -> dict[str, object]:
    return {
        "reevaluation_id": value.reevaluation_id,
        "request_id": value.request_id,
        "feedback_id": value.feedback_id,
        "source_answer_record_id": value.source_answer_record_id,
        "revision": value.revision,
        "state": value.state,
        "created_at": _timestamp(value.created_at),
    }


def _approval_summary_wire(value: ApprovalItemSummary) -> dict[str, object]:
    return {
        "approval_item_id": value.approval_item_id,
        "request_id": value.request_id,
        "request_revision": value.request_revision,
        "approval_round": value.approval_round,
        "revision": value.revision,
        "assigned_at": _timestamp(value.assigned_at),
        "due_at": _timestamp(value.due_at),
        "state": value.state,
    }


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _success(payload: dict[str, object]) -> Response:
    try:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(body) > _MAX_RESPONSE_BYTES:
            return _inbox_error(503, "unavailable")
        return Response(
            body,
            status_code=200,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        return _inbox_error(503, "unavailable")


def _inbox_error(status: int, code: str) -> JSONResponse:
    return JSONResponse(
        {"error": code},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def _idempotency_key(request: Request) -> str | None:
    value = request.headers.get("idempotency-key")
    return (
        value
        if type(value) is str and _IDEMPOTENCY_KEY.fullmatch(value) is not None
        else None
    )


def _reference(value: object) -> bool:
    return type(value) is str and _REFERENCE.fullmatch(value) is not None


def _text(value: object, *, empty: bool) -> bool:
    return type(value) is str and (empty or value != "") and _utf8(value)


def _utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _nonnegative(value: object) -> bool:
    return type(value) is int and value >= 0


__all__ = ["create_central_inbox_router"]
