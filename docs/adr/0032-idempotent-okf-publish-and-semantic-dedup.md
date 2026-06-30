# 멱등 OKF publish — 누적 인덱스 도출·concept_id 안정성·명시 삭제 + 의미 기반 near-dup

상태: proposed (2026-06-30) · **범위 B+C 사용자 승인(grill 합의)** · **ADR 0028(published index·scale routing)·0029(OKF 자동 저작)·0030(owner측 저작 토폴로지·크로스머신 fan-out)의 후속(supersede 아님 — refine)** · ADR 0018(OKF 번들 git 저장·빌더 커밋)·0019(신선도·변경 전파)·0013(OKF)·0004(Authority 중앙)와 정합 · **B는 게이트 내 결정론(머신 존재·즉가치)·C는 후속(임베딩·게이트 밖·T10.5와 합류)** · CONTEXT(증분 재인덱싱·OKF admission·published index 절 갱신 대상·신규 용어 `OkfTombstone`·`OkfConceptKey`·near-dup 후보) · tasks(T11.8 슬라이스 신설) 갱신 대상

## 맥락 — "전체 교체" stateless 저작의 세 결함

OKF 자동 저작(`/author`)의 실 추출 경로가 E2E로 동작 확정됐다(`AON_AUTHOR=claude-code` → `select_author` → `LlmAuthor(ClaudeCodeTransport)` → `run_authoring_pipeline` → `admit_okf` → `commit_okf_bundle` → publish). 그러나 publish 경로가 **stateless 전체 교체**라 세 가지가 깨진다.

추적한 경로(`web.py` `/author/publish` ~1505):

```
disposition 적용(rejected 제외·edited 반영) → OkfDraft(이번 문서 개념만)
  → admit_okf(over-claim 필터)
  → commit_okf_bundle(_git_gateway)                    # ★ owner git: 누적(append-only)
  → build_index_from_admitted(result.admitted, card, tmp_dir, ...)  # ★ 이번 admitted만 임시 디렉터리에 씀
      → build_knowledge_index_from_okf(card, tmp_dir, ...)          #   tmp_dir/{agent_id}/*.md만 glob → 부분 인덱스
  → accept_published_index(... index ...)              # ★ store.put(filtered): 그 agent_id 인덱스 통째 교체
```

**핵심 비대칭(결함의 뿌리)**: `commit_okf_bundle`은 owner git working tree에 파일을 *누적*한다(같은 `agent_id` 디렉터리에 새 `.md` append·`SubprocessGitGateway`는 실 디렉터리에 누적, `FakeGitGateway`는 in-memory 트리에 append). 그런데 `/author/publish`는 인덱스를 owner OKF 본체가 아니라 **격리 임시 디렉터리에 이번 `result.admitted.documents`만 써서** 도출한다 → 부분 인덱스. `accept_published_index`의 `store.put(filtered)`는 그 `agent_id` 인덱스를 *통째로 교체*하므로, **두 번째 문서를 publish하면 첫 문서 개념이 중앙 인덱스에서 사라진다**.

세 결함:

1. **멱등성 없음** — 같은 문서를 다시 추출/publish하면, LLM이 concept_id(제목 슬러그)를 조금만 다르게 내도 다른 개념으로 잡힌다. owner git에는 두 버전이 다 누적되고(`refund-v1.md`·`refund-v2.md` 식), 인덱스도 교체라 결과가 비결정.
2. **증분 누적 안 됨** — "기존 지식에 새 문서를 더한다"가 안 된다(인덱스가 이번 admitted만 보므로 교체).
3. **의미 기반 중복 미처리** — concept_id가 다르지만 의미가 같거나 "비슷하게 조금 다른" 개념을 병합/갱신하는 로직이 없다.

