"""The insights layer: why the data is wrong, and what to do about it.

The quality engine answers *what* is wrong. This layer answers *why, and what should we
do about it* — the question a business user cannot answer from a findings table.

```python
engine = RuleBasedInsightEngine()
insights = engine.derive(report, activity, DatasetSummary.from_bars(bars))
```

`Insight.pattern` explains the pattern in prose with the numbers behind it, and
`Insight.rule` is a concrete, adoptable `SuggestedRule`. Both are JSON-safe by
construction, so the API returns them verbatim and the dashboard renders the rule as a
copyable block.

Adding an insight is "add a file, decorate, done" — see `mdq.insights.engine`. Swapping
the whole engine for an LLM-backed one means implementing `InsightEngine`; nothing
upstream changes.
"""

from mdq.insights.engine import (
    InsightContext,
    InsightEngine,
    InsightResult,
    PatternRule,
    RuleBasedInsightEngine,
    RuleFailure,
    RuleRegistry,
    UnknownRuleError,
    load_rules,
    register_rule,
)
from mdq.insights.models import DatasetSummary, Insight, RuleKind, SuggestedRule

__all__ = [
    "DatasetSummary",
    "Insight",
    "InsightContext",
    "InsightEngine",
    "InsightResult",
    "PatternRule",
    "RuleBasedInsightEngine",
    "RuleFailure",
    "RuleKind",
    "RuleRegistry",
    "SuggestedRule",
    "UnknownRuleError",
    "load_rules",
    "register_rule",
]
