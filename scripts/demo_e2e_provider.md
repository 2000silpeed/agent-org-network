# 수동 시연 — owner OAuth 인프로세스 공급자 스트리밍 (Claude·anthropic SDK)

T9.6. ADR 0027 결정 2·4·9·10. **게이트 밖 수동 시연**(실 OAuth·실 네트워크·비결정 스트리밍).

대화 답변 경로를 `claude -p` 서브프로세스 대신 **owner OAuth 인프로세스 anthropic SDK
스트리밍**으로 바꿔(`AON_PROVIDER=claude-api`) 한 바퀴 돈다. 기본(미설정) 경로는
`scripts/demo_e2e.md`(`claude -p`)와 동일하다 — 이 문서는 *공급자 SDK opt-in*만 다룬다.

핵심 불변식(보존): **중앙은 모델 키/토큰을 0개 보관**한다. 워커의 인자 없는
`anthropic.Anthropic()`가 *owner의 Anthropic OAuth 프로필*을 자동 해석한다 — 생성자·env에
키를 주입하지 않는다. 게이트(`uv run pytest` 1162 passed)는 이 경로를 안 탄다(`StubProviderTransport`
가 기본·실 transport는 opt-in 플래그일 때만 import).

---

## 전제 — Claude 자격 (⚠️ 실 시연 정정 2026-06-27)

**중요 — 실 시연으로 확인된 것**: 인자 없는 `anthropic.Anthropic()`는 owner의 `ANTHROPIC_API_KEY`
env 또는 `ant auth login`(공식 console OAuth) 프로필만 해석한다. **기존 Claude Code `/login` 구독
자격(`~/.claude/.credentials.json`)은 *해석 못 한다*** — 그 토큰을 직접 API에 쓰는 건 Anthropic
ToS 위반·**계정 정지 위험**이라 *안 한다*. 따라서:

- **Claude *구독* 답은 `claude -p`(`ClaudeCodeRuntime`)** — 공식 CLI·robust·실 시연 검증(이 경로는
  `AON_PROVIDER` 미설정 시 워커 기본). 구독 owner는 이 경로를 쓴다.
- **Claude *인프로세스 SDK*(`AON_PROVIDER=claude-api`)** — `ANTHROPIC_API_KEY` 또는 `ant auth login`
  owner용. 아래 확인이 401 없이 통하면 이 빠른 경로를 쓸 수 있다.

확인(인프로세스 SDK 경로용 — API 키/`ant` OAuth가 있을 때):

```bash
# Claude Code 구독 경로(robust·기본)는 이걸로 확인:
claude -p "ping" --output-format text   # pong 나오면 구독 답은 claude -p로 동작

# 인프로세스 SDK 경로는 ANTHROPIC_API_KEY 또는 `ant auth login`이 있어야 통한다:
uv run python - <<'PY'
import anthropic
c = anthropic.Anthropic()  # ANTHROPIC_API_KEY env 또는 ant 프로필 자동 해석(키 주입 X)
with c.messages.stream(model="claude-opus-4-8", max_tokens=64,
                       messages=[{"role": "user", "content": "한 단어로 인사해줘"}],
                       thinking={"type": "adaptive"}) as s:
    print("".join(s.text_stream))
PY
```

`401`/`Could not resolve authentication`이면 API 키/`ant` 프로필이 없는 것이다 — 그 경우 Claude는
`claude -p`(구독·기본 경로)를 쓴다.

---

## 한 바퀴 — 중앙 + 공급자 SDK 워커 + 질문

`.venv` 준비(`uv sync`) 후 세 터미널.

### 터미널 A — 중앙 서버

```bash
scripts/run_central.sh
```

### 터미널 B — owner 워커 (공급자 SDK opt-in)

```bash
AON_PROVIDER=claude-api scripts/run_worker.sh cs_lead
```

로그에 다음이 보이면 공급자 SDK 경로다:

```
[worker] AON_PROVIDER=claude-api — owner OAuth 인프로세스 SDK 스트리밍 사용(...중앙 토큰 0...).
[worker:cs_lead|primary] 중앙에 등록됨(ws://127.0.0.1:8000/worker). 작업 대기.
```

(플래그 없이 `scripts/run_worker.sh cs_lead`로 띄우면 기존 `claude -p` 경로 — 무변경.)

### 터미널 C — 질문 → 답 회수

