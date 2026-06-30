# Implementation Contract

Preset: `dashboard--corporate-trust`
System: Agent Org Network Design System

## Authority Order

1. Existing product task flow and information architecture
2. `design-system/token_schema.json`
3. `design-system/tokens.css` or host adapter token variables
4. `design-system/components/component_specs.*`
5. `design-system/system_spec.md`
6. `design-system/STYLE.md` or `design-system/DESIGN.md` as a derived quick brief
7. External visual references

The style capsule is a derived summary and never overrides the source artifacts.
External references never outrank product IA, tokens, component specs, or semantic
state rules.

## Reference Absorption Scope

Allowed from visual references:

- component morphology
- layout density
- panel/card proportions
- hierarchy rhythm
- interaction affordance patterns

Denied from visual references:

- color palette
- palette composition or derived secondary palettes
- typography family or scale
- semantic status colors
- product copy
- product data model
- navigation labels
- domain information architecture
- redistributable imagery unless explicitly licensed

## Token Binding Rules

- Use `var(--ds-color-*)` for color, surface, border, and feedback states.
- Use `var(--ds-font-*)` for explicit font-family declarations.
- Use `var(--ds-radius-*)` for component radii; only fully circular affordances may use `999px`.
- Do not hard-code hex/rgb/hsl colors in implementation files.
- Keep generated token value blocks in `design-system/` artifacts such as `tokens.css`; implementation files consume `--ds-*` tokens only.
- Do not add reference-derived local palette variables such as teal/gold/slate unless they alias `--ds-*` tokens.
- Token binding is necessary but not sufficient: do not recombine `--ds-*` color roles into a new reference-like palette.
- Derived colors may alias one semantic token or mix one semantic role with a neutral surface/transparent value; do not mix multiple chromatic roles for local palette variables.

## Feedback Promotion Rule

If implementation review or user feedback exposes a repeatable design-system failure,
promote it into ontology governance, this contract, and `lint-implementation` before
calling the current screen complete. Current-screen fixes alone are not enough.

## Color Mode Parity

- Ship normal light mode and dark mode together unless a single-mode artifact is explicitly requested.
- Light mode is the default `:root` or app-default token set; dark mode is an override such as `[data-theme="dark"]`.
- Components must consume the same semantic token roles in both modes. Only token values should change.
- Do not ship dark-only dashboards, tools, landing pages, or prototypes.
- Verify both modes with screenshots or DOM checks when the implementation has a visible UI shell.

## Responsive Resilience

- Verify changed screens at 320, 360, 390, 430, 768, and desktop widths.
- The screen is not complete if `document.documentElement.scrollWidth > window.innerWidth`.
- Buttons, CTAs, tabs, chips, and toolbar actions must not use fixed px widths or mobile-hostile `min-width` values unless a <=480px wrap/stack fallback is present.
- Button-like controls should keep `max-inline-size: 100%`; controls inside flex/grid parents need `min-inline-size: 0`.
- Action rows must wrap or stack on narrow viewports.
- Avoid `width: 100vw` inside padded containers, and do not hide `overflow-x` on `body` as the fix for overflow.

## Emoji-to-SVG Refactor

- UI icons, button decorations, cards, badges, tabs, nav markers, status indicators, empty states, toasts, and banners must not use emoji glyphs.
- During refactors, replace any emoji-looking UI affordance with the project's existing icon library, an existing local SVG/icon component, or a newly authored SVG asset.
- If the project has a raster-only/no-SVG medium directive, replace emoji-looking UI affordances with project-local PNG/WebP/JPEG icon assets instead of SVG.
- UI icons must come from an approved icon system such as Lucide, Heroicons, Phosphor, Tabler, Material Symbols, an existing project library, or a documented approved custom set.
- A custom icon sprite must declare its source or approved grammar, and icons must share one 24px grid, stroke weight, caps, joins, optical size, and active/inactive treatment.
- New SVGs should live in the nearest existing icon/assets directory and use `currentColor` or design tokens for stroke/fill, unless a raster-only/no-SVG directive is active.
- Decorative icons use `aria-hidden="true"`; semantic icons need an accessible label or adjacent visible text.
- Do not replace user-generated emoji content, chat text, blog body, or emoji-picker data.
- App icons are not exempt from quality review: they must read as finished brand identity marks, not rough illustrations, generic tiles, or low-confidence geometric placeholders.

