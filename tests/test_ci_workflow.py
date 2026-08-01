"""ADR 0073 CI event and verification-tier contract."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SCRIPTS = {
    "fast": ROOT / "scripts" / "verify-fast.sh",
    "contract": ROOT / "scripts" / "verify-contract.sh",
    "full": ROOT / "scripts" / "verify-full.sh",
}


def _mapping(value: object) -> dict[object, object]:
    assert isinstance(value, dict)
    return cast(dict[object, object], value)


def _workflow() -> dict[object, object]:
    parsed: object = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return _mapping(parsed)


def _triggers(workflow: dict[object, object]) -> dict[object, object]:
    return _mapping(workflow.get("on", workflow.get(True)))


def _jobs(workflow: dict[object, object]) -> dict[object, object]:
    return _mapping(workflow["jobs"])


def _runs(job: dict[object, object]) -> list[str]:
    steps = cast(list[object], job["steps"])
    return [str(_mapping(step)["run"]) for step in steps if "run" in _mapping(step)]


def test_검증_스크립트는_명시_계층과_책임을_보존한다() -> None:
    texts = {name: path.read_text(encoding="utf-8") for name, path in SCRIPTS.items()}
    assert all(path.is_file() for path in SCRIPTS.values())
    assert all(text.startswith("#!/usr/bin/env bash\nset -euo pipefail\n") for text in texts.values())

    fast = texts["fast"]
    for path in (
        "test_support_contract.py", "test_smoke.py", "test_question_request.py",
        "test_question_request_sqlite.py", "test_question_resolution_application.py",
        "test_auth.py", "test_security_regression.py", "test_ci_workflow.py",
    ):
        assert path in fast
    assert "uv run ruff check ." in fast
    assert "corepack pnpm test" in fast
    assert "corepack pnpm exec tsc --noEmit" in fast
    assert "corepack pnpm lint" in fast
    assert "pnpm build" not in fast

    assert "tests/test_support_contract.py" in texts["contract"]
    assert "tests/test_developer_api_boundary.py" in texts["contract"]
    assert "tests/test_installation_contracts.py" in texts["contract"]
    assert "tests/test_installation_entrypoints.py" in texts["contract"]
    assert "tests/test_central_web_runtime.py" in texts["contract"]
    assert "tests/test_central_next_artifact_contract.py" in texts["contract"]
    assert "corepack pnpm --dir frontend build" in texts["contract"]
    assert "uv run pytest -q" in texts["contract"]
    assert "uv run pytest -q" in texts["full"]
    assert "uv run pyright" in texts["full"]
    assert "uv run ruff check ." in texts["full"]
    for command in ("corepack pnpm test", "corepack pnpm exec tsc --noEmit", "corepack pnpm lint", "corepack pnpm build"):
        assert command in texts["full"]


def test_ci는_pr_fast_contract와_main_nightly_manual_full을_분리한다() -> None:
    workflow = _workflow()
    triggers = _triggers(workflow)
    assert set(triggers) == {"pull_request", "push", "schedule", "workflow_dispatch"}
    assert triggers["pull_request"] is None
    assert _mapping(triggers["push"]) == {"branches": ["main"]}
    assert triggers["workflow_dispatch"] is None
    assert cast(list[object], triggers["schedule"]) == [{"cron": "17 3 * * *"}]

    jobs = _jobs(workflow)
    assert set(jobs) == {"fast", "contract", "full"}
    assert _mapping(jobs["fast"])["if"] == (
        "github.event_name == 'pull_request' || github.event_name == 'push'"
    )
    assert _mapping(jobs["contract"])["if"] == (
        "github.event_name == 'pull_request' || github.event_name == 'push'"
    )
    full = _mapping(jobs["full"])
    assert full["if"] == "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch' || (github.event_name == 'push' && github.ref == 'refs/heads/main')"
    assert _runs(_mapping(jobs["fast"])).pop() == "scripts/verify-fast.sh"
    assert _runs(_mapping(jobs["contract"])).pop() == "scripts/verify-contract.sh"
    assert _runs(full).pop() == "scripts/verify-full.sh"


def test_ci_frontend는_고정_pnpm과_기존_검증을_쓴다() -> None:
    workflow = _workflow()
    for name in ("fast", "contract", "full"):
        job = _mapping(_jobs(workflow)[name])
        steps = [_mapping(step) for step in cast(list[object], job["steps"])]
        pnpm = next(
            step
            for step in steps
            if str(step.get("uses", "")).startswith("pnpm/action-setup@")
        )
        assert _mapping(pnpm["with"])["version"] == "11.18.0"
        assert "corepack pnpm --dir frontend install --frozen-lockfile" in _runs(job)
