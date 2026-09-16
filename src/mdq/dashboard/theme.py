"""The calibration-certificate world: tokens, faces, stylesheet and chart template.

Every finding this platform reports is a deviation from a reference standard, and the
vendor's own published diagnostics are that standard. So the dashboard is dressed as the
document that says exactly that — a calibration certificate. It opens by declaring what was
measured and against what reference, then prints the deviations as a schedule. That is why
there are no cards, no elevation and no rounded panels anywhere in here: a certificate is
ruled, not stacked.

Three rules hold the system together, and the rest of the dashboard depends on them.

**Colour is spent on two things only.** `REFERENCE` marks anything to do with the standard
we measured against; `OUT_OF_TOLERANCE` marks a deviation that failed. Everything else is
ink on stock. A third meaning for colour would make the first two stop working.

**Severity never rides on hue alone.** Every severity carries an ordered hatch as well —
cross-hatch for an error, diagonal for a warning, stipple for an expected artefact. The
whole triage therefore survives being printed in greyscale or read by someone with a colour
vision deficiency, which is ordinarily represented among the financial-services
professionals this tool is for. `HATCHES` is the DOM half and `PATTERN_SHAPES` the Plotly
half of one system.

**The faces are self-hosted, not fetched.** Noto Sans is half of Macquarie's own published
type system and the part of it that is freely licensable; Courier Prime is the typed-onto-
the-form voice, and monospace here is earned because it sets measurements, serial numbers
and checksums rather than standing in for "technical". Both are inlined from
`static/fonts/` as data URIs so the dashboard looks right on a machine with no network —
which matters, because reviewers clone this and run it themselves.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path
from typing import Final

import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

__all__ = [
    "FONT_BODY",
    "FONT_TYPED",
    "HAIRLINE",
    "HATCHES",
    "INK",
    "INK_MUTED",
    "OUT_OF_TOLERANCE",
    "PATTERN_SHAPES",
    "REFERENCE",
    "SECURITY",
    "STOCK",
    "STOCK_SUNK",
    "TEMPLATE_NAME",
    "inject",
    "register_template",
    "stylesheet",
]

# --------------------------------------------------------------------------- #
# Palette
#
# Contrast against STOCK, measured rather than assumed, because PRODUCT.md makes
# WCAG 2.1 AA the floor: INK 14.0:1, INK_MUTED 5.9:1, OUT_OF_TOLERANCE 6.2:1,
# WARNING_INK 5.3:1, EXPECTED_INK 5.0:1. Every one of them clears 4.5:1 as text, so none
# of them leans on the 3:1 graphical-object allowance.
# White on REFERENCE is 9.5:1, so the solid reference fields carry body text safely.
# --------------------------------------------------------------------------- #

#: Certificate stock. Cool and pale — not cream; this is a measurement document.
STOCK: Final = "#F2F3EF"

#: The recessed ply: sunk panels, table stripes, the ground of an inactive control.
STOCK_SUNK: Final = "#E7E9E3"

#: Graphite ink. The dominant weight on the page.
INK: Final = "#1C1C1A"

#: Ink at reading weight for secondary prose. Tinted from the ground, never grey-by-default.
INK_MUTED: Final = "#5A5F58"

#: The reference standard. Owns whole regions as a solid field, not an accent.
REFERENCE: Final = "#23457E"

#: The reference field, one step down, for the band beneath the head.
REFERENCE_DEEP: Final = "#1A3462"

#: A deviation that failed. The only red on the surface, and never decorative.
OUT_OF_TOLERANCE: Final = "#A62A21"

#: Warning ink: inside tolerance, outside comfort.
WARNING_INK: Final = "#8C5A10"

#: An expected artefact — present, recessive, never hidden. Dark enough to clear 4.5:1 on
#: the stock (5.0:1 measured): it labels the EXPECTED plate and the expected-tier tick
#: labels, which are body-sized text, so the graphical-object floor of 3:1 does not apply.
EXPECTED_INK: Final = "#656960"

#: The security tint of a real certificate, used for guilloche hairline chrome.
SECURITY: Final = "#C8D6C9"

#: Rules and borders.
HAIRLINE: Final = "#9AA09B"

#: Severity ink, which the charts and the state plates share so one word means one colour.
SEVERITY_INK: Final[dict[str, str]] = {
    "ERROR": OUT_OF_TOLERANCE,
    "WARNING": WARNING_INK,
    "INFO": EXPECTED_INK,
}

#: The non-colour channel. Plotly's pattern shapes, so a chart read in greyscale still
#: separates the three tiers.
PATTERN_SHAPES: Final[dict[str, str]] = {"ERROR": "x", "WARNING": "/", "INFO": "."}

#: The same three tiers as CSS backgrounds, for the stamped state plates in the DOM.
HATCHES: Final[dict[str, str]] = {
    "ERROR": (
        "repeating-linear-gradient(45deg,{ink} 0 1px,transparent 1px 5px),"
        "repeating-linear-gradient(-45deg,{ink} 0 1px,transparent 1px 5px)"
    ),
    "WARNING": "repeating-linear-gradient(45deg,{ink} 0 1px,transparent 1px 6px)",
    "INFO": "radial-gradient({ink} 0.5px,transparent 0.6px)",
}

# --------------------------------------------------------------------------- #
# Faces
# --------------------------------------------------------------------------- #

#: UI, prose and data. Macquarie's own secondary face, and the licensable half of it.
FONT_BODY: Final = '"Noto Sans", ui-sans-serif, system-ui, sans-serif'

#: Typed onto the form: report numbers, checksums, timestamps, thresholds, measurements.
FONT_TYPED: Final = '"Courier Prime", ui-monospace, "SFMono-Regular", monospace'

_FONT_DIR: Final = Path(__file__).parent / "static" / "fonts"

#: family, file, weight range, style. Noto Sans ships as one variable file.
_FACES: Final[tuple[tuple[str, str, str], ...]] = (
    ("Noto Sans", "notosans-400-800-latin.woff2", "400 800"),
    ("Courier Prime", "courierprime-400-latin.woff2", "400"),
    ("Courier Prime", "courierprime-700-latin.woff2", "700"),
)


@lru_cache(maxsize=1)
def _font_faces() -> str:
    """`@font-face` rules with the woff2 payloads inlined.

    Inlined rather than served so that a reviewer running this on a locked-down network,
    or with no network at all, sees the typography the design was made in. A missing face
    would silently fall back to the platform sans and quietly undo the whole world.

    A face that is missing from the tree is skipped rather than raised on: the dashboard
    degrading to a system stack is far better than a page that will not render.
    """
    rules: list[str] = []
    for family, filename, weight in _FACES:
        path = _FONT_DIR / filename
        if not path.is_file():  # pragma: no cover - the tree ships all three.
            continue
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        rules.append(
            f"@font-face{{font-family:'{family}';font-style:normal;"
            f"font-weight:{weight};font-display:swap;"
            f"src:url(data:font/woff2;base64,{payload}) format('woff2');}}"
        )
    return "".join(rules)


# --------------------------------------------------------------------------- #
# Stylesheet
# --------------------------------------------------------------------------- #

#: Streamlit's own class names change between releases, so every selector below hangs off a
#: `data-testid`, which Streamlit maintains as its test contract, or off a class this
#: package writes itself. That is the difference between a theme that survives an upgrade
#: and one that silently falls off.
_CSS: Final = """
/* ---- ground ------------------------------------------------------------- */
html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"]{
  background:var(--stock); color:var(--ink); font-family:var(--body);
}
[data-testid="stHeader"]{ background:transparent; }
[data-testid="stMainBlockContainer"]{ padding-top:1.5rem; max-width:1500px; }
[data-testid="stDecoration"]{ display:none; }
/* Streamlit's own Deploy button and hamburger menu advertise deploying this app. They are
   not part of the document, and on a certificate they read as someone else's letterhead.
   Hide *those*, not `stToolbar`: the sidebar's expand button is a child of the toolbar, so
   hiding the whole thing left a collapsed sidebar with no way back — the nav and the
   uploader simply gone until a reload. `stToolbarActions` is the narrower target that
   holds only what we actually meant to remove. */
