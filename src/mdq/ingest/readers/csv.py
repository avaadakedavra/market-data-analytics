"""CSV reader — everything arrives as a string, on purpose.

`pl.scan_csv(infer_schema=False, ignore_errors=False, truncate_ragged_lines=False)` is
three deliberate decisions:

* **`infer_schema=False`** — every value arrives as a `String`. Coercion happens once,
  in normalisation, with `strict=False`, so a bad cell becomes a null that is
  attributable to a *row* and a *reason*. If polars inferred the schema instead, the
  same bad cell would either blow up the whole read or silently become a null that
  nobody can explain.
* **`ignore_errors=False`** — we never want polars to quietly drop a value for us.
* **`truncate_ragged_lines=False`** — a line with the wrong number of fields is a
  finding, not something to trim into shape.

A ragged line makes polars raise, and a *short* line makes it pad silently with nulls;
neither is acceptable, so both route to a line-by-line fallback that re-parses the file
with the `csv` module and marks each offending line in `MALFORMED_LINE_COLUMN`. The
short-line case is detected cheaply: padding always leaves the last column null, so the
fallback runs only when that column actually contains nulls.

The file is materialised rather than left lazy. CSV is the upload path, bounded by
`AppSettings.max_upload_bytes`, and the alternative is a parse error surfacing from
somewhere deep inside a query plan with no line number attached.
"""

from __future__ import annotations

import csv as _csv
import io
from collections.abc import Sequence
from pathlib import Path

import polars as pl

from mdq.ingest.readers import MALFORMED_LINE_COLUMN, ReadSource, register_reader

__all__ = ["CsvReader"]


@register_reader(".csv", ".txt")
class CsvReader:
    """Reads delimited text as all-string columns."""

    def read(self, source: ReadSource) -> pl.LazyFrame:
        """Return every field of `source` as a `String`, malformed lines marked."""
        data = _as_bytes(source)
        try:
            frame = pl.scan_csv(
                io.BytesIO(data),
                infer_schema=False,
                ignore_errors=False,
                truncate_ragged_lines=False,
            ).collect()
        except pl.exceptions.NoDataError:
            # A zero-byte file is empty, not broken: no columns, no rows, no rejects.
            return pl.LazyFrame()
        except pl.exceptions.PolarsError:
            # A line with *more* fields than the header: polars refuses, we attribute.
            return _parse_line_by_line(data).lazy()
        if _may_have_short_lines(frame):
            return _parse_line_by_line(data).lazy()
        return frame.lazy()


def _as_bytes(source: ReadSource) -> bytes:
    """Read `source` into memory once, so the fallback can re-read the same bytes."""
    if isinstance(source, bytes):
        return source
    if isinstance(source, str | Path):
        return Path(source).read_bytes()
    payload = source.read()
    return payload if isinstance(payload, bytes) else bytes(payload)


def _may_have_short_lines(frame: pl.DataFrame) -> bool:
    """True when null padding *could* have hidden a line with too few fields.

    Padding a short line always leaves the final column null, so a null-free last
    column proves no line was short. The converse is not true — a legitimately empty
    final field looks the same — which costs one extra parse and no correctness.
    """
    if frame.height == 0 or frame.width == 0:
        return False
    return frame.get_column(frame.columns[-1]).null_count() > 0


def _unique(names: Sequence[str]) -> list[str]:
    """Disambiguate repeated header names the way polars does."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        count = seen.get(name, 0)
        seen[name] = count + 1
        out.append(name if count == 0 else f"{name}_duplicated_{count - 1}")
    return out


def _parse_line_by_line(data: bytes) -> pl.DataFrame:
    """Re-parse `data` one line at a time, marking lines that do not fit the header.

    Every line keeps its position, so `row_id` still points at the line a finding came
    from. Blank lines are skipped, matching polars. Because this path only runs on a
    file that is already malformed, it accepts one simplification the fast path does
    not need: a quoted field may not contain a newline.
    """
    text = data.decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:  # pragma: no cover - polars answers NoDataError before we get here
        return pl.DataFrame()

    header = _unique(_split(lines[0]) or [""])
    width = len(header)
    columns: dict[str, list[str | None]] = {name: [] for name in header}
    malformed: list[str | None] = []

    for line in lines[1:]:
        fields = _split(line)
        if len(fields) == width:
            for name, value in zip(header, fields, strict=True):
                columns[name].append(value or None)
            malformed.append(None)
        else:
            for name in header:
                columns[name].append(None)
            malformed.append(line)

    schema: dict[str, pl.DataType] = dict.fromkeys(header, pl.String())
    schema[MALFORMED_LINE_COLUMN] = pl.String()
    return pl.DataFrame({**columns, MALFORMED_LINE_COLUMN: malformed}, schema=schema)


def _split(line: str) -> list[str]:
    """One CSV line's fields, quoting respected; `[]` if it cannot be parsed at all."""
    try:
        return next(iter(_csv.reader([line])))
    except (_csv.Error, StopIteration):  # pragma: no cover - defensive
        return []
