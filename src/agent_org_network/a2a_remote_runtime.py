"""ADR 0074의 strict outbound A2A Remote Runtime 코어 경계.

공식 SDK·HTTP transport·profile loader는 이 모듈 바깥에서 ``A2AInvocationPort``에
주입한다. 따라서 여기에는 remote endpoint, Remote A2A Agent Card metadata, credential
원문을 Answer 또는 예외에 투영하는 경로가 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from typing import Literal, Protocol, TypeAlias
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_org_network.agent_card import AgentCard
from agent_org_network.owner_device_key_store import OwnerInstallationPublicBindingV1
from agent_org_network.runtime import AgentRuntime, Answer


_SHA256 = re.compile(r"[0-9a-f]{64}")
_OPAQUE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_LITERAL_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@-]{1,64}")

_MAX_TIMEOUT_SECONDS = 120
_MAX_RESPONSE_BYTES = 1_048_576
_MIN_RESPONSE_BYTES = 1_024
_MAX_SERVICE_PATH_LENGTH = 512
_MAX_SERVICE_PATH_SEGMENTS = 16


def _canonical_https_service_endpoint(value: str) -> str:
    """canonical HTTPS origin과 optional bounded literal path인지 확인한다.

    path는 percent-encoding이나 정규화가 필요한 segment를 허용하지 않는다. Remote A2A
    Agent Card가 선언한 interface URL과 profile이 한 canonical 문자열로 exact 비교될 수
    있게 하기 위함이다. DNS/SSRF 확인은 실제 connect 직전 adapter 책임이다.
    """
    if (
        type(value) is not str
        or value != value.strip()
        or len(value) > 2_048
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or "\\" in value
        or "%" in value
    ):
        raise ValueError("canonical HTTPS service endpoint required")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.hostname is None
        ):
            raise ValueError("canonical HTTPS service endpoint required")
        path = parsed.path
        if path:
            segments = path[1:].split("/") if path.startswith("/") else []
            if (
                len(path) > _MAX_SERVICE_PATH_LENGTH
                or path.endswith("/")
                or not 1 <= len(segments) <= _MAX_SERVICE_PATH_SEGMENTS
                or any(
                    segment in {".", ".."} or _LITERAL_PATH_SEGMENT.fullmatch(segment) is None
                    for segment in segments
                )
            ):
                raise ValueError("canonical HTTPS service endpoint required")
        raw_host = parsed.hostname
        if "%" in raw_host or "_" in raw_host or raw_host.endswith("."):
            raise ValueError("canonical HTTPS service endpoint required")
        try:
            address = ipaddress.ip_address(raw_host)
            host = address.compressed
            rendered_host = f"[{host}]" if address.version == 6 else host
        except ValueError:
            labels = raw_host.split(".")
            if any(not label for label in labels):
                raise ValueError("canonical HTTPS service endpoint required") from None
            host = raw_host.encode("idna").decode("ascii").lower()
            if any(
                re.fullmatch(r"(?!-)[a-z0-9-]{1,63}(?<!-)", label) is None
                for label in host.split(".")
            ):
                raise ValueError("canonical HTTPS service endpoint required")
            rendered_host = host
        port = parsed.port
        suffix = "" if port is None or port == 443 else f":{port}"
        canonical = f"https://{rendered_host}{suffix}{path}"
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("canonical HTTPS service endpoint required") from error
    if canonical != value:
        raise ValueError("canonical HTTPS service endpoint required")
    return value


class A2ARemoteRuntimeProfile(BaseModel, frozen=True):
    """Card Owner local profile의 transport 선택 값 객체.

    profile 자체는 credential 원문이 아니라 local secret store의 opaque reference만
    보관한다. active binding exact 비교/loader는 owner composition root의 후속 책임이다.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    binding: OwnerInstallationPublicBindingV1
    runtime_kind: Literal["a2a_remote"] = "a2a_remote"
    service_endpoint: str
    remote_card_path: Literal["/.well-known/agent-card.json"] = "/.well-known/agent-card.json"
    remote_card_sha256: str
    protocol_version: Literal["1.0"] = "1.0"
    protocol_binding: Literal["HTTP+JSON"] = "HTTP+JSON"
    credential_ref: str
    timeout_seconds: int = Field(default=30, ge=1, le=_MAX_TIMEOUT_SECONDS)
    max_response_bytes: int = Field(
        default=_MAX_RESPONSE_BYTES, ge=_MIN_RESPONSE_BYTES, le=_MAX_RESPONSE_BYTES
    )

    @field_validator("service_endpoint")
    @classmethod
    def _service_endpoint(cls, value: str) -> str:
        return _canonical_https_service_endpoint(value)

    @field_validator("remote_card_sha256")
    @classmethod
    def _remote_card_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value

    @field_validator("credential_ref")
    @classmethod
    def _credential_ref(cls, value: str) -> str:
        if _OPAQUE_REFERENCE.fullmatch(value) is None:
            raise ValueError("opaque credential reference required")
        return value


