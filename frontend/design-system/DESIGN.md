# Agent Org Network Design System Style Capsule

- preset: `dashboard--corporate-trust`
- brand: Agent Org Network
- mode: `dashboard` / `corporate-trust`
- capsule_version: `1.0.0`
- companion files: `STYLE.md` is the human brief; `DESIGN.md` is the agent-ready copy.

## Taste Summary
Agent Org Network question-routing console design system.

## Authority Order
1. Product task flow and information architecture
2. `token_schema.json` and generated CSS variables
3. `components/component_specs.*` and `component_inventory.json`
4. `system_spec.md` and `system_ontology.json`
5. External visual references

Visual references are morphology inputs only. They may inform component shape, density, proportion, hierarchy rhythm, and affordance patterns; they must not override tokens, typography, domain IA, product copy, or palette composition.

## Voice And Boundaries
- brand keywords: `agentic`, `operational`, `networked`, `observable`, `governed`, `precise`, `structured`, `calm`, `trustworthy`
- tone: `direct`, `operational`, `audit-friendly`, `low-drama`, `specific`
- visual cues: `trustworthy answer chat with owner and source chips`, `staged routing progress trace`, `owner knowledge authoring workspace`, `staged concept review cards with approve edit reject`, `before-after diff card`, `owner handling inbox with tabs`, `live operations event feed`, `routing outcome status tags`, +4 more
- avoid: `decorative graph`, `marketing-heavy`, `sci-fi glow`, `generic card wall`, `noisy`, `unexplained automation`, `opaque status`, `exposed routing internals to end users`, +1 more

## Color Roles
| Role | Token | Value | Source | Use |
| --- | --- | --- | --- | --- |
| `canvas` | `--ds-color-canvas` | `#F7F8FA` | Canvas White | Generated fallback support color |
| `surface` | `--ds-color-surface` | `#FFFFFF` | Paper | Generated fallback support color |
| `surface_muted` | `--ds-color-surface-muted` | `#EEF1F6` | Surface Muted | Generated fallback support color |
| `surface_elevated` | `--ds-color-surface-elevated` | `#FFFFFF` | Paper | Generated fallback support color |
| `border` | `--ds-color-border` | `#D6DDE6` | Border Neutral | Generated fallback support color |
| `border_strong` | `--ds-color-border-strong` | `#B0BAC7` | Border Strong | Generated fallback support color |
| `ink` | `--ds-color-ink` | `#111111` | Ink | Generated fallback support color |
| `ink_muted` | `--ds-color-ink-muted` | `#4B5563` | Muted Ink | Generated fallback support color |
| `ink_subtle` | `--ds-color-ink-subtle` | `#6B7280` | Subtle Ink | Generated fallback support color |
| `info` | `--ds-color-info` | `#0F4C81` | Classic Blue | 중명도, 중채도, 클래식한 딥 블루 톤 / 신뢰, 평온함, 지성, 안정감 |
| `success` | `--ds-color-success` | `#006A4E` | Bottle Green | 절제됨, 균형, 신뢰, 빈티지, 고급스러움 |
| `warning` | `--ds-color-warning` | `#FFB27F` | Apricot | 밝은 명도, 낮은 채도, 살짝 핑크빛이 도는 부드러운 오렌지 / 따뜻함, 부드러움, 친근함, 여유, 자연스러움 |
| `danger` | `--ds-color-danger` | `#E2725B` | Terracotta | 붉은 흙빛 계열, 오렌지 브라운이 섞인 따뜻한 중채 레드 / 안정감, 따뜻함, 자연스러움, 감성적 |
| `anchor_surface` | `--ds-color-anchor-surface` | `#27503D` | Forest Green | 묵직함, 안정감, 신뢰, 자연적 깊이 |

Color rule: Token binding is necessary but not sufficient. Do not recombine `--ds-*` color roles into a new reference-like palette.

## Typography
| Role | Font | Token | Notes |
| --- | --- | --- | --- |
| heading | Pretendard | `--ds-font-heading` | 한글 UI 서체의 사실상 표준. Apple SD Gothic Neo 기반이지만 더 정교함. 라틴은 Inter 계열. |
| body | Pretendard | `--ds-font-body` | 한글 UI 서체의 사실상 표준. Apple SD Gothic Neo 기반이지만 더 정교함. 라틴은 Inter 계열. |
| mono | Source Code Pro | `--ds-font-mono` | data, code, shortcuts only |
| korean | n/a | `--ds-font-ko` | primary script support |

