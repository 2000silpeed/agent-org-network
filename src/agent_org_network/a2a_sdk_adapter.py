"""ADR 0074 strict A2A 1.0 REST SDK adapter.

The official SDK owns the A2A protobuf codec and REST client.  This module owns
the local, fail-closed boundary around it: fixed card discovery, digest and
interface pinning, OOB bearer credentials, and the narrow core invocation
outcome. It deliberately does not make a production network-pinning claim:
``httpx`` does not expose an address-pinning/SNI transport, so the public
constructor is unavailable. The only successful seam is an exact
``httpx.MockTransport`` used by deterministic component tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import hashlib
import ipaddress
import json
import socket
from typing import Any, Protocol, Self, cast, final
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from a2a.client import ClientCallContext, ClientConfig, ClientFactory
from a2a.types import AgentCard, AgentInterface, Message, Role, SendMessageRequest, TaskState
from google.protobuf.json_format import ParseDict, ParseError

from agent_org_network.a2a_remote_runtime import (
    A2ACompletedText,
    A2AInvocationOutcome,
    A2AInvocationPort,
    A2AProtocolViolation,
    A2ARemoteRejected,
    A2ARemoteRuntimeProfile,
    A2ARemoteUnavailable,
)


_HTTP_JSON = "HTTP+JSON"
_A2A_VERSION = "1.0"
_A2A_VERSION_HEADER = "A2A-Version"
_CARD_PATH = "/.well-known/agent-card.json"
_MAX_REQUEST_BYTES = 1_048_576


class _RemoteRejected(Exception):
    """Internal marker; never rendered outside this module."""


class _RemoteUnavailable(Exception):
    """Internal marker for a bounded 5xx before SDK error parsing."""


def _is_json_content_type(value: str | None) -> bool:
    """Accept only the HTTP+JSON media type (optional parameters allowed)."""
    if value is None:
        return False
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type == "application/json"


class DnsResolver(Protocol):
    """Deterministic seam for the connect-immediately-before DNS policy."""

    def resolve(self, hostname: str) -> Sequence[str]: ...


@final
class SystemDnsResolver:
    """System resolver used only by the owner-local composition root."""

    def resolve(self, hostname: str) -> Sequence[str]:
        return tuple(
            sorted(
                {
                    cast(str, result[4][0])
                    for result in socket.getaddrinfo(
                        hostname,
                        443,
                        type=socket.SOCK_STREAM,
                    )
                }
            )
        )


@final
class _BoundedMockTransport(httpx.AsyncBaseTransport):
    """Test-only wire cap around an exact ``httpx.MockTransport``.

    The SDK REST client otherwise buffers the complete non-stream response.
    This wrapper consumes and caps POST response bytes before the SDK sees
    them. It is component evidence only, not a network transport.
    """

    def __init__(self, *, inner: httpx.MockTransport, max_response_bytes: int) -> None:
        self._inner = inner
        self._max_response_bytes = max_response_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if request.method != "POST":
            return response
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise ValueError("redirect rejected")
        if 400 <= response.status_code < 500:
            await response.aclose()
            raise _RemoteRejected
        if 500 <= response.status_code < 600:
            await response.aclose()
            raise _RemoteUnavailable
        if not _is_json_content_type(response.headers.get("content-type")):
            await response.aclose()
            raise ValueError("application/json response required")
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self._max_response_bytes:
                    raise ValueError("response body too large")
            except ValueError as error:
                await response.aclose()
                raise ValueError("invalid or oversized response body") from error
        try:
            try:
                body = response.content
            except httpx.ResponseNotRead:
                body = await response.aread()
            if len(body) > self._max_response_bytes:
                raise ValueError("response body too large")
        finally:
            await response.aclose()
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=body,
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


@final
class OobBearerCredential:
    """An OOB secret whose repr/str never reveal its raw value."""

    __slots__ = ("_secret",)

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("non-empty OOB bearer credential required")
        self._secret = secret

    def authorization_header_for(self, pinned_origin: str) -> tuple[str, str]:
        # The origin argument intentionally makes accidental global header use
        # visible at this boundary.  Only this adapter calls the private method.
        if not pinned_origin.startswith("https://"):
            raise ValueError("pinned HTTPS origin required")
        return ("Authorization", f"Bearer {self._secret}")

    def __repr__(self) -> str:
        return "OobBearerCredential(<redacted>)"

    __str__ = __repr__


class OobBearerCredentialProvider(Protocol):
    """Owner-local keychain/provider port; the profile contains only its ref."""

    def resolve_bearer(self, credential_ref: str) -> OobBearerCredential | None: ...


@final
class A2ASdkInvocationAdapter(A2AInvocationPort):
    """Official A2A SDK adapter for the deliberately narrow v1 REST profile."""

    def __init__(
        self,
        *,
        credentials: OobBearerCredentialProvider,
        resolver: DnsResolver | None = None,
    ) -> None:
        self._credentials = credentials
        self._resolver = resolver or SystemDnsResolver()
        self._test_transport: httpx.MockTransport | None = None

    @classmethod
    def for_test(
        cls,
        *,
        credentials: OobBearerCredentialProvider,
        resolver: DnsResolver,
        transport: httpx.MockTransport,
    ) -> Self:
        """Create deterministic component evidence with no network egress."""
        if type(transport) is not httpx.MockTransport:
            raise TypeError("test transport must be exact httpx.MockTransport")
        adapter = cls(credentials=credentials, resolver=resolver)
        adapter._test_transport = transport
        return adapter

    def invoke(
        self,
        *,
        profile: A2ARemoteRuntimeProfile,
        question: str,
        context: str | None,
    ) -> A2AInvocationOutcome:
        """Bridge the SDK async client without running a coroutine on its own loop.

        The port is deliberately fail-closed when called from an already-running
        loop: a synchronous wait there would block the caller and a background
        call could outlive its request scope.  Normal synchronous calls create a
        fresh secured HTTP client per invocation, so SDK connections never cross
        separate ``asyncio.run`` event loops.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._invoke_async(profile=profile, question=question, context=context))
        return A2ARemoteUnavailable()

    async def _invoke_async(
        self,
        *,
        profile: A2ARemoteRuntimeProfile,
        question: str,
        context: str | None,
    ) -> A2AInvocationOutcome:
        # ``httpx`` resolves again at connect time and cannot bind that connection
        # to the resolver result while retaining normal TLS/SNI semantics.  Do not
        # silently make a production egress claim with its default transport.
        # Only the explicit exact-MockTransport test seam may proceed until a
        # real address-pinning/SNI transport is implemented.
        if self._test_transport is None:
            return A2ARemoteUnavailable()
        try:
            payload = _question_payload(question, context)
        except ValueError:
            return A2AProtocolViolation()

        try:
            async with self._new_http_client(profile) as http_client:
                self._require_public_resolution(profile.service_endpoint)
                card_json = await self._fetch_card(http_client, profile)
                if card_json is None:
                    return A2ARemoteUnavailable()
                canonical = _canonical_json(card_json)
                if hashlib.sha256(canonical).hexdigest() != profile.remote_card_sha256:
                    return A2AProtocolViolation()
                card = ParseDict(card_json, AgentCard(), ignore_unknown_fields=False)
                selected = _selected_interface(card, profile)
                if (
                    selected is None
                    or not _is_exact_bearer_card(card)
                    or not _supports_text_plain(card)
                ):
                    return A2AProtocolViolation()
                credential = self._credentials.resolve_bearer(profile.credential_ref)
                if credential is None:
                    return A2ARemoteUnavailable()
                self._require_public_resolution(profile.service_endpoint)
                return await self._send_completed_text(
                    http_client=http_client,
                    profile=profile,
                    card=_card_with_only_selected_interface(card, selected),
                    payload=payload,
                    credential=credential,
                )
        except _RemoteRejected:
            return A2ARemoteRejected()
        except _RemoteUnavailable:
            return A2ARemoteUnavailable()
        except (httpx.HTTPError, OSError, socket.gaierror):
            return A2ARemoteUnavailable()
        except (TypeError, ValueError, ParseError):
            return A2AProtocolViolation()
        except Exception:
            # SDK schema/transport exceptions are intentionally redacted at this
            # port.  No endpoint, task identifier, card data, or secret escapes.
            return A2ARemoteUnavailable()

    def _new_http_client(self, profile: A2ARemoteRuntimeProfile) -> httpx.AsyncClient:
        if self._test_transport is None:
            raise RuntimeError("production pinned transport is unavailable")
        return httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            verify=True,
            timeout=httpx.Timeout(float(profile.timeout_seconds)),
            headers={_A2A_VERSION_HEADER: _A2A_VERSION},
            transport=_BoundedMockTransport(
                inner=self._test_transport,
                max_response_bytes=profile.max_response_bytes,
            ),
        )

    async def _fetch_card(
        self, http_client: httpx.AsyncClient, profile: A2ARemoteRuntimeProfile
    ) -> dict[str, object] | None:
        card_url = f"{_origin(profile.service_endpoint)}{_CARD_PATH}"
        request = http_client.build_request("GET", card_url)
        response = await http_client.send(request, stream=True)
        try:
            if response.is_redirect:
                raise ValueError("redirect rejected")
            if 300 <= response.status_code < 400:
                raise ValueError("redirect rejected")
            if 400 <= response.status_code < 500:
                raise _RemoteRejected
            response.raise_for_status()
            if not _is_json_content_type(response.headers.get("content-type")):
                raise ValueError("application/json response required")
            declared_size = response.headers.get("content-length")
            if declared_size is not None and int(declared_size) > profile.max_response_bytes:
                raise ValueError("card body too large")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > profile.max_response_bytes:
                    raise ValueError("card body too large")
            decoded: Any = json.loads(bytes(body))
            return cast(dict[str, object], decoded) if isinstance(decoded, dict) else None
        except _RemoteRejected:
            raise
        finally:
            await response.aclose()

    async def _send_completed_text(
        self,
        *,
        http_client: httpx.AsyncClient,
        profile: A2ARemoteRuntimeProfile,
        card: AgentCard,
        payload: str,
        credential: OobBearerCredential,
    ) -> A2AInvocationOutcome:
        message = Message(message_id=str(uuid4()), role=Role.ROLE_USER)
        message.parts.add(text=payload)
        request = SendMessageRequest(message=message)
        request.configuration.accepted_output_modes.append("text/plain")
        request.configuration.return_immediately = False
        factory = ClientFactory(
            ClientConfig(
                streaming=False,
                polling=False,
                httpx_client=http_client,
                supported_protocol_bindings=[_HTTP_JSON],
                use_client_preference=True,
            )
        )
        client = factory.create(card)
        origin = _origin(card.supported_interfaces[0].url)
        header_name, header_value = credential.authorization_header_for(origin)
        try:
            events = client.send_message(
                request,
                context=ClientCallContext(service_parameters={header_name: header_value}),
            )
            received = [event async for event in events]
        except httpx.HTTPStatusError as error:
            return A2ARemoteRejected() if 400 <= error.response.status_code < 500 else A2ARemoteUnavailable()
        finally:
            await client.close()
        if len(received) != 1:
            return A2AProtocolViolation()
        response = received[0]
        if response.HasField("message"):
            return _completed_text_from_message(response.message, profile.max_response_bytes)
        if not response.HasField("task"):
            return A2AProtocolViolation()
        task = response.task
        if task.status.state in (
            TaskState.TASK_STATE_REJECTED,
            TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_CANCELED,
        ):
            return A2ARemoteRejected()
        if task.status.state == TaskState.TASK_STATE_AUTH_REQUIRED:
            return A2ARemoteUnavailable()
        if (
            task.status.state != TaskState.TASK_STATE_COMPLETED
            or len(task.artifacts) != 1
        ):
            return A2AProtocolViolation()
        artifact = task.artifacts[0]
        if artifact.extensions:
            return A2AProtocolViolation()
        return _completed_text_from_parts(artifact.parts, profile.max_response_bytes)

    def _require_public_resolution(self, endpoint: str) -> None:
        hostname = urlsplit(endpoint).hostname
        if hostname is None:
            raise ValueError("missing hostname")
        addresses = self._resolver.resolve(hostname)
        if not addresses:
            raise OSError("no address")
        for rendered in addresses:
            address = ipaddress.ip_address(rendered)
            if not address.is_global:
                raise OSError("non-public address")

