---
name: Market Data Quality & Analytics
description: A calibration certificate for a data feed — ruled, typed, and printed in two colours.
colors:
  stock: "#F2F3EF"
  stock-sunk: "#E7E9E3"
  ink: "#1C1C1A"
  ink-muted: "#5A5F58"
  reference: "#23457E"
  reference-deep: "#1A3462"
  out-of-tolerance: "#A62A21"
  warning-ink: "#8C5A10"
  expected-ink: "#656960"
  expected-field: "#75796F"
  security: "#C8D6C9"
  hairline: "#9AA09B"
  activity-mid: "#7C97BE"
  reversed: "#FFFFFF"
typography:
  certificate-title:
    fontFamily: "Noto Sans, ui-sans-serif, system-ui, sans-serif"
    fontSize: "2.1rem"
    fontWeight: 800
    lineHeight: 0.98
    letterSpacing: "-0.012em"
  certificate-subtitle:
    fontFamily: "Noto Sans, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.72rem"
    fontWeight: 400
    lineHeight: 1.3
    letterSpacing: "0.14em"
  observed-figure:
    fontFamily: "Courier Prime, ui-monospace, SFMono-Regular, monospace"
    fontSize: "2.9rem"
    fontWeight: 700
    lineHeight: 0.92
    letterSpacing: "-0.03em"
    fontFeature: "tnum 1"
  plate-count:
    fontFamily: "Courier Prime, ui-monospace, SFMono-Regular, monospace"
    fontSize: "1.9rem"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.02em"
    fontFeature: "tnum 1"
  page-heading:
    fontFamily: "Noto Sans, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.35rem"
    fontWeight: 800
    letterSpacing: "0.055em"
  clause:
    fontFamily: "Noto Sans, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.05rem"
    fontWeight: 700
  sub-clause:
    fontFamily: "Noto Sans, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.92rem"
    fontWeight: 700
    letterSpacing: "0.02em"
  body:
    fontFamily: "Noto Sans, ui-sans-serif, system-ui, sans-serif"
    fontWeight: 400
    fontFeature: "tnum 1"
  reading-note:
    fontFamily: "Noto Sans, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.6
    maxWidth: "92ch"
  note:
    fontFamily: "Noto Sans, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.8rem"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: "Noto Sans, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.7rem"
    fontWeight: 600
    letterSpacing: "0.13em"
  plate-name:
    fontFamily: "Noto Sans, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.66rem"
    fontWeight: 700
    letterSpacing: "0.17em"
  typed-field:
    fontFamily: "Courier Prime, ui-monospace, SFMono-Regular, monospace"
    fontSize: "0.73rem"
    fontWeight: 400
    fontFeature: "tnum 1"
rounded:
  none: "0"
spacing:
  hair: "0.3rem"
  rule-gap: "0.4rem"
  plate-gap: "0.6rem"
  band-inset: "0.85rem"
  gutter: "1.1rem"
  head-gap: "1.5rem"
  clause-gap: "2rem"
