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

**Documentation**

| Document | What it covers |
|---|---|
| [PRODUCT.md](PRODUCT.md) | What the product is and how to read its output — the four dashboard pages, what the sample does and does not exhibit, what the tool finds on the real corpus, and how to read the insight confidences. Start here to interpret results. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The layered design, the six extension seams, the project layout, and nine decision records with the measurements behind each. |
| [docs/EVALUATION.md](docs/EVALUATION.md) | The quantitative evaluation: threshold sensitivity, per-check precision and recall, and benchmarks. |
| [DESIGN.md](DESIGN.md) | The dashboard's design system — tokens, components and the rules behind them. |
| [PLAN.md](PLAN.md) | The original design record, with the measurements that drove each choice. |

---

## Quickstart

Python 3.12 and [`uv`](https://docs.astral.sh/uv/) are the only prerequisites.

```bash
uv sync                  # 1. create the venv from the lockfile
make fetch               # 2. download + checksum-verify the pinned 116 MB sample
make test                # 3. 993 tests, coverage gate enforced
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
| `make sensitivity` | Threshold sensitivity sweep → `docs/evaluation/sensitivity.md` (~5 min on the full corpus) |
| `make metrics` | Per-check precision/recall → `docs/evaluation/metrics.md`, plus timings in `benchmarks.md` |

---

## Tests

```bash
make test                                # everything, with the coverage gate
uv run pytest tests/unit -q              # fast, synthetic only
uv run pytest -m golden -q               # against committed real slices + vendor counts
uv run pytest -m integration -q          # FastAPI TestClient + Streamlit AppTest
make lint && make typecheck              # ruff (lint + format) and mypy --strict
```

**993 tests, 98.95% line coverage** (`--cov-fail-under=98`). Every non-dashboard module
is at **100%**; all 47 uncovered statements are Streamlit render paths that `AppTest` does
not reach. `ruff` and `mypy --strict` are clean.

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
