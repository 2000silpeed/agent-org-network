from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import time
from typing import Protocol

import pytest

from agent_org_network.owner_bootstrap_journal import (
    CompleteOwnerBootstrap,
    MarkOwnerBootstrapKeyStored,
    MarkOwnerBootstrapRecoveryLinked,
    OwnerBootstrapJournal,
    OwnerBootstrapJournalConflict,
    OwnerBootstrapJournalUnavailable,
    PrepareOwnerBootstrapAttempt,
    ProductionOwnerBootstrapExecutionFence,
    derive_owner_bootstrap_id,
)


NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
D1 = sha256(b"one").hexdigest()
D2 = sha256(b"two").hexdigest()


def _jcs(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _domain_digest(domain: bytes, value: object) -> str:
    return sha256(domain + _jcs(value).encode()).hexdigest()


class _StartEvent(Protocol):
    def wait(self, timeout: float | None = None) -> bool: ...


def _spawn_fence_worker(
    root: str, bootstrap_id: str, trace_path: str, start_event: _StartEvent
) -> None:
    start_event.wait()
    fence = ProductionOwnerBootstrapExecutionFence(root)
    with fence.acquire(bootstrap_id):
        descriptor = os.open(trace_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, f"start:{os.getpid()}\n".encode())
            time.sleep(0.01)
            os.write(descriptor, f"end:{os.getpid()}\n".encode())
        finally:
            os.close(descriptor)


def _prepare(key: str = "prepare:1") -> PrepareOwnerBootstrapAttempt:
    return PrepareOwnerBootstrapAttempt(
        central_origin="https://central.example",
        org_id="org-1",
        owner_user_id="user-1",
        agent_card_id="card-1",
        device_key_thumbprint="thumbprint-1",
        binding_digest=D1,
        idempotency_key=key,
        now=NOW,
    )


def _recovery_linked(journal: OwnerBootstrapJournal) -> str:
    prepared = journal.prepare(_prepare())
    journal.mark_key_stored(
        MarkOwnerBootstrapKeyStored(
            owner_bootstrap_id=prepared.owner_bootstrap_id,
            expected_revision=1,
            bundle_revision=1,
            bundle_public_digest=D2,
            idempotency_key="key:linked",
            now=NOW,
        )
    )
    journal.mark_recovery_linked(
        MarkOwnerBootstrapRecoveryLinked(
            owner_bootstrap_id=prepared.owner_bootstrap_id,
            expected_revision=2,
            recovery_profile_id="profile-1",
            recovery_resource_digest=D1,
            idempotency_key="recovery:linked",
            now=NOW,
        )
    )
    return prepared.owner_bootstrap_id


def _complete(
    owner_bootstrap_id: str, idempotency_key: str = "complete:1"
) -> CompleteOwnerBootstrap:
    return CompleteOwnerBootstrap(
        owner_bootstrap_id=owner_bootstrap_id,
        expected_revision=3,
        recovery_profile_id="profile-1",
        recovery_resource_digest=D1,
        terminal_receipt_id="terminal-1",
        terminal_receipt_digest=D2,
        verification="paired",
        idempotency_key=idempotency_key,
        now=NOW,
    )


def test_prepare부터_recovery_link까지_exact_replay한다(tmp_path: Path) -> None:
    journal = OwnerBootstrapJournal(tmp_path / "journal.sqlite")
    prepared = journal.prepare(_prepare())
    assert prepared.state == "prepared"
    assert prepared.owner_bootstrap_id == derive_owner_bootstrap_id(_prepare())
    assert journal.prepare(_prepare()).kind == "replayed"

    key_stored = journal.mark_key_stored(
        MarkOwnerBootstrapKeyStored(
            owner_bootstrap_id=prepared.owner_bootstrap_id,
            expected_revision=1,
            bundle_revision=1,
            bundle_public_digest=D2,
            idempotency_key="key:1",
            now=NOW,
        )
    )
    assert (key_stored.state, key_stored.revision) == ("key_stored", 2)
    assert (
        journal.mark_key_stored(
            MarkOwnerBootstrapKeyStored(
                owner_bootstrap_id=prepared.owner_bootstrap_id,
                expected_revision=1,
                bundle_revision=1,
                bundle_public_digest=D2,
                idempotency_key="key:1",
                now=NOW,
            )
        ).kind
        == "replayed"
    )

    linked = journal.mark_recovery_linked(
        MarkOwnerBootstrapRecoveryLinked(
            owner_bootstrap_id=prepared.owner_bootstrap_id,
            expected_revision=2,
            recovery_profile_id="profile-1",
            recovery_resource_digest=D1,
            idempotency_key="recovery:1",
            now=NOW,
        )
    )
    assert (linked.state, linked.revision) == ("recovery_linked", 3)


def test_같은_idempotency_key의_다른_command는_conflict다(tmp_path: Path) -> None:
    journal = OwnerBootstrapJournal(tmp_path / "journal.sqlite")
    journal.prepare(_prepare())
    with pytest.raises(OwnerBootstrapJournalConflict):
        journal.prepare(_prepare("prepare:1").model_copy(update={"agent_card_id": "card-2"}))


def test_transaction_clock을_다시_읽고_caller_now를_권위로_쓰지_않는다(
    tmp_path: Path,
) -> None:
    trusted = datetime(2026, 7, 28, 13, tzinfo=UTC)
    journal = OwnerBootstrapJournal(tmp_path / "journal.sqlite", clock=lambda: trusted)
    result = journal.prepare(_prepare())
    assert result.updated_at == trusted


def test_catalog와_receipt_tamper는_fail_closed다(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite"
    journal = OwnerBootstrapJournal(path)
    journal.prepare(_prepare())
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE injected_catalog(value TEXT) STRICT")
    with pytest.raises(OwnerBootstrapJournalUnavailable):
        OwnerBootstrapJournal(path)


def test_32_distinct_instance중_prepare_winner는하나다(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite"
    OwnerBootstrapJournal(path)
    stores = tuple(OwnerBootstrapJournal(path) for _ in range(32))

    def run(index: int) -> str:
        result = stores[index].prepare(_prepare(f"prepare:{index}"))
        return result.kind

    with ThreadPoolExecutor(max_workers=32) as pool:
        kinds = tuple(pool.map(run, range(32)))
    assert kinds.count("prepared") == 1
    assert kinds.count("joined") == 31


def test_32_distinct_instance가_exact_existing_key_bundle에_join한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite"
    prepared = OwnerBootstrapJournal(path).prepare(_prepare())
    stores = tuple(OwnerBootstrapJournal(path) for _ in range(32))

    def run(index: int) -> str:
        return (
            stores[index]
            .mark_key_stored(
                MarkOwnerBootstrapKeyStored(
                    owner_bootstrap_id=prepared.owner_bootstrap_id,
                    expected_revision=1,
                    bundle_revision=1,
                    bundle_public_digest=D2,
                    idempotency_key=f"key:{index}",
                    now=NOW,
                )
            )
            .kind
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        kinds = tuple(pool.map(run, range(32)))
    assert kinds.count("key_stored") == 1
    assert kinds.count("joined") == 31

    with pytest.raises(OwnerBootstrapJournalConflict):
        stores[0].mark_key_stored(
            MarkOwnerBootstrapKeyStored(
                owner_bootstrap_id=prepared.owner_bootstrap_id,
                expected_revision=1,
                bundle_revision=2,
                bundle_public_digest=D1,
                idempotency_key="key:mismatch",
                now=NOW,
            )
        )


def test_commit직전_fault는_head와receipt를함께rollback한다(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite"

    def fail(point: str) -> None:
        if point == "before_prepare_commit":
            raise RuntimeError("injected")

    with pytest.raises(OwnerBootstrapJournalUnavailable):
        OwnerBootstrapJournal(path, fault=fail).prepare(_prepare())
    assert OwnerBootstrapJournal(path).prepare(_prepare()).kind == "prepared"


@pytest.mark.parametrize(
    ("point", "advance"),
    [
        ("before_key.store_commit", "key"),
        ("before_recovery.link_commit", "recovery"),
    ],
)
def test_transition_commit직전_fault는_head와receipt를함께rollback하고retry한다(
    tmp_path: Path, point: str, advance: str
) -> None:
    path = tmp_path / "journal.sqlite"
    prepared = OwnerBootstrapJournal(path).prepare(_prepare())

    def fail(actual: str) -> None:
        if actual == point:
            raise RuntimeError("injected")

    if advance == "key":
        command = MarkOwnerBootstrapKeyStored(
            owner_bootstrap_id=prepared.owner_bootstrap_id,
            expected_revision=1,
            bundle_revision=1,
            bundle_public_digest=D2,
            idempotency_key="key:fault",
            now=NOW,
        )
        with pytest.raises(OwnerBootstrapJournalUnavailable):
            OwnerBootstrapJournal(path, fault=fail).mark_key_stored(command)
        assert OwnerBootstrapJournal(path).mark_key_stored(command).kind == "key_stored"
    else:
        OwnerBootstrapJournal(path).mark_key_stored(
            MarkOwnerBootstrapKeyStored(
                owner_bootstrap_id=prepared.owner_bootstrap_id,
                expected_revision=1,
                bundle_revision=1,
                bundle_public_digest=D2,
                idempotency_key="key:first",
                now=NOW,
            )
        )
        command = MarkOwnerBootstrapRecoveryLinked(
            owner_bootstrap_id=prepared.owner_bootstrap_id,
            expected_revision=2,
            recovery_profile_id="profile-1",
            recovery_resource_digest=D1,
            idempotency_key="recovery:fault",
            now=NOW,
        )
        with pytest.raises(OwnerBootstrapJournalUnavailable):
            OwnerBootstrapJournal(path, fault=fail).mark_recovery_linked(command)
        assert OwnerBootstrapJournal(path).mark_recovery_linked(command).kind == "recovery_linked"


def test_32_distinct_instance가_exact_existing_recovery_link에join한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite"
    journal = OwnerBootstrapJournal(path)
    prepared = journal.prepare(_prepare())
    journal.mark_key_stored(
        MarkOwnerBootstrapKeyStored(
            owner_bootstrap_id=prepared.owner_bootstrap_id,
            expected_revision=1,
            bundle_revision=1,
            bundle_public_digest=D2,
            idempotency_key="key:1",
            now=NOW,
        )
    )
    stores = tuple(OwnerBootstrapJournal(path) for _ in range(32))

    def run(index: int) -> str:
        return (
            stores[index]
            .mark_recovery_linked(
                MarkOwnerBootstrapRecoveryLinked(
                    owner_bootstrap_id=prepared.owner_bootstrap_id,
                    expected_revision=2,
                    recovery_profile_id="profile-1",
                    recovery_resource_digest=D1,
                    idempotency_key=f"recovery:{index}",
                    now=NOW,
                )
            )
            .kind
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        kinds = tuple(pool.map(run, range(32)))
    assert kinds.count("recovery_linked") == 1
    assert kinds.count("joined") == 31
    with pytest.raises(OwnerBootstrapJournalConflict):
        stores[0].mark_recovery_linked(
            MarkOwnerBootstrapRecoveryLinked(
                owner_bootstrap_id=prepared.owner_bootstrap_id,
                expected_revision=2,
                recovery_profile_id="profile-other",
                recovery_resource_digest=D2,
                idempotency_key="recovery:mismatch",
                now=NOW,
            )
        )


@pytest.mark.parametrize("mutation", ["orphan_head", "orphan_receipt", "receipt_result"])
def test_graph_orphan과receipt_tamper를startup에서거부한다(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / "journal.sqlite"
    journal = OwnerBootstrapJournal(path)
    prepared = journal.prepare(_prepare())
    with sqlite3.connect(path) as connection:
        if mutation == "orphan_head":
            connection.execute(
                "DELETE FROM owner_bootstrap_heads WHERE owner_bootstrap_id=?",
                (prepared.owner_bootstrap_id,),
            )
        else:
            trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='trigger' AND name='owner_bootstrap_receipts_no_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER owner_bootstrap_receipts_no_update")
            if mutation == "orphan_receipt":
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute(
                    "UPDATE owner_bootstrap_transition_receipts "
                    "SET owner_bootstrap_id=? WHERE idempotency_key='prepare:1'",
                    ("f" * 64,),
                )
            else:
                connection.execute(
                    "UPDATE owner_bootstrap_transition_receipts "
                    "SET result_json=replace(result_json,'prepared','key_stored') "
                    "WHERE idempotency_key='prepare:1'"
                )
            connection.execute(trigger_sql)
    with pytest.raises(OwnerBootstrapJournalUnavailable):
        OwnerBootstrapJournal(path)


@pytest.mark.parametrize("mutation", ["empty_idempotency_key", "bool_bundle_revision"])
def test_digest를재계산한_noncanonical_receipt도startup에서거부한다(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / "journal.sqlite"
    journal = OwnerBootstrapJournal(path)
    prepared = journal.prepare(_prepare())
    if mutation == "bool_bundle_revision":
        journal.mark_key_stored(
            MarkOwnerBootstrapKeyStored(
                owner_bootstrap_id=prepared.owner_bootstrap_id,
                expected_revision=1,
                bundle_revision=1,
                bundle_public_digest=D2,
                idempotency_key="key:strict",
                now=NOW,
            )
        )
    target = "prepare:1" if mutation == "empty_idempotency_key" else "key:strict"
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='trigger' AND name='owner_bootstrap_receipts_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER owner_bootstrap_receipts_no_update")
        row = connection.execute(
            "SELECT * FROM owner_bootstrap_transition_receipts WHERE idempotency_key=?",
            (target,),
        ).fetchone()
        idempotency_key = "" if mutation == "empty_idempotency_key" else target
        command = json.loads(row["command_json"])
        if mutation == "bool_bundle_revision":
            command["bundle_revision"] = True
        command_json = _jcs(command)
        command_digest = _domain_digest(
            f"aon.owner.bootstrap.{row['action']}.v1\0".encode(), command
        )
        receipt_digest = _domain_digest(
            b"aon.owner.bootstrap.receipt.v1\0",
            {
                "action": row["action"],
                "command_digest": command_digest,
                "created_at": row["created_at"],
                "idempotency_key": idempotency_key,
                "owner_bootstrap_id": row["owner_bootstrap_id"],
                "result_digest": sha256(row["result_json"].encode()).hexdigest(),
            },
        )
        connection.execute(
            "UPDATE owner_bootstrap_transition_receipts "
            "SET idempotency_key=?,command_json=?,command_digest=?,receipt_digest=? "
            "WHERE idempotency_key=?",
            (
                idempotency_key,
                command_json,
                command_digest,
                receipt_digest,
                target,
            ),
        )
        connection.execute(trigger_sql)
    with pytest.raises(OwnerBootstrapJournalUnavailable):
        OwnerBootstrapJournal(path)


def test_recovery_linked는_exact_paired_terminal_evidence로completed가된다(
    tmp_path: Path,
) -> None:
    journal = OwnerBootstrapJournal(tmp_path / "journal.sqlite")
    owner_bootstrap_id = _recovery_linked(journal)
    completed = journal.complete(_complete(owner_bootstrap_id))
    assert (completed.kind, completed.state, completed.revision) == (
        "completed",
        "completed",
        4,
    )
    assert journal.complete(_complete(owner_bootstrap_id)).kind == "replayed"
    assert journal.complete(_complete(owner_bootstrap_id, "complete:join")).kind == "joined"
    with pytest.raises(OwnerBootstrapJournalConflict):
        journal.complete(
            _complete(owner_bootstrap_id, "complete:conflict").model_copy(
                update={"terminal_receipt_digest": D1}
            )
        )


def test_complete_commit직전fault는head와receipt를rollback하고retry한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite"
    owner_bootstrap_id = _recovery_linked(OwnerBootstrapJournal(path))

    def fail(point: str) -> None:
        if point == "before_bootstrap.complete_commit":
            raise RuntimeError("injected")

    command = _complete(owner_bootstrap_id)
    with pytest.raises(OwnerBootstrapJournalUnavailable):
        OwnerBootstrapJournal(path, fault=fail).complete(command)
    assert OwnerBootstrapJournal(path).complete(command).kind == "completed"


def test_32_distinct_instance중complete_winner는하나고exact_join한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite"
    owner_bootstrap_id = _recovery_linked(OwnerBootstrapJournal(path))
    stores = tuple(OwnerBootstrapJournal(path) for _ in range(32))

    def run(index: int) -> str:
        return stores[index].complete(_complete(owner_bootstrap_id, f"complete:{index}")).kind

    with ThreadPoolExecutor(max_workers=32) as pool:
        kinds = tuple(pool.map(run, range(32)))
    assert kinds.count("completed") == 1
    assert kinds.count("joined") == 31


def test_completed_receipt_graph_tamper와_old_attempt_revival을거부한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite"
    journal = OwnerBootstrapJournal(path)
    owner_bootstrap_id = _recovery_linked(journal)
    journal.complete(_complete(owner_bootstrap_id))
    with pytest.raises(OwnerBootstrapJournalConflict):
        journal.mark_recovery_linked(
            MarkOwnerBootstrapRecoveryLinked(
                owner_bootstrap_id=owner_bootstrap_id,
                expected_revision=3,
                recovery_profile_id="profile-1",
                recovery_resource_digest=D1,
                idempotency_key="recovery:revive",
                now=NOW,
            )
        )

    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM owner_bootstrap_heads WHERE owner_bootstrap_id=?",
            (owner_bootstrap_id,),
        )
    with pytest.raises(OwnerBootstrapJournalUnavailable):
        OwnerBootstrapJournal(path)


def test_production_fence는_shared_root에서_32_process_style_instance를직렬화한다(
    tmp_path: Path,
) -> None:
    root = tmp_path / "locks"
    fences = tuple(ProductionOwnerBootstrapExecutionFence(root) for _ in range(32))
    inside = 0
    maximum = 0

    def enter(index: int) -> None:
        nonlocal inside, maximum
        with fences[index].acquire("a" * 64):
            inside += 1
            maximum = max(maximum, inside)
            time.sleep(0.002)
            inside -= 1

    with ThreadPoolExecutor(max_workers=32) as pool:
        tuple(pool.map(enter, range(32)))
    assert maximum == 1


def test_production_fence는_spawn_process_distinct_instance를직렬화한다(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    root = tmp_path / "process-locks"
    trace = tmp_path / "trace.log"
    processes = tuple(
        context.Process(
            target=_spawn_fence_worker,
            args=(str(root), "b" * 64, str(trace), start),
        )
        for _ in range(8)
    )
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    lines = trace.read_text().splitlines()
    assert len(lines) == 16
    for index in range(0, len(lines), 2):
        assert lines[index].startswith("start:")
        assert lines[index + 1] == lines[index].replace("start:", "end:")


def test_production_fence는매acquire마다root와lock을fail_closed한다(
    tmp_path: Path,
) -> None:
    root = tmp_path / "guarded-locks"
    fence = ProductionOwnerBootstrapExecutionFence(root)
    with pytest.raises(OwnerBootstrapJournalUnavailable):
        with fence.acquire("not-a-digest"):
            pass

    root.chmod(0o755)
    with pytest.raises(OwnerBootstrapJournalUnavailable):
        with fence.acquire("c" * 64):
            pass
    root.chmod(0o700)

    lock_path = root / f"{'d' * 64}.lock"
    lock_path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OwnerBootstrapJournalUnavailable):
        with fence.acquire("d" * 64):
            pass


def test_secret_named_fields는_public_command에없다() -> None:
    fields = (
        set(PrepareOwnerBootstrapAttempt.model_fields)
        | set(MarkOwnerBootstrapKeyStored.model_fields)
        | set(MarkOwnerBootstrapRecoveryLinked.model_fields)
    )
    assert not fields & {"credential_secret", "private_key", "token", "envelope"}
