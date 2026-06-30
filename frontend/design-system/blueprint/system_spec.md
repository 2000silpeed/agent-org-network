# Agent Org Network Design System Spec

## 1. Positioning

- **Brand**: Agent Org Network
- **Product**: A question-routing organization layer: a person asks in plain language, the system finds the accountable human owner, that owner answers from their own knowledge, and every answer carries its owner, trust state, and sources — with internal routing hidden from the asker.
- **Audience**: question asker (end user), knowledge owner accountable for a domain, operations monitor, governance and audit reviewer, team lead
- **Platforms**: web, desktop-web
- **Accessibility floor**: WCAG 2.2 AA, full keyboard operation, non-color status encoding

## 2. Identity Guardrails

- **Brand keywords**: agentic, operational, networked, observable, governed, precise, structured, calm, trustworthy
- **Anti-keywords**: decorative graph, marketing-heavy, sci-fi glow, generic card wall, noisy, unexplained automation, opaque status, exposed routing internals to end users, knowledge bodies leaving the owner environment
- **Tone of voice**: direct, operational, audit-friendly, low-drama, specific
- **Visual direction**: trustworthy answer chat with owner and source chips, staged routing progress trace, owner knowledge authoring workspace, staged concept review cards with approve edit reject, before-after diff card, owner handling inbox with tabs, live operations event feed, routing outcome status tags, worker admission panel, audit log table, restrained multi-accent system, dense legible dark console
- **Interaction direction**: ask and receive an accountable answer, watch a staged routing trace resolve, approve edit or reject a staged concept draft, commit and publish reviewed knowledge, settle contested ownership by assigning, review a backup answer first-person, pull document content on demand, follow a live event feed, admit or revoke a worker, trace one question from arrival to answer

## 3. Design Principles

- **Agentic**: `agentic`를 시각적 선택과 인터랙션 선택의 기본 기준으로 삼습니다.
- **Operational**: `operational`를 시각적 선택과 인터랙션 선택의 기본 기준으로 삼습니다.
- **Networked**: `networked`를 시각적 선택과 인터랙션 선택의 기본 기준으로 삼습니다.
- **Observable**: `observable`를 시각적 선택과 인터랙션 선택의 기본 기준으로 삼습니다.

## 4. Foundation Priorities

- **Grid, container, and page rhythm** (high): signal 42
- **Content design and microcopy rules** (high): signal 36
- **Accessibility rules and contrast baseline** (high): signal 35
- **Color tokens and semantic color policy** (high): signal 35
- **Icon family and stroke policy** (high): signal 30

## 5. Token Strategy

- **Layering**: core -> semantic -> component
- **Core categories**: color, spacing, radius, typography, motion, elevation
- **Semantic categories**: surface, text, border, focus, feedback
- **Component categories**: button, input, navigation, overlay, editor
- **Typography families**: brand, text, mono
- **Spacing scale**: 0, 2, 4, 8, 12, 16, 24, 32, 48, 64, 96

### Typography System (auto-resolved)

- **Heading**: Pretendard
- **Body**: Pretendard
- **Mono**: Source Code Pro
- **Product type detected**: editorial
- **Pairing source**: auto-scored
- **Line height**: normal
- **Type scale**: base 16px, ratio 1.333 (xs=12px, sm=14px, md=16px, lg=21px, xl=28px, 2xl=38px, 3xl=50px)
- **Strategy**:
  - 단일 서체(Pretendard)로 weight 대비 위계 — 일관성 우선
  - 모노스페이스: Source Code Pro — 코드/데이터 영역 전용
  - calm 키워드 → comfortable spacing, 과한 weight 대비 지양
  - precise 키워드 → tight letter-spacing, tabular figures 권장
- **Heading note**: 한글 UI 서체의 사실상 표준. Apple SD Gothic Neo 기반이지만 더 정교함. 라틴은 Inter 계열.
- **Body note**: 한글 UI 서체의 사실상 표준. Apple SD Gothic Neo 기반이지만 더 정교함. 라틴은 Inter 계열.
- **Heading tracking**: lg=-0.015em, xl=-0.015em, 2xl=-0.02em, 3xl=-0.02em
- **Loading**: Pretendard(preload), Source Code Pro(lazy) | display: swap

## 6. Color Reference

- **Source**: REFERENCE X Vol.1 - Color Reference (/Users/sungwoon/ai-projects/design-ontology-harness/docs/color-reference.md)
- **Selection mode**: semantic-ontology
- **Preferred families**: Cool Grays, Standard Blues, Natural Greens, Reds
- **Palette strategy**: temperature=mixed, contrast=balanced, diversity=balanced, surface_style=tinted
- **Palette expansion**: supporting_color_count=8, combination_count=3, prefer_pairings=True
- **Active palette**: ontology-best-fit-1
- **Active roles**:
  - `anchor_surface` -> Forest Green #27503D / green.deep
  - `structural_support` -> Bottle Green #006A4E / green.deep
  - `quiet_background` -> Celadon #ACE1AF / green.pastel
  - `calm_border` -> Cascade #76C1B1 / green.pantone_trend
  - `fresh_accent` -> Arcadia #00A591 / green.pantone_trend
- **Selected colors**:
  - Forest Green #27503D / green.deep / 묵직함, 안정감, 신뢰, 자연적 깊이
  - Bottle Green #006A4E / green.deep / 절제됨, 균형, 신뢰, 빈티지, 고급스러움
  - Celadon #ACE1AF / green.pastel / 자연스러움, 절제, 고요, 전통미, 단아함
  - Arcadia #00A591 / green.pantone_trend / 세련, 청량, 도시적, 미래지향, 균형
  - Cascade #76C1B1 / green.pantone_trend / 정제된 청량감, 세련미, 안정감, 포근함
- **Palette candidates**:
  - signature-1 (Signature): primary=Ochre, accent=Apricot, surface_tint=Wheat / Ochre matches brand tone keywords.; Apricot matches brand tone keywords.
  - assertive-3 (Assertive): primary=Classic Blue, accent=Ochre, surface_tint=Apricot / Classic Blue matches brand tone keywords.; Ochre matches brand tone keywords.
- **Expanded supporting colors**:
  - Apricot #FFB27F / Natural Oranges / reference-color / Apricot reinforces the brand mood signals.
  - Wheat #F5DEB3 / Natural Yellows / reference-color / Wheat reinforces the brand mood signals.
  - Ochre #CC7722 / Standard Oranges / reference-color / Ochre reinforces the brand mood signals.
  - Terracotta #E2725B / Natural Reds / reference-color / Terracotta reinforces the brand mood signals.
  - Classic Blue #0F4C81 / Pantone Trend Blues / reference-color / Classic Blue reinforces the brand mood signals.
  - Lavender Violet #967BB6 / Natural Violets / reference-color / Lavender Violet reinforces the brand mood signals.
  - Navy Blue #000080 / Deep Blues / reference-color / Navy Blue reinforces the brand mood signals.
  - Celadon #ACE1AF / Pastel Greens / reference-color / Celadon reinforces the brand mood signals.
- **Expanded semantic roles**:
  - `canvas` -> Canvas White #F7F8FA / Generated Fallback
  - `surface` -> Paper #FFFFFF / Generated Fallback
  - `surface_muted` -> Surface Muted #EEF1F6 / Generated Fallback
  - `surface_elevated` -> Paper #FFFFFF / Generated Fallback
  - `border` -> Border Neutral #D6DDE6 / Generated Fallback
  - `border_strong` -> Border Strong #B0BAC7 / Generated Fallback
  - `ink` -> Ink #111111 / Generated Fallback
  - `ink_muted` -> Muted Ink #4B5563 / Generated Fallback
  - `ink_subtle` -> Subtle Ink #6B7280 / Generated Fallback
  - `ink_inverse` -> Ink Inverse #FFFFFF / Generated Fallback
- **Combination lists**:
  - Seed Core: anchor_surface=Forest Green, structural_support=Bottle Green, quiet_background=Celadon, calm_border=Cascade, fresh_accent=Arcadia
  - Surface System: canvas=Canvas White, surface=Paper, border=Border Neutral, ink=Ink, ink_muted=Muted Ink
  - Support Spectrum: support=Apricot, support=Wheat, support=Ochre, support=Terracotta, support=Classic Blue, support=Lavender Violet
- **Semantic color selection**: ontology-search-per-run
  - matched pattern: `pattern-brief-palette-stable-green-ui-surface` / 안정적인 그린 서비스 UI 팔레트 후보
- **Ontology-searched candidate palettes**:
  - ontology-best-fit-1 (Best Fit, score=205.0): anchor_surface=Forest Green, structural_support=Bottle Green, quiet_background=Celadon, calm_border=Cascade, fresh_accent=Arcadia
  - ontology-quiet-surface-2 (Quiet Surface, score=158.45): anchor_surface=Bottle Green, structural_support=Celadon, quiet_background=Periwinkle, calm_border=Buttercream, fresh_accent=Chili Oil
  - ontology-clear-structure-3 (Clear Structure, score=163.7): anchor_surface=Moss Green, structural_support=Celadon, quiet_background=Grape Compote, calm_border=Prussian Blue, fresh_accent=Arcadia
  - ontology-strong-signal-4 (Strong Signal, score=161.85): anchor_surface=Hunter Green, structural_support=Celadon, quiet_background=Moss Green, calm_border=Cascade, fresh_accent=Forest Green
  - ontology-cross-family-5 (Cross Family, score=163.2): anchor_surface=Mint Green, structural_support=Prussian Blue, quiet_background=Celadon, calm_border=Periwinkle, fresh_accent=Forest Green
- **Selection rules**:
  - Search Semantic OS ColorPattern and ColorKeyword nodes for every app brief.
  - Do not ship pre-authored palette sets as fixed presets.
  - Use ColorPattern role language as a role model, not as a copied palette table.
  - Publish colors as role, reason, caveat, and proof conditions.
- **Semantic color ontology**: 137 nodes / 487 edges from semantic-os
- **Matched color keywords**:
  - `anchor_surface` -> Forest Green #27503D / green.deep / mood=묵직함, 안정감, 신뢰 / axes=low_value, high_chroma
  - `structural_support` -> Bottle Green #006A4E / green.deep / mood=절제됨, 균형, 신뢰 / axes=low_value, mid_chroma, low_chroma
  - `quiet_background` -> Celadon #ACE1AF / green.pastel / mood=자연스러움, 절제, 고요 / axes=mid_value, low_chroma, pastel
  - `calm_border` -> Cascade #76C1B1 / green.pantone_trend / mood=정제된 청량감, 세련미, 안정감 / axes=mid_value, low_chroma, cool_bias
  - `fresh_accent` -> Arcadia #00A591 / green.pantone_trend / mood=세련, 청량, 도시적 / axes=mid_value, mid_chroma, cool_bias
- **Ontology keyword recommendations**:
  - Peach Puff #FFDAB9 / orange.pastel / matches 'low_chroma'; matches 'reference'
  - Chili Oil #944537 / orange.pantone_trend / matches 'low_chroma'; matches 'reference'
  - Autumn Blaze #D1933F / yellow.pantone_trend / matches 'reference'; matches '자연스러움'
  - Salmon #FA8072 / red.pastel / matches 'reference'; matches 'pastel'
  - Wheat #F5DEB3 / yellow.natural / matches 'low_chroma'; matches 'reference'
- **Semantic color guardrails**:
  - RGB와 CMYK는 산출 매체 기준으로 분리한다: 디지털 산출물은 RGB/sRGB 기준, 인쇄 산출물은 CMYK/프로파일 기준으로 판단한다. 화면색을 인쇄색으로 그대로 기대하지 않는다.
  - 색상 수치는 절댓값이 아니라 기준값이다: 디스플레이와 출력 환경에 따라 색 차이가 생기므로 HEX/CMYK는 재현 기준점으로 쓰고, 최종 판단은 매체별 proof에서 확인한다.
  - 팔레트 확장 전 mood tag를 먼저 잠근다: 무드보드나 브랜드 컬러를 늘리기 전에 에너지, 안정, 깊이, 회복 같은 감정 태그를 먼저 고정하면 색상 후보가 흔들리지 않는다.
  - 배색표를 재구성할 수 있는 수준의 pair 목록은 만들지 않는다: paid source의 조합표, 페이지 배열, 순서, 행/열 구조를 그대로 되살릴 수 있는 palette_pair edge 묶음은 온톨로지에 넣지 않는다.
  - palette_pair edge는 변형된 brief와 함께만 추가한다: 두 색의 직접 pair edge는 브랜드/화면/인쇄/제품 같은 새로운 산출 맥락에서 역할, 대비, 위험, proof 조건이 함께 설명될 때만 추가한다.
