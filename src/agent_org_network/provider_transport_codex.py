"""슬라이스 2 — 실 codex(OpenAI) OAuth ProviderTransport (ADR 0027 결정 2·9·10·11) [게이트 밖]

`StubProviderTransport`(게이트 기본·결정론)와 *같은 Protocol*(`ProviderTransport`)을 만족하는
**실 codex transport**. owner 기기의 `~/.codex/auth.json`(ChatGPT 구독 OAuth) 위임 +
공식 openai SDK 인프로세스 스트리밍. `AnthropicSdkTransport`(T9.6)와 동형 — owner측 자격·중앙 토큰 0.

게이트 경계(정직한 분리):
  - 이 모듈은 `openai` SDK를 *모듈 상단에서 import*한다(결정 9 — `[codex]` extra·dev에도 둬 게이트 통과).
  - 그러나 **실 네트워크·실 OAuth·auth.json 파일은 생성·호출 시점에만 접촉**한다 — 모듈 import·인스턴스
    생성만으론 파일을 안 읽고 클라이언트를 안 만들고 네트워크를 안 탄다(지연). 게이트(`uv run pytest`)는
    이 transport를 *주입하지 않으므로*(워커/web 기본은 `StubProviderTransport`·`ClaudeCodeRuntime`)
    결정론 테스트가 실 SDK·네트워크·auth.json을 절대 안 탄다. 게이트는 *import·타입*만 통과하면 된다.
  - 실 동작 검증은 수동 시연(`scripts/demo_e2e_provider.md` codex 절)이다.

불변식:
  - **중앙 토큰 0** — owner 기기의 `~/.codex/auth.json`(평문·owner 소유·codex CLI가 백그라운드 갱신)만
    읽는다. 중앙 코드에 토큰/키 박지 0·env 하드코딩 0. (`AnthropicSdkTransport`의 owner OAuth 프로필
    위임과 동형 — 자격은 owner 환경이 진실 원천.)
  - **포트 무변경** — `__call__(request: ProviderRequest) -> Iterator[str]`(ProviderTransport Protocol
    그대로). 청크를 yield하면 `assemble_stream`이 조립하고 `map_response_to_answer`가 Answer로 매핑한다
    (노출 불변식·게이트 내 순수 함수 재사용).

bespoke 통합(조사로 확인 — demo에서 키 구조 최종 검증):
  - 토큰 출처: `CODEX_HOME` env 또는 `~/.codex`의 `auth.json`. `tokens.access_token`(OAuth) +
    `tokens.account_id`(ChatGPT account id; 없으면 `id_token` JWT의 `chatgpt_account_id` claim).
    방어적 파싱 — codex CLI 버전별 키 구조가 다를 수 있어 여러 위치를 시도한다.
  - 엔드포인트: `https://chatgpt.com/backend-api/codex`(ChatGPT 구독 경로 — `api.openai.com` 아님).
    openai 클라이언트 `base_url`로 준다(SDK가 `/responses`를 붙인다).
  - 스키마: Responses API — `instructions`(필수·= request.system) + `input`(= request.messages 매핑).
  - 헤더: `Authorization: Bearer <access_token>`(SDK가 api_key로 처리) + `ChatGPT-Account-ID` +
    `User-Agent: codex_cli_rs/...`(default_headers).
  - 갱신: 401 시 auth.json 재독→재시도 1회(codex CLI가 백그라운드 갱신).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import openai

from agent_org_network.provider_runtime import ProviderRequest

# ChatGPT 구독 경로 base_url — openai SDK가 `/responses`를 이어 붙인다(api.openai.com 아님·결정 9).
CHATGPT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
# codex CLI가 보내는 User-Agent 모사 — ChatGPT 구독 백엔드가 codex 클라이언트를 식별한다.
CODEX_USER_AGENT = "codex_cli_rs/0.0.0"


def _codex_home() -> Path:
    """owner 기기의 codex 홈 디렉터리 — `CODEX_HOME` env 또는 `~/.codex` 기본."""
    import os

    override = os.environ.get("CODEX_HOME")
    return Path(override) if override else Path.home() / ".codex"


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """JWT의 payload(중간 segment)를 검증 없이 디코드한다(claim 추출 전용).

    서명 검증은 하지 않는다 — owner 기기의 자기 토큰에서 `chatgpt_account_id` 같은 claim을
    읽기만 한다(신뢰 경계 안). 형식이 JWT가 아니면 빈 dict.
    """
    import base64

    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload_b64 = parts[1]
    # base64url 패딩 보정.
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64)
        claims = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}
    return cast("dict[str, Any]", claims) if isinstance(claims, dict) else {}


def _first_str(*candidates: Any) -> str | None:
    """후보들 중 첫 번째 비어있지 않은 str을 돌려준다(방어적 파싱 헬퍼)."""
    for cand in candidates:
        if isinstance(cand, str) and cand:
            return cand
    return None


def _read_codex_auth() -> tuple[str, str | None]:
    """owner `~/.codex/auth.json`에서 (access_token, chatgpt_account_id)를 방어적으로 읽는다.

    codex CLI 버전별로 키 구조가 다를 수 있어 여러 위치를 시도한다(demo에서 실제 구조 최종 검증):
      - access_token: `tokens.access_token` → top-level `access_token`.
      - account_id: `tokens.account_id` → top-level → `id_token`/`access_token` JWT의
        `chatgpt_account_id`/`account_id` claim.
    account_id는 옵셔널(None 허용) — 없어도 Authorization만으로 동작하는 경우가 있어 헤더를 생략한다.
    파일·키가 없으면 SystemExit(수동 시연 안내) — 게이트 밖이라 친절한 owner 메시지로 멈춘다.
    """
    auth_path = _codex_home() / "auth.json"
    if not auth_path.exists():
        raise SystemExit(
            f"codex auth.json을 찾을 수 없습니다({auth_path}). owner 기기에서 `codex login`"
            "(ChatGPT 구독)으로 로그인하세요. 경로는 CODEX_HOME env로 바꿀 수 있습니다."
        )
    try:
        data: Any = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"codex auth.json 파싱 실패({auth_path}): {exc}") from exc

    obj: dict[str, Any] = cast("dict[str, Any]", data) if isinstance(data, dict) else {}
    raw_tokens = obj.get("tokens")
    tokens: dict[str, Any] = cast("dict[str, Any]", raw_tokens) if isinstance(raw_tokens, dict) else {}

    access_token = _first_str(tokens.get("access_token"), obj.get("access_token"))
    if not access_token:
        raise SystemExit(
            f"codex auth.json에 access_token이 없습니다({auth_path}). `codex login`을 다시 하세요."
        )

    account_id = _first_str(
        tokens.get("account_id"), obj.get("chatgpt_account_id"), obj.get("account_id")
    )
    if not account_id:
        # JWT claim에서 시도(id_token 우선, 없으면 access_token).
        for jwt_field in ("id_token", "access_token"):
            jwt = _first_str(tokens.get(jwt_field), obj.get(jwt_field))
            if jwt is None:
                continue
            claims = _decode_jwt_claims(jwt)
            # ChatGPT account id는 보통 중첩 claim(`https://api.openai.com/auth`)에 있다.
            auth_claim = claims.get("https://api.openai.com/auth")
            if isinstance(auth_claim, dict):
                nested: dict[str, Any] = cast("dict[str, Any]", auth_claim)
                account_id = _first_str(
                    nested.get("chatgpt_account_id"), nested.get("account_id")
                )
                if account_id:
                    break
            account_id = _first_str(
                claims.get("chatgpt_account_id"), claims.get("account_id")
            )
            if account_id:
                break

    return access_token, account_id


def _map_input(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    """공급자 중립 messages를 Responses API `input` 항목으로 매핑한다.

    각 {role, content}를 Responses input 메시지(role + content 텍스트 파트)로 옮긴다.
    system은 `instructions`로 따로 가므로 여기엔 user/assistant 발화만 온다.
    """
    items: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        part_type = "output_text" if role == "assistant" else "input_text"
        items.append({"role": role, "content": [{"type": part_type, "text": content}]})
    return items


class CodexOauthTransport:
    """실 codex(OpenAI) OAuth 인프로세스 스트리밍 ProviderTransport (게이트 밖·슬라이스 2).

    owner `~/.codex/auth.json`(ChatGPT 구독 OAuth)을 *지연* 읽어 공식 openai SDK 클라이언트를
    ChatGPT 구독 base_url로 만들고 `responses.stream(...)`으로 텍스트 델타를 yield한다 —
    기존 `ProviderTransport.__call__(request) -> Iterable[str]`을 만족(포트·매핑 함수 무변경·
    `StubProviderTransport`와 교체 가능). `AnthropicSdkTransport`와 동형.

    토큰 위임(결정 2·9): 클라이언트 생성 시 owner auth.json의 `access_token`을 `api_key`로,
    `chatgpt_account_id`를 `ChatGPT-Account-ID` 헤더로 준다 — 중앙 코드엔 토큰/키 박지 0(owner
    기기·owner 소유 파일이 진실 원천). 401이면 auth.json을 재독해 재시도 1회(codex CLI가 갱신).
    """

    def __init__(self) -> None:
        # 클라이언트·토큰은 *지연 로드*한다 — 모듈 import·인스턴스 생성만으론 auth.json 파일·
        # 네트워크를 안 탄다(게이트가 이 transport를 주입해도 호출 전까진 무접촉). 첫 호출에서
        # owner auth.json을 읽어 클라이언트를 만든다(중앙 토큰 0).
        self._client: openai.OpenAI | None = None

    def _build_client(self) -> openai.OpenAI:
        access_token, account_id = _read_codex_auth()
        headers: dict[str, str] = {"User-Agent": CODEX_USER_AGENT}
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        # access_token을 api_key로 — SDK가 `Authorization: Bearer <access_token>`로 보낸다.
        # base_url은 ChatGPT 구독 경로(api.openai.com 아님). 키 하드코딩 0(owner auth.json이 출처).
        return openai.OpenAI(
            base_url=CHATGPT_CODEX_BASE_URL,
            api_key=access_token,
            default_headers=headers,
        )

    def _ensure_client(self, *, force: bool = False) -> openai.OpenAI:
        if self._client is None or force:
            self._client = self._build_client()
        return self._client

    def __call__(self, request: ProviderRequest) -> Iterator[str]:
        model = request.model
        instructions = request.system
        input_items = _map_input(request.messages)
        try:
            yield from self._stream(self._ensure_client(), model, instructions, input_items)
        except openai.AuthenticationError:
            # 401 — auth.json을 재독(codex CLI가 백그라운드 갱신했을 수 있다)해 클라이언트를
            # 다시 만들고 한 번 재시도한다. 그래도 401이면 전파(owner가 `codex login` 재실행).
            client = self._ensure_client(force=True)
            yield from self._stream(client, model, instructions, input_items)

    def _stream(
        self,
        client: openai.OpenAI,
        model: str,
        instructions: str,
        input_items: list[dict[str, Any]],
    ) -> Iterator[str]:
        with client.responses.stream(
            model=model,
            instructions=instructions,
            input=input_items,  # pyright: ignore[reportArgumentType]
            store=False,  # ChatGPT 구독 codex 백엔드 요구(실 시연 확인 — store=false 강제·max_output_tokens 미지원).
        ) as stream:
            for event in stream:
                # Responses API SSE: `response.output_text.delta` 이벤트의 delta가 텍스트 청크.
                if getattr(event, "type", None) == "response.output_text.delta":
                    delta = getattr(event, "delta", "")
                    if delta:
                        yield delta
