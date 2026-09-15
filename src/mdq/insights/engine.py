"""The insight engine: a registry of pattern rules, and the runner that isolates them.

The quality engine answers *what is wrong*. This layer answers *why, and what should we
do about it* — and that second question is the one a business user cannot answer from a
findings table. Forty-three incoherent bars in a spreadsheet look like forty-three
problems; the insight that thirty-nine of them are one settlement convention and that
twenty-eight of them landed on six dates across the whole crude-oil family is the
difference between a data-entry ticket and a call to the vendor.

**Why rules and not a model.** Deterministic, unit-testable, explainable to a model-risk
reviewer, needs no credentials, and the patterns in this data are structural enough that
rules capture them completely. `InsightEngine` is a protocol precisely so an
`LlmInsightEngine` can be swapped in later: it would receive `report.summary()` plus
evidence samples and return the same `Insight` objects. Nothing upstream changes.

**Shape.** Adding an insight is "add a file, decorate the class, done", exactly as in
`mdq.quality.registry` and `mdq.analytics.registry`:

```python
@register_rule
class MyPattern:
    id = "my_pattern"
    title = "..."
    def applies(self, ctx: InsightContext) -> bool: ...
    def build(self, ctx: InsightContext) -> Insight: ...
```

`load_rules()` imports every module in `mdq.insights.rules` with `pkgutil`, so there is
no import list to forget.

**Isolation.** `derive` treats every rule as untrusted, mirroring `run_checks`: a rule
that raises — in `applies` or in `build` — is recorded as a `RuleFailure` and the other
nine still produce their insights. A single broken rule must never cost the user the
rest of the analysis. Failures are logged and returned by `derive_with_failures`; they
are deliberately *not* dressed up as insights, because a business user reading the
Insights page should never see our stack traces there.

**Deviations from PLAN §5**, both because WP5 lands before the service layer:

* `applies`/`build` take an `InsightContext` rather than a bare `QualityReport`. Rules
  need the activity profile (to say a session was dormant) and the dataset summary (to
  say *four of the five* CL contracts), and one context object keeps every rule's
  signature identical as that set grows. `ctx.report` is the report.
* `derive` takes the `ActivityProfile` object rather than its bare `LazyFrame`, and both
  it and `summary` are optional — the report already carries its own profile, and a
  report is meant to be interpretable on its own.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import traceback
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar, Final, Protocol, runtime_checkable

import polars as pl

from mdq.domain.findings import Severity
from mdq.domain.schema import C
from mdq.insights.models import DatasetSummary, Insight
from mdq.quality.context import ActivityProfile
from mdq.quality.report import QualityReport

__all__ = [
    "MAX_LISTED",
    "ROOT",
    "InsightContext",
    "InsightEngine",
    "InsightResult",
    "PatternRule",
    "RuleBasedInsightEngine",
    "RuleFailure",
    "RuleRegistry",
    "UnknownRuleError",
    "as_iso_dates",
    "count_rows",
    "decode_evidence",
    "distinct",
    "evidence_share",
    "load_rules",
    "local_hhmm",
    "register_rule",
]

logger = logging.getLogger(__name__)

#: The derived column `InsightContext.findings` adds: the product root of `contract`.
ROOT: Final = "root"

#: Ceiling on any list an insight quotes (contracts, dates). The untruncated total is
#: always carried alongside it, so a capped list never becomes a wrong number.
MAX_LISTED: Final = 100

#: Package scanned by `load_rules()`.
_RULES_PACKAGE: Final = "mdq.insights.rules"

#: Wall-clock minutes in a day, for `local_hhmm`.
_MINUTES_IN_DAY: Final = 24 * 60

_SEVERITY_RANK: Final[dict[str, int]] = {s.label: int(s) for s in Severity}


def _root_expr() -> pl.Expr:
    """The product root of `contract`, as an expression.

    The same transformation `mdq.ingest.normalise` applies to build the `root` column:
    strip a trailing month code and year (`CLG26` → `CL`, `SR3H26` → `SR3`), and fall
    back to the contract itself when that would leave nothing.
    """
    derived = pl.col(C.CONTRACT).str.replace(r"(?i)[FGHJKMNQUVXZ]\d{1,2}$", "")
    return (
        pl.when(derived.str.len_chars() > 0).then(derived).otherwise(pl.col(C.CONTRACT)).alias(ROOT)
    )


class UnknownRuleError(KeyError):
    """Raised when a rule id is requested that was never registered."""


# --------------------------------------------------------------------------- #
# helpers every rule shares
# --------------------------------------------------------------------------- #


def evidence_share(explained: float, relevant: float) -> float:
    """`explained / relevant`, clamped to `[0, 1]`; `0.0` when nothing is relevant.

    This is the **only** way a rule is allowed to produce a confidence. Confidence in
    this tool always means one thing — the share of the relevant findings that the
    pattern accounts for — so a reader can compare two insights' confidences and have
    the comparison mean something.
    """
    if relevant <= 0:
        return 0.0
    return max(0.0, min(1.0, float(explained) / float(relevant)))


def decode_evidence(frame: pl.DataFrame, fields: Mapping[str, pl.DataType]) -> pl.DataFrame:
    """Append named `evidence` JSON fields as typed columns.

    Evidence is stored JSON-encoded in one string column, so a rule declares only the
    keys it cares about and the rest is never parsed. A key absent from a finding's
    evidence decodes to null rather than raising, which is what lets one rule read
    findings produced by checks that carry different evidence.
    """
    if not fields:
        return frame
    dtype = pl.Struct(dict(fields))
    decoded = pl.col(C.EVIDENCE).str.json_decode(dtype=dtype)
    return frame.with_columns(decoded.struct.field(name).alias(name) for name in fields)


def count_rows(frame: pl.DataFrame) -> int:
    """How many *bars* a set of findings implicates — the sum of their `count`s.

    Never `frame.height`: one finding can cover an entire session, so counting findings
    would under-report what the user actually has to deal with.
    """
    if frame.height == 0:
        return 0
    return int(frame.get_column(C.COUNT).sum() or 0)


def distinct(frame: pl.DataFrame, column: str) -> list[Any]:
    """Sorted distinct non-null values of `column`."""
    if frame.height == 0:
        return []
    series = frame.get_column(column).drop_nulls().unique().sort()
    return list(series.to_list())


def _capped(values: Sequence[Any]) -> list[Any]:
    return list(values[:MAX_LISTED])


def as_iso_dates(values: Iterable[date | None]) -> list[str]:
    """Dates as ISO strings, for evidence that a human will read in a JSON block."""
    return [d.isoformat() for d in values if d is not None]


def local_hhmm(minute_of_day: int) -> str:
    """`1020` → `"17:00"` — a minute-of-day as the wall clock a trader reads.

    Both gap rules report clock times, and both report them in **local** (Chicago) time,
    because that is the frame in which an exchange's session is defined. Minutes outside
    a single day wrap, so a window that runs past midnight still names a real time.
    """
    minute = int(minute_of_day) % _MINUTES_IN_DAY
    return f"{minute // 60:02d}:{minute % 60:02d}"


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class InsightContext:
    """Everything a pattern rule may consult.

    Attributes:
        report: The findings, with the thresholds and activity profile that produced
            them. The primary input.
        activity: The regime classification. Defaults to `report.activity`; supply it
            explicitly only when the report was built elsewhere.
        summary: Dataset-level facts (which contracts exist, over what span). Optional:
            every rule degrades to a weaker but still true statement without it.
        findings: `report.findings` plus a derived `root` column. Findings carry the
            contract but not its product root, and the headline insight is about whole
            product families, so the root is derived once here rather than in six rules.
    """

    report: QualityReport
    activity: ActivityProfile | None = None
    summary: DatasetSummary | None = None
    findings: pl.DataFrame = field(init=False)

    def __post_init__(self) -> None:
        if self.activity is None:
            object.__setattr__(self, "activity", self.report.activity)
        object.__setattr__(self, "findings", self.report.findings.with_columns(_root_expr()))

    # --- narrowing --------------------------------------------------------- #

    def subset(
        self,
        check_id: str | Iterable[str],
        *,
        min_severity: Severity | None = None,
    ) -> pl.DataFrame:
        """The findings of one or more checks, optionally at or above a severity."""
        wanted = [check_id] if isinstance(check_id, str) else list(check_id)
        frame = self.findings.filter(pl.col(C.CHECK_ID).is_in(wanted))
        if min_severity is not None and frame.height:
            frame = frame.filter(_severity_rank() >= int(min_severity))
        return frame

    def has(self, check_id: str | Iterable[str]) -> bool:
        """True when any finding of those checks exists."""
        return self.subset(check_id).height > 0

    # --- the two numbers every insight quotes ------------------------------ #

    def contracts_in(self, frame: pl.DataFrame) -> list[str]:
        """Sorted contract codes appearing in `frame`, capped at `MAX_LISTED`."""
        return _capped([str(c) for c in distinct(frame, C.CONTRACT)])

    def contract_total(self, frame: pl.DataFrame) -> int:
        """The *uncapped* number of distinct contracts in `frame`."""
        return len(distinct(frame, C.CONTRACT))

    def roots_in(self, frame: pl.DataFrame) -> list[str]:
        """Sorted product roots appearing in `frame`."""
        return [str(r) for r in distinct(frame, ROOT)]

    def family_size(self, root: str) -> int | None:
        """How many contracts of `root` the dataset holds, when the summary says so.

        `None` means "unknown", and a rule must then say *4 contracts* rather than
        *4 of the 5* — an insight never invents a denominator.
        """
        if self.summary is None:
            return None
        size = len(self.summary.contracts_for_root(root))
        return size or None

    @property
    def exchange(self) -> str | None:
        """The dataset's exchange when unambiguous, else `None`."""
        return self.summary.sole_exchange() if self.summary is not None else None