@dataclass(frozen=True, slots=True)
class A2ACompletedText:
    """strict A2A 1.0 REST terminal completed의 text-only 결과."""

    text: str


@dataclass(frozen=True, slots=True)
class A2ARemoteRejected:
    """remote가 요청을 거절했다. 세부 remote reason은 이 경계를 넘지 않는다."""


@dataclass(frozen=True, slots=True)
class A2ARemoteUnavailable:
    """transport/endpoint/authentication 등 remote 도달 실패."""


@dataclass(frozen=True, slots=True)
class A2AProtocolViolation:
    """strict v1 completed text-only profile을 만족하지 않는 remote 결과."""


A2AInvocationOutcome: TypeAlias = (
    A2ACompletedText | A2ARemoteRejected | A2ARemoteUnavailable | A2AProtocolViolation
)
A2ARemoteRuntimeFailureCode: TypeAlias = Literal[
    "a2a_binding_mismatch",
    "a2a_remote_rejected",
    "a2a_remote_unavailable",
    "a2a_protocol_violation",
]


class A2AInvocationPort(Protocol):
    """SDK/HTTP adapter가 구현하는 narrow outbound invocation port."""

    def invoke(
        self,
        *,
        profile: A2ARemoteRuntimeProfile,
        question: str,
        context: str | None,
    ) -> A2AInvocationOutcome: ...


class A2ARemoteRuntimeFailure(RuntimeError):
    """remote details를 보존하지 않는 typed, redacted runtime failure."""

    code: A2ARemoteRuntimeFailureCode

    def __init__(self, code: A2ARemoteRuntimeFailureCode) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"A2ARemoteRuntimeFailure(code='{self.code}')"


class A2ARemoteRuntime(AgentRuntime):
    """A2A completed text-only만 기존 ``AgentRuntime`` Answer로 투영한다."""

    def __init__(self, profile: A2ARemoteRuntimeProfile, invocation: A2AInvocationPort) -> None:
        self._profile = profile
        self._invocation = invocation

    def answer(
        self,
        question: str,
        card: AgentCard,
        context: str | None = None,
        grounding: str | None = None,
    ) -> Answer:
        """question/context만 remote port로 보내고, Answer metadata는 로컬에서 고정한다."""
        del grounding
        if card.agent_id != self._profile.binding.agent_card_id:
            raise A2ARemoteRuntimeFailure("a2a_binding_mismatch")
        outcome = self._invocation.invoke(
            profile=self._profile,
            question=question,
            context=context,
        )
        if isinstance(outcome, A2ACompletedText):
            if not outcome.text.strip() or len(outcome.text.encode("utf-8")) > self._profile.max_response_bytes:
                raise A2ARemoteRuntimeFailure("a2a_protocol_violation")
            return Answer(text=outcome.text, sources=(), mode="full", snapshot_sha=None)
        if isinstance(outcome, A2ARemoteRejected):
            raise A2ARemoteRuntimeFailure("a2a_remote_rejected")
        if isinstance(outcome, A2ARemoteUnavailable):
            raise A2ARemoteRuntimeFailure("a2a_remote_unavailable")
        raise A2ARemoteRuntimeFailure("a2a_protocol_violation")
