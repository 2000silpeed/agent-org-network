"""P17.7 production bootstrap의 fail-closed 공개 계약 RED."""

from __future__ import annotations

import ast
import subprocess
import sys
from copy import copy
from pathlib import Path
from typing import Protocol, cast

import pytest
from pydantic import ValidationError

from agent_org_network.approval_operations import ApprovalOperationsApplication
from agent_org_network.production_bootstrap import (
    InvalidProductionConfiguration,
    MissingProductionConfiguration,
    ProductionBootstrapConfig,
    ProductionBootstrapFailure,
    ProductionCompositionRejected,
    ProductionDependencies,
    ProductionDependencyUnavailable,
    bootstrap_production,
    check_production_readiness_from_env,
    load_production_config,
)
from agent_org_network.question_stream_execution import (
    QuestionStreamApplication,
)
from agent_org_network.question_surface_composition import (
    AtomicQuestionCompletionStorage,
    QuestionSurfaceComposition,
)


ENV_KEYS = (
    "AON_PRODUCTION_ORG_ID",
    "AON_PRODUCTION_DATABASE_DSN",
    "AON_PRODUCTION_OIDC_ISSUER",
    "AON_PRODUCTION_OIDC_CLIENT_ID",
    "AON_PRODUCTION_OIDC_CLIENT_SECRET",
    "AON_PRODUCTION_SESSION_SECRET",
    "AON_PRODUCTION_AUTHORITY_POLICY_REF",
    "AON_PRODUCTION_PROVIDER",
    "AON_PRODUCTION_PROVIDER_CREDENTIAL",
)


class _CleanupAwareFailure(Protocol):
    @property
    def cleanup_pending(self) -> bool: ...

    def close(self) -> None: ...


def _valid_environ() -> dict[str, str]:
    return {
        "AON_PRODUCTION_ORG_ID": "org-production",
        "AON_PRODUCTION_DATABASE_DSN": "postgresql://aon@db.example.test/aon",
        "AON_PRODUCTION_OIDC_ISSUER": "https://identity.example.test/",
        "AON_PRODUCTION_OIDC_CLIENT_ID": "aon-production",
        "AON_PRODUCTION_OIDC_CLIENT_SECRET": "oidc-secret-do-not-reflect",
        "AON_PRODUCTION_SESSION_SECRET": "session-secret-do-not-reflect-32-bytes",
        "AON_PRODUCTION_AUTHORITY_POLICY_REF": "authority-policy-v1",
        "AON_PRODUCTION_PROVIDER": "openai",
        "AON_PRODUCTION_PROVIDER_CREDENTIAL": "provider-secret-do-not-reflect",
    }


def _valid_config_payload() -> dict[str, object]:
    return {
        "org_id": "org-production",
        "database_dsn": "postgresql://aon@db.example.test/aon",
        "oidc_issuer": "https://identity.example.test/",
        "oidc_client_id": "aon-production",
        "oidc_client_secret": "oidc-secret-do-not-reflect",
        "session_secret": "session-secret-do-not-reflect-32-bytes",
        "authority_policy_ref": "authority-policy-v1",
        "provider": "openai",
        "provider_credential": "provider-secret-do-not-reflect",
    }


def _failure_text(failure: object) -> str:
    dumped = getattr(failure, "model_dump", None)
    payload = dumped(mode="json") if callable(dumped) else vars(failure)
    return f"{failure!s} {failure!r} {payload!r}"


class _NeverDependencyFactory:
    def __init__(self) -> None:
        self.calls = 0

    def open(self, config: ProductionBootstrapConfig) -> ProductionDependencyUnavailable:
        del config
        self.calls += 1
        return ProductionDependencyUnavailable(
            kind="production_adapters_unavailable",
            code="production_adapters_unavailable",
        )


@pytest.mark.parametrize("missing_key", ENV_KEYS)
def test_missing_required_env_fails_before_dependency_factory(missing_key: str) -> None:
    environ = _valid_environ()
    del environ[missing_key]
    factory = _NeverDependencyFactory()

    result = bootstrap_production(environ=environ, dependency_factory=factory)

    assert type(result) is MissingProductionConfiguration
    assert factory.calls == 0
    assert missing_key in result.missing_keys
    assert all(secret not in _failure_text(result) for secret in _secret_values())


