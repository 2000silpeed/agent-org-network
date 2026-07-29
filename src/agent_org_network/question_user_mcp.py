"""Thin production Question User MCP (ADR 0070).

This module deliberately does *not* import :mod:`mcp_server`: that module is a
central/test fixture with broader surfaces.  The installed client only knows two
question operations and the gateway derives identity and authority on every call.
"""

from __future__ import annotations

import base64
import argparse
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import webbrowser
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, SecretStr


QUESTION_USER_TOOL_MANIFEST: frozenset[str] = frozenset({"ask_org", "get_question"})
_MAX_BODY = 16 * 1024


class QuestionUserMcpUnavailable(RuntimeError):
    """Fail-closed error; neither secrets nor identity hints are reflected."""


def _https(value: str) -> str:
    parsed = urlsplit(value)
    if (
        type(value) is not str or parsed.scheme != "https" or not parsed.netloc
        or parsed.username is not None or parsed.password is not None
        or parsed.fragment or any(ord(char) < 32 for char in value)
    ):
        raise QuestionUserMcpUnavailable()
    return value


class QuestionMcpProfile(BaseModel, frozen=True):
    """Non-secret client profile.  It contains an opaque keychain reference only."""

    model_config = ConfigDict(extra="forbid", strict=True)
    gateway_url: str
    authorization_url: str
    token_url: str
    client_id: str
    credential_ref: str | None = None

    def model_post_init(self, __context: object) -> None:
        if (
            _https(self.gateway_url) != self.gateway_url
            or _https(self.authorization_url) != self.authorization_url
            or _https(self.token_url) != self.token_url
            or not self.client_id.strip()
            or (self.credential_ref is not None and not self.credential_ref.strip())
        ):
            raise QuestionUserMcpUnavailable()


class OpaqueCredentialStore(Protocol):
    def put(self, token: SecretStr) -> str: ...
    def get(self, reference: str) -> SecretStr: ...
    def delete(self, reference: str) -> None: ...


class KeyringOpaqueCredentialStore:
    """The only production credential persistence seam; refs contain no token data."""

    _SERVICE = "agent-org-network.question-user-mcp.v1"

    def __init__(self) -> None:
        try:
            import keyring
            self._keyring = keyring
        except Exception as error:
            raise QuestionUserMcpUnavailable() from error

    def put(self, token: SecretStr) -> str:
        if type(token) is not SecretStr or not token.get_secret_value():
            raise QuestionUserMcpUnavailable()
        reference = secrets.token_urlsafe(32)
        try:
            self._keyring.set_password(self._SERVICE, reference, token.get_secret_value())
        except Exception as error:
            raise QuestionUserMcpUnavailable() from error
        return reference

    def get(self, reference: str) -> SecretStr:
        try:
            token = self._keyring.get_password(self._SERVICE, reference)
        except Exception as error:
            raise QuestionUserMcpUnavailable() from error
        if not token:
            raise QuestionUserMcpUnavailable()
        return SecretStr(token)

    def delete(self, reference: str) -> None:
        try:
            self._keyring.delete_password(self._SERVICE, reference)
        except Exception as error:
            raise QuestionUserMcpUnavailable() from error


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@dataclass(frozen=True)
class PkcePairingStart:
    authorization_url: str
    redirect_uri: str
    state: str


