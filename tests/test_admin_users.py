"""사용자 프로비저닝 도메인 — 관리자 수동 User 등록(register-only·ADR 0064).

`admin_registry.py`(카드) 테스트의 User 축 형제 — 같은 결의 결정론 단위 테스트:
1. admit_user — 유효/무효 User 후보 admission 판정(우회 없음).
2. AdminUserService.register_user — 라이브 반영·중복 거부·무효 거부·저널·감사 기록.
3. 불변식 회귀 — 무효 User가 registry에 안 들어간다.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent_org_network.admin_registry import AdmissionError
from agent_org_network.admin_users import (
    AdminUserService,
    DuplicateUserError,
    UserCandidate,
    admit_user,
)
from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.registry import Registry
from agent_org_network.user import User

_NOW = datetime(2026, 7, 21, tzinfo=timezone.utc)


def _clock() -> datetime:
    return _NOW


def _registry() -> Registry:
    reg = Registry()
    reg.register_user(User(id="root_mgr"))
    reg.register_user(User(id="alice", email="alice@example.com", manager="root_mgr"))
    reg.validate()
    return reg


def _candidate(**kwargs: object) -> UserCandidate:
    defaults: dict[str, object] = {
        "user_id": "bob",
        "email": "bob@example.com",
        "manager": "root_mgr",
    }
    defaults.update(kwargs)
    return UserCandidate(**defaults)  # type: ignore[arg-type]


@dataclass
class _RecordedRegister:
    user_id: str
    email: str | None
    manager: str | None
    by: str
    at: datetime


def _empty_calls() -> list[_RecordedRegister]:
    return []


@dataclass
class FakeUserJournalSink:
    """`UserJournalSink` 포트의 결정론 테스트 더블 — 호출을 그대로 기록."""

    calls: list[_RecordedRegister] = field(default_factory=_empty_calls)

    def append_register(
        self,
        *,
        user_id: str,
        email: str | None,
        manager: str | None,
        by: str,
        at: datetime,
    ) -> None:
        self.calls.append(_RecordedRegister(user_id, email, manager, by, at))


class TestAdmitUser:
    def test_nonblank_id_위반(self) -> None:
        user, errors = admit_user(_candidate(user_id="  "), _registry())
        assert user is None
        assert errors

    def test_email_필수_위반_require_email_True(self) -> None:
        user, errors = admit_user(
            _candidate(email=None), _registry(), require_email=True
        )
        assert user is None
        assert any("email" in e for e in errors)

    def test_email_필수_아님_require_email_False(self) -> None:
        user, errors = admit_user(
            _candidate(email=None), _registry(), require_email=False
        )
        assert user is not None
        assert errors == []
        assert user.email is None

    def test_email_형식_위반(self) -> None:
        user, errors = admit_user(_candidate(email="bad-email"), _registry())
        assert user is None
        assert errors

    def test_email_전역_유일_위반(self) -> None:
        user, errors = admit_user(
            _candidate(user_id="new_bob", email="alice@example.com"), _registry()
        )
        assert user is None
        assert any("email" in e for e in errors)

    def test_manager_미실재_거부(self) -> None:
        user, errors = admit_user(_candidate(manager="ghost"), _registry())
        assert user is None
        assert any("manager" in e for e in errors)

    def test_manager_None_루트_허용(self) -> None:
        user, errors = admit_user(_candidate(manager=None), _registry())
        assert user is not None
        assert errors == []
        assert user.manager is None

    def test_manager_빈문자열_루트로_정규화(self) -> None:
        user, errors = admit_user(_candidate(manager=""), _registry())
        assert user is not None
        assert errors == []
        assert user.manager is None

    def test_manager_공백만_루트로_정규화(self) -> None:
        user, errors = admit_user(_candidate(manager="   "), _registry())
        assert user is not None
        assert errors == []
        assert user.manager is None

    def test_유효_User_통과(self) -> None:
        user, errors = admit_user(_candidate(), _registry())
        assert user is not None
        assert errors == []
        assert user.id == "bob"
        assert user.email == "bob@example.com"
        assert user.manager == "root_mgr"

    def test_에러_모아서_반환(self) -> None:
        # email 형식 위반 + manager 미실재를 동시에 위반하면 둘 다 모여야 한다.
        user, errors = admit_user(
            _candidate(email="bad-email", manager="ghost"), _registry()
        )
        assert user is None
        assert len(errors) == 2


class TestRegisterUser:
    def test_라이브_반영(self) -> None:
        reg = _registry()
        svc = AdminUserService(reg, clock=_clock)
        user = svc.register_user(_candidate(), by="root_mgr")
        assert user.id == "bob"
        assert reg.get_user("bob").email == "bob@example.com"

    def test_저널_append(self) -> None:
        reg = _registry()
        journal = FakeUserJournalSink()
        svc = AdminUserService(reg, journal_sink=journal, clock=_clock)
        svc.register_user(_candidate(), by="root_mgr")
        assert len(journal.calls) == 1
        call = journal.calls[0]
        assert call.user_id == "bob"
        assert call.email == "bob@example.com"
        assert call.manager == "root_mgr"
        assert call.by == "root_mgr"
        assert call.at == _NOW

    def test_감사_기록_남는다(self) -> None:
        reg = _registry()
        audit = InMemoryAuditLog()
        svc = AdminUserService(reg, audit_sink=audit, clock=_clock)
        svc.register_user(_candidate(), by="root_mgr")
        recs = audit.records()
        assert len(recs) == 1
        assert recs[0]["action"]["kind"] == "UserRegistered"
        assert recs[0]["action"]["subject_id"] == "bob"
        assert recs[0]["action"]["by"] == "root_mgr"

    def test_중복_id_거부(self) -> None:
        reg = _registry()
        svc = AdminUserService(reg, clock=_clock)
        try:
            svc.register_user(_candidate(user_id="alice"), by="root_mgr")
            assert False, "DuplicateUserError가 나야 한다"
        except DuplicateUserError:
            pass

    def test_admission_실패_AdmissionError(self) -> None:
        reg = _registry()
        svc = AdminUserService(reg, clock=_clock)
        try:
            svc.register_user(_candidate(manager="ghost"), by="root_mgr")
            assert False, "AdmissionError가 나야 한다"
        except AdmissionError as exc:
            assert exc.errors
        assert "bob" not in reg.user_ids()

    def test_email_중복_동시_등록_직렬화_거부(self) -> None:
        reg = _registry()
        svc = AdminUserService(reg, clock=_clock)
        results: list[object] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(user_id: str) -> None:
            barrier.wait()
            try:
                user = svc.register_user(
                    UserCandidate(
                        user_id=user_id, email="dup@example.com", manager="root_mgr"
                    ),
                    by="root_mgr",
                )
                with results_lock:
                    results.append(user)
            except AdmissionError as exc:
                with results_lock:
                    results.append(exc)

        t1 = threading.Thread(target=worker, args=("u1",))
        t2 = threading.Thread(target=worker, args=("u2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [r for r in results if isinstance(r, User)]
        failures = [r for r in results if isinstance(r, AdmissionError)]
        assert len(successes) == 1
        assert len(failures) == 1
        dup_users = [u for u in reg.all_users() if u.email == "dup@example.com"]
        assert len(dup_users) == 1


class TestInvariant:
    def test_무효_User_registry에_안_들어감(self) -> None:
        reg = _registry()
        svc = AdminUserService(reg, clock=_clock)
        before = reg.user_ids()
        try:
            svc.register_user(_candidate(email=None), by="root_mgr")
            assert False, "AdmissionError가 나야 한다"
        except AdmissionError:
            pass
        assert reg.user_ids() == before
