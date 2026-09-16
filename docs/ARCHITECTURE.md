# Architecture

Four layers, dependencies pointing downward only. A Polars `LazyFrame` is the data
carrier throughout; pydantic models appear only at the API boundary.

```
                 ┌───────────────────────────┐   ┌──────────────────────────┐
  presentation   │ mdq.dashboard (Streamlit) │──▶│  mdq.api (FastAPI)       │
                 └────────────┬──────────────┘   └────────────┬─────────────┘
                              │  Backend protocol             │
                              ▼                               ▼
  application    ┌────────────────────────────────────────────────────────┐
                 │  mdq.service  (MarketDataService, DatasetStore, cache) │
                 └───┬──────────────┬──────────────┬──────────────┬───────┘
                     ▼              ▼              ▼              ▼
  domain logic   ┌────────┐  ┌───────────┐  ┌──────────┐  ┌──────────┐
                 │ ingest │  │ analytics │  │ quality  │─▶│ insights │
                 └───┬────┘  └─────┬─────┘  └────┬─────┘  └────┬─────┘
                     └─────────────┴─────────────┴─────────────┘
                                          ▼
  foundation     ┌────────────────────────────────────────────────────────┐
                 │  mdq.domain (canonical schema, Frequency, Finding,     │
                 │  Severity, config)   +   mdq.time (localise, sessions) │
                 └────────────────────────────────────────────────────────┘
```

**The rules the arrows encode.**

- `mdq.domain` and `mdq.time` import nothing from the rest of the package. Everything
  else agrees on two constants, `BAR_SCHEMA` and `FINDING_SCHEMA`, which is what let
  ingestion, analytics, quality and insights be built in parallel against synthetic data.
- The four domain-logic packages do not import each other, with one exception drawn in
  the diagram: `insights` reads a `QualityReport`. It never reads bars.
- `mdq.service` is the only thing that knows about all four. `MarketDataService` is the
  single facade; an API route is three lines — parse, delegate, paginate.
- Neither presentation layer knows about ingestion, checks, cleansing or insights. The
  dashboard talks to a `Backend` protocol with two implementations: `HttpBackend` (an
  ordinary client of the documented API) and `EmbeddedBackend` (the service in-process).
  That is what makes `make ui` a single command and what lets `AppTest` drive every page
  without a socket.

**Two frequencies, one code path.** `Frequency` carries `bar_period`, checks and analytics
*declare* which frequencies they apply to, and the registries filter on that declaration.
Nothing else in the codebase branches on frequency.

---

## Repository layout

Where each layer of the diagram above actually lives.

```
src/mdq/
  domain/     canonical BAR_SCHEMA + FINDING_SCHEMA, Frequency, Severity, config
  time/       wall-clock -> UTC localisation, session profiles and the 17:00 roll
  ingest/     readers/ (registry by extension), profiles (data declarations), normalise
  analytics/  registry, daily_bars, rolling_vwap, filter_bars
  quality/    registry + runner, activity-regime context, report, cleanse, checks/ (14)
  insights/   InsightEngine protocol, RuleBasedInsightEngine, rules/ (10)
  service/    DatasetStore, MarketDataService — the one facade both front ends use
  api/        FastAPI app, routers, schemas; analytic routes generated from the registry
  dashboard/  Streamlit shell, Backend protocol (HTTP or embedded), 4 pages, charts
scripts/      fetch_data.py (pinned + checksummed), make_fixtures.py
tests/        unit/ (775) property/ (3) golden/ (45) integration/ (104)
docs/         ARCHITECTURE.md — the diagram, the six extension seams, the decision records
PLAN.md       the design record, with the measurements that drove each choice
```

Dependencies point strictly downward; see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Extension seams

Each of these is "add a file, register, done", with no existing file edited. All four
registries discover their contents with `pkgutil`, so there is no import list to forget.

### 1. A new file format

Drop a module in `src/mdq/ingest/readers/`:

```python
@register_reader(".json")
class JsonReader:
    def read(self, source: ReadSource) -> pl.LazyFrame:
        return pl.scan_ndjson(source)
```