components:
  certificate-head:
    backgroundColor: "{colors.reference}"
    textColor: "{colors.reversed}"
    typography: "{typography.certificate-title}"
    rounded: "{rounded.none}"
    padding: "1.05rem 1.3rem 0.95rem"
  head-fields:
    textColor: "{colors.reversed}"
    typography: "{typography.typed-field}"
  guilloche:
    backgroundColor: "{colors.security}"
    height: "6px"
    rounded: "{rounded.none}"
  schedule-row:
    textColor: "{colors.ink}"
    typography: "{typography.observed-figure}"
    rounded: "{rounded.none}"
    padding: "0.62rem 0 0.58rem"
  schedule-row-fail:
    textColor: "{colors.out-of-tolerance}"
  deviation-bar-track:
    backgroundColor: "{colors.stock-sunk}"
    height: "12px"
    rounded: "{rounded.none}"
  deviation-bar-fill:
    backgroundColor: "{colors.ink}"
    height: "12px"
    rounded: "{rounded.none}"
  deviation-bar-fill-fail:
    backgroundColor: "{colors.out-of-tolerance}"
    height: "12px"
  state-plate:
    textColor: "{colors.ink}"
    typography: "{typography.plate-count}"
    rounded: "{rounded.none}"
    padding: "0.6rem 0.8rem 0.55rem"
  state-plate-error:
    textColor: "{colors.out-of-tolerance}"
  state-plate-warning:
    textColor: "{colors.warning-ink}"
  state-plate-info:
    textColor: "{colors.expected-ink}"
  traceability-band:
    backgroundColor: "{colors.reference-deep}"
    textColor: "{colors.reversed}"
    rounded: "{rounded.none}"
    padding: "0.85rem 1.1rem"
  countersignature:
    textColor: "{colors.ink-muted}"
    typography: "{typography.typed-field}"
    rounded: "{rounded.none}"
    padding: "0.7rem 0 0"
  button-secondary:
    backgroundColor: "{colors.stock}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
  button-secondary-hover:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.stock}"
  page-link-stamp:
    backgroundColor: "{colors.stock}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "0.5rem 0.8rem"
  page-link-stamp-hover:
    backgroundColor: "{colors.reference}"
    textColor: "{colors.reversed}"
  nav-item:
    textColor: "{colors.ink}"
    typography: "{typography.typed-field}"
    rounded: "{rounded.none}"
    padding: "0 0 0 0.65rem"
  nav-item-current:
    backgroundColor: "{colors.stock}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
  nav-step-current:
    backgroundColor: "{colors.reference}"
    textColor: "{colors.reversed}"
    padding: "0.06rem 0.26rem"
  note-banner:
    backgroundColor: "{colors.stock-sunk}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
  expander:
    backgroundColor: "{colors.stock}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
  sidebar:
    backgroundColor: "{colors.stock-sunk}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
---

# Design System: Market Data Quality & Analytics

## Overview

**Creative North Star: "The Calibration Certificate"**

The surface is dressed as the document that proves an instrument was checked against
something outside itself: a metrology calibration certificate, or a certificate of analysis.
It does not open with four big numbers in rounded tiles. It opens by declaring what was
measured and against what reference, then prints the deviations as a ruled schedule. That is
a direct consequence of what the product says (see `PRODUCT.md` § Product Purpose): every
finding is a measured deviation from a reference standard, and the vendor's own published
diagnostics are that standard.

The register is pale, cool and dense — certificate stock rather than cream paper, graphite
ink at reading weight, reference blue owning whole bands as a solid field rather than
appearing as an accent. The scene it is drawn for is a reviewer at a desk in daytime office
light reading tables, which is why the ground is light and why nothing glows. Density is
high and deliberate: rules, not gaps, separate the parts. Figures are set in the typed voice
at plate scale so that the one number that matters is visible from across the room, and the
denominator it was measured against sits immediately beside it.

The platform is **web**, and the surface is a Streamlit app with bespoke HTML/CSS components.
That constrains any consumer of this system in two concrete ways. First, most primitives are
Streamlit's own widgets brought into the world by a single injected stylesheet plus
`.streamlit/config.toml`; you style them through stable `data-testid` hooks, never generated
class names. Second, the signature components are pure HTML-string builders
(`src/mdq/dashboard/certificate.py`) rendered through `st.markdown(..., unsafe_allow_html=True)`
— they have no JavaScript, no client state and no framework runtime, and any new component
must hold to that.

**Key Characteristics:**

- Two colours carry meaning; everything else is ink on stock.
- Every severity carries a hatch and a word as well as a hue.
- Ruled, never stacked: zero radius, zero shadow, zero cards.
- Figures are typed in a monospace earned by measurement, never worn as a costume.
- A figure never appears without what it was measured against.

## Colors

A two-colour document: a cool pale stock with graphite ink, spending chroma only on the
reference standard and on a failed deviation.

### Primary

- **Reference Blue** (`{colors.reference}`): the standard measured against. It owns whole
  regions as a solid field — the certificate head, the current step's number in the nav, the
  VWAP line, the drawn tolerance limit on the coverage chart, link text, caret and selection.
  White on it measures 9.5:1, so the solid fields carry body text safely.
