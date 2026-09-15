"""Building the input to a pattern rule: a real `QualityReport`, not a hand-typed frame.

A rule reads findings, and findings are JSON-encoded evidence produced by a check. Writing
those by hand would test the rule against a frame *we* invented, and would keep passing
after a check changed the evidence key the rule depends on — which is precisely the
regression these tests exist to catch. So the default path here is the real one: build
clean bars with `support.synth`, break them with `support.faults`, run the actual quality
engine, and hand the resulting report to the rule.

`report_of` is the deliberate exception. One shipped rule consumes
`cross_frequency_mismatch`, a check PLAN §4.3 defers to WP8 and which does not exist, so
its findings have to be written out. That is stated rather than hidden, and the rule's own
module docstring declares the evidence contract those findings honour.
"""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl

from mdq.domain.config import QualityConfig
from mdq.domain.findings import Finding
from mdq.domain.frequency import Frequency
from mdq.domain.schema import FINDING_SCHEMA
from mdq.insights import (
    DatasetSummary,
    Insight,
    InsightContext,
    PatternRule,
    RuleBasedInsightEngine,
    RuleRegistry,
)
from mdq.quality import QualityReport
from support import scenarios

__all__ = [
    "context",
    "context_for",
    "fired",
    "insight_from",
    "report_of",
]


def context(
    frame: pl.DataFrame,
    frequency: Frequency = Frequency.MINUTE,
    *,
    rejects: pl.DataFrame | None = None,
    config: QualityConfig | None = None,
    with_summary: bool = True,
) -> InsightContext:
    """Run the real quality engine over `frame` and wrap the result for a rule.

    Args:
        frame: Bars in `BAR_SCHEMA`, usually `synth` output with a fault injected.
        frequency: Frequency to check at.
        rejects: Ingest rejects, for the rules that read `malformed_record`.
        config: Threshold overrides.
        with_summary: `False` omits the `DatasetSummary`, which is how a rule's
            "degrade without a summary" behaviour gets exercised.
    """
    bars, ctx, report = scenarios.checked(frame, frequency, rejects=rejects, config=config)
    summary = DatasetSummary.from_bars(bars) if with_summary else None
    return InsightContext(report=report, activity=ctx.activity, summary=summary)


def context_for(report: QualityReport, summary: DatasetSummary | None = None) -> InsightContext:
    """Wrap an already-built report, for the findings no check produces yet."""
    return InsightContext(report=report, summary=summary)


def report_of(
    findings: Sequence[Finding],
    frequency: Frequency = Frequency.DAILY,
    checks_run: Sequence[str] = (),
) -> QualityReport:
    """A `QualityReport` over hand-written findings. See the module docstring."""
    frame = (
        pl.DataFrame([f.to_row() for f in findings], schema=FINDING_SCHEMA)
        if findings
        else pl.DataFrame(schema=FINDING_SCHEMA)
    )
    run = tuple(checks_run) or tuple(dict.fromkeys(f.check_id for f in findings))
    return QualityReport(frame, frequency, checks_run=run)


def insight_from(rule_id: str, ctx: InsightContext) -> Insight | None:
    """Run one registered rule through the engine; `None` when it does not apply.

    Deliberately routed through `RuleBasedInsightEngine` rather than calling `applies`
    and `build` directly: a rule that raises would then be swallowed into a `RuleFailure`
    and the test would see "did not fire" instead of the bug. `fired` asserts on that.
    """
    return fired(RuleRegistry.get(rule_id), ctx)


def fired(rule: PatternRule, ctx: InsightContext) -> Insight | None:
    """As `insight_from`, for a rule instance. Fails loudly if the rule raised."""
    result = RuleBasedInsightEngine(rules=(rule,)).derive_with_failures(
        ctx.report, ctx.activity, ctx.summary
    )
    assert not result.failures, f"rule raised: {[f.to_dict() for f in result.failures]}"
    return result.insights[0] if result.insights else None