```bash
curl -s -X POST http://127.0.0.1:8000/ask \
  -H 'content-type: application/json' \
  -d '{"question":"환불 규정 알려줘"}'
# → {"type":"pending",...,"tracking":"<HEX>"}

TRACKING=<위 HEX>
until curl -s http://127.0.0.1:8000/ask/$TRACKING | grep -q '"answered"'; do
  echo "...대기"; sleep 2
done
curl -s http://127.0.0.1:8000/ask/$TRACKING
```

`answered`의 `text`가 *anthropic SDK 스트리밍으로 생성된 답*이다. 터미널 B 워커 로그에
`작업 수신 ...` → `답 회신 ...`이 찍힌다. 사용자向 답은 담당·승인·출처(`answered_by`·`mode`·
`sources`)만 싣고 내부(모델·thinking·토큰)는 감춘다(노출 불변식·`map_response_to_answer` 보존).

브라우저 채팅 UI(`http://127.0.0.1:8000/`)에서도 같은 흐름이 돈다(세션 쿠키·멀티턴 맥락 포함).

---

## 검증 항목 (수동)

- [ ] **OAuth 프로필 자동 해석** — 워커가 키 주입 없이(`AON_PROVIDER=claude-api`) 답을 만든다.
      생성자·env에 `ANTHROPIC_API_KEY`를 *안* 넣어도 owner 프로필로 동작(중앙 토큰 0).
- [ ] **스트리밍 답** — 답이 실 anthropic SDK `messages.stream().text_stream`으로 생성된다
      (서브프로세스 스폰 없이 인프로세스 — 첫 토큰까지 지연이 `claude -p`보다 짧음=속도 근거).
- [ ] **기본 모델 `claude-opus-4-8`** — `request.model`이 비면 transport 기본값(`claude-opus-4-8`).
      override는 어댑터/구성에서(게이트 밖).
- [ ] **노출 불변식** — 사용자 답에 모델명·thinking·confidence가 안 실린다(`answered_by`·`mode`·
      `sources`만). 미아 없음·Authority 중앙·종착 무변경(런타임 교체가 라우팅을 안 바꿈).
- [ ] **게이트 무영향** — 같은 저장소에서 `uv run pytest`(1162 passed)·`uv run pyright`(0)·
      `uv run ruff check`(0)이 그대로 green(실 transport opt-in이라 결정론 테스트 무접촉).

---

## 프로필 충돌 정리 (claude-api 스킬 경고)

Claude Code `/login` 자격과 `ant` 프로필이 둘 다 잡혀 있으면 SDK의 프로필 resolution이
어느 쪽을 쓸지 모호할 수 있다(claude-api 스킬 경고). **둘 중 하나로 정리**한다:

1. **Claude Code 로그인만 쓴다(권장)** — `ant auth login`을 따로 하지 않고, 이미 로그인된
   `claude` CLI 프로필을 SDK가 공유하게 둔다. 별도 설정 0.
2. **`ant` 프로필만 쓴다** — `ant auth login`으로 명시 로그인하고, 환경에 `ANTHROPIC_API_KEY`
   같은 키 env가 *없도록* 비운다(키 env가 있으면 OAuth보다 우선해 OAuth 위임이 안 됨).

검증: 위 "전제"의 `anthropic.Anthropic()` 짧은 스트림 블록이 단일 자격으로 401 없이 통하면
정리된 것이다. 두 자격이 충돌하면 한쪽을 로그아웃(`claude /logout` 또는 `ant auth logout`)하고
재확인한다.

> 어느 경우에도 **워커 코드·중앙 코드에 키/토큰을 박지 않는다** — `anthropic.Anthropic()`는
> 항상 *인자 없이* 만들고 자격은 owner 환경 프로필이 진실 원천이다(중앙 토큰 0·ADR 0027 결정 2·9).

---

## 두 번째 공급자 — codex (OpenAI · ChatGPT 구독 OAuth)

슬라이스 2. ADR 0027 결정 2·9·10·11. **게이트 밖 수동 시연**. Claude(`claude-api`)와 *동형* —
같은 포트·같은 매핑 함수·다른 transport(openai SDK)·다른 owner 자격(ChatGPT 구독). 게이트는
**1184 그대로 green**(codex 실 transport는 opt-in·지연·결정론 테스트 무접촉).

핵심 불변식(보존): **중앙은 모델 키/토큰을 0개 보관**한다. 워커가 owner 기기의
`~/.codex/auth.json`(평문·owner 소유·codex CLI가 백그라운드 갱신)만 읽어 OAuth `access_token`을
얻는다 — 중앙 코드·env에 키를 박지 않는다.

### 전제 — owner ChatGPT 구독 OAuth (`codex login`)

워커 기기에 owner의 codex(OpenAI) ChatGPT 구독 OAuth가 잡혀 있어야 한다.

