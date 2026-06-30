# Agent Org Network Component Specs

총 86개 컴포넌트 | 패밀리: button, copilot-artifact, copilot-chat, data-display, document, editorial, feedback, foundation, input, layout, navigation, overlay

## 구현 원칙 (Non-negotiable)

이 스펙의 모든 컴포넌트를 구현할 때 반드시 지킨다:

1. **이모지를 UI로 쓰지 않는다** — 🎨 ✅ 🔥 ⚡ 🚀 ❌ ⭐ 📊 등 이모지를 아이콘, 상태 표시, 버튼 장식, 네비게이션 지표 자리에 절대 넣지 않는다. 리팩토링 중 카드/버튼/배지/탭/상태 UI에서 이모지를 발견하면 SVG 파일, SVG 컴포넌트, 또는 Lucide/Heroicons/Phosphor/Tabler 같은 아이콘 라이브러리로 교체한다.
2. **컴포넌트를 직접 구현한다** — 아래 각 컴포넌트의 anatomy(구조), states(상태), 토큰 바인딩, 접근성 규칙을 그대로 따라 완전하게 구현한다. '임시', 'TODO', '플레이스홀더' 같은 반쪽 구현을 남기지 않는다.
3. **라이브러리 기본 스타일 금지** — 라이브러리 컴포넌트를 그대로 import해서 쓰지 않는다. 반드시 디자인 토큰(--color-*, --space-*, --radius-*, --font-*)으로 스타일을 명시적으로 바인딩한다.
4. **접근성은 옵션이 아니다** — 각 컴포넌트의 '접근성' 섹션에 정의된 role, aria-*, label, focus 관리 규칙을 전부 적용한다.
5. **hex 값 하드코딩 금지** — 색상은 반드시 semantic token을 경유한다 (예: `color: var(--color-ink)` not `color: #2C2C2C`).
6. **모바일 overflow 금지** — 버튼, CTA, 탭, 필터칩, 툴바 액션은 320px viewport에서 화면 밖으로 나가면 안 된다. fixed/min-width px 값으로 폭을 고정하지 말고 wrap/stack fallback을 제공한다.

## 브랜드 적용 규칙

- **hover**: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- **motion**: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- **color**: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음
- **density**: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- **feedback**: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

## Visual-reference 적용 원칙

- anatomy / states / accessibility는 설계서(spec)와 KB 근거를 유지하고, visual adaptation은 elevation / framing / prominence / density 같은 표현 계층에만 advisory signal로 적용한다.
- Active visual signals: surface_style=elevated, density=dense, corner_style=medium, top_layout_cue=data-review-surface
- Connected component hints: cards, data_display, navigation, typography

## Responsive Resilience

- 모바일에서 horizontal scroll이 생기거나 primary action이 화면 밖으로 나가면 컴포넌트 구현이 완료된 것이 아니다.
- Required viewport checks: 320px, 360px, 390px, 430px, 768px, 1024px, 1440px
- Buttons, CTA groups, tabs, filter chips, and toolbar actions must not rely on fixed px widths or mobile-hostile min-width values.
- Every button-like control needs max-inline-size: 100%; controls inside flex/grid parents need min-inline-size: 0 so labels can shrink or wrap.
- Action rows must wrap or stack at narrow widths; two-button rows need a <=480px fallback before implementation is complete.
- Long Korean CTA labels must be tested with real copy. Prefer wrapping/stacking over clipping, overflow hidden, or forcing white-space: nowrap.
- Horizontal rails, tickers, score strips, and carousels must not reveal partially clipped text. Either size cards so visible items are complete, or use compact labels such as icon+code in the rail and move full names to the detailed surface.

---

## button / primary-button

**역할**: 주요 행동을 유도하는 CTA 버튼

**탐지 출처**: baseline

### 구조 (Anatomy)

- container
- label
- leading-icon(optional)
- trailing-icon(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `disabled` | 비활성 (상호작용 불가) |
| `loading` | 로딩 중 (스피너 표시) |

### 토큰 바인딩

```
surface: var(--color-brand-primary)
text: var(--color-text-inverse)
border: var(--color-brand-primary)
radius: var(--radius-md)
padding: var(--space-12) var(--space-24)
max-inline-size: 100%
min-inline-size: 0
label-wrap: white-space: normal
font: var(--font-body) / var(--text-md) / semibold
hover-surface: var(--color-link-hover)
focus-ring: box-shadow: 0 0 0 2px var(--color-surface), 0 0 0 4px var(--color-brand-primary)
motion: background var(--duration-180) var(--ease-standard)
```

### 접근성

- role="button"
- aria-disabled="true" when disabled
- aria-busy="true" when loading
- 최소 44x44 터치 영역
- 텍스트 대비 4.5:1 이상
- 320px viewport에서도 버튼 전체와 focus ring이 화면 밖으로 나가지 않아야 함

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **cta_prominence**: CTA prominence는 restrained이다. 데이터 작업 흐름을 가리지 않도록 primary만 선명하게 두고 나머지는 text/ghost로 낮춘다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium, layout=data-review-surface)

### 레퍼런스 근거

- **Carbon Design System**: These components can toggle between the AI variant and the default variant depending on the user’s interaction. If the user manually overrides the ...
- **Primer**: I get so much joy from writing HTML and CSS, and design systems are one level up - systematically making UIs accessible and consistent. I love conc...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- size prop: sm / md / lg (터치 영역은 항상 최소 44px 보장)
- 모든 버튼은 `max-inline-size: 100%`와 `min-inline-size: 0`을 기본 보호값으로 갖고, 긴 라벨은 모바일에서 wrap 또는 action-group stack으로 처리
- fixed `width`/`min-width` px 값으로 CTA 폭을 고정하지 않음 — 필요하면 container query 또는 <=480px stack fallback을 함께 정의
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## button / secondary-button

**역할**: 보조 행동 버튼

**탐지 출처**: baseline

### 구조 (Anatomy)

- container
- label
- leading-icon(optional)
- trailing-icon(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `disabled` | 비활성 (상호작용 불가) |
| `loading` | 로딩 중 (스피너 표시) |

### 토큰 바인딩

```
surface: var(--color-brand-primary)
text: var(--color-text-inverse)
border: var(--color-brand-primary)
radius: var(--radius-md)
padding: var(--space-12) var(--space-24)
max-inline-size: 100%
min-inline-size: 0
label-wrap: white-space: normal
font: var(--font-body) / var(--text-md) / semibold
hover-surface: var(--color-link-hover)
focus-ring: box-shadow: 0 0 0 2px var(--color-surface), 0 0 0 4px var(--color-brand-primary)
motion: background var(--duration-180) var(--ease-standard)
```

### 접근성

- role="button"
- aria-disabled="true" when disabled
- aria-busy="true" when loading
- 최소 44x44 터치 영역
- 텍스트 대비 4.5:1 이상
- 320px viewport에서도 버튼 전체와 focus ring이 화면 밖으로 나가지 않아야 함

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **cta_prominence**: CTA prominence는 restrained이다. 데이터 작업 흐름을 가리지 않도록 primary만 선명하게 두고 나머지는 text/ghost로 낮춘다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium, layout=data-review-surface)

### 레퍼런스 근거

- **Carbon Design System**: These components can toggle between the AI variant and the default variant depending on the user’s interaction. If the user manually overrides the ...
- **Primer**: I get so much joy from writing HTML and CSS, and design systems are one level up - systematically making UIs accessible and consistent. I love conc...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- size prop: sm / md / lg (터치 영역은 항상 최소 44px 보장)
- 모든 버튼은 `max-inline-size: 100%`와 `min-inline-size: 0`을 기본 보호값으로 갖고, 긴 라벨은 모바일에서 wrap 또는 action-group stack으로 처리
- fixed `width`/`min-width` px 값으로 CTA 폭을 고정하지 않음 — 필요하면 container query 또는 <=480px stack fallback을 함께 정의
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## button / ghost-button

**역할**: 최소한의 시각적 무게를 가진 버튼

**탐지 출처**: baseline

### 구조 (Anatomy)

- container
- label
- leading-icon(optional)
- trailing-icon(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `disabled` | 비활성 (상호작용 불가) |
| `loading` | 로딩 중 (스피너 표시) |

### 토큰 바인딩

```
surface: var(--color-brand-primary)
text: var(--color-text-inverse)
border: var(--color-brand-primary)
radius: var(--radius-md)
padding: var(--space-12) var(--space-24)
max-inline-size: 100%
min-inline-size: 0
label-wrap: white-space: normal
font: var(--font-body) / var(--text-md) / semibold
hover-surface: var(--color-link-hover)
focus-ring: box-shadow: 0 0 0 2px var(--color-surface), 0 0 0 4px var(--color-brand-primary)
motion: background var(--duration-180) var(--ease-standard)
```

### 접근성

- role="button"
- aria-disabled="true" when disabled
- aria-busy="true" when loading
- 최소 44x44 터치 영역
- 텍스트 대비 4.5:1 이상
- 320px viewport에서도 버튼 전체와 focus ring이 화면 밖으로 나가지 않아야 함

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **cta_prominence**: CTA prominence는 restrained이다. 데이터 작업 흐름을 가리지 않도록 primary만 선명하게 두고 나머지는 text/ghost로 낮춘다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium, layout=data-review-surface)

### 레퍼런스 근거

- **Carbon Design System**: These components can toggle between the AI variant and the default variant depending on the user’s interaction. If the user manually overrides the ...
- **Primer**: I get so much joy from writing HTML and CSS, and design systems are one level up - systematically making UIs accessible and consistent. I love conc...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- size prop: sm / md / lg (터치 영역은 항상 최소 44px 보장)
- 모든 버튼은 `max-inline-size: 100%`와 `min-inline-size: 0`을 기본 보호값으로 갖고, 긴 라벨은 모바일에서 wrap 또는 action-group stack으로 처리
- fixed `width`/`min-width` px 값으로 CTA 폭을 고정하지 않음 — 필요하면 container query 또는 <=480px stack fallback을 함께 정의
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## button / icon-button

**역할**: 아이콘만 있는 액션 버튼

**탐지 출처**: baseline

### 구조 (Anatomy)

