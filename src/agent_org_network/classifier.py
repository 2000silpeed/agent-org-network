import subprocess
import tempfile
from collections.abc import Sequence
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


class ClassifierRunner(Protocol):
    """`claude -p`로 분류 1회를 돌리는 호출 가능 객체의 모양 — 테스트는 FakeRunner로 대체.

    `ClaudeRunner`(runtime.py)와 같은 *결정론 경계* 패턴: 실 LLM 호출은 비결정·느리므로
    주입 가능하게 둬 단위 테스트는 고정 응답 FakeRunner로 돌린다. `prompt`(intent 후보가
    실린 분류 프롬프트)를 받아 LLM 원응답(raw text)을 돌려준다 — 파싱은 호출자(LlmClassifier)
    책임. 실 소비는 eval/수동(파트 3).
    """

    def __call__(self, prompt: str, /) -> str: ...


class LlmClassifier:
    """Classifier 포트의 LLM 구현 — `claude -p` 헤드리스로 질문을 intent로 분류한다(T6.2).

    전송 수단(ADR 0010 정신을 중앙 분류에도): `ClaudeCodeRuntime`과 일관되게 `claude -p`
    헤드리스를 쓴다 — 중앙 API 키 0·로컬 claude 인증 재사용. Anthropic API/SDK(중앙 키
    필요)는 프로젝트가 그동안 회피해 온 방향이라 채택하지 않는다. `runner`(`ClassifierRunner`)
    주입으로 결정론 경계를 둔다(`ClaudeCodeRuntime.runner` 패턴 동일).

    intent 어휘 출처(주입): 유효 intent 집합을 *주입*받는다 — `intents`(명시 리스트, 보통
    registry domains의 합집합). LLM은 그 집합에서 하나를 고르거나, 어디에도 안 맞으면
    *빈 문자열* `""`을 낸다. 프롬프트가 후보를 싣고 응답을 파싱한다 — 미지·형식오류·집합 밖
    응답은 모두 안전하게 `""`로 떨어진다(→ Router에서 0 매칭 → Unowned, 미아 없음 보존).
    Router는 Classifier가 *유효 어휘 안의 라벨 또는 ""*를 낸다고 신뢰하므로, 어휘 외 환각을
    ""로 정규화하는 게 이 분류기의 계약이다.

    결정론 경계: runner 주입 fake로 단위 테스트(프롬프트에 intent 후보가 실림·응답 파싱·
    미지→"" 정규화)를 돌리고, 실 LLM 분류 정확도는 eval(파트 3, 골든셋)로만 본다(ADR 0003).
    """

    def __init__(
        self,
        intents: Sequence[str],
        runner: "ClassifierRunner | None" = None,
    ) -> None:
        # 유효 intent 어휘(주입) — LLM이 이 안에서 고르거나 "". 빈 집합이면 항상 ""(미분류).
        self._intents = tuple(intents)
        # 실 호출 경계. 기본값(None)이면 classify가 default_claude_classifier_runner로 지연 바인딩.
        self._runner = runner

    def build_prompt(self, question: str) -> str:
        """질문 + 유효 intent 후보를 실어 '하나를 고르거나 없으면 빈 값'을 요구하는 프롬프트.

        후보(`self._intents`)를 번호 매겨 명확히 나열하고, "정확히 하나의 라벨만 그대로
        출력, 어디에도 안 맞으면 아무것도 쓰지 말고 빈 줄"을 지시한다. 자유서술·설명·따옴표
        없이 라벨 문자열만 내라고 못박는다 — 파싱(_parse)이 어휘 대조로 흡수하지만, 프롬프트
        단에서 형식을 좁혀 오류 응답 자체를 줄인다.
        """
        candidates = "\n".join(f"{i}. {intent}" for i, intent in enumerate(self._intents, 1))
        return (
            "다음 질문을 아래 분류 라벨 중 정확히 하나로 분류하세요.\n"
            "\n"
            "분류 라벨:\n"
            f"{candidates}\n"
            "\n"
            "규칙:\n"
            "- 위 라벨 중 질문에 가장 맞는 것 하나의 텍스트를 그대로 한 줄로 출력하세요.\n"
            "- 어느 라벨에도 맞지 않으면 아무것도 출력하지 말고 빈 줄만 남기세요.\n"
            "- 설명·번호·따옴표·기타 문장 없이 라벨 텍스트만 출력하세요.\n"
            "\n"
            f"질문: {question}"
        )

    def _parse(self, raw: str) -> str:
        """LLM 원응답을 유효 intent 라벨 또는 ""로 정규화한다(파싱 경계).

        응답을 트림·따옴표 제거해 `self._intents`에 *정확히 있으면* 그 라벨, 없으면 ""(어휘
        외 환각·형식오류·미지·여러 줄을 모두 ""로 안전 흡수 — 미아 없음으로 흐름). 관대 처리:
        앞뒤 공백·감싼 따옴표(`'"` 백틱)·끝의 마침표를 벗겨 본 라벨과 대조한다. 단 어휘 밖이면
        무조건 "" — 부분일치·근사매칭은 하지 않는다(환각을 라벨로 승격시키지 않음).
        """
        text = raw.strip()
        if not text:
            return ""
        # 한 줄로 응답하라 지시했지만, 여러 줄/잡음이 와도 안전하게: 라벨에 정확히 일치하는
        # 줄을 찾되, 후보 라벨 자체에 줄바꿈은 없으므로 줄 단위로 대조한다.
        for line in text.splitlines():
            label = line.strip().strip("\"'`").strip().rstrip(".").strip()
            if label in self._intents:
                return label
        return ""

    def classify(self, question: str) -> str:
        """질문을 분류해 유효 intent 라벨 또는 ""를 돌려준다(Classifier 포트).

        build_prompt→runner→_parse. 빈 어휘(`self._intents`가 비면)는 고를 게 없으므로
        LLM을 부르지 않고 항상 ""(미분류 → Router 0매칭 → Unowned). `runner`가 None이면
        실 `claude -p` 러너(`default_claude_classifier_runner`)로 지연 바인딩한다. 미지·집합
        밖 응답은 _parse가 ""로 흡수한다.
        """
        if not self._intents:
            return ""
        runner = self._runner if self._runner is not None else default_claude_classifier_runner
        raw = runner(self.build_prompt(question))
        return self._parse(raw)


