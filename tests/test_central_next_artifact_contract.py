"""Built Central Next artifact contract; source-only checks cannot detect stale wheels."""

from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from typing import ClassVar

from agent_org_network.central_web_runtime import discover_central_next_artifact


ROOT = Path(__file__).parents[1]
STANDALONE = ROOT / "frontend" / ".next" / "standalone"
ROUTE_BUNDLE = STANDALONE / ".next" / "server" / "app" / "api" / "[...path]" / "route.js"
AUTH_ROUTE_BUNDLE = STANDALONE / ".next" / "server" / "app" / "api" / "auth" / "login" / "start" / "route.js"
QUESTION_ROUTE_BUNDLES = (
    STANDALONE / ".next" / "server" / "app" / "api" / "questions" / "route.js",
    STANDALONE / ".next" / "server" / "app" / "api" / "questions" / "[request_id]" / "route.js",
    STANDALONE / ".next" / "server" / "app" / "api" / "questions" / "[request_id]" / "stream" / "route.js",
    STANDALONE / ".next" / "server" / "app" / "api" / "questions" / "[request_id]" / "feedback" / "route.js",
)
INBOX_ROUTE_BUNDLES = (
    STANDALONE / ".next/server/app/api/inbox/conflicts/route.js",
    STANDALONE / ".next/server/app/api/inbox/conflicts/[case_id]/route.js",
    STANDALONE / ".next/server/app/api/inbox/conflicts/[case_id]/concurrences/route.js",
    STANDALONE / ".next/server/app/api/inbox/backup-reviews/route.js",
    STANDALONE / ".next/server/app/api/inbox/backup-reviews/[review_id]/route.js",
    STANDALONE / ".next/server/app/api/inbox/backup-reviews/[review_id]/dispositions/route.js",
    STANDALONE / ".next/server/app/api/inbox/reevaluations/route.js",
    STANDALONE / ".next/server/app/api/inbox/reevaluations/[reevaluation_id]/route.js",
    STANDALONE / ".next/server/app/api/inbox/reevaluations/[reevaluation_id]/dispositions/route.js",
    STANDALONE / ".next/server/app/api/inbox/approvals/route.js",
    STANDALONE / ".next/server/app/api/inbox/approvals/[approval_item_id]/route.js",
    STANDALONE / ".next/server/app/api/inbox/approvals/[approval_item_id]/dispositions/route.js",
    STANDALONE / ".next/server/app/api/inbox/approvals/[approval_item_id]/reassignments/route.js",
)
CENTRAL_ADMIN_ROUTE_BUNDLES = (
    STANDALONE / ".next/server/app/api/console/org/route.js",
    STANDALONE / ".next/server/app/api/admin/scorecard/route.js",
    STANDALONE / ".next/server/app/api/admin/agent-cards/[card_id]/owner-transfers/route.js",
    STANDALONE / ".next/server/app/api/admin/agent-cards/[card_id]/revocations/route.js",
)


def test_built_standalone_uses_dedicated_question_routes_and_denies_generic_fallback() -> None:
    """The Contract Gate builds first, then rejects the former generic Question BFF."""
    artifact = discover_central_next_artifact(source_checkout_root=ROOT)
    assert artifact.root == STANDALONE
    bundle = ROUTE_BUNDLE.read_text(encoding="utf-8")
    assert "v1/questions" not in bundle
    assert all(path.is_file() for path in QUESTION_ROUTE_BUNDLES)
    built = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in QUESTION_ROUTE_BUNDLES)
    for required in ("/api/questions", "create", "retrieve", "stream", "feedback"):
        assert required in built


def test_built_standalone_uses_exact_dedicated_inbox_routes_and_no_generic_fallback() -> None:
    artifact = discover_central_next_artifact(source_checkout_root=ROOT)
    assert artifact.root == STANDALONE
    generic = ROUTE_BUNDLE.read_text(encoding="utf-8")
    assert "/v1/inbox" not in generic
    assert all(path.is_file() for path in INBOX_ROUTE_BUNDLES)
    built = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in STANDALONE.rglob("*.js")
    )
    for required in (
        "/v1/inbox/",
        "conflicts",
        "backup-reviews",
        "reevaluations",
        "approvals",
        "concurrences",
        "dispositions",
        "reassignments",
        "127.0.0.1:8010",
    ):
        assert required in built


