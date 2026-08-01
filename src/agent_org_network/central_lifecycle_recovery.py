"""Production-only Central lifecycle routing and durable recovery adapters.

The adapters deliberately use the Central Registry/Card catalog and the
Authority policy file.  They do not import demo composition, an Owner Runtime,
or an A2A transport.
"""

from __future__ import annotations

from pathlib import Path
import json
import sqlite3
from typing import cast
from unicodedata import normalize

from agent_org_network.agent_card import AgentCard
from agent_org_network.central_authority import (
    load_authority_policy_yaml,
)
from agent_org_network.central_operational_evidence import (
    canonical_v19_file_authority,
)
from agent_org_network.central_question_lifecycle import (
    CentralQuestionLifecycleApplication,
    CentralQuestionLifecycleUnavailable,
    RouteAuthorization,
)
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.sqlite_production_agent_cards import validate_production_agent_card_rows
from agent_org_network.sqlite_production_registry_users import validate_production_registry_user_rows


class CentralPolicyRouter:
    """Rule-authorized, Card-validated deterministic production Router baseline."""

    def __init__(self, *, database_path: Path, authority_policy_path: Path, org_id: str) -> None:
        self._database_path = database_path
        self._policy_path = authority_policy_path
        self._org_id = org_id

    def route(self, question: str) -> Routed | Contested | Unowned:
        try:
            snapshot = load_authority_policy_yaml(
                self._policy_path.read_text(encoding="utf-8"), expected_org_id=self._org_id
            )
            connection = sqlite3.connect(self._database_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                root = _root_user(connection, self._org_id)
                normalized = _normal(question)
                rules = tuple(rule for rule in snapshot.route_rules if _normal(rule.intent) in normalized)
                if not rules:
                    return Unowned(escalated_to=root, reason="approved route 없음", intent="")
                intent = rules[0].intent
                if any(rule.intent != intent for rule in rules):
                    # The exact intent itself is never caller-controlled; a
                    # multi-rule phrase is conservatively contested.
                    candidates = _cards(connection, self._org_id, rules)
                    if len(candidates) >= 2:
                        return Contested(candidates=candidates, reason="복수 승인 route", intent=intent)
                    return Unowned(escalated_to=root, reason="모호한 승인 route", intent=intent)
                cards = list(_cards(connection, self._org_id, rules))
                if not cards:
                    return Unowned(escalated_to=root, reason="승인 route Card 없음", intent=intent)
                if len(cards) > 1:
                    return Contested(candidates=tuple(cards), reason="복수 승인 Card", intent=intent)
                card = cards[0]
                return Routed(
                    primary=card, intent=intent, reason="central approved route",
                    requires_approval=intent in card.approval_when,
                )
            finally:
                connection.close()
        except CentralQuestionLifecycleUnavailable:
            raise
        except Exception as error:
            raise CentralQuestionLifecycleUnavailable("Central policy router unavailable") from error


class FileReloadingLifecycleRouteAuthority:
    """Current policy adapter for lifecycle route/last-resort Manager checks."""

    def __init__(self, *, authority_policy_path: Path, org_id: str) -> None:
        self._policy_path = authority_policy_path
        self._org_id = org_id

    def authorize_route(
        self, org_id: str, intent: str, agent_id: str, transaction: sqlite3.Connection | None
    ) -> RouteAuthorization | None:
        _ = transaction
        try:
            snapshot = self._snapshot(org_id)
            if any(rule.intent == intent and rule.agent_card_id == agent_id for rule in snapshot.route_rules):
                return self._authorization(snapshot.content_sha256)
        except Exception:
            return None
        return None

    def authorize_manager(
        self, org_id: str, manager_id: str, transaction: sqlite3.Connection | None
    ) -> RouteAuthorization | None:
        _ = transaction
        try:
            snapshot = self._snapshot(org_id)
            roles = next((binding.roles for binding in snapshot.subject_roles if binding.subject_id == manager_id), ())
            permissions = {permission.role: permission.actions for permission in snapshot.role_permissions}
            if any("manager.act" in permissions.get(role, ()) for role in roles):
                return self._authorization(snapshot.content_sha256)
        except Exception:
            return None
        return None

    def current_authority(
        self, transaction: sqlite3.Connection | None
    ) -> RouteAuthorization:
        """Reload the exact current file Authority for system-only transitions."""
        _ = transaction
        snapshot = self._snapshot(self._org_id)
        return self._authorization(snapshot.content_sha256)

    @staticmethod
    def _authorization(policy_digest: str) -> RouteAuthorization:
        authority = canonical_v19_file_authority(
            source_policy_digest=policy_digest,
            current_snapshot_digest=policy_digest,
        )
        return RouteAuthorization(
            policy_revision_id=authority.policy_revision_id,
            policy_epoch=authority.policy_epoch,
            policy_digest=authority.policy_digest,
        )

    def _snapshot(self, org_id: str):
        if org_id != self._org_id:
            raise CentralQuestionLifecycleUnavailable("cross-org route authority")
        return load_authority_policy_yaml(self._policy_path.read_text(encoding="utf-8"), expected_org_id=self._org_id)


class CentralLifecycleRecoveryCoordinator:
    """Resumes durable Received rows after create and on Central API startup."""

    def __init__(self, *, database_path: Path, lifecycle: CentralQuestionLifecycleApplication) -> None:
        self._database_path = database_path
        self._lifecycle = lifecycle

    def recover_one(self, request_id: str) -> None:
        try:
            self._lifecycle.process_received(request_id)
        except CentralQuestionLifecycleUnavailable:
            # The receipt is durable and the next startup/retry repeats this
            # recovery; an HTTP create receipt must never be rolled back.
            return None

    def recover_pending(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self._database_path)
            connection.execute("PRAGMA foreign_keys=ON")
            rows = connection.execute("SELECT request_id,state_json FROM question_requests ORDER BY created_at,request_id").fetchall()
            for request_id, state_json in rows:
                state = json.loads(str(state_json))
                if isinstance(state, dict) and cast(dict[str, object], state).get("kind") == "received":
                    self.recover_one(str(request_id))
        except Exception:
            return None
        finally:
            if connection is not None:
                connection.close()


def _cards(connection: sqlite3.Connection, org_id: str, rules: tuple[object, ...]) -> tuple[AgentCard, ...]:
    # Validate the canonical Card capability, including Registry-revision
    # companions, before a raw Card row becomes a route target.
    validate_production_agent_card_rows(connection, org_id)
    identifiers = {str(getattr(rule, "agent_card_id")) for rule in rules}
    cards: list[AgentCard] = []
    for row in connection.execute("SELECT * FROM production_agent_cards WHERE org_id=? ORDER BY agent_id", (org_id,)):
        if str(row["agent_id"]) not in identifiers:
            continue
        card = AgentCard.model_validate(json.loads(str(row["card_json"])))
        if card.agent_id == str(row["agent_id"]) and card.owner == str(row["owner_id"]):
            cards.append(card)
    return tuple(cards)


def _root_user(connection: sqlite3.Connection, org_id: str) -> str:
    roots = tuple(str(row[0]) for row in connection.execute(
        "SELECT user_id FROM production_registry_users WHERE org_id=? AND manager_id IS NULL ORDER BY user_id", (org_id,)
    ))
    if len(roots) != 1:
        raise CentralQuestionLifecycleUnavailable("canonical root unavailable")
    return validate_production_registry_user_rows(connection, org_id, roots[0]).user_id


def _normal(value: str) -> str:
    return " ".join(normalize("NFKC", value).casefold().split())
