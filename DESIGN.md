---
name: Sparton
description: Competitor prices for small webshops, shown as a split-flap departure board.
colors:
  wall: "#e3e4e0"
  wall-recessed: "#d6d8d3"
  plate: "#f7f7f4"
  rule: "#b9bcb6"
  rule-strong: "#8d918b"
  ink: "#17191b"
  ink-2: "#43474b"
  ink-3: "#5c6065"
  board: "#17191b"
  board-frame: "#202326"
  flap: "#26292d"
  flap-top: "#2e3236"
  flap-seam: "#0b0c0d"
  chalk: "#f0ede4"
  chalk-2: "#b5b3ab"
  signal: "#f5c518"
  signal-hover: "#ffd43b"
  signal-edge: "#c99f00"
  signal-ink: "#17191b"
  signal-deep: "#8a6a00"
  down: "#e2553f"
  up: "#4cb874"
  down-ink: "#a8301c"
  up-ink: "#1c6e3f"
typography:
  display:
    fontFamily: "Archivo, Arial Narrow, system-ui, sans-serif"
    fontSize: "clamp(2.5rem, 6vw, 4.75rem)"
    fontWeight: 800
    lineHeight: 0.98
    letterSpacing: "-0.015em"
    fontVariation: "'wdth' 70"
  headline:
    fontFamily: "Archivo, Arial Narrow, system-ui, sans-serif"
    fontSize: "2rem"
    fontWeight: 800
    lineHeight: 1.08
    letterSpacing: "-0.01em"
    fontVariation: "'wdth' 75"
  title:
    fontFamily: "Archivo, Arial Narrow, system-ui, sans-serif"
    fontSize: "1.375rem"
    fontWeight: 800
    lineHeight: 1.08
    fontVariation: "'wdth' 75"
  lede:
    fontFamily: "Archivo, Arial Narrow, system-ui, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 400
    lineHeight: 1.55
  body:
    fontFamily: "Archivo, Arial Narrow, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: "Archivo, Arial Narrow, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.5
  board-title:
    fontFamily: "Archivo, Arial Narrow, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 700
    letterSpacing: "0.08em"
    fontVariation: "'wdth' 75"
  flap:
    fontFamily: "Archivo, Arial Narrow, system-ui, sans-serif"
    fontWeight: 650
    lineHeight: 1
    fontFeature: "'tnum' 1, 'lnum' 1"
    fontVariation: "'wdth' 75"
rounded:
  tile: "3px"
  plate: "6px"
spacing:
  s1: "4px"
  s2: "8px"
  s3: "12px"
  s4: "16px"
  s5: "24px"
  s6: "32px"
  s7: "48px"
  s8: "72px"
  s9: "112px"
components:
  button-primary:
    backgroundColor: "{colors.signal}"
    textColor: "{colors.signal-ink}"
    rounded: "{rounded.tile}"
    padding: "0 16px"
    height: "44px"
  button-primary-hover:
    backgroundColor: "{colors.signal-hover}"
  button-default:
    backgroundColor: "{colors.plate}"
    textColor: "{colors.ink}"
    rounded: "{rounded.tile}"
    padding: "0 16px"
    height: "44px"
  button-default-hover:
    backgroundColor: "{colors.wall-recessed}"
  button-board:
    backgroundColor: "{colors.board}"
    textColor: "{colors.chalk}"
    rounded: "{rounded.tile}"
    padding: "0 16px"
    height: "44px"
  button-lg:
    padding: "0 24px"
    height: "52px"
  button-sm:
    padding: "0 12px"
    height: "36px"
  flap:
    backgroundColor: "{colors.flap}"
    textColor: "{colors.chalk}"
    rounded: "{rounded.tile}"
    typography: "{typography.flap}"
  strip:
    backgroundColor: "{colors.flap}"
    textColor: "{colors.chalk}"
    rounded: "{rounded.tile}"
    padding: "0 0.5em"
    height: "1.9em"
  board:
    backgroundColor: "{colors.board-frame}"
    textColor: "{colors.chalk}"
    rounded: "{rounded.plate}"
    padding: "16px"
  board-demo-plate:
    backgroundColor: "{colors.signal}"
    textColor: "{colors.signal-ink}"
    padding: "2px 8px"
  plate:
    backgroundColor: "{colors.plate}"
    textColor: "{colors.ink}"
    rounded: "{rounded.plate}"
    padding: "24px"
  input:
    backgroundColor: "#ffffff"
    textColor: "{colors.ink}"
    rounded: "{rounded.tile}"
    padding: "0 12px"
    height: "44px"
  lang-switch-active:
    backgroundColor: "{colors.signal}"
    textColor: "{colors.signal-ink}"
    height: "36px"
    width: "40px"
