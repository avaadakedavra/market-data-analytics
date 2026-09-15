"""Parquet reader.

Parquet carries its own types, so unlike CSV there is nothing to infer and nothing to
coerce here — `scan_parquet` is genuinely lazy and normalisation casts whatever the
vendor declared onto `BAR_SCHEMA`. This is the path every real file takes.
"""

from __future__ import annotations

import io

import polars as pl

from mdq.ingest.readers import ReadSource, register_reader

__all__ = ["ParquetReader"]


@register_reader(".parquet", ".pq")
class ParquetReader:
    """Reads Apache Parquet, lazily."""

    def read(self, source: ReadSource) -> pl.LazyFrame:
        """Scan `source` without materialising it."""
        if isinstance(source, bytes):
            return pl.scan_parquet(io.BytesIO(source))
        return pl.scan_parquet(source)