def test_built_standalone_has_only_exact_central_browser_auth_routes_and_no_legacy_identity_bundle() -> None:
    """The wheel's traced Next artifact must keep auth separate from generic BFF."""
    expected = {
        "callback": STANDALONE / ".next/server/app/api/auth/callback/route.js",
        "login": AUTH_ROUTE_BUNDLE,
        "logout": STANDALONE / ".next/server/app/api/auth/logout/route.js",
        "session": STANDALONE / ".next/server/app/api/auth/session/route.js",
    }
    assert all(path.is_file() for path in expected.values())
    generic = ROUTE_BUNDLE.read_text(encoding="utf-8")
    assert "browser-auth" not in generic
    built = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in STANDALONE.rglob("*.js"))
    for required in (
        "/v1/browser-auth/login/start", "/v1/browser-auth/callback",
        "/v1/browser-auth/session", "/v1/browser-auth/logout", "127.0.0.1:8010",
    ):
        assert required in built
    for forbidden in (
        "aon.operator.userId", "passwordless", "/api/login", "/api/owner-api",
        "/api/author", "/api/builder", "owner-api", "A2A Remote Runtime",
    ):
        assert forbidden not in built


def test_built_standalone_has_exact_registry_admission_routes_separate_from_generic_bff() -> None:
    expected = (
        STANDALONE / ".next/server/app/api/onboarding/status/route.js",
        STANDALONE / ".next/server/app/api/admin/users/route.js",
        STANDALONE / ".next/server/app/api/admin/agent-cards/route.js",
    )
    assert all(path.is_file() for path in expected)
    generic = ROUTE_BUNDLE.read_text(encoding="utf-8")
    assert "onboarding/status" not in generic
    assert "admin/agent-cards" not in generic
    built = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in STANDALONE.rglob("*.js"))
    for required in ("/onboarding/status", "/admin/users", "/admin/agent-cards", "central_admission_unavailable"):
        assert required in built
    for forbidden in ("SsoUnavailable", "Knowledge OKF", "owner-api", "content_base64", "aon.operator.userId"):
        assert forbidden not in built


def test_built_standalone_has_exact_central_graph_ownership_and_scorecard_routes() -> None:
    """RB3.2b.6-E keeps the new control-plane browser paths in the Central artifact."""
    assert all(path.is_file() for path in CENTRAL_ADMIN_ROUTE_BUNDLES)
    generic = ROUTE_BUNDLE.read_text(encoding="utf-8")
    assert "/v1/console/org" not in generic
    assert "/v1/admin/scorecard" not in generic
    built = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in CENTRAL_ADMIN_ROUTE_BUNDLES
    )
    all_built = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in STANDALONE.rglob("*.js")
    )
    for required in (
        "/api/console/org",
        "/api/admin/scorecard",
        "/api/admin/agent-cards/",
        "owner-transfers",
        "revocations",
        "central_admin_unavailable",
        "next-standalone-clean",
    ):
        source = all_built if required in {
            "central_admin_unavailable",
            "next-standalone-clean",
        } else built
        assert required in source
    for forbidden in ("/api/owner-api", "raw_source", "full_draft", "A2A Remote Runtime"):
        assert forbidden not in built


