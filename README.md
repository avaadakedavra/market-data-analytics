# Market Data Quality & Analytics

A platform for loading futures bar data, finding what is wrong with it, and explaining
what the defects mean. It ingests minute and daily OHLCV files (parquet or CSV), maps
them onto one canonical schema with a real UTC instant derived from the vendor's declared
wall clock, runs fourteen quality checks whose severity is conditioned on an *observed*
per-contract activity regime, derives cross-cutting insights with a concrete rule to
adopt for each, and serves the lot through a documented FastAPI service and a Streamlit
dashboard. It is built around the finding that on real futures data, unconditional
flagging is useless: the 116 MB evaluation sample produces 15,018 daily findings, of
which **44 need a human** and 14,974 are correct settlement prints and exchange holidays
that the activity model explains.

---

## Quickstart

Python 3.12 and [`uv`](https://docs.astral.sh/uv/) are the only prerequisites.

```bash
uv sync                  # 1. create the venv from the lockfile
make fetch               # 2. download + checksum-verify the pinned 116 MB sample
make test                # 3. 927 tests, coverage gate enforced
make dev                 # 4. API on :8000 (/docs) and dashboard on :8501
```

Four commands, verified from a clean copy of the tree. Step 2 is optional — the test
suite never touches the network or `data/`, and the dashboard comes up with an empty
state and an upload box if no data is present.

| Target | What it does |
|---|---|
| `make test` | `pytest` with coverage and `--cov-fail-under=98` |
| `make lint` / `make fmt` | `ruff check` + `ruff format --check` / auto-fix |
| `make typecheck` | `mypy --strict` over `src/mdq` |
| `make fetch` | Pinned HuggingFace snapshot → `data/raw`, SHA-256 verified. Idempotent and offline-safe once fetched |
| `make api` | FastAPI on `:8000`; OpenAPI docs at `/docs` |
| `make ui` | Dashboard alone, service in-process — one command, no port to coordinate |
| `make ui-http` | Dashboard as an HTTP client of a running `make api` |
| `make dev` | Both together |
| `make fixtures` | Regenerate `tests/fixtures` from `data/raw` |

---

## What the dashboard shows

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

---

## Pointing it at your own data

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

---

## What this sample does and does not exhibit

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

---

## What the tool finds on the real corpus

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

### The vendor is our oracle

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

---

## How to read the insights

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

---

## Project layout

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

## Tests

```bash
make test                                # everything, with the coverage gate
uv run pytest tests/unit -q              # fast, synthetic only
uv run pytest -m golden -q               # against committed real slices + vendor counts
uv run pytest -m integration -q          # FastAPI TestClient + Streamlit AppTest
make lint && make typecheck              # ruff (lint + format) and mypy --strict
```

**927 tests, 98.9% line coverage** (`--cov-fail-under=98`). Every non-Streamlit module is
at **100%**; the 45 uncovered statements are all Streamlit render paths that `AppTest`
does not reach. `ruff` and `mypy --strict` are clean.

The suite never touches the network and never reads `data/`. Golden tests run against
small committed slices of real vendor files (~400 KB total) plus the vendor's published
diagnostics; the one test that will use `data/raw` if it happens to be there skips
cleanly when it is not.

---

## Data source, licence and attribution

The sample is `lynx1231/historical-futures-data-sample` on HuggingFace, pinned to
revision **`29efdfa21c5a5b2d7aa306397385cf116e011559`**. `scripts/fetch_data.py` fetches
exactly that revision and verifies every downloaded file against the published
`checksums.sha256`; all 82 parquet files verify, and the two documentation files
(`README.md`, `dataset.json`) are known to have been edited after the manifest was cut and
are warned about rather than failed on.

The dataset's own `SOURCE_NOTICE.md` states:

> This package is an **evaluation sample** maintained by the operator of
> https://futuresforexandsomeindexes.com. The complete commercial dataset, coverage
> catalog, documentation, and pricing are available through that website.

It is used here only to evaluate this exercise. `data/` is gitignored and no vendor data
is committed to this repository beyond the small test fixtures carved from it by
`scripts/make_fixtures.py`.
