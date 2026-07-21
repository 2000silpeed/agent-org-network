from datetime import date

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.registry import Registry, RegistryError
from agent_org_network.user import User


def make_card(
    *,
    agent_id: str = "contract_ops",
    owner: str = "D",
    domains: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="legal_ops",
        summary="계약 검토",
        domains=domains if domains is not None else ["계약 검토"],
        last_reviewed_at=date(2026, 6, 20),
    )


def test_유효한_카드를_등록하면_조회된다():
    registry = Registry()
    card = make_card()
    registry.register(card)
    assert registry.get("contract_ops") == card
    assert registry.get("contract_ops") is not card


def test_Registry_card_입출력은_mutable_alias를_backing과_공유하지_않는다() -> None:
    registry = Registry()
    registry.register_user(User(id="owner-1"))
    card = make_card(agent_id="safe-card", owner="owner-1", domains=["base"])
    registry.register(card)

    card.domains.append("forged-input")
    returned = registry.get("safe-card")
    returned.domains.append("forged-output")
    object.__setattr__(returned, "owner", "mallory")
    all_cards = registry.all_cards()
    all_cards[0].domains.append("forged-list")
    all_cards.clear()

    stored = registry.get("safe-card")
    assert stored.owner == "owner-1"
    assert stored.domains == ["base"]


def test_Registry_user_입출력은_object_setattr_alias를_backing과_공유하지_않는다() -> None:
    registry = Registry()
    user = User(id="owner-1")
    registry.register_user(user)

    object.__setattr__(user, "id", "forged-input")
    returned = registry.get_user("owner-1")
    object.__setattr__(returned, "id", "forged-output")
    users = registry.all_users()
    object.__setattr__(users[0], "id", "forged-list")
    users.clear()

    assert registry.get_user("owner-1") == User(id="owner-1")


def test_중복_agent_id_등록은_거부된다():
    registry = Registry()
    registry.register(make_card())
    with pytest.raises(RegistryError):
        registry.register(make_card())


def test_owner가_미등록_user면_validate가_실패한다():
    registry = Registry()
    registry.register(make_card(owner="ghost"))
    with pytest.raises(RegistryError):
        registry.validate()


def test_manager가_미등록_user면_validate가_실패한다():
    registry = Registry()
    registry.register_user(User(id="D", manager="ghost"))
    with pytest.raises(RegistryError):
        registry.validate()


def test_일관된_그래프는_validate를_통과한다():
    registry = Registry()
    registry.register_user(User(id="root"))
    registry.register_user(User(id="D", manager="root"))
    registry.register(make_card(owner="D"))
    registry.validate()
