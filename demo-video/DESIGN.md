# DESIGN — Agent Org Network 시연영상

## Style Prompt
Swiss Pulse, dark variant. Clinical developer-tool precision on a deep near-black canvas. Grid-locked layouts, generous negative space, one electric-blue accent for structure plus semantic status colors (green = routed/success, amber = contested/stale/escalation, coral = unowned/거부, violet = precedent/learning). Bold Helvetica/Inter headlines, monospace for intent tokens · agent_id · code · SHA. Snappy entrances (expo/power4/back), restrained crossfade transitions. Nothing decorative — the diagram IS the message.

## Colors
- `#0a0e16` canvas (near-black navy)
- `#141b26` surface (cards/panels)
- `#243044` hairlines/borders
- `#e6edf3` primary text
- `#9aa7b8` muted/labels
- `#4d9fff` accent blue — structure, section index
- `#46d27a` green — routed / success
- `#ffb020` amber — contested / stale / escalation
- `#ff6b6b` coral — unowned / 거부
- `#a98bff` violet — precedent / learning

## Typography
- "Helvetica Neue" / Inter — headlines (800–900), labels (300–600, uppercase, tracked)
- "SF Mono" / Menlo — intent tokens, agent_id, code, snapshot_sha

## Motion
- Entrances: expo.out / power4.out / back.out / power2.out — fast, snap into place
- Transitions: crossfade 0.45s sine.inOut (clinical, no decoration)
- Stagger 0.1s within a scene; rotate entrance direction across scenes (4 variants)

## What NOT to Do
- No full-screen linear gradients (H.264 banding) — radial glow or solid only
- No `#3b82f6` / `#333` / Roboto generic defaults
- No decorative shader transitions (no glitch/swirl) — Swiss restraint
- No exit animations except the final scene hold
- No emoji, no rounded "friendly" bubbles — sharp editorial cards
