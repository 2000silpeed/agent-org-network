"""Read-only S4.3a.0 Conflict-open authority/Registry snapshot contract.

No Conflict Case, baseline, Manager/root selection, or persistence is activated
here.  A later ingress UoW consumes this snapshot and calls ``verify_current``
again immediately before its write.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Final, Protocol, Sequence

from agent_org_network.agent_card import domain_authorized
from agent_org_network.central_authority import ResourceRef
from agent_org_network.question_request import RouteTarget
from agent_org_network.registry import Registry

CONFLICT_OPEN_ACTION: Final = "conflict.open"
CONFLICT_OPEN_RESOURCE_KIND: Final = "question_request"
CONFLICT_OPEN_MANAGER_SELECTION_AVAILABLE: Final = False
CONFLICT_OPEN_ROOT_SELECTION_AVAILABLE: Final = False
# Registry itself has no organization field.  It cannot prove scope without a
# composition adapter; callers must never treat the requested org_id as proof.
CONFLICT_OPEN_REGISTRY_ORG_BINDING_AVAILABLE: Final = False


class ConflictOpenContractError(RuntimeError):
    """Contract violation intentionally does not disclose Registry details."""


@dataclass(frozen=True)
class ConflictOpenCandidateClaim:
    card_id: str
    intent: str
    route: RouteTarget


@dataclass(frozen=True)
class ConflictOpenSnapshotCandidate:
    card_id: str
    owner_subject_id: str
    intent: str
    under_claim: bool
    route: RouteTarget


@dataclass(frozen=True)
class ConflictOpenRegistrySnapshot:
    org_id: str
    candidates: tuple[ConflictOpenSnapshotCandidate, ...]
    candidate_digest: str
    claim_digest: str


class ConflictOpenRegistrySnapshotReader(Protocol):
    def snapshot(
        self, *, org_id: str, claims: Sequence[ConflictOpenCandidateClaim]
    ) -> ConflictOpenRegistrySnapshot: ...

    def verify_current(
        self,
        snapshot: ConflictOpenRegistrySnapshot,
        *,
        claims: Sequence[ConflictOpenCandidateClaim],
    ) -> None: ...


class ConflictOpenRegistryOrgScopeAdapter(Protocol):
    """Composition-owned proof that this Registry is bound to exactly ``org_id``."""

    def proves_registry_org(self, *, registry: Registry, org_id: str) -> bool: ...


def conflict_open_resource(
    *, org_id: str, request_id: str, requester_subject_id: str
) -> ResourceRef:
    return ResourceRef(
        org_id=org_id,
        kind=CONFLICT_OPEN_RESOURCE_KIND,
        resource_id=request_id,
        owner_subject_id=requester_subject_id,
    )


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _route(route: RouteTarget) -> dict[str, object]:
    return route.model_dump(mode="json")


def _claims(claims: Sequence[ConflictOpenCandidateClaim]) -> tuple[ConflictOpenCandidateClaim, ...]:
    canonical = tuple(claims)
    if not canonical or any(type(value) is not ConflictOpenCandidateClaim for value in canonical):
        raise ConflictOpenContractError("ordered Conflict-open candidate claim이 필요합니다.")
    if any(
        not value.card_id.strip()
        or not value.intent.strip()
        or type(value.route) is not RouteTarget
        or value.route.agent_id != value.card_id
        or value.route.intent != value.intent
        for value in canonical
    ):
        raise ConflictOpenContractError(
            "Conflict-open Card/intent/RouteTarget claim이 exact하지 않습니다."
        )
    if len({value.card_id for value in canonical}) != len(canonical):
        raise ConflictOpenContractError("Conflict-open candidate Card는 유일해야 합니다.")
    return canonical


def conflict_open_claim_digest(claims: Sequence[ConflictOpenCandidateClaim]) -> str:
    return _digest(
        {
            "claims": [
                {"card_id": value.card_id, "intent": value.intent, "route": _route(value.route)}
                for value in _claims(claims)
            ]
        }
    )


def conflict_open_candidate_digest(candidates: Sequence[ConflictOpenSnapshotCandidate]) -> str:
    canonical = tuple(candidates)
    if not canonical or any(
        type(value) is not ConflictOpenSnapshotCandidate for value in canonical
    ):
        raise ConflictOpenContractError("current Conflict-open candidate snapshot이 필요합니다.")
    if not all(value.under_claim for value in canonical):
        raise ConflictOpenContractError("under-claim=true current evidence가 필요합니다.")
    return _digest(
        {
            "candidates": [
                {
                    "card_id": value.card_id,
                    "owner_subject_id": value.owner_subject_id,
                    "intent": value.intent,
                    "under_claim": value.under_claim,
                    "route": _route(value.route),
                }
                for value in canonical
            ]
        }
    )


@dataclass(frozen=True)
class RegistryConflictOpenSnapshotReader:
    """Uses one short Registry consistency guard; no lock crosses a UoW boundary."""

    registry: Registry
    org_scope_adapter: ConflictOpenRegistryOrgScopeAdapter

    def snapshot(
        self, *, org_id: str, claims: Sequence[ConflictOpenCandidateClaim]
    ) -> ConflictOpenRegistrySnapshot:
        canonical = _claims(claims)
        if not org_id.strip():
            raise ConflictOpenContractError("Conflict-open org_id가 필요합니다.")
        try:
            with self.registry.consistency_guard():
                if not self.org_scope_adapter.proves_registry_org(
                    registry=self.registry, org_id=org_id
                ):
                    raise ConflictOpenContractError("Registry 조직 scope proof가 없습니다.")
                candidates: list[ConflictOpenSnapshotCandidate] = []
                for claim in canonical:
                    card = self.registry.get(claim.card_id)
                    self.registry.get_user(card.owner)
                    candidates.append(
                        ConflictOpenSnapshotCandidate(
                            claim.card_id,
                            card.owner,
                            claim.intent,
                            domain_authorized(claim.intent, card),
                            claim.route,
                        )
                    )
        except ConflictOpenContractError:
            raise
        except Exception as error:
            raise ConflictOpenContractError(
                "current Conflict-open Registry를 읽을 수 없습니다."
            ) from error
        frozen = tuple(candidates)
        if not all(value.under_claim for value in frozen):
            raise ConflictOpenContractError("current Card under-claim이 아닙니다.")
        if len({value.owner_subject_id for value in frozen}) != len(frozen):
            raise ConflictOpenContractError("Conflict-open candidate Owner는 유일해야 합니다.")
        return ConflictOpenRegistrySnapshot(
            org_id,
            frozen,
            conflict_open_candidate_digest(frozen),
            conflict_open_claim_digest(canonical),
        )

    def verify_current(
        self,
        snapshot: ConflictOpenRegistrySnapshot,
        *,
        claims: Sequence[ConflictOpenCandidateClaim],
    ) -> None:
        if type(snapshot) is not ConflictOpenRegistrySnapshot:
            raise ConflictOpenContractError("Conflict-open snapshot type이 올바르지 않습니다.")
        if self.snapshot(org_id=snapshot.org_id, claims=claims) != snapshot:
            raise ConflictOpenContractError("Conflict-open Registry/claim snapshot이 변경됐습니다.")
