"""What the insights layer hands back: an `Insight` and the `SuggestedRule` inside it.

These two objects are the *product* of this tool, not an internal detail. A business
user reads `Insight.pattern` to understand what is wrong with their data and
`SuggestedRule.rationale` to understand what to do about it; the API returns both
verbatim and the dashboard renders the rule as a copyable block. So both are plain,
frozen, JSON-safe records with no Polars, no enums and no lazy anything inside them.

**JSON safety is enforced, not hoped for.** `params` and `evidence` are free-form
dictionaries built by rules out of whatever they measured — which means dates, Polars
values and tuples leak in easily. Both are normalised at construction (dates to ISO
strings, sequences to lists, everything unknown to `str`), so

```python
Insight.from_dict(json.loads(json.dumps(insight.to_dict()))) == insight
```

holds for every insight this package can produce. A round-trip that quietly changed a
date into a string would otherwise make the API and the dashboard disagree about what
the tool found.

`DatasetSummary` is the third object in PLAN §5's `derive` signature. The service layer
that will own it (WP6) does not exist yet, so a minimal version lives here: only the
facts a rule actually quotes — which contracts exist, under which roots, on which
exchanges, over what span. WP6 can adopt it or widen it; rules only read it through the
accessors below and never touch its fields directly.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Final, Literal

import polars as pl

from mdq.domain.frequency import Frequency
from mdq.domain.schema import BarFrame, C

__all__ = [
    "RULE_KINDS",
    "DatasetSummary",
    "Insight",
    "RuleKind",
    "SuggestedRule",
    "root_of",
]

#: A suggested rule either *removes or relabels* data (cleansing) or *refuses* it
#: (validation). Nothing else: a business user must be able to tell, at a glance,
#: whether adopting the rule changes what they see or changes what they accept.
RuleKind = Literal["cleansing", "validation"]

RULE_KINDS: Final[tuple[str, ...]] = ("cleansing", "validation")

#: Strip a CME-style month code and year: `ESH26` → `ES`, `SR3H26` → `SR3`. Mirrors
#: `mdq.ingest.normalise._root_expr`, which derives the `root` column at ingestion —
#: findings do not carry it, so the insights layer re-derives it from the contract code.
_EXPIRY_PATTERN: Final = r"(?i)[FGHJKMNQUVXZ]\d{1,2}$"


def root_of(contract: str | None) -> str | None:
    """`"CLG26"` → `"CL"`; `None` and unrecognisable codes pass through unchanged."""
    if contract is None:
        return None
    stripped = pl.Series([contract]).str.replace(_EXPIRY_PATTERN, "").item()
    return str(stripped) if stripped else contract


def _jsonable(value: Any) -> Any:
    """Recursively coerce a value into something `json.dumps` round-trips exactly.

    Dates and datetimes become ISO strings, mappings become `str`-keyed dicts, other
    sequences become lists, enums become their values, and anything else becomes its
    `str()`. Floats, ints, bools, strings and `None` pass through untouched.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Iterable):
        return [_jsonable(item) for item in value]
    return str(value)


def _jsonable_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _jsonable(item) for key, item in value.items()}


