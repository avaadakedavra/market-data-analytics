# PLAN.md — Market Data Quality & Analytics Platform

Stack (fixed): Python 3.12 via `uv`, Polars, FastAPI, Streamlit, pytest. Package name: `mdq` (`src/mdq`). Project root: `/Users/joelbraganza/Documents/MacquarieCoding`.

---

## 0. Ground truth that drives the design

Measured against the real HuggingFace dataset `lynx1231/historical-futures-data-sample`, revision `29efdfa21c5a5b2d7aa306397385cf116e011559`. All 92 files (116 MB) are downloaded to `data/raw/`; all 82 parquet files verify against the published `checksums.sha256`.

| Fact | Consequence |
|---|---|
| Minute `timestamp_ms`, decoded as a naive datetime, equals `timestamp_chicago_wall` **exactly**. Both are Chicago wall-clock, not UTC. | Canonical time is a tz-aware UTC instant produced by *localising* the wall clock to `America/Chicago`. Never treat `timestamp_ms` as UTC. |
| `trading_date` is just the wall-clock calendar date; there is no session roll in the vendor's minute file. Sundays have bars from 17:00; Fridays end 15:59; no bars 16:00–16:59 CT (maintenance break). Liquid CME sessions have 1,380 bars (23h). | We derive our own `session_date` (roll at 17:00 CT). Verified below: this materially outperforms calendar-date aggregation. |
| Vendor daily `close` is a **settlement price**, not the minute last trade; daily volume runs ~3–5% above the minute sum. | Cross-frequency reconciliation is tolerance-based on H/L/volume and must **not** compare close. This is an insight, not a defect. |
| Both US DST transitions (2025-11-02, 2026-03-08) fall inside the Fri 16:00 → Sun 17:00 closure for CME products. | DST correctness is still mandatory (other exchanges, uploaded CSVs) but is tested with fixtures, not the sample. |
| Sample is pre-normalised: **0 duplicates, 0 nulls, 0 non-positive prices, 0 negative volume** across all 5.3M minute and 30k daily rows. | Crude checks required by the spec are exercised by a fault-injection harness. This is stated plainly in the README rather than disguised. |
| **43 genuine OHLC violations exist** in daily data (CL 28, ES 7, SR3 5, ZC 3). | Structural checks fire on real data. `invalid_ohlc` is not fixture-only. |
| Vendor `files.parquet` publishes per-file `duplicate_timestamp_count`, `null_price_row_count`, `invalid_ohlc_row_count`, `no_range_bar_count`. | **Free golden tests** — our checks must reproduce the vendor's own counts. |
| Minute liquidity is regime-dependent: ESH26 median bars/day is 2 in Jun-2025 and 1,379 in Mar-2026. Corpus-wide minute coverage is ~309 bars/contract-day against ~1,380 possible (22%). | Gap and stale-bar severity must be conditioned on an *observed activity regime*, or 80%+ of rows get flagged and the tool is useless. This contrast is the centrepiece of the Insights deliverable. |

### 0.1 Verification log (independently confirmed before adopting this plan)

**✅ Vendor diagnostics reproduce exactly.** Independently recomputing the four diagnostic
counts across all 40 daily files and joining to `files.parquet` gives **0 mismatches on
40/40 files**: invalid OHLC 43 = 43, no-range bars 14,152 = 14,152, duplicates 0 = 0, null
prices 0 = 0. Golden tests against vendor counts are therefore sound and should be wired in
at WP2.

**✅ The 17:00 CT session roll is correct, ⚠️ but "exact" needs qualifying.** Aggregating
ESH26 minute bars into sessions and joining to the vendor daily file (197 overlapping days):

| | roll @ 17:00 | calendar date |
|---|---|---|
| open | **193/197 (98.0%)** | 36/197 (18.3%) |
| high | 126/197 (64.0%) | 90/197 (45.7%) |
| low | 113/197 (57.4%) | 74/197 (37.6%) |
| close | 2/197 (1.0%) | 2/197 (1.0%) |

Restricted to liquid sessions (≥1000 bars, n=69): open 67/69, high 66/69, **low 69/69**.

The roll is unambiguously right. But O/H/L do **not** match exactly in general — thin
sessions have sparse minute coverage, so a real high/low simply isn't in the minute file.
**Correction to the golden test:** assert O/H/L equality only on specifically verified
liquid sessions (e.g. 2026-03-04/05/10/11), not as a blanket property. And `close` matches
~1% of the time by design — never assert on it.

### 0.2 The headline pattern for the Insights deliverable

