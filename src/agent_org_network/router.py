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
                    return Routed(
                        primary=card,
                        reason=f"판례 적용: intent '{intent}' → {p.resolution.primary}",
                    )
        candidates = tuple(c for c in self._registry.all_cards() if intent in c.domains)
        if not candidates:
            return Unowned(escalated_to=self._root_user, reason=f"담당 없음: {intent or '미분류'}")
        if len(candidates) == 1:
            return Routed(primary=candidates[0], reason=f"intent '{intent}' 매칭")
        return Contested(candidates=candidates, reason=f"후보 {len(candidates)}건, Authority 미정")