- type scale: `xs`=12, `sm`=14, `md`=16, `lg`=21, `xl`=28, `2xl`=38, `3xl`=50
- line heights: `tight`=1.2, `normal`=1.5, `comfortable`=1.6, `relaxed`=1.75

## Spacing And Shape
- spacing scale: `0`, `2`, `4`, `8`, `12`, `16`, `24`, `32`, `48`, `64`, `96`
- density bias: `dense`
- radius scale: `none`, `sm`, `md`, `lg`, `xl`, `pill`
- corner bias: `medium`

## Component Priorities
| Family | Priority | States | Components |
| --- | --- | --- | --- |
| button | high | `default`, `hover`, `active`, `disabled`, `loading` | `primary-button`, `secondary-button`, `ghost-button`, `icon-button`, `cta-button`, `link-button`, +1 more |
| copilot-artifact | high | `default`, `loading`, `verified`, `error` | `source-card`, `citation-drawer` |
| copilot-chat | high | `default`, `loading`, `complete`, `error` | `tool-call-trace` |
| data-display | high | `default`, `sorted`, `filtered`, `empty` | `data-table`, `tag`, `column-header`, `row-actions`, `chat-message`, `chat-thread`, +17 more |
| document | high | `default`, `selected`, `commenting`, `resolved` | `article-body`, `table-of-contents`, `heading-anchor`, `prose-block`, `reading-pane`, `footnote`, +3 more |
| editorial | high | `default`, `selected`, `editing` | `editor-canvas`, `editor-toolbar`, `inline-format-menu`, `slash-command-menu`, `block-controls` |

## Signature Components
| Component | Family | Anatomy | Token Binding |
| --- | --- | --- | --- |
| `table-of-contents` | document | parts: container, header, content-area, footer(optional), action(optional); states: default, loading, empty, error | slots: surface, text, border, radius, padding |
| `sidebar-nav` | navigation | parts: container, nav-item, icon(optional), label, indicator(active), +1 more; states: default, hover, active, collapsed | slots: surface, text, padding, font |
| `saved-view-bar` | navigation | parts: tab-list, saved-view-tab, count-badge, overflow-menu, save-action; states: default, active, dirty, overflow | slots: surface, border, radius |
| `decision-record-card` | data-display | parts: card, record-id, decision-summary, actor-row, evidence-links, +1 more; states: draft, recorded, locked, expired | slots: surface, border |
| `data-table` | data-display | parts: container, header, content-area, footer(optional), action(optional); states: default, loading, empty, error | slots: surface, text, border, radius, padding |
| `pagination` | navigation | parts: container, nav-item, icon(optional), label, indicator(active), +1 more; states: default, hover, active, collapsed | slots: surface, text, padding, font |

## Advanced Component Menu
| Component | Use When | Pairs With |
| --- | --- | --- |
| `diff-viewer` | AI rewrites, policy edits, or reviewer changes need auditability; users must approve what changed before publishing | `redline-viewer`, `revision-timeline`, `approval-rail` |
| `exception-queue` | multiple issues require triage, assignment, and resolution; reviewers need to batch handle exceptions | `bulk-action-table`, `policy-matrix`, `approval-rail` |
| `resizable-split-pane` | primary work happens between list, canvas/chat, and detail panels; users need to compare or inspect adjacent information without navigation | `thread-list`, `artifact-preview-panel`, `inspector-drawer` |
| `source-card` | AI output depends on external or internal source records; users need a repeatable citation preview component | `citation-drawer`, `evidence-graph`, `inline-citation` |
| `audit-timeline` | regulated workflows require traceable user and AI actions; reviewers need to reconstruct what happened before approval | `decision-record-card`, `approval-rail`, `tool-call-trace` |
| `evidence-graph` | trust depends on seeing relationships between claims and sources; auditors need to trace why an answer or decision was made | `citation-drawer`, `decision-record-card`, `policy-matrix` |
| `saved-view-bar` | teams revisit the same filtered views often; dense tools need stable scope memory | `filter-builder`, `bulk-action-table`, `exception-queue` |
| `tool-call-trace` | AI actions need explainability or debugging; operators need to audit retrieval, policy checks, or workflow calls | `audit-timeline`, `citation-drawer`, `decision-record-card` |

