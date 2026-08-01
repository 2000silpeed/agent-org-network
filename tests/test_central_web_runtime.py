"""RB3.2b.1 Central Next process-contract gates (no child process launch)."""

from __future__ import annotations

from pathlib import Path
import stat
import hashlib
import json
import signal
from typing import cast

import pytest

from agent_org_network.central_composition import CentralInstallationConfig
from agent_org_network.central_web_runtime import (
    CentralWebRuntimeConfigurationError,
    CentralWebRuntimeUnavailable,
    build_central_web_launch_spec,
    discover_central_next_artifact,
    parse_node_major_version,
    run_central_web_child,
)


def _config(tmp_path: Path, **changes: object) -> CentralInstallationConfig:
    values: dict[str, object] = {
        "profile": "local-reference", "org_id": "acme", "oidc_provider_id": "oidc",
        "oidc_issuer": "https://issuer.test", "oidc_audience": "central",
        "oidc_jwks_url": "https://issuer.test/jwks",
        "bootstrap_oidc_device_authorization_url": "https://issuer.test/device",
        "bootstrap_oidc_device_client_id": "client", "bootstrap_oidc_scope": "openid",
        "central_public_origin": "https://central.example.test",
        "browser_oidc_authorization_url": "https://issuer.test/authorize",
        "browser_oidc_token_url": "https://issuer.test/token",
        "browser_oidc_client_id": "aon-central-browser",
        "browser_oidc_scope": "openid email",
        "authority_snapshot_path": tmp_path / "authority.yaml", "database_path": tmp_path / "db.sqlite",
        "data_directory": tmp_path / "data", "bind_host": "127.0.0.1", "port": 8010,
    }
    values.update(changes)
    return CentralInstallationConfig(**values)  # type: ignore[arg-type]


def _artifact(root: Path, *, source: bool = True) -> Path:
    frontend = root / "frontend" if source else root
    standalone = frontend / ".next" / "standalone"
    (standalone / ".next" / "static").mkdir(parents=True)
    (standalone / "public").mkdir()
    (standalone / "server.js").write_text("server", encoding="utf-8")
    (standalone / ".next" / "BUILD_ID").write_text("build", encoding="utf-8")
    (standalone / ".next" / "static" / "asset.js").write_text("asset", encoding="utf-8")
    (standalone / "public" / "asset.txt").write_text("asset", encoding="utf-8")
    files = {
        path.relative_to(standalone).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in standalone.rglob("*")
        if path.is_file()
    }
    (standalone / "aon-standalone-manifest.json").write_text(json.dumps({"version": 1, "files": files}), encoding="utf-8")
    return standalone


def _node(tmp_path: Path) -> Path:
    node = tmp_path / "node"
    node.write_text("node", encoding="utf-8")
    node.chmod(node.stat().st_mode | stat.S_IXUSR)
    return node


def test_source_artifact와_fixed_launch_spec은_caller_environment를_복사하지않는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _artifact(tmp_path)
    monkeypatch.setenv("AON_BACKEND_URL", "https://caller.invalid")
    monkeypatch.setenv("SECRET", "caller-secret")
    artifact = discover_central_next_artifact(source_checkout_root=tmp_path)
    spec = build_central_web_launch_spec(
        _config(tmp_path), artifact=artifact, node_executable=_node(tmp_path), node_version="v24.1.0"
    )

    assert spec.bind_host == "127.0.0.1" and spec.port == 3000
    assert spec.upstream == "http://127.0.0.1:8010"
    assert dict(spec.env) == {
        "AON_FRONTEND_MODE": "central-local-reference", "AON_PUBLIC_ORIGIN": "https://central.example.test",
        "AON_BACKEND_URL": "http://127.0.0.1:8010", "HOSTNAME": "127.0.0.1", "PORT": "3000", "NODE_ENV": "production",
    }
    assert "SECRET" not in spec.env


def test_installed_bundled_artifact_root를_명시적으로_발견한다(tmp_path: Path) -> None:
    standalone = _artifact(tmp_path, source=False)
    assert discover_central_next_artifact(installed_package_root=tmp_path).root == standalone


def test_next_mutable_cache는_manifest_밖이라_실행후에도_artifact를_재검증할수있다(tmp_path: Path) -> None:
    standalone = _artifact(tmp_path)
    cache_file = standalone / ".next" / "cache" / "images" / "runtime-cache"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"mutable")

    assert discover_central_next_artifact(source_checkout_root=tmp_path).root == standalone
    cache_file.write_bytes(b"replaced-after-runtime")
    assert discover_central_next_artifact(source_checkout_root=tmp_path).root == standalone


@pytest.mark.parametrize("version", ["v23.99.0", "24", "v24.x", "vInfinity.0.0", "v24.0.0 extra"])
def test_node_version은_finite_24이상만_허용한다(version: str) -> None:
    with pytest.raises(CentralWebRuntimeUnavailable):
        parse_node_major_version(version)
    assert parse_node_major_version("v24.0.0") == 24


