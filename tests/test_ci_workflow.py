from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import yaml


WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"

CHECKOUT = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
SETUP_UV = "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b"
SETUP_PNPM = "pnpm/action-setup@0ebf47130e4866e96fce0953f49152a61190b271"
SETUP_NODE = "actions/setup-node@53b83947a5a98c8d113130e565377fae1a50d02f"


def _mapping(value: object) -> dict[object, object]:
    assert isinstance(value, dict)
    return cast(dict[object, object], value)


def _sequence(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def _load_workflow() -> tuple[str, dict[object, object]]:
    assert WORKFLOW_PATH.is_file(), f"CI workflow가 없습니다: {WORKFLOW_PATH}"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    parsed: object = yaml.safe_load(text)
    return text, _mapping(parsed)


def _triggers(workflow: dict[object, object]) -> dict[object, object]:
    # PyYAML 1.1은 따옴표 없는 `on`을 bool로 읽으므로 두 표현을 모두 받는다.
    return _mapping(workflow.get("on", workflow.get(True)))


def _jobs(workflow: dict[object, object]) -> dict[object, object]:
    return _mapping(workflow.get("jobs"))


def _steps(job: dict[object, object]) -> list[dict[object, object]]:
    return [_mapping(step) for step in _sequence(job.get("steps"))]


def _uses(steps: list[dict[object, object]]) -> list[str]:
    values: list[str] = []
    for step in steps:
        value = step.get("uses")
        if value is not None:
            assert isinstance(value, str)
            values.append(value)
    return values


def _runs(steps: list[dict[object, object]]) -> list[str]:
    values: list[str] = []
    for step in steps:
        value = step.get("run")
        if value is not None:
            assert isinstance(value, str)
            values.append(value)
    return values


def _step_using(steps: list[dict[object, object]], action: str) -> dict[object, object]:
    matches = [step for step in steps if step.get("uses") == action]
    assert len(matches) == 1
    return matches[0]


def _contains_key(value: object, forbidden: str) -> bool:
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return any(
            key == forbidden or _contains_key(child, forbidden) for key, child in mapping.items()
        )
    if isinstance(value, list):
        return any(_contains_key(child, forbidden) for child in cast(list[object], value))
    return False


def test_ci는_pr과_main_push를_최소권한으로_빠짐없이_검사한다() -> None:
    _, workflow = _load_workflow()

    triggers = _triggers(workflow)
    assert set(triggers) == {"pull_request", "push"}
    assert triggers["pull_request"] is None
    assert _mapping(triggers["push"]) == {"branches": ["main"]}
    assert not _contains_key(triggers, "paths")
    assert not _contains_key(triggers, "paths-ignore")

    assert _mapping(workflow.get("permissions")) == {"contents": "read"}
    concurrency = _mapping(workflow.get("concurrency"))
    assert concurrency == {
        "group": "ci-${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": True,
    }


def test_ci는_backend와_frontend를_병렬_제한시간_job으로_실행한다() -> None:
    _, workflow = _load_workflow()

    jobs = _jobs(workflow)
    assert set(jobs) == {"backend", "frontend"}
    for raw_job in jobs.values():
        job = _mapping(raw_job)
        assert job.get("runs-on") == "ubuntu-24.04"
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and not isinstance(timeout, bool)
        assert 0 < timeout <= 20
        assert "needs" not in job
        assert "permissions" not in job
        # required check 이름만 남기고 job/step이 조건부 skip되는 우회를 막는다.
        assert not _contains_key(job, "if")


def test_ci는_action과_toolchain을_immutable_버전에_고정한다() -> None:
    _, workflow = _load_workflow()
    jobs = _jobs(workflow)
    backend_steps = _steps(_mapping(jobs["backend"]))
    frontend_steps = _steps(_mapping(jobs["frontend"]))

    assert _uses(backend_steps) == [CHECKOUT, SETUP_UV]
    assert _uses(frontend_steps) == [CHECKOUT, SETUP_PNPM, SETUP_NODE]
    for action in _uses(backend_steps) + _uses(frontend_steps):
        assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action)

    assert _mapping(_step_using(backend_steps, SETUP_UV).get("with")) == {
        "version": "0.11.28",
        "python-version": "3.12",
        "enable-cache": True,
    }
    assert _mapping(_step_using(frontend_steps, SETUP_PNPM).get("with")) == {
        "version": "10.33.0",
        "run_install": False,
    }
    assert _mapping(_step_using(frontend_steps, SETUP_NODE).get("with")) == {
        "node-version": "24.14.1",
        "cache": "pnpm",
        "cache-dependency-path": "frontend/pnpm-lock.yaml",
    }


def test_ci는_로컬과_같은_명령만_실행하고_외부자격증명을_쓰지_않는다() -> None:
    text, workflow = _load_workflow()
    jobs = _jobs(workflow)
    backend = _mapping(jobs["backend"])
    frontend = _mapping(jobs["frontend"])

    assert _runs(_steps(backend)) == [
        "uv sync --locked --all-extras --dev",
        "uv run ruff check .",
        "uv run pyright",
        "uv run pytest -q",
    ]
    assert _runs(_steps(frontend)) == [
        "pnpm install --frozen-lockfile",
        "pnpm lint",
        "pnpm build",
    ]
    assert _mapping(_mapping(frontend.get("defaults")).get("run")) == {
        "working-directory": "frontend"
    }

    assert "secrets." not in text
    assert "pull_request_target" not in _triggers(workflow)
    assert not _contains_key(workflow, "secrets")
    assert not _contains_key(workflow, "continue-on-error")
