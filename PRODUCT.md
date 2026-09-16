# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Primary: the Macquarie review panel.** Engineers and hiring reviewers who will clone the
repository and run it themselves (`make dev`), assessing craft, architecture and judgement.
They decide whether this work succeeded. Two consequences follow, and both are load-bearing:

- **The cold-start state is a first impression.** It is uncertain whether a given reviewer
  runs `make fetch` before launching. The dashboard must therefore come up empty without
  looking broken or unfinished, and must make the path to loaded data obvious.
- **They are evaluating in a short sitting, without context.** Nobody will explain a screen
  to them. Every number on a page has to carry its own justification.

**Modelled user, per the exercise brief: business users on a futures desk** — traders, risk
managers and analysts who consume vendor futures data and need to understand its quality
before trusting analytics derived from it. The application is designed as though for them;
it is judged by the panel. Work that would please the panel while making the tool
implausible for a desk user fails both.

## Product Purpose

Ingest historical futures bar data (CSV or parquet), map it onto one canonical schema with a
real UTC instant, generate the analytics the desk asks for, find what is wrong with the
data, and explain what the defects mean with a concrete rule to adopt for each.

Success is a reviewer concluding that the author understood the *domain problem*, not just
the requirements list. The central finding the product exists to demonstrate: on real
futures data, unconditional flagging is useless. The 116 MB evaluation sample produces
15,018 daily findings, of which **44 need a human** and 14,974 are correct settlement prints
and exchange holidays that the activity model explains. Separating those two groups is the
product.

## Positioning

The mechanism a neighbouring implementation could not truthfully copy is **severity
conditioned on an observed per-contract activity regime**. Every comparable exercise
solution detects the same defects; almost none can tell a genuinely broken bar from an
expected artefact, so their output is thousands of undifferentiated warnings.

Three supporting claims, each measured rather than asserted:

- **The vendor is used as an external oracle.** `files.parquet` publishes four per-file
  diagnostics computed before this project existed; the quality engine reproduces all four
  exactly across 40/40 daily files, 0 mismatches.
- **Time is derived, not assumed.** The canonical instant comes from localising a declared
  wall clock. Session aggregation reproduces the vendor's daily open on 193/197 days,
  against 18% for naive calendar-date aggregation.
- **Insights are rule-based by choice, not by limitation.** Deterministic, testable and
  explainable to a model-risk reviewer, with the `InsightEngine` protocol left as the
  documented seam where an LLM implementation would slot in.

## Operating Context

- **Reviewer path:** `uv sync` → optionally `make fetch` (116 MB, pinned + checksummed) →
  `make test` → `make dev`. API on `:8000` with OpenAPI docs at `/docs`, dashboard on
  `:8501`. `make ui` runs the dashboard with the service in-process, so there is no port to
  coordinate.
- **Two front ends over one facade.** `MarketDataService` is the single facade; the
  dashboard talks to it either embedded or as an ordinary HTTP client of the documented API
  (`Backend` protocol). No page knows which it got.
- **Data arrives three ways:** the pinned vendor sample, a directory pointed at via
  `MDQ_DATA_DIR`, or a single file uploaded through the sidebar / `POST /ingest`. Upload is
  available from anywhere in the app and recomputes every page with the file included.
- **Real corpus scale:** 40 contracts, 8 roots (CL, ES, GC, SB, SR3, VX, ZC, ZN),
  6 exchanges, 5,325,341 bars. A full minute-frequency quality pass takes ~5.5 s and ~1.3 GB
  of memory, so it runs only when asked and always announces itself first.

## Capabilities and Constraints

**Shipped capabilities.** Fourteen quality checks; regime-conditioned severity; daily OHLCV
bar generation; gap-aware rolling 15-minute VWAP; filtering by contract and date range; ten
insight rules over a rule-based engine; FastAPI service with analytic routes generated from
the registry; four-page Streamlit dashboard (Overview, Price analytics, Data quality,
Insights); ingest reporting that distinguishes rows rejected because they cannot be *located*
from rows kept and flagged because they are merely *wrong*.

**Six extension seams**, each a register-and-go addition requiring no change to existing
code: file readers, source column profiles, quality checks, analytics, insight rules, and
the insight engine itself.

**Front-end latitude.** Streamlit remains the shell and the four-page structure stands.
Bespoke HTML/CSS/JS components are explicitly in scope wherever a native widget caps the
quality. A replacement front end on the API is *not* currently in scope.

**Current technical state.** The dashboard carries a full design system — see
[DESIGN.md](DESIGN.md) — with `.streamlit/config.toml` for the native theme, self-hosted
typography, and a registered Plotly template so charts inherit it. Charts are Plotly.
`mypy --strict` excludes `src/mdq/dashboard/`; the rest of `src/mdq` is strict-clean.

