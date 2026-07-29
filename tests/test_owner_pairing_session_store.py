from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import sqlite3
from threading import Barrier

from pydantic import SecretStr
import pytest

from agent_org_network.owner_local_authoring_repository import OwnerLocalAuthoringKey
from agent_org_network.owner_pairing_session_store import (
    OwnerPairingBootstrap,
    OwnerPairingSession,
    OwnerPairingSessionStore,
    OwnerPairingSessionStoreUnavailable,
)


class _Keys:
    def __init__(self, value: bytes = b"k" * 32) -> None:
        self.value = value

    def current(self) -> OwnerLocalAuthoringKey:
        return OwnerLocalAuthoringKey(key_id="owner-key", key=self.value)


NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _pairing(
    *, credential: str = "s" * 32, generation: int = 1
) -> OwnerPairingBootstrap:
    return OwnerPairingBootstrap(
        audience="owner-install",
        org_id="acme",
        owner_id="owner",
        agent_id="support",
        card_revision=2,
        card_digest="a" * 64,
        device_public_key_digest="d" * 64,
        identity_provider="corp",
        central_identity_session=SecretStr(credential),
        credential_generation=generation,
        owner_session_ttl_seconds=3600,
        allowed_origin="https://owner.example",
        expires_at=NOW + timedelta(hours=2),
    )


def test_consume_replay_resolve_mutation과_secret_at_rest0(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite"
    store = OwnerPairingSessionStore(path, keys=_Keys(), clock=lambda: NOW)
    first = store.consume(_pairing())
    replay = store.consume(_pairing())
    assert replay.replayed
    assert replay.owner_session.get_secret_value() == first.owner_session.get_secret_value()
    resolved = store.resolve(first.owner_session.get_secret_value())
    assert resolved.org_id == "acme"
    assert (
        store.verify_mutation(
            first.owner_session.get_secret_value(),
            first.csrf_token.get_secret_value(),
            "https://owner.example",
        )
        == resolved
    )
    disk = path.read_bytes()
    for secret in (
        b"s" * 32,
        first.owner_session.get_secret_value().encode(),
        first.csrf_token.get_secret_value().encode(),
    ):
        assert secret not in disk
    assert path.stat().st_mode & 0o077 == 0
    assert first.owner_session.get_secret_value() not in repr(first)


def test_wrong_binding_csrf_origin_expiry와_revoke는_failclosed(tmp_path: Path) -> None:
    current = [NOW]
    store = OwnerPairingSessionStore(
        tmp_path / "sessions.sqlite", keys=_Keys(), clock=lambda: current[0]
    )
    issued = store.consume(_pairing())
    session = issued.owner_session.get_secret_value()
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.verify_mutation(session, "x" * 32, "https://owner.example")
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.verify_mutation(
            session, issued.csrf_token.get_secret_value(), "https://evil.example"
        )
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        current[0] = NOW + timedelta(hours=2)
        store.resolve(session)
    current[0] = NOW
    store.revoke(session)
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.resolve(session)


def test_generation_rotation은_old_session을무효화한다(tmp_path: Path) -> None:
    store = OwnerPairingSessionStore(
        tmp_path / "sessions.sqlite", keys=_Keys(), clock=lambda: NOW
    )
    old = store.consume(_pairing())
    new = store.consume(_pairing(credential="t" * 32, generation=2))
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.resolve(old.owner_session.get_secret_value())
    assert store.resolve(new.owner_session.get_secret_value()).credential_generation == 2
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.consume(_pairing(credential="u" * 32, generation=1))


def test_same_credential의_binding_conflict는_exact_failclosed다(
    tmp_path: Path,
) -> None:
    store = OwnerPairingSessionStore(
        tmp_path / "sessions.sqlite", keys=_Keys(), clock=lambda: NOW
    )
    store.consume(_pairing())
    changed = _pairing().model_copy(update={"card_digest": "b" * 64})
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.consume(changed)


@pytest.mark.parametrize(
    ("csrf", "origin"),
    [
        ("", "https://owner.example"),
        ("x" * 31, "https://owner.example"),
        ("x" * 32, ""),
        ("x" * 32, "http://owner.example"),
        ("x" * 32, "https://owner.example/path"),
        ("x" * 32, "https://owner.example/"),
    ],
)
def test_mutation_input은_exact_token과_origin만받는다(
    tmp_path: Path, csrf: str, origin: str
) -> None:
    store = OwnerPairingSessionStore(
        tmp_path / "sessions.sqlite", keys=_Keys(), clock=lambda: NOW
    )
    issued = store.consume(_pairing())
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.verify_mutation(
            issued.owner_session.get_secret_value(), csrf, origin
        )


def test_runtime_non_string_session_csrf_origin은_failclosed다(tmp_path: Path) -> None:
    store = OwnerPairingSessionStore(
        tmp_path / "sessions.sqlite", keys=_Keys(), clock=lambda: NOW
    )
    issued = store.consume(_pairing())
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.resolve(None)  # pyright: ignore[reportArgumentType]
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.verify_mutation(
            issued.owner_session.get_secret_value(),
            None,  # pyright: ignore[reportArgumentType]
            "https://owner.example",
        )
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.verify_mutation(
            issued.owner_session.get_secret_value(),
            issued.csrf_token.get_secret_value(),
            None,  # pyright: ignore[reportArgumentType]
        )


def test_row_expiry만_future로늘린_coordinated_tamper도_failclosed다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite"
    store = OwnerPairingSessionStore(path, keys=_Keys(), clock=lambda: NOW)
    issued = store.consume(_pairing())
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='owner_pairing_sessions_exact_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER owner_pairing_sessions_exact_update")
        connection.execute(
            "UPDATE owner_pairing_sessions SET expires_at=?",
            ((NOW + timedelta(hours=3)).isoformat(),),
        )
        connection.execute(trigger_sql)
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.resolve(issued.owner_session.get_secret_value())


def test_32way_consume는한_session으로수렴한다(tmp_path: Path) -> None:
    store = OwnerPairingSessionStore(
        tmp_path / "sessions.sqlite", keys=_Keys(), clock=lambda: NOW
    )
    barrier = Barrier(32)

    def consume_after_barrier(_index: int) -> OwnerPairingSession:
        barrier.wait()
        return store.consume(_pairing())

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(consume_after_barrier, range(32)))
    assert len({item.owner_session.get_secret_value() for item in results}) == 1
    assert sum(not item.replayed for item in results) == 1


