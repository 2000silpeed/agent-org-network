from dataclasses import dataclass

from agent_org_network.agent_card import AgentCard


@dataclass(frozen=True)
class Routed:
    """담당(primary)이 정해진 결정. Collaborator·Approval을 동반할 수 있다(T2.5).

    Route(primary)·Collaboration(collaborators)·Approval(requires_approval)은 한 Routed
    안에서 동시에 성립 가능한 *독립 축*이다(CONTEXT Routing outcomes). 셋이 다 비어도
    Routed는 성립한다 — 그래서 둘 다 기본값(빈 튜플·False)이라 기존 생성처·match가 무영향.

    - `requires_approval`(Approval 게이트): 라우팅은 됐고 *실행 전 사람 사인*이 필요한
      상태. True면 답이 `mode="draft_only"`로 투영돼 "초안·승인 대기"로 표시된다(ADR 0012
      mode 강제 패턴 — 워커 자기보고가 아니라 *라우팅 결정*이 mode를 강제, 강제 자리는
      ask_org). 게이트일 뿐 담당은 정해졌다(Escalation과 구분 — CONTEXT Approval).
    - `collaborators`(Collaboration): primary는 그대로 두고 *추가로 끌어들인* 협업 카드들.
      primary 자신은 포함하지 않는다(중복 금지). T2.5는 *부착*까지만 — collaborator를 실제로
      호출해 다중 답을 합치는 건 후속(자리만). 노출 불변식상 사용자向 Answered엔 *싣지
      않는다*(담당·승인·출처만 — collaborator는 조직 내부 협업 구조). audit엔 원형으로 남는다.
    """

    primary: AgentCard
    confidence: float = 1.0
    reason: str = ""
    requires_approval: bool = False
    collaborators: tuple[AgentCard, ...] = ()


@dataclass(frozen=True)
class Contested:
    candidates: tuple[AgentCard, ...]
    reason: str = ""


@dataclass(frozen=True)
class Unowned:
    escalated_to: str
    reason: str = ""


RoutingDecision = Routed | Contested | Unowned
