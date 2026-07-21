"""ADR 0059 authority boundary public shapes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from agent_org_network.reciprocal_review import SourceBindingAuthorizationEnvelopeV7


@dataclass(frozen=True)
class SourceBindingAuthorizationDenied:
    reason_code: str


@dataclass(frozen=True)
class SourceBindingAuthorizationUnavailable:
    reason_code: str


class SourceBindingAuthorizationAuthority(Protocol):
    def issue_intent(self, *, cycle_id: str, source_ref: str, db_now: datetime) -> SourceBindingAuthorizationEnvelopeV7 | SourceBindingAuthorizationDenied | SourceBindingAuthorizationUnavailable: ...