@pytest.mark.parametrize("invalid_key", ENV_KEYS)
@pytest.mark.parametrize("invalid_value", ["", "   "])
def test_blank_env_is_secret_safe_and_factory_is_never_called(
    invalid_key: str,
    invalid_value: str,
) -> None:
    environ = _valid_environ()
    environ[invalid_key] = invalid_value
    factory = _NeverDependencyFactory()

    result = bootstrap_production(environ=environ, dependency_factory=factory)

    assert type(result) is InvalidProductionConfiguration
    assert factory.calls == 0
    assert all(secret not in _failure_text(result) for secret in _secret_values())


@pytest.mark.parametrize(
    ("key", "invalid_value"),
    [
        ("AON_PRODUCTION_DATABASE_DSN", "sqlite:///:memory:"),
        ("AON_PRODUCTION_OIDC_ISSUER", "not-an-https-url"),
    ],
)
def test_invalid_endpoint_configuration_fails_closed_before_factory(
    key: str,
    invalid_value: str,
) -> None:
    environ = _valid_environ()
    environ[key] = invalid_value
    factory = _NeverDependencyFactory()

    result = bootstrap_production(environ=environ, dependency_factory=factory)

    assert type(result) is InvalidProductionConfiguration
    assert factory.calls == 0
    assert invalid_value not in _failure_text(result)


@pytest.mark.parametrize(
    ("field_name", "invalid_value", "raw_secret"),
    [
        (
            "database_dsn",
            "sqlite://dsn-raw-secret@local/database",
            "dsn-raw-secret",
        ),
        ("oidc_client_secret", ["oidc-raw-secret"], "oidc-raw-secret"),
        ("session_secret", {"value": "session-raw-secret"}, "session-raw-secret"),
        (
            "provider_credential",
            ["provider-raw-secret"],
            "provider-raw-secret",
        ),
    ],
)
def test_direct_config_validation_never_reflects_invalid_secret_inputs(
    field_name: str,
    invalid_value: object,
    raw_secret: str,
) -> None:
    payload = _valid_config_payload()
    payload[field_name] = invalid_value

    with pytest.raises(ValidationError) as captured:
        ProductionBootstrapConfig.model_validate(payload, strict=True)

    assert raw_secret not in f"{captured.value!s} {captured.value!r}"


@pytest.mark.parametrize(
    "invalid_issuer",
    [
        "https://issuer-user:issuer-password@identity.example.test/",
        "https://identity.example.test/?issuer-query-secret=value",
        "https://identity.example.test/#issuer-fragment-secret",
    ],
)
def test_oidc_issuer_rejects_userinfo_query_and_fragment_without_reflection(
    invalid_issuer: str,
) -> None:
    payload = _valid_config_payload()
    payload["oidc_issuer"] = invalid_issuer

    with pytest.raises(ValidationError) as captured:
        ProductionBootstrapConfig.model_validate(payload, strict=True)
    assert invalid_issuer not in f"{captured.value!s} {captured.value!r}"

    environ = _valid_environ()
    environ["AON_PRODUCTION_OIDC_ISSUER"] = invalid_issuer
    result = load_production_config(environ)
    assert type(result) is InvalidProductionConfiguration
    assert invalid_issuer not in _failure_text(result)


def _secret_values() -> tuple[str, ...]:
    environ = _valid_environ()
    return (
        environ["AON_PRODUCTION_DATABASE_DSN"],
        environ["AON_PRODUCTION_OIDC_CLIENT_SECRET"],
        environ["AON_PRODUCTION_SESSION_SECRET"],
        environ["AON_PRODUCTION_PROVIDER_CREDENTIAL"],
    )


