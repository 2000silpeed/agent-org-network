from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, cast

from agent_org_network.agent_card import AgentCard
from agent_org_network.user import User


class RegistryError(Exception):
    pass


class Registry:
    """카드·유저 레지스트리.

    동시성: web.py 엔드포인트가 def(비 async)라 스레드풀에서 병렬 실행된다.
    register/register_user의 중복 체크(if in dict)와 쓰기 사이를 `_lock`(RLock)
    으로 직렬화한다 — 동시 등록 시 같은 agent_id/user id가 이중 등록되지 않게
    한다("유효하지 않은 카드는 등록되지 않는다" 불변식의 동시성 보장, 공개
    시그니처·반환값·예외는 불변).
    """

    def __init__(self) -> None:
        self._cards: dict[str, AgentCard] = {}
        self._users: dict[str, User] = {}
        self._lock = threading.RLock()

    def user_ids(self) -> frozenset[str]:
        """등록된 User id 집합(읽기 전용)."""
        return frozenset(self._users.keys())

    def all_users(self) -> list[User]:
        """등록된 User 목록(읽기 전용)."""
        return list(self._users.values())

    def get_user(self, user_id: str) -> User:
        """user_id로 User를 조회한다."""
        return self._users[user_id]

    def register(self, card: AgentCard) -> None:
        with self._lock:
            if card.agent_id in self._cards:
                raise RegistryError(f"중복 agent_id: {card.agent_id}")
            self._cards[card.agent_id] = card

    def register_user(self, user: User) -> None:
        with self._lock:
            if user.id in self._users:
                raise RegistryError(f"중복 user id: {user.id}")
            self._users[user.id] = user

    def get(self, agent_id: str) -> AgentCard:
        return self._cards[agent_id]

    def all_cards(self) -> list[AgentCard]:
        return list(self._cards.values())

    def validate(self) -> None:
        for card in self._cards.values():
            if card.owner not in self._users:
                raise RegistryError(f"미등록 owner: {card.owner} (agent {card.agent_id})")
        for user in self._users.values():
            if user.manager is not None and user.manager not in self._users:
                raise RegistryError(f"미등록 manager: {user.manager} (user {user.id})")

    def load(self, directory: Path) -> None:
        """directory에서 users.yaml과 agents/*.yaml을 읽어 Registry에 등록한다.

        로드 순서: 유저 먼저(그래프) → 카드(owner 참조 무결성). validate()는
        호출자 책임(로드 후 반드시 호출해 무결성 확인).

        users.yaml 스키마:
            users:
              - id: <str>
                manager: <str>  # optional

        agents/*.yaml 스키마: AgentCard 필드 그대로(pydantic model_validate).
        """
        import yaml  # PyYAML — 이미 설치돼 있음(uv에서 확인)

        users_path = directory / "users.yaml"
        if users_path.exists():
            raw: Any = yaml.safe_load(users_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or "users" not in raw:
                raise RegistryError(f"users.yaml 형식 오류: 'users' 키 없음 ({users_path})")
            for entry in cast(list[Any], raw["users"]):
                if not isinstance(entry, dict):
                    raise RegistryError(f"users.yaml 항목 형식 오류: {entry!r}")
                entry_dict: dict[str, Any] = cast(dict[str, Any], entry)
                try:
                    self.register_user(User.model_validate(entry_dict))
                except Exception as exc:
                    raise RegistryError(f"유저 로드 실패 ({entry!r}): {exc}") from exc

        agents_dir = directory / "agents"
        if agents_dir.is_dir():
            for yaml_path in sorted(agents_dir.glob("*.yaml")):
                raw_card: Any = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                if not isinstance(raw_card, dict):
                    raise RegistryError(f"카드 YAML 형식 오류: {yaml_path}")
                try:
                    card = AgentCard.model_validate(raw_card)
                except Exception as exc:
                    raise RegistryError(f"카드 로드 실패 ({yaml_path.name}): {exc}") from exc
                try:
                    self.register(card)
                except RegistryError:
                    raise


def _main() -> None:
    """CLI: python -m agent_org_network.registry <dir>  — load+validate 결과 출력."""
    if len(sys.argv) != 2:
        print("사용법: python -m agent_org_network.registry <registry-dir>")
        sys.exit(1)

    directory = Path(sys.argv[1])
    if not directory.is_dir():
        print(f"오류: 디렉터리를 찾을 수 없음 — {directory}")
        sys.exit(1)

    registry = Registry()
    try:
        registry.load(directory)
        registry.validate()
    except RegistryError as exc:
        print(f"[오류] {exc}")
        sys.exit(1)

    cards = registry.all_cards()
    print(f"[OK] 유저 {len(registry.user_ids())}명, 카드 {len(cards)}장 로드·검증 완료")
    for card in cards:
        print(f"  - {card.agent_id} (owner={card.owner}, domains={card.domains})")


if __name__ == "__main__":
    _main()
