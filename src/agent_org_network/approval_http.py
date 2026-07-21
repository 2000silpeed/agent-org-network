"""P17.6b Approval 처리함의 얇은 HTTP 어댑터.

승인자 신원은 주입된 서버측 resolver에서만 얻는다. 요청 본문은 처분 intent와 새
승인자 식별자만 받으며 조직·행위자·principal을 받지 않는다. 도메인 오류 문자열은
반사하지 않고 고정된 field-free 오류 계약으로 바꾼다.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, TypeAlias, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from agent_org_network.approval import ApprovalPendingSummary, ApproverPrincipal
from agent_org_network.approval_operations import (
    ApprovalAnswered,
    ApprovalDecisionIntent,
    ApprovalDeclined,
    ApprovalOperationsApplication,
    ApprovalOperationsAuthorizationUnavailable,
    ApprovalOperationsConflict,
    ApprovalOperationsDecision,
    ApprovalOperationsDependency,
    ApprovalOperationsError,
    ApprovalOperationsIntegrityError,
    ApprovalOperationsInvalid,
    ApprovalOperationsNotFoundOrDenied,
    ApprovalPendingDetail,
    ApprovalOperationsPrincipal,
    ApprovalReassigned,
    ManualApprovalReassignmentTarget,
)
from agent_org_network.central_authority import AuthenticatedPrincipal


class ApproverNotAuthenticatedError(RuntimeError):
    """서버측 승인자 신원이 없다는 principal resolver의 공개 신호."""


class _ApproverPrincipalUnavailableError(RuntimeError):
    """resolver 실패나 잘못된 principal shape를 외부 세부정보 없이 감싼다."""


ApproverPrincipalResolver: TypeAlias = Callable[[Request], object]

_MAX_PRINCIPAL_FIELD_LENGTH = 512
_RETRY_AFTER_SECONDS = "1"
_APPROVER_PRINCIPAL_STATE = "approval_approver_principal"


def _has_unsafe_principal_character(value: str) -> bool:
    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _resolve_approver_principal(
    resolver: ApproverPrincipalResolver,
    request: Request,
) -> ApprovalOperationsPrincipal:
    try:
        raw = resolver(request)
    except ApproverNotAuthenticatedError:
        raise
    except Exception as error:
        raise _ApproverPrincipalUnavailableError from error
    if type(raw) not in (ApproverPrincipal, AuthenticatedPrincipal):
        raise _ApproverPrincipalUnavailableError
    assert isinstance(raw, (ApproverPrincipal, AuthenticatedPrincipal))
    try:
        principal = type(raw).model_validate(
            raw.model_dump(mode="python", round_trip=True),
            strict=True,
        )
    except Exception as error:
        raise _ApproverPrincipalUnavailableError from error
    if any(
        len(value) > _MAX_PRINCIPAL_FIELD_LENGTH or _has_unsafe_principal_character(value)
        for value in (
            principal.org_id,
            principal.subject_id,
            *(
                (principal.identity_provider, principal.identity_session_id)
                if type(principal) is AuthenticatedPrincipal
                else ()
            ),
        )
    ):
        raise _ApproverPrincipalUnavailableError
    return principal


def _typed_http_error(
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "retryable": retryable,
        },
        headers={"Retry-After": _RETRY_AFTER_SECONDS} if retryable else None,
    )


def approval_operations_http_error(error: ApprovalOperationsError) -> HTTPException:
    """닫힌 Approval Operations 오류를 고정 HTTP 의미로 바꾼다."""
    if isinstance(error, ApprovalOperationsInvalid):
        return _typed_http_error(
            status_code=400,
            code="approval_invalid",
            message="승인 처분 요청이 유효하지 않습니다.",
            retryable=False,
        )
    if isinstance(error, ApprovalOperationsNotFoundOrDenied):
        return _typed_http_error(
            status_code=404,
            code="approval_not_found_or_denied",
            message="승인 항목을 찾을 수 없습니다.",
            retryable=False,
        )
    if isinstance(error, ApprovalOperationsConflict):
        return _typed_http_error(
            status_code=409,
            code="approval_conflict",
            message="승인 항목에 다른 처분이 이미 적용되었습니다.",
            retryable=False,
        )
    if isinstance(error, ApprovalOperationsAuthorizationUnavailable):
        return _typed_http_error(
            status_code=503,
            code="approval_authorization_unavailable",
            message="승인 권한을 일시적으로 확인할 수 없습니다.",
            retryable=True,
        )
    if isinstance(error, ApprovalOperationsDependency):
        return _typed_http_error(
            status_code=503,
            code="approval_dependency",
            message="승인 처리 의존성을 일시적으로 확인할 수 없습니다.",
            retryable=True,
        )
    if isinstance(error, ApprovalOperationsIntegrityError):
        return _typed_http_error(
            status_code=500,
            code="approval_integrity",
            message="승인 처리의 저장 증거가 일치하지 않습니다.",
            retryable=False,
        )
    return _typed_http_error(
        status_code=500,
        code="approval_error",
        message="승인 처리를 완료하지 못했습니다.",
        retryable=False,
    )


def _principal_http_error(error: Exception) -> HTTPException:
    if isinstance(error, ApproverNotAuthenticatedError):
        return _typed_http_error(
            status_code=401,
            code="approval_not_authenticated",
            message="승인 처리함에 로그인해야 합니다.",
            retryable=False,
        )
    return _typed_http_error(
        status_code=503,
        code="approval_principal_dependency",
        message="승인자 신원을 일시적으로 확인할 수 없습니다.",
        retryable=True,
    )


def _approval_body_http_error() -> HTTPException:
    return _typed_http_error(
        status_code=422,
        code="approval_body_invalid",
        message="승인 요청 본문이 유효하지 않습니다.",
        retryable=False,
    )


def _canonical_model_dump(value: BaseModel, expected_type: type[BaseModel]) -> dict[str, object]:
    if type(value) is not expected_type:
        raise ApprovalOperationsIntegrityError
    try:
        canonical = expected_type.model_validate(
            value.model_dump(mode="python", round_trip=True),
            strict=True,
        )
        return cast(dict[str, object], canonical.model_dump(mode="json"))
    except ApprovalOperationsIntegrityError:
        raise
    except Exception as error:
        raise ApprovalOperationsIntegrityError from error


def _decision_dump(value: ApprovalOperationsDecision) -> dict[str, object]:
    if type(value) not in (ApprovalAnswered, ApprovalDeclined):
        raise ApprovalOperationsIntegrityError
    return _canonical_model_dump(value, type(value))


def _operation_response(call: Callable[[], BaseModel]) -> JSONResponse:
    try:
        value = call()
        if type(value) is ApprovalPendingDetail:
            content = _canonical_model_dump(value, ApprovalPendingDetail)
        elif type(value) is ApprovalReassigned:
            content = _canonical_model_dump(value, ApprovalReassigned)
        else:
            content = _decision_dump(cast(ApprovalOperationsDecision, value))
        return JSONResponse(status_code=200, content=content)
    except ApprovalOperationsError as error:
        raise approval_operations_http_error(error) from error
    except Exception as error:
        integrity = ApprovalOperationsIntegrityError()
        raise approval_operations_http_error(integrity) from error


def _principal_or_http_error(
    resolver: ApproverPrincipalResolver,
    request: Request,
) -> ApprovalOperationsPrincipal:
    try:
        return _resolve_approver_principal(resolver, request)
    except (ApproverNotAuthenticatedError, _ApproverPrincipalUnavailableError) as error:
        raise _principal_http_error(error) from error


def create_approval_router(
    *,
    application: ApprovalOperationsApplication,
    principal_resolver: ApproverPrincipalResolver,
) -> APIRouter:
    """한 Approval Operations application에 결박된 처리함 라우터를 만든다."""
    if not callable(principal_resolver):
        raise ValueError("Approval principal_resolver는 callable이어야 합니다.")

    class PrincipalFirstApprovalRoute(APIRoute):
        """FastAPI가 body를 읽기 전에 서버측 principal을 확정한다."""

        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            route_handler = super().get_route_handler()

            async def principal_first_handler(request: Request) -> Response:
                principal = _principal_or_http_error(principal_resolver, request)
                setattr(request.state, _APPROVER_PRINCIPAL_STATE, principal)
                try:
                    return await route_handler(request)
                except RequestValidationError as error:
                    raise _approval_body_http_error() from error

            return principal_first_handler

    router = APIRouter(route_class=PrincipalFirstApprovalRoute)

    def resolve_principal(request: Request) -> ApprovalOperationsPrincipal:
        """route-level 경계가 확정한 principal만 handler에 주입한다."""
        principal = getattr(request.state, _APPROVER_PRINCIPAL_STATE, None)
        if type(principal) not in (ApproverPrincipal, AuthenticatedPrincipal):
            raise _principal_http_error(_ApproverPrincipalUnavailableError())
        assert isinstance(principal, (ApproverPrincipal, AuthenticatedPrincipal))
        return principal

    @router.get("/inbox/approvals", response_model=None)
    def pending_approvals(  # pyright: ignore[reportUnusedFunction]
        principal: ApprovalOperationsPrincipal = Depends(resolve_principal),
    ) -> Response:
        try:
            pending = application.pending_for(principal)
            if type(pending) is not list:
                raise ApprovalOperationsIntegrityError
            content = [
                _canonical_model_dump(summary, ApprovalPendingSummary) for summary in pending
            ]
            return JSONResponse(status_code=200, content=content)
        except ApprovalOperationsError as error:
            raise approval_operations_http_error(error) from error
        except Exception as error:
            integrity = ApprovalOperationsIntegrityError()
            raise approval_operations_http_error(integrity) from error

    @router.get("/inbox/approvals/{item_id}", response_model=None)
    def approval_detail(  # pyright: ignore[reportUnusedFunction]
        item_id: str,
        principal: ApprovalOperationsPrincipal = Depends(resolve_principal),
    ) -> Response:
        return _operation_response(lambda: application.detail(item_id, principal))

    @router.post("/inbox/approvals/{item_id}/decide", response_model=None)
    def decide_approval(  # pyright: ignore[reportUnusedFunction]
        item_id: str,
        intent: ApprovalDecisionIntent,
        principal: ApprovalOperationsPrincipal = Depends(resolve_principal),
    ) -> Response:
        return _operation_response(lambda: application.decide(item_id, principal, intent))

    @router.post("/inbox/approvals/{item_id}/reassign", response_model=None)
    def reassign_approval(  # pyright: ignore[reportUnusedFunction]
        item_id: str,
        target: ManualApprovalReassignmentTarget,
        principal: ApprovalOperationsPrincipal = Depends(resolve_principal),
    ) -> Response:
        return _operation_response(lambda: application.reassign(item_id, principal, target))

    return router


__all__ = [
    "ApproverNotAuthenticatedError",
    "ApproverPrincipalResolver",
    "approval_operations_http_error",
    "create_approval_router",
]
