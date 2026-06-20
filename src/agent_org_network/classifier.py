from typing import Protocol


class Classifier(Protocol):
    def classify(self, question: str) -> str: ...


class FakeClassifier:
    def __init__(self, intent: str) -> None:
        self._intent = intent

    def classify(self, question: str) -> str:
        return self._intent


class RuleBasedClassifier:
    def __init__(self, keyword_intents: dict[str, str], default: str = "") -> None:
        self._keyword_intents = keyword_intents
        self._default = default

    def classify(self, question: str) -> str:
        for keyword, intent in self._keyword_intents.items():
            if keyword in question:
                return intent
        return self._default