def test_wrong_key_tamper_schema_symlink과_ABA는_failclosed(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite"
    store = OwnerPairingSessionStore(path, keys=_Keys(), clock=lambda: NOW)
    issued = store.consume(_pairing())
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        OwnerPairingSessionStore(
            path, keys=_Keys(b"x" * 32), clock=lambda: NOW
        ).resolve(issued.owner_session.get_secret_value())
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE drift(value TEXT) STRICT")
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.resolve(issued.owner_session.get_secret_value())
    original = tmp_path / "original.sqlite"
    os.replace(path, original)
    os.symlink(original, path)
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.resolve(issued.owner_session.get_secret_value())


def test_permission_drift와_post_init_new_regular_swap은_read_write0이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite"
    store = OwnerPairingSessionStore(path, keys=_Keys(), clock=lambda: NOW)
    issued = store.consume(_pairing())
    os.chmod(path, 0o644)
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.resolve(issued.owner_session.get_secret_value())
    os.chmod(path, 0o600)
    held = tmp_path / "held.sqlite"
    os.replace(path, held)
    OwnerPairingSessionStore(path, keys=_Keys(), clock=lambda: NOW)
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.resolve(issued.owner_session.get_secret_value())
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        store.consume(_pairing(credential="z" * 32, generation=2))
    assert (
        OwnerPairingSessionStore(held, keys=_Keys(), clock=lambda: NOW)
        .resolve(issued.owner_session.get_secret_value())
        .owner_id
        == "owner"
    )


def test_A_B_A_connect_swap은_B_write0이고_A가보존된다(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite"
    base = OwnerPairingSessionStore(path, keys=_Keys(), clock=lambda: NOW)
    issued = base.consume(_pairing())
    replacement = tmp_path / "replacement.sqlite"
    OwnerPairingSessionStore(replacement, keys=_Keys(), clock=lambda: NOW)
    held_a = tmp_path / "held-a.sqlite"
    returned_b = tmp_path / "returned-b.sqlite"
    armed = False

    def before_connect() -> None:
        if armed:
            os.replace(path, held_a)
            os.replace(replacement, path)

    def after_connect() -> None:
        if armed:
            os.replace(path, returned_b)
            os.replace(held_a, path)

    guarded = OwnerPairingSessionStore(
        path,
        keys=_Keys(),
        clock=lambda: NOW,
        before_connect_hook=before_connect,
        connect_hook=after_connect,
    )
    armed = True
    with pytest.raises(OwnerPairingSessionStoreUnavailable):
        guarded.consume(_pairing(credential="z" * 32, generation=2))
    assert (
        OwnerPairingSessionStore(path, keys=_Keys(), clock=lambda: NOW)
        .resolve(issued.owner_session.get_secret_value())
        .owner_id
        == "owner"
    )
    with sqlite3.connect(returned_b) as connection:
        assert connection.execute(
            "SELECT count(*) FROM owner_pairing_sessions"
        ).fetchone() == (0,)


def test_fault는_transaction을rollback한다(tmp_path: Path) -> None:
    def fault(point: str) -> None:
        if point == "before_commit":
            raise RuntimeError("secret must not escape")

    path = tmp_path / "sessions.sqlite"
    store = OwnerPairingSessionStore(
        path, keys=_Keys(), clock=lambda: NOW, fault=fault
    )
    with pytest.raises(OwnerPairingSessionStoreUnavailable) as raised:
        store.consume(_pairing())
    assert "secret" not in str(raised.value)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM owner_pairing_sessions").fetchone() == (0,)
