"""The quality engine: activity regimes, a registry of checks, a report and cleansing.

Public entry points:

```python
ctx = CheckContext.build(bars, config)      # derives the activity profile
report = run_checks(bars, ctx)              # every applicable check, isolated
clean, log = cleanse(bars, report)          # what analytics runs on by default
```

Adding a check is "add a file, decorate, done" — see `mdq.quality.registry`.
"""

from mdq.quality.cleanse import DEFAULT_POLICY, CleanseLog, CleansePolicy, cleanse
from mdq.quality.context import (
    ACTIVITY_SCHEMA,
    A,
    ActivityProfile,
    Regime,
    activity_profile,
)
from mdq.quality.emit import (
    MAX_EVIDENCE_ROW_IDS,
    SEVERITY_COL,
    aggregate_by_session,
    findings_from_rows,
    no_findings,
    regime_severity,
)
from mdq.quality.registry import (
    CHECK_FAILED_ID,
    Check,
    CheckContext,
    CheckRegistry,
    UnknownCheckError,
    load_checks,
    register_check,
    run_checks,
)
from mdq.quality.report import QualityReport

__all__ = [
    "ACTIVITY_SCHEMA",
    "CHECK_FAILED_ID",
    "DEFAULT_POLICY",
    "MAX_EVIDENCE_ROW_IDS",
    "SEVERITY_COL",
    "A",
    "ActivityProfile",
    "Check",
    "CheckContext",
    "CheckRegistry",
    "CleanseLog",
    "CleansePolicy",
    "QualityReport",
    "Regime",
    "UnknownCheckError",
    "activity_profile",
    "aggregate_by_session",
    "cleanse",
    "findings_from_rows",
    "load_checks",
    "no_findings",
    "regime_severity",
    "register_check",
    "run_checks",
]