def _severity_rank() -> pl.Expr:
    return pl.col(C.SEVERITY).replace_strict(_SEVERITY_RANK, default=0, return_dtype=pl.Int32)


# --------------------------------------------------------------------------- #
# the rule protocol and registry
# --------------------------------------------------------------------------- #


@runtime_checkable
class PatternRule(Protocol):
    """One named pattern the engine knows how to recognise and explain.

    Implementations are stateless — the registry instantiates one per class and reuses
    it — and split into two halves on purpose: `applies` decides cheaply whether the
    pattern is present at all, `build` does the work of explaining it. That split is
    what keeps a dataset with nothing interesting in it fast, and what makes the
    "does it fire?" half of every rule's test a single call.
    """

    id: ClassVar[str]
    title: ClassVar[str]

    def applies(self, ctx: InsightContext) -> bool:
        """True when this pattern is present in `ctx` and worth reporting."""
        ...

    def build(self, ctx: InsightContext) -> Insight:
        """Explain the pattern. Only called when `applies` returned True."""
        ...


_REGISTRY: dict[str, PatternRule] = {}
_LOADED = False


def register_rule[RuleClass: type[PatternRule]](cls: RuleClass) -> RuleClass:
    """Class decorator adding a pattern rule to the registry. Duplicate ids are an error."""
    instance = cls()
    rule_id = instance.id
    existing = _REGISTRY.get(rule_id)
    if existing is not None and type(existing) is not cls:
        raise ValueError(
            f"duplicate rule id {rule_id!r} ({type(existing).__name__} vs {cls.__name__})"
        )
    _REGISTRY[rule_id] = instance
    return cls