**이미 있는 자산**:
- `build_knowledge_index_from_okf`(okf_index.py)는 **`okf_root/{agent_id}/*.md` 전체를 glob**해 인덱스를 도출한다 — 즉 owner OKF 디렉터리 전체가 진실 원천이다. 워커 경로 `WorkerLogic.publish_frames`(worker.py:187)는 *실 owner OKF 디렉터리 전체*로 인덱스를 도출한다(이미 누적 멱등). ADR 0030 S3가 "커밋-직후 진실 원천은 디스크"라고 명문화했다.
- `reindex_incrementally`(okf_authoring.py:533) — changed_sources만 재처리·concept_id 키로 병합·나머지 보존하는 증분 머신이 *구현돼 있으나 web 라우트에 안 붙어 있다*. ADR 0030이 이를 *저작-시점 메모리 증분*으로 위치 지었고(크로스머신 fan-out은 디스크 재도출 `publish_frames`를 씀), T11.7c/d 내부 최적화로 남겼다.

### 보존해야 할 불변식 (이 ADR이 깨면 안 됨)

- **중앙 비소유** — 중앙엔 *목차*(KnowledgeIndex·본문 0)만. dedup/병합이 raw·body·토큰을 중앙에 흘리면 안 된다. near-dup 임베딩 유사도는 owner측에서만 돈다.
- **Authority 중앙** — over-claim 이중 필터(`admit_okf` + `filter_authorized_concepts`)는 권위 게이트로 유지. 병합/dedup은 권한을 넓히지 않는다.
- **미아 없음** — 0 매칭 → 루트 escalation. 삭제/병합이 라우팅 종착을 안 깬다.
- **owner OAuth · 공급자 중립 · 1인칭 · 전이≠기록** — dedup 임베딩 모델은 owner측·중앙 비의존. 커밋(기록)과 처분(전이) 분리.
- **발화 머신별 단일(ADR 0030)** — owner commit이 reindex만·중앙 index 수용이 reeval만. 이 ADR의 인덱스 도출 변경이 그 분리를 안 흔든다.

---

## B — 멱등 병합·증분·삭제 토폴로지

### 결정 B1: publish 인덱스 도출을 "이번 admitted draft" → **"owner OKF 본체 디렉터리 전체"**로 바꾼다 (후보 i 채택)

`/author/publish`가 인덱스를 **격리 임시 디렉터리 + 이번 admitted draft**에서 도출하던 것을, **커밋된 owner OKF 본체 디렉터리 전체**(`okf_root/{agent_id}/*.md`)에서 도출하도록 바꾼다. `commit_okf_bundle`이 이미 working tree에 누적했으므로, 커밋 *직후* 그 디렉터리 전체를 `build_knowledge_index_from_okf`로 읽으면 **이번 문서 + 기존 문서가 다 든 완전한 인덱스**가 나온다.

```
commit_okf_bundle(_git_gateway)                         # owner git working tree에 누적 쓰기
  → okf_dir = gateway.bundle_dir(agent_id)  (또는 head_sha 스냅샷 추출)
  → index = build_knowledge_index_from_okf(card, okf_root, generated_at=...)   # 전체 glob → 완전 인덱스
  → accept_published_index(... index ...)               # store.put: 완전 인덱스로 교체 → 누적 보존
```

**왜 이게 정답인가 (후보 비교)**:

| 후보 | 멱등(같은 concept_id 덮어쓰기) | 증분 누적(새 .md) | 새 코드 | 비소유 | 결함 |
|------|------|------|------|------|------|
| **(i) owner 번들 전체에서 도출** ✅ | git working tree가 같은 경로 덮어씀(파일 단위 멱등) | 새 파일 추가 → 전체 glob에 자동 합류 | **0**(기존 `build_knowledge_index_from_okf` 재사용·워커 경로와 동일) | OK(목차만 도출) | 디렉터리=진실원천(commit 누적 전제) |
| (ii) prior published index 로드 → concept_id union | prior + 신규 union·신규 승 | union으로 누적 | store에 read-merge 신규 코드·concept_id 충돌 정책 | OK | 중앙 store가 병합 책임을 짐(비소유 정신에 거슬림 — 중앙은 staleness만 봐야) |
| (iii) `reindex_incrementally` 배선 | prior AuthoredOkf 필요(메모리 증분) | OK | `ReindexRequest` 조달(prior·changed_sources) 필요 | OK | publish-시점엔 prior AuthoredOkf가 메모리에 없음(저작-시점 전용·ADR 0030 S3 정정) |

