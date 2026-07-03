"""시연용 즉답 데모 서버 (포트 8765, 인증 ON).

라우팅·Contested 합의·판례 학습·Approval 게이트·cannot_answer 거부·미아 escalation·
감사 로그·Org 그래프·빌더는 **모두 실제 도메인 로직** 그대로다. 단 답변 *텍스트*는
실 claude가 OKF를 읽어 생성한 답을 미리 받아 즉시 반환한다(영상 시연 중 36초 대기를
없애 흐름을 매끄럽게 — 답 내용 자체는 실제 claude 산출물). Approval(draft_only)·
cannot(거부)·Contested(Pending)는 런타임이 아니라 라우팅 결정이 강제하므로 그대로 동작한다.
"""

import os

from agent_org_network.agent_card import AgentCard
from agent_org_network.runtime import Answer, AnswerChunk
from agent_org_network.web import create_app

# 실 claude가 OKF/카드 맥락으로 생성한 답 텍스트 (agent_id, intent) → 답.
_ANSWERS: dict[tuple[str, str], str] = {
    ("cs_ops", "환불"): (
        "네, 환불 가능하지만 전액은 아닙니다. 단순 변심 · 결제 후 20일 · 10만원 기준으로 "
        "환불정책상 계산은 이렇습니다.\n\n"
        "- 결제 후 20일 → 8~30일 구간 → 결제액의 50% = 50,000원\n"
        "- 단순 변심이므로 추가 10% 수수료 차감 → 5,000원 차감\n"
        "- 최종 환불액: 45,000원\n\n"
        "처리는 고객센터 접수(티켓 발급) → 영업일 3일 내 검토 → 원 결제수단 환불 순서입니다. "
        "30일을 넘기면 환불은 불가합니다."
    ),
    ("cs_ops", "보상"): (
        "보상은 환불 정책에 준해 처리됩니다. 귀책 사유와 결제 구간에 따라 보상 한도가 "
        "달라지며, 표준 사례는 환불 정책의 보상 조항을 적용합니다. 개별 건은 고객센터 "
        "접수 후 검토합니다."
    ),
    ("hr_ops", "평가"): (
        "직원 평가는 분기별로 진행됩니다. 목표 대비 성과를 1차 자기평가 → 2차 리더 면담 "
        "→ 최종 등급 산정 순으로 정리하며, 평가 결과는 보상·승진 검토의 근거가 됩니다. "
        "구체 일정과 양식은 인사팀 평가 가이드를 따르세요."
    ),
    ("hr_ops", "채용"): (
        "채용은 서류 전형 → 실무 면접 → 컬처핏 면접 → 처우 협의 순으로 진행됩니다. "
        "직무별 요구사항과 평가표는 채용 가이드를 참고하세요."
    ),
    ("it_ops", "접근권한"): (
        "접근 권한 요청은 대상 시스템·사유·기간을 명시해 신청하면, 보안 정책에 따라 "
        "최소 권한 원칙으로 부여됩니다. 민감 시스템은 승인 절차가 추가됩니다."
    ),
    ("contract_ops", "계약 검토"): (
        "계약 조건 변경은 표준계약서 기준으로 검토합니다. 변경 항목·사유를 정리해 주시면 "
        "법무 검토 후 가능 여부와 대안을 회신합니다."
    ),
    ("finance_ops", "가격"): (
        "가격·견적은 가격표와 할인 규정을 기준으로 산정합니다. 수량·계약 기간에 따른 "
        "할인 적용 여부를 확인해 견적을 제공합니다."
    ),
}

_KW = {
    "환불": "환불", "보상": "보상", "평가": "평가", "채용": "채용", "입사": "채용",
    "접근": "접근권한", "권한": "접근권한", "계약": "계약 검토", "가격": "가격", "견적": "가격",
}


def _intent_of(question: str) -> str:
    for kw, intent in _KW.items():
        if kw in question:
            return intent
    return ""


class DemoRuntime:
    """실 claude 산출 답을 즉시 반환하는 시연용 런타임(AgentRuntime 포트 구현).

    answer_stream: 프런트 /ask/stream 경로용 — 같은 답을 어절 단위 델타로 흘려
    실 스트리밍과 같은 타자기 체감을 준다(촬영 시 흐름 연출).
    """

    def _text_of(self, question: str, card: AgentCard) -> str:
        return _ANSWERS.get((card.agent_id, _intent_of(question)), f"{card.summary}")

    def answer(self, question: str, card: AgentCard, context: str | None = None) -> Answer:
        text = self._text_of(question, card)
        return Answer(text=text, sources=tuple(card.knowledge_sources), mode="full")

    def answer_stream(self, question: str, card: AgentCard, context: str | None = None):
        import time as _t

        text = self._text_of(question, card)
        buf = ""
        for token in text.split(" "):
            buf += token + " "
            if len(buf) >= 14:
                yield AnswerChunk(text_delta=buf)
                buf = ""
                _t.sleep(0.045)
        if buf:
            yield AnswerChunk(text_delta=buf)


app = create_app(
    runtime=DemoRuntime(),
    session_secret=os.environ.get("OPERATOR_SESSION_SECRET", "demo-session-secret-key"),
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
