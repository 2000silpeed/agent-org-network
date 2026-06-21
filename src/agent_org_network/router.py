from agent_org_network.agent_card import AgentCard
from agent_org_network.classifier import Classifier
from agent_org_network.conflict import PrecedentStore
from agent_org_network.decision import Contested, RoutingDecision, Routed, Unowned
from agent_org_network.registry import Registry


class Router:
    def __init__(
        self,
        registry: Registry,
        classifier: Classifier,
        root_user: str,
        precedents: PrecedentStore | None = None,
    ) -> None:
        self._registry = registry
        self._classifier = classifier
        self._root_user = root_user
        self._precedents = precedents

    def route(self, question: str) -> RoutingDecision:
        intent = self._classifier.classify(question)
        if self._precedents is not None and intent:
            p = self._precedents.lookup(intent)
            if p is not None:
                try:
                    card = self._registry.get(p.resolution.primary)
                except KeyError:
                    pass
                else:
                    return self._attach_gates(
                        Routed(
                            primary=card,
                            reason=f"판례 적용: intent '{intent}' → {p.resolution.primary}",
                        ),
                        intent,
                    )
        candidates = tuple(c for c in self._registry.all_cards() if intent in c.domains)
        if not candidates:
            return Unowned(escalated_to=self._root_user, reason=f"담당 없음: {intent or '미분류'}")
        if len(candidates) == 1:
            return self._attach_gates(
                Routed(primary=candidates[0], reason=f"intent '{intent}' 매칭"), intent
            )
        return Contested(candidates=candidates, reason=f"후보 {len(candidates)}건, Authority 미정")

    def _attach_gates(self, routed: Routed, intent: str) -> Routed:
        """Routed에 Approval·Collaborator를 부착한다(TRD §6 라우팅 5단계, T2.5).

        평가는 *intent 기반 결정론*이다 — 비결정(LLM 자유서술·질문 원문 매칭)을 회피하고
        Classifier가 이미 만든 intent 라벨만 본다(같은 질문 → 같은 게이트). 카드 자기보고
        필드(`approval_when`·`collaborate_when`)는 under-claim 보수성 그대로 — Authority는
        중앙이 선언하고, 이 두 필드는 owner가 "이 영역은 사람 사인 필요/협업 필요"를
        *스스로 좁히는* 보수적 자기보고다(ADR 0004 정합).

        규칙:
        - Approval: primary의 `approval_when`에 intent가 들면 `requires_approval=True`.
        - Collaboration: primary의 `collaborate_when`에 intent가 들면, *그 intent를 domains에
          가진 다른 카드*를 collaborator로 끌어들인다(primary 자신은 제외). 끌려들어온 카드가
          Collaborator(CONTEXT). 협업 대상을 카드가 agent_id로 *지목*하지 않고 intent→domains
          매칭으로 찾는 이유: 지목은 또 하나의 중앙 외 Authority 선언이 되고(누가 협업자인지를
          카드가 단정), domains 매칭은 라우팅과 같은 결정론 기준을 재사용한다.

        둘 다 부착이 없으면 routed를 그대로 돌려준다 — 빈 게이트 Routed.
        """
        import dataclasses

        primary = routed.primary
        requires_approval = intent in primary.approval_when
        collaborators: tuple[AgentCard, ...] = (
            self._collaborators_for(intent, primary)
            if intent in primary.collaborate_when
            else ()
        )

        if not requires_approval and not collaborators:
            return routed

        return dataclasses.replace(
            routed,
            requires_approval=requires_approval,
            collaborators=collaborators,
        )

    def _collaborators_for(self, intent: str, primary: AgentCard) -> tuple[AgentCard, ...]:
        """intent를 domains에 가진 *primary 외* 카드들을 collaborator 후보로 모은다.

        primary 자신은 제외(중복 금지). 결정론 정렬(agent_id)로 순서를 고정해 테스트
        안정성을 보장한다 — Registry 순회 순서에 의존하지 않게.
        """
        candidates = sorted(
            (
                c
                for c in self._registry.all_cards()
                if intent in c.domains and c.agent_id != primary.agent_id
            ),
            key=lambda c: c.agent_id,
        )
        return tuple(candidates)
