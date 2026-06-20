from agent_org_network.classifier import Classifier
from agent_org_network.decision import Contested, RoutingDecision, Routed, Unowned
from agent_org_network.registry import Registry


class Router:
    def __init__(self, registry: Registry, classifier: Classifier, root_user: str) -> None:
        self._registry = registry
        self._classifier = classifier
        self._root_user = root_user

    def route(self, question: str) -> RoutingDecision:
        intent = self._classifier.classify(question)
        candidates = tuple(c for c in self._registry.all_cards() if intent in c.domains)
        if not candidates:
            return Unowned(escalated_to=self._root_user, reason=f"담당 없음: {intent or '미분류'}")
        if len(candidates) == 1:
            return Routed(primary=candidates[0], reason=f"intent '{intent}' 매칭")
        return Contested(candidates=candidates, reason=f"후보 {len(candidates)}건, Authority 미정")