Use these as ontology-approved building blocks when the workflow calls for richer professional UI. They still inherit token, typography, accessibility, and reference-governance rules.

## Design Context Pack
- activation: `planned`
- providers: `pinterest`=preview, `lazyweb`=suggested
- flow coverage: data-review(covered), messaging(covered), dashboard(covered), navigation(gap)
| Context | Provider | Allowed Use |
| --- | --- | --- |
| trustworthy answer chat with owner attribution and source citations | `lazyweb` | morphology: `evidence`; flows: `messaging` |
| human approval inbox and review queue with status tags | `lazyweb` | morphology: `general-interface-composition`; flows: `data-review`, `messaging` |
| operations event feed dashboard with audit log table dark theme | `lazyweb` | morphology: `dense-table`, `timeline`; flows: `data-review`, `dashboard` |
- research gap `no-observed-screens`: Capture or export 3-8 representative screens before treating morphology guidance as grounded.
- research gap `real-app-corpus-provider-not-connected`: Connect Lazyweb MCP or export selected Lazyweb screens into visual_reference.sources with provenance.

## Reference Governance
- allowed from references: `component morphology`, `layout density`, `panel/card proportions`, `hierarchy rhythm`, `interaction affordance patterns`
- denied from references: `color palette`, `palette composition or derived secondary palettes`, `typography family or scale`, `semantic status colors`, `product copy`, `product data model`, `navigation labels`, `domain information architecture`, `redistributable imagery unless explicitly licensed`
- implementation guardrails:
  - 기존 핵심 화면, 진입점, 작업 흐름은 명시적 승인 없이 제거하거나 숨기지 않음.
  - 전면 셸 리라이트보다 토큰 -> primitive -> feature surface 순서의 점진적 롤아웃을 우선.
  - 새 시각 규칙은 지원 대상 테마와 breakpoint 전체에서 먼저 검증.
  - 일반(light) 모드와 dark 모드를 함께 제공하고, light를 기본 :root 또는 앱 기본값으로 둠.
  - 모바일 320/360/390/430px에서 horizontal scroll 또는 버튼/CTA 잘림이 있으면 완료로 보지 않음.
  - 버튼·CTA·탭·필터칩·툴바 액션은 fixed width/min-width에 의존하지 않고 wrap 또는 stack fallback을 가져야 함.
- visual asset medium selection: Visual asset slots must choose the medium that matches the subject and runtime role; narrative/content media needs high-fidelity raster or approved production artwork, not ad-hoc SVG sketches.
  - medium override `user-raster-asset-directive`: project-local raster image asset.; denied `svg`, `inline svg`, `deterministic svg placeholder`; triggers `SVG 만들지 말고`, `SVG 금지`, `실제 그림파일`, `실제 이미지 파일`, +6 more
  - A user/reviewer sentence such as 'SVG 만들지 말고 실제 그림파일로 만들거나 검색해서 넣어' is a binding medium override, not a preference. Store it in governance/system_ontology/IMPLEMENTATION_CONTRACT and satisfy it before visual QA.
  - When a raster-only/no-SVG directive is active, do not create SVG avatars, inline SVG sprites, SVG favicons, SVG placeholder art, or SVG UI icons for the affected scope; create or source project-local PNG/WebP/JPEG assets instead.
  - Comic, manga, and webtoon cover or panel-preview slots default to image_gen-generated raster, user-supplied artwork, or licensed/sourced artwork.
- failure pattern `token-bound-reference-palette-mixing`: Token binding is necessary but not sufficient; color role composition must still follow the ontology palette roles.
- prevention: Derived colors may alias a semantic token or mix one semantic role with a neutral surface/transparent value. Do not mix multiple chromatic roles to create a local palette.

## Agent Preflight
1. Read `design-system/IMPLEMENTATION_CONTRACT.md` before UI edits.
2. Read this capsule, then `system_spec.md`, `token_schema.json`, and `components/component_specs.md`.
3. Use external references only for morphology and density. Keep colors, fonts, IA, and copy ontology-led.
4. Run implementation lint before calling the screen complete:

```bash
uv run design-ontology lint-implementation --target-repo .
```