def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None or parsed.query or parsed.fragment:
        raise ValueError("exact HTTPS endpoint required")
    port = "" if parsed.port in (None, 443) else f":{parsed.port}"
    rendered_host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"https://{rendered_host}{port}"


def _selected_interface(card: AgentCard, profile: A2ARemoteRuntimeProfile) -> AgentInterface | None:
    selected = [
        interface
        for interface in card.supported_interfaces
        if interface.url == profile.service_endpoint
        and interface.protocol_binding == profile.protocol_binding
        and interface.protocol_version == profile.protocol_version
        and not interface.tenant
    ]
    return selected[0] if len(selected) == 1 else None


def _card_with_only_selected_interface(card: AgentCard, selected: AgentInterface) -> AgentCard:
    safe = AgentCard()
    safe.CopyFrom(card)
    del safe.supported_interfaces[:]
    sdk_interface = safe.supported_interfaces.add()
    sdk_interface.CopyFrom(selected)
    return safe


def _is_exact_bearer_card(card: AgentCard) -> bool:
    if len(card.security_requirements) != 1:
        return False
    requirement = card.security_requirements[0]
    if len(requirement.schemes) != 1:
        return False
    scheme_name = next(iter(requirement.schemes))
    if scheme_name not in card.security_schemes:
        return False
    scheme = card.security_schemes[scheme_name]
    return (
        scheme.HasField("http_auth_security_scheme")
        and scheme.http_auth_security_scheme.scheme.lower() == "bearer"
    )