- **Reference Deep** (`{colors.reference-deep}`): the same field one step down, for the
  traceability band beneath the head, link hover, and the top of the activity ramp.

### Secondary

- **Out-of-Tolerance Vermilion** (`{colors.out-of-tolerance}`): a deviation that failed.
  The only red on the surface — failed schedule figures and bars, the ERROR plate's border
  and ink, ERROR marks in charts, and Streamlit's own `redColor`. Never decorative.
- **Warning Ochre** (`{colors.warning-ink}`): inside tolerance, outside comfort. The WARNING
  plate, WARNING chart marks, short minute-coverage bars, and the thin-window strip fill.
- **Expected Grey-Green** (`{colors.expected-ink}`): an expected artefact — present,
  recessive, never hidden. The EXPECTED plate and expected-tier chart marks.
  **Expected Field** (`{colors.expected-field}`) is the same meaning as Streamlit's remapped
  `grayColor` for `st.info`-class surfaces.

### Tertiary

- **Security Tint** (`{colors.security}`): the fine pale green of a real certificate's
  security line-work. Used for the guilloche strip under the head and nowhere else.
- **Activity Mid** (`{colors.activity-mid}`): the midpoint of the reference-blue activity
  ramp in the regime heatmap (sunk stock → this → reference deep). Permitted the reference
  hue because it is a measurement of trading activity, not a severity.

### Neutral

- **Certificate Stock** (`{colors.stock}`): the ground of the whole document, the app
  background, both Plotly paper and plot backgrounds, and the fill of a candle that closed
  up.
- **Recessed Ply** (`{colors.stock-sunk}`): sunk panels — the sidebar, note banners, the
  deviation bar's track, code and dataframe headers, the scrollbar track.
- **Graphite Ink** (`{colors.ink}`): the dominant weight. Body text, headings, the deviation
  bar's fill, candle outlines, plate borders, axis lines, hover labels, and the fill of a
  candle that closed down.
- **Reading Ink** (`{colors.ink-muted}`): secondary prose, captions, typed denominators,
  tick labels, tolerance-limit rules. Tinted from the ground, never grey-by-default.
- **Hairline** (`{colors.hairline}`): every rule, border, grid line and widget edge, plus
  the volume strip and the close line in the VWAP chart.
- **Reversed** (`{colors.reversed}`): text and figures on the two reference fields.
  Sub-levels appear as `rgba(255,255,255,.82)` for the head subtitle and
  `rgba(255,255,255,.72)` for field labels.

### Named Rules

**The Two Meanings Rule.** Colour is spent on exactly two things: the reference standard and
a failed deviation. Everything else is ink on stock. A third meaning would stop the first two
working — which is why candlesticks are hollow-versus-filled ink rather than green-versus-red,
and why Streamlit's `redColor` / `orangeColor` / `grayColor` are remapped onto the severity
vocabulary instead of introducing a fourth and fifth hue.

**The Second Channel Rule.** Severity never rides on hue alone. Every severity-coloured mark
also carries its ordered pattern — cross-hatch = ERROR, diagonal = WARNING, stipple =
EXPECTED — in the DOM (`theme.HATCHES`) and in Plotly (`theme.PATTERN_SHAPES`), and the state
is additionally printed in words at plate scale. Audit test: render the screen in greyscale;
if a tier is now ambiguous, the mark is missing its hatch.

**The Measured Contrast Rule.** Every ink token carries a ratio measured against the stock,
not assumed: ink 14.0:1, reading ink 5.9:1, out-of-tolerance 6.2:1, warning 5.3:1, expected
5.0:1. All clear 4.5:1 as text, so nothing on this surface leans on WCAG's 3:1
graphical-object allowance. A new ink must be measured before it ships.

## Typography

**Display / Body Font:** Noto Sans (self-hosted variable, 400–800), falling back to
`ui-sans-serif, system-ui, sans-serif`
**Label / Mono Font:** Courier Prime (self-hosted, 400 and 700), falling back to
`ui-monospace, SFMono-Regular, monospace`