## Commercial Product Realism

- Dashboards, tools, sports/data products, and community products must lead with operational product substance, not pitch-deck hero composition.
- First viewport should expose the active task surface: status strip, filters/date rail, compact table/list rows, next item, source/update label, or primary workflow.
- Do not use homogeneous card walls as the default structure. Promote the primary workflow, compress secondary information into rows/tables/rails, and vary density deliberately.
- Page-level sections should not all be framed as cards. Use cards for repeated entities, modals, and genuinely framed tools; use canvas, map, table, row list, rail, inspector, or sheet structures for the main workflow.
- Exact numbers, predictions, rankings, poll counts, odds, or operational claims need source/update context or a visible sample/demo label.
- Generated or decorative imagery must support the domain context and must not outrank data, navigation, controls, or the first operational surface.
- Include realistic state texture such as live/final/upcoming/delayed/empty/error/source-updated when the domain supports those states.
- `lint-implementation` promotes homogeneous card/panel walls to DS070.

## Icon And Visual Affordance Coverage

- Interactive surfaces with filters, actions, badges, tabs, status chips, or repeated scan rows need meaningful icon affordances paired with text; use SVG by default, or PNG/WebP/JPEG when a raster-only/no-SVG directive is active.
- Domain surfaces for places, products, articles, games, sports, venues, or visual content need at least one real or deterministic domain visual: image, illustration, map, sketch, media, texture, or object/identity asset.
- Deterministic inline SVG visuals must be semantically readable: include visible labels/legend or title/desc plus data-subject landmarks when representing maps, scenes, products, objects, or places.
- Do not use rough ad-hoc `sketch`, `doodle`, or hand-drawn SVG scene art as the product/domain visual. If the slot needs an illustration/photo, use `image_gen`, a sourced/user-supplied asset, or an approved asset pipeline; if it is product logic, use a polished schematic map/chart/diagram instead.
- Comic, manga, webtoon, story, character, editorial-cover, or panel-preview media slots must not use SVG placeholders as final artwork. Use `image_gen`, user-supplied art, sourced licensed art, or approved production artwork.
- SVG icons must use `currentColor` or design tokens, preserve accessible labels through adjacent text or aria, and must not be replaced by emoji glyphs. When raster-only/no-SVG is active, PNG/WebP/JPEG icon assets inherit the same accessibility requirements.
- `lint-implementation` promotes icon-starved interactive surfaces to DS071, domain-visual gaps to DS072, low-information inline SVG visuals to DS073, amateur ad-hoc illustration to DS074, wrong-medium SVG narrative media to DS079, undeclared handmade icon sprites to DS080, and SVG usage under raster-only/no-SVG directives to DS081.

## Visual Evidence And Screenshot Comparison

- Visual feedback is not closed by a new screenshot alone. Preserve baseline and revised screenshots under distinct filenames before claiming visual improvement.
- Run `uv run design-ontology compare-visuals --before <baseline.png> --after <revised.png>` for redesigns, screenshot QA, or feedback-driven visual changes.
- If the before/after hashes are identical or the changed-pixel ratio is below the project threshold, treat the redesign as unverified and keep iterating.
- Do not overwrite screenshots that are needed as comparison inputs; use versioned names such as `before-*`, `after-*`, or timestamped captures.

## Mock Fidelity And Runtime Representation