def test_config_is_frozen_extra_forbid_and_uses_only_exact_nine_env_keys() -> None:
    result = load_production_config(_valid_environ())
    assert type(result) is ProductionBootstrapConfig
    config = result
    assert set(ProductionBootstrapConfig.model_fields) == {
        "org_id",
        "database_dsn",
        "oidc_issuer",
        "oidc_client_id",
        "oidc_client_secret",
        "session_secret",
        "authority_policy_ref",
        "provider",
        "provider_credential",
    }
    with pytest.raises(ValidationError):
        ProductionBootstrapConfig.model_validate(
            {**config.model_dump(mode="python"), "unexpected": "forbidden"},
            strict=True,
        )
    with pytest.raises(ValidationError):
        setattr(config, "org_id", "mutated")


def test_config_repr_and_serialization_mask_all_secret_values() -> None:
    result = load_production_config(_valid_environ())
    assert type(result) is ProductionBootstrapConfig

    rendered = f"{result!s} {result!r} {result.model_dump(mode='json')!r}"

    assert all(secret not in rendered for secret in _secret_values())
    assert result.database_dsn.get_secret_value() == _valid_environ()["AON_PRODUCTION_DATABASE_DSN"]


class _UnavailableFactory:
    def __init__(self, failure: ProductionDependencyUnavailable) -> None:
        self.failure = failure
        self.calls = 0

    def open(
        self,
        config: ProductionBootstrapConfig,
    ) -> ProductionDependencyUnavailable:
        assert config.org_id == "org-production"
        self.calls += 1
        return self.failure


def test_dependency_factory_typed_unavailable_result_is_preserved() -> None:
    failure = ProductionDependencyUnavailable(
        kind="production_adapters_unavailable",
        code="production_adapters_unavailable",
    )
    factory = _UnavailableFactory(failure)

    result = bootstrap_production(environ=_valid_environ(), dependency_factory=factory)

    assert result == failure
    assert factory.calls == 1


class _ObservedApplication:
    def __init__(self, events: list[str], *, fail_once: bool = False) -> None:
        self._events = events
        self._fail_once = fail_once

    def shutdown(self, *, wait: bool = True) -> None:
        assert wait is True
        self._events.append("scheduler")
        if self._fail_once:
            self._fail_once = False
            raise RuntimeError("composition-secret-do-not-reflect")


class _ObservedStorage:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def close(self) -> None:
        self._events.append("storage")


def _exact_composition(
    events: list[str],
    *,
    fail_scheduler_once: bool = False,
) -> QuestionSurfaceComposition:
    return QuestionSurfaceComposition(
        application=cast(
            QuestionStreamApplication,
            _ObservedApplication(events, fail_once=fail_scheduler_once),
        ),
        storage=cast(AtomicQuestionCompletionStorage, _ObservedStorage(events)),
        approval_operations=cast(ApprovalOperationsApplication, object()),
    )


def test_direct_composition_copy_cannot_close_original_lifecycle_resources() -> None:
    events: list[str] = []
    original = _exact_composition(events)
    duplicate = copy(original)
    duplicate_error: Exception | None = None

    try:
        duplicate.close()
    except Exception as error:
        duplicate_error = error
    original.close()

    assert duplicate_error is not None
    assert type(duplicate_error).__name__ == "QuestionSurfaceLifecycleOwnershipError"
    assert str(duplicate_error) == "question surface lifecycle owner mismatch"
    assert events == ["scheduler", "storage"]


class _ContractDependenciesFactory:
    def __init__(
        self,
        dependencies: ProductionDependencies,
    ) -> None:
        self.dependencies = dependencies
        self.open_calls = 0

    def open(self, config: ProductionBootstrapConfig) -> ProductionDependencies:
        assert config.org_id == "org-production"
        self.open_calls += 1
        return self.dependencies


class _CloseableForeignComposition:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def close(self) -> None:
        self._events.append("composition")


class _RaisingCloseDescriptorComposition:
    @property
    def close(self) -> object:
        raise RuntimeError("close-descriptor-secret-do-not-reflect")