**(i) 채택 근거**:
- **새 도출 코드 0** — `build_knowledge_index_from_okf`(전체 glob)를 그대로 쓴다. 워커 `publish_frames`가 *이미* 이 방식이다. `/author/publish`만 어긋난 임시-디렉터리 경로를 쓰고 있었다 → 워커 경로와 **수렴**시키는 게 정답.
- **멱등은 파일 경로 멱등으로 자연 성립** — 같은 concept_id → 같은 `{concept_id}.md` 경로 → git working tree가 덮어쓴다(파일 단위 멱등). 단 concept_id 안정성이 전제(결정 B2).
- **증분 누적은 glob으로 자연 성립** — 새 `.md`가 디렉터리에 추가되면 다음 도출에서 자동 합류. read-merge 로직 불요.
- **비소유 보존** — 중앙 store는 *완전 인덱스를 받아 교체만* 한다(병합 책임은 owner측 디렉터리·git이 짐). 중앙은 여전히 staleness(`generated_at`)만 본다.

**(ii)는 명시 기각** — 중앙 store에 read-merge 책임을 지우면 "중앙은 목차만·staleness만"이 흔들린다. union 충돌 정책(어느 버전 승)을 중앙이 들어야 하는데, 그건 owner 지식 재조정이지 중앙 routing이 아니다.
**(iii)는 publish-시점 부적합** — `reindex_incrementally`는 prior `AuthoredOkf`(저작-시점 메모리)를 요구하는데 publish-시점엔 그게 없다(커밋-직후 진실 원천은 디스크). ADR 0030 S3가 이미 이 타입 불연속을 못박았다. `reindex_incrementally`는 *저작-시점 메모리 증분*(raw 편집 → 메모리 diff → commit 전)에만 남는다 — 이 ADR이 그 위치를 재확인한다.

**커밋-직후 디렉터리 접근 방법** (`GitGateway` 포트 진화 — Open Question OQ-1): 도출 입력 디렉터리를 두 방식 중 하나로 얻는다 —
- **(a) working tree 직독**: `okf_root/{agent_id}/`를 커밋 직후 그대로 읽는다(워커 `publish_frames`가 이미 이 방식·`okf_root` 주입). 가장 단순·`build_knowledge_index_from_okf`가 이미 디렉터리를 받는다.
- **(b) 커밋 스냅샷 추출**: `gateway.head_sha(agent_id)` → `gateway.extract_snapshot(sha, agent_id, tmp)` → tmp에서 도출(ADR 0018 결정 4 "이 답은 이 커밋 기준" 재현·`Answer.snapshot_sha` 정신).

→ **MVP는 (a) working tree 직독**(워커 경로와 동일·`okf_root` 주입만 추가). (b)는 "이 인덱스는 이 커밋 기준" 감사가 필요해지면 후속. `FakeGitGateway`는 working tree가 없으므로(in-memory 트리) **게이트 내 테스트는 `extract_snapshot`으로 in-memory 트리를 임시 디렉터리에 추출 → 도출** 경로를 써 결정론을 잠근다(실 `SubprocessGitGateway`는 working tree 직독·게이트 밖). 즉 게이트 내는 (b)-스타일 추출, 실 경로는 (a) working tree — 둘 다 같은 `build_knowledge_index_from_okf`로 수렴(도출 단일 권위).

### 결정 B2: concept_id를 **결정론 키**로 안정화한다 — `OkfConceptKey`

LLM 슬러그는 런마다 미세 변동한다(같은 문서 재추출 시 `refund-policy` vs `refund-policy-overview`). 멱등(결정 B1의 파일 경로 멱등)이 성립하려면 같은 개념이 같은 concept_id를 받아야 한다. 두 층으로 안정화한다.

**층 1 — 결정론 슬러그 도출(저작-시점)**: concept_id를 LLM 자유 슬러그가 아니라 **`domain` + `title` 정규화 슬러그**로 결정론 도출한다.

```
OkfConceptKey.derive(domain: str, title: str) -> str
  = slugify(normalize(domain) + "-" + normalize(title))
  normalize: NFKC · 소문자 · 공백→하이픈 · 영숫자/하이픈 외 제거 · 연속 하이픈 축약 · 길이 캡
```