def load_rules() -> None:
    """Import every module under `mdq.insights.rules`, populating the registry."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    package = importlib.import_module(_RULES_PACKAGE)
    for info in pkgutil.iter_modules(package.__path__, f"{_RULES_PACKAGE}."):
        importlib.import_module(info.name)


class RuleRegistry:
    """Lookup over every registered pattern rule. Auto-loads on first use."""

    @staticmethod
    def all() -> tuple[PatternRule, ...]:
        """Every registered rule, ordered by id."""
        load_rules()
        return tuple(_REGISTRY[key] for key in sorted(_REGISTRY))

    @staticmethod
    def ids() -> tuple[str, ...]:
        """Sorted ids of every registered rule."""
        load_rules()
        return tuple(sorted(_REGISTRY))

    @staticmethod
    def get(rule_id: str) -> PatternRule:
        """One rule by id."""
        load_rules()
        try:
            return _REGISTRY[rule_id]
        except KeyError:
            raise UnknownRuleError(
                f"unknown insight rule {rule_id!r}; registered: {sorted(_REGISTRY)}"
            ) from None


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RuleFailure:
    """A rule that raised. Reported to operators, never shown as an insight."""

    rule_id: str
    error_type: str
    error: str
    traceback: str = ""

    def to_dict(self) -> dict[str, Any]:
        """A plain dict, for logs and the health endpoint."""
        return {
            "rule_id": self.rule_id,
            "error_type": self.error_type,
            "error": self.error,
            "traceback": self.traceback,
        }


@dataclass(frozen=True)
class InsightResult:
    """What `derive_with_failures` returns: the insights, and what went wrong."""

    insights: list[Insight]
    failures: list[RuleFailure] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.insights)

    def to_dicts(self) -> list[dict[str, Any]]:
        """Every insight as a plain dict."""
        return [insight.to_dict() for insight in self.insights]


class InsightEngine(Protocol):
    """Turns a `QualityReport` into explained patterns and suggested rules.

    The seam for an `LlmInsightEngine`: same inputs, same `Insight` output, swapped by
    configuration. Not built — documented as the extension point (PLAN §5).
    """

    def derive(
        self,
        report: QualityReport,
        activity: ActivityProfile | None = None,
        summary: DatasetSummary | None = None,
    ) -> list[Insight]:
        """Every insight this engine can find in `report`. Never raises on bad data."""
        ...


@dataclass(frozen=True)
class RuleBasedInsightEngine:
    """The shipped engine: run every registered `PatternRule`, isolate the failures.

    Attributes:
        rules: The rules to run. `None` means "every registered rule", which is what
            the application uses; a test passes an explicit tuple to isolate one.
    """

    rules: tuple[PatternRule, ...] | None = None

    def derive(
        self,
        report: QualityReport,
        activity: ActivityProfile | None = None,
        summary: DatasetSummary | None = None,
    ) -> list[Insight]:
        """Insights for `report`, strongest evidence first.

        An empty report yields an empty list — never an exception — because "we found
        nothing to explain" is a perfectly ordinary answer and every caller (the API,
        the dashboard, a test) relies on it.
        """
        return self.derive_with_failures(report, activity, summary).insights

    def derive_with_failures(
        self,
        report: QualityReport,
        activity: ActivityProfile | None = None,
        summary: DatasetSummary | None = None,
    ) -> InsightResult:
        """As `derive`, plus the rules that raised — for operators, not for users."""
        ctx = InsightContext(report=report, activity=activity, summary=summary)
        insights: list[Insight] = []
        failures: list[RuleFailure] = []
        for rule in self.rules if self.rules is not None else RuleRegistry.all():
            outcome = _run_rule(rule, ctx)
            if isinstance(outcome, RuleFailure):
                failures.append(outcome)
            elif outcome is not None:
                insights.append(outcome)
        # Strongest evidence first, then by id so the order is stable between runs.
        insights.sort(key=lambda i: (-i.confidence, i.id))
        return InsightResult(insights=insights, failures=failures)


def _run_rule(rule: PatternRule, ctx: InsightContext) -> Insight | RuleFailure | None:
    """Apply one rule, converting any explosion into a `RuleFailure`."""
    rule_id = getattr(rule, "id", type(rule).__name__)
    try:
        if not rule.applies(ctx):
            return None
        # Deliberately `object`: the protocol promises an `Insight`, and this function
        # exists precisely because a rule is untrusted and may not keep that promise.
        insight: object = rule.build(ctx)
    except Exception as exc:
        logger.warning("insight rule %r failed: %s: %s", rule_id, type(exc).__name__, exc)
        return RuleFailure(
            rule_id=str(rule_id),
            error_type=type(exc).__name__,
            error=str(exc),
            traceback="".join(traceback.format_exception(exc))[-2_000:],
        )
    if not isinstance(insight, Insight):
        logger.warning("insight rule %r returned %s, expected Insight", rule_id, type(insight))
        return RuleFailure(
            rule_id=str(rule_id),
            error_type="TypeError",
            error=f"rule {rule_id!r} returned {type(insight).__name__}, expected Insight",
        )
    return insight
