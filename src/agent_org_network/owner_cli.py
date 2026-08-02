"""Console boundary for the fail-closed Card Owner installation skeleton."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sys
from typing import TextIO

from agent_org_network.owner_api import OwnerApiRunner, run_owner_api, uvicorn_runner
from agent_org_network.owner_composition import (
    OwnerInstallationConfigurationError,
    OwnerPairingAdapter,
    OwnerPairingRequest,
    OwnerPairingResultProjection,
    compose_owner,
    load_owner_installation_config,
    owner_doctor,
)


EXIT_UNAVAILABLE = 69
EXIT_CONFIGURATION = 78


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aon-owner")
    commands = parser.add_subparsers(dest="command", required=True)
    _profile_argument(commands.add_parser("pair"))
    _profile_argument(commands.add_parser("unpair"))
    _profile_argument(commands.add_parser("doctor"))
    api = commands.add_parser("api")
    api_commands = api.add_subparsers(dest="api_command", required=True)
    _profile_argument(api_commands.add_parser("serve"))
    workspace = commands.add_parser("workspace")
    workspace_commands = workspace.add_subparsers(dest="workspace_command", required=True)
    _profile_argument(workspace_commands.add_parser("serve"))
    _profile_argument(commands.add_parser("worker"))
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: OwnerApiRunner = uvicorn_runner,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
    pairing_adapter: OwnerPairingAdapter | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        config = load_owner_installation_config(Path(arguments.profile))
        composition = compose_owner(config, pairing_adapter=pairing_adapter)
        if arguments.command == "doctor":
            if owner_doctor(config, pairing_readiness=composition.pairing_readiness):
                print("Owner doctor: ready", file=out)
                return 0
            print("Owner doctor: unavailable", file=err)
            return EXIT_UNAVAILABLE
        if arguments.command == "pair":
            if pairing_adapter is None:
                print("Owner pairing adapter unavailable", file=err)
                return EXIT_UNAVAILABLE
            request = _read_pairing_request(stdin or sys.stdin)
            value = pairing_adapter.pair(config, request)
            projection = OwnerPairingResultProjection.model_validate(value)
            print(projection.model_dump_json(), file=out)
            return 0
        if arguments.command == "api" and arguments.api_command == "serve":
            run_owner_api(composition, runner)
            return 0
    except (OwnerInstallationConfigurationError, ValueError, json.JSONDecodeError):
        print("Owner installation configuration unavailable", file=err)
        return EXIT_CONFIGURATION
    except Exception:
        print("Owner pairing unavailable", file=err)
        return EXIT_UNAVAILABLE
    print("Owner capability unavailable", file=err)
    return EXIT_UNAVAILABLE


def _profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", required=True, metavar="PATH")


def _read_pairing_request(stdin: TextIO) -> OwnerPairingRequest:
    """Read one bounded JSON request; pairing code is never persisted in profile."""
    if getattr(stdin, "isatty", lambda: False)():
        raise OwnerInstallationConfigurationError(
            "pairing request JSON must be supplied on stdin"
        )
    payload = stdin.read(16 * 1024 + 1)
    if len(payload) > 16 * 1024:
        raise OwnerInstallationConfigurationError("pairing request is too large")
    return OwnerPairingRequest.model_validate_json(payload)