---

# Design System: Sparton

## Overview

**Creative North Star: "The Price Board"**

Sparton is a departure board for competitor prices. Data lives on graphite boards built from split-flap tiles (one character per tile) and whole-word name strips, the way a station board shows times and destinations. Everything around the boards is a light concourse wall (`wall`) carrying enamel plates (`plate`) for forms, reports and settings. Station yellow (`signal`) is the one action colour. The landing page's first viewport is the board itself, cycling a labelled demo week; the dashboard's first view is the same board with the user's real changes.

The system is dense where the data is and quiet everywhere else. Boards are the only dark surfaces inside the app; the wall stays light so reading sections (reports, plans, settings) read like printed notices. Certainty is the product's honesty claim, and the visual system carries it as a mark: exact rows are solid tiles, extracted rows are hatched, sold-out items are struck through. Motion obeys the physical object: flaps turn in whole steps, never tween.

Tokens and shared atoms live in `app/web/tokens.css`; the dashboard in `app/web/styles.css`; the public pages in `app/web/landing.css`; the flap and board-row DOM builders in `app/web/board.js`.

**Key Characteristics:**
- Graphite board, chalk characters, concourse-grey wall, one yellow.
- Certainty shown by texture and line (solid / hatched / struck / dashed), never by hue alone.
- Counts and prices are flap tiles in board title bars, not KPI cards.
- Whole-step motion (`steps()`), snapped under reduced motion.
- One condensed variable family (Archivo, width axis 62–125%) for everything.
- Every visible string comes from i18n keys in Dutch and English.

## Colors

A near-monochrome graphite-and-grey world with one saturated yellow for action and two board-only signal hues for direction of a price move.

### Primary
- **Station Yellow** (`signal`): the one action colour. Primary buttons ("Start free"), the active nav underline (4px), the pressed language button, the DEMO plate on board frames, the email-verify banner, unread dots on board rows, focus outline, text selection, and status strips (SOLD OUT / NEW) on the board. Hover is `signal-hover`; button border `signal-edge`. Text on yellow is always `signal-ink`.
- **Station Yellow, deep** (`signal-deep`): yellow-family text on the light wall (report status, warnings), where the bright yellow would fail contrast.

### Secondary (board signals)
- **Cheaper Red** (`down`) / **Dearer Green** (`up`): only on graphite, in the Change column and onboarding rows. A competitor getting cheaper is red because it threatens the user. Always paired with a `−`/`+` sign in the tiles.
- **Cheaper Red, wall** (`down-ink`, also `--danger`) / **Dearer Green, wall** (`up-ink`, also `--ok`): the same meanings on light ground.

### Neutral
- **Concourse Wall** (`wall`): page background of the app and reading sections.
- **Recessed Wall** (`wall-recessed`): plan section band, table header fills, notes, button hover.
- **Enamel Plate** (`plate`): forms, panels, tables on the wall.
- **Hairline** (`rule`) / **Strong Hairline** (`rule-strong`): dividers and borders; strong for input and button borders.
- **Ink** (`ink`), **Ink 2** (`ink-2`), **Ink 3** (`ink-3`, ≥4.5:1 on wall and plate): text tiers on the wall.
- **Graphite Board** (`board`), **Board Frame** (`board-frame`): top bars, hero, close band, board panels.
- **Flap** (`flap`), **Flap Top** (`flap-top`), **Seam** (`flap-seam`): the tile's two halves and the 1px split between them.
- **Chalk** (`chalk`) / **Chalk 2** (`chalk-2`, ≥4.5:1 on flap): primary and secondary characters on the board.

