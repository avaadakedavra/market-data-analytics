"""Ingestion: files in, one canonical `BarFrame` out, with a reason for every drop.

Three seams, each of which is "add a file, register, done":

* a **reader** per file format (`mdq.ingest.readers`),
* a **profile** per source column layout (`mdq.ingest.profiles`) — a data declaration,
* **normalisation** (`mdq.ingest.normalise`), which is format- and vendor-agnostic.

The public entry points are `ingest_file` (bars *and* rejects *and* statistics) and
`read_bars` (just the bars, for callers that have already handled the report).
"""

from __future__ import annotations

from mdq.ingest.errors import (
    IngestError,
    MixedFrequencyError,
    TooManyMalformedLinesError,
    UnsupportedFormatError,
)
from mdq.ingest.normalise import (
    IngestOptions,
    IngestResult,
    ingest_file,
    normalise,
    read_bars,
    read_buffer,
)
from mdq.ingest.profiles import (
    GENERIC,
    HF_DAILY,
    HF_MINUTE,
    PROFILES,
    SourceProfile,
    TimestampKind,
    detect_profile,
)
from mdq.ingest.readers import READERS, Reader, register_reader, supported_extensions
from mdq.ingest.report import REJECT_SCHEMA, IngestStats

__all__ = [
    "GENERIC",
    "HF_DAILY",
    "HF_MINUTE",
    "PROFILES",
    "READERS",
    "REJECT_SCHEMA",
    "IngestError",
    "IngestOptions",
    "IngestResult",
    "IngestStats",
    "MixedFrequencyError",
    "Reader",
    "SourceProfile",
    "TimestampKind",
    "TooManyMalformedLinesError",
    "UnsupportedFormatError",
    "detect_profile",
    "ingest_file",
    "normalise",
    "read_bars",
    "read_buffer",
    "register_reader",
    "supported_extensions",
]
