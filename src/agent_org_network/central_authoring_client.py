"""Real TLS HTTP client used by the Card Owner installation."""

from __future__ import annotations

import json
import ipaddress
from http.client import HTTPMessage
from typing import IO
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict

from agent_org_network.production_authoring_identity import AuthoringInvocation

from agent_org_network.sqlite_production_authoring_runs import (
    CompleteAuthoringRunCommand,
    CompleteAuthoringRunResult,
    BeginAuthoringRunPublishCommand,
    BeginAuthoringRunPublishResult,
    ReviewAuthoringRunCommand,
    ReviewAuthoringRunResult,
    StartAuthoringRunCommand,
    StartAuthoringRunResult,
)


class CentralAuthoringHttpUnavailable(Exception):
    pass


class _HttpHeaders(Protocol):
    def get(self, name: str, default: str | None = None) -> str | None: ...


class _HttpResponse(Protocol):
    status: int

    @property
    def headers(self) -> _HttpHeaders: ...

    def read(self, amount: int = -1) -> bytes: ...
    def geturl(self) -> str: ...


class _HttpOpener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> _HttpResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


_REAL_OPENER = build_opener(_NoRedirect())


def _open_url(request: Request, *, timeout: float) -> _HttpResponse:
    return _REAL_OPENER.open(request, timeout=timeout)


class _StartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run: dict[str, object]
    replayed: bool


class _CompleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run: dict[str, object]
    replayed: bool


class _ReviewResponse(_CompleteResponse):
    pass


class _PublishResponse(_CompleteResponse):
    pass


class ProductionCentralAuthoringClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 15.0,
        opener: _HttpOpener = _open_url,
    ) -> None:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname or ""
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname.lower() == "localhost"
        if (
            parsed.scheme != "https"
            or not hostname
            or loopback
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or timeout_seconds <= 0
        ):
            raise CentralAuthoringHttpUnavailable()
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._open = opener

    def _post(
        self, path: str, key: str, body: object, invocation: AuthoringInvocation
    ) -> object:
        if type(invocation) is not AuthoringInvocation:
            raise CentralAuthoringHttpUnavailable()
        session = invocation.session.value.get_secret_value()
        request = Request(
            self._base + path,
            data=json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
            method="POST",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "idempotency-key": key,
                "cookie": f"aon_identity_session={session}",
            },
        )
        try:
            response = self._open(request, timeout=self._timeout)
            content_type = response.headers.get("content-type")
            if (
                response.status != 200
                or response.geturl() != self._base + path
                or content_type != "application/json"
            ):
                raise CentralAuthoringHttpUnavailable()
            payload = response.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise CentralAuthoringHttpUnavailable()
            return json.loads(payload)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            raise CentralAuthoringHttpUnavailable() from error

    def start(
        self, command: StartAuthoringRunCommand, *, invocation: AuthoringInvocation
    ) -> StartAuthoringRunResult:
        try:
            if (
                invocation.org_id != command.org_id
                or invocation.principal_id != command.principal_id
            ):
                raise CentralAuthoringHttpUnavailable()
            raw = self._post(
                "/authoring/runs/start",
                command.idempotency_key,
                {
                    "agent_id": command.agent_id,
                    "expected_card_revision": command.expected_card_revision,
                    "expected_card_digest": command.expected_card_digest,
                    "sources": [
                        source.model_dump(mode="json") for source in command.sources
                    ],
                },
                invocation,
            )
            response = _StartResponse.model_validate(raw)
            return StartAuthoringRunResult(
                run=response.run, replayed=response.replayed  # type: ignore[arg-type]
            )
        except CentralAuthoringHttpUnavailable:
            raise
        except Exception as error:
            raise CentralAuthoringHttpUnavailable() from error

    def complete(
        self,
        command: CompleteAuthoringRunCommand,
        *,
        invocation: AuthoringInvocation,
    ) -> CompleteAuthoringRunResult:
        try:
            if (
                invocation.org_id != command.organization_id
                or invocation.principal_id != command.principal_id
            ):
                raise CentralAuthoringHttpUnavailable()
            body = command.model_dump(mode="json")
            body.pop("organization_id")
            body.pop("principal_id")
            body.pop("idempotency_key")
            raw = self._post(
                "/authoring/runs/complete", command.idempotency_key, body, invocation
            )
            response = _CompleteResponse.model_validate(raw)
            return CompleteAuthoringRunResult(
                run=response.run, replayed=response.replayed  # type: ignore[arg-type]
            )
        except CentralAuthoringHttpUnavailable:
            raise
        except Exception as error:
            raise CentralAuthoringHttpUnavailable() from error

    def review(
        self, command: ReviewAuthoringRunCommand, *, invocation: AuthoringInvocation
    ) -> ReviewAuthoringRunResult:
        try:
            if invocation.org_id != command.organization_id or invocation.principal_id != command.principal_id:
                raise CentralAuthoringHttpUnavailable()
            body = command.model_dump(mode="json")
            for key in ("organization_id", "principal_id", "idempotency_key"):
                body.pop(key)
            response = _ReviewResponse.model_validate(self._post("/authoring/runs/review", command.idempotency_key, body, invocation))
            return ReviewAuthoringRunResult(run=response.run, replayed=response.replayed)  # type: ignore[arg-type]
        except CentralAuthoringHttpUnavailable:
            raise
        except Exception as error:
            raise CentralAuthoringHttpUnavailable() from error

    def begin_publish(
        self, command: BeginAuthoringRunPublishCommand, *, invocation: AuthoringInvocation
    ) -> BeginAuthoringRunPublishResult:
        try:
            if invocation.org_id != command.organization_id or invocation.principal_id != command.principal_id:
                raise CentralAuthoringHttpUnavailable()
            body = command.model_dump(mode="json")
            for key in ("organization_id", "principal_id", "idempotency_key"):
                body.pop(key)
            response = _PublishResponse.model_validate(self._post("/authoring/runs/publish-begin", command.idempotency_key, body, invocation))
            return BeginAuthoringRunPublishResult(run=response.run, replayed=response.replayed)  # type: ignore[arg-type]
        except CentralAuthoringHttpUnavailable:
            raise
        except Exception as error:
            raise CentralAuthoringHttpUnavailable() from error


CentralAuthoringHttpClient = ProductionCentralAuthoringClient


__all__ = [
    "CentralAuthoringHttpClient",
    "CentralAuthoringHttpUnavailable",
    "ProductionCentralAuthoringClient",
]
