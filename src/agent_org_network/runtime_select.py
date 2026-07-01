"""공급자 런타임 선택 — `AON_PROVIDER`/`AON_RUNTIME` env 기반 공급자 중립 레지스트리.

ADR 0027 결정 1·11 — 어떤 공급자도 1급 아님. owner가 `AON_PROVIDER`로 자기 구독 공급자를
고른다. 미설정→레거시 기본(`ClaudeCodeRuntime`·`claude -p`·게이트·데모 무변경).

이 모듈을 worker.py·web.py가 *공유*한다(단일 출처). worker(별도 프로세스)뿐 아니라 인프로세스
web 데모(`web:app`)도 같은 선택 로직으로 owner 공급자를 켤 수 있다.

순환 import 회피: 모듈 레벨 import는 코어 `runtime`(AgentRuntime·ClaudeCodeRuntime)만이다.
공급자 SDK 어댑터(anthropic·openai)는 *팩토리 안에서 지연 import*한다 — 그 공급자를 고를 때만
SDK를 건드린다(미설치 owner는 무접촉·중앙 의존 0). `runtime`은 이 모듈을 import하지 않는다.
"""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from agent_org_network.runtime import AgentRuntime, ClaudeCodeRuntime

if TYPE_CHECKING:
    from agent_org_network.git_gateway import GitGateway

# ── 공급자 레지스트리 (ADR 0027 결정 1·11 — 공급자 중립) ──────────────────────────
# AON_PROVIDER 값(별칭) → 공급자 어댑터. 각 공급자는 *대칭*: 자기 SDK extra + 자기 OAuth 프로필
# + 권장 모델을 어댑터 안에 가둔다. 코어는 어떤 공급자도 1급이 아니고 *어떤 공급자 SDK에도 의존하지
# 않는다*(SDK는 선택 의존성 — `pip install agent-org-network[claude-api]`). 새 공급자(codex·gemini)는
# 아래 두 맵에 한 줄씩 추가한다 — claude 특권 없음. SDK import는 그 공급자를 *고를 때만* 한다(다른
# 공급자 owner는 미설치 SDK를 안 건드림).


def _make_claude_api_runtime(okf_root: str | Path | None) -> AgentRuntime:
    """Claude 공급자 어댑터 — owner OAuth 인프로세스 anthropic SDK 스트리밍(중앙 토큰 0).

    okf_root 주입으로 A(ii) OKF 접지 활성 — ClaudeCodeRuntime cwd 접지 대칭.
    """
    try:
        from agent_org_network.provider_runtime import ClaudeApiRuntime
        from agent_org_network.provider_transport_anthropic import AnthropicSdkTransport
    except ImportError as exc:  # 그 공급자 extra 미설치
        raise SystemExit(
            "AON_PROVIDER=claude-api 인데 anthropic SDK가 없습니다 — 자기 공급자 extra를 설치하세요: "
            "pip install 'agent-org-network[claude-api]'  (uv: uv sync --extra claude-api)"
        ) from exc
    return ClaudeApiRuntime(transport=AnthropicSdkTransport(), okf_root=okf_root)


def _make_codex_runtime(okf_root: str | Path | None) -> AgentRuntime:
    """Codex(OpenAI) 공급자 어댑터 — owner ~/.codex/auth.json OAuth 인프로세스 openai SDK 스트리밍.

    claude 팩토리와 대칭: 자기 SDK extra(`[codex]` → openai)·자기 OAuth 자격(owner 기기
    auth.json)을 *지연* import로 가둔다 — codex를 고를 때만 openai SDK를 건드린다(중앙 토큰 0).
    okf_root 주입으로 A(ii) OKF 접지 활성 — ClaudeApiRuntime 대칭.
    """
    try:
        from agent_org_network.provider_runtime import CodexApiRuntime
        from agent_org_network.provider_transport_codex import CodexOauthTransport
    except ImportError as exc:  # 그 공급자 extra 미설치
        raise SystemExit(
            "AON_PROVIDER=codex 인데 openai SDK가 없습니다 — 자기 공급자 extra를 설치하세요: "
            "pip install 'agent-org-network[codex]'  (uv: uv sync --extra codex)"
        ) from exc
    return CodexApiRuntime(transport=CodexOauthTransport(), okf_root=okf_root)