**Character:** Noto Sans is the freely licensable half of Macquarie Group's own published
type system (the proprietary MCQ Global is not licensed here and is not imitated — see
`PRODUCT.md` § Brand Commitments), and it reads as institutional without wearing anyone's
trademark. Courier Prime is the typewriter that filled in the form. Together they behave like
a printed certificate: a set document in a sober sans, with the run-specific facts typed on.

### Hierarchy

- **Certificate Title** (`{typography.certificate-title}`, uppercase, max 20ch): the product
  name in the reference-blue head. The largest type on the surface. Drops to 1.5rem below
  820px.
- **Certificate Subtitle** (`{typography.certificate-subtitle}`, uppercase): what the
  document is, under the title. Tracking relaxes to 0.08em below 820px.
- **Observed Figure** (`{typography.observed-figure}`, typed face): the measured value in a
  schedule row. Drops to 2.1rem below 820px. Fail state takes out-of-tolerance ink.
- **Plate Count** (`{typography.plate-count}`, typed face): the count on a stamped state
  plate. Drops to 1.5rem below 820px.
- **Page Heading** (`{typography.page-heading}`, uppercase): one per page — which page of the
  certificate this is. 1.22rem below 820px.
- **Clause** (`{typography.clause}`) and **Sub-Clause** (`{typography.sub-clause}`): section
  headings. Both draw a 1px ink rule beneath themselves at 80% opacity, suppressed inside an
  expander or the sidebar so nothing is double-ruled.
- **Body** (`{typography.body}`): Streamlit's inherited base size, not overridden. Tabular
  figures are on globally, plus in metrics, dataframes and the typed class.
- **Note** (`{typography.note}`, max 74ch): captions and the explanatory column of a schedule
  row.
- **Label** (`{typography.label}`, uppercase): the "what was measured" clause of a schedule
  row, in reading ink at weight 600.
- **Plate Name** (`{typography.plate-name}`, uppercase, weight 700): the state, in words.
- **Typed Field** (`{typography.typed-field}`): everything typed onto the form. Sizes in use
  run 0.66rem (plate rule) to 0.76rem (denominators); the head's field grid is 0.73rem, the
  ratio figure 0.72rem at weight 700, the countersignature 0.7rem.

### Named Rules

**The Earned Monospace Rule.** Courier Prime sets only what is typed onto the form: report
numbers, measurements, thresholds, denominators, ratios, timestamps, axis tick values, hover
labels. It is earned by measurement and is never a costume for "technical". Prose in the
typed face is a defect.

**The Nothing-Outranks-the-Head Rule.** Heading sizes and weights live in
`.streamlit/config.toml` (`headingFontSizes`, `headingFontWeights`), not in CSS, because
Streamlit's own heading rules outrank an injected stylesheet. The ramp is deliberately small
(1.35rem → 0.78rem) so no page heading can out-scale the 2.1rem certificate title. Set sizes
there; keep character (case, tracking, rules) in the stylesheet.

**The No-Network Face Rule.** Both families are vendored as `latin`-subset woff2 under
`src/mdq/dashboard/static/fonts/` (OFL 1.1, provenance recorded in `PROVENANCE.md`) and
inlined into the stylesheet as base64 data URIs. A face fetched from a CDN would silently
fall back to the platform sans on a locked-down machine and undo the world. A missing file
is skipped rather than raised on.

## Layout

One centred column, `max-width: 1500px`, with `1.5rem` of top padding (`1.25rem` narrow), and
a persistent left sidebar on the recessed ply with a hairline right border. Streamlit's own
chrome — decoration bar, toolbar, status widget — is removed, and the header is made
transparent: none of it belongs to the document.

Vertical rhythm is set by rules rather than by whitespace. Clause headings take `2rem` of
lead, sub-clauses `1.75rem`, and each closes with its own 1px rule at `0.4rem`. Schedule rows
are `0.62rem 0 0.58rem` with a hairline bottom border, so the schedule reads as a continuous
ruled table rather than a stack of blocks. Bands (head, traceability) sit flush to the column
edge and are closed with a 3px ink rule.