- container
- label
- leading-icon(optional)
- trailing-icon(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `disabled` | 비활성 (상호작용 불가) |
| `loading` | 로딩 중 (스피너 표시) |

### 토큰 바인딩

```
surface: var(--color-brand-primary)
text: var(--color-text-inverse)
border: var(--color-brand-primary)
radius: var(--radius-md)
padding: var(--space-12) var(--space-24)
max-inline-size: 100%
min-inline-size: 0
label-wrap: white-space: normal
font: var(--font-body) / var(--text-md) / semibold
hover-surface: var(--color-link-hover)
focus-ring: box-shadow: 0 0 0 2px var(--color-surface), 0 0 0 4px var(--color-brand-primary)
motion: background var(--duration-180) var(--ease-standard)
```

### 접근성

- role="button"
- aria-disabled="true" when disabled
- aria-busy="true" when loading
- 최소 44x44 터치 영역
- 텍스트 대비 4.5:1 이상
- 320px viewport에서도 버튼 전체와 focus ring이 화면 밖으로 나가지 않아야 함

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **cta_prominence**: CTA prominence는 restrained이다. 데이터 작업 흐름을 가리지 않도록 primary만 선명하게 두고 나머지는 text/ghost로 낮춘다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium, layout=data-review-surface)

### 레퍼런스 근거

- **Carbon Design System**: These components can toggle between the AI variant and the default variant depending on the user’s interaction. If the user manually overrides the ...
- **Primer**: I get so much joy from writing HTML and CSS, and design systems are one level up - systematically making UIs accessible and consistent. I love conc...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- size prop: sm / md / lg (터치 영역은 항상 최소 44px 보장)
- 모든 버튼은 `max-inline-size: 100%`와 `min-inline-size: 0`을 기본 보호값으로 갖고, 긴 라벨은 모바일에서 wrap 또는 action-group stack으로 처리
- fixed `width`/`min-width` px 값으로 CTA 폭을 고정하지 않음 — 필요하면 container query 또는 <=480px stack fallback을 함께 정의
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## button / link-button

**역할**: 텍스트 링크 스타일 버튼

**탐지 출처**: baseline

### 구조 (Anatomy)

- container
- label
- leading-icon(optional)
- trailing-icon(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `disabled` | 비활성 (상호작용 불가) |
| `loading` | 로딩 중 (스피너 표시) |

### 토큰 바인딩

```
surface: var(--color-brand-primary)
text: var(--color-text-inverse)
border: var(--color-brand-primary)
radius: var(--radius-md)
padding: var(--space-12) var(--space-24)
max-inline-size: 100%
min-inline-size: 0
label-wrap: white-space: normal
font: var(--font-body) / var(--text-md) / semibold
hover-surface: var(--color-link-hover)
focus-ring: box-shadow: 0 0 0 2px var(--color-surface), 0 0 0 4px var(--color-brand-primary)
motion: background var(--duration-180) var(--ease-standard)
```

### 접근성

- role="button"
- aria-disabled="true" when disabled
- aria-busy="true" when loading
- 최소 44x44 터치 영역
- 텍스트 대비 4.5:1 이상
- 320px viewport에서도 버튼 전체와 focus ring이 화면 밖으로 나가지 않아야 함

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **cta_prominence**: CTA prominence는 restrained이다. 데이터 작업 흐름을 가리지 않도록 primary만 선명하게 두고 나머지는 text/ghost로 낮춘다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium, layout=data-review-surface)

### 레퍼런스 근거

- **Carbon Design System**: These components can toggle between the AI variant and the default variant depending on the user’s interaction. If the user manually overrides the ...
- **Primer**: I get so much joy from writing HTML and CSS, and design systems are one level up - systematically making UIs accessible and consistent. I love conc...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- size prop: sm / md / lg (터치 영역은 항상 최소 44px 보장)
- 모든 버튼은 `max-inline-size: 100%`와 `min-inline-size: 0`을 기본 보호값으로 갖고, 긴 라벨은 모바일에서 wrap 또는 action-group stack으로 처리
- fixed `width`/`min-width` px 값으로 CTA 폭을 고정하지 않음 — 필요하면 container query 또는 <=480px stack fallback을 함께 정의
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / tag

**역할**: 분류/라벨 태그

**탐지 출처**: tags and labels

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 레퍼런스 근거

- **Carbon Design System**: Tag Date picker
- **Primer**: Primer color design tokens are made available within data-attribute selectors on the body tag or other high level dom element. There are three dist...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## feedback / status-badge

**역할**: 상태를 색상으로 표시하는 뱃지

**탐지 출처**: tags and labels

### 구조 (Anatomy)

- container
- icon
- message
- action(optional)
- close-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `info` | 정보 알림 |
| `success` | 성공 알림 |
| `warning` | 경고 알림 |
| `danger` | 에러/위험 알림 |

### 토큰 바인딩

```
surface: var(--color-surface-muted)
text: var(--color-text)
icon: var(--color-info)
border: var(--color-border)
radius: var(--radius-sm)
padding: var(--space-12) var(--space-16)
severity-info: var(--color-info)
severity-success: var(--color-success)
severity-warning: var(--color-warning)
severity-danger: var(--color-danger)
```

### 접근성

- role="alert" for urgent messages
- role="status" for non-urgent
- aria-live="polite" or "assertive"
- 닫기 버튼에 aria-label 필수

### 브랜드 적용

- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 레퍼런스 근거

- **Carbon Design System**: We welcome all feedback, designs, or ideas in order to produce the best possible experience for our users. If you're interested in contributing, ch...
- **Primer**: Messaging components are used to provide important and relevant information to the user, including feedback, contextual information, product update...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- auto-dismiss 시간은 내용 길이에 비례 (기본 5초)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## input / chip

**역할**: 선택/해제 가능한 칩

**탐지 출처**: tags and labels

### 구조 (Anatomy)

- container
- label
- input-area
- helper-text(optional)
- leading-icon(optional)
- clear-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focus` | 키보드 포커스 시 |
| `filled` | 값이 입력된 상태 |
| `error` | 유효성 검증 실패 |
| `disabled` | 비활성 (상호작용 불가) |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
placeholder: var(--color-text-subtle)
border: var(--color-border)
border-focus: var(--color-brand-primary)
border-error: var(--color-danger)
radius: var(--radius-sm)
padding: var(--space-8) var(--space-12)
font: var(--font-body) / var(--text-md) / regular
```

### 접근성