class _CentralProbeHandler(BaseHTTPRequestHandler):
    paths: ClassVar[list[str]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler hook
        type(self).paths.append(self.path)
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(
            b'{"items":[]}'
            if self.path == "/v1/inbox/approvals"
            else b'{"state":"received"}'
        )

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args
        return


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _request(port: int, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _request_with_headers(
    port: int, method: str, path: str, headers: dict[str, str] | None = None, body: bytes | None = None
) -> tuple[int, bytes, list[tuple[str, str]]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), response.getheaders()
    finally:
        connection.close()


def _wait_for_health(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"Central Next exited early: {process.returncode}")
        try:
            status, _ = _request(port, "/healthz")
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError("Central Next health endpoint did not become ready")


def test_built_node_generic_bff_never_forwards_question_path_to_central_api() -> None:
    """Exercise the compiled generic route, not a transpiled TypeScript approximation."""
    node = shutil.which("node")
    assert node is not None
    _CentralProbeHandler.paths = []
    backend = ThreadingHTTPServer(("127.0.0.1", 8010), _CentralProbeHandler)
    server_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    server_thread.start()
    port = _unused_loopback_port()
    environment = {
        "AON_FRONTEND_MODE": "central-local-reference",
        "AON_PUBLIC_ORIGIN": "https://central.example.test",
        "AON_BACKEND_URL": "http://127.0.0.1:8010",
        "HOSTNAME": "127.0.0.1",
        "PORT": str(port),
        "NODE_ENV": "production",
    }
    process = subprocess.Popen(
        [node, "server.js"],
        cwd=STANDALONE,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        _wait_for_health(port, process)
        status, _ = _request(port, "/api/v1/questions/request-1")
        assert status == 404
        assert _CentralProbeHandler.paths == []

        _CentralProbeHandler.paths.clear()
        for suffix in (
            "%2e%2e",
            "%252e%252e",
            "%2f",
            "%2F",
            "%5c",
            "%5C",
            "request%2fchild",
            "request%5cchild",
        ):
            status, _ = _request(port, f"/api/v1/questions/{suffix}")
            assert status == 404, suffix
        assert _CentralProbeHandler.paths == []
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        backend.shutdown()
        backend.server_close()
        server_thread.join(timeout=5)


def test_built_node_inbox_wrapper_rejects_raw_clean_marker_and_forwarding_claims() -> None:
    """Only the pre-Next wrapper's unforgeable companion may admit synthesized forwarding."""
    node = shutil.which("node")
    assert node is not None
    _CentralProbeHandler.paths = []
    backend = ThreadingHTTPServer(("127.0.0.1", 8010), _CentralProbeHandler)
    server_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    server_thread.start()
    port = _unused_loopback_port()
    process = subprocess.Popen(
        [node, "server.js"],
        cwd=STANDALONE,
        env={
            "AON_FRONTEND_MODE": "central-local-reference",
            "AON_PUBLIC_ORIGIN": "https://central.example.test",
            "AON_BACKEND_URL": "http://127.0.0.1:8010",
            "HOSTNAME": "127.0.0.1",
            "PORT": str(port),
            "NODE_ENV": "production",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    base = {
        "Host": "central.example.test",
        "Cookie": "__Host-aon-central-session=opaque-session",
    }
    try:
        _wait_for_health(port, process)
        status, body, _headers = _request_with_headers(
            port, "GET", "/api/inbox/approvals", base
        )
        assert status == 200
        assert body == b'{"items":[]}'
        assert _CentralProbeHandler.paths == ["/v1/inbox/approvals"]

        forged = (
            {**base, "X-AON-Admission-Proxy-Provenance": "next-standalone-clean"},
            {**base, "X-Forwarded-Host": "central.example.test"},
            {
                **base,
                "X-AON-Admission-Proxy-Provenance": "next-standalone-clean",
                "X-Forwarded-Host": "central.example.test",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": "127.0.0.1",
            },
        )
        for headers in forged:
            status, body, _response_headers = _request_with_headers(
                port, "GET", "/api/inbox/approvals", headers
            )
            assert status == 403
            assert b'"code":"invalid_input"' in body
        assert _CentralProbeHandler.paths == ["/v1/inbox/approvals"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        backend.shutdown()
        backend.server_close()
        server_thread.join(timeout=5)


class _BrowserAuthProbeHandler(BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, str, dict[str, str]]]] = []

    def do_GET(self) -> None:  # noqa: N802
        self._reply()

    def do_POST(self) -> None:  # noqa: N802
        self._reply()

    def _reply(self) -> None:
        type(self).calls.append((self.command, self.path, {key.lower(): value for key, value in self.headers.items()}))
        if self.path == "/v1/browser-auth/login/start":
            self.send_response(303)
            self.send_header("location", "https://idp.example.test/authorize?opaque=1")
            self.send_header("set-cookie", "__Host-aon-central-oidc-tx=tx; Max-Age=600; Path=/; SameSite=lax; Secure; HttpOnly")
        elif self.path == "/v1/browser-auth/callback?code=ok&state=state":
            self.send_response(303)
            self.send_header("location", "/ask")
            self.send_header("set-cookie", "__Host-aon-central-oidc-tx=; Max-Age=0; Path=/; SameSite=lax; Secure; HttpOnly")
            self.send_header("set-cookie", "__Host-aon-central-session=session; Max-Age=28800; Path=/; SameSite=lax; Secure; HttpOnly")
            self.send_header("set-cookie", "__Host-aon-central-csrf=csrf; Max-Age=28800; Path=/; SameSite=strict; Secure")
        elif self.path == "/v1/browser-auth/callback?code=external&state=state":
            self.send_response(303)
            self.send_header("location", "https://evil.example.test/")
        elif self.path == "/v1/browser-auth/session":
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(b'{"authenticated":true,"registry_user_ref":"opaque-user","expires_at":"2030-01-01T00:00:00Z","actions":["session.read"]}')
            return
        elif self.path == "/v1/browser-auth/logout":
            self.send_response(204)
            self.send_header("set-cookie", "__Host-aon-central-oidc-tx=; Max-Age=0; Path=/; SameSite=lax; Secure; HttpOnly")
            self.send_header("set-cookie", "__Host-aon-central-session=; Max-Age=0; Path=/; SameSite=lax; Secure; HttpOnly")
            self.send_header("set-cookie", "__Host-aon-central-csrf=; Max-Age=0; Path=/; SameSite=strict; Secure")
        else:
            self.send_response(404)
        self.send_header("cache-control", "no-store")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args
        return


def test_built_node_browser_auth_bff_relays_only_exact_routes_and_browser_safe_headers() -> None:
    """Exercise dedicated compiled BFF routes against the fixed Central API socket."""
    node = shutil.which("node")
    assert node is not None
    _BrowserAuthProbeHandler.calls = []
    backend = ThreadingHTTPServer(("127.0.0.1", 8010), _BrowserAuthProbeHandler)
    server_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    server_thread.start()
    port = _unused_loopback_port()
    process = subprocess.Popen(
        [node, "server.js"], cwd=STANDALONE,
        env={
            "AON_FRONTEND_MODE": "central-local-reference", "AON_PUBLIC_ORIGIN": "https://central.example.test",
            "AON_BACKEND_URL": "http://127.0.0.1:8010", "HOSTNAME": "127.0.0.1", "PORT": str(port), "NODE_ENV": "production",
        }, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
    )
    start_headers = {
        "origin": "https://central.example.test", "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "authorization": "Bearer ignored",
    }
    try:
        _wait_for_health(port, process)
        status, _body, headers = _request_with_headers(port, "POST", "/api/auth/login/start", start_headers)
        assert status == 403
        assert _BrowserAuthProbeHandler.calls == []
        start_headers.pop("authorization")
        status, _body, headers = _request_with_headers(port, "POST", "/api/auth/login/start", start_headers)
        assert status == 303
        assert dict(headers)["location"] == "https://idp.example.test/authorize?opaque=1"
        assert _BrowserAuthProbeHandler.calls[-1][:2] == ("POST", "/v1/browser-auth/login/start")
        forwarded_start = _BrowserAuthProbeHandler.calls[-1][2]
        assert {"origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest"} <= set(forwarded_start)
        assert {"cookie", "authorization", "x-user", "x-forwarded-host", "x-aon-csrf"}.isdisjoint(forwarded_start)

        status, _body, headers = _request_with_headers(port, "GET", "/api/auth/callback?code=ok&state=state", {"cookie": "__Host-aon-central-oidc-tx=tx", "origin": "https://evil.test"})
        assert status == 303
        assert dict(headers)["location"] == "/ask"
        callback_cookies = [value for key, value in headers if key.lower() == "set-cookie"]
        assert len(callback_cookies) == 3
        _assert_cookie_contract(callback_cookies[0], {"__host-aon-central-oidc-tx=", "max-age=0", "path=/", "samesite=lax", "secure", "httponly"})
        _assert_cookie_contract(callback_cookies[1], {"__host-aon-central-session=session", "max-age=28800", "path=/", "samesite=lax", "secure", "httponly"})
        _assert_cookie_contract(callback_cookies[2], {"__host-aon-central-csrf=csrf", "max-age=28800", "path=/", "samesite=strict", "secure"})
        assert "httponly" not in callback_cookies[2].lower()
        assert _BrowserAuthProbeHandler.calls[-1][2].get("cookie") == "__Host-aon-central-oidc-tx=tx"
        assert "origin" not in _BrowserAuthProbeHandler.calls[-1][2]

        before = len(_BrowserAuthProbeHandler.calls)
        for method, path, headers in (
            ("GET", "/api/auth/callback?code=ok&state=state&return_to=/ask", {}),
            ("GET", "/api/auth/session?user=forged", {"cookie": "__Host-aon-central-session=session"}),
            ("POST", "/api/auth/login/start?return_to=/ask", start_headers),
            ("POST", "/api/auth/login%2fstart", start_headers),
        ):
            status, _body, _headers = _request_with_headers(port, method, path, headers)
            assert status in {400, 403, 404}
        assert len(_BrowserAuthProbeHandler.calls) == before

        status, body, _headers = _request_with_headers(port, "GET", "/api/auth/session", {"cookie": "__Host-aon-central-session=session", "x-user": "forged"})
        assert status == 400 and body == b""
        status, body, _headers = _request_with_headers(port, "GET", "/api/auth/session", {"cookie": "__Host-aon-central-session=session", "origin": "https://evil.test"})
        assert status == 200 and b"opaque-user" in body
        assert _BrowserAuthProbeHandler.calls[-1][2].get("cookie") == "__Host-aon-central-session=session"
        assert "origin" not in _BrowserAuthProbeHandler.calls[-1][2]

        status, body, headers = _request_with_headers(port, "POST", "/api/auth/logout", {
            "cookie": "__Host-aon-central-session=session; __Host-aon-central-csrf=csrf", "origin": "https://central.example.test",
            "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "x-aon-csrf": "csrf",
        })
        assert status == 204 and body == b""
        logout_cookies = [value for key, value in headers if key.lower() == "set-cookie"]
        assert len(logout_cookies) == 3
        _assert_cookie_contract(logout_cookies[0], {"__host-aon-central-oidc-tx=", "max-age=0", "path=/", "samesite=lax", "secure", "httponly"})
        _assert_cookie_contract(logout_cookies[1], {"__host-aon-central-session=", "max-age=0", "path=/", "samesite=lax", "secure", "httponly"})
        _assert_cookie_contract(logout_cookies[2], {"__host-aon-central-csrf=", "max-age=0", "path=/", "samesite=strict", "secure"})
        assert "httponly" not in logout_cookies[2].lower()
        assert _BrowserAuthProbeHandler.calls[-1][2]["x-aon-csrf"] == "csrf"

        status, body, headers = _request_with_headers(port, "GET", "/api/auth/callback?code=external&state=state", {"cookie": "__Host-aon-central-oidc-tx=tx"})
        assert status == 502 and body == b""
        assert not any(key.lower() == "location" for key, _value in headers)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        backend.shutdown()
        backend.server_close()
        server_thread.join(timeout=5)


def _assert_cookie_contract(value: str, expected_parts: set[str]) -> None:
    normalized = {part.strip().lower() for part in value.split(";")}
    assert expected_parts <= normalized


class _AdmissionProbeHandler(BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, str, dict[str, str], bytes]]] = []

    def do_GET(self) -> None:  # noqa: N802
        self._reply()

    def do_POST(self) -> None:  # noqa: N802
        self._reply()

    def _reply(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        type(self).calls.append((self.command, self.path, {key.lower(): value for key, value in self.headers.items()}, body))
        if self.path == "/onboarding/status":
            payload = b'{"revision":4,"card_capability":"available","steps":[{"kind":"user","label":"Registry User","state":"complete"},{"kind":"card","label":"Agent Card","state":"current"},{"kind":"card_owner_installation","label":"Card Owner Installation","state":"locked"}],"cards":[],"card_owner_installation":{"artifact":"agent-org-owner","href":"/onboarding#card-owner-installation"}}'
        elif self.path == "/admin/users" and self.command == "GET":
            payload = b'[{"user_id":"root","email":"root@example.test","manager":null,"sso_link_status":"verified_email_match"}]'
        elif self.path == "/admin/agent-cards" and self.command == "GET":
            payload = b'[]'
        elif self.path == "/admin/users" and self.command == "POST":
            payload = b'{"user_id":"alice","email":"alice@example.test","manager":null,"revision":5,"replayed":false}'
        elif self.path == "/admin/agent-cards" and self.command == "POST":
            payload = b'{"card":{"agent_id":"support","owner":"alice","team":"Support","summary":"Support","domains":[],"last_reviewed_at":"2026-07-31","maintainer":null,"can_answer":[],"cannot_answer":[],"approval_when":[],"collaborate_when":[],"knowledge_sources":[],"trust_labels":[]},"revision":6,"replayed":false}'
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "public, max-age=600")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args


def test_built_node_admission_bff_uses_five_exact_routes_and_rejects_malicious_input_before_backend() -> None:
    node = shutil.which("node")
    assert node is not None
    _AdmissionProbeHandler.calls = []
    backend = ThreadingHTTPServer(("127.0.0.1", 8010), _AdmissionProbeHandler)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    port = _unused_loopback_port()
    process = subprocess.Popen([node, "server.js"], cwd=STANDALONE, env={
        "AON_FRONTEND_MODE": "central-local-reference", "AON_PUBLIC_ORIGIN": "https://central.example.test",
        "AON_BACKEND_URL": "http://127.0.0.1:8010", "HOSTNAME": "127.0.0.1", "PORT": str(port), "NODE_ENV": "production",
    }, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    cookie = "__Host-aon-central-session=session; __Host-aon-central-csrf=" + "a" * 43
    post_headers = {"cookie": cookie, "origin": "https://central.example.test", "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "x-aon-csrf": "a" * 43, "idempotency-key": "admission_1", "content-type": "application/json"}
    runtime_cache = STANDALONE / ".next" / "cache" / "images" / "post-start-mutable-cache"
    try:
        _wait_for_health(port, process)
        # Local Next Image uses unoptimized, versioned brand assets in this
        # standalone artifact; it must not depend on an absent native sharp.
        status, _body = _request(port, "/brand/mark.png")
        assert status == 200
        # A restart validates the already-running artifact again.  Cache data
        # written after build is mutable Next state, not a release tamper.
        runtime_cache.parent.mkdir(parents=True, exist_ok=True)
        runtime_cache.write_bytes(b"runtime cache")
        assert discover_central_next_artifact(source_checkout_root=ROOT).root == STANDALONE
        for path in ("/api/onboarding/status", "/api/admin/users", "/api/admin/agent-cards"):
            status, _body, headers = _request_with_headers(port, "GET", path, {"cookie": cookie})
            assert status == 200
            assert dict(headers)["cache-control"] == "no-store"
        status, _body, _headers = _request_with_headers(port, "POST", "/api/admin/users", post_headers, b'{"expected_revision":4,"user_id":"alice","email":"alice@example.test","manager":null}')
        assert status == 200
        status, _body, _headers = _request_with_headers(port, "POST", "/api/admin/agent-cards", post_headers, b'{"expected_revision":5,"agent_id":"support","owner":"alice","team":"Support","summary":"Support","domains":[],"maintainer":null,"can_answer":[],"cannot_answer":[],"approval_when":[],"collaborate_when":[],"knowledge_sources":[],"trust_labels":[]}')
        assert status == 200
        assert [(method, path) for method, path, _headers, _body in _AdmissionProbeHandler.calls] == [("GET", "/onboarding/status"), ("GET", "/admin/users"), ("GET", "/admin/agent-cards"), ("POST", "/admin/users"), ("POST", "/admin/agent-cards")]
        forwarded = _AdmissionProbeHandler.calls[-1][2]
        assert forwarded["host"] == "127.0.0.1:8010"
        assert set(forwarded).isdisjoint({"authorization", "forwarded", "x-forwarded-host", "x-aon-role"})
        assert {"cookie", "origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "x-aon-csrf", "idempotency-key", "content-type"} <= set(forwarded)
        before = len(_AdmissionProbeHandler.calls)
        status, _body, _headers = _request_with_headers(port, "GET", "/api/admin/users", {"cookie": cookie, "host": "evil.example"})
        assert status in {400, 404}
        for method, path, headers, body in (
            ("GET", "/api/admin/users?actor=forged", {"cookie": cookie}, None),
            ("POST", "/api/admin/users", {**post_headers, "x-forwarded-host": "evil.example"}, b"{}"),
            ("POST", "/api/admin/users", {**post_headers, "x-forwarded-host": f"127.0.0.1:{port}"}, b"{}"),
            ("POST", "/api/admin/users", {**post_headers, "x-forwarded-for": "198.51.100.7"}, b"{}"),
            ("POST", "/api/admin/users", {**post_headers, "x-forwarded-for": "127.0.0.1"}, b"{}"),
            ("POST", "/api/admin/users", {**post_headers, "x-forwarded-proto": "http"}, b"{}"),
            ("POST", "/api/admin/users", {**post_headers, "forwarded": "for=127.0.0.1;host=127.0.0.1"}, b"{}"),
            ("POST", "/api/admin/agent-cards", {**post_headers, "authorization": "Bearer forged"}, b"{}"),
            ("PUT", "/api/admin/users", post_headers, b"{}"),
            ("POST", "/api/onboarding/status", post_headers, b"{}"),
        ):
            status, _body, _headers = _request_with_headers(port, method, path, headers, body)
            assert status in {400, 403, 404, 405}
        assert len(_AdmissionProbeHandler.calls) == before
    finally:
        runtime_cache.unlink(missing_ok=True)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=5)