# 분류용 모델·타임아웃(ADR 0010 정신: 중앙 API 키 0, 로컬 claude 인증 재사용). 분류는
# 짧은 라벨 1개만 내면 되는 가벼운 작업이라 가장 빠르고 싼 Haiku(claude-api: Haiku 4.5,
# "fastest"·$1/$5 MTok)를 alias로 지정한다 — `ClaudeCodeRuntime`(담당자 답, 모델 미지정·
# 기본 모델)과 달리 분류는 명시적으로 빠른 모델로 좁힌다. 타임아웃도 답변(120s)보다 짧게.
CLASSIFIER_MODEL = "claude-haiku-4-5"
DEFAULT_CLASSIFIER_TIMEOUT_SECONDS = 30


def default_claude_classifier_runner(
    prompt: str,
    /,
    *,
    model: str = CLASSIFIER_MODEL,
    timeout: int = DEFAULT_CLASSIFIER_TIMEOUT_SECONDS,
) -> str:
    """`claude -p`를 한 번 돌려 분류 원응답(stdout)을 돌려준다 — `ClassifierRunner` 실 구현.

    `runtime._run_claude_headless`(ADR 0010·0013)와 같은 헤드리스 패턴: 임시 디렉터리를
    cwd로 두고(응답 잡음·프로젝트 CLAUDE.md/MCP 간섭 차단) `claude -p <prompt>
    --model <haiku> --output-format text`를 1회성으로 실행한다. 분류는 owner 지식·도구가
    필요 없으므로 `--allowedTools` 없이 텍스트 답만 받는다. 비정상 종료는 RuntimeError로
    올린다(LlmClassifier.classify가 부르는 자리 — 실패 처리·정책은 호출 맥락 책임).

    **이 함수는 게이트 밖**이다(실 LLM·비결정·외부 프로세스). 단위 테스트는 FakeRunner를
    주입하고, 이 실 러너의 분류 품질은 eval(파트 3, CLI `--classifier llm`)·수동으로만 본다
    (`ClaudeCodeRuntime` 실 claude와 같은 결정론 경계).
    """
    with tempfile.TemporaryDirectory() as workdir:
        completed = subprocess.run(
            ["claude", "-p", prompt, "--model", model, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workdir,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"claude -p (classifier) exited with {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout
