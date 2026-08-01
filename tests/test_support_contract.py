"""실행 지원 상태와 문서 표기를 고정하는 기계 계약."""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
import re
import tomllib
from typing import TypedDict, cast

import pytest

from agent_org_network.owner_authoring_web import (
    OwnerAuthoringProductionUnavailable,
    create_owner_authoring_app,
)
from agent_org_network.question_user_mcp import QUESTION_USER_TOOL_MANIFEST


REPOSITORY_ROOT = Path(__file__).parents[1]
CONTRACT_PATH = REPOSITORY_ROOT / "docs" / "support-contract.json"

EXPECTED_STATUS_VOCABULARY = [
    "runnable_developer_reference",
    "runnable_legacy_fixture",
    "installable_dependent_client",
    "tested_component_factory",
    "product_target_not_available",
]


class SupportRow(TypedDict):
    id: str
    surface: str
    status: str
    boundary: str
    run: str | None
    requirements: list[str]
    prohibited_claims: list[str]


class DocumentationContract(TypedDict):
    required_files: list[str]
    required_reference: str
    required_statuses_by_file: dict[str, list[str]]
    forbidden_current_command_patterns: list[str]


class SupportContract(TypedDict):
    schema_version: int
    status_vocabulary: list[str]
    support_matrix: list[SupportRow]
    documentation_contract: DocumentationContract


def _contract() -> SupportContract:
    return cast(SupportContract, json.loads(CONTRACT_PATH.read_text(encoding="utf-8")))


def _support_matrix(contract: SupportContract) -> dict[str, SupportRow]:
    return {row["id"]: row for row in contract["support_matrix"]}


def test_지원_상태_vocabulary와_현재_지원_매트릭스가_정확하다() -> None:
    contract = _contract()

    assert contract["schema_version"] == 1
    assert contract["status_vocabulary"] == EXPECTED_STATUS_VOCABULARY

    matrix = _support_matrix(contract)
    assert {row["status"] for row in matrix.values()} == set(EXPECTED_STATUS_VOCABULARY)
    assert {
        "developer_api": "runnable_developer_reference",
        "browser_frontend": "runnable_developer_reference",
        "legacy_central_demo": "runnable_legacy_fixture",
        "legacy_owner_worker_demo": "runnable_legacy_fixture",
        "legacy_mcp_demo": "runnable_legacy_fixture",
        "question_user_mcp_client": "installable_dependent_client",
        "production_central_component_factories": "tested_component_factory",
        "a2a_remote_runtime_component": "tested_component_factory",
        "central_installation_entrypoint_skeleton": "tested_component_factory",
        "owner_installation_entrypoint_skeleton": "tested_component_factory",
        "production_owner_authoring_app": "product_target_not_available",
        "three_install_target": "product_target_not_available",
    } == {identifier: row["status"] for identifier, row in matrix.items()}


def test_windows_native가_docker_없이_기본_실행_경로다() -> None:
    contract = _contract()
    contract_dict = cast(dict[str, object], contract)
    baseline = cast(dict[str, object], contract_dict["runtime_baseline"])
    assert baseline["primary_platform"] == "windows"
    assert baseline["docker_required"] is False
    assert baseline["wsl_required"] is False
    assert baseline["git_bash_required"] is False
    scripts = cast(list[str], baseline["native_scripts"])
    assert scripts
    for relative_path in scripts:
        path = REPOSITORY_ROOT / relative_path
        assert path.is_file(), relative_path
        source = path.read_text(encoding="utf-8").lower()
        assert "docker run" not in source
        assert "docker build" not in source

    for relative_path in (
        "scripts/verify-fast.ps1",
        "scripts/verify-contract.ps1",
        "scripts/verify-full.ps1",
    ):
        source = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8").lower()
        assert "docker" not in source or "optional" in source


def test_제품_target_허위_주장을_막기_위한_지원_매트릭스는_요건과_금지_주장을_명시한다() -> None:
    matrix = _support_matrix(_contract())
    expected_fields = {
        "id",
        "surface",
        "status",
        "run",
        "boundary",
        "requirements",
        "prohibited_claims",
    }
    no_entrypoint_rows = {
        "production_central_component_factories",
        "a2a_remote_runtime_component",
        "production_owner_authoring_app",
        "three_install_target",
    }

    for identifier, row in matrix.items():
        assert set(row) == expected_fields, identifier
        assert isinstance(row["id"], str) and row["id"]
        assert isinstance(row["surface"], str) and row["surface"]
        assert isinstance(row["status"], str) and row["status"]
        assert isinstance(row["boundary"], str) and row["boundary"]
        assert isinstance(row["requirements"], list) and row["requirements"]
        assert all(isinstance(requirement, str) and requirement for requirement in row["requirements"])
        assert isinstance(row["prohibited_claims"], list) and row["prohibited_claims"]
        assert all(
            isinstance(prohibited_claim, str) and prohibited_claim
            for prohibited_claim in row["prohibited_claims"]
        )
        if identifier in no_entrypoint_rows:
            assert row["run"] is None
        else:
            assert isinstance(row["run"], str) and row["run"]

    assert matrix["browser_frontend"]["run"] == (
        "cd frontend && corepack pnpm build && AON_FRONTEND_MODE=development "
        "AON_BACKEND_URL=http://127.0.0.1:8011 corepack pnpm start"
    )
    for row in matrix.values():
        if row["status"] == "product_target_not_available":
            assert row["run"] is None

    target_claims = matrix["three_install_target"]["prohibited_claims"]
    assert any("aon-central" in claim and "aon-owner" in claim for claim in target_claims)


