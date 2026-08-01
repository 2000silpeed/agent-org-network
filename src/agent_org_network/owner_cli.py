"""Console boundary for the fail-closed Card Owner installation skeleton."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys
from typing import TextIO

from agent_org_network.owner_api import OwnerApiRunner, run_owner_api, uvicorn_runner
from agent_org_network.owner_composition import (
    OwnerInstallationConfigurationError,
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
) -> int:
    arguments = build_parser().parse_args(argv)
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        config = load_owner_installation_config(Path(arguments.profile))
        if arguments.command == "doctor":
            if owner_doctor(config):
                print("Owner doctor: ready", file=out)
                return 0
            print("Owner doctor: unavailable", file=err)
            return EXIT_UNAVAILABLE
        if arguments.command == "api" and arguments.api_command == "serve":
            run_owner_api(compose_owner(config), runner)
            return 0
    except OwnerInstallationConfigurationError:
        print("Owner installation configuration unavailable", file=err)
        return EXIT_CONFIGURATION
    print("Owner capability unavailable", file=err)
    return EXIT_UNAVAILABLE


def _profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", required=True, metavar="PATH")