@pytest.mark.parametrize("foreign_kind", ["dict", "arbitrary_object"])
def test_foreign_composition_result_is_rejected_and_closeable_value_is_cleaned_first(
    foreign_kind: str,
) -> None:
    events: list[str] = []
    foreign: object = {} if foreign_kind == "dict" else _CloseableForeignComposition(events)

    def composition_factory(*, production_style: bool) -> QuestionSurfaceComposition:
        assert production_style is True
        return cast(QuestionSurfaceComposition, foreign)

    dependencies = ProductionDependencies(
        composition_factory=composition_factory,
        close=lambda: events.append("external_dependency"),
    )
    result = bootstrap_production(
        environ=_valid_environ(),
        dependency_factory=_ContractDependenciesFactory(dependencies),
    )

    assert type(result) is ProductionCompositionRejected
    assert events == ["external_dependency"]


def test_raising_foreign_close_descriptor_is_rejected_without_raw_exception() -> None:
    events: list[str] = []
    foreign = _RaisingCloseDescriptorComposition()

    def composition_factory(*, production_style: bool) -> QuestionSurfaceComposition:
        assert production_style is True
        return cast(QuestionSurfaceComposition, foreign)

    dependencies = ProductionDependencies(
        composition_factory=composition_factory,
        close=lambda: events.append("external_dependency"),
    )

    result = bootstrap_production(
        environ=_valid_environ(),
        dependency_factory=_ContractDependenciesFactory(dependencies),
    )

    assert type(result) is ProductionCompositionRejected
    assert result.cleanup_pending is False
    assert events == ["external_dependency"]
    assert "close-descriptor-secret-do-not-reflect" not in _failure_text(result)


def test_direct_exact_composition_without_production_attestation_is_rejected() -> None:
    events: list[str] = []
    composition = _exact_composition(events)

    def composition_factory(*, production_style: bool) -> QuestionSurfaceComposition:
        assert production_style is True
        return composition

    dependencies = ProductionDependencies(
        composition_factory=composition_factory,
        close=lambda: events.append("external_dependency"),
    )

    result = bootstrap_production(
        environ=_valid_environ(),
        dependency_factory=_ContractDependenciesFactory(dependencies),
    )

    assert type(result) is ProductionCompositionRejected
    assert events == ["external_dependency"]


class _QuestionSurfaceCompositionSubclass(QuestionSurfaceComposition):
    pass


def test_composition_subclass_is_rejected_and_cleaned_before_external_dependencies() -> None:
    events: list[str] = []
    base = _exact_composition(events)
    subclass = _QuestionSurfaceCompositionSubclass(
        application=base.application,
        storage=base.storage,
        approval_operations=base.approval_operations,
    )

    def composition_factory(*, production_style: bool) -> QuestionSurfaceComposition:
        assert production_style is True
        return subclass

    dependencies = ProductionDependencies(
        composition_factory=composition_factory,
        close=lambda: events.append("external_dependency"),
    )
    result = bootstrap_production(
        environ=_valid_environ(),
        dependency_factory=_ContractDependenciesFactory(dependencies),
    )

    assert type(result) is ProductionCompositionRejected
    assert events == ["external_dependency"]


def test_composition_rejection_closes_external_dependencies_and_hides_exception() -> None:
    events: list[str] = []

    def composition_factory(*, production_style: bool) -> QuestionSurfaceComposition:
        assert production_style is True
        raise RuntimeError("provider-secret-do-not-reflect")

    dependencies = ProductionDependencies(
        composition_factory=composition_factory,
        close=lambda: events.append("external_dependency"),
    )

    result = bootstrap_production(
        environ=_valid_environ(),
        dependency_factory=_ContractDependenciesFactory(dependencies),
    )

    assert type(result) is ProductionCompositionRejected
    assert events == ["external_dependency"]
    assert "provider-secret-do-not-reflect" not in _failure_text(result)