```bash
# codex CLI로 ChatGPT 구독 로그인(API 키 경로 아님 — 구독 OAuth).
codex login

# 토큰 파일 존재 확인(경로는 CODEX_HOME env 또는 ~/.codex 기본).
ls -l ~/.codex/auth.json
```

`auth.json`에 OAuth `access_token`과 ChatGPT account id가 들어간다(정확한 키 구조는 codex CLI
버전별로 다를 수 있어 transport가 방어적으로 파싱한다 — 아래 검증 항목 참조).

### 한 바퀴 — codex SDK 워커

중앙(터미널 A)은 위와 동일(`scripts/run_central.sh`). 워커만 codex로 띄운다.

```bash
# extra 설치(자기 구독 공급자만): openai SDK.
uv sync --extra codex

# owner 워커 — codex opt-in.
AON_PROVIDER=codex scripts/run_worker.sh cs_lead
```

로그에 다음이 보이면 codex SDK 경로다:

```
[worker] AON_PROVIDER=codex → owner OAuth 인프로세스 공급자 SDK 어댑터 사용(...중앙 토큰 0...).
[worker:cs_lead|primary] 중앙에 등록됨(ws://127.0.0.1:8000/worker). 작업 대기.
```

(`AON_PROVIDER=openai`도 같은 codex 어댑터로 라우팅된다.) 질문→답 회수는 위 claude 절의
터미널 C와 동일하다 — `answered`의 `text`가 *openai Responses API 스트리밍으로 생성된 답*이다.
사용자向 답은 담당·승인·출처만 싣고 내부(모델·토큰)는 감춘다(노출 불변식·`map_response_to_answer`
보존).

### ✅ 실 시연 검증 완료 (2026-06-27 · owner ChatGPT 구독)

실제 owner 구독으로 `CodexApiRuntime.answer`까지 한 바퀴 돌려 **접속·OAuth·스트리밍을 입증**했다(답 생성 성공·카드 페르소나 반영·`sources` 보존·`mode=full`·중앙 토큰 0). 그 과정에서 bespoke 백엔드의 요구 3가지를 확정해 코드에 반영했다:

- [x] **auth.json 키 구조** — `tokens.access_token`·`tokens.account_id`·`tokens.id_token`(JWT) 확인(`_read_codex_auth` 정합·`OPENAI_API_KEY: null` = 구독 OAuth 경로).
- [x] **엔드포인트·OAuth 도달** — `base_url=https://chatgpt.com/backend-api/codex`에 도달·OAuth 토큰 인증 성공(401 아님).
- [x] **모델 = ChatGPT 계정 지원 모델만** — `gpt-5.2-codex`는 *미지원*(400 "not supported when using Codex with a ChatGPT account"). 지원: `gpt-5.5`(기본)·`gpt-5.4`·`gpt-5.4-mini`·`gpt-5.3-codex-spark`(`~/.codex/models_cache.json`·`config.toml`). → **기본 모델을 `gpt-5.5`로 수정**(`CodexApiRuntime._DEFAULT_CODEX_MODEL`).
- [x] **`store=false` 강제** — 미설정 시 400 "Store must be set to false". → `responses.stream(store=False)`.
- [x] **`max_output_tokens` 미지원** — 보내면 400 "Unsupported parameter". → 제거.
- [x] **Responses API SSE** — `response.output_text.delta`의 `delta`가 텍스트 청크로 yield(형식 맞음).
- [x] **게이트 무영향·공급자 중립** — `uv run pytest` 1184·pyright 0·ruff 0 그대로. `uv sync --no-dev`엔 openai 0(codex 고를 때만 `[codex]` extra·import).
- [ ] **남은 확인**: 토큰 만료 401→재독 재시도(장시간 세션)·다른 owner 기기/계정에서 동일 동작.

> codex transport도 **워커·중앙 코드에 키/토큰을 박지 않는다** — owner `~/.codex/auth.json`이
> 진실 원천이고 그 파일은 owner 기기·owner 소유다(중앙 토큰 0·ADR 0027 결정 2·9·11).
>
> **SDK 검증 완료**: 공식 openai SDK(결정 9)의 `client.responses.stream(base_url=chatgpt 구독·store=False)`이
> ChatGPT 구독 codex 백엔드와 정합함을 실 시연으로 확인했다(httpx 폴백 불요). `response.output_text.delta`
> SSE를 그대로 yield. 백엔드 변경 시 폴백이 필요해지면 `_stream`만 raw httpx로 바꾼다(`__call__` 안에
> 캡슐화·포트·매핑 함수 무변경).