- label과 input을 for/id로 연결
- aria-describedby로 helper/error text 연결
- aria-invalid="true" when error
- aria-required="true" when required

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Number input Text input
- **Primer**: I worked in data visualization and map-making for most of my career, and solving design problems with data is my jam. To me there's something uniqu...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- error 상태에서 helper text → error message로 자동 전환
- label은 항상 visible (placeholder만으로 대체 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## editorial / editor-canvas

**역할**: 텍스트 편집 영역

**탐지 출처**: rich text editor

### 구조 (Anatomy)

- canvas
- toolbar
- content-blocks
- selection-handle(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `editing` | 편집 모드 활성 |
| `selecting` | 텍스트/블록 선택 중 |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
font: var(--font-body) / var(--text-md) / regular
heading-font: var(--font-heading) / var(--text-2xl) / bold
padding: var(--space-24) var(--space-32)
line-height: var(--leading-relaxed)
```

### 접근성

- contenteditable 영역에 role="textbox"
- aria-multiline="true"
- 도구 모음에 role="toolbar"
- 서식 버튼에 aria-pressed 상태

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 블록 단위 데이터 모델, JSON 직렬화 가능한 구조
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## editorial / editor-toolbar

**역할**: 서식 도구 모음

**탐지 출처**: rich text editor

### 구조 (Anatomy)

- canvas
- toolbar
- content-blocks
- selection-handle(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `editing` | 편집 모드 활성 |
| `selecting` | 텍스트/블록 선택 중 |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
font: var(--font-body) / var(--text-md) / regular
heading-font: var(--font-heading) / var(--text-2xl) / bold
padding: var(--space-24) var(--space-32)
line-height: var(--leading-relaxed)
```

### 접근성

- contenteditable 영역에 role="textbox"
- aria-multiline="true"
- 도구 모음에 role="toolbar"
- 서식 버튼에 aria-pressed 상태

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 블록 단위 데이터 모델, JSON 직렬화 가능한 구조
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## editorial / inline-format-menu

**역할**: 텍스트 선택 시 나타나는 인라인 포맷 메뉴

**탐지 출처**: rich text editor

### 구조 (Anatomy)

- canvas
- toolbar
- content-blocks
- selection-handle(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `editing` | 편집 모드 활성 |
| `selecting` | 텍스트/블록 선택 중 |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
font: var(--font-body) / var(--text-md) / regular
heading-font: var(--font-heading) / var(--text-2xl) / bold
padding: var(--space-24) var(--space-32)
line-height: var(--leading-relaxed)
```

### 접근성

- contenteditable 영역에 role="textbox"
- aria-multiline="true"
- 도구 모음에 role="toolbar"
- 서식 버튼에 aria-pressed 상태

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 블록 단위 데이터 모델, JSON 직렬화 가능한 구조
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## editorial / slash-command-menu

**역할**: / 입력으로 블록 타입 선택

**탐지 출처**: rich text editor

### 구조 (Anatomy)

- canvas
- toolbar
- content-blocks
- selection-handle(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `editing` | 편집 모드 활성 |
| `selecting` | 텍스트/블록 선택 중 |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
font: var(--font-body) / var(--text-md) / regular
heading-font: var(--font-heading) / var(--text-2xl) / bold
padding: var(--space-24) var(--space-32)
line-height: var(--leading-relaxed)
```

### 접근성

- contenteditable 영역에 role="textbox"
- aria-multiline="true"
- 도구 모음에 role="toolbar"
- 서식 버튼에 aria-pressed 상태

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 블록 단위 데이터 모델, JSON 직렬화 가능한 구조
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## editorial / block-controls

**역할**: 블록 이동/삭제/타입 변경 컨트롤

**탐지 출처**: rich text editor

### 구조 (Anatomy)

- canvas
- toolbar
- content-blocks
- selection-handle(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `editing` | 편집 모드 활성 |
| `selecting` | 텍스트/블록 선택 중 |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
font: var(--font-body) / var(--text-md) / regular
heading-font: var(--font-heading) / var(--text-2xl) / bold
padding: var(--space-24) var(--space-32)
line-height: var(--leading-relaxed)
```

### 접근성

- contenteditable 영역에 role="textbox"
- aria-multiline="true"
- 도구 모음에 role="toolbar"
- 서식 버튼에 aria-pressed 상태

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 블록 단위 데이터 모델, JSON 직렬화 가능한 구조
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / data-table

**역할**: 정렬·필터 가능한 데이터 테이블

**탐지 출처**: data tables

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 레퍼런스 근거

- **Carbon Design System**: Data table Modal
- **Primer**: DataTable DataTable is a 2-dimensional data structure where each row is an item, and each column is a data point about the item.

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / column-header

**역할**: 테이블 컬럼 헤더 (정렬 토글)

**탐지 출처**: data tables

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / row-actions

**역할**: 행별 액션 메뉴

**탐지 출처**: data tables

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## navigation / pagination

**역할**: 페이지 이동 컨트롤

**탐지 출처**: data tables

### 구조 (Anatomy)

- container
- nav-item
- icon(optional)
- label
- indicator(active)
- badge(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `collapsed` | 접힌 상태 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text-muted)
text-active: var(--color-text)
indicator: var(--color-brand-accent)
padding: var(--space-8) var(--space-16)
font: var(--font-body) / var(--text-sm) / medium
```

### 접근성

- nav landmark (role="navigation")
- aria-current="page" for active item
- 키보드 화살표 탐색 지원

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Library menu navigation There are two kinds of symbols — library symbols and document symbols. Library symbols are available in any Sketch document...
- **Primer**: Octicon nav items navigation 12 px

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- active 상태는 URL/라우터와 자동 동기화
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## input / filter-chip

**역할**: 활성 필터를 칩으로 표시/해제

**탐지 출처**: data tables

### 구조 (Anatomy)

- container
- label
- input-area
- helper-text(optional)
- leading-icon(optional)
- clear-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focus` | 키보드 포커스 시 |
| `filled` | 값이 입력된 상태 |
| `error` | 유효성 검증 실패 |
| `disabled` | 비활성 (상호작용 불가) |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
placeholder: var(--color-text-subtle)
border: var(--color-border)
border-focus: var(--color-brand-primary)
border-error: var(--color-danger)
radius: var(--radius-sm)
padding: var(--space-8) var(--space-12)
font: var(--font-body) / var(--text-md) / regular
```

### 접근성

- label과 input을 for/id로 연결
- aria-describedby로 helper/error text 연결
- aria-invalid="true" when error
- aria-required="true" when required

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Number input Text input
- **Primer**: I worked in data visualization and map-making for most of my career, and solving design problems with data is my jam. To me there's something uniqu...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- error 상태에서 helper text → error message로 자동 전환
- label은 항상 visible (placeholder만으로 대체 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / chat-message

**역할**: 채팅 메시지 말풍선

**탐지 출처**: chat and messaging

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## input / chat-input

**역할**: 메시지 입력 영역

**탐지 출처**: chat and messaging

### 구조 (Anatomy)

- container
- label
- input-area
- helper-text(optional)
- leading-icon(optional)
- clear-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focus` | 키보드 포커스 시 |
| `filled` | 값이 입력된 상태 |
| `error` | 유효성 검증 실패 |
| `disabled` | 비활성 (상호작용 불가) |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
placeholder: var(--color-text-subtle)
border: var(--color-border)
border-focus: var(--color-brand-primary)
border-error: var(--color-danger)
radius: var(--radius-sm)
padding: var(--space-8) var(--space-12)
font: var(--font-body) / var(--text-md) / regular
```

### 접근성

- label과 input을 for/id로 연결
- aria-describedby로 helper/error text 연결
- aria-invalid="true" when error
- aria-required="true" when required

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

### 레퍼런스 근거

- **Carbon Design System**: Number input Text input
- **Primer**: I worked in data visualization and map-making for most of my career, and solving design problems with data is my jam. To me there's something uniqu...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- error 상태에서 helper text → error message로 자동 전환
- label은 항상 visible (placeholder만으로 대체 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / chat-thread

**역할**: 대화 스레드 목록

**탐지 출처**: chat and messaging

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / chart-container

**역할**: 차트 래퍼 (타이틀, 범례 포함)

**탐지 출처**: charts and visualization

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## overlay / chart-tooltip

**역할**: 데이터 포인트 호버 시 상세 정보

**탐지 출처**: charts and visualization

### 구조 (Anatomy)

- backdrop
- container
- header
- content
- footer(optional)
- close-button

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `closed` | 닫힌 상태 |
| `opening` | 열리는 중 (전환 애니메이션) |
| `open` | 열린 상태 |
| `closing` | 닫히는 중 |

### 토큰 바인딩

```
surface: var(--color-surface-elevated)
backdrop: rgb(0 0 0 / 0.5)
radius: var(--radius-lg)
padding: var(--space-24)
border: var(--color-border)
motion: opacity var(--duration-180) var(--ease-standard)
```

### 접근성

- role="dialog" with aria-modal="true"
- focus trap (Tab 순환)
- Escape로 닫기
- aria-labelledby로 제목 연결
- 닫은 후 trigger 요소로 포커스 복귀

### 브랜드 적용

- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 레퍼런스 근거

- **Primer**: ActionMenu is composed of ActionList and Overlay patterns used for quick actions and selections. AnchoredOverlay

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- Escape / backdrop click으로 닫기, 열 때 첫 focusable 요소로 이동
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / chart-legend

**역할**: 차트 범례

**탐지 출처**: charts and visualization

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / avatar

**역할**: 사용자 프로필 이미지/이니셜

**탐지 출처**: user profile and avatar

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 레퍼런스 근거

- **Primer**: Avatar Avatar is an image that represents a user or organization.

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## overlay / user-menu

**역할**: 사용자 드롭다운 메뉴

**탐지 출처**: user profile and avatar

### 구조 (Anatomy)

- backdrop
- container
- header
- content
- footer(optional)
- close-button

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `closed` | 닫힌 상태 |
| `opening` | 열리는 중 (전환 애니메이션) |
| `open` | 열린 상태 |
| `closing` | 닫히는 중 |

### 토큰 바인딩

```
surface: var(--color-surface-elevated)
backdrop: rgb(0 0 0 / 0.5)
radius: var(--radius-lg)
padding: var(--space-24)
border: var(--color-border)
motion: opacity var(--duration-180) var(--ease-standard)
```

### 접근성

- role="dialog" with aria-modal="true"
- focus trap (Tab 순환)
- Escape로 닫기
- aria-labelledby로 제목 연결
- 닫은 후 trigger 요소로 포커스 복귀

### 브랜드 적용

- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Primer**: ActionMenu is composed of ActionList and Overlay patterns used for quick actions and selections. AnchoredOverlay

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- Escape / backdrop click으로 닫기, 열 때 첫 focusable 요소로 이동
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / profile-card

**역할**: 사용자 프로필 요약 카드

**탐지 출처**: user profile and avatar

**Slot archetype**: `surface-card`

### 구조 (Anatomy)

- container
- inner-content

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `focus-visible` | focus-visible |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
border-hover: var(--color-border-strong)
radius: var(--radius-lg)
padding: var(--space-32)
gap: var(--space-16)
motion: border-color var(--duration-180) var(--ease-standard)
```

### 접근성

- 카드 자체가 링크/버튼이면 <a>/<button> 래퍼 사용
- 장식적 카드는 단순 <article> 또는 <div>

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## input / text-field

**역할**: 단일 줄 텍스트 입력

**탐지 출처**: forms

### 구조 (Anatomy)

- container
- label
- input-area
- helper-text(optional)
- leading-icon(optional)
- clear-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focus` | 키보드 포커스 시 |
| `filled` | 값이 입력된 상태 |
| `error` | 유효성 검증 실패 |
| `disabled` | 비활성 (상호작용 불가) |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
placeholder: var(--color-text-subtle)
border: var(--color-border)
border-focus: var(--color-brand-primary)
border-error: var(--color-danger)
radius: var(--radius-sm)
padding: var(--space-8) var(--space-12)
font: var(--font-body) / var(--text-md) / regular
```

### 접근성

- label과 input을 for/id로 연결
- aria-describedby로 helper/error text 연결
- aria-invalid="true" when error
- aria-required="true" when required

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

### 레퍼런스 근거

- **Carbon Design System**: Number input Text input
- **Primer**: I worked in data visualization and map-making for most of my career, and solving design problems with data is my jam. To me there's something uniqu...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- error 상태에서 helper text → error message로 자동 전환
- label은 항상 visible (placeholder만으로 대체 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## input / textarea

**역할**: 여러 줄 텍스트 입력

**탐지 출처**: forms

### 구조 (Anatomy)

- container
- label
- input-area
- helper-text(optional)
- leading-icon(optional)
- clear-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focus` | 키보드 포커스 시 |
| `filled` | 값이 입력된 상태 |
| `error` | 유효성 검증 실패 |
| `disabled` | 비활성 (상호작용 불가) |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
placeholder: var(--color-text-subtle)
border: var(--color-border)
border-focus: var(--color-brand-primary)
border-error: var(--color-danger)
radius: var(--radius-sm)
padding: var(--space-8) var(--space-12)
font: var(--font-body) / var(--text-md) / regular
```

### 접근성

- label과 input을 for/id로 연결
- aria-describedby로 helper/error text 연결
- aria-invalid="true" when error
- aria-required="true" when required

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

### 레퍼런스 근거

- **Carbon Design System**: Number input Text input
- **Primer**: I worked in data visualization and map-making for most of my career, and solving design problems with data is my jam. To me there's something uniqu...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- error 상태에서 helper text → error message로 자동 전환
- label은 항상 visible (placeholder만으로 대체 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## input / select

**역할**: 드롭다운 선택

**탐지 출처**: forms

### 구조 (Anatomy)

- container
- label
- input-area
- helper-text(optional)
- leading-icon(optional)
- clear-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focus` | 키보드 포커스 시 |
| `filled` | 값이 입력된 상태 |
| `error` | 유효성 검증 실패 |
| `disabled` | 비활성 (상호작용 불가) |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
placeholder: var(--color-text-subtle)
border: var(--color-border)
border-focus: var(--color-brand-primary)
border-error: var(--color-danger)
radius: var(--radius-sm)
padding: var(--space-8) var(--space-12)
font: var(--font-body) / var(--text-md) / regular
```

### 접근성

- label과 input을 for/id로 연결
- aria-describedby로 helper/error text 연결
- aria-invalid="true" when error
- aria-required="true" when required

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

### 레퍼런스 근거

- **Carbon Design System**: Select Data table
- **Primer**: Primer color design tokens are made available within data-attribute selectors on the body tag or other high level dom element. There are three dist...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- error 상태에서 helper text → error message로 자동 전환
- label은 항상 visible (placeholder만으로 대체 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## input / checkbox

**역할**: 체크박스

**탐지 출처**: forms

### 구조 (Anatomy)

- container
- label
- input-area
- helper-text(optional)
- leading-icon(optional)
- clear-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focus` | 키보드 포커스 시 |
| `filled` | 값이 입력된 상태 |
| `error` | 유효성 검증 실패 |
| `disabled` | 비활성 (상호작용 불가) |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
placeholder: var(--color-text-subtle)
border: var(--color-border)
border-focus: var(--color-brand-primary)
border-error: var(--color-danger)
radius: var(--radius-sm)
padding: var(--space-8) var(--space-12)
font: var(--font-body) / var(--text-md) / regular
```

### 접근성

- label과 input을 for/id로 연결
- aria-describedby로 helper/error text 연결
- aria-invalid="true" when error
- aria-required="true" when required

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

### 레퍼런스 근거

- **Carbon Design System**: Checkbox Form
- **Primer**: I worked in data visualization and map-making for most of my career, and solving design problems with data is my jam. To me there's something uniqu...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- error 상태에서 helper text → error message로 자동 전환
- label은 항상 visible (placeholder만으로 대체 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## input / radio-group

**역할**: 라디오 버튼 그룹

**탐지 출처**: forms

### 구조 (Anatomy)

- container
- label
- input-area
- helper-text(optional)
- leading-icon(optional)
- clear-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focus` | 키보드 포커스 시 |
| `filled` | 값이 입력된 상태 |
| `error` | 유효성 검증 실패 |
| `disabled` | 비활성 (상호작용 불가) |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
placeholder: var(--color-text-subtle)
border: var(--color-border)
border-focus: var(--color-brand-primary)
border-error: var(--color-danger)
radius: var(--radius-sm)
padding: var(--space-8) var(--space-12)
font: var(--font-body) / var(--text-md) / regular
```

### 접근성

- label과 input을 for/id로 연결
- aria-describedby로 helper/error text 연결
- aria-invalid="true" when error
- aria-required="true" when required

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

### 레퍼런스 근거

- **Carbon Design System**: Number input Text input
- **Primer**: I worked in data visualization and map-making for most of my career, and solving design problems with data is my jam. To me there's something uniqu...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- error 상태에서 helper text → error message로 자동 전환
- label은 항상 visible (placeholder만으로 대체 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## input / form-section

**역할**: 폼 섹션 그룹핑과 레이블

**탐지 출처**: forms

### 구조 (Anatomy)

- container
- label
- input-area
- helper-text(optional)
- leading-icon(optional)
- clear-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focus` | 키보드 포커스 시 |
| `filled` | 값이 입력된 상태 |
| `error` | 유효성 검증 실패 |
| `disabled` | 비활성 (상호작용 불가) |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
placeholder: var(--color-text-subtle)
border: var(--color-border)
border-focus: var(--color-brand-primary)
border-error: var(--color-danger)
radius: var(--radius-sm)
padding: var(--space-8) var(--space-12)
font: var(--font-body) / var(--text-md) / regular
```

### 접근성

- label과 input을 for/id로 연결
- aria-describedby로 helper/error text 연결
- aria-invalid="true" when error
- aria-required="true" when required

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

### 레퍼런스 근거

- **Carbon Design System**: Number input Text input
- **Primer**: I worked in data visualization and map-making for most of my career, and solving design problems with data is my jam. To me there's something uniqu...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- error 상태에서 helper text → error message로 자동 전환
- label은 항상 visible (placeholder만으로 대체 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## button / form-actions

**역할**: 폼 하단 제출/취소 버튼 영역

**탐지 출처**: forms

### 구조 (Anatomy)

- container
- label
- leading-icon(optional)
- trailing-icon(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `disabled` | 비활성 (상호작용 불가) |
| `loading` | 로딩 중 (스피너 표시) |

### 토큰 바인딩

```
surface: var(--color-brand-primary)
text: var(--color-text-inverse)
border: var(--color-brand-primary)
radius: var(--radius-md)
padding: var(--space-12) var(--space-24)
max-inline-size: 100%
min-inline-size: 0
label-wrap: white-space: normal
font: var(--font-body) / var(--text-md) / semibold
hover-surface: var(--color-link-hover)
focus-ring: box-shadow: 0 0 0 2px var(--color-surface), 0 0 0 4px var(--color-brand-primary)
motion: background var(--duration-180) var(--ease-standard)
```

### 접근성

- role="button"
- aria-disabled="true" when disabled
- aria-busy="true" when loading
- 최소 44x44 터치 영역
- 텍스트 대비 4.5:1 이상
- 320px viewport에서도 버튼 전체와 focus ring이 화면 밖으로 나가지 않아야 함

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **cta_prominence**: CTA prominence는 restrained이다. 데이터 작업 흐름을 가리지 않도록 primary만 선명하게 두고 나머지는 text/ghost로 낮춘다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium, layout=data-review-surface)

### 레퍼런스 근거

- **Carbon Design System**: These components can toggle between the AI variant and the default variant depending on the user’s interaction. If the user manually overrides the ...
- **Primer**: I get so much joy from writing HTML and CSS, and design systems are one level up - systematically making UIs accessible and consistent. I love conc...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- size prop: sm / md / lg (터치 영역은 항상 최소 44px 보장)
- 모든 버튼은 `max-inline-size: 100%`와 `min-inline-size: 0`을 기본 보호값으로 갖고, 긴 라벨은 모바일에서 wrap 또는 action-group stack으로 처리
- fixed `width`/`min-width` px 값으로 CTA 폭을 고정하지 않음 — 필요하면 container query 또는 <=480px stack fallback을 함께 정의
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / stat-card

**역할**: 주요 수치를 표시하는 통계 카드

**탐지 출처**: dashboard cards

**Slot archetype**: `surface-card`

### 구조 (Anatomy)

- container
- inner-content

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `focus-visible` | focus-visible |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
border-hover: var(--color-border-strong)
radius: var(--radius-lg)
padding: var(--space-32)
gap: var(--space-16)
motion: border-color var(--duration-180) var(--ease-standard)
```

### 접근성

- 카드 자체가 링크/버튼이면 <a>/<button> 래퍼 사용
- 장식적 카드는 단순 <article> 또는 <div>

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / insight-card

**역할**: 인사이트나 트렌드를 요약하는 카드

**탐지 출처**: dashboard cards

**Slot archetype**: `surface-card`

### 구조 (Anatomy)

- container
- inner-content

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `focus-visible` | focus-visible |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
border-hover: var(--color-border-strong)
radius: var(--radius-lg)
padding: var(--space-32)
gap: var(--space-16)
motion: border-color var(--duration-180) var(--ease-standard)
```

### 접근성

- 카드 자체가 링크/버튼이면 <a>/<button> 래퍼 사용
- 장식적 카드는 단순 <article> 또는 <div>

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / activity-card

**역할**: 최근 활동 피드 카드

**탐지 출처**: dashboard cards

**Slot archetype**: `surface-card`

### 구조 (Anatomy)

- container
- inner-content

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `focus-visible` | focus-visible |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
border-hover: var(--color-border-strong)
radius: var(--radius-lg)
padding: var(--space-32)
gap: var(--space-16)
motion: border-color var(--duration-180) var(--ease-standard)
```

### 접근성

- 카드 자체가 링크/버튼이면 <a>/<button> 래퍼 사용
- 장식적 카드는 단순 <article> 또는 <div>

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / section-header

**역할**: 대시보드 섹션 구분 헤더

**탐지 출처**: dashboard cards

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / kanban-board

**역할**: 칸반 보드 레이아웃

**탐지 출처**: kanban and board

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / kanban-column

**역할**: 칸반 컬럼 (상태별)

**탐지 출처**: kanban and board

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / kanban-card

**역할**: 드래그 가능한 작업 카드

**탐지 출처**: kanban and board

**Slot archetype**: `surface-card`

### 구조 (Anatomy)

- container
- inner-content

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `focus-visible` | focus-visible |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
border-hover: var(--color-border-strong)
radius: var(--radius-lg)
padding: var(--space-32)
gap: var(--space-16)
motion: border-color var(--duration-180) var(--ease-standard)
```

### 접근성

- 카드 자체가 링크/버튼이면 <a>/<button> 래퍼 사용
- 장식적 카드는 단순 <article> 또는 <div>

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## document / article-body

**역할**: long-form article body — prose block + heading anchor

**탐지 출처**: reference documentation

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 레퍼런스 근거

- **Carbon Design System**: AI chat is a conversational framework between a user and an AI that can aid in creating tasks, finding insights, tracking documents, and more. For ...
- **Primer**: The GitHub Design Infrastructure and Design Engineering teams build and maintain Primer — this includes our CSS framework, style guide documentatio...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## document / table-of-contents

**역할**: TOC sidebar with anchor-linked heading outline

**탐지 출처**: reference documentation

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 레퍼런스 근거

- **Carbon Design System**: AI chat is a conversational framework between a user and an AI that can aid in creating tasks, finding insights, tracking documents, and more. For ...
- **Primer**: The GitHub Design Infrastructure and Design Engineering teams build and maintain Primer — this includes our CSS framework, style guide documentatio...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## document / heading-anchor

**역할**: h1~h6 heading with #id anchor and copy-link

**탐지 출처**: reference documentation

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 레퍼런스 근거

- **Carbon Design System**: AI chat is a conversational framework between a user and an AI that can aid in creating tasks, finding insights, tracking documents, and more. For ...
- **Primer**: The GitHub Design Infrastructure and Design Engineering teams build and maintain Primer — this includes our CSS framework, style guide documentatio...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## document / prose-block

**역할**: prose reading block rendering markdown article content

**탐지 출처**: reference documentation

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 레퍼런스 근거

- **Carbon Design System**: AI chat is a conversational framework between a user and an AI that can aid in creating tasks, finding insights, tracking documents, and more. For ...
- **Primer**: The GitHub Design Infrastructure and Design Engineering teams build and maintain Primer — this includes our CSS framework, style guide documentatio...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## document / reading-pane

**역할**: main reading column, measured width 65–75ch

**탐지 출처**: reference documentation

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 레퍼런스 근거

- **Carbon Design System**: AI chat is a conversational framework between a user and an AI that can aid in creating tasks, finding insights, tracking documents, and more. For ...
- **Primer**: The GitHub Design Infrastructure and Design Engineering teams build and maintain Primer — this includes our CSS framework, style guide documentatio...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## document / footnote

**역할**: numbered reference footnote inside article body

**탐지 출처**: reference documentation

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 레퍼런스 근거

- **Carbon Design System**: AI chat is a conversational framework between a user and an AI that can aid in creating tasks, finding insights, tracking documents, and more. For ...
- **Primer**: The GitHub Design Infrastructure and Design Engineering teams build and maintain Primer — this includes our CSS framework, style guide documentatio...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## navigation / prev-next-pager

**역할**: bottom-of-article prev/next reference link

**탐지 출처**: reference documentation

### 구조 (Anatomy)

- container
- nav-item
- icon(optional)
- label
- indicator(active)
- badge(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `collapsed` | 접힌 상태 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text-muted)
text-active: var(--color-text)
indicator: var(--color-brand-accent)
padding: var(--space-8) var(--space-16)
font: var(--font-body) / var(--text-sm) / medium
```

### 접근성

- nav landmark (role="navigation")
- aria-current="page" for active item
- 키보드 화살표 탐색 지원

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Library menu navigation There are two kinds of symbols — library symbols and document symbols. Library symbols are available in any Sketch document...
- **Primer**: Octicon nav items navigation 12 px

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- active 상태는 URL/라우터와 자동 동기화
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## document / link-card

**역할**: related article card with title + summary

**탐지 출처**: reference documentation

**Slot archetype**: `surface-card`

### 구조 (Anatomy)

- container
- inner-content

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `focus-visible` | focus-visible |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
border-hover: var(--color-border-strong)
radius: var(--radius-lg)
padding: var(--space-32)
gap: var(--space-16)
motion: border-color var(--duration-180) var(--ease-standard)
```

### 접근성

- 카드 자체가 링크/버튼이면 <a>/<button> 래퍼 사용
- 장식적 카드는 단순 <article> 또는 <div>

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)

### 레퍼런스 근거

- **Carbon Design System**: AI chat is a conversational framework between a user and an AI that can aid in creating tasks, finding insights, tracking documents, and more. For ...
- **Primer**: The GitHub Design Infrastructure and Design Engineering teams build and maintain Primer — this includes our CSS framework, style guide documentatio...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## feedback / toast

**역할**: 일시적 성공/에러 알림

**탐지 출처**: notifications

### 구조 (Anatomy)

- container
- icon
- message
- action(optional)
- close-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `info` | 정보 알림 |
| `success` | 성공 알림 |
| `warning` | 경고 알림 |
| `danger` | 에러/위험 알림 |

### 토큰 바인딩

```
surface: var(--color-surface-muted)
text: var(--color-text)
icon: var(--color-info)
border: var(--color-border)
radius: var(--radius-sm)
padding: var(--space-12) var(--space-16)
severity-info: var(--color-info)
severity-success: var(--color-success)
severity-warning: var(--color-warning)
severity-danger: var(--color-danger)
```

### 접근성

- role="alert" for urgent messages
- role="status" for non-urgent
- aria-live="polite" or "assertive"
- 닫기 버튼에 aria-label 필수

### 브랜드 적용

- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 레퍼런스 근거

- **Carbon Design System**: We welcome all feedback, designs, or ideas in order to produce the best possible experience for our users. If you're interested in contributing, ch...
- **Primer**: Messaging components are used to provide important and relevant information to the user, including feedback, contextual information, product update...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- auto-dismiss 시간은 내용 길이에 비례 (기본 5초)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## feedback / inline-alert

**역할**: 페이지 내 알림 배너

**탐지 출처**: notifications

### 구조 (Anatomy)

- container
- icon
- message
- action(optional)
- close-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `info` | 정보 알림 |
| `success` | 성공 알림 |
| `warning` | 경고 알림 |
| `danger` | 에러/위험 알림 |

### 토큰 바인딩

```
surface: var(--color-surface-muted)
text: var(--color-text)
icon: var(--color-info)
border: var(--color-border)
radius: var(--radius-sm)
padding: var(--space-12) var(--space-16)
severity-info: var(--color-info)
severity-success: var(--color-success)
severity-warning: var(--color-warning)
severity-danger: var(--color-danger)
```

### 접근성

- role="alert" for urgent messages
- role="status" for non-urgent
- aria-live="polite" or "assertive"
- 닫기 버튼에 aria-label 필수

### 브랜드 적용

- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 레퍼런스 근거

- **Carbon Design System**: We welcome all feedback, designs, or ideas in order to produce the best possible experience for our users. If you're interested in contributing, ch...
- **Primer**: Messaging components are used to provide important and relevant information to the user, including feedback, contextual information, product update...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- auto-dismiss 시간은 내용 길이에 비례 (기본 5초)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## feedback / empty-state

**역할**: 데이터가 없을 때 안내 화면

**탐지 출처**: notifications

### 구조 (Anatomy)

- container
- icon
- message
- action(optional)
- close-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `info` | 정보 알림 |
| `success` | 성공 알림 |
| `warning` | 경고 알림 |
| `danger` | 에러/위험 알림 |

### 토큰 바인딩

```
surface: var(--color-surface-muted)
text: var(--color-text)
icon: var(--color-info)
border: var(--color-border)
radius: var(--radius-sm)
padding: var(--space-12) var(--space-16)
severity-info: var(--color-info)
severity-success: var(--color-success)
severity-warning: var(--color-warning)
severity-danger: var(--color-danger)
```

### 접근성

- role="alert" for urgent messages
- role="status" for non-urgent
- aria-live="polite" or "assertive"
- 닫기 버튼에 aria-label 필수

### 브랜드 적용

- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 레퍼런스 근거

- **Carbon Design System**: We welcome all feedback, designs, or ideas in order to produce the best possible experience for our users. If you're interested in contributing, ch...
- **Primer**: Empty states Empty states are used to fill spaces when no content has been added yet, or is temporarily empty due to the nature of the feature.

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- auto-dismiss 시간은 내용 길이에 비례 (기본 5초)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## feedback / banner

**역할**: 전체 화면 상단 공지 배너

**탐지 출처**: notifications

### 구조 (Anatomy)

- container
- icon
- message
- action(optional)
- close-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `info` | 정보 알림 |
| `success` | 성공 알림 |
| `warning` | 경고 알림 |
| `danger` | 에러/위험 알림 |

### 토큰 바인딩

```
surface: var(--color-surface-muted)
text: var(--color-text)
icon: var(--color-info)
border: var(--color-border)
radius: var(--radius-sm)
padding: var(--space-12) var(--space-16)
severity-info: var(--color-info)
severity-success: var(--color-success)
severity-warning: var(--color-warning)
severity-danger: var(--color-danger)
```

### 접근성

- role="alert" for urgent messages
- role="status" for non-urgent
- aria-live="polite" or "assertive"
- 닫기 버튼에 aria-label 필수

### 브랜드 적용

- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)

### 레퍼런스 근거

- **Carbon Design System**: We welcome all feedback, designs, or ideas in order to produce the best possible experience for our users. If you're interested in contributing, ch...
- **Primer**: Banner Banner is used to highlight important information.

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- auto-dismiss 시간은 내용 길이에 비례 (기본 5초)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / comment-thread

**역할**: 댓글 스레드 목록

**탐지 출처**: comments and discussion

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## input / comment-input

**역할**: 댓글 입력 영역

**탐지 출처**: comments and discussion

### 구조 (Anatomy)

- container
- label
- input-area
- helper-text(optional)
- leading-icon(optional)
- clear-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focus` | 키보드 포커스 시 |
| `filled` | 값이 입력된 상태 |
| `error` | 유효성 검증 실패 |
| `disabled` | 비활성 (상호작용 불가) |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
placeholder: var(--color-text-subtle)
border: var(--color-border)
border-focus: var(--color-brand-primary)
border-error: var(--color-danger)
radius: var(--radius-sm)
padding: var(--space-8) var(--space-12)
font: var(--font-body) / var(--text-md) / regular
```

### 접근성

- label과 input을 for/id로 연결
- aria-describedby로 helper/error text 연결
- aria-invalid="true" when error
- aria-required="true" when required

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

### 레퍼런스 근거

- **Carbon Design System**: Number input Text input
- **Primer**: I worked in data visualization and map-making for most of my career, and solving design problems with data is my jam. To me there's something uniqu...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- error 상태에서 helper text → error message로 자동 전환
- label은 항상 visible (placeholder만으로 대체 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## overlay / mention-popup

**역할**: @멘션 자동완성 팝업

**탐지 출처**: comments and discussion

### 구조 (Anatomy)

- backdrop
- container
- header
- content
- footer(optional)
- close-button

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `closed` | 닫힌 상태 |
| `opening` | 열리는 중 (전환 애니메이션) |
| `open` | 열린 상태 |
| `closing` | 닫히는 중 |

### 토큰 바인딩

```
surface: var(--color-surface-elevated)
backdrop: rgb(0 0 0 / 0.5)
radius: var(--radius-lg)
padding: var(--space-24)
border: var(--color-border)
motion: opacity var(--duration-180) var(--ease-standard)
```

### 접근성

- role="dialog" with aria-modal="true"
- focus trap (Tab 순환)
- Escape로 닫기
- aria-labelledby로 제목 연결
- 닫은 후 trigger 요소로 포커스 복귀

### 브랜드 적용

- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음

### 레퍼런스 근거

- **Primer**: ActionMenu is composed of ActionList and Overlay patterns used for quick actions and selections. AnchoredOverlay

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- Escape / backdrop click으로 닫기, 열 때 첫 focusable 요소로 이동
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## feedback / step-progress

**역할**: 단계별 진행 표시

**탐지 출처**: onboarding and stepper

### 구조 (Anatomy)

- container
- icon
- message
- action(optional)
- close-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `info` | 정보 알림 |
| `success` | 성공 알림 |
| `warning` | 경고 알림 |
| `danger` | 에러/위험 알림 |

### 토큰 바인딩

```
surface: var(--color-surface-muted)
text: var(--color-text)
icon: var(--color-info)
border: var(--color-border)
radius: var(--radius-sm)
padding: var(--space-12) var(--space-16)
severity-info: var(--color-info)
severity-success: var(--color-success)
severity-warning: var(--color-warning)
severity-danger: var(--color-danger)
```

### 접근성

- role="alert" for urgent messages
- role="status" for non-urgent
- aria-live="polite" or "assertive"
- 닫기 버튼에 aria-label 필수

### 브랜드 적용

- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 레퍼런스 근거

- **Carbon Design System**: We welcome all feedback, designs, or ideas in order to produce the best possible experience for our users. If you're interested in contributing, ch...
- **Primer**: Messaging components are used to provide important and relevant information to the user, including feedback, contextual information, product update...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- auto-dismiss 시간은 내용 길이에 비례 (기본 5초)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## navigation / wizard-layout

**역할**: 위저드 레이아웃 (이전/다음)

**탐지 출처**: onboarding and stepper

### 구조 (Anatomy)

- container
- nav-item
- icon(optional)
- label
- indicator(active)
- badge(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `collapsed` | 접힌 상태 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text-muted)
text-active: var(--color-text)
indicator: var(--color-brand-accent)
padding: var(--space-8) var(--space-16)
font: var(--font-body) / var(--text-sm) / medium
```

### 접근성

- nav landmark (role="navigation")
- aria-current="page" for active item
- 키보드 화살표 탐색 지원

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Library menu navigation There are two kinds of symbols — library symbols and document symbols. Library symbols are available in any Sketch document...
- **Primer**: Octicon nav items navigation 12 px

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- active 상태는 URL/라우터와 자동 동기화
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## overlay / tooltip-guide

**역할**: 기능 안내 툴팁

**탐지 출처**: onboarding and stepper

### 구조 (Anatomy)

- backdrop
- container
- header
- content
- footer(optional)
- close-button

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `closed` | 닫힌 상태 |
| `opening` | 열리는 중 (전환 애니메이션) |
| `open` | 열린 상태 |
| `closing` | 닫히는 중 |

### 토큰 바인딩

```
surface: var(--color-surface-elevated)
backdrop: rgb(0 0 0 / 0.5)
radius: var(--radius-lg)
padding: var(--space-24)
border: var(--color-border)
motion: opacity var(--duration-180) var(--ease-standard)
```

### 접근성

- role="dialog" with aria-modal="true"
- focus trap (Tab 순환)
- Escape로 닫기
- aria-labelledby로 제목 연결
- 닫은 후 trigger 요소로 포커스 복귀

### 브랜드 적용

- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음

### 레퍼런스 근거

- **Primer**: ActionMenu is composed of ActionList and Overlay patterns used for quick actions and selections. AnchoredOverlay

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- Escape / backdrop click으로 닫기, 열 때 첫 focusable 요소로 이동
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## foundation / app-shell

**역할**: —

### 구조 (Anatomy)

- container
- header
- content-area
- footer(optional)
- action(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `error` | 유효성 검증 실패 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
radius: var(--radius-md)
padding: var(--space-16) var(--space-20)
heading-font: var(--font-heading) / var(--text-md) / semibold
body-font: var(--font-body) / var(--text-sm) / regular
text: var(--color-text)
text-muted: var(--color-text-muted)
```

### 접근성

- 적절한 heading level 사용
- 데이터 테이블은 scope와 caption 필수
- 빈 상태에서 안내 텍스트 제공

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 레퍼런스 근거

- **Carbon Design System**: Carbon is IBM’s open source design system for products and digital experiences. With the IBM Design Language as its foundation, the system consists...
- **Primer**: Shared Foundations Accessibility

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## navigation / sidebar-nav

**역할**: —

### 구조 (Anatomy)

- container
- nav-item
- icon(optional)
- label
- indicator(active)
- badge(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `collapsed` | 접힌 상태 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text-muted)
text-active: var(--color-text)
indicator: var(--color-brand-accent)
padding: var(--space-8) var(--space-16)
font: var(--font-body) / var(--text-sm) / medium
```

### 접근성

- nav landmark (role="navigation")
- aria-current="page" for active item
- 키보드 화살표 탐색 지원

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Total panel width (“sidebar nav”) + Left outside margin + Right outside margin = Total margins Artboard width - Total margins = Total width
- **Primer**: Octicon nav items navigation 12 px

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- active 상태는 URL/라우터와 자동 동기화
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## navigation / topbar

**역할**: —

### 구조 (Anatomy)

- container
- nav-item
- icon(optional)
- label
- indicator(active)
- badge(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `collapsed` | 접힌 상태 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text-muted)
text-active: var(--color-text)
indicator: var(--color-brand-accent)
padding: var(--space-8) var(--space-16)
font: var(--font-body) / var(--text-sm) / medium
```

### 접근성

- nav landmark (role="navigation")
- aria-current="page" for active item
- 키보드 화살표 탐색 지원

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Library menu navigation There are two kinds of symbols — library symbols and document symbols. Library symbols are available in any Sketch document...
- **Primer**: Octicon nav items navigation 12 px

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- active 상태는 URL/라우터와 자동 동기화
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## navigation / breadcrumb

**역할**: —

### 구조 (Anatomy)

- container
- nav-item
- icon(optional)
- label
- indicator(active)
- badge(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `collapsed` | 접힌 상태 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text-muted)
text-active: var(--color-text)
indicator: var(--color-brand-accent)
padding: var(--space-8) var(--space-16)
font: var(--font-body) / var(--text-sm) / medium
```

### 접근성

- nav landmark (role="navigation")
- aria-current="page" for active item
- 키보드 화살표 탐색 지원

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Library menu navigation There are two kinds of symbols — library symbols and document symbols. Library symbols are available in any Sketch document...
- **Primer**: Octicon nav items navigation 12 px

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- active 상태는 URL/라우터와 자동 동기화
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## navigation / workspace-switcher

**역할**: —

### 구조 (Anatomy)

- container
- nav-item
- icon(optional)
- label
- indicator(active)
- badge(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `collapsed` | 접힌 상태 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text-muted)
text-active: var(--color-text)
indicator: var(--color-brand-accent)
padding: var(--space-8) var(--space-16)
font: var(--font-body) / var(--text-sm) / medium
```

### 접근성

- nav landmark (role="navigation")
- aria-current="page" for active item
- 키보드 화살표 탐색 지원

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Library menu navigation There are two kinds of symbols — library symbols and document symbols. Library symbols are available in any Sketch document...
- **Primer**: Octicon nav items navigation 12 px

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- active 상태는 URL/라우터와 자동 동기화
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## input / radio

**역할**: —

### 구조 (Anatomy)

- container
- label
- input-area
- helper-text(optional)
- leading-icon(optional)
- clear-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focus` | 키보드 포커스 시 |
| `filled` | 값이 입력된 상태 |
| `error` | 유효성 검증 실패 |
| `disabled` | 비활성 (상호작용 불가) |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
placeholder: var(--color-text-subtle)
border: var(--color-border)
border-focus: var(--color-brand-primary)
border-error: var(--color-danger)
radius: var(--radius-sm)
padding: var(--space-8) var(--space-12)
font: var(--font-body) / var(--text-md) / regular
```

### 접근성

- label과 input을 for/id로 연결
- aria-describedby로 helper/error text 연결
- aria-invalid="true" when error
- aria-required="true" when required

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

### 레퍼런스 근거

- **Carbon Design System**: Radio button Tile
- **Primer**: I worked in data visualization and map-making for most of my career, and solving design problems with data is my jam. To me there's something uniqu...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- error 상태에서 helper text → error message로 자동 전환
- label은 항상 visible (placeholder만으로 대체 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## button / cta-button

**역할**: —

### 구조 (Anatomy)

- container
- label
- leading-icon(optional)
- trailing-icon(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `disabled` | 비활성 (상호작용 불가) |
| `loading` | 로딩 중 (스피너 표시) |

### 토큰 바인딩

```
surface: var(--color-brand-primary)
text: var(--color-text-inverse)
border: var(--color-brand-primary)
radius: var(--radius-md)
padding: var(--space-12) var(--space-24)
max-inline-size: 100%
min-inline-size: 0
label-wrap: white-space: normal
font: var(--font-body) / var(--text-md) / semibold
hover-surface: var(--color-link-hover)
focus-ring: box-shadow: 0 0 0 2px var(--color-surface), 0 0 0 4px var(--color-brand-primary)
motion: background var(--duration-180) var(--ease-standard)
```

### 접근성

- role="button"
- aria-disabled="true" when disabled
- aria-busy="true" when loading
- 최소 44x44 터치 영역
- 텍스트 대비 4.5:1 이상
- 320px viewport에서도 버튼 전체와 focus ring이 화면 밖으로 나가지 않아야 함

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **cta_prominence**: CTA prominence는 strong이다. 섹션당 primary CTA 1개만 fill/accent로 강하게 띄우고 secondary는 조용하게 후퇴시킨다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium, layout=data-review-surface)

### 레퍼런스 근거

- **Carbon Design System**: These components can toggle between the AI variant and the default variant depending on the user’s interaction. If the user manually overrides the ...
- **Primer**: I get so much joy from writing HTML and CSS, and design systems are one level up - systematically making UIs accessible and consistent. I love conc...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- size prop: sm / md / lg (터치 영역은 항상 최소 44px 보장)
- 모든 버튼은 `max-inline-size: 100%`와 `min-inline-size: 0`을 기본 보호값으로 갖고, 긴 라벨은 모바일에서 wrap 또는 action-group stack으로 처리
- fixed `width`/`min-width` px 값으로 CTA 폭을 고정하지 않음 — 필요하면 container query 또는 <=480px stack fallback을 함께 정의
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## navigation / mobile-topbar

**역할**: —

### 구조 (Anatomy)

- container
- nav-item
- icon(optional)
- label
- indicator(active)
- badge(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `collapsed` | 접힌 상태 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text-muted)
text-active: var(--color-text)
indicator: var(--color-brand-accent)
padding: var(--space-8) var(--space-16)
font: var(--font-body) / var(--text-sm) / medium
```

### 접근성

- nav landmark (role="navigation")
- aria-current="page" for active item
- 키보드 화살표 탐색 지원

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Library menu navigation There are two kinds of symbols — library symbols and document symbols. Library symbols are available in any Sketch document...
- **Primer**: Octicon nav items navigation 12 px

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- active 상태는 URL/라우터와 자동 동기화
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## navigation / mobile-tab-bar

**역할**: —

### 구조 (Anatomy)

- container
- nav-item
- icon(optional)
- label
- indicator(active)
- badge(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `collapsed` | 접힌 상태 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text-muted)
text-active: var(--color-text)
indicator: var(--color-brand-accent)
padding: var(--space-8) var(--space-16)
font: var(--font-body) / var(--text-sm) / medium
```

### 접근성

- nav landmark (role="navigation")
- aria-current="page" for active item
- 키보드 화살표 탐색 지원

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Library menu navigation There are two kinds of symbols — library symbols and document symbols. Library symbols are available in any Sketch document...
- **Primer**: Octicon nav items navigation 12 px

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- active 상태는 URL/라우터와 자동 동기화
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## navigation / back-button

**역할**: —

### 구조 (Anatomy)

- container
- nav-item
- icon(optional)
- label
- indicator(active)
- badge(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `collapsed` | 접힌 상태 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text-muted)
text-active: var(--color-text)
indicator: var(--color-brand-accent)
padding: var(--space-8) var(--space-16)
font: var(--font-body) / var(--text-sm) / medium
```

### 접근성

- nav landmark (role="navigation")
- aria-current="page" for active item
- 키보드 화살표 탐색 지원

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **cta_prominence**: CTA prominence는 restrained이다. 데이터 작업 흐름을 가리지 않도록 primary만 선명하게 두고 나머지는 text/ghost로 낮춘다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium, layout=data-review-surface)
- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Library menu navigation There are two kinds of symbols — library symbols and document symbols. Library symbols are available in any Sketch document...
- **Primer**: Octicon nav items navigation 12 px

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- active 상태는 URL/라우터와 자동 동기화
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## navigation / section-tabs

**역할**: —

### 구조 (Anatomy)

- container
- nav-item
- icon(optional)
- label
- indicator(active)
- badge(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `active` | 클릭/탭 중 |
| `collapsed` | 접힌 상태 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text-muted)
text-active: var(--color-text)
indicator: var(--color-brand-accent)
padding: var(--space-8) var(--space-16)
font: var(--font-body) / var(--text-sm) / medium
```

### 접근성

- nav landmark (role="navigation")
- aria-current="page" for active item
- 키보드 화살표 탐색 지원

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Library menu navigation There are two kinds of symbols — library symbols and document symbols. Library symbols are available in any Sketch document...
- **Primer**: Octicon nav items navigation 12 px

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- active 상태는 URL/라우터와 자동 동기화
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## overlay / bottom-sheet

**역할**: —

### 구조 (Anatomy)

- backdrop
- container
- header
- content
- footer(optional)
- close-button

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `closed` | 닫힌 상태 |
| `opening` | 열리는 중 (전환 애니메이션) |
| `open` | 열린 상태 |
| `closing` | 닫히는 중 |

### 토큰 바인딩

```
surface: var(--color-surface-elevated)
backdrop: rgb(0 0 0 / 0.5)
radius: var(--radius-lg)
padding: var(--space-24)
border: var(--color-border)
motion: opacity var(--duration-180) var(--ease-standard)
```

### 접근성

- role="dialog" with aria-modal="true"
- focus trap (Tab 순환)
- Escape로 닫기
- aria-labelledby로 제목 연결
- 닫은 후 trigger 요소로 포커스 복귀

### 브랜드 적용

- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음

### 레퍼런스 근거

- **Primer**: ActionMenu is composed of ActionList and Overlay patterns used for quick actions and selections. AnchoredOverlay

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- Escape / backdrop click으로 닫기, 열 때 첫 focusable 요소로 이동
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## overlay / modal-dialog

**역할**: —

### 구조 (Anatomy)

- backdrop
- container
- header
- content
- footer(optional)
- close-button

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `closed` | 닫힌 상태 |
| `opening` | 열리는 중 (전환 애니메이션) |
| `open` | 열린 상태 |
| `closing` | 닫히는 중 |

### 토큰 바인딩

```
surface: var(--color-surface-elevated)
backdrop: rgb(0 0 0 / 0.5)
radius: var(--radius-lg)
padding: var(--space-24)
border: var(--color-border)
motion: opacity var(--duration-180) var(--ease-standard)
```

### 접근성

- role="dialog" with aria-modal="true"
- focus trap (Tab 순환)
- Escape로 닫기
- aria-labelledby로 제목 연결
- 닫은 후 trigger 요소로 포커스 복귀

### 브랜드 적용

- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음

### 레퍼런스 근거

- **Primer**: ActionMenu is composed of ActionList and Overlay patterns used for quick actions and selections. AnchoredOverlay

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- Escape / backdrop click으로 닫기, 열 때 첫 focusable 요소로 이동
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## input / search-field

**역할**: —

### 구조 (Anatomy)

- container
- label
- input-area
- helper-text(optional)
- leading-icon(optional)
- clear-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focus` | 키보드 포커스 시 |
| `filled` | 값이 입력된 상태 |
| `error` | 유효성 검증 실패 |
| `disabled` | 비활성 (상호작용 불가) |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
placeholder: var(--color-text-subtle)
border: var(--color-border)
border-focus: var(--color-brand-primary)
border-error: var(--color-danger)
radius: var(--radius-sm)
padding: var(--space-8) var(--space-12)
font: var(--font-body) / var(--text-md) / regular
```

### 접근성

- label과 input을 for/id로 연결
- aria-describedby로 helper/error text 연결
- aria-invalid="true" when error
- aria-required="true" when required

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Number input Text input
- **Primer**: I worked in data visualization and map-making for most of my career, and solving design problems with data is my jam. To me there's something uniqu...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- error 상태에서 helper text → error message로 자동 전환
- label은 항상 visible (placeholder만으로 대체 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## input / segmented-control

**역할**: —

### 구조 (Anatomy)

- container
- label
- input-area
- helper-text(optional)
- leading-icon(optional)
- clear-button(optional)

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focus` | 키보드 포커스 시 |
| `filled` | 값이 입력된 상태 |
| `error` | 유효성 검증 실패 |
| `disabled` | 비활성 (상호작용 불가) |
| `readonly` | 읽기 전용 |

### 토큰 바인딩

```
surface: var(--color-surface)
text: var(--color-text)
placeholder: var(--color-text-subtle)
border: var(--color-border)
border-focus: var(--color-brand-primary)
border-error: var(--color-danger)
radius: var(--radius-sm)
padding: var(--space-8) var(--space-12)
font: var(--font-body) / var(--text-md) / regular
```

### 접근성

- label과 input을 for/id로 연결
- aria-describedby로 helper/error text 연결
- aria-invalid="true" when error
- aria-required="true" when required

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] feedback: 명확한 상태 구분, 진행률/결과를 수치로 표시 + subtle inline alert 선호, 과한 컬러 블록 지양 + 결과를 반드시 확인, 실패 시 복구 방법 안내

### 레퍼런스 근거

- **Carbon Design System**: Number input Text input
- **Primer**: I worked in data visualization and map-making for most of my career, and solving design problems with data is my jam. To me there's something uniqu...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- error 상태에서 helper text → error message로 자동 전환
- label은 항상 visible (placeholder만으로 대체 금지)
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## document / diff-viewer

**역할**: Before/after document comparison with inline additions and removals

**Slot archetype**: `advanced:diff-viewer`

### Advanced Usage

Use when:
- AI rewrites, policy edits, or reviewer changes need auditability
- users must approve what changed before publishing
Avoid when:
- only a short status message changed
Pairs with: redline-viewer, revision-timeline, approval-rail

### 구조 (Anatomy)

- container
- version-header
- line-list
- change-marker
- gutter
- summary-footer

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `side-by-side` | side-by-side |
| `inline` | inline |
| `collapsed-context` | collapsed-context |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
added: var(--color-success)
removed: var(--color-danger)
muted-surface: var(--color-surface-muted)
font: var(--font-body)
mono: var(--font-mono)
```

### 접근성

- changes are announced with text labels, not color alone
- line numbers are decorative unless referenced by controls
- keyboard shortcuts have visible command alternatives

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 레퍼런스 근거

- **Carbon Design System**: AI chat is a conversational framework between a user and an AI that can aid in creating tasks, finding insights, tracking documents, and more. For ...
- **Primer**: The GitHub Design Infrastructure and Design Engineering teams build and maintain Primer — this includes our CSS framework, style guide documentatio...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / exception-queue

**역할**: Work queue for unresolved policy, data, or workflow exceptions

**Slot archetype**: `advanced:exception-queue`

### Advanced Usage

Use when:
- multiple issues require triage, assignment, and resolution
- reviewers need to batch handle exceptions
Avoid when:
- exceptions are rare and single-item
Pairs with: bulk-action-table, policy-matrix, approval-rail

### 구조 (Anatomy)

- queue-list
- queue-item
- priority
- assignee
- due-state
- bulk-action-bar

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `selected` | selected |
| `assigned` | assigned |
| `resolved` | resolved |
| `empty` | 데이터 없음 |

### 토큰 바인딩

```
surface: var(--color-surface)
selected-surface: var(--color-surface-tint)
border: var(--color-border)
priority: var(--color-warning)
radius: var(--radius-md)
```

### 접근성

- multi-select state is announced with aria-selected
- bulk actions disclose affected count
- empty state explains how exceptions appear

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## layout / resizable-split-pane

**역할**: Resizable two/three-pane workspace shell for dense tools

**Slot archetype**: `advanced:resizable-split-pane`

### Advanced Usage

Use when:
- primary work happens between list, canvas/chat, and detail panels
- users need to compare or inspect adjacent information without navigation
Avoid when:
- single linear form or landing page is enough
Pairs with: thread-list, artifact-preview-panel, inspector-drawer

### 구조 (Anatomy)

- container
- pane
- resize-handle
- collapse-button(optional)
- keyboard-resize affordance

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `resizing` | resizing |
| `collapsed` | 접힌 상태 |
| `focus` | 키보드 포커스 시 |

### 토큰 바인딩

```
surface: var(--color-canvas)
pane-surface: var(--color-surface)
divider: var(--color-border)
handle-focus: var(--color-brand-primary)
radius: var(--radius-lg)
gap: var(--space-16)
```

### 접근성

- resize handle uses role="separator" with aria-orientation
- aria-valuemin / aria-valuemax / aria-valuenow describe pane size
- Arrow keys resize focused handle; Enter toggles collapsed state

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 레퍼런스 근거

- **Carbon Design System**: The introduction of CSS Grid to build robust layouts on top of the 2x grid A 90% decrease in compilation for Styles from Carbon
- **Primer**: Use LabelGroup to add commonly used margins and other layout constraints to groups of Labels Link

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## copilot-artifact / source-card

**역할**: Compact source record card with title, excerpt, metadata, and verification state

**Slot archetype**: `advanced:source-card`

### Advanced Usage

Use when:
- AI output depends on external or internal source records
- users need a repeatable citation preview component
Avoid when:
- source metadata is unavailable
Pairs with: citation-drawer, evidence-graph, inline-citation

### 구조 (Anatomy)

- card
- source-title
- excerpt
- metadata-row
- verification-badge
- open-action

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `hover` | 마우스 오버 시 |
| `verified` | verified |
| `stale` | stale |
| `unavailable` | unavailable |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
verified: var(--color-success)
stale: var(--color-warning)
radius: var(--radius-md)
```

### 접근성

- source title is a heading or labelled link
- excerpt length is bounded and not a full copyrighted passage
- verification state includes text

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / audit-timeline

**역할**: Chronological audit trail with actor, action, timestamp, and linked artifact

**Slot archetype**: `advanced:audit-timeline`

### Advanced Usage

Use when:
- regulated workflows require traceable user and AI actions
- reviewers need to reconstruct what happened before approval
Avoid when:
- events are not user-facing or not actionable
Pairs with: decision-record-card, approval-rail, tool-call-trace

### 구조 (Anatomy)

- list
- event-item
- timestamp
- actor
- event-summary
- artifact-link

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `filtered` | 필터 적용됨 |
| `expanded` | expanded |
| `empty` | 데이터 없음 |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
timestamp: var(--color-brand-primary)
muted: var(--color-text-muted)
mono: var(--font-mono)
```

### 접근성

- timeline is an ordered list when chronology matters
- timestamps use machine-readable datetime when possible
- expanded details are reachable by keyboard

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / evidence-graph

**역할**: Node-link evidence map connecting claims, sources, policies, and decisions

**Slot archetype**: `advanced:evidence-graph`

### Advanced Usage

Use when:
- trust depends on seeing relationships between claims and sources
- auditors need to trace why an answer or decision was made
Avoid when:
- a simple source list communicates the relationship
Pairs with: citation-drawer, decision-record-card, policy-matrix

### 구조 (Anatomy)

- graph-canvas
- node
- edge
- legend
- selection-detail
- zoom-control

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `focused` | focused |
| `filtered` | 필터 적용됨 |
| `empty` | 데이터 없음 |

### 토큰 바인딩

```
surface: var(--color-surface)
node-surface: var(--color-surface-muted)
edge: var(--color-border-strong)
active: var(--color-brand-primary)
radius: var(--radius-md)
```

### 접근성

- graph has a table/list fallback with the same relationships
- selected node detail is announced in a live region
- zoom controls are buttons with visible labels

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## navigation / saved-view-bar

**역할**: Saved view and scope switcher for repeated operational filters

**Slot archetype**: `advanced:saved-view-bar`

### Advanced Usage

Use when:
- teams revisit the same filtered views often
- dense tools need stable scope memory
Avoid when:
- filters are one-off and simple
Pairs with: filter-builder, bulk-action-table, exception-queue

### 구조 (Anatomy)

- tab-list
- saved-view-tab
- count-badge
- overflow-menu
- save-action

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `active` | 클릭/탭 중 |
| `dirty` | dirty |
| `overflow` | overflow |

### 토큰 바인딩

```
surface: var(--color-surface)
active-surface: var(--color-surface-tint)
border: var(--color-border)
active: var(--color-brand-primary)
radius: var(--radius-md)
```

### 접근성

- tabs use role="tablist" / role="tab" when switching panels
- dirty state is text-announced
- overflow menu has keyboard navigation

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration

### Visual Adaptation Hints

- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)

### 레퍼런스 근거

- **Carbon Design System**: Library menu navigation There are two kinds of symbols — library symbols and document symbols. Library symbols are available in any Sketch document...
- **Primer**: Octicon nav items navigation 12 px

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- active 상태는 URL/라우터와 자동 동기화
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## copilot-chat / tool-call-trace

**역할**: Expandable trace of AI tool calls, inputs, outputs, and latency

**Slot archetype**: `advanced:tool-call-trace`

### Advanced Usage

Use when:
- AI actions need explainability or debugging
- operators need to audit retrieval, policy checks, or workflow calls
Avoid when:
- trace data is sensitive and cannot be summarized
Pairs with: audit-timeline, citation-drawer, decision-record-card

### 구조 (Anatomy)

- trace-list
- trace-step
- tool-name
- input-summary
- output-summary
- latency

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `collapsed` | 접힌 상태 |
| `expanded` | expanded |
| `running` | running |
| `failed` | failed |
| `complete` | complete |

### 토큰 바인딩

```
surface: var(--color-surface-muted)
border: var(--color-border)
running: var(--color-brand-primary)
failed: var(--color-danger)
mono: var(--font-mono)
```

### 접근성

- each trace step is expandable with aria-expanded
- running state uses aria-live="polite"
- sensitive payloads are summarized or redacted

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## document / redline-viewer

**역할**: Review-oriented prose surface with suggested insertions, deletions, and comments

**Slot archetype**: `advanced:redline-viewer`

### Advanced Usage

Use when:
- legal, compliance, or editorial text needs reviewer markup
- comments must stay anchored to exact text ranges
Avoid when:
- structured rows are more important than prose
Pairs with: diff-viewer, comment-thread, approval-rail

### 구조 (Anatomy)

- reading-pane
- marked-text
- comment-anchor
- comment-margin
- resolve-action

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `selected` | selected |
| `commenting` | commenting |
| `resolved` | resolved |

### 토큰 바인딩

```
surface: var(--color-surface)
mark-surface: var(--color-surface-tint)
comment-border: var(--color-border)
accent: var(--color-brand-accent)
radius: var(--radius-md)
```

### 접근성

- marked ranges expose aria-describedby to comment text
- resolved comments remain reachable from audit history
- keyboard can move between comment anchors

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 레퍼런스 근거

- **Carbon Design System**: AI chat is a conversational framework between a user and an AI that can aid in creating tasks, finding insights, tracking documents, and more. For ...
- **Primer**: The GitHub Design Infrastructure and Design Engineering teams build and maintain Primer — this includes our CSS framework, style guide documentatio...

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / bulk-action-table

**역할**: Selectable data table with sticky bulk action affordances

**Slot archetype**: `advanced:bulk-action-table`

### Advanced Usage

Use when:
- users handle many records at once
- selection count and destructive actions must stay visible
Avoid when:
- records are read-only or single-action
Pairs with: saved-view-bar, filter-builder, exception-queue

### 구조 (Anatomy)

- table
- selection-cell
- column-header
- row
- bulk-action-bar
- pagination

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `default` | 기본 상태 |
| `selected` | selected |
| `filtered` | 필터 적용됨 |
| `sorted` | 정렬 적용됨 |
| `empty` | 데이터 없음 |

### 토큰 바인딩

```
surface: var(--color-surface)
row-hover: var(--color-surface-muted)
selected: var(--color-surface-tint)
border: var(--color-border)
font: var(--font-body)
```

### 접근성

- header checkbox exposes mixed state when partially selected
- selection count is announced when it changes
- bulk action bar appears after selection in logical focus order

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **filter_nav_density**: filter/nav density는 compact하다. chip, scope, pagination을 촘촘하게 묶되 의미 단위별 group은 분리한다. (source=navigation; confidence=0.29; provenance=inferred; direction=navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다.; evidence=Data review surface, layout=data-review-surface, density=dense)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 반응형 검증: 320px, 360px, 390px, 430px에서 control overflow와 viewport horizontal scroll이 없어야 함
- action row는 narrow viewport에서 `flex-wrap: wrap` 또는 세로 stack으로 전환하고, overflow-x 숨김으로 문제를 덮지 않음
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## copilot-artifact / citation-drawer

**역할**: Source and citation drawer for AI answers, policies, and quoted evidence

**Slot archetype**: `advanced:citation-drawer`

### Advanced Usage

Use when:
- answers must show supporting policy, document, or source records
- users need to inspect evidence without losing conversation context
Avoid when:
- citations are static footnotes only
Pairs with: inline-citation, source-card, evidence-graph

### 구조 (Anatomy)

- drawer
- source-list
- source-card
- quote-snippet
- metadata-row
- open-source-action

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `closed` | 닫힌 상태 |
| `open` | 열린 상태 |
| `loading` | 로딩 중 (스피너 표시) |
| `empty` | 데이터 없음 |
| `verified` | verified |

### 토큰 바인딩

```
surface: var(--color-surface-elevated)
source-surface: var(--color-surface)
border: var(--color-border)
verified: var(--color-success)
radius: var(--radius-lg)
```

### 접근성

- each citation has a stable label and source title
- snippets are summaries unless licensing permits direct quote
- drawer focus order follows source ranking

### 브랜드 적용

- [precise+calm+trustworthy] hover: 정확한 border/outline 변화 + opacity 변화 (0.08-0.12), elevation 변화 없음 + 예측 가능하고 일관된 hover 패턴
- [precise+calm+trustworthy] motion: 120-180ms, 군더더기 없는 전환 + 150-200ms ease-out, bounce/spring 없음 + 모든 전환에 동일한 easing/duration
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지

---

## data-display / decision-record-card

**역할**: Auditable decision record summarizing decision, actor, evidence, and retention

**Slot archetype**: `advanced:decision-record-card`

### Advanced Usage

Use when:
- a reviewer or AI-assisted workflow reaches a durable decision
- regulated teams need record ids and retention status
Avoid when:
- the action is transient and not auditable
Pairs with: audit-timeline, approval-rail, citation-drawer

### 구조 (Anatomy)

- card
- record-id
- decision-summary
- actor-row
- evidence-links
- retention-state

### 상태 (States)

| 상태 | 설명 |
|------|------|
| `draft` | draft |
| `recorded` | recorded |
| `locked` | locked |
| `expired` | expired |

### 토큰 바인딩

```
surface: var(--color-surface)
border: var(--color-border)
locked: var(--color-brand-primary)
expired: var(--color-warning)
mono: var(--font-mono)
```

### 접근성

- record id is selectable text
- locked and expired states include text labels
- evidence links are grouped under an accessible heading

### 브랜드 적용

- [precise+calm+trustworthy] density: 엄격한 spacing scale 준수, 임의 값 금지 + comfortable 모드 기본, 여유로운 padding + 기존 레이아웃 유지, 갑작스런 위치 변경 없음
- [precise+calm+trustworthy] color: 정확한 semantic 분리, 모호한 중간 톤 지양 + 중성 톤 위주, accent는 최소한으로 + 안정적인 neutral 기반, 과한 accent 변화 없음

### Visual Adaptation Hints

- **card_elevation_tendency**: 카드는 중간 이하 shadow만 허용하고 deep shadow stack은 피한다. 압축된 spacing에서도 header/body/footer 구획은 divider나 tint로 유지한다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **border_vs_fill_emphasis**: fill 중심이다. depth와 surface contrast가 우선이며 border는 선택적으로만 쓴다. (source=cards; confidence=0.29; provenance=inferred; direction=raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지.; evidence=surface=elevated, density=dense, corner=medium)
- **chart_panel_framing**: 차트 패널은 single raised surface로 프레이밍하고 nested card는 피한다. 헤더의 metric, controls, plot 영역을 분리하고 내부 여백은 촘촘하게 관리한다. (source=data_display; confidence=0.29; provenance=inferred; direction=정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다.; evidence=layout=dashboard-grid, density=dense, layout=data-review-surface, surface=elevated)

### 구현 노트

- 기존에 같은 역할의 컴포넌트가 있으면 토큰 교체부터 시작
- variant prop으로 시각적 변형을 관리 (하드코딩 금지)
- 빈 상태(empty-state)와 에러 상태를 반드시 처리
- 애니메이션은 상태 설명용으로만 사용, 장식 효과 금지
