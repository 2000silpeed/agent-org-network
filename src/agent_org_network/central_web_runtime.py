"""RB3.2b.1 Central Next artifact validation and child lifecycle contract."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
import signal
import subprocess
import hashlib
import json
import math
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, cast

from agent_org_network.central_composition import (
    CentralInstallationConfig,
    CentralInstallationConfigurationError,
    validate_central_installation_config,
)


_BACKEND_URL = "http://127.0.0.1:8010"
_HOST = "127.0.0.1"
_PORT = 3000
_NODE_VERSION = re.compile(r"^v?(0|[1-9][0-9]*)\.[0-9]+\.[0-9]+$")
_BUILD_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_MANIFEST_ENTRIES = 20_000
_MAX_RELATIVE_PATH_BYTES = 512
_MAX_ARTIFACT_FILE_BYTES = 256 * 1024 * 1024


class CentralWebRuntimeConfigurationError(ValueError):
    """The Central profile or caller-supplied executable reference is invalid."""


class CentralWebRuntimeUnavailable(RuntimeError):
    """A required local Central Next capability is missing, foreign, or corrupt."""


class CentralWebChildExit(RuntimeError):
    """Typed child-only result for the future process parent to surface unchanged."""

    def __init__(self, exit_code: int) -> None:
        if type(exit_code) is not int or exit_code < 0 or exit_code > 255:
            raise ValueError("bounded child exit code required")
        self.exit_code = exit_code
        super().__init__()


@dataclass(frozen=True, slots=True)
class CentralNextArtifact:
    """Validated standalone directory; all paths are capability roots, not build inputs."""

    root: Path
    server_js: Path
    build_id: Path
    static_dir: Path
    public_dir: Path


@dataclass(frozen=True, slots=True)
class CentralWebLaunchSpec:
    """Pure launch input. Process, port and signal ownership remains with the runtime slice."""

    node_executable: Path
    artifact: CentralNextArtifact
    argv: tuple[str, str]
    env: Mapping[str, str]
    bind_host: str
    port: int
    upstream: str


class _ChildProcess(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


ChildSpawner = Callable[[tuple[str, str], Path, dict[str, str]], _ChildProcess]
SignalRegistrar = Callable[[int, object], object]
SignalMasker = Callable[[int, set[int]], set[int]]
_TERMINATION_SIGNALS = (int(signal.SIGINT), int(signal.SIGTERM))


def parse_node_major_version(version: str) -> int:
    """Accept only a finite canonical Node semver output with a supported major."""
    if type(version) is not str:
        raise CentralWebRuntimeUnavailable()
    match = _NODE_VERSION.fullmatch(version)
    if match is None:
        raise CentralWebRuntimeUnavailable()
    major = int(match.group(1))
    if major < 24:
        raise CentralWebRuntimeUnavailable()
    return major


def discover_central_next_artifact(
    *,
    source_checkout_root: Path | None = None,
    installed_package_root: Path | None = None,
) -> CentralNextArtifact:
    """Discover exactly one prebuilt source or bundled-package standalone capability.

    A source root resolves only to ``frontend/.next/standalone``; an installed resource
    root resolves only to ``.next/standalone``. No build, download or alternate lookup is
    attempted when the selected capability is incomplete.
    """
    if (source_checkout_root is None) == (installed_package_root is None):
        raise CentralWebRuntimeConfigurationError("one artifact discovery root required")
    if source_checkout_root is not None:
        root = _source_standalone_root(source_checkout_root)
    else:
        assert installed_package_root is not None
        root = _installed_standalone_root(installed_package_root)
    return _validate_artifact(root)


def build_central_web_launch_spec(
    config: CentralInstallationConfig,
    *,
    artifact: CentralNextArtifact,
    node_executable: Path,
    node_version: str,
) -> CentralWebLaunchSpec:
    """Create the fixed child projection; no caller environment or upstream is accepted."""
    try:
        validate_central_installation_config(config)
    except CentralInstallationConfigurationError as error:
        raise CentralWebRuntimeConfigurationError() from error
    if type(artifact) is not CentralNextArtifact:
        raise CentralWebRuntimeConfigurationError()
    _validate_artifact(artifact.root)
    node = _validate_node(node_executable, node_version)
    env = MappingProxyType(
        {
            "AON_FRONTEND_MODE": "central-local-reference",
            "AON_PUBLIC_ORIGIN": config.central_public_origin,
            "AON_BACKEND_URL": _BACKEND_URL,
            "HOSTNAME": _HOST,
            "PORT": str(_PORT),
            "NODE_ENV": "production",
        }
    )
    return CentralWebLaunchSpec(
        node_executable=node,
        artifact=artifact,
        argv=(str(node), str(artifact.server_js)),
        env=env,
        bind_host=_HOST,
        port=_PORT,
        upstream=_BACKEND_URL,
    )


def resolve_node_from_path() -> tuple[Path, str]:
    """Resolve and preflight Node before any browser child can bind."""
    raw = shutil.which("node")
    if raw is None:
        raise CentralWebRuntimeUnavailable()
    node = Path(raw)
    try:
        completed = subprocess.run(
            [str(node), "--version"], check=False, capture_output=True, text=True, timeout=5
        )
    except Exception as error:
        raise CentralWebRuntimeUnavailable() from error
    if completed.returncode != 0:
        raise CentralWebRuntimeUnavailable()
    return _validate_node(node, completed.stdout.strip()), completed.stdout.strip()


def run_central_web_child(
    spec: CentralWebLaunchSpec,
    *,
    spawn: ChildSpawner | None = None,
    register_signal: SignalRegistrar | None = None,
    signal_mask: SignalMasker | None = None,
    wait_timeout_seconds: float = 5.0,
) -> int:
    """Run a prevalidated child and deterministically reap it on termination.

    Signal handlers are installed before the child can be spawned.  Unix also
    blocks the termination signals while the handler/child reference is made,
    so a pending signal is delivered only after the child is owned and can be
    reaped.  Platforms without ``pthread_sigmask`` retain the same pre-spawn
    handler ordering without claiming that unavailable OS primitive.
    """
    if (
        type(spec) is not CentralWebLaunchSpec
        or type(wait_timeout_seconds) not in {int, float}
        or not math.isfinite(float(wait_timeout_seconds))
        or not 0 < float(wait_timeout_seconds) <= 60
    ):
        raise CentralWebRuntimeUnavailable()
    spawn_child = spawn or _spawn_child
    set_signal = register_signal or cast(SignalRegistrar, signal.signal)
    set_mask = signal_mask if signal_mask is not None else _default_signal_masker()
    child: _ChildProcess | None = None
    child_reaped = False
    previous: dict[int, object] = {}
    previous_mask: set[int] | None = None
    mask_blocked = False
    result: int | None = None
    failure: BaseException | None = None
    restoration_failure: Exception | None = None

    def cleanup(*, force: bool = False) -> None:
        nonlocal child_reaped
        if child is None or (child_reaped and not force):
            return
        try:
            child.terminate()
            child.wait(timeout=float(wait_timeout_seconds))
            child_reaped = True
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=float(wait_timeout_seconds))
            child_reaped = True
        except Exception as error:
            raise CentralWebRuntimeUnavailable() from error

    def stop(signum: int, _frame: object) -> None:
        # A signal can arrive after handlers are installed but before spawn on
        # a platform without pthread_sigmask.  There is no child to orphan in
        # that state; restoration is still performed by the outer finally.
        cleanup()
        raise SystemExit(128 + signum)

    try:
        if set_mask is not None:
            previous_mask = set_mask(signal.SIG_BLOCK, set(_TERMINATION_SIGNALS))
            mask_blocked = True
        for current in _TERMINATION_SIGNALS:
            previous[current] = set_signal(current, stop)
        child = spawn_child(spec.argv, spec.artifact.root, dict(spec.env))
        if mask_blocked:
            assert previous_mask is not None
            masker = set_mask
            assert masker is not None
            masker(signal.SIG_SETMASK, previous_mask)
            mask_blocked = False
        code = child.wait()
        if type(code) is not int or not -255 <= code <= 255:
            raise CentralWebRuntimeUnavailable()
        child_reaped = True
        result = code if code >= 0 else 128 + abs(code)
    except BaseException as error:
        failure = error
        if child is not None and not child_reaped:
            try:
                cleanup()
            except CentralWebRuntimeUnavailable as cleanup_failure:
                restoration_failure = cleanup_failure
    finally:
        try:
            _restore_handlers(previous, set_signal)
        except Exception as restore_error:
            restoration_failure = restoration_failure or restore_error
        if mask_blocked:
            try:
                assert set_mask is not None and previous_mask is not None
                set_mask(signal.SIG_SETMASK, previous_mask)
            except Exception as restore_error:
                restoration_failure = restoration_failure or restore_error
        if restoration_failure is not None and child is not None:
            try:
                # Preserve the previous fail-closed contract: if restoring the
                # parent signal state fails, terminate/reap the child even
                # after its observed wait result cannot be safely trusted.
                cleanup(force=True)
            except CentralWebRuntimeUnavailable:
                pass
    if restoration_failure is not None:
        raise CentralWebRuntimeUnavailable() from restoration_failure
    if failure is not None:
        if isinstance(failure, SystemExit):
            raise failure
        raise CentralWebRuntimeUnavailable() from failure
    assert result is not None
    return result


def _spawn_child(argv: tuple[str, str], cwd: Path, env: dict[str, str]) -> _ChildProcess:
    return subprocess.Popen(argv, cwd=cwd, env=env)


def _restore_handlers(previous: Mapping[int, object], registrar: SignalRegistrar) -> None:
    for current, handler in previous.items():
        registrar(current, handler)


def _default_signal_masker() -> SignalMasker | None:
    masker = getattr(signal, "pthread_sigmask", None)
    if masker is None:
        return None
    return cast(SignalMasker, masker)


def _source_standalone_root(root: Path) -> Path:
    return _safe_directory(root) / "frontend" / ".next" / "standalone"


def _installed_standalone_root(root: Path) -> Path:
    return _safe_directory(root) / ".next" / "standalone"


def _safe_directory(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise CentralWebRuntimeConfigurationError()
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CentralWebRuntimeUnavailable() from error
    if not resolved.is_dir() or resolved.is_symlink():
        raise CentralWebRuntimeUnavailable()
    return resolved


def _validate_artifact(root: Path) -> CentralNextArtifact:
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise CentralWebRuntimeUnavailable() from error
    if root.is_symlink() or not resolved_root.is_dir():
        raise CentralWebRuntimeUnavailable()
    server = _contained_file(resolved_root, "server.js")
    build_id = _contained_file(resolved_root, ".next", "BUILD_ID")
    static_dir = _contained_directory(resolved_root, ".next", "static")
    public_dir = _contained_directory(resolved_root, "public")
    _validate_manifest(resolved_root, server, build_id, static_dir, public_dir)
    return CentralNextArtifact(resolved_root, server, build_id, static_dir, public_dir)


def _validate_manifest(root: Path, server: Path, build_id: Path, static_dir: Path, public_dir: Path) -> None:
    manifest = _contained_file(root, "aon-standalone-manifest.json")
    try:
        if manifest.stat().st_size > _MAX_MANIFEST_BYTES:
            raise ValueError
        value: object = json.loads(
            manifest.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
        if not isinstance(value, dict):
            raise ValueError
        typed_value = cast(dict[str, object], value)
        if set(typed_value) != {"version", "files"}:
            raise ValueError
        if typed_value["version"] != 1:
            raise ValueError
        files = typed_value["files"]
        if not isinstance(files, dict) or not files:
            raise ValueError
        typed_files = cast(dict[str, object], files)
        if len(typed_files) > _MAX_MANIFEST_ENTRIES:
            raise ValueError
        actual = _regular_artifact_files(root, manifest)
        if set(typed_files) != set(actual):
            raise ValueError
        for name, path in actual.items():
            digest = typed_files.get(name)
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError
            if _file_digest(path) != digest:
                raise ValueError
        if (
            server.relative_to(root).as_posix() not in typed_files
            or build_id.relative_to(root).as_posix() not in typed_files
        ):
            raise ValueError
        if build_id.stat().st_size > 128:
            raise ValueError
        build = build_id.read_text(encoding="utf-8")
        if (
            _BUILD_ID.fullmatch(build) is None
            or static_dir.relative_to(root).as_posix() != ".next/static"
            or public_dir.relative_to(root).as_posix() != "public"
        ):
            raise ValueError
    except Exception as error:
        raise CentralWebRuntimeUnavailable() from error


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _regular_artifact_files(root: Path, manifest: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        # Next runtime caches (notably image optimization) are mutable process
        # data, never release content.  Their creation or replacement must not
        # turn a verified standalone artifact into an unavailable one.
        if relative == ".next/cache" or relative.startswith(".next/cache/"):
            continue
        if path.is_symlink() or not path.is_relative_to(root):
            raise ValueError
        if path.is_dir():
            continue
        if path == manifest:
            continue
        if not path.is_file():
            raise ValueError
        if (
            not relative
            or len(relative.encode("utf-8")) > _MAX_RELATIVE_PATH_BYTES
            or relative.startswith("/")
            or ".." in Path(relative).parts
            or path.stat().st_size > _MAX_ARTIFACT_FILE_BYTES
        ):
            raise ValueError
        files[relative] = path
    if len(files) > _MAX_MANIFEST_ENTRIES:
        raise ValueError
    return files


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def discover_default_central_next_artifact() -> CentralNextArtifact:
    """Use module-relative source layout first, otherwise the bundled wheel resource."""
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "frontend").is_dir():
        return discover_central_next_artifact(source_checkout_root=source_root)
    return discover_central_next_artifact(installed_package_root=Path(__file__).resolve().parent / "_central_frontend")


def _contained_file(root: Path, *parts: str) -> Path:
    path = _contained(root, *parts)
    if not path.is_file():
        raise CentralWebRuntimeUnavailable()
    return path


def _contained_directory(root: Path, *parts: str) -> Path:
    path = _contained(root, *parts)
    if not path.is_dir():
        raise CentralWebRuntimeUnavailable()
    return path


def _contained(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise CentralWebRuntimeUnavailable() from error
    if path.is_symlink() or resolved != path:
        raise CentralWebRuntimeUnavailable()
    return path


def _validate_node(node_executable: Path, node_version: str) -> Path:
    if not node_executable.is_absolute():
        raise CentralWebRuntimeConfigurationError()
    try:
        node = node_executable.resolve(strict=True)
    except OSError as error:
        raise CentralWebRuntimeUnavailable() from error
    if not node.is_file() or not os.access(node, os.X_OK):
        raise CentralWebRuntimeUnavailable()
    parse_node_major_version(node_version)
    return node


__all__ = [
    "CentralNextArtifact",
    "CentralWebChildExit",
    "CentralWebLaunchSpec",
    "CentralWebRuntimeConfigurationError",
    "CentralWebRuntimeUnavailable",
    "build_central_web_launch_spec",
    "discover_central_next_artifact",
    "discover_default_central_next_artifact",
    "parse_node_major_version",
    "resolve_node_from_path",
    "run_central_web_child",
]