**Quality gates that must stay green through any change.** 993 tests,
`--cov-fail-under=98` (currently 98.95%; every non-dashboard module at 100%), `ruff check`,
`ruff format --check`, `mypy --strict`. The suite never touches the network and never reads
`data/`. Two evaluation harnesses live in `scripts/` and are exercised by tests without
being coverage-measured — see [docs/EVALUATION.md](docs/EVALUATION.md).

**Status: in flight.** Not yet submitted. Substantive change is permitted, including
structural change, provided the gates above stay green.

**Terminology, used consistently in the UI** — this vocabulary is deliberate and must be
preserved: *instrument* (not "contract code"), *trading session* (not "date"), *finding*
(not "error"), *regime* (ACTIVE / THIN / DORMANT), severity tiers ERROR / WARNING / INFO.

**Explicit non-goals.** A shipped holiday calendar (a maintenance liability and a per-venue
licensing question — cross-contract corroboration made one unnecessary); a
`cross_frequency_mismatch` check; LLM-backed insights in this iteration.

## Brand Commitments

- **Name:** "Market Data Quality & Analytics". Package and CLI identity is `mdq`.
- **Identity direction: Macquarie-flavoured.** The interface should echo the visual language
  of an institutional financial house to demonstrate fit with the environment it is being
  reviewed in. **Constraint:** generic institutional cues only — no Macquarie logo, wordmark,
  trademarked assets or any styling that would imply the firm produced or endorsed this
  work. It is a candidate's exercise and must remain legible as one.

### Reference: Macquarie Group's published brand fundamentals