def test_composition_rejection_preserves_failed_external_cleanup_for_safe_retry() -> None:
    events: list[str] = []
    cleanup_attempts = 0

    def composition_factory(*, production_style: bool) -> QuestionSurfaceComposition:
        assert production_style is True
        raise RuntimeError("composition-provider-secret-do-not-reflect")

    def close_external() -> None:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        events.append("external_dependency")
        if cleanup_attempts < 3:
            raise RuntimeError("external-cleanup-secret-do-not-reflect")

    dependencies = ProductionDependencies(
        composition_factory=composition_factory,
        close=close_external,
    )
    result = bootstrap_production(
        environ=_valid_environ(),
        dependency_factory=_ContractDependenciesFactory(dependencies),
    )

    assert type(result) is ProductionCompositionRejected
    cleanup_failure = cast(_CleanupAwareFailure, result)
    assert cleanup_failure.cleanup_pending is True
    assert "composition-provider-secret-do-not-reflect" not in _failure_text(result)
    assert "external-cleanup-secret-do-not-reflect" not in _failure_text(result)
    assert "ProductionDependencies" not in _failure_text(result)

    copied_result = result.model_copy(deep=True)
    copied_cleanup = cast(_CleanupAwareFailure, copied_result)
    assert copied_cleanup.cleanup_pending is True

    with pytest.raises(
        RuntimeError,
        match=r"^production bootstrap cleanup failed$",
    ) as captured_cleanup:
        copied_cleanup.close()
    assert captured_cleanup.value.__cause__ is None
    assert captured_cleanup.value.__context__ is None
    assert cleanup_failure.cleanup_pending is True
    assert copied_cleanup.cleanup_pending is True
    cleanup_failure.close()
    assert copied_cleanup.cleanup_pending is False
    copied_cleanup.close()
    cleanup_failure.close()

    assert cleanup_failure.cleanup_pending is False
    assert events == [
        "external_dependency",
        "external_dependency",
        "external_dependency",
    ]


def test_all_bootstrap_failure_arms_expose_common_noop_cleanup_contract() -> None:
    failures: tuple[ProductionBootstrapFailure, ...] = (
        MissingProductionConfiguration(missing_keys=("AON_PRODUCTION_ORG_ID",)),
        InvalidProductionConfiguration(),
        ProductionDependencyUnavailable(),
        ProductionCompositionRejected(),
    )

    for failure in failures:
        cleanup_failure = cast(_CleanupAwareFailure, failure)
        assert cleanup_failure.cleanup_pending is False
        cleanup_failure.close()
        cleanup_failure.close()
        assert cleanup_failure.cleanup_pending is False


def test_actual_env_readiness_is_always_fail_closed_until_real_adapters_exist() -> None:
    result = check_production_readiness_from_env(_valid_environ())

    assert type(result) is ProductionDependencyUnavailable
    assert result.kind == "production_adapters_unavailable"
    assert result.code == "production_adapters_unavailable"


def test_actual_readiness_defaults_to_live_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _valid_environ().items():
        monkeypatch.setenv(key, value)

    result = check_production_readiness_from_env()

    assert type(result) is ProductionDependencyUnavailable
    assert result.code == "production_adapters_unavailable"


FORBIDDEN_MODULES = {
    "fastapi",
    "agent_org_network.web",
    "agent_org_network.server",
    "agent_org_network.demo",
    "agent_org_network.demo_question_surfaces",
    "agent_org_network.runtime_select",
}


def test_production_bootstrap_has_no_demo_or_runtime_selector_imports() -> None:
    source_path = (
        Path(__file__).parents[1] / "src" / "agent_org_network" / "production_bootstrap.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    assert imported.isdisjoint(FORBIDDEN_MODULES)


def test_fresh_production_bootstrap_import_has_no_forbidden_indirect_imports() -> None:
    forbidden = sorted(FORBIDDEN_MODULES)
    script = f"""
import builtins

forbidden = {forbidden!r}
original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if any(name == item or name.startswith(item + '.') for item in forbidden):
        raise AssertionError('forbidden production import: ' + name)
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
import agent_org_network.production_bootstrap
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_bootstrap_failure_union_is_closed_to_public_failure_types() -> None:
    assert ProductionBootstrapFailure == (
        MissingProductionConfiguration
        | InvalidProductionConfiguration
        | ProductionDependencyUnavailable
        | ProductionCompositionRejected
    )