- LLM은 여전히 `title`·`domain`을 자유롭게 내되, **concept_id는 그 둘에서 결정론으로 파생**한다(LLM이 concept_id를 직접 내지 않는다). 같은 (domain, title) → 같은 concept_id → 같은 `{concept_id}.md` 경로 → git 덮어쓰기(멱등).
- 충돌(서로 다른 개념이 같은 키) 처리: 같은 (domain, title)이면 *같은 개념으로 간주해 덮어쓴다*(의도된 멱등). title이 미세하게 다르면 다른 키 → 다른 파일(결정 B3의 near-dup이 그 "비슷한데 다른" 경우를 다룬다).
- 기존 `validate_safe_path_component`(agent_card.py)를 그대로 통과(경로 안전 superset 유지).

**층 2 — 병합 단계 정규화 매칭(증분-시점)**: `reindex_incrementally`/저작면이 prior와 신규를 합칠 때, concept_id를 정규화해 비교한다(이미 `reindex_incrementally`가 concept_id 키 병합을 한다 — 정규화 키로 매칭하면 미세 변동을 흡수).

**왜 content-hash 키가 아닌가** (기각): content 기반 concept_id(body 해시)는 "1글자만 바뀐 같은 개념"이 다른 키가 돼 멱등을 깬다(ADR 0029 T11.6이 source_id를 content 해시로 안 만든 같은 이유). (domain, title)은 개념의 *정체성*에 가깝고 본문 미세 변경에 안정적이다.

**LLM 슬러그를 받되 매칭하는 대안과의 트레이드오프**: LLM 슬러그를 그대로 받고 병합 단계에서만 정규화/매칭하는 방법도 가능하나(층 2만), 그건 *런마다 다른 파일*이 git에 쌓이는 걸 못 막는다(`refund-v1.md`·`refund-v2.md` 누적). 층 1(결정론 도출)이 **파일 경로 자체를 안정화**해 git 누적 오염을 원천 차단하므로 층 1을 1급으로 둔다. 층 2는 layer-1 도입 전 기존 파일·외부 유입 슬러그를 흡수하는 보강.

### 결정 B3: 명시 삭제 — `OkfTombstone` 도메인 연산

교체 모델에선 "빼고 재저작"하면 개념이 사라졌지만, 누적 병합 모델(결정 B1)에선 새 publish가 *덮거나 더할* 뿐 **빼지 못한다**(과거 .md가 디렉터리에 남아 계속 인덱스에 합류). 따라서 owner가 개념을 제거하는 **명시 삭제 도메인 연산**이 필요하다.

**삭제 토폴로지 = 물리 삭제 + 발화 단일 보존**:
- **owner측 = 물리 삭제 커밋**: owner가 개념 삭제를 명령하면 `okf_root/{agent_id}/{concept_id}.md`를 **삭제하는 커밋**을 만든다(`commit_okf_bundle`을 *삭제 의도*로 확장 — `OkfFile` 추가만이 아니라 *제거 목록*도 받는다). 커밋 후 디렉터리 전체 재도출(결정 B1) → 그 개념이 인덱스에서 빠진다(자연 전파).
- **중앙측 = 완전 인덱스 교체로 자연 반영**: 삭제 커밋 후 도출한 인덱스에는 그 개념이 없다. `accept_published_index`의 `store.put`이 더 새 `generated_at`으로 교체 → 중앙 목차에서 그 개념이 빠진다. **중앙은 삭제를 따로 모른다**(완전 인덱스 교체가 곧 삭제 반영 — 중앙 비소유 유지·삭제 신호를 중앙에 안 보냄).

**tombstone vs 물리 삭제**:
- **MVP는 물리 삭제 채택**(git이 *이력*을 보존하므로 tombstone 불요 — `git log`·`git show`로 삭제된 개념을 되살릴 수 있다). 별도 tombstone 레코드는 중앙 store에 죽은 개념 메타를 남겨 비소유·목차 청결을 해친다.
- **`OkfTombstone` 값 객체는 *도메인 연산의 표현*으로만 둔다**(중앙 영속 레코드 아님) — `OkfTombstone(agent_id, concept_id, reason)`은 삭제 *명령*을 표현하고, 삭제 커밋 + 재도출로 소비된 뒤 사라진다. ADR 0019 staleness와 짝: 삭제도 변경이라 **그 개념에 기댄 과거 Precedent·답을 reeval**해야 한다(삭제 = 가장 강한 staleness — 그 지식이 *없어졌다*). reeval 트리거는 ADR 0030의 인덱스-수용 훅이 자연 처리(완전 인덱스가 더 새 `generated_at`으로 수용 → `on_okf_committed` 발화 → 그 agent 과거 판례 보수적 재적재).