class PkceLoopbackPairing:
    """One-shot 127.0.0.1 PKCE transaction.  Code/verifier never leave this object."""

    def __init__(self, profile: QuestionMcpProfile, *, random: Callable[[], str] = lambda: secrets.token_urlsafe(32)) -> None:
        self._profile = profile
        self._random = random
        self._state: str | None = None
        self._verifier: str | None = None
        self._redirect_uri: str | None = None

    def begin(self, port: int) -> PkcePairingStart:
        if not 1 <= port <= 65535:
            raise QuestionUserMcpUnavailable()
        state, verifier = self._random(), self._random()
        if len(state) < 32 or len(verifier) < 32:
            raise QuestionUserMcpUnavailable()
        redirect = f"http://127.0.0.1:{port}/callback"
        query = urlencode({"response_type": "code", "client_id": self._profile.client_id,
                           "redirect_uri": redirect, "state": state,
                           "code_challenge": _challenge(verifier), "code_challenge_method": "S256"})
        parsed = urlsplit(self._profile.authorization_url)
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
        self._state, self._verifier, self._redirect_uri = state, verifier, redirect
        return PkcePairingStart(url, redirect, state)

    def consume_callback(self, *, state: str, code: SecretStr, exchange: Callable[..., SecretStr], store: OpaqueCredentialStore) -> str:
        verifier, redirect, expected = self._verifier, self._redirect_uri, self._state
        self._state = self._verifier = self._redirect_uri = None
        if verifier is None or redirect is None or expected is None or not secrets.compare_digest(state, expected):
            raise QuestionUserMcpUnavailable()
        try:
            token = exchange(code=code, redirect_uri=redirect, code_verifier=verifier)
            if type(token) is not SecretStr or not token.get_secret_value():
                raise QuestionUserMcpUnavailable()
            return store.put(token)
        except QuestionUserMcpUnavailable:
            raise
        except Exception as error:
            raise QuestionUserMcpUnavailable() from error

    def receive_once(
        self,
        port: int,
        *,
        exchange: Callable[..., SecretStr],
        store: OpaqueCredentialStore,
        timeout_seconds: float = 120.0,
    ) -> str:
        """Accept exactly one local callback then discard its code and verifier.

        The handler neither logs nor retains the query.  Binding to the literal
        IPv4 loopback address is intentional: ``localhost`` can be remotely
        reconfigured or resolve to another address family.
        """
        pairing = self

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return None

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query, strict_parsing=True)
                state = query.get("state", [])
                code = query.get("code", [])
                if parsed.path != "/callback" or len(state) != 1 or len(code) != 1:
                    self.send_response(400)
                    self.end_headers()
                    self.server.result = None  # type: ignore[attr-defined]
                    return
                try:
                    self.server.result = pairing.consume_callback(  # type: ignore[attr-defined]
                        state=state[0], code=SecretStr(code[0]), exchange=exchange, store=store
                    )
                    self.send_response(200)
                except Exception:
                    self.server.result = None  # type: ignore[attr-defined]
                    self.send_response(400)
                self.send_header("cache-control", "no-store")
                self.end_headers()

        if not 1 <= port <= 65535 or not 0 < timeout_seconds <= 600:
            raise QuestionUserMcpUnavailable()
        server: HTTPServer | None = None
        try:
            server = HTTPServer(("127.0.0.1", port), Callback)
            server.timeout = timeout_seconds
            server.handle_request()
            result = getattr(server, "result", None)
        except OSError as error:
            raise QuestionUserMcpUnavailable() from error
        finally:
            if server is not None:
                server.server_close()
        if type(result) is not str:
            raise QuestionUserMcpUnavailable()
        return result


class RemoteQuestionGateway(Protocol):
    def ask_org(self, question: str) -> str: ...
    def get_question(self, request_id: str) -> str: ...


class _HttpResponse(Protocol):
    status: int
    def geturl(self) -> str: ...
    def read(self, amount: int = -1) -> bytes: ...


class _HttpOpener(Protocol):
    def __call__(self, request: Request) -> _HttpResponse: ...


class HttpsQuestionGatewayClient:
    """No fallback transport: a paired token is sent only to the configured HTTPS origin."""
    def __init__(self, profile: QuestionMcpProfile, credentials: OpaqueCredentialStore, *, opener: _HttpOpener | None = None) -> None:
        self._profile, self._credentials = profile, credentials
        self._open = opener or build_opener(_NoRedirect()).open

    def ask_org(self, question: str) -> str:
        return self._call("/ask_org", {"question": question})

    def get_question(self, request_id: str) -> str:
        return self._call("/get_question", {"request_id": request_id})

    def _call(self, path: str, payload: dict[str, str]) -> str:
        reference = self._profile.credential_ref
        if reference is None:
            raise QuestionUserMcpUnavailable()
        token = self._credentials.get(reference)
        url = self._profile.gateway_url.rstrip("/") + path
        request = Request(url, data=json.dumps(payload, separators=(",", ":")).encode(), method="POST", headers={"content-type": "application/json", "authorization": "Bearer " + token.get_secret_value()})
        try:
            response = self._open(request)
            if getattr(response, "status", None) != 200 or response.geturl() != url:
                raise QuestionUserMcpUnavailable()
            raw = response.read(_MAX_BODY + 1)
            if len(raw) > _MAX_BODY:
                raise QuestionUserMcpUnavailable()
            parsed = cast(object, json.loads(raw))
            if type(parsed) is not dict:
                raise QuestionUserMcpUnavailable()
            response_body = cast(dict[object, object], parsed)
            if set(response_body) != {"text"} or type(response_body["text"]) is not str:
                raise QuestionUserMcpUnavailable()
            return response_body["text"]
        except (HTTPError, URLError, ValueError, OSError) as error:
            raise QuestionUserMcpUnavailable() from error


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        return None


