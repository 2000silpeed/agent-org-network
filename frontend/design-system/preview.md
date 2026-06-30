# dashboard--corporate-trust

## 어떤 제품에 맞나
- Agent Org Network question-routing console design system

- app_mode: `dashboard` / brand_tone: `corporate-trust`

## Color Tokens (light + dark)

### Semantic
- success: `#006A4E`
- warning: `#FFB27F`
- danger: `#E2725B`
- info: `#0F4C81`

> light/dark 변형은 어댑터(`nextjs-tailwind-shadcn` 등)가 파생. 원본 팔레트는 위 HEX 1세트.

## Typography
- heading: Pretendard
- body: Pretendard
- mono: Source Code Pro
- korean: n/a

## 대표 컴포넌트
- **table-of-contents** — parts: container, header, content-area, footer(optional), action(optional) | states: default, loading, empty, error
- **sidebar-nav** — parts: container, nav-item, icon(optional), label, indicator(active) | states: default, hover, active, collapsed
- **decision-record-card** — parts: card, record-id, decision-summary, actor-row, evidence-links | states: draft, recorded, locked, expired

## 주의사항
- image-derived hints는 advisory (truth source: system_spec.md, token_schema.json)
- 이 프리셋과 맞지 않으면 `brand_tone` 축을 바꿔 재시도
