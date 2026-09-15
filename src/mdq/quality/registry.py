"""The check protocol, the registry, and the runner.

Adding a quality check is "add a file, decorate the class, done": drop a module in
`mdq/quality/checks/`, decorate the class with `@register_check`, and the report, the
API and the dashboard pick it up with no edit anywhere else. `load_checks()` imports
every module in that package with `pkgutil`, so there is no import list to forget.

`run_checks` treats each check as untrusted: a check that raises — or that returns a
frame which is not `FINDING_SCHEMA` — is turned into an ERROR-severity `check_failed`
finding and the rest of the report is still produced. A single broken check must never
cost the user the other thirteen.
"""

from __future__ import annotations

import importlib
import pkgutil
import traceback
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import ClassVar, Protocol, runtime_checkable

import polars as pl

from mdq.domain.config import QualityConfig
from mdq.domain.findings import Finding, Severity
from mdq.domain.frequency import Frequency
from mdq.domain.schema import FINDING_SCHEMA, BarFrame, empty_finding_df
from mdq.quality.context import ActivityProfile, activity_profile
from mdq.quality.report import QualityReport
from mdq.time.sessions import DEFAULT_SESSIONS, SessionProfile

__all__ = [
    "CHECK_FAILED_ID",
    "Check",
    "CheckContext",
    "CheckRegistry",
    "UnknownCheckError",
    "load_checks",
    "register_check",
    "run_checks",
]

#: The id under which `run_checks` reports a check that blew up.
CHECK_FAILED_ID = "check_failed"

#: Package scanned by `load_checks()`.
_CHECKS_PACKAGE = "mdq.quality.checks"


class UnknownCheckError(KeyError):
    """Raised when a check id is requested that was never registered."""


@runtime_checkable
class Check(Protocol):
    """A single, pure, vectorised quality rule.

    Implementations are stateless: the registry instantiates one per class and reuses
    it. `run` receives the raw bar frame and must return a `FINDING_SCHEMA` frame built
    with Polars expressions — never a per-row Python loop.
    """

    id: ClassVar[str]
    title: ClassVar[str]
    frequencies: ClassVar[frozenset[Frequency]]
    default_severity: ClassVar[Severity]
    suggested_rule_id: ClassVar[str | None]

    def run(self, bars: pl.LazyFrame, ctx: CheckContext) -> pl.DataFrame:
        """Findings for `bars`, in `FINDING_SCHEMA`."""
        ...


@dataclass(frozen=True)
class CheckContext:
    """Everything a check may consult besides the bars themselves.

    Attributes:
        frequency: Frequency of the frame being checked.
        config: Thresholds. The single source of every number a check compares against.
        activity: The regime classification (`mdq.quality.context`) used to downgrade
            severities. `None` means "no regime knowledge", which every check treats as
            "do not downgrade" rather than as DORMANT.
        sessions: Per-exchange session profiles; `intrabar_gap` reads `break_local`.
        sibling_contracts: Optional `(exchange, session_date)` frame of sessions on
            which *any* contract of that exchange traded. `missing_session` uses it to
            tell a holiday (everything absent) from a real hole (one contract absent).
            Derived from the bars themselves when not supplied.
        rejects: Rows ingestion could not place, in the shape documented by
            `mdq.quality.checks.malformed_record`. `None` when no ingest step ran.
    """

    frequency: Frequency
    config: QualityConfig = field(default_factory=QualityConfig)
    activity: ActivityProfile | None = None
    sessions: dict[str, SessionProfile] = field(default_factory=lambda: dict(DEFAULT_SESSIONS))
    sibling_contracts: pl.LazyFrame | None = None
    rejects: pl.DataFrame | None = None

    @classmethod
    def build(
        cls,
        bars: BarFrame,
        config: QualityConfig | None = None,
        *,
        sessions: dict[str, SessionProfile] | None = None,
        sibling_contracts: pl.LazyFrame | None = None,
        rejects: pl.DataFrame | None = None,
    ) -> CheckContext:
        """The normal way to build a context: derive the activity profile from `bars`."""
        cfg = config or QualityConfig()
        return cls(
            frequency=bars.frequency,
            config=cfg,
            activity=activity_profile(bars, cfg),
            sessions=dict(sessions) if sessions is not None else dict(DEFAULT_SESSIONS),
            sibling_contracts=sibling_contracts,
            rejects=rejects,
        )

    @property
    def profile(self) -> ActivityProfile:
        """The activity profile, or an empty one when no regime knowledge exists."""
        return self.activity or ActivityProfile.empty(self.frequency, self.config)

    def with_regime(self, bars: pl.LazyFrame) -> pl.LazyFrame:
        """`bars` plus the joined `regime` and the thresholds that produced it."""
        return self.profile.join(bars)


_REGISTRY: dict[str, Check] = {}
_LOADED = False


def register_check[CheckClass: type[Check]](cls: CheckClass) -> CheckClass:
    """Class decorator adding a check to the registry. Duplicate ids are an error."""
    instance = cls()
    check_id = instance.id
    existing = _REGISTRY.get(check_id)
    if existing is not None and type(existing) is not cls:
        raise ValueError(
            f"duplicate check id {check_id!r} ({type(existing).__name__} vs {cls.__name__})"
        )
    _REGISTRY[check_id] = instance
    return cls


