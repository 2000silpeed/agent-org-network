# 멱등 OKF publish — 누적 인덱스 도출·concept_id 안정성·명시 삭제 + 의미 기반 near-dup

상태: **구현 완료 (2026-07-01) — B·C 전부(B1·B2·B3·C1-C4) 구현·실 fastembed E2E 검증 완료** · **범위 B+C 사용자 승인(grill 합의)** · **ADR 0028(published index·scale routing)·0029(OKF 자동 저작)·0030(owner측 저작 토폴로지·크로스머신 fan-out)의 후속(supersede 아님 — refine)** · ADR 0018(OKF 번들 git 저장·빌더 커밋)·0019(신선도·변경 전파)·0013(OKF)·0004(Authority 중앙)와 정합 · **B는 게이트 내 결정론(T11.8a·T11.8b)·C는 순수 도메인 게이트 내 + 실 임베딩 어댑터 게이트 밖(T11.8d, `fastembed`+`intfloat/multilingual-e5-small`)** · CONTEXT·tasks-v0 갱신 완료(`OkfConceptKey`·`Embedder`·`DedupCandidate` 반영) · 남은 미검증 영역은 OQ-5(임계값 실값)뿐 — 실 owner 골든셋 운영 데이터로 후속 조정

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

## C — 구현 계약 (T11.8d로 구현 완료 — 시그니처는 실 코드와 일치)

이 절은 tdd-engineer(Fake·순수 함수·값 객체)·mcp-runtime-engineer(실 어댑터·라우트 배선)가 **그대로 구현만 하면 되게** 타입·포트·함수·엔드포인트 계약을 못박는다. 코드는 아직 작성하지 않았다(도메인 설계 산출). 책임 분담: **순수 도메인(값 객체·`Embedder` 포트·`classify_dedup_candidates`)은 tdd-engineer**, **실 어댑터(`FastEmbedEmbedder`)·신규 라우트 배선은 mcp-runtime-engineer**.

### 배치(새 모듈) — `src/agent_org_network/okf_dedup.py`

`okf_authoring.py`는 이미 1033줄이라 dedup 도메인을 새 모듈로 분리한다. 의존 방향: `okf_dedup`은 순수(IO 0·SDK 0)·`pydantic`만 임포트하고 `okf_authoring`을 *역참조하지 않는다*(`OkfDocumentDraft`를 받아 쓰지도 않는다 — 입력은 *이미 추출된 벡터*와 식별자뿐이라 결합이 0이다). 실 어댑터 `FastEmbedEmbedder`는 별도 어댑터 파일(예: `provider_embed_fastembed.py` — mcp-runtime이 위치 결정)에 두고 `fastembed` extra가 있을 때만 임포트한다(공급자 SDK extra 격리 패턴).

### 포트 — `Embedder` (Protocol·순수 인터페이스)

```python
class Embedder(Protocol):
    """텍스트 → 임베딩 벡터 포트 — Classifier·KnowledgeIndexMatcher·OkfAuthor 포트 동일 패턴.

    구현: FakeEmbedder(테스트·고정 벡터 주입)·FastEmbedEmbedder(실·게이트 밖).
    벡터는 L2 정규화돼 있다고 *가정*(어댑터 책임) — 그래야 dot product가 곧 cosine.
    e5 prefix("query: ")·풀링·정규화는 실 어댑터 내부 책임이고, 이 포트 계약은 그걸 모른다.
    """

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """texts 각 원소를 임베딩 벡터로 변환. 입력 순서 보존·같은 차원.
        빈 시퀀스는 빈 튜플 반환. 차원 일관성(모든 벡터 같은 길이)은 구현 보장."""
        ...
```

- 시그니처가 `Sequence[str] -> tuple[tuple[float, ...], ...]`인 이유: 순수·해시 불가능 회피·배치 임베딩(어댑터가 한 번에 N개 인코딩)·결정론 테스트(FakeEmbedder가 텍스트→고정 벡터 dict 주입). `numpy` 타입을 포트에 노출하지 않는다(코어 결합 0 — 어댑터가 numpy→tuple 변환).