**미아 없음 영향 (검증)**: 마지막 개념을 삭제해 인덱스가 빈 concepts가 되면, stage-1이 0 후보 → `Unowned` → root escalation(미아 없음 보존). 삭제가 라우팅 종착을 안 깬다 — 빈 인덱스는 "0 후보"로 자연 처리되고 미아가 아니라 escalation으로 종착한다(`filter_authorized_concepts`가 빈 concepts 인덱스를 보관하는 정신과 동일).

**과거 답 staleness**: 삭제된 개념에 기댄 과거 답은 reeval 큐로 가서 owner가 "이 답은 더 이상 유효한 지식 없음"을 처리한다(ADR 0019 1인칭 처분). 자동 무효화는 안 한다(owner 검토 — 과병합·과삭제 손실 위험과 같은 보수성).

---

## C — 의미 기반 near-dup (임베딩) · 후속

### 결정 C1: near-dup 탐지·병합은 **owner측 저작 단계**에서 돈다 (중앙 0)

concept_id가 다른데(결정 B2의 결정론 키로도 안 잡히는 — title이 "환불 정책" vs "반품·환불 안내"처럼 *의미는 같고 표현이 다른*) 의미가 같거나 유사한 개념을 탐지·병합한다. 임베딩 유사도가 필요하다.

**위치 = owner측 저작면(중앙 아님)**. 근거:
- raw·body·임베딩 입력이 모두 owner측이다(비소유). near-dup 판정은 개념 *본문·title*을 임베딩해야 하는데, 그건 owner OKF 본체라 중앙에 없다(중앙은 목차만). 따라서 dedup은 **owner 자기 지식 재조정**이지 중앙 routing이 아니다.
- 중앙 staleness/scoping은 무변경 — dedup이 끝난 *완전 인덱스*만 중앙이 받는다(결정 B1 경로 그대로). 중앙은 near-dup을 모른다.

### 결정 C2: 임베딩 모델 = **로컬·다국어·중앙 토큰 0**

ADR 0028 §7 "스케일 어댑터 = 로컬 임베딩 ANN"·grill-me 세션(임베딩 모델 선택: 로컬·다국어·중앙 토큰 0)과 합류한다.
- **로컬 임베딩 모델**(외부 API 0·중앙 토큰 0) — owner 환경에서 돈다. 한국어 포함 다국어(우리 도메인 텍스트가 한국어).
- **T10.5 `EmbeddingAnnMatcher`와 같은 임베딩 인프라를 공유**(ADR 0028 §7·게이트 밖·deferred) — routing용 임베딩 ANN과 dedup용 임베딩이 *같은 로컬 모델·같은 의존성*을 쓴다(어댑터 추가 0). dedup이 T10.5 임베딩 도입의 *첫 사용처*가 될 수 있다(routing ANN보다 작은 입력·owner측 한정).
- **포트 뒤**(코어 결합 0) — `KnowledgeIndexMatcher`의 `EmbeddingAnnMatcher`가 확장 어댑터인 정신. dedup도 포트(`ConceptDeduplicator` 또는 임베딩 유사도 포트) 뒤에 둬 코어가 임베딩 라이브러리에 안 묶인다.

### 결정 C3: 보수성 — 자동 병합 말고 **owner 확인**(과병합=지식 손실)

애매한 near-dup은 **자동 병합하지 않고 owner에게 후보를 보여 확인받는다**.
- 유사도 임계 ≥ τ_high(거의 동일·예: 0.95+)면 자동 병합 후보 *제안*(여전히 owner 1클릭 승인)·τ_low ≤ 유사도 < τ_high면 "비슷한 개념이 있습니다" 후보만 표시·< τ_low면 무시.
- **자동 병합 금지 근거**: 과병합은 *지식 손실*이다(서로 다른 미묘한 개념을 하나로 뭉개면 라우팅 정밀도·답 품질 손실). owner가 "이 둘은 같다/다르다"를 정한다(1인칭 처분·ADR 0025 HITL 정신·결정 B3 삭제 보수성과 같은 결).
- 임계값(τ_high·τ_low)은 **주입 정책값**(`clear_winner_margin`·`staleness_threshold` 주입 정신·카드 자기보고 아님). 게이트 내 결정론 단언은 임계 주입·**Fake 임베딩**(고정 벡터/유사도 주입)으로 검증·실 임베딩 모델은 게이트 밖.

