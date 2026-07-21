"""R5.5 sealed composition for the four credential MCP tools.

The two historical public factories deliberately remain closed.  The enabled
surface can only be made from the three already sealed, path-bound operations;
it never accepts raw delivery, approval, scope, or authority dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, Callable, Final, cast, final

from mcp.server.fastmcp import FastMCP

from agent_org_network.central_authority import AuthenticatedPrincipal
from agent_org_network.credential_delivery import DeliveryStage, StageMissing
from agent_org_network.credential_issue_scoped_orchestration import (
    Issued,
    ReleasePending,
    ScopedCredentialIssueBridgeCapability,
    ScopedCredentialIssueCommand,
    ScopedCredentialIssueOperations,
    create_path_bound_scoped_credential_issue_operations,
)
from agent_org_network.credential_scoped_read import (
    CredentialReadList,
    CredentialReadNotFoundOrDenied,
    CredentialReadView,
    CredentialScopedRead,
    CredentialScopedReadCapability,
    create_credential_scoped_read,
)
from agent_org_network.credential_scoped_revoke import (
    CredentialRevokeCommand,
    CredentialRevokeConflict,
    CredentialRevokeResult,
    CredentialScopedRevoke,
    CredentialScopedRevokeCapability,
    create_credential_scoped_revoke,
)
from agent_org_network.durable_credentials import CredentialAction

__all__ = (
    "CREDENTIAL_MCP_ACTION_BINDINGS",
    "CREDENTIAL_MCP_TOOL_ACTIONS",
    "CredentialMcpActionBinding",
    "CredentialMcpCapability",
    "DeliveryStage",
    "StageMissing",
    "create_credential_mcp_capability",
    "create_enabled_credential_mcp_server",
    "create_durable_credential_mcp_server",
    "create_sealed_credential_mcp_server",
)


@dataclass(frozen=True)
class CredentialMcpActionBinding:
    """A tool name and its only permitted central action."""

    tool_name: str
    action: CredentialAction


CREDENTIAL_MCP_ACTION_BINDINGS: Final = (
    CredentialMcpActionBinding("issue_worker_credential", "worker_credential.issue"),
    CredentialMcpActionBinding("list_worker_credentials", "worker_credential.read"),
    CredentialMcpActionBinding("get_worker_credential", "worker_credential.read"),
    CredentialMcpActionBinding("revoke_worker_credential", "worker_credential.revoke"),
)
CREDENTIAL_MCP_TOOL_ACTIONS: Final = MappingProxyType(
    {binding.tool_name: binding.action for binding in CREDENTIAL_MCP_ACTION_BINDINGS}
)

_CAPABILITY_FACTORY_SEAL: Final = object()
_CAPABILITY_SEAL: Final = object()


def _zero_tool_server() -> FastMCP:
    return FastMCP("Agent Org Network — Credential MCP Unavailable")


def create_sealed_credential_mcp_server(*_args: object, **_kwargs: object) -> FastMCP:
    """Historical closed factory; it must not inspect supplied objects."""
    del _args, _kwargs
    return _zero_tool_server()


def create_durable_credential_mcp_server(*_args: object, **_kwargs: object) -> FastMCP:
    """Historical direct-registry factory remains permanently unavailable."""
    del _args, _kwargs
    return _zero_tool_server()


@final
class CredentialMcpCapability:
    """Single-use aggregate proof for an enabled credential MCP surface."""

    def __init__(
        self,
        *,
        path: Path,
        org_id: str,
        principal: AuthenticatedPrincipal,
        issue: ScopedCredentialIssueBridgeCapability,
        read: CredentialScopedReadCapability,
        revoke: CredentialScopedRevokeCapability,
        clock: Callable[[], datetime],
        seal: object,
    ) -> None:
        if seal is not _CAPABILITY_FACTORY_SEAL:
            raise TypeError("credential MCP capability는 sealed factory로만 조립합니다.")
        self._path = path
        self._org_id = org_id
        self._principal = principal
        self._issue = issue
        self._read = read
        self._revoke = revoke
        self._clock = clock
        self._seal = _CAPABILITY_SEAL
        self._claimed = False
        self._lock = RLock()

    def _claim(
        self,
    ) -> tuple[
        Path,
        str,
        ScopedCredentialIssueOperations,
        CredentialScopedRead,
        CredentialScopedRevoke,
        Callable[[], datetime],
    ]:
        # Fixed lock order also serializes a direct child claim against this
        # aggregate claim; no earlier child can be consumed alone on a race.
        with self._lock, self._issue._claim_lock, self._read._claim_lock, self._revoke._claim_lock:  # pyright: ignore[reportPrivateUsage]
            if (
                self._seal is not _CAPABILITY_SEAL
                or self._claimed
                or self._issue._claimed  # pyright: ignore[reportPrivateUsage]
                or self._read._claimed  # pyright: ignore[reportPrivateUsage]
                or self._revoke._used  # pyright: ignore[reportPrivateUsage]
            ):
                raise ValueError("credential MCP capability를 claim할 수 없습니다.")
            # This is the only claim point, after enabled-factory matrix
            # preflight.  All three calls are sealed and cannot fail after the
            # checked state above; concurrent calls to this aggregate serialize.
            issue = create_path_bound_scoped_credential_issue_operations(self._issue)
            read = create_credential_scoped_read(self._read)
            revoke = create_credential_scoped_revoke(self._revoke)
            self._claimed = True
            return self._path, self._org_id, issue, read, revoke, self._clock


@dataclass
class _EnabledOperations:
    value: (
        tuple[
            Path,
            str,
            ScopedCredentialIssueOperations,
            CredentialScopedRead,
            CredentialScopedRevoke,
            Callable[[], datetime],
        ]
        | None
    ) = None


def _same_principal(left: object, right: AuthenticatedPrincipal, org_id: str) -> bool:
    return type(left) is AuthenticatedPrincipal and left == right and right.org_id == org_id


def create_credential_mcp_capability(
    *,
    path: object,
    org_id: object,
    server_principal: object,
    issue_capability: object,
    read_capability: object,
    revoke_capability: object,
    server_clock: object,
) -> CredentialMcpCapability:
    """Validate the entire exact aggregate before claiming any component."""
    if (
        not isinstance(path, Path)
        or type(org_id) is not str
        or not org_id
        or type(server_principal) is not AuthenticatedPrincipal
        or type(issue_capability) is not ScopedCredentialIssueBridgeCapability
        or type(read_capability) is not CredentialScopedReadCapability
        or type(revoke_capability) is not CredentialScopedRevokeCapability
        or not callable(server_clock)
    ):
        raise TypeError("credential MCP의 exact sealed aggregate가 필요합니다.")
    # These are sealed component internals, not caller-provided dependency
    # objects.  Check every path/principal first; no capability is claimed here.
    try:
        bridge = issue_capability._bridge  # pyright: ignore[reportPrivateUsage]
        issue_path = bridge._path  # pyright: ignore[reportPrivateUsage]
        issue_principal = bridge._scoped_operations._guard.server_principal  # pyright: ignore[reportPrivateUsage]
        read_principal = read_capability._read._principal  # pyright: ignore[reportPrivateUsage]
        revoke_principal = revoke_capability._value._principal  # pyright: ignore[reportPrivateUsage]
    except Exception as error:
        raise TypeError("credential MCP sealed aggregate를 검증할 수 없습니다.") from error
    if (
        type(bridge).__module__ != "agent_org_network.credential_issue_scoped_orchestration"
        or type(bridge).__name__ != "_PathBoundScopedIssueBridge"
        or type(read_capability._read) is not CredentialScopedRead  # pyright: ignore[reportPrivateUsage]
        or type(revoke_capability._value) is not CredentialScopedRevoke  # pyright: ignore[reportPrivateUsage]
        or issue_capability._claimed  # pyright: ignore[reportPrivateUsage]
        or read_capability._claimed  # pyright: ignore[reportPrivateUsage]
        or revoke_capability._used  # pyright: ignore[reportPrivateUsage]
        or issue_path != path
        or not _same_principal(issue_principal, server_principal, org_id)
        or not _same_principal(read_principal, server_principal, org_id)
        or not _same_principal(revoke_principal, server_principal, org_id)
    ):
        raise TypeError("credential MCP path 또는 server principal이 일치하지 않습니다.")
    return CredentialMcpCapability(
        path=path,
        org_id=org_id,
        principal=server_principal,
        issue=issue_capability,
        read=read_capability,
        revoke=revoke_capability,
        clock=cast(Callable[[], datetime], server_clock),
        seal=_CAPABILITY_FACTORY_SEAL,
    )


def _timestamp(clock: Callable[[], datetime]) -> str | None:
    try:
        now = clock()
    except Exception:
        return None
    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
        return None
    value = now.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _view(value: CredentialReadView) -> dict[str, object]:
    return {
        "credential_id": value.credential_id,
        "role": value.role,
        "generation": value.generation,
        "revision": value.revision,
        "status": value.status,
        "issued_at": value.issued_at,
        "revoked_at": value.revoked_at,
    }


def _json(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _request(value: object, fields: tuple[str, ...]) -> dict[str, object] | None:
    """Opaque MCP boundary: malformed input never reaches FastMCP field validation."""
    try:
        decoded: object = json.loads(value) if type(value) is str else value
    except Exception:
        return None
    if not isinstance(decoded, dict):
        return None
    raw = cast(dict[object, object], decoded)
    payload: dict[str, object] = {key: value for key, value in raw.items() if type(key) is str}
    if len(payload) != len(raw) or set(payload) != set(fields):
        return None
    return payload


def create_enabled_credential_mcp_server(capability: CredentialMcpCapability) -> FastMCP:
    """Claim one complete aggregate and register exactly the immutable matrix."""
    if type(capability) is not CredentialMcpCapability:
        raise TypeError("exact credential MCP capability가 필요합니다.")
    # Registration is fully preflighted before *any* component capability is
    # consumed.  The exported matrix is immutable in normal operation; this
    # additionally closes test/host tampering before it can burn a capability.
    if tuple(CREDENTIAL_MCP_TOOL_ACTIONS.items()) != tuple(
        (binding.tool_name, binding.action) for binding in CREDENTIAL_MCP_ACTION_BINDINGS
    ):
        raise RuntimeError("credential MCP 도구 매트릭스가 완전하지 않습니다.")
    operations = _EnabledOperations()
    mcp = FastMCP("Agent Org Network — Credential MCP")
    registered: dict[str, CredentialAction] = {}

    def tool(
        name: str, action: CredentialAction, description: str
    ) -> Callable[[Callable[..., str]], Any]:
        if name in registered or CREDENTIAL_MCP_TOOL_ACTIONS.get(name) != action:
            raise RuntimeError("credential MCP 도구 등록 매트릭스가 일치하지 않습니다.")
        registered[name] = action
        return mcp.tool(name=name, description=description)

    def action_for(name: str) -> CredentialAction:
        action = registered.get(name)
        if action is None or CREDENTIAL_MCP_TOOL_ACTIONS.get(name) != action:
            raise RuntimeError("credential MCP 도구 등록 매트릭스가 일치하지 않습니다.")
        return action

    @tool(
        "issue_worker_credential",
        "worker_credential.issue",
        "현재 범위로 worker credential을 발급합니다.",
    )
    def issue_worker_credential(request: object = None) -> str:  # pyright: ignore[reportUnusedFunction]
        try:
            current = operations.value
            if current is None:
                return _json({"status": "unavailable"})
            _path, org_id, issue, _read, _revoke, clock = current
            if action_for("issue_worker_credential") != "worker_credential.issue":
                return _json({"status": "unavailable"})
            payload = _request(
                request,
                (
                    "target_id",
                    "credential_id",
                    "agent_card_id",
                    "owner_subject_id",
                    "role",
                    "request_id",
                    "attempt",
                ),
            )
            if (
                payload is None
                or not all(
                    type(payload[name]) is str
                    for name in (
                        "target_id",
                        "credential_id",
                        "agent_card_id",
                        "owner_subject_id",
                        "role",
                        "request_id",
                    )
                )
                or type(payload["attempt"]) is not int
            ):
                return _json({"status": "unavailable"})
            target_id = cast(str, payload["target_id"])
            credential_id = cast(str, payload["credential_id"])
            agent_card_id = cast(str, payload["agent_card_id"])
            owner_subject_id = cast(str, payload["owner_subject_id"])
            role = cast(str, payload["role"])
            request_id = cast(str, payload["request_id"])
            attempt = payload["attempt"]
            expires_at = None
            created_at = _timestamp(clock)
            if created_at is None:
                return _json({"status": "unavailable"})
            stage_key = sha256(
                f"{org_id}\x00{target_id}\x00{credential_id}\x00{request_id}\x00{attempt}".encode()
            ).hexdigest()
            result = issue.issue(
                ScopedCredentialIssueCommand(
                    org_id,
                    target_id,
                    credential_id,
                    agent_card_id,
                    owner_subject_id,
                    role,
                    request_id,
                    attempt,
                    expires_at,
                    stage_key,
                    created_at,
                )
            )
            if isinstance(result, Issued):
                return _json({"status": "issued", "credential_id": result.credential_id})
            if isinstance(result, ReleasePending):
                return _json({"status": "release_pending", "credential_id": result.credential_id})
            # Every other closed issue outcome is intentionally indistinguishable.
            return _json({"status": "unavailable"})
        except Exception:
            pass
        return _json({"status": "unavailable"})

    @tool(
        "list_worker_credentials",
        "worker_credential.read",
        "현재 범위의 worker credential을 조회합니다.",
    )
    def list_worker_credentials(request: object = None) -> str:  # pyright: ignore[reportUnusedFunction]
        try:
            current = operations.value
            if current is None:
                return _json({"status": "unavailable"})
            path, org_id, _issue, read, _revoke, _clock = current
            if action_for("list_worker_credentials") != "worker_credential.read":
                return _json({"status": "unavailable"})
            if _request(request, ()) is None:
                return _json({"status": "unavailable"})
            result = read.list(path, org_id)
            if isinstance(result, CredentialReadList):
                return _json(
                    {"status": "ok", "credentials": [_view(item) for item in result.credentials]}
                )
        except Exception:
            pass
        return _json({"status": "unavailable"})

    @tool("get_worker_credential", "worker_credential.read", "worker credential 하나를 조회합니다.")
    def get_worker_credential(request: object = None) -> str:  # pyright: ignore[reportUnusedFunction]
        try:
            current = operations.value
            if current is None:
                return _json({"status": "unavailable"})
            path, org_id, _issue, read, _revoke, _clock = current
            if action_for("get_worker_credential") != "worker_credential.read":
                return _json({"status": "unavailable"})
            payload = _request(request, ("credential_id",))
            if payload is None or type(payload["credential_id"]) is not str:
                return _json({"status": "unavailable"})
            credential_id = payload["credential_id"]
            result = read.get(path, org_id, credential_id)
            if isinstance(result, CredentialReadView):
                return _json({"status": "ok", "credential": _view(result)})
            if isinstance(result, CredentialReadNotFoundOrDenied):
                return _json({"status": "not_found_or_denied"})
        except Exception:
            pass
        return _json({"status": "unavailable"})

    @tool("revoke_worker_credential", "worker_credential.revoke", "worker credential을 철회합니다.")
    def revoke_worker_credential(request: object = None) -> str:  # pyright: ignore[reportUnusedFunction]
        try:
            current = operations.value
            if current is None:
                return _json({"status": "unavailable"})
            path, org_id, _issue, _read, revoke, _clock = current
            if action_for("revoke_worker_credential") != "worker_credential.revoke":
                return _json({"status": "unavailable"})
            payload = _request(
                request,
                (
                    "credential_id",
                    "command_id",
                    "attempt",
                    "expected_generation",
                    "expected_revision",
                ),
            )
            if (
                payload is None
                or not all(type(payload[name]) is str for name in ("credential_id", "command_id"))
                or not all(
                    type(payload[name]) is int
                    for name in ("attempt", "expected_generation", "expected_revision")
                )
            ):
                return _json({"status": "unavailable"})
            credential_id = cast(str, payload["credential_id"])
            command_id = cast(str, payload["command_id"])
            attempt = cast(int, payload["attempt"])
            expected_generation = cast(int, payload["expected_generation"])
            expected_revision = cast(int, payload["expected_revision"])
            result = revoke.revoke(
                path,
                org_id,
                credential_id,
                command=CredentialRevokeCommand(
                    command_id, attempt, expected_generation, expected_revision
                ),
            )
            if isinstance(result, CredentialRevokeResult):
                return _json({"status": "ok", "credential": _view(result.credential)})
            if isinstance(result, CredentialRevokeConflict):
                return _json({"status": "conflict"})
            if isinstance(result, CredentialReadNotFoundOrDenied):
                return _json({"status": "not_found_or_denied"})
        except Exception:
            pass
        return _json({"status": "unavailable"})

    # FastMCP owns these registered callables; this local reference makes that
    # ownership explicit to static analysis without exposing another surface.
    _registered_handlers = (
        issue_worker_credential,
        list_worker_credentials,
        get_worker_credential,
        revoke_worker_credential,
    )
    del _registered_handlers
    if tuple(registered.items()) != tuple(CREDENTIAL_MCP_TOOL_ACTIONS.items()):
        raise RuntimeError("credential MCP 도구 매트릭스가 완전하지 않습니다.")
    operations.value = capability._claim()  # pyright: ignore[reportPrivateUsage]
    return mcp
