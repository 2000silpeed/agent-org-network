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

## 전제 — owner OAuth 프로필

워커 기기에 owner의 Anthropic OAuth 프로필이 *하나* 잡혀 있어야 한다. 둘 중 하나로 정리한다
(아래 "프로필 충돌 정리" 참조):

- **(권장) 기존 Claude Code 로그인 재사용** — 이미 `claude` CLI에 로그인돼 있으면(`claude
  /login` 또는 구독 인증) 같은 프로필 resolution을 SDK가 공유한다. owner 재설정 0.
- **`ant auth login`** — Anthropic 공식 CLI로 OAuth 로그인. Claude Code를 안 쓰는 owner용.

확인:

```bash
# Claude Code 로그인 상태(서브프로세스 경로가 쓰던 그 인증)
claude -p "ping" --output-format text

# anthropic SDK가 프로필을 해석하는지(인자 없는 클라이언트가 401 없이 짧은 스트림을 내는지)
uv run python - <<'PY'
import anthropic
c = anthropic.Anthropic()  # 키 주입 X — owner OAuth 프로필 자동 해석
with c.messages.stream(model="claude-opus-4-8", max_tokens=64,
                       messages=[{"role": "user", "content": "한 단어로 인사해줘"}],
                       thinking={"type": "adaptive"}) as s:
    print("".join(s.text_stream))
PY
```

마지막 블록이 짧은 한국어 인사를 출력하면 OAuth 프로필이 잡힌 것이다. `401`/`AuthenticationError`
가 나면 프로필이 없거나 충돌이다(아래 정리).

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