### 값 객체 — `DedupCandidate` (pydantic frozen)

```python
class DedupCandidate(BaseModel, frozen=True):
    """near-dup 후보 한 쌍 — owner 처분(C3) 입력. 중앙 미도달(owner측 transient).

    new_concept_id: 이번 /author/run으로 새로 추출된(아직 미게시) 개념 id.
    existing_concept_id: 이미 게시된 라이브러리의 기존 개념 id.
    similarity: cosine 유사도(0.0~1.0). 범위 밖 거부.
    grade: 'auto_suggest'(sim≥τ_high·자동 병합 후보 제안)
           | 'similar'(τ_low≤sim<τ_high·"비슷한 개념" 표시만). C3 두 등급.
    """

    new_concept_id: str
    existing_concept_id: str
    similarity: float
    grade: Literal["auto_suggest", "similar"]

    @field_validator("new_concept_id", "existing_concept_id")
    @classmethod
    def _validate_ids(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("DedupCandidate concept_id는 빈 문자열/공백이 될 수 없습니다.")
        return value

    @field_validator("similarity")
    @classmethod
    def _validate_similarity(cls, value: float) -> float:
        if not (0.0 <= value <= 1.0):
            raise ValueError(f"similarity는 0.0~1.0 범위여야 합니다: {value}")
        return value
```

- `grade`는 sealed 두 값(`auto_suggest`·`similar`)으로 C3의 두 단계를 망라한다. `<τ_low`는 후보가 *아니므로* `DedupCandidate`로 만들지 않는다(등급 셋이 아니라 둘 — 무시 케이스는 객체 부재로 표현). `Literal`로 제3의 등급 유입을 컴파일타임 차단.

### 순수 함수 — `classify_dedup_candidates`

```python
def classify_dedup_candidates(
    new_concepts: Sequence[tuple[str, tuple[float, ...]]],
    existing_concepts: Sequence[tuple[str, tuple[float, ...]]],
    *,
    tau_high: float,
    tau_low: float,
) -> tuple[DedupCandidate, ...]:
    """신규 추출분 × 기존 게시 라이브러리의 pairwise cosine으로 near-dup 후보 분류.

    순수·IO 0·결정론. *임베딩을 직접 계산하지 않는다* — 호출자가 Embedder.embed로 미리
    벡터를 낸 뒤 (concept_id, vector) 쌍으로 넘긴다. 이 함수는 cosine 계산·임계 분류만.

    입력 (concept_id, vector): new는 이번 /author/run 미게시 개념, existing은 게시 라이브러리.
    cosine(u, v) = dot(u, v) / (||u|| * ||v||). 0벡터·차원 불일치는 ValueError(fail-loud).
    같은 concept_id(new와 existing이 우연히 같은 id)는 자기쌍이라 후보에서 제외한다
    (결정 B2 결정론 키로 이미 같은 파일=멱등 덮어쓰기 경로라 dedup 대상 아님).

    분류:
      sim >= tau_high → DedupCandidate(grade='auto_suggest')
      tau_low <= sim < tau_high → DedupCandidate(grade='similar')
      sim < tau_low → 후보 아님(객체 생성 안 함).

    결과 정렬: similarity 내림차순, 동점은 (new_concept_id, existing_concept_id) 오름차순
    (결정론 — KnowledgeIndexMatcher.match 정렬 규약과 같은 결).

    tau_high·tau_low는 *주입 정책값*(결정 C3·OQ-5 — 하드코딩 금지·카드 자기보고 아님).
    호출자가 OQ-5 기본값(0.88·0.70)을 주입하되, 이 함수는 어떤 값이든 받는다.
    전제: tau_low <= tau_high(역전 시 'similar' 구간이 빔 — 호출자 책임, 함수는 거부 안 함).
    """
    ...
```

- **순수·결정론·IO 0** — 벡터를 받아 cosine·분류만. 게이트 내 단언은 `FakeEmbedder` 고정 벡터 + 임계 주입으로 결정론(결정 C3 "Fake 임베딩으로 검증·실 모델은 게이트 밖").
- **비교 대상 정의(ADR 124~125행 의도 고정)**: `new_concepts`=이번 `/author/run`이 낸 미게시 staged 개념, `existing_concepts`=이미 게시된 owner 라이브러리(아래 엔드포인트 계약이 둘을 조달). "재추출 시 기존과 겹치는가"를 본다.