### Named Rules
**The One Yellow Rule.** Station yellow marks the one thing to do (or the one thing that is "now"). A screen has one yellow button; a second CTA is a ghost or board button.

**The Mark-Not-Hue Rule.** Certainty is never a colour. Exact = solid tile, extracted = hatched tile (`repeating-linear-gradient(-45deg, flap 0 4px, #3a3e43 4px 6px)`), sold out = struck text (2px line-through), unknown = dashed outline. Direction colours always travel with a sign.

## Typography

**Display / Body / Label Font:** Archivo, self-hosted variable (`app/web/fonts/archivo-latin*.woff2`, weight 100–900, width 62–125%), fallback Arial Narrow, system-ui.

**Character:** One condensed grotesque doing every job. Headings and tiles squeeze the width axis (70–80%) to read like painted signage; body copy runs at 100% width for comfort. Numbers are tabular and lining everywhere a price appears (`.tnum`, `.price`, flaps).

### Hierarchy
- **Display** (800, `clamp(2.5rem, 6vw, 4.75rem)`, 0.98, width 70%, max 12ch): landing and comparison hero h1 only. 2.6rem on phones.
- **Headline** (800, 2rem / `--fs-4`, width 75%): view titles, story steps, report and plan headings. Section h2s on the landing use `clamp(2rem, 4vw, 3rem)`.
- **Title** (800, 1.375rem / `--fs-3`): plate headings, shop names, plan names.
- **Lede** (400, 1.0625rem / `--fs-2`, 1.55, 44–60ch): hero lede, step body, section ledes.
- **Body** (400, 0.9375rem / `--fs-1`, 1.55): default; prose capped at 72ch.
- **Label / Meta** (0.8125rem / `--fs-0`): hints, legends, footers, fine print.
- **Board title** (700, `--fs-0`, uppercase, 0.08em tracking, width 75%): board title bars and board column heads (column heads at 0.75rem). This is station signage, native to the board.

### Named Rules
**The Squeeze Rule.** Headings (h1–h3) default to width 75%, line-height 1.08, `text-wrap: balance`. Body text is never condensed.

**The Tabular Rule.** Every price, count and percentage uses tabular lining figures, so columns of numbers align like a timetable.

## Layout

- **Container:** 1240px max, 24px side gutter (16px under 640px). Auth stage 1080px; onboarding 760px; settings blocks 640px.
- **Spacing:** 4px base scale `s1`–`s9` (4, 8, 12, 16, 24, 32, 48, 72, 112). Views stack at 24px; landing sections pad 72px vertically.
- **Landing grid:** hero and story are 5fr / 7fr (copy / board), 48px gap; hero fills `100dvh − 64px`. The story pins the board (`position: sticky; top: 96px`) beside scrolling steps (each ~72vh). Plans are three equal columns; FAQ 4fr / 8fr.
- **Board grid:** five columns, `minmax(0,1.2fr) minmax(0,2fr) 6.6em 6.6em 5.6em` (shop, product, was, now, change). The week board adds a 2.4em evidence column. Column headers share the rows' font-size so em widths align.
- **Breakpoints:** 1100px (hero and story collapse to one column; story stage becomes a sticky band under the bar), 900px (dashboard nav collapses into a drawer behind a Menu button), 860px (landing nav hides; plans, reports split and auth stack to one column), 760px (comparison table becomes stacked cards with Sparton/Prisync labels), 640px (board rows re-flow).
- **Board on phones (≤640px):** column header hidden; each row becomes a 2×2 grid, areas `"a e" "b d"` (shop + change on top, product + now below); the "was" column is dropped. Hero reorders so the board comes straight after the headline and CTA.
- **Touch targets:** 44px minimum for buttons, inputs, nav items and toggles (36px for small buttons and the language switch).

## Elevation & Depth

Mostly flat, with two physical shadows that describe real objects: enamel plates sit slightly proud of the wall, and boards hang heavy from it. No other surface casts a shadow.