def test_legacy_중앙과_owner_worker_수동_시연의_실행_인자_shape가_정확하다() -> None:
    matrix = _support_matrix(_contract())
    assert matrix["legacy_central_demo"]["run"] == "scripts/run_central.sh 8000 127.0.0.1"
    assert matrix["legacy_owner_worker_demo"]["run"] == (
        "scripts/run_worker.sh cs_lead primary 8000 127.0.0.1"
    )
    assert matrix["legacy_owner_worker_demo"]["requirements"] == [
        "동일 저장소 checkout과 uv 개발 환경",
        "OWNER_ID, primary 또는 backup role, Central endpoint",
        "기본 Claude Runtime이면 로컬 claude 로그인 또는 선택 provider credential과 extra"
    ]

    central_script = (REPOSITORY_ROOT / "scripts" / "run_central.sh").read_text(encoding="utf-8")
    assert "scripts/run_central.sh [PORT] [HOST]" in central_script
    assert 'PORT="${1:-8000}"' in central_script
    assert 'HOST="${2:-127.0.0.1}"' in central_script

    worker_script = (REPOSITORY_ROOT / "scripts" / "run_worker.sh").read_text(encoding="utf-8")
    assert "scripts/run_worker.sh <OWNER_ID> [ROLE] [PORT] [CENTRAL_HOST]" in worker_script
    assert 'OWNER="${1:?owner를 지정하세요: scripts/run_worker.sh <OWNER_ID> [ROLE] [PORT] [CENTRAL_HOST]}"' in worker_script
    assert 'ROLE="${2:-primary}"' in worker_script
    assert 'PORT="${3:-8000}"' in worker_script
    assert 'CENTRAL_HOST="${4:-127.0.0.1}"' in worker_script


def test_패키지_실행_명령은_세_설치_경계를_정확히_가리킨다() -> None:
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"] == {
        "aon-mcp": "agent_org_network.question_user_mcp:main",
        "aon-central": "agent_org_network.central_cli:main",
        "aon-owner": "agent_org_network.owner_cli:main",
    }
    assert project["project"]["optional-dependencies"]["owner-keychain"] == [
        "keyring>=25.6,<26"
    ]
    assert project["project"]["optional-dependencies"]["a2a"] == ["a2a-sdk==1.1.1"]


def test_현재_데모와_의존형_mcp_진입점이_실재한다() -> None:
    assert hasattr(importlib.import_module("agent_org_network.web"), "app")
    assert hasattr(importlib.import_module("agent_org_network.server"), "central_app")
    assert hasattr(importlib.import_module("agent_org_network.mcp_server"), "main")
    assert hasattr(importlib.import_module("agent_org_network.question_user_mcp"), "main")

    expected_scripts = {
        "scripts/run_central.sh": "agent_org_network.server:central_app",
        "scripts/run_worker.sh": "agent_org_network.worker",
        "scripts/run_mcp.sh": "agent_org_network.mcp_server",
    }
    for relative_path, entrypoint in expected_scripts.items():
        script = REPOSITORY_ROOT / relative_path
        assert script.is_file()
        assert entrypoint in script.read_text(encoding="utf-8")