- **Notes**: Base surfaces should stay neutral and readable under dense operational data., Use blue for primary focus and selection, green for healthy automation, red for risk and blocked handoffs., Avoid a single purple or blue gradient identity; the product needs role, risk, and ownership distinction.
- **Application rule**: 레퍼런스 컬러는 semantic token으로 번역해서 사용하고, 접근성과 theme 호환성을 우선합니다.

## 7. Visual Reference Signals

- **Mode**: local-images
- **Coverage**: source 0 / image 0 / selected 0
- **Rule**: visual references are advisory signals for motif and layout direction; official KB/spec remain the structural source of truth.
- **Provenance**: `observed` = directly measured from local pixels, `inferred` = synthesized from image/query/brand signals, `unverified` = reliable visual evidence not yet available.
- **Query seeds**: trustworthy answer chat with owner attribution and source citations, human approval inbox and review queue with status tags, operations event feed dashboard with audit log table dark theme
### Visual Direction

- **Density**: dense (confidence 0.52, provenance inferred) / dashboard x0.45, table x3.25, control x1.8
- **Surface Style**: elevated (confidence 0.15, provenance inferred) / modal x0.9
- **Corner Style**: medium (confidence 0.28, provenance inferred) / card x5.4
- **Typography Mood**: utilitarian (confidence 0.33, provenance inferred) / dashboard x0.45, control x1.8, table x3.25
- **Color balance**: temperature=unknown, contrast=unknown, neutral_bias=unknown, provenance=unverified

### Layout Rhythm

- **Data review surface**: confidence 0.52 / provenance inferred / table x3.25, audit x3.25, log x2.15, data x0.9
- **Editorial feed**: confidence 0.52 / provenance inferred / content x0.45, feed x3.7, review x4.4, publish x0.45
- **Conversation side panel**: confidence 0.46 / provenance inferred / chat x2.15, thread x0.9, inbox x3.0500000000000003
- **Dashboard grid**: confidence 0.42 / provenance inferred / dashboard x0.45, metric x0.9, table x3.25

### Image-derived Component Hints

- **Cards**: raised cards with restrained depth를 기본으로 하고, 압축된 spacing과 얇은 divider 중심의 hierarchy. 중간 반경으로 제품 UI 절제 유지. / provenance inferred / surface=elevated, density=dense, corner=medium
- **Navigation**: navigation은 compact하게 유지하되 filter/scope controls와 혼합하지 않는다. / provenance inferred / Data review surface
- **Typography**: 정보 밀도에 맞춰 type scale 차이를 줄이고 table/list label의 정렬 정확도를 우선한다. / provenance inferred / typography_mood=utilitarian
- **Data Display**: 정보 밀도를 유지하되 thin dividers와 restrained accent로 hierarchy를 만든다. / provenance inferred / layout=dashboard-grid, density=dense

### Synthesis Notes

- layout는 Data review surface 기준으로 정리
- surface language는 elevated 성향 우선
- typography mood는 utilitarian 축 유지
- density는 dense 기준으로 primitive spacing 조정
- palette temperature는 unknown 쪽을 우선
- Avoid: decorative network graph without labels
- Avoid: abstract AI glow background
- Avoid: unexplained autonomous action
- Avoid: internal routing scores exposed to the end user

### Design Context Pack

- **Activation**: planned
- **Schema**: design-context-pack/v1
- **Rule**: Reference context is research input only; it never overrides product IA, tokens, component specs, or governance.
- **Providers**:
  - `pinterest`: preview / manual-or-playwright-capture / search assist and shortlist support
  - `lazyweb`: suggested / mcp-or-manual-export / real app flow and screen corpus provider
- **Flow coverage**:
  - data-review: covered (2 context cards; lazyweb)
  - messaging: covered (2 context cards; lazyweb)
  - dashboard: covered (1 context cards; lazyweb)
  - navigation: gap (0 context cards; no selected provider evidence)
- **Context cards**:
  - `research-query-01` (research-query, planned): trustworthy answer chat with owner attribution and source citations / flows: messaging / morphology: evidence
  - `research-query-02` (research-query, planned): human approval inbox and review queue with status tags / flows: data-review, messaging / morphology: general-interface-composition
  - `research-query-03` (research-query, planned): operations event feed dashboard with audit log table dark theme / flows: data-review, dashboard / morphology: dense-table, timeline
- **Research gaps**:
  - no-observed-screens (high): Capture or export 3-8 representative screens before treating morphology guidance as grounded.
  - real-app-corpus-provider-not-connected (medium): Connect Lazyweb MCP or export selected Lazyweb screens into visual_reference.sources with provenance.
  - flow-coverage-gaps (medium): Search corpus/provider screens by these flows before mock generation.

## 8. Component Strategy

- **Product primitives**: answer chat thread, question and answer bubble, trust state chip, source chip, owner chip, staged routing progress trace, staged pipeline stepper, concept draft card, approve edit reject disposition control, before-after diff card, owner inbox tabs, contested case card, candidate coverage list, on-demand document pull, assign owner control, backup answer review card, re-review queue row, metric summary card, live event feed, routing outcome status tag, worker admission panel, audit log table, light org reference map, data tables, search and filter, workspace navigation, modal and dialog, notifications, onboarding and stepper, forms
- **Required families**: button, copilot-artifact, copilot-chat, data-display, document, editorial, feedback, input, layout, navigation, foundation, overlay
- **Advanced component recommendations**:

- **diff-viewer** (document, score 16): AI rewrites, policy edits, or reviewer changes need auditability; users must approve what changed before publishing / pairs with: redline-viewer, revision-timeline, approval-rail
- **exception-queue** (data-display, score 16): multiple issues require triage, assignment, and resolution; reviewers need to batch handle exceptions / pairs with: bulk-action-table, policy-matrix, approval-rail
- **resizable-split-pane** (layout, score 16): primary work happens between list, canvas/chat, and detail panels; users need to compare or inspect adjacent information without navigation / pairs with: thread-list, artifact-preview-panel, inspector-drawer
- **source-card** (copilot-artifact, score 16): AI output depends on external or internal source records; users need a repeatable citation preview component / pairs with: citation-drawer, evidence-graph, inline-citation
- **audit-timeline** (data-display, score 14): regulated workflows require traceable user and AI actions; reviewers need to reconstruct what happened before approval / pairs with: decision-record-card, approval-rail, tool-call-trace
- **evidence-graph** (data-display, score 14): trust depends on seeing relationships between claims and sources; auditors need to trace why an answer or decision was made / pairs with: citation-drawer, decision-record-card, policy-matrix
- **saved-view-bar** (navigation, score 14): teams revisit the same filtered views often; dense tools need stable scope memory / pairs with: filter-builder, bulk-action-table, exception-queue
- **tool-call-trace** (copilot-chat, score 14): AI actions need explainability or debugging; operators need to audit retrieval, policy checks, or workflow calls / pairs with: audit-timeline, citation-drawer, decision-record-card

- **Visual-reference archetypes**:

- **Data review table** (data-display / 0.48): data-table, column-header, row-actions, filter-toolbar, pagination
- **Review coverage system** (editorial / 0.48): review-card, score-badge, comparison-table, ranking-list, filter-chip
- **Workspace shell** (navigation / 0.48): app-shell, sidebar-nav, workspace-switcher, breadcrumb, context-panel
- **Conversation sidecar** (overlay / 0.43): chat-panel, message-thread, message-composer, context-drawer
- **Dashboard insight module** (data-display / 0.33): stat-card, insight-card, chart-panel, section-header, filter-chip

- **button**: primary-button, secondary-button, ghost-button, icon-button, cta-button, link-button, form-actions
- **copilot-artifact**: source-card, citation-drawer
- **copilot-chat**: tool-call-trace
- **data-display**: data-table, tag, column-header, row-actions, chat-message, chat-thread, chart-container, chart-legend
- **document**: article-body, table-of-contents, heading-anchor, prose-block, reading-pane, footnote, link-card, diff-viewer
- **editorial**: editor-canvas, editor-toolbar, inline-format-menu, slash-command-menu, block-controls
- **feedback**: filter-chip, toast, inline-alert, empty-state, banner, status-badge, step-progress
- **input**: text-field, select, checkbox, radio, textarea, search-field, segmented-control, chip
- **layout**: resizable-split-pane
- **navigation**: sidebar-nav, topbar, breadcrumb, workspace-switcher, mobile-topbar, mobile-tab-bar, back-button, section-tabs
- **foundation**: column-header, row-actions, pagination, app-shell, form-section
- **overlay**: bottom-sheet, modal-dialog, chart-tooltip, user-menu, mention-popup, tooltip-guide

## 9. Implementation Guardrails

- 기존 핵심 화면, 진입점, 작업 흐름은 명시적 승인 없이 제거하거나 숨기지 않음
- 전면 셸 리라이트보다 토큰 -> primitive -> feature surface 순서의 점진적 롤아웃을 우선
- 새 시각 규칙은 지원 대상 테마와 breakpoint 전체에서 먼저 검증
- 일반(light) 모드와 dark 모드를 함께 제공하고, light를 기본 :root 또는 앱 기본값으로 둠
- 모바일 320/360/390/430px에서 horizontal scroll 또는 버튼/CTA 잘림이 있으면 완료로 보지 않음
- 버튼·CTA·탭·필터칩·툴바 액션은 fixed width/min-width에 의존하지 않고 wrap 또는 stack fallback을 가져야 함
- padded container 안에서 width: 100vw를 쓰지 않음 — width: 100%, max-width: 100%, documented full-bleed 패턴을 우선
- 기존 데이터 밀도와 업무 완료 경로를 유지한 상태에서 시각 품질을 높이는 방향을 우선
- 기능 위치 변경, 정보 구조 변경, 패널 제거는 별도의 migration plan이 있을 때만 수행
- 레퍼런스는 형태·밀도·컴포넌트 비례만 흡수하고, 색 조합·폰트 스케일·도메인 IA는 토큰과 제품 온톨로지를 따른다
- 토큰을 사용하더라도 status/tint/info 역할을 섞어 레퍼런스처럼 보이는 새 팔레트를 만들지 않는다
- 구현 중 사용자·리뷰어가 반복 가능한 실패 패턴을 지적하면 현재 화면 수정에 그치지 않고 governance/contract/linter로 승격한다
- 상용 제품형 화면은 피치덱식 히어로/균일 카드벽보다 실제 작업 표면, 데이터 밀도, 상태, 필터, 출처를 첫 화면에 우선 배치한다
- 데이터·스포츠·운영 UI에서 정확한 수치, 예측, 순위, 투표수는 출처/업데이트 시각/샘플 라벨 없이 확정값처럼 보이게 하지 않는다
- 사이트·앱·랜딩·제품·장소·콘텐츠·게임 목업은 도메인 실체를 보여주는 이미지/미디어/identity asset을 적극적으로 사용하고, 이미지 없는 카드·그라디언트만으로 완성 처리하지 않는다
- 만화·웹툰·잡지 표지, 컷 미리보기, 서사 콘텐츠 미디어 슬롯은 image_gen/사용자 제공/라이선스 소스/승인된 고품질 아트워크를 기본값으로 삼고, 즉석 SVG 스케치나 기하학 플레이스홀더를 최종 자산으로 쓰지 않는다
- 사용자·리뷰어가 'SVG 만들지 말고', '실제 그림파일', 'PNG/WebP/JPEG', '검색해서 넣어'처럼 매체를 지정하면 해당 범위는 raster-only medium override로 기록하고 SVG/inline vector/아이콘 스프라이트로 대체하지 않는다
- 생성 이미지와 장식 비주얼은 도메인 맥락을 보조해야 하며 일정, 결과, 표, 필터, 상태 같은 핵심 작업 표면을 압도하지 않는다
- Codex image_gen이 실패하거나 실제 사진성이 더 중요해 sourced visual fallback을 사용할 때는 라이선스/저작자/출처/attribution/sha256을 manifest에 기록하고 프로젝트 에셋으로 복사한 뒤 사용한다
- 유료 stock provider는 구매·구독·프로젝트 라이선스 증빙이 없으면 구현 에셋으로 승격하지 않고, reference-only provider는 형태·밀도·flow 참고로만 사용한다
- 라이선스 메타데이터가 없는 검색 이미지를 사용하지 않고, 런타임 코드가 원격 검색/CDN URL을 hotlink하지 않는다
- 아이콘 자리에 이모지(🎨 ✅ 🔥 등)를 넣지 않음 — 리팩토링 중 발견하면 SVG 파일/아이콘 컴포넌트 또는 아이콘 라이브러리로 교체
- favicon, 앱 셸 브랜드 마크, 웹 manifest에는 브랜드 특정 앱 아이콘을 사용하고 일반 이니셜 타일을 최종 아이콘으로 남기지 않음
- 컴포넌트는 component_specs.md의 anatomy/states/token binding을 그대로 따라 완전히 구현
- 'TODO 컴포넌트', '임시 버튼', '플레이스홀더 카드' 같은 반쪽 구현을 남기지 않음