### Shadow Vocabulary
- **Plate** (`--shadow-plate`: `0 1px 0 rgb(255 255 255 / 0.6) inset, 0 2px 6px -2px rgb(23 25 27 / 0.18)`): plates, auth card, report teaser.
- **Board** (`--shadow-board`: `0 18px 40px -18px rgb(23 25 27 / 0.55), 0 2px 4px rgb(23 25 27 / 0.2)`, plus `inset 0 0 0 6px board` for the painted frame): board panels, mobile nav drawer.
- **Tile lip** (`0 1px 0 rgb(0 0 0 / 0.5)`): under every flap and strip.

### Named Rules
**The Physical Object Rule.** A shadow is allowed only if the thing exists in the world: a plate on a wall, a board on a wall, a tile in a board.

## Shapes

Tight, machined corners. Tiles and buttons use `tile` (3px); plates and boards use `plate` (6px); the DEMO plate, plan flag and legend tiles use 2px. No pills in the board world. Every flap and strip has a 1px `flap-seam` line across its vertical middle (a `::after` pseudo-element) over a two-tone gradient (`flap-top` above, `flap` below); the seam is what makes it read as a flap. Board rows are separated by 1px black rules. Progress and limits are drawn as rows of small empty/filled tiles (18×26px), not bars.

## Components

### Flap tile (`flapWord`, `flapTo` in `board.js`)
One uppercase character per tile, tabular, chalk on the flap gradient with the seam. A row of tiles has `role="img"` and one `aria-label` (the whole word), never letter-by-letter. `flapTo` walks each tile along the drum (`" A–Z 0–9 €.,-−+%/:"`) at most 8 steps, 55ms per step, each tile starting one step after its left neighbour. Blank tiles keep their size (`data-blank`). Used for the brand mark, prices, percentages, plan prices and counters.

### Strip
A whole-word name on one tile (shop names, product names), width 80%, weight 600, ellipsis on overflow, 1.9em high. Turns as a single flap (110ms, 2 steps). A **status strip** (`cell-span`) spans the last two columns for words too long for figures (SOLD OUT, NEW, FOUND), like a CANCELLED strip; NEW and back-in-stock are yellow.

### Board
Graphite frame panel (`board-frame` inside a 6px painted `board` inset, black 1px border, board shadow). Head row: board title (left), DEMO plate or a link (right). **Counters live in the board's title bar as flap tiles** with a small chalk-2 label (`board-counts`); a warning count turns its tiles `down`. Below: optional column header, `<ol>` of rows, legend (solid / hatched / struck samples) as a `figcaption`. Empty state is a single "quiet" line inside the board, not an illustration. Loading dims rows in two steps.

### Board row (`boardRow` in `board.js`)
Five cells: strip (shop), strip (product), figure (was), figure (now), figure (change) or a status strip. Row class `mark-row-exact | mark-row-extracted` hatches every tile in an extracted row; `is-struck` strikes the names. `data-tone` = `down | up | stock | new` colours the change tiles. Visual cells are `aria-hidden`; a visually hidden sentence reads the row once, with "(extracted)" appended when relevant. On the week board, a full-row link (`row-open`) plus a separate evidence button (↗, 2.2em) that turns yellow on hover; unread rows get a yellow dot before the shop name.

### DEMO plate
Small yellow enamel plate (0.75rem, weight 800, uppercase, 2px radius) on any board showing demo data. Required wherever the data is not the user's.

### Buttons
- **Shape:** 3px corners, 1px border, min 44px tall, weight 650, width 87%.
- **Primary:** station yellow with `signal-edge` border, `signal-ink` text. One per screen.
- **Default:** plate background, strong-hairline border; hover recesses to `wall-recessed`.
- **Board:** graphite with chalk text (used on the yellow verify banner).
- **Ghost:** transparent; on dark grounds the ghost has a chalk-2 border and hovers to `flap`.
- **Danger:** plate background, danger text and border.
- **Sizes:** sm 36px, lg 52px. Press moves 1px down. Loading shows a small spinner and blocks pointer events.

### Language switch (`langSwitch` in `i18n.js`)
NL / EN segmented toggle, 1px currentColor border, 3px corners, width 75%, weight 700, uppercase. `role="group"`, each button `aria-pressed` and `lang`; the pressed one is yellow. Switching stores the choice and reloads.

