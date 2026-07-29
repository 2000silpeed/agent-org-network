from hashlib import sha256
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from agent_org_network.owner_local_authoring_repository import (
    MAX_ENVELOPE_BYTES,
    MAX_PLAINTEXT_BYTES,
    AuthoringArtifactRef,
    OwnerLocalAuthoringConflict,
    OwnerLocalAuthoringKey,
    OwnerLocalAuthoringRepository,
    OwnerLocalAuthoringUnavailable,
)


class _Keys:
    def __init__(self, key: bytes | None = b"k" * 32, key_id: str = "key-1") -> None:
        self.key = key
        self.key_id = key_id

    def current(self) -> OwnerLocalAuthoringKey:
        if self.key is None:
            raise OwnerLocalAuthoringUnavailable()
        return OwnerLocalAuthoringKey(key_id=self.key_id, key=self.key)


def _ref(payload: bytes, **changes: object) -> AuthoringArtifactRef:
    values: dict[str, object] = {
        "organization_id": "acme",
        "agent_id": "support",
        "run_id": "run-1",
        "revision": 0,
        "artifact_kind": "raw_source",
        "artifact_digest": sha256(payload).hexdigest(),
    }
    values.update(changes)
    return AuthoringArtifactRef(**values)  # type: ignore[arg-type]


def test_raw와_full_draft를_ciphertext로만_atomic_durable저장한다(tmp_path: Path) -> None:
    root = tmp_path / "owner"
    repository = OwnerLocalAuthoringRepository(root, keys=_Keys())
    raw = b"PRIVATE RAW SOURCE"
    draft = b'{"full":"PRIVATE DRAFT BUNDLE"}'
    first = repository.put(_ref(raw), raw)
    second = repository.put(
        _ref(draft, revision=1, artifact_kind="full_draft_bundle"), draft
    )
    assert first.replayed is second.replayed is False
    assert repository.read(_ref(raw)) == raw
    assert repository.read(
        _ref(draft, revision=1, artifact_kind="full_draft_bundle")
    ) == draft
    disk = b"".join(path.read_bytes() for path in root.iterdir() if path.is_file())
    assert raw not in disk
    assert draft not in disk
    assert (root.stat().st_mode & 0o077) == 0
    assert all((path.stat().st_mode & 0o077) == 0 for path in root.iterdir())


def test_same_ref_payload는_replay하고_different_payload는_conflict다(tmp_path: Path) -> None:
    repository = OwnerLocalAuthoringRepository(tmp_path / "owner", keys=_Keys())
    payload = b"source"
    ref = _ref(payload)
    repository.put(ref, payload)
    assert repository.put(ref, payload).replayed is True
    with pytest.raises(OwnerLocalAuthoringConflict):
        repository.put(ref, b"different")


def test_same_payload_16way는_single_write와_replay다(tmp_path: Path) -> None:
    root = tmp_path / "owner"
    payload = b"source"
    ref = _ref(payload)

    def put(_index: int) -> bool:
        return OwnerLocalAuthoringRepository(
            root, keys=_Keys()
        ).put(ref, payload).replayed

    with ThreadPoolExecutor(max_workers=16) as pool:
        replayed = list(pool.map(put, range(16)))
    assert replayed.count(False) == 1
    assert replayed.count(True) == 15


@pytest.mark.parametrize("mode", ["missing", "wrong", "metadata", "ciphertext"])
def test_missing_wrong_key와_tamper는_failclosed다(tmp_path: Path, mode: str) -> None:
    root = tmp_path / "owner"
    repository = OwnerLocalAuthoringRepository(root, keys=_Keys())
    payload = b"private"
    ref = _ref(payload)
    repository.put(ref, payload)
    if mode == "missing":
        reader = OwnerLocalAuthoringRepository(root, keys=_Keys(None))
    elif mode == "wrong":
        reader = OwnerLocalAuthoringRepository(root, keys=_Keys(b"x" * 32))
    else:
        artifact = next(path for path in root.iterdir() if path.suffix == ".aon")
        data = bytearray(artifact.read_bytes())
        data[-10 if mode == "metadata" else -2] ^= 1
        artifact.write_bytes(data)
        reader = repository
    with pytest.raises(OwnerLocalAuthoringUnavailable):
        reader.read(ref)


@pytest.mark.parametrize(
    "field",
    ["organization_id", "agent_id", "run_id"],
)
def test_path_traversal_ref는_거부한다(field: str) -> None:
    with pytest.raises(ValidationError):
        _ref(b"x", **{field: "../escape"})