@pytest.mark.parametrize("path_kind", ["server", "build", "static", "public"])
def test_artifact_missing_or_symlink은_fail_close다(tmp_path: Path, path_kind: str) -> None:
    frontend = _artifact(tmp_path)
    paths = {
        "server": frontend / "server.js",
        "build": frontend / ".next" / "BUILD_ID",
        "static": frontend / ".next" / "static",
        "public": frontend / "public",
    }
    target = paths[path_kind]
    target.rename(target.with_name(target.name + ".real"))
    target.symlink_to(target.with_name(target.name + ".real"), target_is_directory=path_kind in {"static", "public"})
    with pytest.raises(CentralWebRuntimeUnavailable):
        discover_central_next_artifact(source_checkout_root=tmp_path)


def test_invalid_Central_config_and_relative_or_nonexecutable_node은_configuration_failure다(tmp_path: Path) -> None:
    _artifact(tmp_path)
    artifact = discover_central_next_artifact(source_checkout_root=tmp_path)
    with pytest.raises(CentralWebRuntimeConfigurationError):
        build_central_web_launch_spec(
            _config(tmp_path, port=8011), artifact=artifact, node_executable=Path("node"), node_version="v24.0.0"
        )
    node = tmp_path / "not-executable"
    node.write_text("node", encoding="utf-8")
    with pytest.raises(CentralWebRuntimeUnavailable):
        build_central_web_launch_spec(_config(tmp_path), artifact=artifact, node_executable=node, node_version="v24.0.0")


class _Child:
    def __init__(self, *, exit_code: int = 0, invoke_signal: bool = False) -> None:
        self.exit_code = exit_code
        self.invoke_signal = invoke_signal
        self.handlers: dict[int, object] = {}
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if timeout is None and self.invoke_signal:
            handler = self.handlers[int(signal.SIGINT)]
            handler(int(signal.SIGINT), None)  # type: ignore[operator]
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_child_runner_forwards_exact_spec_and_normalizes_exit_and_restores_handlers(tmp_path: Path) -> None:
    _artifact(tmp_path)
    artifact = discover_central_next_artifact(source_checkout_root=tmp_path)
    spec = build_central_web_launch_spec(_config(tmp_path), artifact=artifact, node_executable=_node(tmp_path), node_version="v24.0.0")
    child = _Child(exit_code=-15)
    received: list[object] = []

    def spawn(argv: tuple[str, str], cwd: Path, env: dict[str, str]) -> _Child:
        assert set(child.handlers) == {int(signal.SIGINT), int(signal.SIGTERM)}
        assert all(callable(handler) for handler in child.handlers.values())
        received.extend((argv, cwd, env))
        return child

    def register(number: int, handler: object) -> object:
        previous = child.handlers.get(number, "original")
        child.handlers[number] = handler
        return previous

    assert run_central_web_child(spec, spawn=spawn, register_signal=register) == 143
    assert received == [spec.argv, spec.artifact.root, dict(spec.env)]
    assert child.handlers == {int(signal.SIGINT): "original", int(signal.SIGTERM): "original"}


def test_child_runner_signal_and_registration_failure_restore_without_spawning(tmp_path: Path) -> None:
    _artifact(tmp_path)
    artifact = discover_central_next_artifact(source_checkout_root=tmp_path)
    spec = build_central_web_launch_spec(_config(tmp_path), artifact=artifact, node_executable=_node(tmp_path), node_version="v24.0.0")
    child = _Child(invoke_signal=True)

    def register(number: int, handler: object) -> object:
        previous = child.handlers.get(number, "original")
        child.handlers[number] = handler
        return previous

    def spawn_signal(_argv: tuple[str, str], _cwd: Path, _env: dict[str, str]) -> _Child:
        return child

    with pytest.raises(SystemExit) as stopped:
        run_central_web_child(spec, spawn=spawn_signal, register_signal=register)
    assert stopped.value.code == 130 and child.terminated and not child.killed

    handlers: dict[int, object] = {}
    spawn_called = False

    def failing_register(number: int, _handler: object) -> object:
        if number == int(signal.SIGTERM):
            raise RuntimeError
        handlers[number] = _handler
        return "original"

    def spawn_failed(_argv: tuple[str, str], _cwd: Path, _env: dict[str, str]) -> _Child:
        nonlocal spawn_called
        spawn_called = True
        raise AssertionError("registration must finish before spawning")

    with pytest.raises(CentralWebRuntimeUnavailable):
        run_central_web_child(spec, spawn=spawn_failed, register_signal=failing_register)
    assert not spawn_called
    assert handlers[int(signal.SIGINT)] == "original"