### Color Mode Parity

- **Rule**: Every generated or refactored product UI must ship a normal light mode and a dark mode unless the user explicitly requests a single-mode artifact.
- **Required modes**: light, dark
- **Default mode**: light
- **Implementation rules**:
- Use light mode as the default :root or app-default token set; dark mode must be an override such as [data-theme="dark"].
- Do not build dark-only surfaces for dashboards, tools, landing pages, or prototypes unless explicitly requested.
- Every semantic surface/text/border/accent role needs a light and dark value or a documented derivation.
- Theme toggles, preview links, screenshots, or QA scripts must verify both modes when the implementation has a UI shell.
- Do not solve dark mode by inverting the entire page; define mode-specific semantic tokens and keep imagery/icons legible in both modes.
- **Promoted color mode failure patterns**:
- **dark-only-implementation**: Normal light mode is required alongside dark mode. Prevention: Define :root light tokens, add [data-theme="dark"] overrides, and verify both modes before completion.
- **theme-token-drift**: Mode values must map through the same semantic token roles. Prevention: Keep mode differences inside artifact token files such as design-system/tokens.css; components should consume the same semantic variables in both modes.

### Responsive Resilience

- **Viewport contract**: verify 320px, 360px, 390px, 430px, 768px, 1024px, 1440px.
- **Pass condition**: document.documentElement.scrollWidth <= window.innerWidth and all primary controls remain reachable without horizontal scrolling.
- **Control rules**:
- Buttons, CTA groups, tabs, filter chips, and toolbar actions must not rely on fixed px widths or mobile-hostile min-width values.
- Every button-like control needs max-inline-size: 100%; controls inside flex/grid parents need min-inline-size: 0 so labels can shrink or wrap.
- Action rows must wrap or stack at narrow widths; two-button rows need a <=480px fallback before implementation is complete.
- Long Korean CTA labels must be tested with real copy. Prefer wrapping/stacking over clipping, overflow hidden, or forcing white-space: nowrap.
- Horizontal rails, tickers, score strips, and carousels must not reveal partially clipped text. Either size cards so visible items are complete, or use compact labels such as icon+code in the rail and move full names to the detailed surface.
- Do not use width: 100vw inside padded containers; it commonly creates horizontal scroll. Use width: 100%, max-width: 100%, or a documented full-bleed pattern.
- Never hide body overflow-x as the fix for a layout bug; remove the overflowing width/min-width instead.
- **Promoted responsive failure patterns**:
- **mobile-control-overflow**: Controls must fit, wrap, or stack within their container at mobile widths. Prevention: Ban fixed/min-width px sizing on button-like controls unless paired with max-inline-size: 100%, min-inline-size: 0, and a mobile wrap/stack fallback.
- **viewport-horizontal-overflow**: No generated screen is complete while scrollWidth exceeds innerWidth on supported mobile viewports. Prevention: Avoid 100vw in padded containers, use minmax(0, 1fr) for grids, set min-width: 0 on flex/grid children, and verify 320/360/390/430px screenshots.
- **horizontal-rail-label-clipping**: Scrollable rails may hint that more content exists, but visible text inside each rendered item must be complete and legible. Prevention: Use whole-card column math at tablet/desktop breakpoints; at mobile widths show one full card or remove full names from the rail. Split dense scan labels from descriptive names, and preserve full names in aria-labels or detail panels.

### Emoji-to-SVG Refactor

- **Rule**: During UI refactors, emoji-looking UI affordances must be replaced with SVG-based icons instead of preserved as text glyphs.
- **Targets**: button, card, badge, tab, navigation item, status indicator, empty state, toast, banner
- **Replacement order**:
- If the user/reviewer has declared a raster-only or no-SVG medium override, use approved PNG/WebP/JPEG icon/image assets and document the override.
- Use the project's existing icon library when one is already installed and stylistically compatible.
- Reuse existing local SVG/icon components when available.
- Create a simple local SVG file or SVG component when no suitable icon exists.
- **Quality floor**: UI icons must come from an approved icon system or document a consistent icon grammar; arbitrary hand-drawn path sets are not acceptable just because they are SVG.
- consistent 24px grid or documented asset grid
- consistent stroke weight, caps, joins, and optical size
- currentColor or token-bound stroke/fill
- visible text or aria-label for semantic controls
- no mixed filled/outlined styles unless the state model requires it
- **Implementation rules**:
- Keep SVG stroke/fill bound to currentColor or design tokens, not hard-coded palette values.
- Decorative SVG icons use aria-hidden="true"; semantic icons get an accessible label or adjacent text.
- Store new SVG assets in the nearest existing icons/assets directory; create a minimal icons directory only when none exists.
- Do not replace user-generated emoji content, chat text, blog body, or emoji-picker data.
- Do not use emoji as a placeholder while searching for a proper icon.
- Do not use SVG icons when a project-specific medium override says no SVG or requires real raster image files; use project-local PNG/WebP/JPEG icon assets instead.
- Do not hand-roll a UI icon sprite when a suitable icon library exists; if a local sprite is used, declare its source or approved custom grammar.
- Navigation, toolbar, status, and action icons must share one visual grammar across stroke width, corner style, optical size, and active/inactive treatment.
- **Promoted icon failure patterns**:
- **emoji-ui-affordance**: UI affordances must use SVG files/components or an approved icon library, never emoji glyphs. Prevention: Replace the emoji with an appropriate existing icon, imported icon, or locally authored SVG with token-bound color and accessible semantics.
- **icon-starved-control-surface**: Icons are part of scanability for controls and state, not optional decoration. Prevention: Add token-bound SVG icons to filters, actions, status, and repeated scan surfaces while keeping accessible text labels.
- **amateur-custom-svg-icon-set**: SVG is a file format, not a quality guarantee; UI icons must use an approved icon system or a documented custom grammar. Prevention: Replace the handmade sprite with Lucide/Heroicons/Phosphor/Tabler/Material or mark and document an approved custom icon set with consistent stroke, caps, joins, sizing, and accessibility.

### Brand App Icon Identity

- **Rule**: Every app or website implementation must include a brand-specific app icon identity asset; generic initial-letter tiles are not acceptable as final app icons.
- **Required assets**:
- **Brand app icon**: targets favicon, app shell brand mark, web app manifest, mobile home-screen icon; formats source asset (SVG by default; PNG/WebP/JPEG when a no-SVG medium override is active), favicon, web app manifest icon when applicable
- **Implementation rules**:
- Do not ship a plain initials tile such as WC, AI, DS, or App as the final app icon unless the brand system explicitly defines that lettermark.
- The app icon must use the brand palette, visual keywords, and product primitives as evidence for shape language.
- Use a deterministic SVG source for the primary app icon; generated raster imagery may support marketing visuals but must not replace the identity icon source.
- If the user/reviewer explicitly forbids SVG or requires actual raster image files, the app icon identity source may be PNG/WebP/JPEG instead; record that medium override in the ontology and do not create an SVG fallback silently.
- Wire the app icon into favicon/link metadata and the visible app-shell brand mark when the implementation has one.
- Keep small-size legibility: the icon must remain recognizable at 32px and in a 44px navigation mark.
- The app icon must read as a finished identity mark, not a rough illustration, generic tile, emoji-like sticker, or low-confidence geometric placeholder.
- **Promoted app icon failure patterns**:
- **generic-initials-app-icon**: App icons are required brand identity assets, not temporary text badges. Prevention: Create or reuse a brand-specific SVG app icon, wire it to favicon/manifest/app-shell surfaces, and document it in the ontology.
- **low-quality-app-icon-identity**: App icons are identity assets and must meet a finished visual quality floor. Prevention: Redesign the app icon as a compact, legible, brand-specific SVG mark with coherent geometry, palette, and small-size testing.

### Mockup Visual Substance

- **Rule**: Commercial mockups should use meaningful visual assets by default; image-free screens are incomplete when the product, content, place, object, or story needs visual substance.
- **Applies to**: website mockup, landing page, product page, commerce, editorial/content surface, portfolio, venue/place page, sports hub, travel/food/real-estate, game or interactive experience, empty state/onboarding
- **Why image-free mockups fail**:
- Image-free mockups often look unfinished because cards, hero sections, editorial modules, and content surfaces have no concrete subject matter.
- Gradient blocks, abstract blobs, and homogeneous placeholder panels read as AI-generated polish rather than a real product or brand surface.
- Professional sites usually reveal an actual product, place, person, object, state, gameplay, article subject, or brand identity asset early in the experience.
- **Required visual substance signals**:
- at least one relevant visual asset when the first viewport is a landing, brand, product, venue, editorial, portfolio, game, or content-led surface
- real content thumbnails or product/place/object imagery where repeated cards represent visual entities
- image_gen, sourced visual fallback, user-supplied assets, or deterministic SVG identity assets selected according to the visual asset acquisition contract
- deterministic inline SVG visuals include visible labels/legends or title/desc plus data-subject anchors when they represent places, products, diagrams, maps, or scenes
- manifest entry with acquisition_mode, asset_path, intended_for, alt_text, sha256, crop/focal notes when applicable
- responsive crop and light/dark legibility verified by screenshots or DOM checks
- **Image acquisition order**:
- Use user-supplied licensed imagery when provided and relevant.
- Use Codex image_gen for brand-specific synthetic raster imagery.
- Use sourced visual fallback when real-world photography is more appropriate or image_gen is unavailable.
- Use deterministic SVG/identity assets for app icons, logos, flags, diagrams, and UI glyphs.
- **Implementation rules**:
- Do not ship a commercial website/app mockup with only text, bordered cards, gradients, and empty media placeholders when the domain naturally needs imagery.
- Hero, product, venue, editorial, portfolio, and game surfaces need a concrete visual subject, not a purely atmospheric background.
- Repeated content cards should use thumbnails or compact visual identity when the item represents a place, person, product, match, article, media, or object.
- Empty states and onboarding panels can use illustration, but the illustration must clarify the product state rather than decorate a blank panel.
- Path-only inline SVGs with generic map/sketch/illustration classes do not count as visual substance unless the visual is semantically anchored with labels, legend, title/desc, or data-subject landmarks.
- Do not invent rough hand-drawn scene illustrations inside implementation code as a substitute for product visuals; use image_gen, sourced/user-supplied assets, approved assets, or polished product schematics.
- Mockups must declare the real app representation for visual surfaces: map SDK/tile layer, generated or sourced media, chart/table, data visualization, or explicit loading/empty state.
- Do not represent evidence maps, relation maps, or product data graphs as hand-positioned HTML nodes connected by rotated CSS lines. Use a real graph/chart library, SVG/canvas visualization with semantic labels and runtime data, or a ledger/table when the relationship is simple.
- A media/photo runtime surface is not complete when it is only CSS gradients or texture patterns; it must bind an image/video asset or show an explicit empty/loading state.
- Operational dashboards, sports/data products, and tools may keep imagery secondary, but should still use domain visuals such as app icons, team/flag identity, venue thumbnails, product objects, or editorial context where they add credibility.
- Do not let images obscure Korean text or controls; define stable aspect ratios, object-fit/object-position, and mobile crop behavior.
- Every integrated raster image must be represented in the visual asset manifest before product code references it.
- **Promoted visual substance failure patterns**:
- **image-free-commercial-mockup**: Visual substance is part of mockup completeness, not optional decoration. Prevention: Add relevant generated, sourced, user-supplied, or deterministic visual assets and record them in the manifest before calling the mockup complete.
- **placeholder-gradient-as-image**: A visual slot must reveal the actual product, place, object, state, content, or brand identity. Prevention: Replace placeholder media with image_gen, sourced, user-supplied, or deterministic SVG assets that match the domain and slot.
- **low-information-inline-svg-visual**: Deterministic SVG visuals need semantic anchors; otherwise they are decorative placeholders, not visual substance. Prevention: Add visible labels, legend, title/desc, and data-subject landmarks, or replace the slot with a stronger generated, sourced, or user-supplied asset.
- **amateur-ad-hoc-illustration**: A bad drawing does not become acceptable because it is labeled; low-confidence illustration should be removed or replaced. Prevention: Use image_gen, a sourced/user-supplied asset, a reference-backed illustration, or a clean product schematic/data visualization. Do not ship rough path art as visual substance.
- **ambiguous-mock-runtime-surface**: Even a mockup must make the production representation legible. Prevention: Mark the surface as a map SDK layer, generated/sourced media, chart/table, product schematic, or explicit loading/empty state; avoid ambiguous decorative stand-ins.
- **ad-hoc-node-link-placeholder-graph**: A hand-drawn node-link sketch is not a product-grade data visualization. Prevention: Use a proven graph/chart library, a semantically labeled SVG/canvas visualization backed by real runtime data, or replace simple relationships with an evidence ledger/table.
- **media-runtime-surface-without-asset**: Runtime media surfaces need actual media assets or explicit empty/loading states. Prevention: Bind a generated, sourced, or user-supplied image/video asset with alt text and manifest metadata, or render a clear empty/loading state instead of fake visual texture.
- **media-tile-without-asset**: Every visible media/evidence tile needs its own asset or an explicit empty/loading/pending state. Prevention: Attach generated, sourced, or user-supplied media to each tile, or mark the tile as an intentional empty/loading/pending state with clear copy.
- **unmanifested-mockup-image**: Integrated raster assets must be traceable. Prevention: Record acquisition_mode, asset_path, intended_for, alt_text, sha256, and source/prompt metadata before wiring the asset.