The decorator sets `extensions` on the class and registers the instance, so the protocol
is satisfied by the declaration rather than by a duplicated literal. A reader returns the
source's columns *untouched* — no renaming, no coercion, no validation, because that all
belongs where a bad value can be attributed to a row id and a reason. One reserved
convention lets a reader report a line it could not parse at all: emit the raw text in
the `MALFORMED_LINE_COLUMN`, and normalisation turns it into a `MALFORMED_LINE` reject.
Claiming an extension another reader already owns is a startup error, never a silent
import-order-dependent overwrite.

### 2. A new source column layout

Add a `SourceProfile` to `PROFILES` in `src/mdq/ingest/profiles.py`. A profile is a **data
declaration, not code**:

```python
BLOOMBERG_DAILY = SourceProfile(
    name="bloomberg_daily",
    timestamps=(TimestampSource("PX_DATE", "date"),),
    required={C.CONTRACT: ("SECURITY",)},
    optional={C.OPEN: ("PX_OPEN",), C.CLOSE: ("PX_LAST",), ...},
    discriminators=("PX_SETTLE",),
    frequency_hint=Frequency.DAILY,
)
```

Profiles are matched in order by `detect(columns)`; `generic` is last and is also the
fallback. `discriminators` exist because two layouts can share a timestamp column — the
vendor's minute and daily files both carry `timestamp_ms`, and `minute_of_day`
distinguishes them. **The timestamp kind is always declared**, per candidate column, and
that is the seam's most important job (ADR-1).

### 3. A new quality check

Drop a module in `src/mdq/quality/checks/`:

```python
@register_check
class TickSizeViolation:
    id: ClassVar[str] = "tick_size_violation"
    title: ClassVar[str] = "Price is not a multiple of the tick size"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset(Frequency)
    default_severity: ClassVar[Severity] = Severity.WARNING
    suggested_rule_id: ClassVar[str | None] = None

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        ...  # return FINDING_SCHEMA; use ctx.with_regime(bars) to downgrade
```

The report, `/quality/summary`, `/quality/findings`, the Overview bar chart, the findings
table and its filters all pick it up with no edit anywhere. Findings are **vectorised** —
a `pl.DataFrame` in `FINDING_SCHEMA`, never Python objects in a loop — and
`mdq.quality.emit.aggregate_by_session` does the grouping, messaging and evidence
packing so a check is 15–40 lines of Polars. `run_checks` treats every check as untrusted:
one that raises, or returns a frame that is not `FINDING_SCHEMA`, becomes a single
ERROR-severity `check_failed` finding and the other thirteen still produce their report.

### 4. A new analytic

Drop a module in `src/mdq/analytics/`:

```python
@register_analytic
class RealisedVolatility:
    name: ClassVar[str] = "realised_volatility"
    frequencies: ClassVar[frozenset[Frequency]] = frozenset({Frequency.MINUTE})

    class Params(BaseModel):
        window: str = "1d"
        annualise: bool = True

    def run(self, bars: BarFrame, params: Params) -> pl.DataFrame: ...
```

`GET /analytics/realised_volatility` then appears in the OpenAPI document with `window`
and `annualise` as typed query parameters, with their real defaults and constraints, and
`?window=bogus` is a 422 produced by the analytic's own model. **Nothing in `mdq.api`
names an analytic**: `build_analytic_routes` walks the registry at startup, merges each
`Params` with the shared `BarSelection` into one request model, and attaches it to a
handler through `__signature__`. This is why `Params` must be a real introspectable
pydantic model rather than a `dict` — the documentation and the validation both fall out
of it. A `Params` field colliding with a `BarSelection` field is a hard startup error, not
a silent shadowing at request time.

### 5. A new insight rule

Drop a module in `src/mdq/insights/rules/`:

```python
@register_rule
class MyPattern:
    id: ClassVar[str] = "my_pattern"
    title: ClassVar[str] = "..."
    def applies(self, ctx: InsightContext) -> bool: ...
    def build(self, ctx: InsightContext) -> Insight: ...
```

`applies` is asked before `build`, which is what lets a rule ship *before* the check it
consumes exists. `settlement_reconciliation.py` is the live example: it reads
`cross_frequency_mismatch` findings, that check is not built, `applies` is simply false,
and the engine produces nothing — no error, no empty card, no placeholder. Its docstring
declares the evidence contract (`field`, `relative_difference`) any future implementation
must honour. Rules are isolated exactly as checks are: one that raises is recorded as a
`RuleFailure` and the other nine still run, and failures are never dressed up as insights
on a page a business user reads.

### 6. An LLM-backed insight engine

`InsightEngine` is a protocol:

```python
class InsightEngine(Protocol):
    def derive(self, report, activity=None, summary=None) -> list[Insight]: ...
```

`RuleBasedInsightEngine` is one implementation. An `LlmInsightEngine` would be another:
it receives `report.summary()` plus bounded evidence samples and returns the same
`Insight` objects, with the same `SuggestedRule` shape and the same `confidence`
semantics. `MarketDataService` holds an engine instance, so swapping it is a construction
argument; the API, the dashboard and the `Insight` schema are untouched. The reason this
was not built is ADR-5.

---

## Decision records

### ADR-1 — The canonical instant is derived by localising a declared wall clock

**Context.** The vendor's minute files carry both `timestamp_chicago_wall` (a naive
datetime) and `timestamp_ms` (an integer). The name `timestamp_ms` reads as a unix epoch.
It is not. Decoded as a naive datetime, `timestamp_ms` equals `timestamp_chicago_wall`
*exactly*, across all 5.3M rows — both are Chicago wall clock. Treating either as UTC
shifts every bar by five or six hours, and the failure is silent: charts still draw,
aggregations still return numbers, and session boundaries land in the wrong place.

**Decision.** `ts_utc` is always *derived*: localise the declared wall clock to its zone
(`replace_time_zone(tz, ambiguous="earliest", non_existent="null")`) then
`convert_time_zone("UTC")`. All windowing, gap arithmetic and sorting use `ts_utc`.
`ts_local` is carried alongside for display and minute-of-day analysis and is never used
for arithmetic. **The kind of every timestamp column is declared by its `SourceProfile`**,
never inferred — `chicago_wall_naive`, `epoch_ms_chicago_wall`, `epoch_ms_utc`, `iso_utc`,
`date`. Guessing is not a fallback; it is the bug.

**Consequences.** Two hours a year are not clean wall clocks, and both are handled
explicitly rather than by luck. Spring-forward 02:00–02:59 never happens, so those rows
become rejects with reason `NONEXISTENT_LOCAL_TIME` — never silently shifted into 03:00.
Fall-back 01:00–01:59 happens twice; `ambiguous="earliest"` picks the first pass
deterministically and the row is flagged `is_ambiguous`, computed by localising twice
(earliest and latest) and comparing. Because both passes then share one `ts_utc`, two
fall-back bars land in the same VWAP window *and* trip `duplicate_timestamp`. That is
correct: a vendor who wanted them distinguished would have to supply a UTC offset, and
the ambiguity is itself the finding. The daily files carry only a date, so their `ts_utc`
is midnight UTC — documented in the schema as a **label**, not a trading instant.

**Measurement.** `timestamp_ms` decoded naively == `timestamp_chicago_wall`, exactly, on
every minute row. The fault harness carries a `ShiftWallClockToUtc` scenario whose whole
purpose is to assert that session aggregation *diverges* from the vendor's daily file if
this decision is reversed.

### ADR-2 — Sessions roll at 17:00 local, per exchange

**Context.** A CME trading day is not a calendar day: the session that settles on Monday
opens at 17:00 CT on Sunday. The vendor's minute file has no session column —
`trading_date` is just the wall-clock calendar date — so we have to derive one, and the
choice changes every aggregation downstream.

**Decision.** `SessionProfile(roll_hour_local, break_local, expected_bars)` per exchange.
CME-family venues (CME, CBOT, COMEX, NYMEX, CFE): roll at 17:00, maintenance break
16:00–17:00 excluded from gap detection, 1,380 bars in a full session. ICEUS and anything
unverified: `CALENDAR_SESSION`, roll hour 0, session date == calendar date. Bars at or
after the roll hour belong to the *next* session date.

**Measurement.** Aggregating ESH26 minute bars into sessions and joining to the vendor's
own daily file over 197 overlapping days:

| field | roll @ 17:00 | calendar date |
|---|---|---|
| open | **193/197 (98.0%)** | 36/197 (18.3%) |
| high | 126/197 (64.0%) | 90/197 (45.7%) |
| low | 113/197 (57.4%) | 74/197 (37.6%) |
| close | 2/197 (1.0%) | 2/197 (1.0%) |

Restricted to liquid sessions (≥1,000 bars, n=69): open 67/69, high 66/69, **low 69/69**.