### 신규 엔드포인트 계약 — `POST /author/dedup/{agent_id}` (탐지 전용·쓰기 0)

dedup 슬라이스가 내야 할 *유일한 새 쓰기 경로는 후보 탐지뿐*이다(결정 C4 — 병합 *실행*은 기존 PUT/DELETE 재사용). 이 라우트는 **읽기 전용**(중앙 store 무변경·owner git 무변경)이라 GET이 자연스러우나, 신규 추출분 staged 개념(본문 다수)을 body로 받아야 하므로 `POST`로 둔다(쓰기 0·idempotent). 실 라우트 구현은 **mcp-runtime-engineer**.

요청 바디(`AuthorDedupRequest`):
```python
class AuthorDedupRequest(BaseModel):
    """POST /author/dedup/{agent_id} 요청 — 신규 추출 staged 개념 vs 게시 라이브러리 near-dup.

    concepts: 이번 /author/run이 낸 미게시 staged 개념(아직 commit 안 됨). 각 원소는
              /author/run 응답 concepts 원소와 같은 모양(concept_id·title·core_question·
              body·domain·type). 임베딩 입력 텍스트는 이 본문이라 owner측에서만 계산된다.
    """
    concepts: list[AuthorDedupConcept]


class AuthorDedupConcept(BaseModel):
    concept_id: str
    title: str
    core_question: str
    body: str
    domain: str
    type: str | None = None
```

라우트 흐름(핸들러 의사코드 — mcp-runtime이 배선):
```
card = _author_scoped_card(agent_id, request)        # owner 스코프 가드(401/404/403) 재사용
existing_docs = _read_all_concept_docs(card)         # 게시 라이브러리 전체를 owner측에서 읽기
    # head_sha → extract_snapshot → 디렉터리 glob → 각 *.md를 OkfDocumentDraft로 역parse.
    # 게시 인덱스 없으면(store None·미게시) 빈 리스트 → 후보 0(미아 아님).
embed_text(c) = f"{c.title}\n{c.core_question}\n{c.body}"   # 임베딩 입력 합성(owner측·중앙 0)
embedder = select_embedder()                         # env(AON_EMBEDDER) 분기·기본 Fake/미설정 무력
new_vecs = embedder.embed([embed_text(c) for c in req.concepts])
existing_vecs = embedder.embed([embed_text(d) for d in existing_docs])
candidates = classify_dedup_candidates(
    new_concepts=list(zip([c.concept_id for c in req.concepts], new_vecs)),
    existing_concepts=list(zip([d.concept_id for d in existing_docs], existing_vecs)),
    tau_high=DEDUP_TAU_HIGH, tau_low=DEDUP_TAU_LOW,   # OQ-5 주입값(상수/설정에서·하드코딩 한 곳)
)
return {"candidates": [serialize(c) for c in candidates]}
```

응답:
```json
{"candidates": [
  {"new_concept_id": "...", "existing_concept_id": "...",
   "similarity": 0.91, "grade": "auto_suggest"}
]}
```

- **`select_embedder()` 시임**(`select_author`·`select_runtime` 정신): env(`AON_EMBEDDER`)로 분기 — 미설정/`demo`면 `FakeEmbedder`(또는 임베딩 비활성 → 빈 후보), `fastembed`면 실 `FastEmbedEmbedder`. 기본 경로는 임베딩 의존성 없이도 빈 후보로 통과(extra 미설치 owner 무영향).
- **임계 주입처**: `DEDUP_TAU_HIGH=0.88`·`DEDUP_TAU_LOW=0.70`을 *한 곳*(모듈 상수 또는 설정)에 두고 라우트가 주입한다. OQ-5 갱신 시 이 한 곳만 바뀐다(하드코딩 분산 금지·결정 C3 주입 정책값).
- **병합 실행은 이 라우트가 안 한다**: owner가 후보를 보고 "같다"를 확정하면 프론트가 ① 남길 개념 `PUT /author/concept/{agent_id}/{concept_id}`(병합 본문 갱신·admit_okf 재검증) ② 버릴 개념 `DELETE /author/concept/{agent_id}/{concept_id}`(삭제 커밋) — *기존 T11.8c 라우트 재사용*(web.py:1779·1855). 새 병합 도메인 연산 0(결정 C4).