### Visual Asset Medium Selection

- **Rule**: Visual asset slots must choose the medium that matches the subject and runtime role; narrative/content media needs high-fidelity raster or approved production artwork, not ad-hoc SVG sketches.
- **Directive overrides**:
- **user-raster-asset-directive**: priority highest; required project-local raster image asset; denied svg, inline svg, deterministic svg placeholder; triggers SVG 만들지 말고, SVG 금지, 실제 그림파일, 실제 이미지 파일, 검색해서 넣어, PNG
- **Decision sequence**:
- First honor explicit user/reviewer medium directives. A no-SVG or raster-only directive overrides default identity/icon/vector preferences for the affected project or slot.
- Classify the slot before drawing: identity/icon, control glyph, diagram/data, factual real-world media, narrative/content media, or decorative support.
- If the slot's user expectation is rendered content art, product/place photography, story atmosphere, or inspectable media, use image_gen, user-supplied licensed imagery, sourced licensed imagery, or an already approved high-fidelity asset.
- Use deterministic SVG for app icons, logos, flags, UI glyphs, charts, diagrams, maps, schematics, and semantic product illustrations where vector geometry is the correct runtime representation.
- When a faster-to-author SVG would reduce the slot to a placeholder, treat that as a wrong-medium failure rather than a stylistic option.
- **Slot family defaults**:
- **high-fidelity-narrative-media**: modes image_gen, user_supplied, sourced; examples comic/manga/webtoon cover, panel or strip preview, story or character scene, editorial/article cover, gameplay or sprite-like scene, portfolio/content artwork; SVG: denied unless the project already has approved production-grade vector artwork for that exact content slot
- **factual-real-world-media**: modes user_supplied, sourced; examples real venue, real product, food/travel/place photo, person or event photo; SVG: allowed only for maps, diagrams, or clearly labeled schematics, not as a photo substitute
- **identity-control-technical-vector**: modes deterministic_svg, icon_library, semantic_html_css; examples app icon, logo, favicon, flag, UI icon, chart, diagram, map schematic; SVG: preferred when token-bound, accessible, and semantically anchored
- **user-specified-raster-assets**: modes image_gen, user_supplied, sourced, project_local_raster; examples AI avatar, chat character portrait, generated app visual, search/sourced image slot, raster-only UI icon set; SVG: denied whenever the user/reviewer says no SVG, real image file, raster-only, or asks to search/generate actual imagery
- **Implementation rules**:
- A user/reviewer sentence such as 'SVG 만들지 말고 실제 그림파일로 만들거나 검색해서 넣어' is a binding medium override, not a preference. Store it in governance/system_ontology/IMPLEMENTATION_CONTRACT and satisfy it before visual QA.
- When a raster-only/no-SVG directive is active, do not create SVG avatars, inline SVG sprites, SVG favicons, SVG placeholder art, or SVG UI icons for the affected scope; create or source project-local PNG/WebP/JPEG assets instead.
- Comic, manga, and webtoon cover or panel-preview slots default to image_gen-generated raster, user-supplied artwork, or licensed/sourced artwork.
- A geometric SVG, rough path drawing, or low-information vector placeholder is not an acceptable final comic cover, manga panel, article cover, product photo, or story media asset.
- Do not substitute inline SVG scene art solely because it is faster to author; use the imagegen skill when synthetic art is appropriate and available.
- Deterministic SVG remains appropriate for app icons, logos, flags, UI icons, charts, diagrams, maps, and product schematics when those are the actual runtime medium.
- If a narrative/content media slot intentionally uses vector artwork, document why it is production-grade artwork rather than a placeholder and record it in the manifest or implementation notes.
- Manifest or implementation records should include acquisition_mode and medium_decision for non-obvious visual slots.
- **Promoted medium failure patterns**:
- **wrong-medium-svg-for-narrative-media**: Narrative/content media slots require the medium users expect to inspect: generated, sourced, user-supplied, or approved polished artwork. Prevention: Use image_gen or licensed/user-supplied raster artwork for the slot; reserve deterministic SVG for identity, controls, diagrams, maps, charts, or schematics.
- **user-raster-directive-svg-violation**: Explicit user/reviewer raster directives override default SVG icon and identity guidance. Prevention: Replace SVG assets with project-local PNG/WebP/JPEG files generated, user-supplied, or license-verified from search; record medium_decision and verify the implementation has no .svg or inline <svg> references.
- **comic-cover-as-geometric-placeholder**: Comic media quality is a product signal; placeholder geometry does not satisfy a comic/content asset slot. Prevention: Generate or source finished cover and panel artwork, then verify crop, alt text, manifest metadata, and mobile legibility.
- **unreviewed-visual-medium-substitution**: Medium substitution is a design-system decision and must be traceable. Prevention: Record the medium_decision, intended_for slot, and reason for deterministic vector use, or restore the appropriate media pipeline.

### Commercial Product Realism

- **Rule**: Product and data UIs must feel operated, not generated: lead with real workflow state, data density, provenance, and asymmetric hierarchy instead of pitch-deck hero composition.
- **Applies to**: dashboard, tool, sports data product, community product, operational surface, B2B/SaaS product UI
- **Why AI-looking screens fail**:
- AI-looking screens often use a large cinematic hero, symmetric card grids, generic metric tiles, and equally polished panels before the actual task surface appears.
- Commercial sports and data products feel more credible because they expose compact live modules, filters, list/table rows, timestamps, source labels, status variation, and editorial or utility rails.
- Generated raster imagery becomes suspicious when it dominates a workflow screen and is not tied to actual product state, team identity, venue context, or inspectable content.
- **Required realism signals**:
- first-viewport task surface
- compact data/list/table module where the domain expects scanning
- clear primary action or filter path
- status variation such as live, final, upcoming, delayed, empty, error, or source-updated
- source labels, timestamps, sample/demo labels, or data provenance for exact numbers
- domain-specific identity assets such as team crests, app icon, venue/match labels, or object imagery when applicable
- national flag identity marks for country-based tournaments, paired with code/name text for scanability and accessibility
- reference-backed domain morphology such as score strips, compact rails, tables, tabs, and editorial sidebars before major realism refactors
- **Successful reusable patterns**:
- **same-domain-reference-before-redesign**: Before a realism pass, collect same-domain commercial references and current-state screenshots. Implementation: Use reference screenshots to extract morphology only: module order, density, rail/table rhythm, status texture, and hierarchy. Verification: A research report or design-context pack exists, and implementation notes name what was absorbed and what was not copied.
- **operational-header-before-hero-media**: Sports/data products open with operational status and task controls, not a cinematic hero. Implementation: Use compact status strips, date/filter rails, next match/current item, source labels, and primary task surfaces above decorative imagery. Verification: First viewport contains inspectable data/state modules before or alongside any generated visual context.
- **score-ticker-as-scan-surface**: Match tickers are scan surfaces; they should favor compact identity and state over full descriptive copy. Implementation: Use flag/code or icon/code labels, status chips, short prediction/result labels, and whole-card scroll math. Move full names and explanations to detail panels or aria-labels. Verification: Ticker item text does not clip at 390, 1024, or 1440px, and full match names remain available in detail views or accessibility labels.
- **national-flag-code-identity**: Country-based tournament UIs use national flag identity marks plus team codes/names as the primary recognition layer. Implementation: Use deterministic local SVG/CSS flag marks or licensed flag assets; pair with FIFA/IOC-style codes in dense rails and names in detailed surfaces. Verification: No emoji flags are used as UI icons; flag colors are represented through design-system tokens such as --ds-color-* rather than local raw colors.
- **source-ledger-and-sample-labeling**: Exact-looking sports metrics, predictions, and schedules need visible provenance. Implementation: Add source ledger, updated-at labels, sample/demo labels, and clear separation between official fixtures/results and MVP sample predictions/opinions. Verification: Numbers and predictions have source/update/sample context in the first screen or nearby metadata.
- **editorial-insight-side-rail**: Sports hubs benefit from an asymmetric side rail for context, fan pulse, and editorial watch points. Implementation: Pair the primary schedule/table with a sticky or stacked rail containing selected match, country tracking, fan reaction, and group implication cards. Verification: Primary task remains dominant while the rail provides contextual depth without becoming a homogeneous card wall.
- **visual-context-secondary**: Generated or atmospheric imagery supports venue/domain context but does not replace the product workflow. Implementation: Keep generated images small or secondary in operational products; use them to reinforce venue/command-center mood after schedule/status surfaces are visible. Verification: The image is not the largest first-viewport object in dashboards/tools unless the user explicitly requests a landing page.
- **dual-mode-screenshot-qa**: Light mode is the default product mode and dark mode remains available; both need screenshot QA. Implementation: Bind components to paired semantic tokens and capture at least light mode plus dark mode when theme support exists. Verification: The implementation includes :root light tokens, dark overrides, and viewport screenshots or checks for both modes.
- **brand-app-icon-as-required-identity**: App icon identity is part of product completeness, not optional polish. Implementation: Create or discover a brand-specific deterministic SVG app icon, wire favicon/manifest/app-shell, and avoid generic initials such as WC unless explicitly defined by the brand. Verification: BrandIdentityAsset is present in the ontology and the icon is visible in browser/app shell surfaces.
- **Implementation rules**:
- For dashboards, tools, sports/data products, and community products, do not make the first screen read like a marketing landing page unless the user explicitly asks for a landing page.
- Replace oversized hero pitches with an operational header: current status, primary workflow, filters/date rail, next item, or live summary.
- Use compact rows, tables, rails, tabs, and status chips when the domain task is scanning or comparison; reserve large cards for true summaries or repeated content items.
- Avoid homogeneous card walls where every module has the same weight, radius, tint, icon treatment, and spacing. Create an explicit hierarchy between primary task, secondary rail, and supporting modules.
- Exact metrics, model outputs, poll counts, odds, rankings, or match data need source/update context or a visible sample/demo label.
- Generated or decorative imagery must support the domain object, venue, person, product, or state. It must not replace data, navigation, controls, or the first operational surface.
- Use asymmetry and real product rhythm: some dense modules, some editorial/context rails, some compact controls, and visible state variation.
- For country-based sports competitions, represent teams with deterministic SVG/CSS flag marks plus text codes or names. Do not use platform emoji flags as UI icons, and do not default to generic colored letter badges when national identity is the primary domain signal.
- Flag colors and domain identity marks are design-system tokens. Do not create implementation-local --flag-* or raw color values in component CSS; use --ds-color-* or generated asset metadata.
- When a product UI is judged AI-looking, gather at least two same-domain commercial references and convert only observed morphology into the implementation: module order, density, status texture, rail/table patterns, and state hierarchy. Do not copy competitor copy, data, palette, or navigation taxonomy.
- **Promoted realism failure patterns**:
- **pitch-deck-dashboard-shell**: Operational products must lead with the user's live task or inspectable product state, not a pitch-deck composition. Prevention: Start with a compact command header, status strip, active filters/date rail, table/list, or primary workflow module; move marketing copy lower or remove it.
- **homogeneous-card-wall**: Commercial product UIs need hierarchy, density variation, and task-led asymmetry. Prevention: Promote one primary workflow module, compress secondary data into rows/tables/rails, and vary module scale only when the information architecture justifies it.
- **unverified-redesign-screenshot**: Visual feedback is not closed until before/after screenshots are preserved and compared. Prevention: Capture baseline and revised screenshots under distinct filenames, run compare-visuals, and cite hashes plus changed-pixel ratio before claiming a visual change.
- **decorative-ai-hero-over-data**: Generated imagery supports product context but does not outrank the operational surface. Prevention: Make imagery secondary, domain-specific, and connected to real content; prioritize score strips, tables, filters, or domain objects in the first viewport.
- **synthetic-metric-copy**: Credible product data must expose provenance or clearly identify itself as sample/demo data. Prevention: Add source/update labels, sample badges, data-footnote components, or remove exact-looking fabricated values until real data is available.
- **missing-operational-state-texture**: Commercial interfaces reveal operational state texture through varied statuses and edge cases. Prevention: Design and implement realistic domain states before final visual polish; include at least the states required by component_specs.md and product primitives.
- **reference-free-realism-refactor**: Commercial realism fixes must be evidence-backed: reference data informs morphology, while ontology tokens, component specs, and product goals remain authoritative. Prevention: Capture a current-state screenshot, collect at least two same-domain references, summarize observed patterns, and implement the relevant density, rail, table, status, or hierarchy changes without copying protected content.
- **generic-national-team-badges**: National-team products should expose flag identity marks as the primary visual cue, with text codes/names retained for scanability and accessibility. Prevention: Use local deterministic SVG/CSS flag marks or licensed flag assets paired with team codes. Avoid emoji flags and avoid replacing readable text with image-only flags.
- **untokenized-domain-identity-colors**: Domain identity colors are still governed design tokens; component CSS consumes token roles rather than inventing local palette variables. Prevention: Promote domain identity colors into token files as --ds-color-* or documented asset metadata, then bind components to those variables.