**Consequences.** The roll is unambiguously right, but O/H/L do **not** match in general —
a thin session has no minute bar at its true high, so a real high is simply absent from
the minute file. The golden test therefore asserts O/H/L equality only on specifically
verified liquid sessions, never as a blanket property, and **never asserts on close** (see
ADR-3). `expected_bars` is informational only and is never used as a threshold; coverage
expectations are observed per contract from the data (ADR-4), which is what lets gap
detection survive 23-hour sessions, listing and expiry ramps and holidays without a
calendar.

### ADR-3 — The daily close is a settlement price and is never reconciled

**Context.** The obvious cross-frequency check is "compare the daily bar with the same
session rebuilt from minutes, flag anything that differs".

**Decision.** Reconcile high, low and volume with tolerances. **Exclude `close` by name**,
recording the settlement convention as the reason.

**Measurement.** The daily close agrees with the minute last trade on 2 of 197 sessions
(~1%), and on **zero** of the four verified liquid sessions where O, H and L all match
exactly. Daily volume runs 3–5% above the sum of the minute bars.

**Consequences.** One per cent is not a tolerance problem; it is a definition problem. The
daily close is the exchange's settlement price — struck from a closing range or calculated
by the exchange — and the minute file's last bar is simply the last trade to print. They
are two different quantities that share a name. Left in, the close generates a permanent
stream of correct-but-failing comparisons; nobody triages the same false alarm for long,
and when the reconciliation is switched off in frustration, the genuine breaks in high,
low and volume go dark with it. Excluding the close by name is what keeps the rest of the
check alive. The Analytics page states this in the reconciliation table rather than hiding
it, and `insights/rules/settlement_reconciliation.py` encodes it as an adoptable rule.

### ADR-4 — Severity is conditioned on an observed activity regime

**Context.** Measured on the corpus: 47% of daily rows and 48% of minute rows are flat
bars; 81% of CLG26's daily bars are flat; the median contract-day with any minute data has
222 bars against 1,380 possible. ESH26's median bars/day is 2 in June 2025 and 1,379 in
March 2026 — the same instrument, an order of magnitude apart, because a deferred future
barely trades until it nears the front. A checker that flags all of that unconditionally
produces noise, and a report nobody reads is worse than no report.

**Decision.** Classify every `(contract, session_date)` into `DORMANT | THIN | ACTIVE`
against a baseline **observed per contract from the data itself** — the p90 of `bar_count`
for minute frames, the maximum session volume for daily frames — never a constant, never a
calendar. Checks whose finding could be explained by "nothing was trading" consult
`ctx.with_regime(bars)` and downgrade accordingly. Every threshold lives in
`QualityConfig` and is **written into the report** alongside the regime, so the Overview
page can show a user the numbers that downgraded their row.

**Consequences.** 15,018 daily findings become 5 ERROR, 39 WARNING and 14,974 INFO: 44
things need a human and the rest are explained. `intrabar_gap` fires only inside ACTIVE
sessions, so the Friday-to-Sunday closure and the daily maintenance break produce nothing.
`missing_session` drops to INFO when every sibling contract on the exchange is also absent,
which is what turns 388 of 389 gaps into a derived holiday calendar (see the README).
`invalid_ohlc` keeps ERROR in a live session and drops to WARNING in a dormant one — 5 and
38 respectively. The risk is real and stated: these are heuristics, and a wrong threshold
flips a severity. It is mitigated three ways — the numbers are configurable, they travel
with the report, and the Overview heatmap makes the classification visible per contract
per month so a user can see the model's judgement rather than trust it. The sensitivity of
this split to each threshold is now measured rather than left as a stated risk:
`docs/EVALUATION.md`, and the sweep behind it, report it per parameter per frequency.

### ADR-5 — Insights are rule-based, with the LLM as a documented seam

**Context.** The exercise asks for insights and suggested rules. An LLM is the obvious
reach.

**Decision.** Ship `RuleBasedInsightEngine`: ten deterministic `PatternRule`s behind an
`InsightEngine` protocol. Do not ship an LLM engine; document the seam (§6 above) and
build the protocol so one drops in without touching anything upstream.

**Consequences.** Rules are deterministic, unit-testable with a positive and a negative
case each, explainable to a model-risk reviewer line by line, need no credentials, and
never invent a number — every figure on the Insights page traces back to findings in the
report. For *this* data that costs nothing, because the patterns are structural: a shared
row shape (39 of 43), a date cluster (five CL contracts at once), a regime correlation
(99.8%), a cross-contract corroboration (388 of 389). Rules capture those completely; an
LLM would restate them less reliably and could not be regression-tested.