def _supports_text_plain(card: AgentCard) -> bool:
    return (
        "text/plain" in card.default_input_modes
        and "text/plain" in card.default_output_modes
    )


def _question_payload(question: str, context: str | None) -> str:
    if not question:
        raise ValueError("question required")
    payload = question if context is None else f"{question}\n\n{context}"
    if len(payload.encode("utf-8")) > _MAX_REQUEST_BYTES:
        raise ValueError("request body too large")
    return payload


def _completed_text_from_parts(parts: Any, max_response_bytes: int) -> A2AInvocationOutcome:
    texts: list[str] = []
    for part in parts:
        if (
            part.WhichOneof("content") != "text"
            or not part.text.strip()
            or part.filename
            or part.media_type not in ("", "text/plain")
        ):
            return A2AProtocolViolation()
        texts.append(part.text)
    text = "".join(texts)
    if not text or len(text.encode("utf-8")) > max_response_bytes:
        return A2AProtocolViolation()
    return A2ACompletedText(text=text)


def _completed_text_from_message(message: Message, max_response_bytes: int) -> A2AInvocationOutcome:
    if (
        message.role != Role.ROLE_AGENT
        or not message.context_id.strip()
        or message.task_id
        or message.extensions
        or message.reference_task_ids
    ):
        return A2AProtocolViolation()
    return _completed_text_from_parts(message.parts, max_response_bytes)