def test_root와_artifact_symlink_escape는_거부한다(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root_link = tmp_path / "root-link"
    os.symlink(outside, root_link)
    with pytest.raises(OwnerLocalAuthoringUnavailable):
        OwnerLocalAuthoringRepository(root_link, keys=_Keys())

    root = tmp_path / "owner"
    repository = OwnerLocalAuthoringRepository(root, keys=_Keys())
    payload = b"secret"
    ref = _ref(payload)
    target = outside / "target"
    target.write_bytes(b"outside")
    os.symlink(target, repository.path_for(ref))
    with pytest.raises(OwnerLocalAuthoringUnavailable):
        repository.put(ref, payload)
    assert target.read_bytes() == b"outside"


def test_crash_temp는_open에서_정리하고_delete는_exact_artifact만_지운다(
    tmp_path: Path,
) -> None:
    root = tmp_path / "owner"
    repository = OwnerLocalAuthoringRepository(root, keys=_Keys())
    payload = b"source"
    ref = _ref(payload)
    repository.put(ref, payload)
    temp = root / ".tmp-123-aaaaaaaaaaaaaaaaaaaaaaaa"
    temp.write_bytes(b"partial")
    reopened = OwnerLocalAuthoringRepository(root, keys=_Keys())
    assert not temp.exists()
    assert reopened.delete(ref) is True
    assert reopened.delete(ref) is False


def test_unknown_temp는_자동삭제하지않고_failclosed다(tmp_path: Path) -> None:
    root = tmp_path / "owner"
    root.mkdir()
    unknown = root / ".tmp-unknown"
    unknown.write_bytes(b"do-not-guess")
    with pytest.raises(OwnerLocalAuthoringUnavailable):
        OwnerLocalAuthoringRepository(root, keys=_Keys())
    assert unknown.read_bytes() == b"do-not-guess"


def test_delete_rename_window_external_swap은_B를_지우지않고_quarantine보존한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "owner"
    repository = OwnerLocalAuthoringRepository(root, keys=_Keys())
    payload = b"source-A"
    ref = _ref(payload)
    repository.put(ref, payload)
    target = repository.path_for(ref)
    saved_a = root / "saved-a"
    original_rename = os.rename
    swapped = False

    def hostile_rename(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and source == target.name:
            swapped = True
            os.replace(target, saved_a)
            target.write_bytes(b"external-B")
        original_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "rename", hostile_rename)
    with pytest.raises(OwnerLocalAuthoringUnavailable):
        repository.delete(ref)
    quarantines = list(root.glob(".quarantine-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == b"external-B"
    assert saved_a.exists()
    with pytest.raises(OwnerLocalAuthoringUnavailable):
        OwnerLocalAuthoringRepository(root, keys=_Keys())
    assert quarantines[0].exists()


def test_invalid_utf8_envelope는_typed_unavailable이다(tmp_path: Path) -> None:
    root = tmp_path / "owner"
    repository = OwnerLocalAuthoringRepository(root, keys=_Keys())
    payload = b"source"
    ref = _ref(payload)
    repository.put(ref, payload)
    repository.path_for(ref).write_bytes(b"\xff\xfe\xfa")
    with pytest.raises(OwnerLocalAuthoringUnavailable):
        repository.read(ref)


def test_owner_local_module은_central_sqlite_authoring에_의존하지않는다() -> None:
    import agent_org_network.owner_local_authoring_repository as module

    source = Path(module.__file__).read_text()
    assert "sqlite_production_authoring_runs" not in source
    assert "InMemory" not in source
    assert "Fake" not in source
    central = (
        Path(module.__file__).parent / "sqlite_production_authoring_runs.py"
    ).read_text()
    assert "owner_local_authoring_repository" not in central


@pytest.mark.parametrize("tamper", ["duplicate", "noncanonical"])
def test_envelope는_duplicate_key와_noncanonical_json을_거부한다(
    tmp_path: Path, tamper: str
) -> None:
    root = tmp_path / "owner"
    repository = OwnerLocalAuthoringRepository(root, keys=_Keys())
    payload = b"secret"
    ref = _ref(payload)
    repository.put(ref, payload)
    artifact = repository.path_for(ref)
    raw = artifact.read_bytes()
    if tamper == "duplicate":
        changed = raw.replace(b'{"artifact":', b'{"version":1,"artifact":', 1)
    else:
        changed = raw.replace(b'{"artifact":', b'{ "artifact":', 1)
    artifact.write_bytes(changed)
    with pytest.raises(OwnerLocalAuthoringUnavailable):
        repository.read(ref)


def test_root_path_swap뒤에도_original_inode밖_write_delete가_없다(tmp_path: Path) -> None:
    root = tmp_path / "owner"
    repository = OwnerLocalAuthoringRepository(root, keys=_Keys())
    original = tmp_path / "original-inode"
    root.rename(original)
    root.mkdir()
    decoy = root / "decoy"
    decoy.write_bytes(b"must-survive")
    payload = b"source"
    ref = _ref(payload)
    repository.put(ref, payload)
    assert repository.read(ref) == payload
    assert repository.delete(ref) is True
    assert decoy.read_bytes() == b"must-survive"
    assert list(root.iterdir()) == [decoy]


def test_put_delete_16way는_inode밖을_건드리지않고_valid_state로_수렴한다(
    tmp_path: Path,
) -> None:
    root = tmp_path / "owner"
    repository = OwnerLocalAuthoringRepository(root, keys=_Keys())
    payload = b"source"
    ref = _ref(payload)

    def race(index: int) -> None:
        candidate = OwnerLocalAuthoringRepository(root, keys=_Keys())
        if index % 2:
            candidate.put(ref, payload)
        else:
            candidate.delete(ref)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(race, range(16)))
    try:
        assert repository.read(ref) == payload
    except OwnerLocalAuthoringUnavailable:
        assert repository.delete(ref) is False


def test_plaintext와_envelope_size는_bounded다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert MAX_PLAINTEXT_BYTES == 100 * 1024 * 1024
    assert MAX_ENVELOPE_BYTES > MAX_PLAINTEXT_BYTES
    import agent_org_network.owner_local_authoring_repository as module

    monkeypatch.setattr(module, "MAX_PLAINTEXT_BYTES", 4)
    repository = OwnerLocalAuthoringRepository(tmp_path / "owner", keys=_Keys())
    with pytest.raises(OwnerLocalAuthoringConflict):
        repository.put(_ref(b"12345"), b"12345")