### Source tag (`src-tag`)
Certainty in tables and lists on the wall: a 14px square before the label, solid graphite for exact, hatched graphite/grey for extracted, dashed outline for no data. Competitor lists use the same marks at 18px (`comp-mark`), with solid danger for failed.

### Price-history chart (`views/product.js`)
Hand-built SVG on a plate. Prices are a **step line** (a price holds until the next reading), never a slope. Competitors are dashed (`ink-2`, 2.5px, `7 5`); the user's own price is solid ink in the legend but marked "not tracked yet" until the backend records it (D-031 in `docs/DECISIONS.md`). Readings are 5px dots: filled ink = exact, hollow = extracted; sold-out readings get a danger tick above the dot. Vertical rules fall on Mondays (ISO week labels); horizontal rules are dotted at round steps (1, 2, 2.5, 5 × 10^k). The SVG has `role="img"` and a one-sentence summary; a readings table sits below it.

### Plates / containers
Enamel plate: `plate` background, hairline border, 6px corners, plate shadow, 24px padding (16px on phones). Plans are not plates: each tier is its own small board (`planBoard` in `plans.js`, shared by the landing page and Billing) with the name, the price on tiles, its limits as flap rows (Shops / Competitors / Checks) and extras in chalk. The featured tier gets a 2px station-yellow border and the yellow flag; the current tier sits on the darker board. Signed-out screens put a labelled DEMO board of three change rows beside the form.

### Inputs / fields
White fill, strong-hairline border, 3px corners, 44px tall, label above at `--fs-0` weight 600 in `ink-2`. Focus uses the global yellow outline without the ink halo. Password fields carry an inline Show button.

### Navigation
Graphite top bar, sticky, 60px (56px mobile). Brand is a flap word. Items are chalk-2, hover chalk; the current page is chalk with a 4px yellow bar along the bottom. Under 900px the nav becomes a drop-down drawer (220ms ease-out), current item marked by a 10px yellow square.

### Legacy components (still live)
`.card`, `.badge`, `.empty`, `.error-state`, `.banner`, toasts, `dialog`, `.table`, `.metric` in `styles.css` still serve the helpers in `app/web/ui.js` and older views. They read from legacy alias tokens (`--surface`, `--text-2`, `--primary` …) mapped onto the board palette in `tokens.css`. Keep them working; do not use them as patterns for new surfaces.

## Do's and Don'ts

### Do:
- **Do** put data on a board: shops and products as strips, prices and percentages as flap tiles, through `boardRow` / `flapWord` rather than hand-built markup.
- **Do** show certainty with marks: solid for exact, hatched for extracted, struck for sold out, dashed for unknown; in charts, the user's price solid and competitors dashed.
- **Do** animate flaps only with `steps()` and the 55ms step; let `prefers-reduced-motion` snap them (both `board.js` and the global reduced-motion rule do this).
- **Do** put counters in the board title bar as tiles.
- **Do** label demo data with the yellow DEMO plate on the board frame.
- **Do** add every string as an i18n key in both `i18n/en.json` and `i18n/nl.json`, and translate static HTML with `data-i18n` / `data-i18n-attr`.
- **Do** give each board row one screen-reader sentence and hide the tiles from assistive tech.
- **Do** keep focus visible: 3px yellow outline, 2px offset, 5px ink halo.
- **Do** use screenshots of real HTML (like `og-card.html` → `og-image.png`) for any raster.

### Don't:
- **Don't** tween a flap, fade a digit, or count numbers up smoothly; a tile shows one character or the next.
- **Don't** add a second action colour or a second yellow button on the same screen.
- **Don't** encode exact/extracted in hue, and don't use `up`/`down` without the sign.
- **Don't** build KPI cards or metric tiles for counts; the legacy `.metric` grid is not a pattern.
- **Don't** hard-code copy in JS or HTML without an i18n key.
- **Don't** use generated imagery, stock photos or illustrations.
- **Don't** put `down`/`up` (the bright board hues) on the light wall; use `down-ink`/`up-ink`.
- **Don't** add new shadows beyond plate, board and tile lip.