The schedule row is a four-column grid —
`minmax(10rem,1.1fr) auto auto minmax(11rem,2fr)` with `0.3rem 1.1rem` gaps, baseline-aligned
— so label, figure, denominator and evidence line up down the page. State plates are a
wrapping flex row at `flex: 1 1 0` with a `7rem` minimum, which spreads them across the full
measure rather than huddling them at the left.

There is one breakpoint, at `820px`. The head stacks and resets its `22rem` flex basis (as a
column basis it would leave a band of empty blue), its field grid goes left-aligned and
full-width, the schedule row collapses to two columns with the label spanning and the
evidence dropping to its own line, and plates go one per line at `flex: 1 1 100%`.

### Named Rules

**The Keep-Every-Part Rule.** Narrow rules may stack or resize; nothing may be hidden. A
reviewer on a 1280 laptop and one on a 390 phone must be able to read the same schedule,
including every threshold printed on every plate. Three plates abreast at 390px sets the
EXPECTED rule as fourteen lines of two words — present, and unreadable — which is why plates
go full-measure rather than shrinking.

## Elevation & Depth

**There are no shadows anywhere in this system, and no elevation.** A certificate is ruled,
not stacked. Depth is conveyed by exactly two devices: the recessed ply
(`{colors.stock-sunk}`) for anything sunk beneath the stock — the sidebar, note banners, bar
tracks, table stripes, an inactive control — and a graded vocabulary of rules. There is no
shadow vocabulary to catalogue.

Rule weights, and what each one means:

- **1px hairline** (`{colors.hairline}`): an ordinary division — between schedule rows, around
  a bar track, around an expander, under a clause's tab list.
- **1px ink at 80%**: the rule that closes a clause or sub-clause heading.
- **1.5px ink**: the border of a stamped object — a state plate, a button, a page-link stamp.
- **2px ink**: the top of the schedule, and the top of a note banner.
- **3px ink**: the close of a full-width band — under the head, above the traceability band.

### Named Rules

**The Ruled-Not-Stacked Rule.** Depth is a rule weight or the recessed ply. If a surface
needs to feel separate, rule it or sink it; do not lift it. A `box-shadow` on this surface is
a defect, not a variant.

## Shapes

Right angles only. `baseRadius = "none"` and `buttonRadius = "none"` in
`.streamlit/config.toml`, with `border-radius: 0` restated on every Streamlit surface that
ships a radius of its own (uploader dropzone, dataframe, expander, JSON, notifications,
alerts, nav links, page links, buttons) and on the focus ring. The form language is the
rectangle: full-bleed bands, ruled rows, outlined plates, a 12px-tall bar track with a fill
that starts at a zero rule on the left.

The one non-rectilinear texture in the system is pattern, not shape: the ordered hatches
behind plate counts at `opacity: .16`, and the 6px guilloche strip
(`repeating-linear-gradient(90deg, security 0 2px, transparent 2px 4px)`) that closes the
head.

### Named Rules

**The Zero Radius Rule.** Nothing in this world is rounded — not a button, not a plate, not a
table, not the focus outline. A rounded corner reads as a card, and there are no cards here.

## Components

The signature components are pure HTML builders in `src/mdq/dashboard/certificate.py`; the
shared presentation language that assembles them per page is `src/mdq/dashboard/ui.py`. The
vocabulary replaces `st.metric` entirely.

### Certificate Head

The document's masthead, and the first thing on every page. A reference-blue field carrying
the product name in heavy Noto Sans caps on the left and, on the right, a two-column
definition grid of typed fields — report number, instruments, bars, sessions, page *n* of *m*
— in Courier Prime with uppercase tracked labels at 72% white and right-aligned values in
white. Closed by a 3px ink rule and then the 6px security guilloche. It renders on an empty
service with its fields dashed out; a cold start is a valid state and must never look broken.

### Schedule Row