Where an LLM would genuinely add value is the case rules are bad at — a novel pattern
nobody wrote a rule for, and prose that adapts to a non-technical reader. That engine
would receive `report.summary()` plus bounded evidence samples and return the same
`Insight` objects. Two things would have to travel with it into production and are worth
naming now: every number in the returned insight must be checkable against the report
(the rule-based engine's `evidence` dict is exactly that contract), and the evidence
samples leaving the process are client market data.

### ADR-6 — `?view=raw` is the default, not `?view=clean`

**Context.** The plan called for analytics to run on the cleansed view by default
(duplicates collapsed, ERROR rows removed), which is the intuitively safer choice.

**Measurement.** On daily data it is free: 0.07 s to check, 0.02 s to cleanse. On the
minute corpus it costs 4.9 s to check plus 3.0 s to cleanse.

**Decision.** Default to `raw`. `?view=clean` is one parameter away and is memoised after
the first call.

**Consequences.** A single-contract chart request would otherwise drag all 5.3M rows
through the quality engine before drawing anything, and the interactive product would stop
being interactive. The cost of the choice is that a chart can show a row the quality engine
would have removed — which is mitigated by the fact that ERROR rows are 5 in the whole
daily corpus and 0 at minute frequency, and that the Data Quality page names every one of
them. `CleanseLog` travels with the clean view, so nothing is ever removed silently.

### ADR-7 — Lazy Polars end to end; the store is in memory

**Context.** The sample is 116 MB and 5.3M rows; the commercial dataset it is drawn from
is ~900M rows. A take-home should not pretend to solve the second, but it should not be
architecturally incapable of it either.

**Decision.** `pl.LazyFrame` is the carrier from reader to API boundary; nothing collects
until a caller needs rows. Startup **registers** files rather than reading them —
classifying all 80 files by reading their columns only costs 11 ms — and the first request
that wants a frequency pays for it (0.12 s daily, 1.1 s minute). `DatasetStore` is a
plain in-memory dict of datasets with lazily cached `QualityReport`, `ActivityProfile` and
insights, invalidated on mutation. No database.

**Consequences.** `/health` can be polled every second against the whole corpus without
pulling a row, and `in_memory` reports which frequencies have actually been materialised,
so laziness is observable rather than asserted. Measured warm latencies on the full corpus
(FastAPI `TestClient`, median of 20):

| endpoint | median | p95 |
|---|---|---|
| `GET /health` | 0.9 ms | 1.2 ms |
| `GET /bars` daily, 5,000 rows | 19.0 ms | 19.4 ms |
| `GET /bars` minute, 5,000 rows | 18.3 ms | 18.7 ms |
| `GET /analytics/daily_bars` (ESH26 from minute) | 10.0 ms | 14.4 ms |
| `GET /analytics/rolling_vwap` (one session) | 12.0 ms | 12.3 ms |
| `GET /quality/summary` daily | 3.2 ms | 3.5 ms |
| `GET /insights` daily | 1.0 ms | 1.1 ms |

The trade is stated plainly: a restart reloads `MDQ_DATA_DIR`, uploads do not survive it,
and nothing here is distributed. Lazy scanning is the part that would carry over to a
larger corpus; a single process is not.

### ADR-8 — Ingestion never silently fixes data

**Context.** Every ingestion layer faces the same choice about a bad row: drop it, repair
it, or keep it.

**Decision.** A row is **rejected** only when it cannot be *located* — no contract, no
usable timestamp, an unparseable line. Everything else is **kept and flagged**. Duplicates
are kept at ingest and classified by the quality engine; `mdq.quality.cleanse` removes the
safe ones later, under a policy, with a log.

**Consequences.** A row with a negative price, a null volume or an incoherent OHLC arrives
in the bar frame with its `row_id` intact, appears in the findings table with its evidence,
and can be traced back to the line of the file it came from. The business user sees it in
context rather than discovering later that something disappeared. Rejects come back with
their original values so the fix is obvious rather than guessed at, and the rejects frame
is bounded — above `max_reject_ratio` (default 5%) ingestion aborts with a clear error
instead of producing a million reject rows.

### ADR-9 — The vendor's published diagnostics are the golden test

**Context.** Every test we write asserts against an expectation we also wrote. That cannot
catch a check that is confidently, self-consistently wrong.

**Decision.** `files.parquet` publishes `invalid_ohlc_row_count`, `no_range_bar_count`,
`duplicate_timestamp_count` and `null_price_row_count` per file. Our checks must reproduce
them. `tests/golden/` asserts this against committed complete vendor files.

**Consequences.** All four reproduce exactly on 40/40 daily files, 0 mismatches. It also
forced a correction the plan had wrong: the plan defined `stale_bar` as
`volume == 0 & o == h == l == c` *and* claimed the vendor's count of 14,152. Those are
inconsistent — the volume clause gives 12,841. Detection now follows the vendor's
definition (no volume clause, 14,152) and volume becomes a **severity** discriminator
instead of a filter, which loses nothing because volume is carried in the evidence either
way. Without the external oracle this would have shipped looking correct.

---

## Assumptions

Stated as commitments, with what would falsify each.

1. **All vendor timestamps are Chicago wall clock, including non-Chicago venues.** The
   vendor does this and we follow it. Verified for the minute files; a non-CME venue
   publishing in its own local time would need a per-profile `timezone`, which
   `IngestOptions` already carries.
2. **The 17:00 roll is verified for CME-family products only** (CME, CBOT, COMEX, NYMEX,
   CFE). ICEUS and anything unrecognised use `CALENDAR_SESSION`. Adding a venue is one row
   in `DEFAULT_SESSIONS`; getting it wrong shows up immediately as a collapse in the
   open-match rate of ADR-2's table.
3. **Activity regimes are heuristics, not ground truth.** They are configurable, they
   travel with the report, and the Overview heatmap renders them. A wrong threshold flips
   a severity, not a finding — nothing is ever hidden, only ranked.
4. **A daily bar's `ts_utc` is a label.** Daily files carry a date, not an instant. The
   schema documents this; no arithmetic treats it as a trading time.
5. **Findings are about rows that exist plus sessions that should.** Anything else — a
   contract that was never loaded, a file that was never delivered — is outside what the
   data can tell us.

## Non-goals

Deliberate, and each one is a decision rather than an omission.

- **Persistence.** In-memory store; a restart reloads `MDQ_DATA_DIR` (ADR-7).
- **Authentication and authorisation.** No user model, no tenancy.
- **Streaming or real-time ingestion.** Batch files only.
- **Exchange holiday calendars.** Replaced by cross-contract corroboration, which
  reconstructed 41 real US market holidays from the data alone. Shipping a calendar is a
  per-venue maintenance and licensing liability; deriving one is free and self-updating.
- **Tick-size and contract-spec validation.** Needs a reference-data source this exercise
  does not have.
- **Continuous / rolled front-month series** and **open-interest analytics.**
- **LLM integration.** Interface only, by decision (ADR-5).
- **Splitting a mixed-frequency file.** `MixedFrequencyError`, with `IngestOptions.frequency`
  as the escape hatch.
- **Scaling to the 900M-row commercial dataset.** Lazy scanning carries over; distributed
  compute is not attempted (ADR-7).
- **Guessing a time zone.** It is declared (ADR-1).

### Not built: `cross_frequency_mismatch`

The planned stretch check — minute-derived session H/L within the daily H/L, minute/daily
volume ratio outside `[0.8, 1.05]` — is not implemented, and its consumer rule
`daily_close_is_a_settlement_price` therefore never fires. This is visible rather than
hidden: the rule ships, its `applies` returns false, and its docstring declares the
evidence contract (`field`, `relative_difference`) an implementation must honour.

It is worth saying why it is more than a 40-line check. Every other check has the
signature `run(bars: pl.LazyFrame, ctx: CheckContext)` and sees exactly one frequency;
`CheckContext` carries no counterpart frame. A dataset-level check comparing two
frequencies needs a new seam — a `counterpart: BarFrame | None` on the context, or a
separate `DatasetCheck` protocol with its own registry and its own path through
`run_checks`, the service and the API. That is an architectural addition, and adding it
under time pressure to meet the same 100%-coverage bar as everything else was the worse
trade against getting the written deliverables right. The analysis it would have produced
is already stated where it matters — the Analytics page reconciliation table, ADR-3, and
`settlement_reconciliation.py` — with the measurements behind it.
