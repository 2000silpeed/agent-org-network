import json
from typing import cast
from urllib.request import Request
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from pydantic import SecretStr
from pydantic import ValidationError

from agent_org_network.central_authoring_client import (
    CentralAuthoringHttpClient,
    CentralAuthoringHttpUnavailable,
)
from agent_org_network.production_authoring_identity import (
    AuthoringIdentitySessionRef,
    AuthoringInvocation,
)
from agent_org_network.sqlite_production_authoring_runs import (
    AuthoringSourceRef,
    StartAuthoringRunCommand,
)


class _Headers:
    def __init__(self, content_type: str) -> None:
        self._content_type = content_type

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._content_type if name == "content-type" else default


class _Response:
    def __init__(
        self,
        value: object,
        *,
        url: str = "https://central.example/authoring/runs/start",
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self._body = json.dumps(value).encode()
        self._url = url
        self.status = status
        self._headers = _Headers(content_type)

    @property
    def headers(self) -> _Headers:
        return self._headers

    def read(self, amount: int = -1) -> bytes:
        return self._body[:amount]

    def geturl(self) -> str:
        return self._url


def _command() -> StartAuthoringRunCommand:
    return StartAuthoringRunCommand(
        org_id="acme",
        principal_id="owner",
        idempotency_key="start-1",
        agent_id="support",
        expected_card_revision=2,
        expected_card_digest="b" * 64,
        sources=(
            AuthoringSourceRef(
                source_digest="c" * 64,
                byte_size=12,
                media_type="text/markdown",
            ),
        ),
    )


def _invocation(session: str = "s" * 32) -> AuthoringInvocation:
    return AuthoringInvocation(
        session=AuthoringIdentitySessionRef(value=SecretStr(session)),
        org_id="acme",
        principal_id="owner",
        identity_provider="corp",
    )


def test_real_client는_metadata_only_exact_TLS_POST를보낸다() -> None:
    seen: list[Request] = []

    def open_request(request: Request, *, timeout: float) -> _Response:
        seen.append(request)
        assert timeout == 3
        return _Response(
            {
                "run": {
                    "org_id": "acme",
                    "run_id": "run-1",
                    "agent_id": "support",
                    "owner_id": "owner",
                    "stage": "Extracting",
                    "revision": 0,
                    "card_revision": 2,
                    "card_digest": "b" * 64,
                    "source_set_digest": "d" * 64,
                    "source_count": 1,
                    "total_bytes": 12,
                    "created_at": "2026-07-28T00:00:00.000Z",
                },
                "replayed": False,
            }
        )

    client = CentralAuthoringHttpClient(
        "https://central.example",
        timeout_seconds=3,
        opener=open_request,
    )
    assert client.start(_command(), invocation=_invocation()).run.stage == "Extracting"
    wire = cast(bytes, seen[0].data)
    assert seen[0].full_url == "https://central.example/authoring/runs/start"
    assert b"PRIVATE" not in wire
    assert b"filename" not in wire
    assert b"full_draft" not in wire
    assert seen[0].get_header("Cookie") == f"aon_identity_session={'s' * 32}"
    assert "s" * 32 not in repr(client)
    assert client.start(
        _command(), invocation=_invocation("t" * 32)
    ).run.stage == "Extracting"
    assert seen[1].get_header("Cookie") == f"aon_identity_session={'t' * 32}"


@pytest.mark.parametrize(
    "url",
    [
        "http://central.example",
        "https://user@central.example",
        "https://central.example/path",
        "https://central.example?query=1",
        "https://central.example#fragment",
        "https://localhost",
    ],
)
def test_invalid_or_nonTLS_url은_failclosed다(url: str) -> None:
    with pytest.raises(CentralAuthoringHttpUnavailable):
        CentralAuthoringHttpClient(url)


@pytest.mark.parametrize(
    "session",
    ["short", "s" * 31, "s" * 129, "s" * 31 + " ", "s" * 31 + ";", "s" * 31 + ",", "s" * 31 + "\n"],
)
def test_O1_opaque_session_grammar를_exact재사용한다(session: str) -> None:
    with pytest.raises(ValidationError):
        _invocation(session)


@pytest.mark.parametrize(
    ("final_url", "status"),
    [
        ("https://evil.example/authoring/runs/start", 200),
        ("http://central.example/authoring/runs/start", 200),
        ("https://central.example/authoring/runs/start", 302),
    ],
)
def test_redirect_or_final_provenance_mismatch는_second_request0이다(
    final_url: str, status: int
) -> None:
    seen: list[Request] = []

    def open_once(request: Request, *, timeout: float) -> _Response:
        seen.append(request)
        return _Response({}, url=final_url, status=status)

    client = CentralAuthoringHttpClient(
        "https://central.example",
        opener=open_once,
    )
    with pytest.raises(CentralAuthoringHttpUnavailable):
        client.start(_command(), invocation=_invocation())
    assert len(seen) == 1
    assert seen[0].full_url.startswith("https://central.example/")
    assert seen[0].get_header("Cookie") == f"aon_identity_session={'s' * 32}"


def test_production_opener에는_redirect_handler가완전히비활성화돼있다() -> None:
    import agent_org_network.central_authoring_client as module

    handlers = getattr(module, "_REAL_OPENER").handlers
    redirect = next(
        handler for handler in handlers if type(handler).__name__ == "_NoRedirect"
    )
    assert redirect.redirect_request(
        Request("https://central.example/start"),
        None,
        302,
        "Found",
        {},
        "https://evil.example/steal",
    ) is None


def test_real_urllib_handler_chain은_302를따르지않아_target과credential유출0이다() -> None:
    source_count = 0
    target_count = 0
    target_headers: list[tuple[str | None, str | None]] = []

    class Target(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal target_count
            target_count += 1
            target_headers.append(
                (
                    self.headers.get("Cookie"),
                    self.headers.get("Idempotency-Key"),
                )
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return None

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    target_thread = Thread(target=target.serve_forever, daemon=True)
    target_thread.start()

    class Source(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal source_count
            source_count += 1
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{target.server_address[1]}/steal",
            )
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return None

    source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    source_thread = Thread(target=source.serve_forever, daemon=True)
    source_thread.start()
    try:
        import agent_org_network.central_authoring_client as module

        request = Request(
            f"http://127.0.0.1:{source.server_address[1]}/start",
            data=b"{}",
            method="POST",
            headers={
                "Cookie": f"aon_identity_session={'s' * 32}",
                "Idempotency-Key": "start-1",
            },
        )
        with pytest.raises(HTTPError) as raised:
            getattr(module, "_REAL_OPENER").open(request, timeout=2)
        assert raised.value.code == 302
        assert source_count == 1
        assert target_count == 0
        assert target_headers == []
    finally:
        source.shutdown()
        target.shutdown()
        source.server_close()
        target.server_close()
        source_thread.join(timeout=2)
        target_thread.join(timeout=2)