[data-testid="stAppDeployButton"], [data-testid="stMainMenu"],
[data-testid="stStatusWidget"]{ display:none; }

/* Tabular figures everywhere a number can line up under another number. */
body, [data-testid="stMetricValue"], [data-testid="stDataFrame"], .mdq-typed{
  font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1;
}

/* ---- the parts the browser draws, which still belong to the design ------ */
::selection{ background:var(--reference); color:#fff; }
input, textarea{ caret-color:var(--reference); }
* { scrollbar-color:var(--hairline) var(--sunk); scrollbar-width:thin; }
::-webkit-scrollbar{ width:11px; height:11px; }
::-webkit-scrollbar-track{ background:var(--sunk); }
::-webkit-scrollbar-thumb{ background:var(--hairline); border:3px solid var(--sunk); }
::-webkit-scrollbar-thumb:hover{ background:var(--ink-muted); }
a{ color:var(--reference); text-decoration-thickness:1px; text-underline-offset:3px; }
a:hover{ color:var(--reference-deep); }
:focus-visible{ outline:2px solid var(--reference); outline-offset:2px; border-radius:0; }

/* ---- type ---------------------------------------------------------------- */
/* Sizes and weights come from `.streamlit/config.toml` (headingFontSizes), whose rules are
   more specific than an injected stylesheet's. What is left here is character. */
h1, h2, h3, h4{
  font-family:var(--body); color:var(--ink);
  text-wrap:balance; margin-bottom:.35rem;
}
h1{ text-transform:uppercase; letter-spacing:.055em; margin:1.15rem 0 .2rem; }
h2{ margin-top:2rem; }
h3{ margin-top:1.75rem; letter-spacing:.02em; }
/* A caption's measure is wide enough to sit under a full-width ruled heading without
   reading as an indent. At 74ch and 0.8rem these wrapped at roughly half the column, which
   against a heading and a chart that both span it looked like the text had been tabbed
   back. Still a measure, and still narrower and smaller than the reading note, so the
   hierarchy between "the argument" and "prose that qualifies a chart" survives. */
[data-testid="stCaptionContainer"], .mdq-note{
  color:var(--ink-muted); font-size:.85rem; line-height:1.55; max-width:104ch;
}

/* The note on how to read the results. Deliberately NOT the caption style above: a caption
   qualifies something else, and this block is the argument the whole product exists to
   make. Full ink, reading size, claim in bold, and a measure wide enough not to look like
   a stranded footnote — but still a measure, because a 130-character line on a 1,500px
   column is a line a reader loses their place in. */
.mdq-reading{
  color:var(--ink); font-size:1rem; line-height:1.6; max-width:92ch;
  margin:.95rem 0 1.35rem; border-top:1px solid var(--hairline); padding-top:.8rem;
}
.mdq-reading b{ font-weight:700; }
code, [data-testid="stJson"], [data-testid="stCode"]{ font-family:var(--typed); }
.mdq-typed{ font-family:var(--typed); }

/* Code blocks wrap via `st.code(..., wrap_lines=True)`, not from here. Two attempts to do
   it in CSS lost a specificity fight with the highlighter's own `white-space:pre` on the
   code element, and the supported parameter is both shorter and upgrade-proof. */

/* A section heading is a ruled certificate clause, not a bare line of text. `st.subheader`
   renders an h3 and `st.header` an h2, so both get the rule or half the pages would
   silently lose it. */
h2::after, h3::after{
  content:""; display:block; height:1px; background:var(--ink);
  margin-top:.4rem; opacity:.8;
}
/* …but not for a heading inside something already ruled, or the box gets a double line. */
[data-testid="stExpander"] h2::after, [data-testid="stExpander"] h3::after,
[data-testid="stSidebar"] h2::after, [data-testid="stSidebar"] h3::after{ display:none; }

/* ---- certificate head ---------------------------------------------------- */
.mdq-head{
  background:var(--reference); color:#fff; padding:1.05rem 1.3rem .95rem;
  display:flex; flex-wrap:wrap; gap:1.5rem; align-items:flex-end;
  justify-content:space-between; border-bottom:3px solid var(--ink);
}
.mdq-head-block{ flex:1 1 22rem; }
/* Both selectors name the element as well as the class. These are paragraphs inside a
   Streamlit markdown container, whose own `[data-testid="stMarkdownContainer"] p` rule
   (0,1,1) outranks a bare class (0,1,0) and silently collapsed both to ~1rem — which
   inverted the head's own hierarchy, since the tracked subtitle then read larger than the
   title above it. This is the FORM block's stated risk about unstable Streamlit selectors
   arriving in practice; the answer is specificity, not a heavier font size. */
.mdq-head p.mdq-head-title{
  font-weight:800; font-size:2.1rem; line-height:.98; letter-spacing:-0.012em;
  text-transform:uppercase; margin:0; color:#fff; max-width:20ch;
}
.mdq-head p.mdq-head-sub{
  font-size:.72rem; letter-spacing:.14em; text-transform:uppercase; font-weight:400;
  margin:.5rem 0 0; color:rgba(255,255,255,.82); line-height:1.3;
}
.mdq-head-fields{
  display:grid; grid-template-columns:auto auto; gap:.15rem 1.1rem;
  font-family:var(--typed); font-size:.73rem; margin:0 0 0 auto;
}
.mdq-head-fields dt{
  color:rgba(255,255,255,.72); letter-spacing:.1em; text-transform:uppercase;
}
.mdq-head-fields dd{ margin:0; color:#fff; text-align:right; }

/* ---- schedule of results ------------------------------------------------- */
.mdq-schedule{ border-top:2px solid var(--ink); margin:0 0 .35rem; }
.mdq-row{
  display:grid; grid-template-columns:minmax(10rem,1.1fr) auto auto minmax(11rem,2fr);
  gap:.3rem 1.1rem; align-items:baseline;
  padding:.62rem 0 .58rem; border-bottom:1px solid var(--hairline);
}
.mdq-row-label{
  font-size:.7rem; letter-spacing:.13em; text-transform:uppercase;
  color:var(--ink-muted); font-weight:600;
}
.mdq-row-observed{
  font-family:var(--typed); font-weight:700; font-size:2.9rem; line-height:.92;
  letter-spacing:-0.03em; color:var(--ink); white-space:nowrap;
}
.mdq-row-observed.is-fail{ color:var(--fail); }
.mdq-row-of{
  font-family:var(--typed); font-size:.76rem; color:var(--ink-muted); white-space:nowrap;
}
.mdq-row-note{ font-size:.76rem; color:var(--ink-muted); line-height:1.5; }

/* The ratio the bar draws, printed as a figure so the sliver has a number to be. */
.mdq-row-pct{
  font-family:var(--typed); font-size:.72rem; font-weight:700; color:var(--ink);
  display:block; margin-top:.28rem; letter-spacing:.02em;
}
/* Ruled off the prose beneath it, or the figure reads as that paragraph's first line. */
.mdq-row-pct + .mdq-row-note{
  border-top:1px solid var(--hairline); padding-top:.45rem; margin-top:.45rem;
}

/* The deviation bar: grows from a zero rule, between drawn tolerance limits. The track is
   outlined so "the whole" is legible, and the fill keeps a minimum width so a genuinely
   tiny share — 6 findings in 2,102 — reads as a deliberate sliver rather than as a bar
   that failed to draw. That distinction is the entire argument of the Overview page. */
/* The track carries a hairline on every side, never ink: the ink fill has to be the only
   ink in the box or a 3px sliver is read as a thickening of the zero rule. */
.mdq-bar{
  position:relative; height:12px; background:var(--sunk);
  border:1px solid var(--hairline); margin-top:.3rem;
}
/* The motion grammar: rows resolve in sequence from the zero rule, 70ms apart, so a
   schedule of several rows measures in rather than snapping. `--i` is the row's index,
   set inline by the builder. */
.mdq-bar-fill{
  position:absolute; inset:0 auto 0 0; background:var(--ink); min-width:3px;
  animation:mdq-measure .62s cubic-bezier(.16,1,.3,1) both;
  animation-delay:calc(var(--i, 0) * 70ms);
}
/* A measured zero draws nothing. The min-width above rescues a real sliver from being
   invisible; applied to zero it would invent one, which on this page is the difference
   between "no finding needs a human" and "one might". */
.mdq-bar-fill.is-zero{ min-width:0; }
.mdq-bar-fill.is-fail{ background:var(--fail); }
.mdq-bar-limit{
  position:absolute; top:-3px; bottom:-3px; width:1px; background:var(--ink-muted);
}
@keyframes mdq-measure{ from{ transform:scaleX(0); transform-origin:left; } }
@media (prefers-reduced-motion:reduce){
  .mdq-bar-fill{ animation:none; }
}

/* ---- stamped state plates ------------------------------------------------ */
/* Plates share the full width rather than huddling at the left: the scene is a reviewer
   reading a dense sheet, and half an empty band is what made this read as stock-with-a-
   theme. `flex:1 1 0` spreads them; the min-width only holds the line on a phone. */
.mdq-plates{ display:flex; flex-wrap:wrap; gap:.6rem; margin:.5rem 0 0; }
.mdq-plate{
  border:1.5px solid var(--ink); padding:.6rem .8rem .55rem; flex:1 1 0; min-width:7rem;
  position:relative; overflow:hidden;
}
.mdq-plate-hatch{ position:absolute; inset:0; opacity:.16; pointer-events:none; }
.mdq-plate-count{
  font-family:var(--typed); font-weight:700; font-size:1.9rem; line-height:1;
  letter-spacing:-0.02em; display:block; position:relative;
}
.mdq-plate-name{
  font-size:.66rem; letter-spacing:.17em; text-transform:uppercase; font-weight:700;
  display:block; position:relative; margin-top:.3rem;
}
/* The threshold that decided this tier, printed on the plate rather than in a JSON dump. */
.mdq-plate-rule{
  font-family:var(--typed); font-size:.66rem; line-height:1.45; color:var(--ink-muted);
  display:block; position:relative; margin-top:.4rem;
  border-top:1px solid var(--hairline); padding-top:.32rem;
}
.mdq-plate.is-error{ border-color:var(--fail); }
.mdq-plate.is-error .mdq-plate-count, .mdq-plate.is-error .mdq-plate-name{ color:var(--fail); }
.mdq-plate.is-warning .mdq-plate-count, .mdq-plate.is-warning .mdq-plate-name{
  color:var(--warn);
}
.mdq-plate.is-info .mdq-plate-count, .mdq-plate.is-info .mdq-plate-name{ color:var(--expected); }

/* ---- traceability band and countersignature ------------------------------ */
.mdq-trace{
  background:var(--reference-deep); color:#fff; padding:.85rem 1.1rem;
  display:flex; flex-wrap:wrap; gap:.4rem 1.6rem; align-items:baseline;
  border-top:3px solid var(--ink); margin:.4rem 0 1.4rem;
}
.mdq-trace-label{
  font-size:.64rem; letter-spacing:.17em; text-transform:uppercase;
  color:rgba(255,255,255,.72); font-weight:700;
}
.mdq-trace-claim{ font-size:.86rem; line-height:1.5; max-width:70ch; }
.mdq-trace-figure{ font-family:var(--typed); font-weight:700; }

.mdq-sign{
  border-top:1px solid var(--hairline); margin-top:1.6rem; padding-top:.7rem;
  display:flex; flex-wrap:wrap; gap:.3rem 2.2rem;
  font-family:var(--typed); font-size:.7rem; color:var(--ink-muted);
}
.mdq-sign b{ color:var(--ink); font-weight:700; }

/* Guilloche: the fine security line-work a certificate carries at its edges. */
.mdq-guilloche{
  height:6px;
  background:repeating-linear-gradient(90deg,
    var(--security) 0 2px, transparent 2px 4px);
  border-bottom:1px solid var(--hairline);
}

/* ---- Streamlit widgets, brought into the world --------------------------- */
[data-testid="stSidebar"]{
  background:var(--sunk); border-right:1px solid var(--hairline);
}
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3{ font-size:.78rem; letter-spacing:.15em;
  text-transform:uppercase; font-weight:700; }

/* The nav is the certificate's procedure index: numbered, because the page order is
   itself an argument this project makes. */
[data-testid="stSidebarNav"] ul{ counter-reset:mdq-step; }
[data-testid="stSidebarNav"] li a{
  border-left:1px solid var(--hairline); border-radius:0;
  padding-left:.65rem; margin-left:.2rem;
}
[data-testid="stSidebarNav"] li a::before{
  counter-increment:mdq-step; content:counter(mdq-step,decimal-leading-zero);
  font-family:var(--typed); font-size:.68rem; color:var(--ink-muted);
  margin-right:.6rem;
}
/* The page you are on is stamped, not tabbed: its step number reverses out into the
   reference field, which is the document's own way of marking a thing. */
[data-testid="stSidebarNav"] li a[aria-current="page"]{
  border-left:1px solid var(--ink); background:var(--stock); font-weight:700;
}
[data-testid="stSidebarNav"] li a[aria-current="page"]::before{
  background:var(--reference); color:#fff; padding:.06rem .26rem; margin-right:.42rem;
}

/* The page's one action. In a world of stamps and typed fields an untreated inline link
   was the only element wearing nothing: it now reads as the stamped instruction it is. */
[data-testid="stPageLink"] a{
  border:1.5px solid var(--ink); border-radius:0; background:var(--stock);
  padding:.5rem .8rem; margin:.2rem 0 0; display:inline-flex; width:auto;
  font-weight:700; font-size:.76rem; letter-spacing:.09em; text-transform:uppercase;
  color:var(--ink); text-decoration:none;
}
[data-testid="stPageLink"] a:hover{
  background:var(--reference); border-color:var(--reference); color:#fff;
}
[data-testid="stPageLink"] a:hover p, [data-testid="stPageLink"] a:hover span{ color:#fff; }
[data-testid="stPageLink"] a p{ font-weight:700; letter-spacing:.09em; }

.stButton button, [data-testid="stBaseButton-secondary"]{
  border-radius:0; border:1.5px solid var(--ink); background:var(--stock);
  color:var(--ink); font-weight:700; letter-spacing:.06em; text-transform:uppercase;
  font-size:.72rem;
}
.stButton button:hover{ background:var(--ink); color:var(--stock); border-color:var(--ink); }

[data-testid="stFileUploaderDropzone"], [data-testid="stDataFrame"],
[data-testid="stExpander"], [data-testid="stJson"], [data-testid="stNotification"],
[data-testid="stAlert"]{ border-radius:0; }
[data-testid="stExpander"] details{
  border:1px solid var(--hairline); border-radius:0; background:var(--stock);
}
[data-testid="stExpander"] summary{
  font-size:.76rem; font-weight:700; letter-spacing:.06em; text-transform:uppercase;
}
/* A note is ruled off at the top like every other clause on the page, rather than wearing
   a coloured tab down one side. */
[data-testid="stAlert"], [data-testid="stNotification"]{
  border:1px solid var(--hairline); border-top:2px solid var(--ink);
  background:var(--sunk); color:var(--ink);
}
[data-testid="stTooltipHoverTarget"] svg{ color:var(--ink-muted); }
[data-baseweb="tab-list"]{ background:transparent; border-bottom:1px solid var(--hairline); }

/* The segmented control is left to the native theme deliberately. `baseRadius: none` and
   `primaryColor` already square it and give it the reference hue, and its selected option
   computes to reference blue on a 10% tint of itself — 7.2:1 on the stock, well past the
   AA floor. Restyling it means overriding Streamlit's own `aria-checked` rule, which sits
   behind an internal emotion class; two attempts lost that specificity fight silently,
   which is the failure mode this file warns about twice. The supported theme surface wins
   over a selector that breaks on upgrade. */
[data-testid="stRadio"] label{ font-size:.8rem; }
hr, [data-testid="stDivider"] hr{ border-color:var(--hairline); }

/* ---- narrow --------------------------------------------------------------- */
/* The certificate keeps its parts; it stops pretending to be four columns wide.
   Every rule here only stacks or resizes — nothing is hidden, because a reviewer on a
   laptop at 1280 and one on a phone must be able to read the same schedule. */
@media (max-width:820px){
  [data-testid="stMainBlockContainer"]{ padding-top:1.25rem; }
  /* The head stacks here, so `flex:1 1 22rem` would be a 22rem *height* basis and leave
     a band of empty blue under the subtitle. Reset it rather than restating a basis. */
  .mdq-head{ flex-direction:column; align-items:flex-start; gap:.85rem; }
  .mdq-head-block{ flex:none; width:100%; }
  .mdq-head-title{ font-size:1.5rem; max-width:none; }
  .mdq-head-fields{ margin:0; grid-template-columns:auto 1fr; width:100%; }
  .mdq-head-fields dd{ text-align:left; }
  .mdq-head-sub{ letter-spacing:.08em; }
  .mdq-row{
    grid-template-columns:1fr auto; gap:.15rem .8rem;
    padding:.75rem 0 .7rem;
  }
  .mdq-row-label{ grid-column:1 / -1; }
  .mdq-row-observed{ font-size:2.1rem; }
  .mdq-row-of{ text-align:right; }
  .mdq-row > div:last-child{ grid-column:1 / -1; }
  /* One plate per line. Three abreast at 390px leaves each rule about twelve characters
     wide, which sets the EXPECTED rule as fourteen lines of two words — present, and
     unreadable. A plate carrying a sentence needs the full measure. */
  .mdq-plate{ flex:1 1 100%; min-width:0; padding:.55rem .7rem; }
  .mdq-plate-count{ font-size:1.5rem; }
  .mdq-plate-rule{ font-size:.64rem; }
  .mdq-trace{ flex-direction:column; gap:.35rem; }
  h1{ font-size:1.22rem; }
}
"""


def _custom_properties() -> str:
    """The palette as CSS custom properties, so the rules below name tokens not hexes.

    Concatenated rather than interpolated into `_CSS`. The stylesheet is full of literal
    percent signs and braces, which is exactly what every string-templating mechanism in
    Python wants to consume, so the only substitution happens here — where there is no CSS
    to get confused by.
    """
    pairs = {
        "stock": STOCK,
        "sunk": STOCK_SUNK,
        "ink": INK,
        "ink-muted": INK_MUTED,
        "reference": REFERENCE,
        "reference-deep": REFERENCE_DEEP,
        "fail": OUT_OF_TOLERANCE,
        "warn": WARNING_INK,
        "expected": EXPECTED_INK,
        "security": SECURITY,
        "hairline": HAIRLINE,
        "body": FONT_BODY,
        "typed": FONT_TYPED,
    }
    declarations = "".join(f"--{name}:{value};" for name, value in pairs.items())
    return f":root{{{declarations}}}"


@lru_cache(maxsize=1)
def stylesheet() -> str:
    """The whole world as one `<style>` payload, built once per process."""
    return f"<style>{_font_faces()}{_custom_properties()}{_CSS}</style>"


def inject() -> None:
    """Put the stylesheet on the page. Called once, from the shell."""
    st.markdown(stylesheet(), unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Chart template
# --------------------------------------------------------------------------- #

#: Registered with Plotly so every figure inherits the world instead of restating it.
TEMPLATE_NAME: Final = "mdq_certificate"


def register_template() -> str:
    """Register the certificate chart template and return its name.

    Charts are part of the document, not pictures pasted into it: the same stock, the same
    ink, the same hairlines and the same typed face for the numbers. Idempotent, so the
    dashboard can call it from wherever a figure is first needed.
    """
    template = go.layout.Template(
        layout={
            "paper_bgcolor": STOCK,
            "plot_bgcolor": STOCK,
            "font": {"family": FONT_BODY, "size": 12, "color": INK},
            "title": {
                "font": {"family": FONT_BODY, "size": 13, "color": INK},
                "x": 0,
                "xanchor": "left",
            },
            "colorway": [REFERENCE, INK, WARNING_INK, EXPECTED_INK, OUT_OF_TOLERANCE],
            # `automargin` is load-bearing, not a nicety. The figures carry a fixed 10px
            # side margin, and a rotated axis title sitting outside four-digit tick labels
            # falls off the edge — "Bars" rendered as "ars". Letting Plotly grow the margin
            # to fit its own text is the only version of this that survives a dataset whose
            # numbers are wider than the last one's.
            "xaxis": {
                "gridcolor": HAIRLINE,
                "griddash": "dot",
                "linecolor": INK,
                "zerolinecolor": INK,
                "ticks": "outside",
                "tickcolor": HAIRLINE,
                "automargin": True,
                "tickfont": {"family": FONT_TYPED, "size": 10.5, "color": INK_MUTED},
                "title": {"font": {"size": 10.5, "color": INK_MUTED}},
            },
            "yaxis": {
                "gridcolor": HAIRLINE,
                "griddash": "dot",
                "linecolor": INK,
                "zerolinecolor": INK,
                "ticks": "outside",
                "tickcolor": HAIRLINE,
                "automargin": True,
                "tickfont": {"family": FONT_TYPED, "size": 10.5, "color": INK_MUTED},
                "title": {"font": {"size": 10.5, "color": INK_MUTED}},
            },
            "legend": {
                "font": {"size": 10.5, "color": INK},
                "bgcolor": "rgba(0,0,0,0)",
                "borderwidth": 0,
            },
            "hoverlabel": {
                "bgcolor": INK,
                "bordercolor": INK,
                "font": {"family": FONT_TYPED, "size": 11, "color": STOCK},
            },
        }
    )
    pio.templates[TEMPLATE_NAME] = template
    return TEMPLATE_NAME