The structure that replaces every KPI tile. Four ruled cells: the tracked uppercase label of
what was measured; the observed figure at plate scale in the typed face; the denominator it
was measured against; and an evidence cell holding the deviation bar, the ratio as a figure,
and a note. A failed row takes out-of-tolerance ink on both figure and bar. When both a ratio
figure and a note are present the note is ruled off above with a hairline, or the figure
reads as the paragraph's first line.

### Deviation Bar

A 12px track on the recessed ply, hairline-bordered on all four sides (never ink — the fill
has to be the only ink in the box, or a 3px sliver reads as a thickening of the zero rule),
with an ink fill growing from the left. A `min-width: 3px` floor keeps a genuinely tiny share
legible; the `is-zero` class removes it. A tolerance limit is a 1px reading-ink rule
overhanging the track by 3px top and bottom.

### Stamped State Plate

A severity stamped, not chipped: a 1.5px ink outline containing its ordered hatch at 16%
opacity, the count in the typed face at 1.9rem, the state's name in tracked uppercase, and —
ruled off below with a hairline — the threshold that decided the tier, in the typed face.
ERROR additionally takes an out-of-tolerance border. The count and name take the severity
ink; the plate never relies on that ink alone.

### Traceability Band

A reference-deep full-width band naming the standard this run was measured against: a tracked
uppercase label, a claim at 0.86rem capped at 70ch, and an optional figure in the typed face
at weight 700. Stacks to a column below 820px.

### Countersignature Block

The run's provenance: a hairline-topped wrapping row of typed `label value` pairs in reading
ink with the values in ink at weight 700 — who prepared it, the source, the thresholds in
force. It sits last on the page.

### Reading Note

The page's argument, set as a statement rather than a caption: 1rem in full graphite ink at
1.6 line-height on a 92ch measure, hairline-ruled above, with its claim in weight 700 and
the definitions following in reading weight. `certificate.reading_note(lead, body)`.

It exists because the alternative was tried and was wrong. The sentence carrying the whole
triage — *"44 of 15,018 findings need a human…"* — was originally an `st.caption`, which this
theme renders at 0.8rem in reading ink on a 74ch measure: about half the column, at 5.9:1.
Footnote treatment for the one claim the product is about. In ink at 1rem it measures 14:1
and reads as the first thing on the page after the figures. **One block per page at most.**
A second one competes with the first, and then neither is the argument.

### Buttons

- **Shape:** square (`{rounded.none}`).
- **Secondary (the only variant):** stock ground, 1.5px ink border, ink label in tracked
  uppercase at 0.72rem weight 700.
- **Hover:** inverts to ink ground with stock label.
- **Focus:** a 2px reference-blue outline at 2px offset, square, applied globally via
  `:focus-visible`.

### Page-Link Stamp

A page's one cross-reference action, treated as the stamped instruction it is rather than as
an inline link: square, 1.5px ink border, stock ground, tracked uppercase at 0.76rem weight
700, inline-flex at auto width. Hover fills with reference blue and reverses the label to
white (both the anchor's `p` and `span` have to be named, or Streamlit's own text colour
wins).

### Inputs / Fields

Streamlit's native widgets with `showWidgetBorder = true`, square, on the stock or the
recessed ply, bordered in hairline. The caret is reference blue; the focus ring is the global
square reference-blue outline. A radio's labels are set at 0.8rem. Error states are not a
field treatment here: a backend failure renders as a note banner in the place the answer
would have been.

**Pick the control from the shape of the choice, not from how it looks.** A short set of
named, discrete options is a `st.segmented_control` with `required=True`, never a
`st.select_slider`. A slider affords a continuous drag it cannot honour: with four stops the
handle travels through dead space and nothing changes until it crosses the next one, so the
control misrepresents what it does — the VWAP window was exactly this and was replaced. Its
selected option is left to the native theme, which squares it via `baseRadius` and marks the
selection in reference blue on a 10% tint of itself at 7.2:1. A continuous range still gets
a slider; a long list still gets a select.

### Note Banner

Alerts and notifications are ruled clauses, not coloured tabs: recessed ply, 1px hairline
border, 2px ink top rule, ink text, square. No coloured left edge.

### Navigation