def load_checks() -> None:
    """Import every module under `mdq.quality.checks`, populating the registry."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    package = importlib.import_module(_CHECKS_PACKAGE)
    for info in pkgutil.iter_modules(package.__path__, f"{_CHECKS_PACKAGE}."):
        importlib.import_module(info.name)


class CheckRegistry:
    """Lookup over every registered check. Auto-loads on first use."""

    @staticmethod
    def all() -> tuple[Check, ...]:
        """Every registered check, ordered by id."""
        load_checks()
        return tuple(_REGISTRY[key] for key in sorted(_REGISTRY))

    @staticmethod
    def ids() -> tuple[str, ...]:
        """Sorted ids of every registered check."""
        load_checks()
        return tuple(sorted(_REGISTRY))

    @staticmethod
    def for_frequency(frequency: Frequency) -> tuple[Check, ...]:
        """Checks that declare themselves applicable to `frequency`."""
        return tuple(c for c in CheckRegistry.all() if frequency in c.frequencies)

    @staticmethod
    def get(check_id: str) -> Check:
        """One check by id."""
        load_checks()
        try:
            return _REGISTRY[check_id]
        except KeyError:
            raise UnknownCheckError(
                f"unknown check {check_id!r}; registered: {sorted(_REGISTRY)}"
            ) from None


def run_checks(
    bars: BarFrame,
    ctx: CheckContext,
    checks: Iterable[str | Check] | None = None,
) -> QualityReport:
    """Run every applicable check over `bars` and assemble a `QualityReport`.

    Args:
        bars: The frame to check.
        ctx: Thresholds, activity profile, sessions and ingest rejects.
        checks: Optional subset — ids or `Check` instances. Defaults to every
            registered check applicable to `bars.frequency`.

    Returns:
        A report whose `findings` frame is the concatenation of every check's output.
        A check that raises contributes a single ERROR `check_failed` finding instead of
        aborting the run.

    Raises:
        ValueError: if `ctx.frequency` disagrees with `bars.frequency` — that mismatch
            would silently select the wrong checks and the wrong activity model.
    """
    if ctx.frequency is not bars.frequency:
        raise ValueError(
            f"context frequency {ctx.frequency} does not match bars frequency {bars.frequency}"
        )
    selected = _resolve(checks, bars.frequency)
    lf = bars.lf
    frames: list[pl.DataFrame] = []
    for check in selected:
        try:
            frames.append(_conform(check.run(lf, ctx), check.id))
        except Exception as exc:
            frames.append(_check_failed(check, bars.frequency, exc))
    findings = pl.concat(frames, how="vertical") if frames else empty_finding_df()
    return QualityReport(
        findings=findings,
        frequency=bars.frequency,
        checks_run=tuple(c.id for c in selected),
        activity=ctx.activity,
        thresholds=_thresholds(ctx),
    )


def _resolve(checks: Iterable[str | Check] | None, frequency: Frequency) -> tuple[Check, ...]:
    """Normalise the `checks` argument to applicable `Check` instances, order preserved."""
    if checks is None:
        return CheckRegistry.for_frequency(frequency)
    resolved: list[Check] = []
    for item in checks:
        check = CheckRegistry.get(item) if isinstance(item, str) else item
        if frequency in check.frequencies:
            resolved.append(check)
    return tuple(resolved)


def _conform(frame: pl.DataFrame, check_id: str) -> pl.DataFrame:
    """Validate and project a check's output, raising so the caller can isolate it."""
    if not isinstance(frame, pl.DataFrame):
        raise TypeError(
            f"check {check_id!r} returned {type(frame).__name__}, expected a polars DataFrame"
        )
    missing = [name for name in FINDING_SCHEMA.names() if name not in frame.columns]
    if missing:
        raise ValueError(f"check {check_id!r} returned a frame missing columns {missing}")
    return frame.select(FINDING_SCHEMA.names()).cast(dict(FINDING_SCHEMA))  # type: ignore[arg-type]


def _check_failed(check: Check, frequency: Frequency, exc: Exception) -> pl.DataFrame:
    """Turn an exploding check into one honest ERROR finding."""
    finding = Finding(
        check_id=CHECK_FAILED_ID,
        severity=Severity.ERROR,
        contract=None,
        frequency=frequency,
        message=f"check {check.id!r} failed: {type(exc).__name__}: {exc}",
        count=1,
        evidence={
            "check_id": check.id,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": "".join(traceback.format_exception(exc))[-2_000:],
            "row_ids": [],
        },
    )
    return pl.DataFrame([finding.to_row()], schema=FINDING_SCHEMA)


def _thresholds(ctx: CheckContext) -> dict[str, object]:
    """The numbers in force, so the report can explain its own severities."""
    return {
        "config": ctx.config.model_dump(mode="json"),
        "activity": ctx.profile.thresholds(),
    }
