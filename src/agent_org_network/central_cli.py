"""Console boundary for the fail-closed Central installation skeleton."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
import sys
from typing import TextIO

from agent_org_network.central_api import CentralApiRunner, run_central_api, uvicorn_runner
from agent_org_network.central_composition import (
    CentralCompositionUnavailable,
    CentralInstallationConfigurationError,
    CentralProductionUnavailable,
    bootstrap_central_admin,
    central_doctor,
    compose_central,
    load_central_installation_config,
    migrate_central_schema,
)
from agent_org_network.central_bootstrap_admin import (
    BootstrapAdminConfigurationError,
    BootstrapAdminConflict,
    BootstrapAdminDenied,
    BootstrapAdminResult,
    BootstrapAdminUnavailable,
    BootstrapOidcDeviceAuthorizer,
)
from agent_org_network.central_web_runtime import (
    CentralWebRuntimeConfigurationError,
    CentralWebRuntimeUnavailable,
    CentralWebLaunchSpec,
    build_central_web_launch_spec,
    discover_default_central_next_artifact,
    resolve_node_from_path,
    run_central_web_child,
)


EXIT_UNAVAILABLE = 69
EXIT_CONFLICT = 75
EXIT_DENIED = 77
EXIT_CONFIGURATION = 78


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aon-central")
    commands = parser.add_subparsers(dest="command", required=True)
    _profile_argument(commands.add_parser("migrate"))
    _profile_argument(commands.add_parser("doctor"))
    bootstrap = commands.add_parser("bootstrap-admin")
    _profile_argument(bootstrap)
    bootstrap.add_argument("--attestation", required=True, metavar="PATH")
    web = commands.add_parser("web")
    web_commands = web.add_subparsers(dest="web_command", required=True)
    _profile_argument(web_commands.add_parser("serve"))
    api = commands.add_parser("api")
    api_commands = api.add_subparsers(dest="api_command", required=True)
    _profile_argument(api_commands.add_parser("serve"))
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CentralApiRunner = uvicorn_runner,
    bootstrap_device_authorizer: BootstrapOidcDeviceAuthorizer | None = None,
    web_runner: Callable[[CentralWebLaunchSpec], int] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run a command and return a stable process exit status."""
    arguments = build_parser().parse_args(argv)
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        config = load_central_installation_config(Path(arguments.profile))
        if arguments.command == "migrate":
            migrate_central_schema(config)
            print("Central schema migrated", file=out)
            return 0
        if arguments.command == "doctor":
            if central_doctor(config):
                print("Central doctor: ready", file=out)
                return 0
            print("Central doctor: unavailable", file=err)
            return EXIT_UNAVAILABLE
        if arguments.command == "bootstrap-admin":
            result = bootstrap_central_admin(
                config,
                attestation_path=Path(arguments.attestation),
                device_authorizer=bootstrap_device_authorizer,
            )
            _print_bootstrap_result(result, out)
            return 0
        if arguments.command == "web" and arguments.web_command == "serve":
            artifact = discover_default_central_next_artifact()
            node, version = resolve_node_from_path()
            spec = build_central_web_launch_spec(
                config, artifact=artifact, node_executable=node, node_version=version
            )
            return (web_runner or run_central_web_child)(spec)
        if arguments.command == "api" and arguments.api_command == "serve":
            run_central_api(compose_central(config), runner)
            return 0
    except (CentralProductionUnavailable, CentralCompositionUnavailable):
        print("Central production capability unavailable", file=err)
        return EXIT_UNAVAILABLE
    except BootstrapAdminConflict:
        print("Central bootstrap-admin: conflict", file=err)
        return EXIT_CONFLICT
    except BootstrapAdminDenied:
        print("Central bootstrap-admin: denied", file=err)
        return EXIT_DENIED
    except BootstrapAdminUnavailable:
        print("Central bootstrap-admin: unavailable", file=err)
        return EXIT_UNAVAILABLE
    except BootstrapAdminConfigurationError:
        print("Central bootstrap-admin: configuration unavailable", file=err)
        return EXIT_CONFIGURATION
    except CentralWebRuntimeConfigurationError:
        print("Central web configuration unavailable", file=err)
        return EXIT_CONFIGURATION
    except CentralWebRuntimeUnavailable:
        print("Central web capability unavailable", file=err)
        return EXIT_UNAVAILABLE
    except CentralInstallationConfigurationError:
        print("Central installation configuration unavailable", file=err)
        return EXIT_CONFIGURATION
    print("Central command unavailable", file=err)
    return EXIT_UNAVAILABLE


def _profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", required=True, metavar="PATH")


def _print_bootstrap_result(result: BootstrapAdminResult, stream: TextIO) -> None:
    """Emit only the explicitly public-safe bootstrap completion projection."""
    print(
        "Central bootstrap-admin: "
        f"{result.state} attestation={result.attestation_id} "
        f"registry-user={result.registry_user_id} revision={result.revision} "
        f"registration-digest={result.registration_command_digest} "
        f"seal-digest={result.seal_digest} replayed={str(result.replayed).lower()}",
        file=stream,
    )
