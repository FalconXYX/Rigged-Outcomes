# Rigged Outcomes — design rules (v3 · 2026-11-07)

Target: a financial platform page (FT, Koyfin, Stripe's finance pages), not a dashboard template. Flat, sharp, dense,
typographic, hairline-ruled, numbers aligned, color scarce and meaningful. In one line: **hairlines not shadows,
2–4px corners not pills, ink text on neutral surfaces, a hue only as a small key mark.** Tokens: `src/styles/tokens.css`.
Shared classes: `src/styles/base.css` (type, `.card`, `.section-head`, `.eyebrow`/`.label`, `.tag`, `.table`,
`.popover`, `.accent-rule`, `.key-dot`, `.section-icon`, `.meter`, `.rule-key`, `.meta-line`), `src/components/ui.css` +
`ui.tsx` (`.stat-strip` + StatTile, Segmented, Switch, Select, Button, Toolbar + ToolbarGroup, Legend, Tooltip,
`keyStyle`, `FONT_SANS`) and `src/components/shell.css` (header, footer, `.icon-btn`, `.chip` alias). Every view
follows this file.

## 1. Geometry, depth, spacing

| Token | Value | Use |
|---|---|---|
| `--radius-xs` | 2px | tags, inputs, selects, buttons, segmented, legend items, code, icon buttons |
| `--radius-sm` | 3px | popovers, tooltips, the stale-load note |
| `--radius` | 4px | cards. Tables, strips, bands, rows: **0** (write `border-radius: 0`). |
| `--radius-lg` | = `--radius` | retired alias, replace on sight. Nothing is rounder than 4px except the switch track/thumb. |
| `--shadow-1`, `--shadow-2` | `none` | retired. Cards and tiles have a 1px `--border`; hover = `--border-strong`. |
| `--shadow-pop` | faint drop | popovers, tooltips, the stale note only (always with a 1px `--border-strong`). |
| `--sp-1..6` | 4 · 8 · 12 · 16 · 24 · 32 | gaps 8–12, card padding 14–16 (`.card-pad`), tile padding 10–12, section spacing 32 (`--sp-6`), never 64. |
| `--header-h` | 52px | header; sticky offsets are `calc(var(--header-h) + 12px)`. |

Sections are separated by a hairline, not air: `.section-head` = `margin-top: 32px; padding-top: 16px;
border-top: 1px solid var(--border)`, then an `.eyebrow` (optional) and the `.section-title`. No bars, no markers.

## 2. Typography — Montserrat (`--font-sans`; canvas/SVG strings use `FONT_SANS` from `ui.tsx`)

| Token | Size | Weight / tracking / leading | Used for |
|---|---|---|---|
| `--fs-display` | clamp(40px, 5.4vw, 60px) | 700 · -0.035em · 1.0 | the home hero title only (`.gradient-text`) |
| `--fs-h1` | clamp(22px, 2.4vw, 28px) | 700 · -0.025em · 1.15 | page titles (`h1` default) |
| `--fs-h2` | clamp(17px, 1.7vw, 20px) | 700 · -0.02em · 1.2 | section titles (`.section-title`, `h2` default) |
| `--fs-h3` | 15px | 600 · -0.01em · 1.3 | card titles (`h3` default) |
| `--fs-lede` | 15px | 400 · 1.5 | the one paragraph under a page title (`.lede`, `--text-2`) |
| `--fs-body` | 14px | 400 · 1.55 | prose (`--text-2`) |
| `--fs-ui` | 13px | 500 (active 600) · 1.3 | nav, tabs, buttons, selects, segmented, switch labels, table cells |
| `--fs-sm` | 12px | 500 · 1.4 | sentence-case tags, legend items, crumbs, captions, tooltip rows, counts |
| `--fs-xs` | 11px | 600 · +0.06em · uppercase | **the label style** (below) |
| `--fs-stat` | clamp(18px, 1.6vw, 21px) | 600 · -0.02em · 1.1 · tabular | stat values |

**Label style** (`.eyebrow`, `.label`, `.stat-label`, `.table th`, `.tag` default): 11px, 600, +0.06em, uppercase,
`--text-3`. Eyebrows carry no bar. Numbers that align (table columns,
stat values, ticks, tooltip rows, counts, replay clocks) are `.tabular` and right-aligned in tables; nothing below
11px. Weights: 400 prose · 500 UI text and cells · 600 labels, active controls, card titles, stat values, `<b>` ·
700 h1/h2/hero · never 800. Active nav/tab links keep their weight (ink color + underline only) so widths never
shift. Montserrat wraps where the old face fit: shorten the label → `nowrap` + ellipsis → one size step down → let
the row wrap. Check 390px.

## 3. Color vocabulary — the complete list. Nothing else carries a hue.

Surfaces are neutral: `--page` body · `--surface` cards, controls, header, strips · `--surface-2` insets, table
head, row hover · `--surface-3` control hover, meter track. Text is ink: `--text` headings/values/active ·
`--text-2` body/labels · `--text-3` captions/eyebrows/resting icons. No `color-mix` of a hue into any surface,
border or text. `--tint-*`, `--edge-*`, `--glow-*`, `--brand-tint*`, `--brand-ring`, `--brand-hairline`,
`--key-mix-*`, `--raised` are retired: they now resolve to neutral/none so leftovers degrade, but delete them.

| Where | Class / token | Spec |
|---|---|---|
| Active tab / nav link | `[aria-current="page"]` | ink text + 2px `--accent` underline sitting on the toolbar/header rule. No fill. |
| Focus | `:focus-visible` | 2px `--focus` ring, 1px offset, 2px radius |
| Selection / brush / scrubber / switch on / spinner | `--accent`, `--accent-wash` | the one state hue (indigo). Chrome, not data; outside the budget. |
| Section strip top rule | `.accent-rule` (+ inline `--key`), `.card.keyed` | `border-top: 2px solid var(--key)` on a strip or card about one series |
| Stat key | `<StatTile color="var(--c-*)">` → `.stat.keyed` | 6px `.key-dot` before the label; value stays ink |
| Legend key | `<Legend>` / `<LegendKey>` | the series mark on a neutral item; off = struck label, mark at 35% |
| Status / category dot | `.key-dot` + inline `--key` | 6px circle; inside `.tag`, table cells, status text |
| Heading icon | `.section-icon` + inline `--key` | a 16px line icon stroked in the key (default `--text-3`). **No square behind it.** |
| Meter | `.meter > .meter-fill` + `--key`, `style={{width}}` | 4px track `--surface-3`, radius 0, fill in the key; the % is ink text beside it |
| Keyed rule | `.rule-key` + `--key`; `.error-note` (red) | 2px left rule on a quote or note |
| Hero title / 404 numeral | `.gradient-text` | `--brand-gradient` clipped to text. The only gradient in the app. |
| Brand mark | `<BrandMark>` | the gradient tile, 22px, rx 4/32 — header and footer only |
| Chart marks & washes | `--c-*`, `--wash-*` | the validated series palette, unchanged |

**Per-view budget (chrome only; chart marks, legend keys and meter fills are exempt): ≤ 2 accent hues.**
Home: none (hero gradient + `--accent`) · Timeline: `--c-whale`, `--c-crowd` stat keys · Traders: `--c-critical`
(flagged keys, pull-quote rule), `--c-whale` · 404: gradient numeral. Dark mode: same rules,
dark surfaces, hairlines one step lighter (`--border #2a2e36`); keys clear 3:1 on `--surface`.

## 4. Components (as shipped)

- **Card** `.card`: `--surface`, 1px `--border`, 4px, no shadow; interactive hover `--border-strong`; `.card-pad`
  16 (14 under 720px); `.card.keyed` = 2px top rule. No top bars, colored borders, rings or tints.
- **Tag** `.tag` (replaces every chip/pill): inline-flex, 2px, 1px `--border`, transparent, `padding: 2px 6px`, label
  style, `--text-2`, `<b>` ink 600, `svg` `--text-3`. `.tag.sentence` = 12px/500 sentence case, `padding: 2px 7px`
  (dates, "34,924 positions"). Key = a `.key-dot` inside. `a.tag`/`.tag.link` hover = ink border; `.tag.filled`
  = `--surface-2` only when the tag sits on `--surface-2`. The Resolved tag is `ResolvedChip` from `Home.tsx`
  (`.resolved` in shell.css): a key dot + "Resolved · Yes", the dot's colour set by `data-outcome` — leave it. `.chip`/`.chip.raised`/`.chip.tinted`
  are aliases of `.tag.sentence` for this round; rename to `.tag` on sight.
- **Stat strip** `.stat-strip` (alias `.stat-grid`) of `<StatTile>` cells: one ruled row with 1px grid lines
  (`gap: 1px; background: var(--border); border: 1px solid var(--border)`), radius 0; cells `--surface`,
  `padding: 10px 12px`; 2 columns under 900px (odd last cell spans), `--cols` (default 5) above — set
  `style={{ "--cols": 4 }}` for a set of four. Cell = `.stat-label` (label style, `.key-dot` when keyed, optional
  12px `icon`) over `.stat-value` (`--fs-stat`, tabular). `emphasis` = value at 700. Never place strips inside tinted group cards.
- **Table** `.table`: `border-collapse`, 13px/500 `--text-2`; `th` label style + `border-bottom: 1px solid var(--border-strong)`;
  `td` `padding: 8px 10px` + 1px `--border`; `.num` right + tabular; `.ink` = ink 600; row hover `--surface-2`.
  No zebra, no radius, no outer border. Status cells = `.key-dot` + text.
- **Segmented** `.segmented`: hairline box (1px `--border-strong`, 2px, `--surface`); options 13px/500, `padding: 5px 10px`,
  hairline-separated; **active = inverted ink cell** (`--text` fill, `--text-inverse` text, 600). No track, no raised face.
  Inside a Toolbar it never scrolls: the options wrap into a ruled grid of cells (1px gap over `--border`, cells grow to
  fill their row, the active cell's 1px ink outline covers its seams), so every option is on screen at 390px.
- **Toolbar** `<Toolbar>` + `<ToolbarGroup>` (`.toolbar*`): every sort / filter row on every page. One `.btn-ghost`-style
  button ("Filter", "Sort & filter") with a 6px `--accent` dot while something is applied; the bar (`--surface-2`, one
  hairline, radius 0, `padding: 8px 12px`, `gap: 8px 20px`) pushes out underneath only when open and holds labelled
  groups (label style + a Segmented / Select / Switch) and a text "Clear" link at the end. Nothing is applied by
  default and the bar starts closed unless the URL carries a sort or filter. Under 640px a group with a Segmented
  stacks its label above the control and takes the whole bar.
- **Meta line** `.meta-line` (+ `.label` for small caps): one wrapping line of facts, `<b>` ink. The " · " separator is
  an `::after` on the item before it, 8px from both neighbours, so a wrapped line never opens with a dot.
- **Switch** `.switch`: 28×16 track (`--border-strong`; on = `--accent`), 12px white thumb — the only pill.
- **Select** `.select`: 2px, 1px `--border-strong`, `--surface`, 13px/500, `padding: 5px 26px 5px 9px`, `min-width: 0`.
- **Button** `.btn`: 2px, 13px/500, `padding: 5px 10px`; `.btn-ghost` 1px `--border-strong` + `--surface` (hover `--surface-2`,
  ink border); `.btn-solid` (= `.btn-brand`) ink fill 600, one per view; `<Button size="sm">` → `.btn-sm` 12px, `3px 8px`.
- **Legend** `.legend-item`: 12px/500, 2px, `padding: 2px 6px`; toggles carry a 1px `--border` (hover ink border +
  `--surface-2`), off = struck `--text-3` + mark at 35%. **Tooltip** `.tooltip`: `--surface`, 1px `--border-strong`,
  3px, `--shadow-pop`, `padding: 8px 10px`, 12px; `tip-title` 12px/600.
- **Popover** `.popover`: same box as the tooltip, `padding: 10px 12px`, `min-width: 220px`, absolute, 2px below its
  trigger; `.popover-row` = label + control. Trigger = `.icon-btn.sm` (28px), `.icon-btn.on` while open.
- **Header** (`shell.css`): 52px, `<BrandMark>` + 14px/700 wordmark · section links 13px/500 running the full
  height, current = ink + 2px `--accent` underline on the header rule · 32px `.icon-btn`s. One hairline below.
- **Footer**: one ruled strip, `margin-top: 32px`, `padding: 14px 0`, 12px `--text-3`, flex row wrap.
- **Page title block** (`market.css`): 12px crumbs → `h1` (`margin-top: 6px`) → `.market-meta` row of `.tag.sentence`
  (gap 6) → `.market-toolbar` (`margin-top: 14px`, one hairline) with text `.view-tab`s (first flush left, sliding
  underline) + a flat `<Select>`. The flagged count is `.tab-count`: 12px tabular `--text-3` + red `.key-dot` when > 0.
- **Steps / method**: a hairline grid of cells (`gap: 1px; background: var(--border)`), each `.label` number + `h3` +
  13px text. No icon badges, squares, chevron circles or tints.

## 5. Migration — what each view deletes this round (the frame above is already live)

- **Home** (`home.css`): `.hero::before` glow; the hero's own 92px size (use `--fs-display`); eyebrow bars; `.step`
  tints, icon squares, chevron circles → hairline grid; `.market-card` gradient ring/shadow → `.card` hover; legend
  box → plain `<Legend>`; hero facts → `.tag.sentence`; `.section-title` overrides → `.section-head`.
- **Timeline** (`timeline.css`): `.tl-ctrl` gradient band + rule → `--surface-2` band or none; `.tl-title-key` bar
  → none; stat groups (`.tl-stat-group` tints/edges) → one `.stat-strip` per group with a `.accent-rule` in the
  group's series; scrubber → `--accent` fill on `--border-strong`; Play → `.btn-ghost` / `.btn-solid`; `.tl-speed`
  overrides → default `.segmented`; `FONT` strings → `FONT_SANS`.
- **Traders** (`traders.css`, `paint.ts`): eyebrow pill → `.eyebrow` + `.key-dot`; narrative card tint/border →
  `.card` + `.rule-key`; chart-card gradient top bars → none; icon squares → `.section-icon`; `.tr-legend` pills →
  default `.legend-item`; treemap "All whales" chip → `.tag`; `FONT_STACK` → `FONT_SANS`.

## 6. DON'T

- No pills: nothing with `border-radius` above 4px (switch excepted). No `999px`, no `--radius-lg`.
- No shadows on cards, tiles, chips, bands or buttons; `--shadow-pop` only on popovers/tooltips/stale note.
- No tinted surfaces, glows, radial washes, gradient bands/hairlines/top bars, brand rings, colored card borders.
- No colored text, no text on a colored fill (except `--text-inverse` on ink), no gradient outside `.gradient-text`.
- No icon-in-tinted-square badges; icons are line glyphs in `--text-3` or a key.
- No filled status chips; identity is a `.key-dot` + text. No rainbow rows; ≤ 2 hues of chrome per view.
- No 800 weight, no 92px type, no 64px section gaps, no `font-family` or hex in a component — tokens only.