The sidebar nav is the certificate's procedure index. Each item is prefixed by a CSS counter
in the typed face at 0.68rem — `01`, `02`, `03`, `04` — in reading ink, with a hairline left
border. Sidebar headings are 0.78rem tracked uppercase at weight 700 on the recessed ply. The
current page is *stamped*, not tabbed: its left border goes to ink, its label to weight 700,
its ground to stock, and its step number reverses out into a solid reference-blue field. The
nav itself is Streamlit's own; on narrow viewports it collapses into Streamlit's drawer.

### Icons

**Material Symbols Rounded**, Streamlit's bundled set, referenced as `:material/<name>:` —
one drawn library at one stroke weight, never a mixture. In use: `dashboard`, `show_chart`,
`rule` and `lightbulb` on the four procedure-index items, `verified` for the browser tab,
`hourglass_top` on the minute-pass notice, `warning` on an ingest reject, `info` on a
scoped note. Icons appear only in Streamlit's own chrome — the nav, a notice, the tab.
Nothing in the certificate body carries one: a schedule row, a plate, the traceability band
and the countersignature are typographic, and an icon inside them would be the one
ornament on the page doing no measuring.

No emoji, anywhere. An earlier build used 📈, ⏳ and ⚠️ in these positions; they are a
different set at a different weight that renders differently per platform, and they were
replaced deliberately. The `:material/` prefix is the house form.

### Charts

Charts are part of the document, not pictures pasted into it. Plotly template
`mdq_certificate` (registered in `theme.py`, idempotent) sets stock for both paper and plot,
Noto Sans at 12px in ink, hairline dotted grid lines, ink axis and
zero lines, outside ticks, tick labels in Courier Prime at 10.5px in reading ink, a
transparent borderless legend, and an ink hover label with typed stock-coloured text. The
colourway is reference, ink, warning, expected, out-of-tolerance. Severity fills always pair
`SEVERITY_COLOURS` with `SEVERITY_PATTERNS` at `fillmode: "overlay"` (pattern-only would
replace the fill and render as pale hatching on transparent). Candles are hollow for up and
filled for down, both outlined in 1px ink. An empty frame still returns a figure carrying a
centred sentence in reading ink.

