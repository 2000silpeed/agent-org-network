"""SqliteUserJournal + replay_user_journal — User 라이브 등록 durable 저널(ADR 0064 결정 ⑦).

`test_sqlite_stores_phase12.py`(카드 저널)의 User 축 형제 테스트다. `tmp_path` DB 파일로
재시작 생존을 검증한다. 결정론: 주입 clock, 실 LLM/네트워크 0.

검증 축:
  - conformance — append→reopen→entries 보존(register-only).
  - replay — 저널 재생으로 라이브 User 복원(재기동 시 생존).
  - admission 경유 — 미등록 manager·잘못된 email·email 충돌 항목은 복원 스킵(무효 User
    등록 금지 불변식이 리플레이에도 적용).
  - **user → card 순서 회귀** — User가 카드보다 먼저 재생돼야 라이브 카드 owner가 라이브
    등록 User를 참조할 수 있다(부팅 리플레이 순서 하드 계약).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_org_network.admin_registry import replay_registry_journal
from agent_org_network.admin_users import replay_user_journal
from agent_org_network.registry import Registry
from agent_org_network.sqlite_stores import SqliteRegistryJournal, SqliteUserJournal
from agent_org_network.user import User

T0 = datetime(2026, 7, 5, 0, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=10)


def _root_only_registry() -> Registry:
    reg = Registry()
    reg.register_user(User(id="root_mgr"))
    reg.validate()
    return reg


# ── conformance ──────────────────────────────────────────────────────────────


def test_SqliteUserJournal_는_등록을_append하고_재오픈_후_보존(tmp_path: Path) -> None:
    db = tmp_path / "u.db"
    journal = SqliteUserJournal(db)
    journal.append_register(
        user_id="alice",
        email="alice@company.com",
        manager="root_mgr",
        by="root_mgr",
        at=T0,
    )
    journal.close()

    reopened = SqliteUserJournal(db)
    entries = reopened.entries()
    assert len(entries) == 1
    assert entries[0].kind == "register"
    assert entries[0].candidate.user_id == "alice"
    assert entries[0].candidate.email == "alice@company.com"
    assert entries[0].candidate.manager == "root_mgr"


def test_SqliteUserJournal_는_email_None_manager_None을_보존(tmp_path: Path) -> None:
    db = tmp_path / "u.db"
    journal = SqliteUserJournal(db)
    journal.append_register(user_id="root2", email=None, manager=None, by="op", at=T0)
    journal.close()

    entries = SqliteUserJournal(db).entries()
    assert entries[0].candidate.email is None
    assert entries[0].candidate.manager is None


# ── replay — 복원 ───────────────────────────────────────────────────────────


def test_replay_user_journal_은_등록_User를_복원한다(tmp_path: Path) -> None:
    db = tmp_path / "u.db"
    journal = SqliteUserJournal(db)
    journal.append_register(
        user_id="alice",
        email="alice@company.com",
        manager="root_mgr",
        by="root_mgr",
        at=T0,
    )

    reg = _root_only_registry()
    assert "alice" not in reg.user_ids()
    replay_user_journal(journal, reg)
    assert "alice" in reg.user_ids()
    assert reg.get_user("alice").email == "alice@company.com"
    assert reg.get_user("alice").manager == "root_mgr"


def test_replay_user_journal_은_미등록_manager_항목을_스킵(tmp_path: Path) -> None:
    """참조 무결성 붕괴(당시엔 있었으나 지금 시드엔 없는 manager) — 안전측 스킵."""
    db = tmp_path / "u.db"
    journal = SqliteUserJournal(db)
    journal.append_register(
        user_id="ghost",
        email="ghost@company.com",
        manager="nonexistent_mgr",
        by="op",
        at=T0,
    )

    reg = _root_only_registry()
    replay_user_journal(journal, reg)
    assert "ghost" not in reg.user_ids()  # 미등록 manager → admission 실패 → 스킵


def test_replay_user_journal_은_email_충돌_항목을_스킵(tmp_path: Path) -> None:
    """email 전역 유일 admission이 리플레이에서도 강제 — 중복 email 두 번째는 스킵."""
    db = tmp_path / "u.db"
    journal = SqliteUserJournal(db)
    journal.append_register(
        user_id="alice", email="dup@company.com", manager="root_mgr", by="op", at=T0
    )
    journal.append_register(
        user_id="alice2", email="dup@company.com", manager="root_mgr", by="op", at=T1
    )

    reg = _root_only_registry()
    replay_user_journal(journal, reg)
    assert "alice" in reg.user_ids()  # 먼저 재생 — 통과.
    assert "alice2" not in reg.user_ids()  # 같은 email — admission 거부 → 스킵.


def test_replay_user_journal_은_이미_존재하는_id를_멱등_스킵(tmp_path: Path) -> None:
    db = tmp_path / "u.db"
    journal = SqliteUserJournal(db)
    journal.append_register(user_id="alice", email="a@x.com", manager="root_mgr", by="op", at=T0)

    reg = _root_only_registry()
    reg.register_user(User(id="alice", manager="root_mgr", email="a@x.com"))
    # 이미 있는 id를 다시 재생해도 예외 없이 스킵(멱등 방어).
    replay_user_journal(journal, reg)
    assert "alice" in reg.user_ids()


# ── user → card 순서 회귀 (부팅 리플레이 하드 계약) ──────────────────────────


def test_user가_card보다_먼저_재생돼야_라이브_카드_owner_참조가_성립(tmp_path: Path) -> None:
    """라이브 카드 owner가 라이브 등록 User를 참조 — User 저널을 카드 저널보다 먼저 재생.

    시드엔 root_mgr만 있다. User 저널은 alice(라이브 등록 owner 후보)를, 카드 저널은
    alice가 owner인 카드를 담는다. **user → card 순서**로 재생하면 카드 admission의
    owner 실재(alice)가 통과해 카드가 복원된다.
    """
    user_db = tmp_path / "u.db"
    card_db = tmp_path / "c.db"
    user_journal = SqliteUserJournal(user_db)
    user_journal.append_register(
        user_id="alice", email="alice@company.com", manager="root_mgr", by="op", at=T0
    )
    card_journal = SqliteRegistryJournal(card_db)
    card_journal.append_register(
        agent_id="alice_ops",
        owner="alice",  # 라이브 등록 User(시드에 없음) — User 저널 재생 후에만 실재.
        team="ops",
        summary="alice 담당",
        domains=["신규"],
        last_reviewed_at="2026-06-20",
        by="op",
        at=T1,
    )

    reg = _root_only_registry()
    # 부팅 순서: user → card.
    replay_user_journal(user_journal, reg)
    replay_registry_journal(card_journal, reg)

    assert "alice" in reg.user_ids()
    assert reg.has_card("alice_ops")
    assert reg.get("alice_ops").owner == "alice"


def test_card를_먼저_재생하면_owner_미실재로_카드가_복원되지_않는다(tmp_path: Path) -> None:
    """순서 역전 시의 실패를 명시 — 카드를 먼저 재생하면 owner(alice) 미실재로 스킵된다.

    이 테스트가 **user → card 순서 계약의 근거**다(순서를 지키지 않으면 라이브 카드가
    소멸). 부팅 코드(web.py)는 반드시 user 저널을 카드 저널보다 먼저 재생해야 한다.
    """
    user_db = tmp_path / "u.db"
    card_db = tmp_path / "c.db"
    user_journal = SqliteUserJournal(user_db)
    user_journal.append_register(
        user_id="alice", email="alice@company.com", manager="root_mgr", by="op", at=T0
    )
    card_journal = SqliteRegistryJournal(card_db)
    card_journal.append_register(
        agent_id="alice_ops",
        owner="alice",
        team="ops",
        summary="alice 담당",
        domains=["신규"],
        last_reviewed_at="2026-06-20",
        by="op",
        at=T1,
    )

    reg = _root_only_registry()
    # 순서 역전(card 먼저) — owner alice 미실재 → 카드 admission 실패 → 스킵.
    replay_registry_journal(card_journal, reg)
    assert not reg.has_card("alice_ops")
    # user 저널은 그래도 복원된다(카드가 소멸했을 뿐).
    replay_user_journal(user_journal, reg)
    assert "alice" in reg.user_ids()