### 결정 C4: 병합 연산 = 결정 B2·B3 재사용

near-dup 병합은 새 토폴로지가 아니라 결정 B2(concept_id 정규화 매칭)·B3(삭제) 위에 얹는다 — owner가 "A와 B는 같다"를 확정하면 ① 둘을 합친 개념을 한 concept_id로 쓰고(결정 B1 멱등 쓰기) ② 다른 하나를 삭제(결정 B3 tombstone 커밋). 즉 dedup = "유사 후보 탐지(C·임베딩) + owner 처분(C3) + 병합 쓰기·삭제(B2·B3 재사용)". 병합 자체는 B의 기계가 처리한다.

---

## 불변식 영향 종합

| 불변식 | B(누적·키·삭제) 영향 | C(near-dup) 영향 |
|--------|------|------|
| 중앙 비소유(목차만) | 보존 — 중앙은 완전 인덱스를 받아 교체만(병합·삭제는 owner 디렉터리·git). 삭제 신호 중앙 미도달 | 보존·강화 — 임베딩 입력(body·title)은 owner측·중앙 0. dedup 끝난 목차만 도달 |
| Authority 중앙 | 보존 — `admit_okf`+`filter_authorized_concepts` 이중 필터 무변경. 누적·삭제가 권한 안 넓힘 | 보존 — 병합이 권한 안 넓힘(병합 개념도 admit_okf 재통과) |
| 미아 없음 | 보존 — 빈 인덱스(전 개념 삭제)도 0 후보→Unowned→root escalation | 보존 — 병합이 후보를 줄여도 0이면 escalation |
| owner OAuth·공급자 중립 | 보존 — 추출은 owner OAuth·도출은 결정론 | 보존 — 임베딩 모델 owner측·로컬·공급자 중립 |
| 전이≠기록 | 보존 — 커밋(기록)·삭제 커밋(기록) ↔ owner 처분(전이) 분리 | 보존 — near-dup 처분(전이)·병합 커밋(기록) 분리 |
| 발화 머신별 단일(0030) | 보존 — owner commit이 reindex만(누적·삭제 둘 다 commit)·중앙 수용이 reeval만 | 보존 — dedup은 commit 전 저작-시점·발화점 무신설 |

---

## Open Questions / 게이트 밖