## 10. Reference Absorption Rule

- Analysed live reference sources: 3
- Rule: copy visuals from no single source; absorb patterns only when they reinforce brand keywords and avoid anti-keywords.
- Use references to validate structure, accessibility, token discipline, and documentation quality.
- Scope rule: Visual references are morphology inputs only; tokens, component specs, and product IA remain authoritative.
- Allowed from references:
  - component morphology
  - layout density
  - panel/card proportions
  - hierarchy rhythm
  - interaction affordance patterns
- Denied from references:
  - color palette
  - palette composition or derived secondary palettes
  - typography family or scale
  - semantic status colors
  - product copy
  - product data model
  - navigation labels
  - domain information architecture
  - redistributable imagery unless explicitly licensed
- Promoted failure patterns:
  - **token-bound-reference-palette-mixing**: Token binding is necessary but not sufficient; color role composition must still follow the ontology palette roles. Prevention: Derived colors may alias a semantic token or mix one semantic role with a neutral surface/transparent value. Do not mix multiple chromatic roles to create a local palette.
- Feedback promotion: When implementation review identifies a repeatable design-system failure, promote it into governance, generated artifacts, and lint checks before treating the current screen as complete. Outputs: design_system_blueprint.governance, system_spec.md, system_ontology.json, IMPLEMENTATION_CONTRACT.md

## 11. AI Synthesis Principles

- **hex를 만들지 않는다**: AI는 색상 hex 값을 임의로 생성하지 않는다. 반드시 color_reference, CSS 추출, 브랜드 가이드 등 실증 소스에서 가져온 값만 사용한다.
- **토큰명을 만들지 않는다**: AI는 토큰 이름을 임의로 발명하지 않는다. 네이밍 패턴(core/semantic/component 레이어 규칙)은 정의하되, 구체적 토큰명은 실제 컴포넌트와 역할에서 도출한다.
- **팩트 위에 해석만**: AI는 수집된 레퍼런스, 프로필, 온톨로지 증거 위에 해석과 구조화만 수행한다. 증거 없는 추론, 존재하지 않는 패턴 서술, 가상의 사용 사례 생성을 금지한다.
- **이모지를 UI 요소로 쓰지 않는다**: AI는 아이콘, 상태 표시, 버튼 장식, 네비게이션 지표 등 UI 컴포넌트 자리에 이모지(🎨 ✅ 🔥 ⚡ 🚀 ❌ ⭐ 📊 등)를 절대 넣지 않는다. 기본값은 SVG 파일/아이콘 컴포넌트 또는 아이콘 라이브러리(Lucide, Heroicons, Phosphor, Tabler 등)이지만, 사용자·리뷰어가 no-SVG/raster-only 매체를 지정한 프로젝트에서는 PNG/WebP/JPEG 아이콘 에셋을 사용한다. 리팩토링 중 카드, 버튼, 배지, 탭, 상태 표시, empty state에서 이모지를 발견하면 그대로 두지 말고 프로젝트의 활성 medium directive에 맞는 실제 아이콘 에셋으로 교체한다. 이모지는 본문 콘텐츠(예: 블로그 텍스트, 사용자 입력)에서만 허용되며, 시스템 UI 요소로는 금지한다.
- **컴포넌트를 직접 구현한다**: AI는 '임시 버튼', '플레이스홀더 카드', 'TODO 컴포넌트' 같은 반쪽 구현을 남기지 않는다. system_spec.md의 Component Strategy와 component_specs.md에 정의된 구조(anatomy), 상태(states), 토큰 바인딩, 접근성 규칙을 그대로 따라 완전한 컴포넌트를 구현한다. 기존 라이브러리 컴포넌트를 그냥 import해서 쓰는 대신, 디자인 시스템 토큰으로 스타일을 명시적으로 바인딩한다.
- **상용 제품 화면처럼 구성한다**: AI는 대시보드, 도구, 데이터 제품, 커뮤니티 제품을 피치덱식 히어로와 균일한 장식 카드 묶음으로 시작하지 않는다. 첫 화면은 사용자가 실제로 확인하거나 조작해야 하는 상태, 필터, 표/리스트, 출처, 업데이트 시각, 핵심 액션을 먼저 보여준다.
- **검색 이미지는 라이선스가 검증될 때만 사용한다**: AI는 image_gen을 사용할 수 없거나 실제 사진성이 더 중요한 경우에만 sourced visual fallback을 사용한다. 무료 provider는 per-asset license metadata가 필요하고, paid provider는 license_proof/usage_scope/licensed_to가 필요하다. Reference-only provider는 형태와 밀도 참고만 가능하며 이미지를 구현 에셋으로 복사하지 않는다. source_url, download_url, provider, author, license, attribution_required, sha256, alt_text를 manifest에 기록하지 못하는 이미지는 구현에 넣지 않는다. 런타임 hotlink와 stock/search 이미지를 앱 아이콘·로고·상태 아이콘으로 쓰는 것을 금지한다.
- **사용자가 지정한 이미지 매체는 기본 SVG 규칙보다 우선한다**: 사용자나 리뷰어가 'SVG 만들지 말고', '실제 그림파일', '검색해서 넣어', 'PNG/WebP/JPEG', '래스터', '비트맵'처럼 시각 에셋 매체를 명시하면 그 지시는 온톨로지의 medium override가 된다. 해당 범위의 아바타, 캐릭터, 콘텐츠 이미지, 앱 아이콘, UI 아이콘은 SVG/inline vector로 대체하지 않고 생성·사용자 제공·라이선스 소스 기반의 실제 래스터 파일을 프로젝트에 복사해 사용한다. 예외가 필요하면 사용자의 명시 승인과 medium_decision 기록이 먼저 있어야 한다.
- **목업은 관련 이미지를 적극적으로 사용한다**: AI는 사이트, 앱, 랜딩, 제품 소개, 콘텐츠 카드, 스포츠/장소/상품/포트폴리오 목업을 이미지 없는 카드와 그라디언트 블록만으로 끝내지 않는다. 도메인 실체를 드러내는 생성 이미지, 라이선스 검증 이미지, 사용자 제공 이미지, 브랜드 identity asset을 적극적으로 배치하고 manifest/alt/crop/반응형 검증까지 완료한다. 단 대시보드·운영 UI에서는 이미지가 표, 필터, 상태, 출처 같은 핵심 작업 표면을 밀어내지 않게 한다.
- **카드벽을 기본 레이아웃으로 쓰지 않는다**: AI는 페이지 섹션 전체를 카드 안에 다시 넣거나, 동일한 radius/shadow/padding을 가진 카드 묶음으로 화면을 채우지 않는다. 반복되는 객체에는 카드가 가능하지만, 1차 작업 표면은 canvas, map, table, row list, rail, inspector, sheet 같은 도메인 구조로 먼저 만든다.
- **아이콘과 도메인 그림은 완성 조건이다**: AI는 필터, 상태, 액션, 추천 근거, 도메인 객체에 프로젝트의 활성 medium directive에 맞는 아이콘이나 이미지 자산을 적극적으로 연결한다. 기본값은 SVG 아이콘이나 deterministic SVG/이미지 자산이지만, no-SVG/raster-only directive가 있으면 PNG/WebP/JPEG 에셋을 사용한다. 도메인이 장소·상품·콘텐츠·게임·스포츠처럼 시각 실체를 갖는 경우, 텍스트와 테두리만으로 완료 처리하지 않는다.

## 12. Ontology Targets

- **component**: 121
- **design_system**: 68
- **layout**: 42
- **content**: 36
- **accessibility**: 35
- **color**: 35
- **pattern**: 35
- **iconography**: 30

## 13. Profile Validation