def test_aon_mcp는_정확히_두_질문_도구와_두_하위_명령만_제공한다() -> None:
    assert QUESTION_USER_TOOL_MANIFEST == frozenset({"ask_org", "get_question"})

    source = (REPOSITORY_ROOT / "src" / "agent_org_network" / "question_user_mcp.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    subcommands = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }

    assert subcommands == {"pair", "serve-stdio"}


def test_owner_제품_서버는_현재_의도적으로_사용_불가하다() -> None:
    with pytest.raises(OwnerAuthoringProductionUnavailable):
        create_owner_authoring_app(object())  # type: ignore[arg-type]


def test_developer_api는_html_runtime과_central_owner_api를_소유하지_않는다() -> None:
    """ADR 0073: browser는 Next, FastAPI는 JSON/SSE/WebSocket만 소유한다."""
    web = (REPOSITORY_ROOT / "src" / "agent_org_network" / "web.py").read_text(
        encoding="utf-8"
    )
    assert "FileResponse" not in web
    assert "_WEB_DIR" not in web
    assert "owner-api" not in web
    for retired in (
        'app.get("/")', 'app.get("/inbox")', 'app.get("/builder")',
        'app.get("/monitor/view")', 'app.get("/org/view")',
        'app.get("/console/view")', 'app.get("/supervision")', 'app.get("/admin")',
    ):
        assert retired not in web


def test_browser_runtime_standalone_docker와_owner_경계_계약이_명시된다() -> None:
    next_config = (REPOSITORY_ROOT / "frontend" / "next.config.mjs").read_text(
        encoding="utf-8"
    )
    assert "output: \"standalone\"" in next_config
    assert not (REPOSITORY_ROOT / "frontend" / "app" / "owner-api").exists()
    onboarding = (REPOSITORY_ROOT / "frontend" / "lib" / "onboarding-api.ts").read_text(
        encoding="utf-8"
    )
    assert '"/owner-api/' not in onboarding
    dockerfile = REPOSITORY_ROOT / "frontend" / "Dockerfile"
    dockerignore = REPOSITORY_ROOT / "frontend" / ".dockerignore"
    assert dockerfile.is_file() and dockerignore.is_file()
    docker = dockerfile.read_text(encoding="utf-8")
    assert ".next/standalone" in docker
    assert "AON_FRONTEND_MODE" in docker


def test_docker_모든_stage는_CI와_같은_node24_pnpm을_고정한다() -> None:
    docker = (REPOSITORY_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    stages = re.findall(r"^FROM\s+node:([^\s]+)", docker, flags=re.MULTILINE)
    assert stages == ["24-alpine", "24-alpine", "24-alpine"]
    assert docker.count("pnpm@11.18.0") == 2

    package = json.loads((REPOSITORY_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert package["packageManager"] == "pnpm@11.18.0"

    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert workflow.count('node-version: "24.14.1"') == 3
    assert workflow.count('version: "11.18.0"') == 3


def test_standalone_시작은_instrumentation이_아닌_preflight_wrapper를_항상_거친다() -> None:
    package = json.loads((REPOSITORY_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    docker = (REPOSITORY_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    wrapper = REPOSITORY_ROOT / "frontend" / "scripts" / "start-standalone.mjs"

    assert package["scripts"]["start"] == "node scripts/start-standalone.mjs"
    assert 'CMD ["node", "scripts/start-standalone.mjs"]' in docker
    assert "COPY --from=builder --chown=nextjs:nextjs /app/scripts/start-standalone.mjs" in docker
    assert wrapper.is_file()

    source = wrapper.read_text(encoding="utf-8")
    assert "readFrontendRuntimeConfig" in source
    assert 'process.exitCode = 1' in source
    assert 'new URL("../.next/standalone/server.js", import.meta.url)' in source
    assert "await import(server.href)" in source
    assert "instrumentation" not in package["scripts"]["start"]
    assert "instrumentation" not in docker.split("CMD", maxsplit=1)[1]


def test_bff와_runtime_계약은_allowlist와_fail_closed_입력을_명시한다() -> None:
    bff = (REPOSITORY_ROOT / "frontend" / "lib" / "bff-policy.ts").read_text(
        encoding="utf-8"
    )
    runtime = (REPOSITORY_ROOT / "frontend" / "lib" / "frontend-runtime.ts").read_text(
        encoding="utf-8"
    )
    assert "isAllowedBffRequest" in bff
    assert "MAX_BFF_BODY_BYTES" in bff
    assert "bffRequestHeaders" in bff
    assert "readBffBody" in bff
    assert "readFrontendRuntimeConfig" in runtime
    assert "AON_FRONTEND_MODE" in runtime
    assert "AON_BACKEND_URL" in runtime
    assert "AON_PUBLIC_ORIGIN" in runtime
    assert "production" in runtime and "https:" in runtime


def test_ssot와_frontend_문서가_지원_계약과_상태_경계를_명시한다() -> None:
    contract = _contract()
    documentation = contract["documentation_contract"]
    required_files = documentation["required_files"]
    required_reference = documentation["required_reference"]
    statuses_by_file = documentation["required_statuses_by_file"]

    for relative_path in required_files:
        text = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
        assert required_reference in text, relative_path
        expected_statuses = statuses_by_file[relative_path]
        for status in expected_statuses:
            assert status in text, f"{relative_path}: {status}"


def test_문서_코드_블록은_지원_계약이_금지한_현재_명령을_제시하지_않는다() -> None:
    documentation = _contract()["documentation_contract"]
    forbidden_patterns = documentation["forbidden_current_command_patterns"]
    assert forbidden_patterns == []
    target_claims = _support_matrix(_contract())["three_install_target"]["prohibited_claims"]
    assert any("aon-central" in claim and "aon-owner" in claim for claim in target_claims)

    for relative_path in documentation["required_files"]:
        document = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
        for block in _fenced_code_blocks(document):
            for pattern in forbidden_patterns:
                assert re.search(pattern, block, flags=re.MULTILINE) is None, relative_path


def _fenced_code_blocks(document: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] | None = None
    for line in document.splitlines():
        if line.startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append("\n".join(current))
                current = None
        elif current is not None:
            current.append(line)
    assert current is None, "unclosed fenced code block"
    return blocks