- **OQ-1 (B·결정 B1)**: 커밋-직후 인덱스 도출 입력 디렉터리 = working tree 직독(a) vs 커밋 스냅샷 추출(b). MVP는 (a)(워커 경로 동일). "이 인덱스는 이 커밋 기준" 감사가 필요해지면 (b)로(ADR 0018 결정 4 정신·`KnowledgeIndex`에 `snapshot_sha` 추가 여부). `GitGateway` 포트에 `bundle_dir(agent_id)` 또는 도출용 디렉터리 노출 메서드를 더할지(현재 `extract_snapshot`만 디렉터리를 냄) — tdd/mcp-runtime이 더 작은 쪽 선택.
- **OQ-2 (B·결정 B2)**: `OkfConceptKey.derive`의 정규화 규칙 세부(길이 캡·유니코드 정규화 형식·동일 (domain,title)이 다른 개념일 충돌 빈도). 실 추출 골든셋으로 키 안정성·충돌율 관측 후 확정.
- **OQ-3 (B·결정 B3)**: ~~삭제 = `commit_okf_bundle` 확장(제거 목록 인자)의 시그니처.~~ **decided (2026-07-01) — `removed_paths: tuple[str, ...] = ()` on `CommitRequest`/`BuilderCommitRequest`**. 별도 `delete_okf_concept` 연산이 아니라 *기존 커밋 시그니처에 삭제 목록 필드를 더하는* 최소 확장을 택했다(새 값객체·새 연산 0 — 추가·삭제·편집이 모두 한 커밋 경로로 수렴). 근거: ① `commit_okf_bundle`이 이미 단일 커밋 오케스트레이터라 삭제도 같은 닫힌 루프(쓰기+커밋)에 자연 합류한다 ② `files`만 들던 추가 전용 커밋은 빈 튜플 기본값으로 *완전 하위호환*(기존 호출 무영향) ③ 편집(같은 경로 덮어쓰기)·삭제(`files=()`+`removed_paths`)·삭제+추가가 모두 한 `CommitRequest`로 표현된다 — 별도 연산이면 트랜잭션 경계가 둘로 갈린다. 경로 안전은 `validate_removed_paths`(`validate_okf_paths`와 같은 규칙)로 `FakeGitGateway`·`SubprocessGitGateway` 동일 강제. `FakeGitGateway`는 working tree에서 path 제거 후 새 스냅샷, `SubprocessGitGateway`는 `git rm -f --ignore-unmatch`(없는 path idempotent). `OkfTombstone` 값 객체는 *만들지 않았다* — 삭제 명령이 `removed_paths` 한 필드로 충분히 표현돼 새 도메인 타입이 과설계였다(결정 B3의 "tombstone은 도메인 연산 표현으로만·중앙 영속 0" 정신을 *필드*로 더 가볍게 실현). 구현: `git_gateway.py`(`removed_paths`·`validate_removed_paths`), web의 `DELETE /author/concept/{agent_id}/{concept_id}`(삭제 커밋→인덱스 재도출).
- **OQ-4 (C·결정 C2)**: 로컬 다국어 임베딩 모델 선택(구체 모델명·크기·한국어 품질)·새 의존성(되돌리기 어려움) — T10.5와 합류해 한 번에. dedup이 T10.5 임베딩 첫 사용처가 될지(routing ANN 먼저냐 dedup 먼저냐).
- **OQ-5 (C·결정 C3)**: τ_high·τ_low 임계 실값·UX(자동 제안 vs 후보 목록만). 실 임베딩 관측 후.
- **OQ-6 (B·전역)**: 기존에 *교체 모델로 publish된* 인덱스(런마다 다른 슬러그가 쌓인 owner 디렉터리)의 마이그레이션 — layer-1 결정론 키 도입 시 기존 파일 재키잉(rekeying)이 필요한지. owner OKF 디렉터리가 아직 데모뿐이라 MVP는 무시 가능(실 운영 owner 생기면 재검토).

---

## 결정 요약 (5줄)

1. **B1**: publish 인덱스 도출을 "이번 admitted draft+임시 디렉터리" → **owner OKF 본체 디렉터리 전체 glob**(워커 `publish_frames`와 수렴·새 도출 코드 0·commit 누적이 곧 멱등 누적).
2. **B2**: concept_id를 LLM 자유 슬러그 → **`(domain,title)` 결정론 키(`OkfConceptKey`)** + 병합 정규화 매칭(같은 개념=같은 파일 경로=git 덮어쓰기 멱등).
3. **B3**: 명시 삭제 = **물리 삭제 커밋**(git 이력이 보존·중앙은 완전 인덱스 교체로 자연 반영·삭제도 staleness reeval) — tombstone은 도메인 연산 표현으로만(중앙 영속 0). **구현(OQ-3 decided)**: 별도 `OkfTombstone` 값객체 없이 `CommitRequest.removed_paths: tuple[str,...] = ()` 한 필드로 삭제 명령을 실현(추가·편집·삭제가 한 커밋 경로로 수렴·완전 하위호환). `DELETE /author/concept/{agent_id}/{concept_id}`가 삭제 커밋→인덱스 재도출로 owner 자기 개념 삭제를 제공한다.
4. **C1·C2**: 의미 near-dup 탐지·병합은 **owner측 저작 단계·로컬 다국어 임베딩(중앙 토큰 0·T10.5 인프라 공유·포트 뒤)** — 중앙 무변경.
5. **C3·C4**: **자동 병합 금지·owner 확인 후보 제안**(과병합=지식 손실·1인칭 처분), 병합 연산은 B2·B3 기계 재사용.
