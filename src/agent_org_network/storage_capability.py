"""Question Request workflow Store의 원자 capability·내구성·동일성 조립 gate.

``workflow_durability``는 각 adapter가 선언한 재시작 내구성만 비교한다. terminal
completion 조립은 별도의 ``atomic_v1`` marker와 세 포트 callable shape도 요구한다.
두 marker 모두 foreign key, lease, outbox 소비 또는 exactly-once를 단독으로 증명하지
않는다. terminal transaction은 P17.3c 전용 Unit of Work가, 나머지 durable workflow는
P17.9가 맡는다.
"""

from __future__ import annotations

from typing import Final, Literal, TypeAlias

WorkflowDurability: TypeAlias = Literal["ephemeral", "durable"]
QuestionCompletionStorageCapability: TypeAlias = Literal["atomic_v1"]
_QUESTION_COMPLETION_REQUIRED_CALLABLES: Final = (
    "create",
    "get",
    "compare_and_set",
    "nonterminal",
    "complete",
    "by_request",
    "by_record",
)


class WorkflowCompositionError(ValueError):
    """Question Resolution Store 조립의 내구성 계약 위반."""


class UnknownWorkflowDurabilityError(WorkflowCompositionError):
    """Store가 검증 가능한 durability marker를 선언하지 않음."""


class MixedWorkflowDurabilityError(WorkflowCompositionError):
    """한 workflow 안에 ephemeral과 durable Store가 섞임."""


class NonDurableWorkflowCompositionError(WorkflowCompositionError):
    """production-style 조립이 전부 durable하지 않음."""


class QuestionCompletionStorageIdentityError(WorkflowCompositionError):
    """Request Store·Completion UoW·Reader가 동일 객체가 아님."""


class QuestionCompletionStorageCapabilityError(WorkflowCompositionError):
    """동일 Store가 검증된 atomic completion capability를 제공하지 않음."""


def workflow_durability_of(component: object) -> WorkflowDurability:
    """Store의 strict ``workflow_durability`` marker를 읽는다."""
    value = getattr(component, "workflow_durability", None)
    if value == "ephemeral":
        return "ephemeral"
    if value == "durable":
        return "durable"
    raise UnknownWorkflowDurabilityError(
        f"{type(component).__name__}가 유효한 workflow_durability를 선언하지 않았습니다."
    )


def question_completion_storage_capability_of(
    component: object,
) -> QuestionCompletionStorageCapability:
    """strict marker와 세 포트의 일곱 callable shape를 호출 없이 검증한다."""
    marker = getattr(component, "question_completion_storage_capability", None)
    if marker != "atomic_v1":
        raise QuestionCompletionStorageCapabilityError(
            f"{type(component).__name__}가 유효한 atomic Question Completion "
            "storage capability를 선언하지 않았습니다."
        )
    missing = tuple(
        name
        for name in _QUESTION_COMPLETION_REQUIRED_CALLABLES
        if not callable(getattr(component, name, None))
    )
    if missing:
        raise QuestionCompletionStorageCapabilityError(
            "atomic_v1 Question Completion storage의 필수 callable이 없습니다: "
            + ", ".join(missing)
        )
    return "atomic_v1"


def validate_question_completion_storage(
    *,
    requests: object,
    completion_uow: object,
    completion_reader: object,
    require_durable: bool,
) -> WorkflowDurability:
    """Question Completion 세 포트가 한 객체인지 확인하고 내구성을 검증한다.

    동일 DB 경로나 동등 비교는 transaction 경계를 증명하지 못한다. 반드시 객체
    identity가 같아야 하며, identity가 다르면 어떤 capability나 Store 메서드도 읽기
    전에 거부한다. 이 함수는 terminal completion 세 포트만 검증한다. Resolution,
    Approval, broker까지 같은 인스턴스를 전달하는 전체 조립은 P17.2c-2의 composition
    root가 맡는다.
    """
    if not (requests is completion_uow and completion_uow is completion_reader):
        raise QuestionCompletionStorageIdentityError(
            "production-style Question Completion 조립에는 Question Request Store·"
            "Completion UoW·Completion Reader의 동일 객체 인스턴스가 필요합니다."
        )
    question_completion_storage_capability_of(requests)
    durability = workflow_durability_of(requests)
    if require_durable and durability != "durable":
        raise NonDurableWorkflowCompositionError(
            "production-style Question Completion 조립에는 durable Store가 필요합니다."
        )
    return durability


def validate_workflow_composition(
    *,
    requests: object,
    conflicts: object,
    managers: object,
    require_durable: bool,
) -> WorkflowDurability:
    """세 Store의 선언을 비교하고 production-style이면 durable만 허용한다.

    반환값은 조립의 공통 재시작 내구성이다. ``durable`` 반환도 세 Store가 한
    transaction에 있다는 뜻은 아니다.
    """
    capabilities = (
        workflow_durability_of(requests),
        workflow_durability_of(conflicts),
        workflow_durability_of(managers),
    )
    distinct = set(capabilities)
    if len(distinct) != 1:
        raise MixedWorkflowDurabilityError(
            "Question Request와 linked writer의 workflow_durability가 섞여 있습니다: "
            f"requests={capabilities[0]}, conflicts={capabilities[1]}, "
            f"managers={capabilities[2]}"
        )
    capability = capabilities[0]
    if require_durable and capability != "durable":
        raise NonDurableWorkflowCompositionError(
            "production-style Question Resolution 조립에는 durable Store가 필요합니다."
        )
    return capability
