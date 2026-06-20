from agent_org_network.agent_card import AgentCard
from agent_org_network.user import User


class RegistryError(Exception):
    pass


class Registry:
    def __init__(self) -> None:
        self._cards: dict[str, AgentCard] = {}
        self._users: dict[str, User] = {}

    def register(self, card: AgentCard) -> None:
        if card.agent_id in self._cards:
            raise RegistryError(f"중복 agent_id: {card.agent_id}")
        self._cards[card.agent_id] = card

    def register_user(self, user: User) -> None:
        if user.id in self._users:
            raise RegistryError(f"중복 user id: {user.id}")
        self._users[user.id] = user

    def get(self, agent_id: str) -> AgentCard:
        return self._cards[agent_id]

    def validate(self) -> None:
        for card in self._cards.values():
            if card.owner not in self._users:
                raise RegistryError(f"미등록 owner: {card.owner} (agent {card.agent_id})")
        for user in self._users.values():
            if user.manager is not None and user.manager not in self._users:
                raise RegistryError(f"미등록 manager: {user.manager} (user {user.id})")
