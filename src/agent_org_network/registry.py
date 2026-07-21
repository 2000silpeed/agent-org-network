from __future__ import annotations

import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from agent_org_network.agent_card import AgentCard
from agent_org_network.user import User


class RegistryError(Exception):
    pass


def _copy_card(card: AgentCard) -> AgentCard:
    return AgentCard.model_validate(
        card.model_dump(mode="python", round_trip=True),
        strict=True,
    )


def _copy_user(user: User) -> User:
    return User.model_validate(
        user.model_dump(mode="python", round_trip=True),
        strict=True,
    )


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
        self._snapshot_state = threading.local()

    def user_ids(self) -> frozenset[str]:
        """등록된 User id 집합(읽기 전용)."""
        return frozenset(self._users.keys())

    @contextmanager
    def consistency_guard(self) -> Generator[None]:
        """여러 Registry read와 외부 CAS 사이 snapshot을 한 RLock 아래 고정한다."""
        with self._lock:
            depth = int(getattr(self._snapshot_state, "depth", 0))
            self._snapshot_state.depth = depth + 1
            try:
                yield
            finally:
                self._snapshot_state.depth = depth

    def _reject_snapshot_mutation(self) -> None:
        if int(getattr(self._snapshot_state, "depth", 0)) > 0:
            raise RegistryError("Registry consistency snapshot 안에서는 mutation할 수 없습니다.")

    def all_users(self) -> list[User]:
        """등록된 User 목록(읽기 전용)."""
        with self._lock:
            return [_copy_user(user) for user in self._users.values()]

    def get_user(self, user_id: str) -> User:
        """user_id로 User를 조회한다."""
        with self._lock:
            return _copy_user(self._users[user_id])

    def register(self, card: AgentCard) -> None:
        canonical = _copy_card(card)
        with self._lock:
            self._reject_snapshot_mutation()
            if canonical.agent_id in self._cards:
                raise RegistryError(f"중복 agent_id: {canonical.agent_id}")
            self._cards[canonical.agent_id] = canonical

    def register_user(self, user: User) -> None:
        canonical = _copy_user(user)
        with self._lock:
            self._reject_snapshot_mutation()
            if canonical.id in self._users:
                raise RegistryError(f"중복 user id: {canonical.id}")
            self._users[canonical.id] = canonical

    def replace_card(self, card: AgentCard) -> None:
        """기존 agent_id의 카드 값을 교체한다(오너 변경 전이의 스위치·ADR 0034 결정 2).

        `register`가 신규 agent_id만 받는(중복 거부) 것과 대칭 — 이건 *이미 있는*
        agent_id의 frozen 값 교체다. owner가 A→B로 바뀐 새 카드 값으로 갈아끼운다
        (agent_id 불변·frozen 값이라 mutation이 아니라 값 교체). 없는 agent_id면
        RegistryError(전이 대상 부재). 참조 무결성(새 owner 실재)은 호출측
        admission이 진다(우회 API 금지 — `register`와 같은 관문 재사용).
        """
        canonical = _copy_card(card)
        with self._lock:
            self._reject_snapshot_mutation()
            if canonical.agent_id not in self._cards:
                raise RegistryError(f"미존재 agent_id: {canonical.agent_id}")
            self._cards[canonical.agent_id] = canonical

    def get(self, agent_id: str) -> AgentCard:
        with self._lock:
            return _copy_card(self._cards[agent_id])

    def has_card(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id in self._cards

    def all_cards(self) -> list[AgentCard]:
        with self._lock:
            return [_copy_card(card) for card in self._cards.values()]

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