39 of the 43 OHLC violations share one signature: `open == high == low` but `close`
differs; 38 of 43 have `volume == 0`. Mechanism: on a non-trading day OHL were **carried
forward** while `close` was **updated to the new settlement**, so the settlement sits
outside its own bar's range.

They are not scattered — all 43 fall on just **11 dates**, striking whole contract families
at once (2022-01-13, 2021-12-16 and 2021-12-22 each hit 7 contracts, mostly every CL
contract simultaneously). That is a **feed-level incident**, not per-contract corruption.

The insights layer must therefore detect **two axes**: the row-shape *signature*, and the
cross-contract *date clustering*.

---

## 1. Architecture overview

Layered; dependencies point downward only. Polars `LazyFrame` is the data carrier throughout; pydantic models appear only at the API boundary.

```
                 ┌───────────────────────────┐   ┌──────────────────────────┐
  presentation   │ mdq.dashboard (Streamlit) │──▶│  mdq.api (FastAPI)       │
                 └────────────┬──────────────┘   └────────────┬─────────────┘
                              │  Backend protocol              │
                              ▼                                ▼
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

Extension seams — each is "add a file, register, done", with no existing code edited:

| Want to add… | Do this | Mechanism |
|---|---|---|
| A new file format (JSON, Arrow IPC) | New module in `mdq/ingest/readers/` implementing `Reader`, decorated `@register_reader(".json")` | `ReaderRegistry` keyed by extension; `pkgutil` auto-imports |
| A new source column layout | New `SourceProfile` in `mdq/ingest/profiles.py` (a data declaration, not code) | Profiles matched by `detect(columns)`; `generic` is the fallback |
| A new quality check | New module in `mdq/quality/checks/` implementing `Check`, decorated `@register_check` | `CheckRegistry`; auto-imported; report, API and dashboard render it with zero changes |
| A new analytic | New module in `mdq/analytics/` implementing `Analytic` with a pydantic `Params`, decorated `@register_analytic` | API generates `GET /analytics/{name}` routes from the registry at startup |
| A new insight rule | New `PatternRule` in `mdq/insights/rules/` | Same registry pattern |
| An LLM-assisted insight engine | Implement the `InsightEngine` protocol | Swapped via config; same `Insight`/`SuggestedRule` output |

Directory layout:

```
pyproject.toml            uv, python 3.12, src layout
Makefile                  test / lint / api / ui / fetch targets
README.md                 quickstart + how to read the results
docs/ARCHITECTURE.md      diagram + ADR-style decisions
scripts/fetch_data.py     HF snapshot -> data/raw (pinned, checksummed)
scripts/make_fixtures.py  carve committed test fixtures from data/raw
data/                     gitignored (data/.gitkeep committed)
src/mdq/
  domain/    schema.py frequency.py findings.py config.py
  time/      localise.py sessions.py
  ingest/    readers/{__init__,parquet,csv}.py profiles.py normalise.py report.py
  analytics/ registry.py daily_bars.py vwap.py filtering.py
  quality/   registry.py context.py report.py cleanse.py checks/*.py
  insights/  engine.py models.py rules/*.py
  service/   store.py service.py
  api/       app.py routes_*.py schemas.py
  dashboard/ app.py backend.py pages/*.py
tests/
  unit/ integration/ golden/ property/
  fixtures/  (small real parquet slices, CSV fixtures, vendor files diagnostics)
  support/   synth.py faults.py
```

---

## 2. Domain model

### 2.1 Canonical bar frame (`mdq/domain/schema.py`)

Every source, both frequencies, maps onto one Polars schema. Column names are constants (`C.CONTRACT`, …) so no string literals leak.

| column | dtype | meaning |
|---|---|---|
| `row_id` | u32 | position in the source file; traceability from findings back to input |
| `contract` | str | e.g. `ESH26` |
| `exchange` | str, nullable | `CME` … (null if source lacks it) |
| `root` | str, nullable | `ES` … (derived from contract if absent: leading letters) |
| `ts_utc` | datetime[us, UTC] | **canonical instant**. Minute: localised wall clock → UTC. Daily: date at 00:00 UTC (a *label*, documented as such) |
| `ts_local` | datetime[us], naive | Chicago wall clock as vendor and traders see it. Display and minute-of-day analysis only; never arithmetic |
| `session_date` | date | trading session the bar belongs to (§2.3). Daily: equals the date |
| `open, high, low, close` | f64, nullable | nulls retained and flagged, never dropped |
| `volume` | i64, nullable | |
| `open_interest` | i64, nullable | daily only; null for minute |

Wrapped in a small deep object:

```python
@dataclass(frozen=True)
class BarFrame:
    lf: pl.LazyFrame          # conforms to BAR_SCHEMA (validated on construction)
    frequency: Frequency      # DAILY | MINUTE — metadata, not a column; frames are homogeneous
    source: str               # file path / upload name, for provenance
    def collect(self) -> pl.DataFrame
    def contracts(self) -> list[str]
    def is_empty(self) -> bool
```

Frequency-specific behaviour lives in **one place**: `Frequency` carries `bar_period: timedelta | None`, and the session module keys on it. Checks declare which frequencies they apply to and the registry filters. Nothing else branches on frequency.

### 2.2 Timestamps — the rule

`mdq/time/localise.py`:

```python
def localise_wall_clock(lf, col, tz="America/Chicago") -> tuple[pl.LazyFrame, pl.LazyFrame]:
    """Returns (frame with ts_utc + ts_local, rejected rows).
    ambiguous='earliest', non_existent='null' -> nulls become rejects with
    reason NONEXISTENT_LOCAL_TIME. Also emits an `is_ambiguous` bool
    (earliest != latest localisation) consumed by the DST check."""
```

`replace_time_zone(tz, ambiguous="earliest", non_existent="null")` then `convert_time_zone("UTC")`. All windowing, gap arithmetic and sorting operate on `ts_utc`. The source's timestamp *kind* is declared by the `SourceProfile` (`chicago_wall_naive` | `epoch_ms_chicago_wall` | `epoch_ms_utc` | `iso_utc` | `date`), so the HF trap is handled by declaration, never by guessing.

### 2.3 Sessions (`mdq/time/sessions.py`)

```python
@dataclass(frozen=True)
class SessionProfile:
    roll_hour_local: int        # bars at/after this local hour belong to the NEXT session_date
    break_local: tuple[time, time] | None   # maintenance window excluded from gap detection
    expected_bars: int | None   # informational baseline (1380 for a CME 23h session)

DEFAULT_SESSIONS: dict[str, SessionProfile]
# CME/CBOT/COMEX/NYMEX/CFE -> roll 17, break 16:00-17:00, 1380
# ICEUS -> roll 0 (calendar), no break;  fallback = calendar
def session_date_expr(profile) -> pl.Expr
```

Roll hour is a small per-exchange table (verified in §0.1). *Coverage expectations* are observed per contract from the data (§4.2), never hardcoded — that is what lets gap detection survive 23h sessions, listing/expiry boundaries and holidays without a holiday calendar.

---

## 3. Module-by-module breakdown

### 3.1 `mdq.ingest`

**Readers** — `Reader` protocol: `extensions: frozenset[str]`; `read(path_or_buffer) -> pl.LazyFrame` (raw columns; all-string for CSV). `ParquetReader` uses `pl.scan_parquet`. `CsvReader` uses `pl.scan_csv(infer_schema=False, ignore_errors=False, truncate_ragged_lines=False)` so *every* value arrives as a string and coercion happens in normalisation with `strict=False`, turning bad cells into nulls attributable to a row and a reason. Ragged/unparseable lines fall back to a line-by-line path producing a rejects frame with `MALFORMED_LINE` (bounded: above `max_reject_ratio`, abort with a clear error). `read_bars(path) -> BarFrame` picks the reader by extension; unknown extension → `UnsupportedFormatError`.

**Profiles** — `SourceProfile(name, required: dict[canonical→source col], optional, timestamp_kind, frequency_hint, detect(cols)->bool)`. Three shipped: `hf_minute` (uses `timestamp_chicago_wall`, falls back to `timestamp_ms` decoded naive), `hf_daily` (uses `date`), `generic` (case-insensitive aliases `contract|symbol|contract_symbol`, `timestamp|ts|datetime|date|time`, `open|o`, …, `volume|vol|v`; timestamp kind from `IngestOptions`, default `chicago_wall_naive`; frequency inferred from median timestamp step unless given).

**Normalise** — `normalise(raw, profile, options) -> IngestResult`:

```python
@dataclass(frozen=True)
class IngestResult:
    bars: BarFrame  # all placeable rows, sorted by (contract, ts_utc, row_id), NOT deduplicated
    rejects: pl.DataFrame  # row_id, reason (enum), raw_values, source
    stats: IngestStats  # rows_in, rows_out, rejects_by_reason, was_sorted, contracts, span
```

Rejection reasons: `MISSING_REQUIRED_COLUMN` (whole file), `UNPARSEABLE_TIMESTAMP`, `NULL_CONTRACT`, `NONEXISTENT_LOCAL_TIME`, `MALFORMED_LINE`. Rows with null/unparseable *prices or volume* are **kept** and flagged by quality checks — they are locatable and the business user should see them in context. Duplicates are kept here too; the quality engine classifies them and `cleanse` removes the safe ones. **Ingestion never silently fixes data.**

Edge cases: empty file → empty `BarFrame`, correct schema, zero rejects; mixed-frequency file → `MixedFrequencyError` (splitting is a non-goal); thousands separators → reject; negative `timestamp_ms` (pre-1970 daily) → valid; `volume` float `12.0` → cast to i64, `12.5` → null + flag.

### 3.2 `mdq.analytics`

```python
class Analytic(Protocol):
    name: ClassVar[str]; frequencies: ClassVar[frozenset[Frequency]]
    Params: ClassVar[type[BaseModel]]
    def run(self, bars: BarFrame, params: BaseModel) -> pl.DataFrame
```

- **`daily_bars`** — group by `(contract, session_date)`: `open=first(open) by ts_utc`, `high=max`, `low=min`, `close=last`, `volume=sum`, `bar_count=len`, `first_ts`, `last_ts`, `null_price_bars`. On DAILY input it is the identity with `bar_count=1`, so the endpoint works for both. Golden test per §0.1: verified liquid sessions only, O/H/L only, never close.
- **`rolling_vwap`** — `Params(window="15m", price=Typical|Close, min_volume=0)`. Time-based and gap-aware: `lf.rolling(index_column="ts_utc", period=window, group_by=["contract","session_date"], closed="right").agg(...)`. Grouping by `session_date` means a window never spans the maintenance break or a weekend. Output per bar: `vwap` (null when window volume is 0 — never divide by zero, never fall back to price), `window_volume`, `bars_in_window`, `window_start_utc`. Typical price `(h+l+c)/3` default. A bar after a 40-minute gap gets `bars_in_window=1`; that is correct and visible, not hidden.
- **`filter_bars(bars, contracts=None, start=None, end=None) -> BarFrame`** — inclusive dates on `session_date`; `start > end` → `InvalidRangeError` (422 at the API); unknown contract → empty frame (404 only at the API, where the contract list is known). Empty results keep the schema.

### 3.3 `mdq.service`

`DatasetStore`: in-memory `{dataset_id: Dataset}`; `Dataset` = `{Frequency: BarFrame}` + `IngestResult`s + lazily cached `QualityReport`, `ActivityProfile`, `list[Insight]`, invalidated on mutation. `MarketDataService` is the single facade for API and dashboard: `load_directory(path)`, `ingest_upload(name, bytes)`, `summary()`, `bars(...)`, `run_analytic(name, params)`, `findings(filter)`, `quality_summary()`, `insights()`. Startup autoload from `MDQ_DATA_DIR` (default `data/raw`) via `pydantic-settings`.

### 3.4 `mdq.api`

FastAPI, pydantic response models, OpenAPI as the API doc. Routes: `GET /health`, `GET /datasets/summary`, `POST /ingest` (multipart CSV/parquet → `IngestReport` incl. rejects sample), `GET /contracts`, `GET /bars`, `GET /analytics` (list from registry), `GET /analytics/{name}` (routes generated from the registry; params model → query params; 422 on bad params), `GET /quality/summary`, `GET /quality/findings?contract&severity&check&start&end&limit`, `GET /insights`. Errors: 400 unsupported format, 404 unknown contract/analytic, 413 upload too large, 422 invalid range. Polars → JSON via `to_dicts()` with `limit` (default 5,000, max 50,000) and `next_offset`.

### 3.5 `mdq.dashboard`

Streamlit multipage against a `Backend` protocol: `HttpBackend` (httpx → API; default) and `EmbeddedBackend` (in-process `MarketDataService`; used by `AppTest` and `--embedded`). Pages: **Overview** (span/contracts, severity counts, findings-by-check bar, contract × month activity-regime heatmap), **Analytics** (contract + date range pickers; candlestick of daily bars; close vs 15-min VWAP with `bars_in_window` shading), **Data Quality** (filterable findings table with evidence expander; gap timeline strip), **Insights** (one card per insight: pattern, evidence, suggested rule as a copyable JSON/YAML block), sidebar **Upload** (CSV/parquet → ingest report + rejects table). Plotly for charts. Empty states everywhere rather than exceptions.

---

## 4. The quality-check engine

### 4.1 Check protocol and registry

```python
class Check(Protocol):
    id: ClassVar[str]                          # "duplicate_timestamp"
    title: ClassVar[str]
    frequencies: ClassVar[frozenset[Frequency]]
    default_severity: ClassVar[Severity]       # INFO | WARNING | ERROR
    suggested_rule_id: ClassVar[str | None]
    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame   # FINDING_SCHEMA

@dataclass(frozen=True)
class CheckContext:
    frequency: Frequency
    config: QualityConfig                      # thresholds, per-exchange sessions
    activity: pl.LazyFrame                     # ActivityProfile (4.2)
    sessions: dict[str, SessionProfile]
    sibling_contracts: pl.LazyFrame | None     # (exchange, session_date, has_bars) for corroboration

def register_check(cls) -> cls                 # CheckRegistry.all(), .for_frequency(f), .get(id)
def run_checks(bars: BarFrame, ctx, checks=None) -> QualityReport
```

Checks return a **vectorised** `pl.DataFrame` (not Python objects) in `FINDING_SCHEMA`: `check_id, severity, contract, frequency, start_utc, end_utc, session_date, count, message, evidence (struct→JSON str: row_ids sample ≤20, values), suggested_rule_id`. `QualityReport` = concat of check outputs + ingest-reject findings (`check_id="malformed_record"`), with `summary()` (counts by check × severity × contract), `to_findings() -> list[Finding]`, `rows_affected(severity>=ERROR) -> row_ids`. Checks are independent and pure; `run_checks` isolates exceptions per check into an `ERROR`-severity `check_failed` finding rather than aborting the report.

### 4.2 Activity profile (`mdq/quality/context.py`) — the regime model

Computed once per dataset, per `(contract, session_date)`: `bar_count`, `volume`, `regime ∈ {DORMANT, THIN, ACTIVE}`. Minute: `p90` = per-contract 90th percentile of `bar_count`; `ACTIVE` if `volume > 0 and bar_count >= 0.5·p90`; `DORMANT` if `volume == 0 or bar_count < 0.1·p90`; else `THIN`. Daily: `DORMANT` if `volume == 0`; `THIN` if `volume < 1%` of contract max; else `ACTIVE`. Thresholds live in `QualityConfig` and are surfaced in the report so a business user can see why a row was downgraded. First/last `ACTIVE` session per contract bounds "expected coverage" for missing-session detection.

### 4.3 Checks to implement

| id | freq | severity | logic | exercised by |
|---|---|---|---|---|
| `malformed_record` | both | ERROR | from `IngestResult.rejects` | **fixtures** (CSV) |
| `missing_value` | both | ERROR | null in any of o/h/l/c/volume | **fixtures**; golden = 0 on sample |
| `duplicate_timestamp` | both | INFO if exact duplicate, ERROR if conflicting | group `(contract, ts_utc)` count>1; compare hashes of value columns | **fixtures**; golden = 0 (matches vendor) |
| `invalid_ohlc` | both | ERROR, downgraded to WARNING when regime DORMANT | `high<low`, `high<max(o,c)`, `low>min(o,c)` | **real**: 43 corpus-wide (golden vs vendor `invalid_ohlc_row_count`) + fixtures for `high<low` |
| `non_positive_price` | both | ERROR | any o/h/l/c ≤ 0; per-root override (CL can legitimately print negative — documented) | **fixtures** |
| `negative_volume` | both | ERROR | volume < 0 | **fixtures** |
| `off_grid_timestamp` | minute | WARNING | seconds/micros ≠ 0 | **fixtures**; golden = 0 |
| `ambiguous_local_time` | minute | WARNING | `is_ambiguous` from localisation | **fixtures** (DST) |
| `timestamp_out_of_range` | both | WARNING | `ts_utc` > now + 1 day, or before 1900 | **fixtures** |
| `stale_bar` | daily | INFO when DORMANT, WARNING when ACTIVE | `volume==0 & o==h==l==c` | **real**: 14,152 corpus-wide (golden vs vendor `no_range_bar_count`) |
| `zero_volume_with_range` | both | WARNING (INFO when DORMANT) | `volume==0 & high != low` | **real** |
| `intrabar_gap` | minute | WARNING | within ACTIVE sessions only, consecutive `ts_utc` delta > `max_gap` (default 5 min), excluding the break window; one finding per gap with `minutes_missing` | **real**: ESH26 Mar-2026; fixtures via `DropRange` |
| `missing_session` | both | WARNING; INFO if all sibling contracts on the exchange are also absent (holiday-like) | Mon–Fri `session_date` with zero bars between a contract's first and last ACTIVE session | **real** (US holidays appear as INFO); fixtures for WARNING |
| `outlier_return` (optional per spec) | both | INFO | \|log(close/prev close)\| > k·rolling MAD over 20 ACTIVE bars, between consecutive ACTIVE bars only | **real**; fixtures with injected spike |
| `cross_frequency_mismatch` (stretch, WP8) | dataset | WARNING | minute-derived session H/L within daily H/L; minute/daily volume ratio outside `[0.8, 1.05]` | **real** (documents the settlement-close difference) |

Fourteen checks plus one stretch. Each is 15–40 lines of Polars plus a test module.

### 4.4 Cleansing (`mdq/quality/cleanse.py`)

`cleanse(bars, report, policy=DEFAULT) -> tuple[BarFrame, CleanseLog]`: drop exact duplicates (keep first `row_id`), drop rows referenced by ERROR findings, keep everything else. Analytics run on the *cleansed* view by default (`?view=raw` available). This closes "handle duplicates/missing/malformed" end-to-end rather than only reporting it.

---

## 5. The insights layer

Rule-based, deliberately: deterministic, unit-testable, explainable to a model-risk reviewer, needs no credentials, and the patterns here are structural enough that rules capture them fully. Stated as a decision in the README.

```python
class InsightEngine(Protocol):
    def derive(self, report: QualityReport, activity: pl.LazyFrame,
               summary: DatasetSummary) -> list[Insight]

@dataclass(frozen=True)
class SuggestedRule:
    rule_id: str; kind: Literal["cleansing", "validation"]
    params: dict[str, Any]; rationale: str; confidence: float   # 0..1, from evidence share

@dataclass(frozen=True)
class Insight:
    id: str; title: str; pattern: str; evidence: dict[str, Any]
    affected_contracts: list[str]; finding_count: int; rule: SuggestedRule
```

`RuleBasedInsightEngine` runs a registry of `PatternRule`s (`applies(report) -> bool`, `build(report, ...) -> Insight`). Shipped rules:

1. **Dormancy explains staleness** — if ≥90% of `stale_bar` findings fall in DORMANT sessions → cleansing rule `classify_settlement_only_bars{volume==0 & flat → tag, exclude from VWAP/volatility, alert only when ACTIVE}`.
2. **Settlement close outside carried-forward range** — `invalid_ohlc` findings where `o==h==l` and `volume==0` → validation rule `ohlc_bounds{exempt: volume==0 & o==h==l; treat close as settlement}`. *This is the 39/43 signature from §0.2.*
3. **★ Feed-incident date clustering** — when one `session_date` trips a check across ≥3 contracts of one root → insight "feed incident on {date} affected {n} {root} contracts" → validation rule `cross_contract_date_quarantine{root, dates}`. *This is the 11-date clustering from §0.2 and is the most distinctive insight available in this dataset.*
4. **Gaps cluster by time of day** — histogram of `intrabar_gap` start `minute_of_day`; a bin with ≥30% of gaps → rule `session_break_window{exchange, hh:mm–hh:mm}`.
5. **Gaps cluster by weekday / session edge** → rule `session_calendar{exchange, open, close}`.
6. **Duplicates are exact** (all INFO) → cleansing rule `drop_exact_duplicates{key: contract+ts}`; **conflicting** → validation rule `reject_conflicting_duplicates{keep: none, alert}`.
7. **Missing sessions corroborated across the exchange** → rule `holiday_calendar{exchange, dates:[…]}`.
8. **Zero volume with range in ACTIVE regime** → validation rule `zero_volume_requires_flat`.
9. **Null/malformed concentration** — if rejects concentrate in one column or file → rule `schema_contract{column, type}`.
10. (with the stretch check) **Daily close is settlement** → rule `reconcile_daily_vs_minute{fields: high, low, volume; tolerance; skip: close}`.

An `LlmInsightEngine` would implement the same protocol, receive `report.summary()` plus evidence samples, and return `Insight`s in the same shape. Documented as the extension point; not built.

---

## 6. Test strategy

Layout: `tests/unit` (pure module tests, synthetic data, fast), `tests/property` (hypothesis), `tests/golden` (committed real slices vs vendor diagnostics), `tests/integration` (FastAPI `TestClient` end-to-end, Streamlit `AppTest` smoke). **Network is never touched in tests.**

**Fixtures** (`tests/fixtures`, generated by `scripts/make_fixtures.py`, <400 KB total): `ESH26_minute_2026-03-02_to_03-13.parquet` (two liquid weeks spanning the 2026-03-08 DST weekend), `ESH26_daily.parquet`, `CLG26_daily.parquet` (dormant-heavy, carries OHLC violations), `vendor_diagnostics.csv` (the four counts from `files.parquet`), plus hand-authored CSVs: `generic_clean.csv`, `generic_malformed.csv` (ragged line, text in price, bad date, blank contract, thousands separator), `generic_utc_iso.csv`.

**Synthetic builder** `tests/support/synth.py`: `minute_sessions(contract, dates, bars_per_session=1380, roll_hour=17, seed)` producing perfect data; `daily_series(...)`. Property: **clean synthetic data yields zero findings** — the precision guard for every check.

**Fault-injection harness** `tests/support/faults.py`: `inject(frame, *faults) -> tuple[pl.DataFrame, list[Expected]]` where faults are dataclasses — `ExactDuplicate(n)`, `ConflictingDuplicate(n)`, `DropRange(start, end)`, `DropSession(date)`, `SwapHighLow(rows)`, `CloseOutsideRange(rows)`, `NegativePrice(rows)`, `NegativeVolume(rows)`, `NullField(col, rows)`, `OffGridSeconds(rows)`, `NonexistentLocalTime(date)`, `AmbiguousLocalTime(date)`, `FutureTimestamp(rows)`, `PriceSpike(row, factor)`, `ShiftWallClockToUtc()` (the trap: asserts session aggregation now *diverges* from vendor daily). Each returns `Expected(check_id, count, row_ids)`; tests assert the report contains exactly those findings and nothing else — recall *and* precision per check.

**Tricky cases, explicitly tested**
- DST spring-forward: 02:30 on 2026-03-08 → rejected `NONEXISTENT_LOCAL_TIME`; neighbours localise correctly.
- DST fall-back **(corrected during WP1 — the original wording here was self-contradictory)**: two 01:30 bars on 2025-11-02 both localise and both flag `ambiguous_local_time`. Under `ambiguous="earliest"` they necessarily receive the **same** `ts_utc`, so they land in the **same** VWAP window and additionally trip `duplicate_timestamp`. The 60-minute fork is between the *earliest* and *latest* readings of that one wall clock — which is precisely what `is_ambiguous` detects by localising twice and comparing. **WP3 must not expect the two bars in different windows.** A vendor that distinguished the two passes would have to supply a UTC offset; ours does not, and that ambiguity is itself the finding.
- Session roll: 16:59 and 17:00 CT on the same wall date land in different `session_date`s. Golden: `daily_bars(ESH26 minute)` O/H/L equals vendor daily **on verified liquid sessions only** (§0.1) and never compares close.
- VWAP: window of only zero-volume bars → null; gap > window → `bars_in_window == 1`; window never spans the break or Fri→Sun; property `min(low) ≤ vwap ≤ max(high)`; identical to naive sum when dense.
- Gaps: a 40-minute hole in an ACTIVE session → one finding, `minutes_missing=39`; same hole when DORMANT → no finding; the maintenance break → no finding; Fri 15:59 → Sun 17:00 → no finding.
- Empty/filtered-to-nothing: every analytic, `run_checks` and `derive` accept an empty `BarFrame` and return empty outputs with correct schema; API returns 200 with `[]`; dashboard shows an empty state.
- Golden counts vs vendor diagnostics: `invalid_ohlc` = 43, `stale_bar` = 14,152, `duplicate_timestamp` = 0, `missing_value` = 0 — verified reproducible in §0.1.

**Coverage target**: ≥90% line coverage on `mdq.domain/time/ingest/analytics/quality/insights`; ≥80% on `mdq.api` and `mdq.service`; `mdq.dashboard` excluded from the threshold (one `AppTest` smoke per page against `EmbeddedBackend`). Enforced with `pytest --cov --cov-fail-under=85`. `ruff` (lint + format) and `mypy --strict` on `src/mdq` (dashboard excluded from strict).

---

## 7. Build order — work packages

Interfaces between packages are the signatures in §§2–5 plus the `BAR_SCHEMA` / `FINDING_SCHEMA` constants. Anything downstream may be built against `tests/support/synth.py` output without waiting for real ingestion.

| WP | Scope | Depends on | Acceptance criteria |
|---|---|---|---|
| **0 Scaffold & data** | `pyproject.toml` (uv, `requires-python = ">=3.12,<3.13"`, deps: polars≥1.44,<2, pyarrow, fastapi, uvicorn, pydantic, pydantic-settings, python-multipart, httpx, streamlit, plotly; dev: pytest, pytest-cov, hypothesis, ruff, mypy, huggingface_hub); src layout; `Makefile`; `scripts/fetch_data.py`; `tests/support/synth.py`; `conftest.py` | — | `uv sync` succeeds; `uv run pytest` passes (synth builder tests); `fetch_data.py --roots ES` populates `data/raw` and verifies checksums; `make lint` clean |
| **1 Domain & time** | `domain/{schema,frequency,findings,config}.py`, `time/{localise,sessions}.py`, `BarFrame` | 0 | Unit tests for both DST transitions, session roll, schema validation errors, `FINDING_SCHEMA`; 100% coverage of `time/` |
| **2 Ingestion** | readers + registry, profiles, normalise, `IngestResult`; CSV fixtures; `scripts/make_fixtures.py`; committed real slices | 1 | HF minute/daily parquet and generic CSV normalise to `BAR_SCHEMA`; malformed CSV yields exactly the expected rejects; unknown extension → `UnsupportedFormatError`; empty file OK; fixtures committed |
| **3 Analytics** | registry, `daily_bars`, `rolling_vwap`, `filter_bars`; property tests | 1 (‖ 2, 4) | All tricky VWAP/session cases in §6 pass on synthetic data; hypothesis properties green; empty input OK |
| **4 Quality engine** | registry, `CheckContext`, `ActivityProfile`, `QualityReport`, `cleanse`, all 14 checks, `tests/support/faults.py` | 1 (‖ 2, 3) | Clean synthetic → zero findings; each fault → exactly its expected finding; regime downgrades verified; `run_checks` isolates a raising check |
| **5 Insights** | `InsightEngine` protocol, models, `RuleBasedInsightEngine`, 10 pattern rules (incl. feed-incident clustering) | 4 | Each rule has a positive and negative test from a synthetic `QualityReport`; insights serialise to JSON |
| **6 Service & API** | `DatasetStore`, `MarketDataService`, FastAPI app with generated analytic routes, schemas; golden tests | 2,3,4,5 | `TestClient`: autoload fixtures → `/quality/summary` matches vendor counts; upload faulty CSV → findings + insights; `/analytics/rolling_vwap` in OpenAPI without being hand-written; 404/422/400 paths tested; golden daily O/H/L passes |
| **7 Dashboard** | `Backend` protocol, `HttpBackend`, `EmbeddedBackend`, 4 pages + upload sidebar, Plotly charts | 6 | `AppTest` smoke per page with `EmbeddedBackend`; `make ui` + `make api` run together; empty states verified |
| **8 Docs & polish (+ stretch)** | `README.md` (quickstart, what the sample does/doesn't exhibit, how to read insights), `docs/ARCHITECTURE.md`, coverage gate, `cross_frequency_mismatch` if time allows | all | `make test` ≥85% overall; README reproducible from a clean clone in ≤5 commands |

Parallelism: WP2, WP3 and WP4 are independent once WP1 lands — disjoint packages sharing only `synth.py` and `BAR_SCHEMA`. WP5 can start against a hand-built `QualityReport` as soon as `FINDING_SCHEMA` exists.

---

## 8. Data acquisition

`scripts/fetch_data.py`: `huggingface_hub.snapshot_download(repo_id="lynx1231/historical-futures-data-sample", repo_type="dataset", revision="29efdfa21c5a5b2d7aa306397385cf116e011559", local_dir="data/raw", allow_patterns=[...])`, then verify every downloaded file against `checksums.sha256` and fail loudly on mismatch. Flags: `--roots`, `--frequencies`, `--all` (default: all 80 bar files, ≈110 MB). `data/` is gitignored (`data/.gitkeep` committed); tests depend only on `tests/fixtures`. README records the revision, the licence notice (`SOURCE_NOTICE.md`) and that this is an evaluation sample.

Note: `README.md` and `dataset.json` in the dataset do **not** match `checksums.sha256` (docs edited after the manifest was cut). All 82 parquet files do. The verifier should therefore check data files strictly and warn — not fail — on the two documentation files.

---

## 9. Risks and explicit non-goals

**Risks**
- Regime thresholds are heuristics; a wrong threshold flips severities. Mitigated by config, surfacing in the report, and the Overview heatmap making classification visible.
- The session table is verified only for CME-family products; ICEUS uses calendar dates. Stated as an assumption in `ARCHITECTURE.md`.
- Vendor timestamps are Chicago wall clock even for non-Chicago venues; we follow the vendor and say so.
- Streamlit `AppTest` can be brittle; keep smoke tests minimal and logic in the `Backend`, not in pages.
- Polars API drift: pin `<2`.
- HF availability: never on the test path.

**Non-goals (deliberate)**: persistence/database (in-memory store; restart reloads `data/raw`); authentication; real-time/streaming ingestion; exchange holiday calendars (replaced by cross-contract corroboration); tick-size and contract-spec validation; continuous/rolled front-month series; open-interest analytics; LLM integration (interface only); splitting mixed-frequency files; scaling to the 900M-row commercial dataset (lazy scanning is used, distributed compute is not); fully generic timezone inference (timezone is declared, not guessed).