- Warning: [pitfall#3-rebrand] Ochre vs Apricot (orange): lightness diff 28, saturation diff 29 — possible rebrand remnant
- Warning: visual_reference.sources is empty

## 14. Quick Start

이 문서는 **Agent Org Network Design System**의 디자인 시스템 사양입니다.

### 시작하기

1. **토큰 적용**: Drop-in CSS(아래 섹션 16)의 CSS 변수를 프로젝트에 복사합니다.
2. **컬러 세팅**: Color Reference(섹션 6)의 semantic role을 기준으로 surface/text/border를 잡습니다.
3. **타이포 세팅**: Token Strategy(섹션 5)의 font family와 type scale을 적용합니다.
4. **시각 방향 확인**: Visual Reference Signals(섹션 7)에서 density/surface/layout cue를 먼저 확인합니다.
5. **컴포넌트 구현**: Component Strategy(섹션 8)의 family 순서대로 하나씩 빌드합니다.

### 우선순위

핵심 primitive: **answer chat thread, question and answer bubble, trust state chip, source chip, owner chip**

이 primitive를 지원하는 컴포넌트부터 구현하고, 나머지는 필요에 따라 확장합니다.

## 15. DO / DON'T

### DO

- **Agentic**: agentic와 충돌하는 컴포넌트 변형은 만들지 않기
- **Operational**: operational와 충돌하는 컴포넌트 변형은 만들지 않기
- **Networked**: networked와 충돌하는 컴포넌트 변형은 만들지 않기
- **Observable**: observable와 충돌하는 컴포넌트 변형은 만들지 않기
- 모든 시각적 선택에서 **agentic, operational, networked** 기준을 적용
- semantic token을 통해 컬러를 적용 (하드코딩 금지)
- 일반(light) 모드와 dark 모드를 같은 semantic token 역할로 함께 구현
- 접근성 기준을 모든 text/surface 조합에서 먼저 검증
- 컴포넌트 변형 추가 전 기존 variant로 해결 가능한지 먼저 확인
- 아이콘은 SVG 컴포넌트 또는 Lucide/Heroicons/Phosphor/Tabler 등 라이브러리로 구현
- 앱 아이콘은 브랜드 특정 SVG identity asset으로 구현하고 favicon/manifest/app shell에 연결
- component_specs.md의 anatomy/states/token binding을 그대로 따라 완전히 구현

### DON'T

- **decorative graph** 방향의 디자인 결정을 하지 않음
- **marketing-heavy** 방향의 디자인 결정을 하지 않음
- **sci-fi glow** 방향의 디자인 결정을 하지 않음
- **generic card wall** 방향의 디자인 결정을 하지 않음
- hex 값을 임의로 생성하지 않음 (반드시 레퍼런스에서 가져오기)
- 토큰명을 임의로 발명하지 않음 (네이밍 패턴에서 도출)
- 한 레퍼런스의 비주얼을 그대로 복제하지 않음
- 다크모드만 구현하고 일반 모드를 빠뜨리지 않음
- 기존 기능 진입점을 승인 없이 제거하지 않음
- **이모지(🎨 ✅ 🔥 ⚡ 🚀 ❌ ⭐ 📊 등)를 아이콘/버튼/상태 표시로 절대 쓰지 않음** — 본문 콘텐츠에만 허용
- '임시 버튼', 'TODO 컴포넌트', '플레이스홀더 카드' 같은 반쪽 구현을 남기지 않음
- 라이브러리 컴포넌트를 기본 스타일로 그냥 쓰지 않음 — 반드시 디자인 토큰으로 스타일 바인딩

## 16. Drop-in CSS

아래 CSS 변수를 `:root`에 복사하여 즉시 사용할 수 있습니다.

```css
:root {
  /* --- Spacing --- */
  --space-0: 0px;
  --space-2: 2px;
  --space-4: 4px;
  --space-8: 8px;
  --space-12: 12px;
  --space-16: 16px;
  --space-24: 24px;
  --space-32: 32px;
  --space-48: 48px;
  --space-64: 64px;
  --space-96: 96px;

  /* --- Radius --- */
  --radius-none: 0;
  --radius-sm: 4px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;
  --radius-pill: 9999px;

  /* --- Typography --- */
  --font-heading: 'Pretendard', serif;
  --font-body: 'Pretendard', sans-serif;
  --font-mono: 'Source Code Pro', monospace;
  --text-xs: 12px;
  --text-sm: 14px;
  --text-md: 16px;
  --text-lg: 21px;
  --text-xl: 28px;
  --text-2xl: 38px;
  --text-3xl: 50px;

  --leading-tight: 1.2;
  --leading-normal: 1.5;
  --leading-comfortable: 1.6;
  --leading-relaxed: 1.75;

  /* --- Color (from reference) --- */
  --color-anchor-surface: #27503D;
  --color-structural-support: #006A4E;
  --color-quiet-background: #ACE1AF;
  --color-calm-border: #76C1B1;
  --color-fresh-accent: #00A591;

  /* --- Semantic roles (expanded) --- */
  --color-canvas: #F7F8FA;
  --color-surface: #FFFFFF;
  --color-surface-muted: #EEF1F6;
  --color-surface-elevated: #FFFFFF;
  --color-border: #D6DDE6;
  --color-border-strong: #B0BAC7;
  --color-ink: #111111;
  --color-ink-muted: #4B5563;
  --color-ink-subtle: #6B7280;
  --color-ink-inverse: #FFFFFF;
  --color-info: #0F4C81;
  --color-success: #006A4E;
  --color-warning: #FFB27F;
  --color-danger: #E2725B;

  /* --- Button — secondary --- */
  --color-button-secondary-surface-default: #FFFFFF;
  --color-button-secondary-surface-hover: #F1F1F1;
  --color-button-secondary-surface-active: #E7E7E7;
  --color-button-secondary-surface-disabled: #F7F8FA;
  --color-button-secondary-text-default: #111111;
  --color-button-secondary-text-disabled: #909091;
  --color-button-secondary-border-default: #BDC8D7;
  --color-button-secondary-border-hover: #A3B3C7;
  --color-button-secondary-focus-ring: #111111;

  /* --- Button — ghost --- */
  --color-button-ghost-surface-default: transparent;
  --color-button-ghost-surface-hover: #F3F3F3;
  --color-button-ghost-surface-active: #EAEAEA;
  --color-button-ghost-surface-disabled: transparent;
  --color-button-ghost-text-default: #4B5563;
  --color-button-ghost-text-hover: #111111;
  --color-button-ghost-text-disabled: #AAAFB6;
  --color-button-ghost-border-default: transparent;
  --color-button-ghost-focus-ring: #111111;

  /* --- Button — danger --- */
  --color-button-danger-surface-default: #E2725B;
  --color-button-danger-surface-hover: #DD5C41;
  --color-button-danger-surface-active: #DA4D30;
  --color-button-danger-text-default: #111111;
  --color-button-danger-border-default: #E2725B;
  --color-button-danger-focus-ring: #E2725B;

  /* --- Input --- */
  --color-input-surface-default: #FFFFFF;
  --color-input-surface-filled: #FFFFFF;
  --color-input-surface-disabled: #F7F8FA;
  --color-input-text-default: #111111;
  --color-input-text-placeholder: #90969F;
  --color-input-text-disabled: #AAAFB6;
  --color-input-border-default: #D6DDE6;
  --color-input-border-hover: #B6C3D3;
  --color-input-border-focus: #111111;
  --color-input-border-error: #E2725B;
  --color-input-border-disabled: #E6EAF0;

  /* --- Card --- */
  --color-card-surface-default: #FFFFFF;
  --color-card-surface-hover: #FAFAFA;
  --color-card-surface-muted: #F7F8FA;
  --color-card-border-default: #D6DDE6;
  --color-card-border-hover: #B6C3D3;
  --color-card-border-focus: #111111;

  /* --- Nav link --- */
  --color-nav-link-text-default: #4B5563;
  --color-nav-link-text-hover: #111111;
  --color-nav-link-text-active: #111111;
  --color-nav-link-surface-hover: #F5F5F5;
  --color-nav-link-indicator: #111111;

  /* --- Link --- */
  --color-link-text-default: #111111;
  --color-link-text-hover: #111111;
  --color-link-text-visited: #4B5563;

  /* --- Feedback — info --- */
  --color-feedback-info-surface: #E2EAF0;
  --color-feedback-info-text: #0F4C81;
  --color-feedback-info-border: #5580A5;
  --color-feedback-info-icon: #0F4C81;

  /* --- Feedback — success --- */
  --color-feedback-success-surface: #E0EDEA;
  --color-feedback-success-text: #006A4E;
  --color-feedback-success-border: #4A9582;
  --color-feedback-success-icon: #006A4E;

  /* --- Feedback — warning --- */
  --color-feedback-warning-surface: #FFF6F0;
  --color-feedback-warning-text: #FFB27F;
  --color-feedback-warning-border: #FDC7A4;
  --color-feedback-warning-icon: #FFB27F;

  /* --- Feedback — danger --- */
  --color-feedback-danger-surface: #FCEEEB;
  --color-feedback-danger-text: #E2725B;
  --color-feedback-danger-border: #E89A8B;
  --color-feedback-danger-icon: #E2725B;

  /* --- Motion --- */
  --duration-0: 0ms;
  --duration-80: 80ms;
  --duration-120: 120ms;
  --duration-180: 180ms;
  --duration-240: 240ms;
  --duration-320: 320ms;
  --ease-standard: cubic-bezier(0.4, 0, 0.2, 1);
  --ease-enter: cubic-bezier(0, 0, 0.2, 1);
  --ease-exit: cubic-bezier(0.4, 0, 1, 1);
  --ease-emphasized: cubic-bezier(0.2, 0, 0, 1);
}
```

## 17. CSS Extraction Summary

- CSS 추출 데이터 없음 (크롤링 시 CSS가 수집되지 않았거나 extract-css가 실행되지 않음)

## 18. Component-Token Map

| Component | Tokens Used |
|-----------|-------------|
| activity-card | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| app-shell | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| article-body | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| audit-timeline | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| avatar | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| back-button | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| banner | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| block-controls | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| bottom-sheet | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| breadcrumb | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| bulk-action-table | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| chart-container | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| chart-legend | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| chart-tooltip | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| chat-input | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| chat-message | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| chat-thread | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| checkbox | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| chip | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| citation-drawer | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| column-header | `spacing.12→padding`, `radius.md→radius`, `spacing.12→padding`, `radius.md→radius`, `font:Pretendard`, `font:Pretendard` |
| comment-input | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| comment-thread | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| cta-button | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| data-table | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| decision-record-card | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| diff-viewer | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| editor-canvas | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| editor-toolbar | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| empty-state | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| evidence-graph | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| exception-queue | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| filter-chip | `spacing.12→padding`, `radius.md→radius`, `spacing.12→padding`, `radius.md→radius`, `font:Pretendard`, `font:Pretendard` |
| footnote | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| form-actions | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| form-section | `spacing.12→padding`, `radius.md→radius`, `spacing.12→padding`, `radius.md→radius`, `font:Pretendard`, `font:Pretendard` |
| ghost-button | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| heading-anchor | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| icon-button | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| inline-alert | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| inline-format-menu | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| insight-card | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| kanban-board | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| kanban-card | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| kanban-column | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| link-button | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| link-card | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| mention-popup | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| mobile-tab-bar | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| mobile-topbar | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| modal-dialog | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| pagination | `spacing.12→padding`, `radius.md→radius`, `spacing.12→padding`, `radius.md→radius`, `font:Pretendard`, `font:Pretendard` |
| prev-next-pager | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| primary-button | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| profile-card | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| prose-block | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| radio | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| radio-group | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| reading-pane | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| redline-viewer | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| resizable-split-pane | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| row-actions | `spacing.12→padding`, `radius.md→radius`, `spacing.12→padding`, `radius.md→radius`, `font:Pretendard`, `font:Pretendard` |
| saved-view-bar | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| search-field | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| secondary-button | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| section-header | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| section-tabs | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| segmented-control | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| select | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| sidebar-nav | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| slash-command-menu | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| source-card | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| stat-card | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| status-badge | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| step-progress | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| table-of-contents | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| tag | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| text-field | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| textarea | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| toast | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| tool-call-trace | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| tooltip-guide | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| topbar | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| user-menu | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| wizard-layout | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |
| workspace-switcher | `spacing.12→padding`, `radius.md→radius`, `font:Pretendard` |

## 19. Color Mode Parity

- **Policy**: Every generated or refactored product UI must ship a normal light mode and a dark mode unless the user explicitly requests a single-mode artifact.
- **Default mode**: `light`
- **Implementation rules**:
  - Use light mode as the default :root or app-default token set; dark mode must be an override such as [data-theme="dark"].
  - Do not build dark-only surfaces for dashboards, tools, landing pages, or prototypes unless explicitly requested.
  - Every semantic surface/text/border/accent role needs a light and dark value or a documented derivation.
  - Theme toggles, preview links, screenshots, or QA scripts must verify both modes when the implementation has a UI shell.
  - Do not solve dark mode by inverting the entire page; define mode-specific semantic tokens and keep imagery/icons legible in both modes.
- **Promoted failure patterns**:
  - dark-only-implementation: Define :root light tokens, add [data-theme="dark"] overrides, and verify both modes before completion.
  - theme-token-drift: Keep mode differences inside artifact token files such as design-system/tokens.css; components should consume the same semantic variables in both modes.

| Mode | Required | Default |
|------|----------|---------|
| dark | yes | no |
| light | yes | yes |

## 20. Contrast Audit

| Background | Foreground | Ratio | Level |
|------------|------------|-------|-------|
| Celadon | Ink | 12.72:1 | AAA (pass) |
| Forest Green | Paper | 9.13:1 | AAA (pass) |
| Cascade | Ink | 9.02:1 | AAA (pass) |
| Bottle Green | Paper | 6.62:1 | AA (pass) |
| Forest Green | Celadon | 6.15:1 | AA (pass) |
| Arcadia | Ink | 6.11:1 | AA (pass) |
| Bottle Green | Celadon | 4.46:1 | AA-large (large-only) |
| Forest Green | Cascade | 4.36:1 | AA-large (large-only) |
| Bottle Green | Cascade | 3.16:1 | AA-large (large-only) |
| Arcadia | Paper | 3.09:1 | AA-large (large-only) |
| Forest Green | Arcadia | 2.95:1 | fail (FAIL) |
| Bottle Green | Ink | 2.85:1 | fail (FAIL) |
| Bottle Green | Arcadia | 2.14:1 | fail (FAIL) |
| Cascade | Paper | 2.09:1 | fail (FAIL) |
| Celadon | Arcadia | 2.08:1 | fail (FAIL) |
| Forest Green | Ink | 2.07:1 | fail (FAIL) |
| Celadon | Paper | 1.48:1 | fail (FAIL) |
| Cascade | Arcadia | 1.48:1 | fail (FAIL) |
| Celadon | Cascade | 1.41:1 | fail (FAIL) |
| Forest Green | Bottle Green | 1.38:1 | fail (FAIL) |

## 21. Pattern Catalog

### Layout Patterns

- **answer chat thread**: —
- **question and answer bubble**: —
- **trust state chip**: —
- **source chip**: —
- **owner chip**: —
- **staged routing progress trace**: —
- **staged pipeline stepper**: —
- **concept draft card**: —
- **approve edit reject disposition control**: —
- **before-after diff card**: —
- **owner inbox tabs**: —
- **contested case card**: —
- **candidate coverage list**: —
- **on-demand document pull**: —
- **assign owner control**: —
- **backup answer review card**: —
- **re-review queue row**: —
- **metric summary card**: —
- **live event feed**: —
- **routing outcome status tag**: —
- **worker admission panel**: —
- **audit log table**: —
- **light org reference map**: —
- **data tables**: data-table, column-header, filter-chip, row-actions, pagination
- **workspace navigation**: app-shell, sidebar-nav, topbar, breadcrumb, workspace-switcher
- **modal and dialog**: —
- **onboarding and stepper**: step-progress, wizard-layout, tooltip-guide
- **dashboard cards**: stat-card, insight-card, activity-card, section-header

### Interaction Patterns

- **search and filter**: —
- **notifications**: toast, inline-alert, empty-state, banner
- **forms**: text-field, select, checkbox, radio, textarea, form-section, radio-group, form-actions
- **command palette**: —

## 22. Brand Identity Assets

- **Policy**: Every app or website implementation must include a brand-specific app icon identity asset; generic initial-letter tiles are not acceptable as final app icons.
- **Implementation rules**:
  - Do not ship a plain initials tile such as WC, AI, DS, or App as the final app icon unless the brand system explicitly defines that lettermark.
  - The app icon must use the brand palette, visual keywords, and product primitives as evidence for shape language.
  - Use a deterministic SVG source for the primary app icon; generated raster imagery may support marketing visuals but must not replace the identity icon source.
  - If the user/reviewer explicitly forbids SVG or requires actual raster image files, the app icon identity source may be PNG/WebP/JPEG instead; record that medium override in the ontology and do not create an SVG fallback silently.
  - Wire the app icon into favicon/link metadata and the visible app-shell brand mark when the implementation has one.
  - Keep small-size legibility: the icon must remain recognizable at 32px and in a 44px navigation mark.
- **Promoted failure patterns**:
  - generic-initials-app-icon: Create or reuse a brand-specific SVG app icon, wire it to favicon/manifest/app-shell surfaces, and document it in the ontology.
  - low-quality-app-icon-identity: Redesign the app icon as a compact, legible, brand-specific SVG mark with coherent geometry, palette, and small-size testing.

| Asset | Required | Workspace Path | Targets | Formats |
|-------|----------|----------------|---------|---------|
| Brand app icon | yes | `—` | app shell brand mark, favicon, mobile home-screen icon, web app manifest | favicon, source asset (SVG by default; PNG/WebP/JPEG when a no-SVG medium override is active), web app manifest icon when applicable |

## 23. Generated Visual Asset Plan

- **Contract**: `visual-asset-manifest/v1` / preferred manifest `public/generated/design-system/manifest.json`
- **Compatible paths**: `public/generated/design-system/manifest.json`, `design-system/generated_visual_assets.json`
- **Execution**: built-in Codex `image_gen`; workspace copy required; original generated PNG preserved in manifest; API fallback disabled.
- **Required asset record fields**: `id`, `label`, `slot`, `status`, `asset_path`, `original_png_path`, `format`, `dimensions`, `size_kb`, `sha256`, `intended_for`, `alt_text`, `prompt_summary`

- **Sourced fallback**: license-verified sourced visual fallback / candidate manifest `public/generated/design-system/sourced-visual-candidates.json`
- **Allowed visual providers**: `openverse`, `wikimedia-commons`, `unsplash`, `pexels`, `adobe-stock`, `shutterstock`, `getty-images`, `istock`
- **Licensed providers require proof**: `adobe-stock`, `shutterstock`, `getty-images`, `istock`, `envato-elements`, `local-licensed-file`
- **Reference-only providers**: `lazyweb`, `mobbin`, `dribbble`, `behance`, `awwwards`; morphology only, no runtime asset copy.
- **Sourced execution**: license metadata required; workspace copy required; runtime hotlinking disabled; stock/search images are not valid identity assets.
- **Required sourced record fields**: `id`, `label`, `slot`, `status`, `acquisition_mode`, `asset_path`, `source_url`, `download_url`, `provider`, `author`, `license`, `attribution_required`, `sha256`, `intended_for`, `alt_text`, `selection_reason`

- **Visual Asset Medium Selection**: Visual asset slots must choose the medium that matches the subject and runtime role; narrative/content media needs high-fidelity raster or approved production artwork, not ad-hoc SVG sketches.
- **Directive overrides**:
  - user-raster-asset-directive: project-local raster image asset required; triggers: SVG 만들지 말고, SVG 금지, 실제 그림파일, 실제 이미지 파일, 검색해서 넣어; denied: svg, inline svg, deterministic svg placeholder
- **Medium decision sequence**:
  - First honor explicit user/reviewer medium directives. A no-SVG or raster-only directive overrides default identity/icon/vector preferences for the affected project or slot.
  - Classify the slot before drawing: identity/icon, control glyph, diagram/data, factual real-world media, narrative/content media, or decorative support.
  - If the slot's user expectation is rendered content art, product/place photography, story atmosphere, or inspectable media, use image_gen, user-supplied licensed imagery, sourced licensed imagery, or an already approved high-fidelity asset.
  - Use deterministic SVG for app icons, logos, flags, UI glyphs, charts, diagrams, maps, schematics, and semantic product illustrations where vector geometry is the correct runtime representation.
  - When a faster-to-author SVG would reduce the slot to a placeholder, treat that as a wrong-medium failure rather than a stylistic option.
- **Slot family defaults**:
  - high-fidelity-narrative-media: image_gen, user_supplied, sourced; examples: comic/manga/webtoon cover, panel or strip preview, story or character scene, editorial/article cover
  - factual-real-world-media: user_supplied, sourced; examples: real venue, real product, food/travel/place photo, person or event photo
  - identity-control-technical-vector: deterministic_svg, icon_library, semantic_html_css; examples: app icon, logo, favicon, flag
  - user-specified-raster-assets: image_gen, user_supplied, sourced, project_local_raster; examples: AI avatar, chat character portrait, generated app visual, search/sourced image slot

| Asset Slot | Mode | Required Medium | Source | Intended For | Manifest | Policy |
|------------|------|-----------------|--------|--------------|----------|--------|
| Brand-aligned raster image | generated | high-fidelity-raster-support; SVG: False | Codex image_gen skill | only when the implementation surface would benefit from generated imagery | `public/generated/design-system/manifest.json` | no API fallback |
| Brand-aligned raster image sourced fallback | sourced | high-fidelity-raster-support; SVG: False | Openverse, Wikimedia Commons, Unsplash, Pexels | only when the implementation surface would benefit from generated imagery | `public/generated/design-system/manifest.json` | license-verified sourced visual fallback |
| Card thumbnail | generated | content-media; SVG: only for approved product schematics or semantic vector thumbnails | Codex image_gen skill | activity-card, decision-record-card, insight-card, kanban-card, link-card, profile-card | `public/generated/design-system/manifest.json` | no API fallback |
| Card thumbnail sourced fallback | sourced | content-media; SVG: only for approved product schematics or semantic vector thumbnails | Openverse, Wikimedia Commons, Unsplash, Pexels | activity-card, decision-record-card, insight-card, kanban-card, link-card, profile-card | `public/generated/design-system/manifest.json` | license-verified sourced visual fallback |
| Editorial cover | generated | high-fidelity-narrative-media; SVG: denied unless approved production vector artwork exists | Codex image_gen skill | article-body, block-controls, editor-canvas, editor-toolbar, inline-format-menu, slash-command-menu | `public/generated/design-system/manifest.json` | no API fallback |
| Editorial cover sourced fallback | sourced | high-fidelity-narrative-media; SVG: denied unless approved production vector artwork exists | Openverse, Wikimedia Commons, Unsplash, Pexels | article-body, block-controls, editor-canvas, editor-toolbar, inline-format-menu, slash-command-menu | `public/generated/design-system/manifest.json` | license-verified sourced visual fallback |
| Empty-state illustration | generated | supportive-illustration; SVG: allowed when polished, semantic, and not substituting for content media | Codex image_gen skill | empty-state, step-progress, tooltip-guide, wizard-layout, feedback | `public/generated/design-system/manifest.json` | no API fallback |
| Empty-state illustration sourced fallback | sourced | supportive-illustration; SVG: allowed when polished, semantic, and not substituting for content media | Openverse, Wikimedia Commons, Unsplash, Pexels | empty-state, step-progress, tooltip-guide, wizard-layout, feedback | `public/generated/design-system/manifest.json` | license-verified sourced visual fallback |

## 24. Mockup Visual Substance

- **Policy**: Commercial mockups should use meaningful visual assets by default; image-free screens are incomplete when the product, content, place, object, or story needs visual substance.
- **Applies to**: website mockup, landing page, product page, commerce, editorial/content surface, portfolio, venue/place page, sports hub, travel/food/real-estate, game or interactive experience
- **Why image-free mockups fail**:
  - Image-free mockups often look unfinished because cards, hero sections, editorial modules, and content surfaces have no concrete subject matter.
  - Gradient blocks, abstract blobs, and homogeneous placeholder panels read as AI-generated polish rather than a real product or brand surface.
  - Professional sites usually reveal an actual product, place, person, object, state, gameplay, article subject, or brand identity asset early in the experience.
- **Medium selection policy**: Visual asset slots must choose the medium that matches the subject and runtime role; narrative/content media needs high-fidelity raster or approved production artwork, not ad-hoc SVG sketches.
- **Medium selection rules**:
  - A user/reviewer sentence such as 'SVG 만들지 말고 실제 그림파일로 만들거나 검색해서 넣어' is a binding medium override, not a preference. Store it in governance/system_ontology/IMPLEMENTATION_CONTRACT and satisfy it before visual QA.
  - When a raster-only/no-SVG directive is active, do not create SVG avatars, inline SVG sprites, SVG favicons, SVG placeholder art, or SVG UI icons for the affected scope; create or source project-local PNG/WebP/JPEG assets instead.
  - Comic, manga, and webtoon cover or panel-preview slots default to image_gen-generated raster, user-supplied artwork, or licensed/sourced artwork.
  - A geometric SVG, rough path drawing, or low-information vector placeholder is not an acceptable final comic cover, manga panel, article cover, product photo, or story media asset.
  - Do not substitute inline SVG scene art solely because it is faster to author; use the imagegen skill when synthetic art is appropriate and available.
  - Deterministic SVG remains appropriate for app icons, logos, flags, UI icons, charts, diagrams, maps, and product schematics when those are the actual runtime medium.
- **Promoted medium failure patterns**:
  - wrong-medium-svg-for-narrative-media: Use image_gen or licensed/user-supplied raster artwork for the slot; reserve deterministic SVG for identity, controls, diagrams, maps, charts, or schematics.
  - user-raster-directive-svg-violation: Replace SVG assets with project-local PNG/WebP/JPEG files generated, user-supplied, or license-verified from search; record medium_decision and verify the implementation has no .svg or inline <svg> references.
  - comic-cover-as-geometric-placeholder: Generate or source finished cover and panel artwork, then verify crop, alt text, manifest metadata, and mobile legibility.
  - unreviewed-visual-medium-substitution: Record the medium_decision, intended_for slot, and reason for deterministic vector use, or restore the appropriate media pipeline.
- **Required visual substance signals**:
  - at least one relevant visual asset when the first viewport is a landing, brand, product, venue, editorial, portfolio, game, or content-led surface
  - real content thumbnails or product/place/object imagery where repeated cards represent visual entities
  - image_gen, sourced visual fallback, user-supplied assets, or deterministic SVG identity assets selected according to the visual asset acquisition contract
  - deterministic inline SVG visuals include visible labels/legends or title/desc plus data-subject anchors when they represent places, products, diagrams, maps, or scenes
  - manifest entry with acquisition_mode, asset_path, intended_for, alt_text, sha256, crop/focal notes when applicable
  - responsive crop and light/dark legibility verified by screenshots or DOM checks
- **Image acquisition order**:
  - Use user-supplied licensed imagery when provided and relevant.
  - Use Codex image_gen for brand-specific synthetic raster imagery.
  - Use sourced visual fallback when real-world photography is more appropriate or image_gen is unavailable.
  - Use deterministic SVG/identity assets for app icons, logos, flags, diagrams, and UI glyphs.
- **Implementation rules**:
  - Do not ship a commercial website/app mockup with only text, bordered cards, gradients, and empty media placeholders when the domain naturally needs imagery.
  - Hero, product, venue, editorial, portfolio, and game surfaces need a concrete visual subject, not a purely atmospheric background.
  - Repeated content cards should use thumbnails or compact visual identity when the item represents a place, person, product, match, article, media, or object.
  - Empty states and onboarding panels can use illustration, but the illustration must clarify the product state rather than decorate a blank panel.
  - Path-only inline SVGs with generic map/sketch/illustration classes do not count as visual substance unless the visual is semantically anchored with labels, legend, title/desc, or data-subject landmarks.
  - Do not invent rough hand-drawn scene illustrations inside implementation code as a substitute for product visuals; use image_gen, sourced/user-supplied assets, approved assets, or polished product schematics.
  - Mockups must declare the real app representation for visual surfaces: map SDK/tile layer, generated or sourced media, chart/table, data visualization, or explicit loading/empty state.
  - Do not represent evidence maps, relation maps, or product data graphs as hand-positioned HTML nodes connected by rotated CSS lines. Use a real graph/chart library, SVG/canvas visualization with semantic labels and runtime data, or a ledger/table when the relationship is simple.
  - A media/photo runtime surface is not complete when it is only CSS gradients or texture patterns; it must bind an image/video asset or show an explicit empty/loading state.
  - Operational dashboards, sports/data products, and tools may keep imagery secondary, but should still use domain visuals such as app icons, team/flag identity, venue thumbnails, product objects, or editorial context where they add credibility.
- **Promoted failure patterns**:
  - image-free-commercial-mockup: Add relevant generated, sourced, user-supplied, or deterministic visual assets and record them in the manifest before calling the mockup complete.
  - placeholder-gradient-as-image: Replace placeholder media with image_gen, sourced, user-supplied, or deterministic SVG assets that match the domain and slot.
  - low-information-inline-svg-visual: Add visible labels, legend, title/desc, and data-subject landmarks, or replace the slot with a stronger generated, sourced, or user-supplied asset.
  - amateur-ad-hoc-illustration: Use image_gen, a sourced/user-supplied asset, a reference-backed illustration, or a clean product schematic/data visualization. Do not ship rough path art as visual substance.
  - ambiguous-mock-runtime-surface: Mark the surface as a map SDK layer, generated/sourced media, chart/table, product schematic, or explicit loading/empty state; avoid ambiguous decorative stand-ins.
  - ad-hoc-node-link-placeholder-graph: Use a proven graph/chart library, a semantically labeled SVG/canvas visualization backed by real runtime data, or replace simple relationships with an evidence ledger/table.
  - media-runtime-surface-without-asset: Bind a generated, sourced, or user-supplied image/video asset with alt text and manifest metadata, or render a clear empty/loading state instead of fake visual texture.
  - media-tile-without-asset: Attach generated, sourced, or user-supplied media to each tile, or mark the tile as an intentional empty/loading/pending state with clear copy.

## 25. Reference Intelligence Pack

- **Activation**: planned / research gaps: 3
- **Allowed from references**: component morphology, layout density, panel/card proportions, hierarchy rhythm, interaction affordance patterns, flow pattern labels
- **Denied from references**: color palette, palette composition, typography scale, domain information architecture, product copy, redistributable imagery unless explicitly licensed

| Provider | Status | Access | Role |
|----------|--------|--------|------|
| Lazyweb MCP real-app corpus | suggested | mcp-or-manual-export | real app flow and screen corpus provider |
| Pinterest-assisted capture | preview | manual-or-playwright-capture | search assist and shortlist support |

| Context | Provider | Provenance | Allowed Use |
|---------|----------|------------|-------------|
| human approval inbox and review queue with status tags | Lazyweb MCP real-app corpus | planned | flows: data-review, messaging; morphology: general-interface-composition |
| operations event feed dashboard with audit log table dark theme | Lazyweb MCP real-app corpus | planned | flows: data-review, dashboard; morphology: dense-table, timeline |
| trustworthy answer chat with owner attribution and source citations | Lazyweb MCP real-app corpus | planned | flows: messaging; morphology: evidence |

## 26. Commercial Product Realism

- **Policy**: Product and data UIs must feel operated, not generated: lead with real workflow state, data density, provenance, and asymmetric hierarchy instead of pitch-deck hero composition.
- **Applies to**: dashboard, tool, sports data product, community product, operational surface, B2B/SaaS product UI
- **Why AI-looking screens fail**:
  - AI-looking screens often use a large cinematic hero, symmetric card grids, generic metric tiles, and equally polished panels before the actual task surface appears.
  - Commercial sports and data products feel more credible because they expose compact live modules, filters, list/table rows, timestamps, source labels, status variation, and editorial or utility rails.
  - Generated raster imagery becomes suspicious when it dominates a workflow screen and is not tied to actual product state, team identity, venue context, or inspectable content.
- **Required realism signals**:
  - first-viewport task surface
  - compact data/list/table module where the domain expects scanning
  - clear primary action or filter path
  - status variation such as live, final, upcoming, delayed, empty, error, or source-updated
  - source labels, timestamps, sample/demo labels, or data provenance for exact numbers
  - domain-specific identity assets such as team crests, app icon, venue/match labels, or object imagery when applicable
  - national flag identity marks for country-based tournaments, paired with code/name text for scanability and accessibility
  - reference-backed domain morphology such as score strips, compact rails, tables, tabs, and editorial sidebars before major realism refactors
- **Successful reusable patterns**:
  - same-domain-reference-before-redesign: Before a realism pass, collect same-domain commercial references and current-state screenshots.; Use reference screenshots to extract morphology only: module order, density, rail/table rhythm, status texture, and hierarchy.; A research report or design-context pack exists, and implementation notes name what was absorbed and what was not copied.
  - operational-header-before-hero-media: Sports/data products open with operational status and task controls, not a cinematic hero.; Use compact status strips, date/filter rails, next match/current item, source labels, and primary task surfaces above decorative imagery.; First viewport contains inspectable data/state modules before or alongside any generated visual context.
  - score-ticker-as-scan-surface: Match tickers are scan surfaces; they should favor compact identity and state over full descriptive copy.; Use flag/code or icon/code labels, status chips, short prediction/result labels, and whole-card scroll math. Move full names and explanations to detail panels or aria-labels.; Ticker item text does not clip at 390, 1024, or 1440px, and full match names remain available in detail views or accessibility labels.
  - national-flag-code-identity: Country-based tournament UIs use national flag identity marks plus team codes/names as the primary recognition layer.; Use deterministic local SVG/CSS flag marks or licensed flag assets; pair with FIFA/IOC-style codes in dense rails and names in detailed surfaces.; No emoji flags are used as UI icons; flag colors are represented through design-system tokens such as --ds-color-* rather than local raw colors.
  - source-ledger-and-sample-labeling: Exact-looking sports metrics, predictions, and schedules need visible provenance.; Add source ledger, updated-at labels, sample/demo labels, and clear separation between official fixtures/results and MVP sample predictions/opinions.; Numbers and predictions have source/update/sample context in the first screen or nearby metadata.
  - editorial-insight-side-rail: Sports hubs benefit from an asymmetric side rail for context, fan pulse, and editorial watch points.; Pair the primary schedule/table with a sticky or stacked rail containing selected match, country tracking, fan reaction, and group implication cards.; Primary task remains dominant while the rail provides contextual depth without becoming a homogeneous card wall.
  - visual-context-secondary: Generated or atmospheric imagery supports venue/domain context but does not replace the product workflow.; Keep generated images small or secondary in operational products; use them to reinforce venue/command-center mood after schedule/status surfaces are visible.; The image is not the largest first-viewport object in dashboards/tools unless the user explicitly requests a landing page.
  - dual-mode-screenshot-qa: Light mode is the default product mode and dark mode remains available; both need screenshot QA.; Bind components to paired semantic tokens and capture at least light mode plus dark mode when theme support exists.; The implementation includes :root light tokens, dark overrides, and viewport screenshots or checks for both modes.
  - brand-app-icon-as-required-identity: App icon identity is part of product completeness, not optional polish.; Create or discover a brand-specific deterministic SVG app icon, wire favicon/manifest/app-shell, and avoid generic initials such as WC unless explicitly defined by the brand.; BrandIdentityAsset is present in the ontology and the icon is visible in browser/app shell surfaces.
- **Implementation rules**:
  - For dashboards, tools, sports/data products, and community products, do not make the first screen read like a marketing landing page unless the user explicitly asks for a landing page.
  - Replace oversized hero pitches with an operational header: current status, primary workflow, filters/date rail, next item, or live summary.
  - Use compact rows, tables, rails, tabs, and status chips when the domain task is scanning or comparison; reserve large cards for true summaries or repeated content items.
  - Avoid homogeneous card walls where every module has the same weight, radius, tint, icon treatment, and spacing. Create an explicit hierarchy between primary task, secondary rail, and supporting modules.
  - Exact metrics, model outputs, poll counts, odds, rankings, or match data need source/update context or a visible sample/demo label.
  - Generated or decorative imagery must support the domain object, venue, person, product, or state. It must not replace data, navigation, controls, or the first operational surface.
  - Use asymmetry and real product rhythm: some dense modules, some editorial/context rails, some compact controls, and visible state variation.
  - For country-based sports competitions, represent teams with deterministic SVG/CSS flag marks plus text codes or names. Do not use platform emoji flags as UI icons, and do not default to generic colored letter badges when national identity is the primary domain signal.
  - Flag colors and domain identity marks are design-system tokens. Do not create implementation-local --flag-* or raw color values in component CSS; use --ds-color-* or generated asset metadata.
  - When a product UI is judged AI-looking, gather at least two same-domain commercial references and convert only observed morphology into the implementation: module order, density, status texture, rail/table patterns, and state hierarchy. Do not copy competitor copy, data, palette, or navigation taxonomy.
- **Promoted failure patterns**:
  - pitch-deck-dashboard-shell: Start with a compact command header, status strip, active filters/date rail, table/list, or primary workflow module; move marketing copy lower or remove it.
  - homogeneous-card-wall: Promote one primary workflow module, compress secondary data into rows/tables/rails, and vary module scale only when the information architecture justifies it.
  - unverified-redesign-screenshot: Capture baseline and revised screenshots under distinct filenames, run compare-visuals, and cite hashes plus changed-pixel ratio before claiming a visual change.
  - decorative-ai-hero-over-data: Make imagery secondary, domain-specific, and connected to real content; prioritize score strips, tables, filters, or domain objects in the first viewport.
  - synthetic-metric-copy: Add source/update labels, sample badges, data-footnote components, or remove exact-looking fabricated values until real data is available.
  - missing-operational-state-texture: Design and implement realistic domain states before final visual polish; include at least the states required by component_specs.md and product primitives.
  - reference-free-realism-refactor: Capture a current-state screenshot, collect at least two same-domain references, summarize observed patterns, and implement the relevant density, rail, table, status, or hierarchy changes without copying protected content.
  - generic-national-team-badges: Use local deterministic SVG/CSS flag marks or licensed flag assets paired with team codes. Avoid emoji flags and avoid replacing readable text with image-only flags.
  - untokenized-domain-identity-colors: Promote domain identity colors into token files as --ds-color-* or documented asset metadata, then bind components to those variables.
