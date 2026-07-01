"""실 `claude -p` ProviderTransport — Claude 구독 자격 인프로세스 경로 [게이트 밖].

`StubProviderTransport`(게이트 기본·결정론)와 *같은 Protocol*(`ProviderTransport`)을 만족하는
**실 공급자 transport**. owner의 Claude Code 구독 자격을 `claude -p` 서브프로세스로 위임한다.

게이트 경계(정직한 분리·`provider_transport_anthropic.py` 패턴 그대로):
  - 이 모듈은 stdlib(`subprocess`)만 import하고 모듈 상단에서 네트워크·자격을 안 탄다.
  - 실 동작(`claude -p`)은 **기본 runner를 호출할 때만** 접촉한다. 게이트(`uv run pytest`)는
    이 transport에 **fake runner를 주입**하므로(또는 transport 자체를 안 쓰므로) 결정론 테스트가
    실 subprocess·네트워크를 절대 안 탄다. 게이트는 *import·타입*만 통과하면 된다(pyright·ruff).
  - 실 동작 검증은 수동 시연(`/author?AON_AUTHOR=claude-code`)이다.

왜 anthropic SDK가 아니라 `claude -p`인가(`provider_transport_anthropic.py` §실 시연 정정과 동일 논거):
  - 데모 백엔드 env에 `ANTHROPIC_API_KEY`가 없다 — 인자 없는 SDK는 owner의 Claude Code `/login`
    **구독 자격**(`~/.claude/.credentials.json`)을 해석 못 한다. 그 구독 토큰을 직접 API에 쓰는 건
    ToS/계정 정지 위험이라 안 한다.
  - **Claude 구독 답의 공식·robust 경로는 `claude -p`**(로컬 CLI·구독 인증·키 0). 그래서 author의
    실 추출도 SDK가 아니라 이 `claude -p` transport로 owner 구독 자격을 위임한다.

불변식:
  - **포트 무변경** — `__call__(request: ProviderRequest) -> Iterator[str]`(ProviderTransport
    Protocol 그대로). `LlmAuthor`가 `assemble_stream`으로 청크를 join하므로 응답 전체를 1청크로
    yield하면 충분하다(스트리밍 델타 분해 불필요).
  - **중앙 토큰 0** — `claude -p`는 owner 기기의 구독 자격을 쓴다. 중앙은 키/토큰을 주입하지 않는다.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from agent_org_network.provider_runtime import ProviderRequest

# `claude -p` 기본 타임아웃(초). author는 split/derive/link 3회 순차 호출이라 호출당 넉넉히.
DEFAULT_CLAUDE_CODE_TIMEOUT = 120.0


@dataclass(frozen=True)
class ClaudeCodeCall:
    """runner seam에 넘기는 단일 호출 값 객체 — 평탄화된 prompt + model.

    `ProviderRequest`(system + messages)를 단일 prompt로 평탄화한 뒤 runner에 넘긴다.
    model은 호출별 권위값(request.model 우선·없으면 transport 기본).
    """

    prompt: str
    model: str | None


def _flatten_request(request: ProviderRequest, default_model: str | None) -> ClaudeCodeCall:
    """ProviderRequest → 단일 prompt로 평탄화(순수·IO 0).

    system을 맨 앞에 두고 messages content를 순서대로 이어붙인다(맥락 우선). `claude -p`는
    system top-level 파라미터가 없어 단일 prompt로 합친다(SDK의 system 분리와 비대칭).
    """
    user_content = "\n\n".join(m.get("content", "") for m in request.messages)
    parts = [p for p in (request.system, user_content) if p]
    prompt = "\n\n".join(parts)
    return ClaudeCodeCall(prompt=prompt, model=request.model or default_model)


def _default_claude_runner(timeout: float) -> Callable[[ClaudeCodeCall], str]:
    """기본 runner 팩토리 — `claude -p <prompt> --output-format text` blocking 실행(게이트 밖).

    도구를 비허용(`--allowedTools ""`)해 응답이 OKF 읽기·파일 접근 부수효과로 오염되지 않게
    한다(author는 입력을 prompt로 받지 OKF를 읽지 않는다 — `runtime._exec_claude`의 OKF 읽기
    도구 허용과 비대칭). returncode 비정상이면 명확한 RuntimeError(fail-loud).
    """

    def _run(call: ClaudeCodeCall) -> str:
        args = ["claude", "-p", call.prompt, "--output-format", "text", "--allowedTools", ""]
        if call.model:
            args += ["--model", call.model]
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"claude -p exited with {completed.returncode}: {completed.stderr.strip()}"
            )
        return completed.stdout

    return _run


class ClaudeCodeTransport:
    """실 `claude -p` 인프로세스 transport — owner Claude 구독 자격 위임(게이트 밖).

    `ProviderTransport.__call__(request) -> Iterator[str]`을 만족(`StubProviderTransport`와
    교체 가능). request를 단일 prompt로 평탄화 → runner(`claude -p`) 호출 → 응답 전체를
    1청크로 yield(`assemble_stream`이 join). runner는 주입 가능 seam이라 테스트는 fake를
    꽂아 결정론을 유지하고, 미주입 시 기본 `claude -p` blocking runner를 쓴다.
    """

    def __init__(
        self,
        runner: Callable[[ClaudeCodeCall], str] | None = None,
        *,
        model: str | None = None,
        timeout: float = DEFAULT_CLAUDE_CODE_TIMEOUT,
    ) -> None:
        self._runner: Callable[[ClaudeCodeCall], str] = runner or _default_claude_runner(timeout)
        self._model = model

    def __call__(self, request: ProviderRequest) -> Iterator[str]:
        call = _flatten_request(request, self._model)
        text = self._runner(call)
        yield text