### 불변식 재확인(설계로 보장)

- **중앙 비소유**: 임베딩 입력(title·core_question·body)은 `/author/dedup`이 owner측 staged 개념·owner git 번들에서만 읽고, 임베딩·cosine·후보 분류가 모두 owner 프로세스에서 돈다. 응답(`candidates`)은 concept_id·유사도·등급뿐(본문 0)이고 *어떤 중앙 store에도 쓰지 않는다*(`/author/run`과 같은 transient·읽기 전용). 중앙 `published_index_store`는 dedup을 모른다.
- **Authority 중앙**: dedup 라우트는 *권한을 안 만진다*(읽기만). 병합 *실행*은 기존 PUT 경로라 그 안의 `admit_okf`(domain∈card.domains 재검증)를 그대로 통과한다 — 병합이 권한을 넓히는 새 우회 경로가 없다(PUT/DELETE가 유일한 쓰기 경로·둘 다 admit 게이트 뒤).
- **미아 없음**: 게시 라이브러리가 비면(`existing_docs=[]`) 후보 0(빈 리스트) — escalation·라우팅 종착 불변. dedup이 0 후보를 미아로 만들지 않는다.
- **전이≠기록**: near-dup 후보 *탐지*(전이 후보 제시)와 병합 *커밋*(PUT/DELETE 기록)이 분리(탐지는 transient·기록은 git 커밋).

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
- **OQ-2 (B·결정 B2)**: ~~`OkfConceptKey.derive`의 정규화 규칙 세부(길이 캡·유니코드 정규화 형식·동일 (domain,title)이 다른 개념일 충돌 빈도).~~ **decided (2026-07-01) — `okf_authoring.derive_concept_key(domain, title)`(T11.8b)**. NFKC 정규화→소문자→공백을 하이픈으로→영숫자/하이픈 외 제거→연속 하이픈 축약→80자 캡. 정규화 후 빈 문자열(한글뿐인 입력)은 `(domain,title)` sha256 해시 16자로 폴백(`concept-<hex16>`). 동일 (domain,title) 충돌은 *의도된 멱등*(같은 개념으로 간주해 덮어쓴다) — 다른 개념인데 키가 같아지는 실제 빈도는 실 추출 골든셋 운영 데이터 없이는 여전히 미관측(80자 truncation 충돌 가능성 포함, 실 owner 트래픽이 쌓이면 재관측). `parse_split_response`가 LLM concept_id를 무시하고 이 함수로 덮어쓴다. `reindex_incrementally` 병합 비교는 `_concept_match_key`(같은 정규화 규칙)로 층2 보강.
- **OQ-3 (B·결정 B3)**: ~~삭제 = `commit_okf_bundle` 확장(제거 목록 인자)의 시그니처.~~ **decided (2026-07-01) — `removed_paths: tuple[str, ...] = ()` on `CommitRequest`/`BuilderCommitRequest`**. 별도 `delete_okf_concept` 연산이 아니라 *기존 커밋 시그니처에 삭제 목록 필드를 더하는* 최소 확장을 택했다(새 값객체·새 연산 0 — 추가·삭제·편집이 모두 한 커밋 경로로 수렴). 근거: ① `commit_okf_bundle`이 이미 단일 커밋 오케스트레이터라 삭제도 같은 닫힌 루프(쓰기+커밋)에 자연 합류한다 ② `files`만 들던 추가 전용 커밋은 빈 튜플 기본값으로 *완전 하위호환*(기존 호출 무영향) ③ 편집(같은 경로 덮어쓰기)·삭제(`files=()`+`removed_paths`)·삭제+추가가 모두 한 `CommitRequest`로 표현된다 — 별도 연산이면 트랜잭션 경계가 둘로 갈린다. 경로 안전은 `validate_removed_paths`(`validate_okf_paths`와 같은 규칙)로 `FakeGitGateway`·`SubprocessGitGateway` 동일 강제. `FakeGitGateway`는 working tree에서 path 제거 후 새 스냅샷, `SubprocessGitGateway`는 `git rm -f --ignore-unmatch`(없는 path idempotent). `OkfTombstone` 값 객체는 *만들지 않았다* — 삭제 명령이 `removed_paths` 한 필드로 충분히 표현돼 새 도메인 타입이 과설계였다(결정 B3의 "tombstone은 도메인 연산 표현으로만·중앙 영속 0" 정신을 *필드*로 더 가볍게 실현). 구현: `git_gateway.py`(`removed_paths`·`validate_removed_paths`), web의 `DELETE /author/concept/{agent_id}/{concept_id}`(삭제 커밋→인덱스 재도출).
- **OQ-4 (C·결정 C2)**: ~~로컬 다국어 임베딩 모델 선택(구체 모델명·크기·한국어 품질)·새 의존성(되돌리기 어려움).~~ **decided (2026-07-01) — `fastembed` + `intfloat/multilingual-e5-small`(ONNX·다국어·torch 없음), `dedup` extra로 격리.** 아래 [C2 임베딩 모델 선택 근거](#oq-4-임베딩-모델라이브러리-선택-근거-decided-2026-07-01) 참조. dedup이 임베딩의 첫 사용처다(routing ANN보다 입력이 작고 owner측 한정 — T10.5 `EmbeddingAnnMatcher`가 같은 `Embedder` 포트·같은 의존성을 재사용하게 모양을 잡는다).
- **OQ-5 (C·결정 C3)**: ~~τ_high·τ_low 임계 실값·UX(자동 제안 vs 후보 목록만).~~ **decided (2026-07-01) — τ_high=0.88·τ_low=0.70(cosine), e5 instruct prefix 전제·주입 정책값(하드코딩 금지).** 아래 [C3 임계값 근거](#oq-5-τ_highτ_low-임계값-근거-decided-2026-07-01) 참조.

---

## OQ-4 임베딩 모델·라이브러리 선택 근거 (decided 2026-07-01)

결정 C2의 제약("로컬·다국어·중앙 토큰 0·owner측 온디맨드 단발 호출·한국어 위주·CI 가동시간 민감") 하에서 두 후보를 비교했다.

| 축 | `sentence-transformers` + `paraphrase-multilingual-MiniLM-L12-v2` | **`fastembed` + `intfloat/multilingual-e5-small`** ✅ |
|----|------|------|
| 런타임 의존 | torch + transformers(설치 수백 MB~1GB+·플랫폼별 wheel) | onnxruntime + numpy + tokenizers(수십 MB) |
| 다국어/한국어 | 검증됨(50+ 언어·STS 학습) | 검증됨(e5는 100언어·MTEB 다국어 상위·한국어 포함) |
| STS(의미 유사도) 적합 | 직접 타깃(paraphrase 학습) | retrieval 학습이나 STS cosine에 실무 통용(prefix 전제) |
| CI/온디맨드 가동시간 | torch import·모델 로드가 무겁다(콜드스타트 수 초~) | ONNX 그래프 로드가 가볍다(콜드스타트 짧음) |
| 생태계/문서 | 가장 풍부(표준) | 작지만 owner-로컬 단발 용도에 충분 |
| 새 의존성 회수성 | 무거운 torch가 의존 트리에 박힘 | 가벼움·extra 격리로 미설치 owner 무영향 |

**`fastembed` + `intfloat/multilingual-e5-small` 채택 근거**:
- 이 프로젝트의 사용 패턴은 **owner측 로컬·온디맨드 단발 호출**(저작 시점에 신규 개념 수십 개 × 기존 개념 수백 개 임베딩)이지, 상시 서빙 대용량 추론이 아니다. torch의 무게(설치 용량·콜드스타트·플랫폼 wheel 매트릭스)는 이 용도에 과하다.
- **CI 가동시간 민감** — pyproject가 이미 공급자 SDK를 `optional-dependencies` extra로 격리하는 패턴(`claude-api`·`codex`)을 쓴다. dedup 임베딩도 **`dedup` extra**로 격리해 코어·다른 owner·기본 CI는 torch/onnxruntime를 안 받는다. 가벼운 ONNX 스택이 extra 설치 시간도 짧다.
- **한국어 품질** — e5-small은 MTEB 다국어 벤치에서 작은 크기 대비 한국어 retrieval/STS가 견고하다. 우리 도메인 텍스트(개념 title·body가 한국어)에 맞는다. paraphrase-MiniLM도 한국어를 커버하나, 모델 교체는 `Embedder` 포트 뒤라 owner가 후속에 바꿀 수 있다(코어 결합 0).
- **e5 prefix 규약 주의(어댑터 책임)**: e5 계열은 입력에 `"query: "` 또는 `"passage: "` prefix를 붙여야 제 성능이 난다. dedup은 *대칭 비교*(두 개념 본문)라 양쪽 모두 `"query: "` prefix를 권고한다. **이 prefix·정규화·풀링은 실 어댑터(`FastEmbedEmbedder`) 내부 책임**이고 순수 분류 함수(`classify_dedup_candidates`)는 *이미 임베딩된 벡터*만 받는다 — 포트가 이 규약을 코어에서 격리한다.

**기존 스택 충돌 검토**: pyproject는 `uv` 기반·코어 의존 6종(fastapi·mcp·pydantic 등)에 torch/numpy 없음. `dedup` extra 추가는 코어·`claude-api`·`codex` extra와 독립이라 충돌 0. `uv.lock`은 extra를 별도 마커로 잠그므로 미선택 owner의 resolve에 영향 없다.

```toml
# pyproject.toml [project.optional-dependencies]에 추가(실 배선은 mcp-runtime-engineer):
dedup = ["fastembed>=0.4.0"]   # onnxruntime·tokenizers·numpy를 transitive로 끌어옴(torch 없음)
```

## OQ-5 τ_high·τ_low 임계값 근거 (decided 2026-07-01)

cosine similarity 척도(0~1, 정규화 임베딩 가정)에서 기본값을 정한다. 결정 C3의 두 등급에 매핑한다.

- **τ_high = 0.88** — 이 이상이면 *거의 동일*로 보고 **자동 병합 후보 제안**(`grade="auto_suggest"`·여전히 owner 1클릭 승인).
- **τ_low = 0.70** — `0.70 ≤ sim < 0.88`이면 **"비슷한 개념이 있습니다" 후보 표시만**(`grade="similar"`).
- `sim < 0.70`은 무시(후보 아님).

**근거**:
- 다국어 STS/retrieval 임베딩(e5·paraphrase 계열)에서 *동일 의미 다른 표현*(paraphrase)의 cosine은 통상 **0.85~0.95**, *관련 있으나 다른 개념*은 **0.6~0.8**, *무관*은 **0.3 이하**에 분포한다(MTEB STS 분포·실무 관행). 제안으로 든 0.95는 보수적이나 e5 계열은 **상한이 압축**되는 경향(무관 쌍도 0.7대가 흔함)이라 0.95는 과도하게 빡빡해 실제 paraphrase를 놓친다.
- e5 instruct prefix(`"query: "`)를 붙인 대칭 비교에서 paraphrase 쌍이 **0.88 부근**에 모이는 게 실무 관측이라 τ_high=0.88로 둔다. 과병합 위험(지식 손실)은 owner 확인(C3 자동 병합 금지)이 *최종 안전망*이라, τ_high를 0.95처럼 극단으로 올려 paraphrase를 통째로 놓치는 것보다 0.88로 잡아 owner에게 *보여주는* 쪽이 안전하다(놓침 < 잘못 합침이지만, 여기선 "제안"일 뿐 자동 합치지 않으므로 잡아서 보여주는 비용이 낮다).
- τ_low=0.70은 "비슷하다"는 *약한 신호*의 하한이다. 0.7 미만은 관련성이 약해 owner를 방해하는 노이즈가 된다.
- **이 두 값은 미검증 기본값**(실 e5 임베딩·실 owner 골든셋 관측 전). 그래서 **주입 정책값**으로 설계한다(아래 구현 계약 — `classify_dedup_candidates(..., tau_high, tau_low)` 함수 인자). 카드 자기보고 절대 아님(`clear_winner_margin`·`staleness_threshold` 주입 정신). 실 임베딩 관측 후 이 줄을 갱신한다(임계는 코드 상수가 아니라 호출자 주입이라 ADR 한 줄·주입처 한 줄만 바뀐다).
- **OQ-6 (B·전역)**: 기존에 *교체 모델로 publish된* 인덱스(런마다 다른 슬러그가 쌓인 owner 디렉터리)의 마이그레이션 — layer-1 결정론 키 도입 시 기존 파일 재키잉(rekeying)이 필요한지. owner OKF 디렉터리가 아직 데모뿐이라 MVP는 무시 가능(실 운영 owner 생기면 재검토).

---

## 결정 요약 (5줄)

1. **B1**: publish 인덱스 도출을 "이번 admitted draft+임시 디렉터리" → **owner OKF 본체 디렉터리 전체 glob**(워커 `publish_frames`와 수렴·새 도출 코드 0·commit 누적이 곧 멱등 누적).
2. **B2**: concept_id를 LLM 자유 슬러그 → **`(domain,title)` 결정론 키(`OkfConceptKey`)** + 병합 정규화 매칭(같은 개념=같은 파일 경로=git 덮어쓰기 멱등). **구현(OQ-2 decided·T11.8b)**: `okf_authoring.derive_concept_key`+`parse_split_response` 강제 덮어쓰기(층1)·`reindex_incrementally`의 `_concept_match_key` 정규화 비교(층2).
3. **B3**: 명시 삭제 = **물리 삭제 커밋**(git 이력이 보존·중앙은 완전 인덱스 교체로 자연 반영·삭제도 staleness reeval) — tombstone은 도메인 연산 표현으로만(중앙 영속 0). **구현(OQ-3 decided)**: 별도 `OkfTombstone` 값객체 없이 `CommitRequest.removed_paths: tuple[str,...] = ()` 한 필드로 삭제 명령을 실현(추가·편집·삭제가 한 커밋 경로로 수렴·완전 하위호환). `DELETE /author/concept/{agent_id}/{concept_id}`가 삭제 커밋→인덱스 재도출로 owner 자기 개념 삭제를 제공한다.
4. **C1·C2**: 의미 near-dup 탐지·병합은 **owner측 저작 단계·로컬 다국어 임베딩(중앙 토큰 0·T10.5 인프라 공유·포트 뒤)** — 중앙 무변경. **구현 완료(T11.8d)**: 모델·라이브러리 = `fastembed` + `intfloat/multilingual-e5-small`(ONNX·torch 없음·`dedup` extra 격리). `Embedder` 포트(`okf_dedup.py`·numpy 비노출) 뒤·실 어댑터 `provider_embed_fastembed.FastEmbedEmbedder`(e5 prefix·L2 정규화 실측 norm=1.0)는 게이트 밖이나 실 E2E 검증 완료(finance_ops "가격 정책" 85% similar 탐지).
5. **C3·C4**: **자동 병합 금지·owner 확인 후보 제안**(과병합=지식 손실·1인칭 처분), 병합 연산은 B2·B3 기계 재사용. **구현 완료(T11.8d)**: τ_high=0.88·τ_low=0.70(cosine·주입 정책값·미검증 기본값·하드코딩 금지). 탐지=`POST /author/dedup/{agent_id}`(읽기 전용·후보만 반환·중앙 0·`index.md` 번들 메타 제외)·`classify_dedup_candidates`(순수 pairwise cosine·임계 분류)·`DedupCandidate`(frozen·grade `auto_suggest`|`similar` sealed). 병합 실행=기존 `PUT`/`DELETE /author/concept` 재사용(새 병합 연산 0). 프론트(`app/author/page.tsx`): 추출 직후 자동 dedup 체크·카드별 후보 배너·"기존 개념으로 병합" 1클릭(PUT 갱신 + 신규는 거부 처리).