- Mockups are allowed to use sample data, but every major visual surface must make its real app counterpart obvious.
- Map products should look like a map SDK/tile layer with controls, pins, scale, labels, and data overlays, not an invented illustration.
- Media-led surfaces should use generated, sourced, or user-supplied assets when the slot represents a real photo/thumbnail; otherwise show an explicit loading, empty, or pending state. CSS gradients, texture strips, and abstract patterns do not count as media assets.
- Each visible media/evidence tile such as `place-photo`, `texture-card`, `media-card`, `evidence-card`, or `thumbnail-card` needs its own media asset or an explicit empty/loading/pending state.
- Use `data-runtime-surface` to mark runtime intent such as `map-sdk-layer`, `generated-place-photo`, `sourced-thumbnail`, `chart-layer`, `table-view`, or `empty-state`.
- App-shell marks, favicons, and manifest icons must use a brand-specific identity asset, not a generic initials tile. SVG is the default source format; PNG/WebP/JPEG is required when a raster-only/no-SVG directive is active.
- `lint-implementation` promotes ambiguous schematic/mock/placeholder visual surfaces without runtime intent to DS075, media/photo runtime surfaces without assets to DS076, generic initials app marks to DS077, and individual media/evidence tiles without assets to DS078.

## Visual Asset Medium Selection

- Classify the slot before authoring: identity/icon, control glyph, diagram/data, factual real-world media, narrative/content media, or decorative support.
- Honor explicit user/reviewer medium directives before the default slot classification. Phrases such as `SVG 만들지 말고`, `실제 그림파일`, `검색해서 넣어`, `PNG/WebP/JPEG`, `raster-only`, or `no-svg` make the affected scope project-local raster asset only.
- Under a raster-only/no-SVG directive, do not create SVG avatars, inline SVG sprites, SVG favicons, SVG placeholder art, or SVG UI icons for the affected scope. Use generated, user-supplied, or license-verified PNG/WebP/JPEG assets and record the medium decision.
- Use deterministic SVG for app icons, logos, flags, UI glyphs, charts, diagrams, maps, schematics, and semantic product illustrations when vector is the correct runtime representation.
- Use generated, user-supplied, sourced, or approved high-fidelity artwork for content media users are expected to inspect: comic covers, manga/webtoon panels, story scenes, editorial covers, product/place photos, portfolio pieces, and game scenes.
- Do not substitute inline SVG scene art solely because it is faster to author. If the expected experience is polished art or photography, the wrong medium is a defect.
- If narrative/content media intentionally uses vector artwork, document why it is production-grade artwork rather than a placeholder and record the medium decision.
- `lint-implementation` promotes SVG assets in narrative/content media slots to DS079 and SVG usage under raster-only/no-SVG directives to DS081.

## Visual Asset Acquisition

- Commercial mockups should actively use relevant visual assets when the product, place, object, article, game, venue, or content model needs visual substance.
- Do not treat image-free card walls, gradient media blocks, or empty framed placeholders as complete website/app mockups when the domain naturally expects imagery.
- Use Codex built-in `image_gen` first when a brand-specific synthetic raster is appropriate.
- For comic, manga, webtoon, article-cover, story-scene, and panel-preview slots, `image_gen`, user-supplied art, sourced licensed art, or approved production artwork is the default. Geometric SVG and rough vector placeholders fail the medium-selection contract.
- If `image_gen` is unavailable, fails, or real-world photography is more appropriate, use sourced visual fallback rather than another image-generation API.
- Sourced visual fallback requires source URL, download URL, provider, author/creator, license label, attribution requirement, sha256, alt text, and intended component slot in the manifest.
- Free sourced providers can be used with per-asset license metadata; paid/licensed providers additionally require license proof, usage scope, and licensed-to metadata.
- Reference-only providers such as design galleries or app screenshot corpora are morphology inputs only; do not copy their images into runtime assets.
- Do not hotlink remote search/CDN URLs from runtime code; copy accepted visuals into the project workspace before referencing them.
- Do not use searched stock/free images as app icons, favicons, logos, button glyphs, status markers, or flags. Those remain deterministic identity/icon assets.
- Keep images secondary in operational dashboards/tools when data and controls are the primary task, but still use domain visuals such as identity marks, thumbnails, venue/object imagery, or editorial context when they increase credibility.

## Preflight

Run this before considering an implementation aligned:

```bash
uv run design-ontology lint-implementation --target-repo .
```