# 별칭 → 공급자 키 (후속: "gemini"/"google" → "gemini").
_PROVIDER_ALIASES: dict[str, str] = {
    "claude-api": "claude-api",
    "anthropic": "claude-api",
    "provider": "claude-api",
    "codex": "codex",
    "openai": "codex",
}
# 공급자 키 → lazy 어댑터 팩토리 (okf_root: str | Path | None → AgentRuntime).
_PROVIDER_FACTORIES: dict[str, Callable[[str | Path | None], AgentRuntime]] = {
    "claude-api": _make_claude_api_runtime,
    "codex": _make_codex_runtime,
}


def select_runtime(
    okf_root: str | Path | None,
    git_gateway: "GitGateway | None" = None,
) -> AgentRuntime:
    """env 플래그로 답 생성 런타임을 고른다 — 공급자 중립 레지스트리(ADR 0027 결정 1·11).

    `AON_PROVIDER`(또는 `AON_RUNTIME`):
      - 미설정/`claude-code` → `ClaudeCodeRuntime`(레거시 기본·`claude -p` 서브프로세스·okf cwd).
        **게이트·기존 데모 무변경.** 이건 claude CLI를 쓰는 *레거시 기본*이지 코어의 claude 의존이
        아니다 — 코어 pip 의존엔 어떤 공급자 SDK도 없다(SDK는 선택 extra). owner는 `AON_PROVIDER`로
        자기 구독 공급자를 고른다.
      - 등록된 공급자(`claude-api` 등) → 그 공급자의 OAuth 인프로세스 SDK 어댑터(중앙 토큰 0).
        공급자 SDK는 *그 공급자를 고를 때만* import(미설치면 extra 설치 안내).
      - 알 수 없는 값 → 명시 실패(조용히 claude로 안 떨어진다 — owner 의도 보존).

    `git_gateway`(선택, ADR 0018 결정 4): 주입 시 **claude-code 분기에서만** `ClaudeCodeRuntime`에
    넘겨 커밋 스냅샷 모드를 켠다 — 답 생성이 그 게이트웨이의 `head_sha` 번들(시드+저작)을 cwd로
    접지한다. 다른 공급자 분기(`claude-api`·`codex` 등)는 자기 SDK가 owner OKF를 직접 접지하므로
    게이트웨이를 *쓰지 않는다*(무영향). 미주입이면 기존 동작(working tree 직독·하위호환).

    모든 어댑터가 같은 `AgentRuntime` 포트라 호출 측(`WorkerLogic`·`build_demo`) 무변경(런타임
    교체가 종착·라우팅·노출 불변식을 안 바꿈). 자격증명은 owner측·**중앙은 키/토큰 0**.
    """
    import os

    flag = (os.environ.get("AON_PROVIDER") or os.environ.get("AON_RUNTIME") or "").strip().lower()
    if not flag or flag == "claude-code":
        return ClaudeCodeRuntime(okf_root=okf_root, git_gateway=git_gateway)
    provider = _PROVIDER_ALIASES.get(flag)
    factory = _PROVIDER_FACTORIES.get(provider) if provider is not None else None
    if factory is None:
        supported = ", ".join(sorted(_PROVIDER_ALIASES))
        raise SystemExit(f"알 수 없는 AON_PROVIDER={flag!r} — 지원: claude-code(기본), {supported}")
    print(
        f"[runtime_select] AON_PROVIDER={flag} → owner OAuth 인프로세스 공급자 SDK 어댑터 사용"
        "(owner 프로필 자동 해석·중앙 토큰 0·게이트 밖 T9.6)."
    )
    return factory(okf_root)