**Both axes carry `automargin: True`, and no text is drawn inside the plot.** The figures set
a fixed 10px side margin, so a rotated axis title outside four-digit tick labels falls off
the edge unless Plotly is allowed to grow the margin for its own text — "Bars" once rendered
as "ars". For the same reason a drawn limit gets no `annotation_text`: Plotly places it
*inside* the plot at the line's own height, and on the coverage chart the bars run to the
limit, so the label sat over both the rule and the data with nowhere clear to move it. The
threshold is named in the page's caption and given a tick of its own on the axis — `[0, 690,
1380]` on the coverage chart — so the rule is readable as a number without ink over the bars.
A drawn limit nobody can put a number to is decoration.

**No chart carries its own title.** `_layout` takes no title parameter, so the rule cannot be
forgotten at one call site. The page supplies a ruled `h3` clause and a caption above the
plot; the chart supplies only the plot. Two reasons, and the first is a defect this replaced:
a Plotly title and a horizontal legend occupy the same band above the plot, so any title long
enough to reach the legend's first swatch collides with it. The second is that a heading is a
ruled certificate clause, and a Plotly title is the one piece of text on the page that would
not be set in the document's voice. The legend stays horizontal at `y: 1.02`, left-aligned,
above the plot, with `t: 34` of top margin to hold it.

### Named Rules

**The Measured-Against Rule.** A figure never appears without what it was measured against.
`ScheduleRow` raises at construction when given neither an `of_total` nor a `note` — the
reviewer meets every screen cold, and a bare number asks them to take it on trust.

**The Drawn-Limit Rule.** A tolerance limit is drawn only where a real threshold exists —
the 1,380-minute full CME session is the honest instance. No limit is drawn for "findings
requiring review" or for insight confidence; the ratio is printed as a figure beside the bar
instead. An invented limit is a fabricated measurement.

**The Measured-Zero Rule.** A measured zero draws nothing. The 3px minimum width rescues a
real sliver from invisibility; applied to zero it would invent one, which is the difference
between "no finding needs a human" and "one might".

**The Printed-Threshold Rule.** The threshold that decided a tier is printed on the plate, in
the typed face, read from the thresholds the report travelled with — never restated in code
and never hidden behind a JSON expander.

## Do's and Don'ts

### Do:

- **Do** spend colour on only two meanings: reference blue (`{colors.reference}`) for the
  standard measured against, out-of-tolerance vermilion (`{colors.out-of-tolerance}`) for a
  failed deviation. Everything else is ink on stock.
- **Do** give every severity mark its ordered hatch and its name in words as well as its ink:
  cross-hatch = ERROR, diagonal = WARNING, stipple = EXPECTED, in both the DOM and Plotly.
- **Do** measure a new ink's contrast against the stock and record the ratio beside the token,
  and keep it above 4.5:1 as text.
- **Do** print the denominator, threshold or note beside every figure; construct schedule rows
  through `ScheduleRow` so the rule is enforced rather than remembered.
- **Do** reserve Courier Prime for what is typed onto the form — report numbers, measurements,
  thresholds, ratios, timestamps, tick values.
- **Do** set heading sizes and weights in `.streamlit/config.toml`, and keep case, tracking
  and rules in `theme.py`'s stylesheet.
- **Do** hang every selector off a `data-testid` or a class this package writes itself, and
  name the element alongside the class when fighting
  `[data-testid="stMarkdownContainer"] p` — a bare class at (0,1,0) loses to it, which
  silently collapsed the head's title and subtitle to one size and inverted its hierarchy.
- **Do** wrap code with the supported `st.code(..., wrap_lines=True)`; CSS loses a specificity
  fight with the syntax highlighter's own `white-space: pre`.
- **Do** keep the page package named `views/`. A `pages/` directory beside the main script
  makes Streamlit's filesystem multipage discovery hijack cold deep links and render a blank
  page, which conflicts with `st.navigation`. Do not rename it back.
- **Do** vendor and inline any new face as a data URI, with its licence and provenance
  recorded.
- **Do** honour `prefers-reduced-motion: reduce` by rendering final states instantly.

### Don't:

- **Don't** introduce a third meaning for colour. A fifth hue in a chart, or a stock
  Streamlit semantic colour left unmapped, breaks the two that carry the argument.
- **Don't** use green-versus-red for price direction; direction is hollow-versus-filled ink.
- **Don't** add a `box-shadow`, an elevation layer, a card or a rounded corner. Rule it or
  sink it to the recessed ply instead.
- **Don't** draw a tolerance limit where no real threshold exists, and don't let a measured
  zero draw a sliver.
- **Don't** use `st.metric` or a KPI-tile row; the schedule row replaces them.
- **Don't** set prose in the typed face, and don't reach for monospace to signal "technical".
- **Don't** hide a part of the certificate at narrow widths. Stack or resize only.
- **Don't** put a page heading at or above the certificate title's scale; nothing outranks
  the head.
- **Don't** put a coloured tab down the side of an alert, or let severity ink carry a state
  on its own.
- **Don't** select styles off Streamlit's generated class names; they change between releases.
- **Don't** use an emoji where an icon belongs, and don't mix icon sets. `:material/<name>:`
  is the only form; the certificate body stays typographic and carries none at all.
- **Don't** put a load-bearing claim in a caption. The caption style is 0.8rem reading ink
  on a 74ch measure — correct for prose that qualifies a chart, and footnote treatment for
  an argument. If a sentence is the point of the page, it is a reading note.
- **Don't** use a slider for a handful of named options, and don't draw a label inside a
  plot. Both misrepresent something: the first affords a drag it cannot honour, the second
  puts ink over the data it is describing.
- **Don't** give a chart a Plotly title. The page's ruled heading names it; a title collides
  with the horizontal legend and speaks in the wrong voice.
- **Don't** name the page package `pages/`. Streamlit's filesystem multipage discovery
  claims that directory and hijacks cold deep links away from `st.navigation`, rendering a
  blank screen. It is `views/` for that reason.
