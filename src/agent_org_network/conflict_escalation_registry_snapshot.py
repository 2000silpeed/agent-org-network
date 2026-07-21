"""S4.3c.0 graph-aware Registry snapshot for future Conflict escalation.

This is deliberately independent of the Conflict-open reader.  It creates no
receipt, authority action, Manager queue item, or durable write.  Its only
responsibility is proving the current candidate and Owner graph under one
Registry consistency guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Protocol, Sequence

from agent_org_network.agent_card import domain_authorized
from agent_org_network.conflict_open_contract import (
    ConflictOpenCandidateClaim,
    ConflictOpenSnapshotCandidate,
    conflict_open_candidate_digest,
    conflict_open_claim_digest,
)
from agent_org_network.question_request import RouteTarget
from agent_org_network.registry import Registry


class ConflictEscalationRegistrySnapshotError(RuntimeError):
    """The scoped candidate/Owner graph cannot safely prove escalation."""


class ConflictEscalationRegistryOrgScopeAdapter(Protocol):
    """Composition-owned proof that a Registry serves exactly one organization."""

    def proves_registry_org(self, *, registry: Registry, org_id: str) -> bool: ...


@dataclass(frozen=True)
class ConflictEscalationSnapshotCandidate:
    ordinal: int
    card_ref: str
    owner_subject_ref: str
    domain_ref: str
    route_sha256: str
    under_claim: bool


@dataclass(frozen=True)
class ConflictEscalationOwnerPath:
    owner_subject_ref: str
    path_subject_refs: tuple[str, ...]


@dataclass(frozen=True)
class ConflictEscalationRegistrySnapshot:
    org_id: str
    candidates: tuple[ConflictEscalationSnapshotCandidate, ...]
    owner_paths: tuple[ConflictEscalationOwnerPath, ...]
    manager_subject_ref: str | None
    root_subject_ref: str
    candidate_digest: str
    claim_digest: str
    graph_digest: str


class ConflictEscalationRegistrySnapshotReader(Protocol):
    def snapshot(
        self, *, org_id: str, claims: Sequence[ConflictOpenCandidateClaim]
    ) -> ConflictEscalationRegistrySnapshot: ...

    def verify_current(
        self,
        snapshot: ConflictEscalationRegistrySnapshot,
        *,
        claims: Sequence[ConflictOpenCandidateClaim],
    ) -> None: ...


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{sha256(value.encode('utf-8')).hexdigest()}"


def _route(route: RouteTarget) -> dict[str, object]:
    return route.model_dump(mode="json")


def _claims(claims: Sequence[ConflictOpenCandidateClaim]) -> tuple[ConflictOpenCandidateClaim, ...]:
    values = tuple(claims)
    try:
        # Reuse only the immutable input grammar/digest, never the legacy graph.
        conflict_open_claim_digest(values)
    except Exception as error:
        raise ConflictEscalationRegistrySnapshotError(
            "ordered Conflict escalation claim이 exact하지 않습니다."
        ) from error
    return values


def _graph_digest(
    paths: tuple[ConflictEscalationOwnerPath, ...],
    *,
    manager_subject_ref: str | None,
    root_subject_ref: str,
) -> str:
    return _sha(
        {
            "owner_paths": [
                {
                    "owner_subject_ref": value.owner_subject_ref,
                    "path_subject_refs": value.path_subject_refs,
                }
                for value in paths
            ],
            "manager_subject_ref": manager_subject_ref,
            "root_subject_ref": root_subject_ref,
        }
    )


@dataclass(frozen=True)
class RegistryConflictEscalationSnapshotReader:
    """One-guard typed candidate and Owner-graph proof for S4.3c only."""

    registry: Registry
    org_scope_adapter: ConflictEscalationRegistryOrgScopeAdapter

    def snapshot(
        self, *, org_id: str, claims: Sequence[ConflictOpenCandidateClaim]
    ) -> ConflictEscalationRegistrySnapshot:
        canonical_claims = _claims(claims)
        if not org_id.strip():
            raise ConflictEscalationRegistrySnapshotError(
                "Conflict escalation org_id가 필요합니다."
            )
        try:
            with self.registry.consistency_guard():
                if not self.org_scope_adapter.proves_registry_org(
                    registry=self.registry, org_id=org_id
                ):
                    raise ConflictEscalationRegistrySnapshotError(
                        "Conflict escalation Registry 조직 scope proof가 없습니다."
                    )
                candidates = tuple(
                    self._candidate(index, claim)
                    for index, claim in enumerate(canonical_claims, start=1)
                )
                if not all(candidate.under_claim for candidate in candidates):
                    raise ConflictEscalationRegistrySnapshotError(
                        "current Conflict escalation under-claim proof가 없습니다."
                    )
                owners = tuple(candidate.owner_subject_ref for candidate in candidates)
                if len(set(owners)) != len(owners):
                    raise ConflictEscalationRegistrySnapshotError(
                        "Conflict escalation candidate Owner는 유일해야 합니다."
                    )
                paths = tuple(self._path(owner_ref) for owner_ref in owners)
                manager_ref, root_ref = self._selection(paths)
                # This exact digest is the immutable ingress/Case candidate
                # identity.  The new graph DTO itself only exposes typed refs.
                ingress_candidate_digest = conflict_open_candidate_digest(
                    tuple(
                        ConflictOpenSnapshotCandidate(
                            card_id=claim.card_id,
                            owner_subject_id=self.registry.get(claim.card_id).owner,
                            intent=claim.intent,
                            under_claim=candidate.under_claim,
                            route=claim.route,
                        )
                        for claim, candidate in zip(canonical_claims, candidates, strict=True)
                    )
                )
        except ConflictEscalationRegistrySnapshotError:
            raise
        except Exception as error:
            raise ConflictEscalationRegistrySnapshotError(
                "current Conflict escalation Registry graph을 읽을 수 없습니다."
            ) from error
        return ConflictEscalationRegistrySnapshot(
            org_id=org_id,
            candidates=candidates,
            owner_paths=paths,
            manager_subject_ref=manager_ref,
            root_subject_ref=root_ref,
            candidate_digest=ingress_candidate_digest,
            claim_digest=conflict_open_claim_digest(canonical_claims),
            graph_digest=_graph_digest(
                paths, manager_subject_ref=manager_ref, root_subject_ref=root_ref
            ),
        )

    def verify_current(
        self,
        snapshot: ConflictEscalationRegistrySnapshot,
        *,
        claims: Sequence[ConflictOpenCandidateClaim],
    ) -> None:
        if type(snapshot) is not ConflictEscalationRegistrySnapshot:
            raise ConflictEscalationRegistrySnapshotError(
                "Conflict escalation snapshot type이 올바르지 않습니다."
            )
        if self.snapshot(org_id=snapshot.org_id, claims=claims) != snapshot:
            raise ConflictEscalationRegistrySnapshotError(
                "Conflict escalation Registry/claim/Owner graph snapshot이 변경됐습니다."
            )

    def _candidate(
        self, ordinal: int, claim: ConflictOpenCandidateClaim
    ) -> ConflictEscalationSnapshotCandidate:
        card = self.registry.get(claim.card_id)
        # Both reads happen under the outer guard.  The User lookup rejects a
        # dangling Card owner instead of inferring an escalation target.
        self.registry.get_user(card.owner)
        if (
            card.agent_id != claim.card_id
            or claim.route.agent_id != claim.card_id
            or claim.route.intent != claim.intent
        ):
            raise ConflictEscalationRegistrySnapshotError(
                "Conflict escalation Card/Route/claim proof가 다릅니다."
            )
        return ConflictEscalationSnapshotCandidate(
            ordinal=ordinal,
            card_ref=_ref("card", card.agent_id),
            owner_subject_ref=_ref("subject", card.owner),
            domain_ref=_ref("domain", claim.intent),
            route_sha256=_sha(_route(claim.route)),
            under_claim=domain_authorized(claim.intent, card),
        )

    def _path(self, owner_ref: str) -> ConflictEscalationOwnerPath:
        # The raw User ID exists only inside this guard and is never emitted.
        owner_id = self._user_id_for_ref(owner_ref)
        visited: set[str] = set()
        path: list[str] = []
        current = owner_id
        while True:
            if current in visited:
                raise ConflictEscalationRegistrySnapshotError(
                    "Conflict escalation Owner manager graph에 cycle이 있습니다."
                )
            visited.add(current)
            user = self.registry.get_user(current)
            if user.manager is None:
                path.append(_ref("subject", current))
                return ConflictEscalationOwnerPath(owner_ref, tuple(path))
            if user.manager == current:
                raise ConflictEscalationRegistrySnapshotError(
                    "Conflict escalation Owner manager graph에 self-loop가 있습니다."
                )
            path.append(_ref("subject", user.manager))
            current = user.manager

    def _user_id_for_ref(self, subject_ref: str) -> str:
        # Registry does not index typed refs.  The candidate set is small and
        # enumerated under the same guard; no raw ID leaves this helper.
        for user in self.registry.all_users():
            if _ref("subject", user.id) == subject_ref:
                return user.id
        raise ConflictEscalationRegistrySnapshotError("Conflict escalation Owner가 없습니다.")

    @staticmethod
    def _selection(
        paths: tuple[ConflictEscalationOwnerPath, ...],
    ) -> tuple[str | None, str]:
        roots = {path.path_subject_refs[-1] for path in paths}
        if len(roots) != 1:
            raise ConflictEscalationRegistrySnapshotError(
                "Conflict escalation Manager/root가 유일하지 않습니다."
            )
        root = next(iter(roots))
        common = set(paths[0].path_subject_refs)
        for path in paths[1:]:
            common.intersection_update(path.path_subject_refs)
        if not common:
            raise ConflictEscalationRegistrySnapshotError(
                "Conflict escalation common Manager/root가 없습니다."
            )
        managers = common - {root}
        if not managers:
            return None, root
        # A common Manager is the one nearest every candidate Owner.  A tie
        # has no deterministic graph proof and deliberately remains unavailable.
        distances = {
            manager: max(path.path_subject_refs.index(manager) for path in paths)
            for manager in managers
        }
        nearest = min(distances.values())
        selected = [manager for manager, distance in distances.items() if distance == nearest]
        if len(selected) != 1:
            raise ConflictEscalationRegistrySnapshotError(
                "Conflict escalation Manager 선택이 유일하지 않습니다."
            )
        return selected[0], root
