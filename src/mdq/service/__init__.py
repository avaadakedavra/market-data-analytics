"""The application layer: one facade over ingestion, analytics, quality and insights.

```python
service = MarketDataService()
service.autoload()                       # MDQ_DATA_DIR, columns only — no rows read
service.quality_summary()                # daily by default; computed once, then cached
service.run_analytic("rolling_vwap", {"window": "15m"}, contracts=["ESH26"])
```

`MarketDataService` is the only thing `mdq.api` and `mdq.dashboard` import, so the two
presentation layers cannot drift apart. `DatasetStore` underneath it is what makes the
platform usable on 5.3M rows: nothing is read until a request needs it, and nothing
derived is computed twice.
"""

from mdq.service.service import (
    AUTOLOAD_ID,
    CheckSummary,
    ContractInfo,
    FindingFilter,
    FrequencySummary,
    LoadReport,
    MarketDataService,
    QualitySummary,
    ServiceSummary,
    SkippedFile,
    UnknownContractError,
    UploadReport,
    UploadTooLargeError,
    View,
    check_title,
)
from mdq.service.store import (
    COMBINED_ID,
    Dataset,
    DatasetStore,
    Source,
    UnknownDatasetError,
    scan_file,
)

__all__ = [
    "AUTOLOAD_ID",
    "COMBINED_ID",
    "CheckSummary",
    "ContractInfo",
    "Dataset",
    "DatasetStore",
    "FindingFilter",
    "FrequencySummary",
    "LoadReport",
    "MarketDataService",
    "QualitySummary",
    "ServiceSummary",
    "SkippedFile",
    "Source",
    "UnknownContractError",
    "UnknownDatasetError",
    "UploadReport",
    "UploadTooLargeError",
    "View",
    "check_title",
    "scan_file",
]