From the public [Brand Experience Portal](https://brand.macquarie.com/en/fundamentals.html),
researched 2026-09-16. These are the cues future design work may draw on:

- **Typography.** The house typeface is *MCQ Global*, described as "a structured sans serif
  font, balanced with distinct softer curves to hit the mark between personality and
  dependability," paired with **Noto Sans** as the global complement, chosen so the system
  works across alphabets. MCQ Global is proprietary and **not licensable for this project**.
  Noto Sans is freely available and is genuinely half of Macquarie's own type system, so it
  is the sanctioned way to sound right without touching a trademarked asset.
- **Colour.** Black and white at the core, supported by warm, brighter secondaries that gain
  contrast when used against black. No hex values are published; a palette in that spirit
  must be derived rather than copied — which also keeps it clear of trademarked assets.
- **Illustration and pattern.** Black-and-white lines, positive/negative shapes, repetitive
  action. Patterns serve as an alternative to imagery.
- **Layout.** An explicit grid system is named as the structural foundation across formats.
- **Tone.** Professional yet approachable; dependability balanced with personality.

**Two near-misses to avoid.** *GEL* (Global Experience Language) is **Westpac's** design
system, also used by Bankwest and GBST — it is not Macquarie's, and the name is a
semi-generic term in Australian financial services. *GEM* (Global Experience Macquarie) is
**Macquarie University's** design system, a different organisation from Macquarie Group.
Neither is a valid reference for this work. Macquarie Group publishes no design system or
component library, and none exists on GitHub.
- **Voice, established and consistent across README, architecture doc and UI copy:**
  measured, specific and quantified. Claims arrive with the measurement that supports them.
  Limitations are stated up front rather than buried — the README has a section on what the
  sample does *not* exhibit. No marketing register, no superlatives.

## Evidence on Hand

Real, in-repository, and usable without fabrication:

- **Vendor oracle agreement:** 4 published diagnostics reproduced exactly on 40/40 daily
  files (`tests/golden/test_quality_golden.py`).
- **Measured findings on the real corpus:** `stale_bar` 14,152 · `zero_volume_with_range`
  400 · `missing_session` 389 · `invalid_ohlc` 43 · `outlier_return` 34. Triage:
  5 ERROR / 39 WARNING / 14,974 INFO.
- **Four firing insights with real confidences:** `dormancy_explains_staleness` 0.998,
  `missing_sessions_are_holidays` 0.997, `carried_forward_settlement` 0.907,
  `feed_incident_date_cluster` 0.651. The spread is itself evidence the confidence metric
  discriminates.
- **A derived US market holiday calendar** (41 dates, 2022–2026) reconstructed purely from
  cross-contract corroboration with no calendar shipped.
- **Fault-injection harness** (`tests/support/faults.py`): 19 fault types, each asserted for
  both recall and precision.
- **Session reconstruction:** reproduces the vendor's daily open on 193/197 days vs 18% for
  calendar-date aggregation.
- **Decision records:** nine ADRs in `docs/ARCHITECTURE.md`, each with the measurement that
  drove it. `PLAN.md` holds the fuller design record.

**Absences future work must not paper over.** There are no users, no customers, no
testimonials, no benchmarks against competing products, no deployment and no production
history. The evaluation sample is pre-normalised: zero duplicate timestamps, zero null
prices, zero non-positive prices and zero negative volumes across all 5.3M rows — the checks
covering those return zero, which is the correct answer and is *not* evidence they work.
That distinction is stated openly in the README and must never be smoothed over in the UI.

## Product Principles

1. **Separate the 44 from the 14,974.** Any surface that presents findings without
   distinguishing what needs a human from what the activity model explains has failed at the
   product's central job, however complete it is.
2. **Every number carries its own justification.** A reviewer meets each screen cold, with
   nobody to explain it. Thresholds, denominators and definitions belong next to the figure
   they qualify — not in a doc, not in a tooltip nobody opens.
3. **State limits in the open.** Honesty about what the corpus does not exhibit, what a
   confidence actually measures, and how few bars a "15-minute average" really averaged is a
   credibility asset with this audience, not a weakness to hide.
4. **Nothing is silently repaired.** A row that cannot be located is rejected with its
   original values; a row that is merely wrong is kept and flagged. The interface must
   preserve that distinction rather than presenting cleaned data as though it were the input.
5. **The empty state is a first impression, not an error.** A service holding no data is a
   valid state and must read as an invitation with an obvious next step.

## Accessibility & Inclusion

**WCAG 2.1 AA contrast is the floor**, adopted deliberately rather than by default: Macquarie
Group's own published fundamentals state that their palette shades meet AA for text against
black or white, so an interface echoing that language and failing the same bar would
undercut its own premise.

Two product-specific consequences:

- **Severity must never be carried by colour alone.** ERROR / WARNING / INFO is the primary
  triage axis of the whole application, and the population is financial-services
  professionals, among whom colour vision deficiency is ordinarily represented. Every
  severity distinction needs a second channel — label, icon, weight or position.
- **The same applies inside charts.** Regime bands (ACTIVE / THIN / DORMANT) and flagged bars
  must remain distinguishable without relying on hue discrimination.

---

# Reading the Results

Everything above is the durable product record: who this is for, what it claims, and what
future work must preserve. Everything below is how to read what the tool actually puts on
screen. The split matters because the two have different readers — the record is for
someone changing the product, this part is for someone interpreting its output.

### What the dashboard shows

Four pages, in the order a user should meet them.

| Page | What it answers |
|---|---|
| **Overview** | What is loaded (instruments, bars, span). Then the triage: errors / warnings / expected, with one sentence saying what "expected" means. Then findings by check, the thresholds that decided each severity, and a contract × month activity heatmap — a deferred contract sits dark for years and brightens near expiry, which is *why* the flat bars inside it are not defects. |
| **Price analytics** | One instrument at two zoom levels: a daily candlestick over a bounded date range, and one session minute-by-minute with close against a rolling VWAP. Under the VWAP is a strip showing how many bars each window actually averaged — with median 222 minute bars in a contract-day against 1,380 possible, "15-minute average" often means two prints, and the chart says so. An expander reconciles the session rebuilt from minutes against the vendor's daily bar. |
| **Data quality** | Every finding, filterable by instrument, check, severity and session range. **Opens on warnings and errors, not on everything** — the INFO tier is one checkbox away and its size is printed above the table. Each finding's evidence, including a sample of source row ids, is in an expander so a claim can be traced back to the input file. Below it, a timeline strip of where the data simply is not. |
| **Insights** | One card per pattern: the plain-English claim, the evidence, and the suggested rule as copyable YAML or JSON. The feed-incident card is pinned to the top because it is the only one implying an action outside the team. |

The sidebar takes a CSV or parquet upload anywhere in the app, reports on it immediately,
and recomputes every other page with it included.

The API is the same surface, documented at `/docs`:
`/health`, `/datasets/summary`, `/contracts`, `/bars`, `/analytics`,
`/analytics/{name}` (generated from the registry — nothing in the API module names an
analytic), `/quality/summary`, `/quality/findings`, `/insights`, `POST /ingest`.

### Pointing it at your own data

Three ways, in increasing order of permanence.

**Upload one file.** Drop a `.csv` or `.parquet` into the dashboard sidebar, or
`POST /ingest`. It becomes its own dataset; the ingest report tells you rows in, rows
that became bars, rows that could not be placed and why, and the findings in the file.

**Point the whole service at a directory.**

```bash
MDQ_DATA_DIR=/path/to/my/bars make ui
```

Every file in the directory is classified by reading its columns only (11 ms for the
116 MB sample); no rows are read until something asks for them.

**Column matching.** A file that does not look like the vendor's layout falls through to
the `generic` profile, which matches these names case-insensitively:

| canonical | accepted source names |
|---|---|
| contract | `contract`, `symbol`, `contract_symbol`, `ticker` |
| timestamp | `timestamp`, `ts`, `datetime`, `date`, `time` |
| open / high / low / close | `open`\|`o`, `high`\|`h`, `low`\|`l`, `close`\|`c`\|`last` |
| volume | `volume`, `vol`, `v` |
| exchange / root / open interest | `exchange`\|`venue`, `root`\|`product`, `open_interest`\|`oi` |

A minimal file is `contract,timestamp,open,high,low,close,volume`. Frequency is inferred
from the median timestamp step. **The time zone is declared, never guessed**: the generic
profile assumes Chicago wall clock, overridable per ingest with
`IngestOptions.timestamp_kind` (`chicago_wall_naive`, `epoch_ms_chicago_wall`,
`epoch_ms_utc`, `iso_utc`, `date`). Nothing is silently repaired — a row that cannot be
*located* (no contract, no usable timestamp) is rejected with its original values; a row
that is merely *wrong* is kept and flagged, because a business user needs to see it in
context.

Thresholds are overridable from the environment, e.g. `MDQ_QUALITY__MAX_GAP_MINUTES=10`.

### What this sample does and does not exhibit

This matters for reading the results honestly, so it is stated up front rather than
buried.

**The vendor sample is pre-normalised.** Across all 5,295,239 minute rows and 30,102
daily rows there are **zero** duplicate timestamps, **zero** null prices or volumes,
**zero** non-positive prices and **zero** negative volumes. The vendor's own
`files.parquet` publishes two of those counts as zero, and our independent recomputation
agrees on 40/40 files.

The exercise requires those checks, and they are implemented — `duplicate_timestamp`,
`missing_value`, `non_positive_price`, `negative_volume`, `malformed_record`,
`off_grid_timestamp`, `ambiguous_local_time`, `timestamp_out_of_range`. On this corpus
they all return zero. That is the correct answer, and it is also no evidence that they
work.

So they are exercised by a **fault-injection harness** (`tests/support/faults.py`)
instead: nineteen fault types — `ExactDuplicate`, `ConflictingDuplicate`, `DropRange`,
`DropSession`, `SwapHighLow`, `CloseOutsideRange`, `CarriedForwardSettlement`,
`NegativePrice`, `NegativeVolume`, `NullField`, `ZeroVolumeWithRange`,
`FlatZeroVolumeBar`, `OffGridSeconds`, `FutureTimestamp`, `AmbiguousLocalTime`,
`NonexistentLocalTime`, `MalformedLine`, `PriceSpike` and `ShiftWallClockToUtc` (the
timestamp trap of ADR-1, which asserts that session aggregation *diverges* from the
vendor's daily file if the wall clock is mistaken for UTC) — each of which is applied to
clean synthetic data and returns the findings it *should* produce. Tests then assert the
report contains **exactly** those findings and nothing else, so each check is measured for
recall and precision. The complementary property is that clean synthetic data yields zero
findings from every check.

Two further things the sample does not exercise, tested with fixtures instead:

- **DST transitions.** Both US transitions in the span (2025-11-02, 2026-03-08) fall
  inside the Friday 16:00 → Sunday 17:00 CME closure, so no real bar sits in either. DST
  correctness still matters for other venues and for uploaded CSVs, and is tested against
  hand-built frames on both sides of both transitions.
- **Malformed input.** The vendor ships parquet; ragged lines, text in a price column and
  thousands separators only arise from CSV, so they are tested from committed CSV
  fixtures.

What the sample *does* exhibit, richly, is the interesting half: 43 genuine OHLC
violations, 14,152 no-range bars, 389 missing sessions, and a liquidity structure that
makes the difference between a usable tool and an unusable one.

### What the tool finds on the real corpus

40 contracts, 8 roots (CL, ES, GC, SB, SR3, VX, ZC, ZN), 6 exchanges, 5,325,341 bars,
116 MB. Daily frequency, full corpus:

| check | findings | severity |
|---|---|---|
| `stale_bar` | 14,152 | all INFO |
| `zero_volume_with_range` | 400 | all INFO |
| `missing_session` | 389 | 1 WARNING, 388 INFO |
| `invalid_ohlc` | 43 | 5 ERROR, 38 WARNING |
| `outlier_return` | 34 | all INFO |
| `duplicate_timestamp`, `missing_value`, `non_positive_price`, `negative_volume`, `malformed_record`, `timestamp_out_of_range` | 0 | — |

**Triage: 5 ERROR · 39 WARNING · 14,974 INFO.** 44 of 15,018 findings need a human;
99.7% are expected artefacts the activity model explained. That ratio is the entire point
of the regime model — without it, 47% of daily rows and 48% of minute rows are flat bars
and every one of them would be a warning.

At minute frequency the same corpus produces 0 ERROR, 139,929 WARNING and 18,132 INFO
across 158,061 findings covering 4.1M bars; 139,780 of the warnings are `intrabar_gap` —
holes inside sessions the contract was genuinely trading, which is the one place a gap is
diagnostic. The full minute quality pass takes about 5.5 s, so the dashboard runs it only
when asked and says so behind a spinner.

**How much of this depends on the thresholds, and how good is each check?**
`docs/EVALUATION.md` is the quantitative answer to both, with two generated reports behind
it. The daily separation above turns out to be almost threshold-independent: 44 findings
need a human at every value swept for all six parameters bar one, and 46 at that one's
degenerate extreme. At minute frequency the same sweep shows the opposite —
`max_gap_minutes` moves the count by a factor of 112 across a defensible range — which is
why the daily report is the one the headline is drawn from. Per-check precision and recall
are measured against the fault harness's own declarations, and the timings quoted in this
README are re-measured there rather than left to rot in prose.

#### The vendor is our oracle

`files.parquet` publishes four per-file diagnostics the vendor computed before we existed.
Our engine reproduces **all four exactly, on 40/40 daily files, with 0 mismatches**:

| vendor column | vendor | ours |
|---|---|---|
| `invalid_ohlc_row_count` | 43 | 43 |
| `no_range_bar_count` | 14,152 | 14,152 |
| `duplicate_timestamp_count` | 0 | 0 |
| `null_price_row_count` | 0 | 0 |

These are external golden tests (`tests/golden/test_quality_golden.py`) — they can catch a
check that is confidently, self-consistently wrong in a way our own fixtures cannot.
Matching the oracle also forced one deliberate correction to the plan: the vendor's
no-range definition does not mention volume, so `stale_bar` detects on
`open == high == low == close` and uses volume as a *severity* discriminator rather than a
filter. Adding the volume clause would have given 12,841 and broken the oracle.

### How to read the insights

Four patterns fire on the daily corpus. Each carries a **confidence**, which always means
the same thing — the share of the relevant findings the pattern accounts for — so two
confidences can be compared and the comparison means something.

| insight | conf. | findings | what it says |
|---|---|---|---|
| `dormancy_explains_staleness` | 0.998 | 14,152 | 99.8% of no-range bars fall in DORMANT or THIN sessions. They are settlement prints on days the instrument did not trade, not a frozen feed. Rule: tag them, exclude from VWAP and volatility, alert only when ACTIVE. |
| `missing_sessions_are_holidays` | 0.997 | 389 | 388 of 389 absent sessions are absent for *every* contract on the exchange. Rule: a `holiday_calendar` of the 41 dates derived. |
| `carried_forward_settlement` | 0.907 | 43 | 39 of the 43 incoherent bars share one signature — `open == high == low != close`, 38 of them with `volume == 0`. OHL were carried forward while `close` was updated to the new settlement, so the settlement sits outside its own bar's range. Rule: exempt that shape from OHLC bounds and treat `close` as a settlement. |
| `feed_incident_date_cluster` | 0.651 | 43 | The violations are not scattered. They strike whole product families on single dates — five CL contracts simultaneously on each of six dates. **Escalate to the data vendor, not the contract owner.** |

Two things worth noticing about that table.

**The confidences are not all high, and that is the number working.** The feed-incident
rule sits at 0.65 precisely because it explains a smaller share of the findings than the
settlement-signature rule does. A rule that always reported high confidence would be
telling you nothing.

**`missing_sessions_are_holidays` derives a real US market holiday calendar with no
calendar shipped.** Purely from cross-contract corroboration it reconstructs Good Friday,
Memorial Day, Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas, New
Year's Day, MLK Day and Presidents' Day across 2022–2026. Holiday calendars are an
explicit non-goal of this project (they are a maintenance liability and a per-venue
licensing question); corroboration turned out strong enough to make one unnecessary.

Every card offers its rule as copyable YAML and JSON, labelled *cleansing* (changes the
data) or *validation* (refuses data), so adopting one is a paste rather than a
re-derivation.