def _exchange_code(profile: QuestionMcpProfile, *, code: SecretStr, redirect_uri: str, code_verifier: str) -> SecretStr:
    """Minimal authorization-code exchange; the response is kept process-local."""
    body = urlencode({"grant_type": "authorization_code", "code": code.get_secret_value(),
                      "redirect_uri": redirect_uri, "client_id": profile.client_id,
                      "code_verifier": code_verifier}).encode()
    request = Request(profile.token_url, data=body, method="POST", headers={"content-type": "application/x-www-form-urlencoded"})
    try:
        response = build_opener(_NoRedirect()).open(request, timeout=30)
        if response.status != 200 or response.geturl() != profile.token_url:
            raise QuestionUserMcpUnavailable()
        raw = response.read(_MAX_BODY + 1)
        if len(raw) > _MAX_BODY:
            raise QuestionUserMcpUnavailable()
        payload = cast(object, json.loads(raw))
        if type(payload) is not dict:
            raise QuestionUserMcpUnavailable()
        token = cast(dict[object, object], payload).get("id_token")
        if type(token) is not str or not token:
            raise QuestionUserMcpUnavailable()
        return SecretStr(token)
    except (HTTPError, URLError, ValueError, OSError) as error:
        raise QuestionUserMcpUnavailable() from error


def create_question_user_mcp(*, gateway: RemoteQuestionGateway) -> FastMCP:
    """Production `aon-mcp` server: exactly the two ADR-0070 question tools."""
    # Protocol runtime checks are not reliable; retain a narrow callable boundary.
    if not callable(getattr(gateway, "ask_org", None)) or not callable(getattr(gateway, "get_question", None)):
        raise TypeError("remote question gateway required")
    mcp = FastMCP("Agent Org Network — Question User")
    @mcp.tool(name="ask_org", description="조직에 질문을 접수합니다.")
    def ask_org(question: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return gateway.ask_org(question)
    @mcp.tool(name="get_question", description="내가 접수한 질문 결과를 조회합니다.")
    def get_question(request_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return gateway.get_question(request_id)
    return mcp


def _read_profile(path: str) -> QuestionMcpProfile:
    try:
        return QuestionMcpProfile.model_validate_json(Path(path).read_bytes())
    except Exception as error:
        raise QuestionUserMcpUnavailable() from error


def main() -> None:
    """`aon-mcp pair|serve-stdio`; no identity, role, org or token CLI flags exist."""
    parser = argparse.ArgumentParser(prog="aon-mcp")
    commands = parser.add_subparsers(dest="command", required=True)
    pair = commands.add_parser("pair")
    pair.add_argument("--profile", required=True)
    pair.add_argument("--output", required=True)
    pair.add_argument("--port", type=int, required=True)
    serve = commands.add_parser("serve-stdio")
    serve.add_argument("--profile", required=True)
    args = parser.parse_args()
    if args.command == "serve-stdio":
        profile = _read_profile(args.profile)
        create_question_user_mcp(gateway=HttpsQuestionGatewayClient(profile, KeyringOpaqueCredentialStore())).run()
        return
    profile = _read_profile(args.profile)
    pairing = PkceLoopbackPairing(profile)
    start = pairing.begin(args.port)
    webbrowser.open(start.authorization_url, new=1)
    def exchange(*, code: SecretStr, redirect_uri: str, code_verifier: str) -> SecretStr:
        return _exchange_code(
            profile, code=code, redirect_uri=redirect_uri, code_verifier=code_verifier
        )
    reference = pairing.receive_once(
        args.port, exchange=exchange, store=KeyringOpaqueCredentialStore()
    )
    paired = profile.model_copy(update={"credential_ref": reference})
    try:
        Path(args.output).write_text(paired.model_dump_json(), encoding="utf-8")
    except OSError as error:
        raise QuestionUserMcpUnavailable() from error


__all__ = ["HttpsQuestionGatewayClient", "KeyringOpaqueCredentialStore", "OpaqueCredentialStore", "PkceLoopbackPairing", "PkcePairingStart", "QUESTION_USER_TOOL_MANIFEST", "QuestionMcpProfile", "QuestionUserMcpUnavailable", "create_question_user_mcp", "main"]