def test_child_runner_unmasks_queued_signal_only_after_child_reference_and_reaps_it(
    tmp_path: Path,
) -> None:
    _artifact(tmp_path)
    artifact = discover_central_next_artifact(source_checkout_root=tmp_path)
    spec = build_central_web_launch_spec(_config(tmp_path), artifact=artifact, node_executable=_node(tmp_path), node_version="v24.0.0")
    child = _Child()
    handlers: dict[int, object] = {}
    events: list[object] = []
    unmasked_once = False

    def register(number: int, handler: object) -> object:
        events.append(("register", number))
        previous = handlers.get(number, "original")
        handlers[number] = handler
        return previous

    def spawn(_argv: tuple[str, str], _cwd: Path, _env: dict[str, str]) -> _Child:
        events.append("spawn")
        assert set(handlers) == {int(signal.SIGINT), int(signal.SIGTERM)}
        return child

    def mask(how: int, watched: set[int]) -> set[int]:
        nonlocal unmasked_once
        events.append(("mask", how, watched))
        if how == signal.SIG_BLOCK:
            assert watched == {int(signal.SIGINT), int(signal.SIGTERM)}
            return {99}
        assert how == signal.SIG_SETMASK
        assert watched == {99}
        if unmasked_once:
            return set()
        unmasked_once = True
        handler = handlers[int(signal.SIGTERM)]
        handler(int(signal.SIGTERM), None)  # type: ignore[operator]
        raise AssertionError("queued signal must terminate control flow")

    with pytest.raises(SystemExit) as stopped:
        run_central_web_child(spec, spawn=spawn, register_signal=register, signal_mask=mask)
    assert stopped.value.code == 143
    assert child.terminated and not child.killed
    assert handlers == {int(signal.SIGINT): "original", int(signal.SIGTERM): "original"}
    assert events[:4] == [
        ("mask", signal.SIG_BLOCK, {int(signal.SIGINT), int(signal.SIGTERM)}),
        ("register", int(signal.SIGINT)),
        ("register", int(signal.SIGTERM)),
        "spawn",
    ]


def test_child_runner_retries_mask_restore_after_ordinary_unmask_failure(tmp_path: Path) -> None:
    _artifact(tmp_path)
    artifact = discover_central_next_artifact(source_checkout_root=tmp_path)
    spec = build_central_web_launch_spec(_config(tmp_path), artifact=artifact, node_executable=_node(tmp_path), node_version="v24.0.0")
    child = _Child()
    handlers: dict[int, object] = {}
    unmask_calls = 0

    def register(number: int, handler: object) -> object:
        previous = handlers.get(number, "original")
        handlers[number] = handler
        return previous

    def mask(how: int, watched: set[int]) -> set[int]:
        nonlocal unmask_calls
        if how == signal.SIG_BLOCK:
            assert watched == {int(signal.SIGINT), int(signal.SIGTERM)}
            return {99}
        assert how == signal.SIG_SETMASK and watched == {99}
        unmask_calls += 1
        if unmask_calls == 1:
            raise RuntimeError("mask restore failed")
        return set()

    with pytest.raises(CentralWebRuntimeUnavailable):
        run_central_web_child(
            spec, spawn=lambda _argv, _cwd, _env: child, register_signal=register, signal_mask=mask
        )
    assert unmask_calls == 2
    assert child.terminated
    assert handlers == {int(signal.SIGINT): "original", int(signal.SIGTERM): "original"}


@pytest.mark.parametrize("timeout", [True, float("nan"), float("inf")])
def test_child_runner_rejects_nonfinite_or_boolean_timeout(tmp_path: Path, timeout: float | bool) -> None:
    _artifact(tmp_path)
    artifact = discover_central_next_artifact(source_checkout_root=tmp_path)
    spec = build_central_web_launch_spec(_config(tmp_path), artifact=artifact, node_executable=_node(tmp_path), node_version="v24.0.0")
    with pytest.raises(CentralWebRuntimeUnavailable):
        run_central_web_child(spec, wait_timeout_seconds=timeout)


def test_child_runner_invalid_wait_result_and_restore_failure_are_unavailable(tmp_path: Path) -> None:
    _artifact(tmp_path)
    artifact = discover_central_next_artifact(source_checkout_root=tmp_path)
    spec = build_central_web_launch_spec(_config(tmp_path), artifact=artifact, node_executable=_node(tmp_path), node_version="v24.0.0")

    class InvalidResultChild(_Child):
        def __init__(self, result: object) -> None:
            super().__init__()
            self.result = result

        def wait(self, timeout: float | None = None) -> int:
            _ = timeout
            return cast(int, self.result)

    for result in (256, "not-an-exit"):
        invalid = InvalidResultChild(result)
        with pytest.raises(CentralWebRuntimeUnavailable):
            run_central_web_child(spec, spawn=lambda _argv, _cwd, _env: invalid)  # type: ignore[arg-type]
        assert invalid.terminated

    child = _Child()
    calls = 0
    def restore_fails(number: int, _handler: object) -> object:
        nonlocal calls
        calls += 1
        if calls > 2:
            raise RuntimeError
        return number
    with pytest.raises(CentralWebRuntimeUnavailable):
        run_central_web_child(spec, spawn=lambda _argv, _cwd, _env: child, register_signal=restore_fails)  # type: ignore[arg-type]
    assert child.terminated