@dataclass(frozen=True)
class SuggestedRule:
    """A concrete, adoptable rule derived from an observed pattern.

    Attributes:
        rule_id: Stable identifier, shared with `Check.suggested_rule_id` so a finding
            and the rule that would govern it can be linked in the UI.
        kind: `"cleansing"` (change the data) or `"validation"` (refuse the data).
        params: The rule's configuration, as a business user would paste it into a
            config file. Free-form, but always JSON-safe.
        rationale: Why this rule, in the reader's language and with the numbers that
            justify it. This is read by traders and risk managers, not by us.
        confidence: `0..1`, always the share of the relevant findings this pattern
            accounts for. Never a constant — a low number is a real signal that the
            pattern explains only part of what was observed.
    """

    rule_id: str
    kind: RuleKind
    params: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if not self.rule_id:
            raise ValueError("SuggestedRule.rule_id must be a non-empty string")
        if self.kind not in RULE_KINDS:
            raise ValueError(f"SuggestedRule.kind must be one of {RULE_KINDS}, got {self.kind!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"SuggestedRule.confidence must lie in [0, 1], got {self.confidence!r}"
            )
        object.__setattr__(self, "params", _jsonable_dict(self.params))
        object.__setattr__(self, "confidence", round(float(self.confidence), 4))

    def to_dict(self) -> dict[str, Any]:
        """A plain, JSON-safe dict."""
        return {
            "rule_id": self.rule_id,
            "kind": self.kind,
            "params": dict(self.params),
            "rationale": self.rationale,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SuggestedRule:
        """Inverse of `to_dict`."""
        kind = data["kind"]
        if kind not in RULE_KINDS:
            raise ValueError(f"unknown rule kind {kind!r}; expected one of {RULE_KINDS}")
        return cls(
            rule_id=data["rule_id"],
            kind=kind,
            params=dict(data.get("params") or {}),
            rationale=data.get("rationale", ""),
            confidence=float(data.get("confidence", 0.0)),
        )


@dataclass(frozen=True)
class Insight:
    """One explained pattern in a dataset, and the rule it argues for.

    Attributes:
        id: The `PatternRule` that produced it.
        title: A short headline, e.g. "Feed incident on 4 dates hit whole CL family".
        pattern: The explanation, in prose, quantified. The main thing a user reads.
        evidence: The numbers behind the prose, so nothing has to be taken on trust.
            Always JSON-safe; always includes enough to reproduce the confidence.
        affected_contracts: Sorted contract codes the pattern touches (capped; the
            exact total is in `evidence["affected_contract_count"]`).
        finding_count: How many *findings* the insight was drawn from. The number of
            *bars* implicated is usually larger and is carried in the evidence as
            `bars_affected`, because one finding can cover a whole session.
        rule: The rule to adopt.
    """

    id: str
    title: str
    pattern: str
    evidence: dict[str, Any]
    affected_contracts: list[str]
    finding_count: int
    rule: SuggestedRule

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Insight.id must be a non-empty string")
        if self.finding_count < 0:
            raise ValueError(f"Insight.finding_count must be >= 0, got {self.finding_count}")
        object.__setattr__(self, "evidence", _jsonable_dict(self.evidence))
        object.__setattr__(self, "affected_contracts", [str(c) for c in self.affected_contracts])

    @property
    def confidence(self) -> float:
        """Shorthand for `rule.confidence`; the engine sorts on it."""
        return self.rule.confidence

    def to_dict(self) -> dict[str, Any]:
        """A plain, JSON-safe dict — what the API returns and the dashboard renders."""
        return {
            "id": self.id,
            "title": self.title,
            "pattern": self.pattern,
            "evidence": dict(self.evidence),
            "affected_contracts": list(self.affected_contracts),
            "finding_count": self.finding_count,
            "rule": self.rule.to_dict(),
        }

    def to_json(self, *, indent: int | None = None) -> str:
        """`to_dict` serialised. No `default=` fallback: the dict is already safe."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Insight:
        """Inverse of `to_dict`."""
        return cls(
            id=data["id"],
            title=data["title"],
            pattern=data["pattern"],
            evidence=dict(data.get("evidence") or {}),
            affected_contracts=list(data.get("affected_contracts") or []),
            finding_count=int(data["finding_count"]),
            rule=SuggestedRule.from_dict(data["rule"]),
        )

    @classmethod
    def from_json(cls, text: str) -> Insight:
        """Inverse of `to_json`."""
        return cls.from_dict(json.loads(text))


@dataclass(frozen=True)
class DatasetSummary:
    """The few dataset-level facts a pattern rule quotes.

    Kept deliberately small. A rule asks it questions like "how many CL contracts are in
    this dataset?" so it can say *four of the five CL contracts* rather than the much
    weaker *four contracts*. Everything else it needs is in the report.
    """

    contracts: tuple[str, ...] = ()
    exchanges: tuple[str, ...] = ()
    frequency: Frequency | None = None
    row_count: int = 0
    first_session: date | None = None
    last_session: date | None = None

    @classmethod
    def from_bars(cls, bars: BarFrame) -> DatasetSummary:
        """Derive the summary from a `BarFrame` in a single pass.

        The two distinct-value lists are **imploded** into one row apiece rather than
        selected side by side: a 40-contract, 6-exchange dataset is exactly the shape
        this object exists to describe, and columns of unequal length cannot share a
        frame.
        """
        stats = bars.lf.select(
            pl.col(C.CONTRACT).drop_nulls().unique().sort().implode().alias(C.CONTRACT),
            pl.col(C.EXCHANGE).drop_nulls().unique().sort().implode().alias(C.EXCHANGE),
            pl.len().alias("rows"),
            pl.col(C.SESSION_DATE).min().alias("first"),
            pl.col(C.SESSION_DATE).max().alias("last"),
        ).collect()
        rows = int(stats.get_column("rows")[0])
        if rows == 0:
            return cls(frequency=bars.frequency)
        return cls(
            contracts=tuple(str(c) for c in stats.get_column(C.CONTRACT)[0].to_list()),
            exchanges=tuple(str(e) for e in stats.get_column(C.EXCHANGE)[0].to_list()),
            frequency=bars.frequency,
            row_count=rows,
            first_session=stats.get_column("first")[0],
            last_session=stats.get_column("last")[0],
        )

    def contracts_for_root(self, root: str | None) -> tuple[str, ...]:
        """Every contract in the dataset sharing `root` — the denominator for "4 of 5"."""
        if root is None:
            return ()
        wanted = root.upper()
        return tuple(c for c in self.contracts if (root_of(c) or "").upper() == wanted)

    def sole_exchange(self) -> str | None:
        """The exchange, when the dataset has exactly one; `None` when it is mixed.

        Rules that suggest an exchange-scoped rule (a session calendar, a break window)
        may only name an exchange when there is no ambiguity about which one.
        """
        return self.exchanges[0] if len(self.exchanges) == 1 else None

    def to_dict(self) -> dict[str, Any]:
        """A plain, JSON-safe dict."""
        return _jsonable_dict(
            {
                "contracts": list(self.contracts),
                "exchanges": list(self.exchanges),
                "frequency": self.frequency.value if self.frequency else None,
                "row_count": self.row_count,
                "first_session": self.first_session,
                "last_session": self.last_session,
            }
        )
