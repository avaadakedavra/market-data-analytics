"""`Insight`, `SuggestedRule` and `DatasetSummary` — the objects the tool hands back.

These are the product, not an internal detail: the API returns them verbatim and the
dashboard renders the rule as a copyable block. So the two properties worth protecting are
that they cannot be constructed in a nonsensical state, and that a JSON round-trip changes
nothing at all — a date that quietly became a string would make the API and the dashboard
disagree about what the tool found.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import polars as pl
import pytest

from mdq.domain.frequency import Frequency
from mdq.domain.schema import BAR_SCHEMA, BarFrame, C
from mdq.insights.models import DatasetSummary, Insight, SuggestedRule, root_of
from support import synth

pytestmark = pytest.mark.unit


def _rule(**overrides: object) -> SuggestedRule:
    kwargs: dict[str, object] = {
        "rule_id": "a_rule",
        "kind": "cleansing",
        "params": {"column": "volume"},
        "rationale": "because the numbers say so",
        "confidence": 0.5,
    }
    kwargs.update(overrides)
    return SuggestedRule(**kwargs)  # type: ignore[arg-type]


def _insight(**overrides: object) -> Insight:
    kwargs: dict[str, object] = {
        "id": "an_insight",
        "title": "A title",
        "pattern": "A pattern, quantified.",
        "evidence": {"bars_affected": 3},
        "affected_contracts": ["CLG26", "ESH26"],
        "finding_count": 2,
        "rule": _rule(),
    }
    kwargs.update(overrides)
    return Insight(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# SuggestedRule
# --------------------------------------------------------------------------- #


def test_a_rule_must_have_an_id() -> None:
    with pytest.raises(ValueError, match="must be a non-empty string"):
        _rule(rule_id="")


def test_a_rule_is_either_cleansing_or_validation() -> None:
    """Nothing else: a user must see at a glance whether adopting it changes the data."""
    with pytest.raises(ValueError, match="must be one of"):
        _rule(kind="advisory")


@pytest.mark.parametrize("confidence", [-0.1, 1.5])
def test_confidence_must_be_a_share(confidence: float) -> None:
    with pytest.raises(ValueError, match=r"must lie in \[0, 1\]"):
        _rule(confidence=confidence)


def test_confidence_is_rounded_so_two_runs_produce_the_same_json() -> None:
    assert _rule(confidence=1 / 3).confidence == 0.3333


def test_rule_params_are_coerced_to_json_safe_values_at_construction() -> None:
    rule = _rule(
        params={
            "dates": (date(2022, 1, 13), date(2021, 12, 2)),
            "seen_at": datetime(2026, 3, 3, 17, 0, tzinfo=UTC),
            "frequency": Frequency.DAILY,
            "nested": {"tolerance": 0.05, "fields": ["high", "low"]},
            "opaque": object(),
            "flag": True,
            "nothing": None,
        }
    )
    params = rule.params
    assert params["dates"] == ["2022-01-13", "2021-12-02"]
    assert params["seen_at"] == "2026-03-03T17:00:00+00:00"
    assert params["frequency"] == "daily"
    assert params["nested"] == {"tolerance": 0.05, "fields": ["high", "low"]}
    assert isinstance(params["opaque"], str)
    assert params["flag"] is True
    assert params["nothing"] is None
    assert json.loads(json.dumps(params)) == params


def test_rule_round_trips_through_a_dict() -> None:
    rule = _rule()
    assert SuggestedRule.from_dict(rule.to_dict()) == rule


def test_from_dict_rejects_an_unknown_kind_rather_than_storing_it() -> None:
    data = _rule().to_dict() | {"kind": "nonsense"}
    with pytest.raises(ValueError, match="unknown rule kind"):
        SuggestedRule.from_dict(data)


def test_from_dict_tolerates_a_minimal_payload() -> None:
    rule = SuggestedRule.from_dict({"rule_id": "r", "kind": "validation"})
    assert rule.params == {}
    assert rule.rationale == ""
    assert rule.confidence == 0.0


# --------------------------------------------------------------------------- #
# Insight
# --------------------------------------------------------------------------- #


def test_an_insight_must_name_the_rule_that_produced_it() -> None:
    with pytest.raises(ValueError, match="must be a non-empty string"):
        _insight(id="")


def test_finding_count_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="must be >= 0"):
        _insight(finding_count=-1)


def test_confidence_is_the_rules_confidence() -> None:
    """The engine sorts on this, so it must not drift from the rule it summarises."""
    assert _insight(rule=_rule(confidence=0.75)).confidence == 0.75


def test_evidence_is_coerced_to_json_safe_values() -> None:
    insight = _insight(evidence={"dates": [date(2022, 1, 13)], "roots": ("CL", "ES")})
    assert insight.evidence == {"dates": ["2022-01-13"], "roots": ["CL", "ES"]}


def test_affected_contracts_are_always_strings() -> None:
    assert _insight(affected_contracts=[1, "ESH26"]).affected_contracts == ["1", "ESH26"]


def test_an_insight_round_trips_through_json_unchanged() -> None:
    """The acceptance property: serialise, parse, rebuild, and get the same object."""
    insight = _insight(
        evidence={"dates": [date(2022, 1, 13)], "share": 0.9074, "nested": {"a": [1, 2]}}
    )
    assert Insight.from_json(insight.to_json()) == insight
    assert Insight.from_dict(json.loads(json.dumps(insight.to_dict()))) == insight


def test_to_json_is_stable_between_runs() -> None:
    """Sorted keys, so a diff of two reports shows content changes and nothing else."""
    assert _insight().to_json() == _insight().to_json()
    assert json.loads(_insight().to_json(indent=2))["id"] == "an_insight"


def test_to_dict_hands_out_copies_not_the_live_evidence() -> None:
    insight = _insight()
    insight.to_dict()["evidence"]["bars_affected"] = 999
    assert insight.evidence["bars_affected"] == 3


def test_from_dict_tolerates_a_payload_with_no_evidence_or_contracts() -> None:
    insight = Insight.from_dict(
        {
            "id": "i",
            "title": "t",
            "pattern": "p",
            "finding_count": 0,
            "rule": _rule().to_dict(),
        }
    )
    assert insight.evidence == {}
    assert insight.affected_contracts == []


# --------------------------------------------------------------------------- #
# root_of
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("contract", "expected"),
    [("CLG26", "CL"), ("ESH26", "ES"), ("SR3H26", "SR3"), ("VXX6", "VX"), (None, None)],
)
def test_root_of_strips_the_month_code_and_year(contract: str | None, expected: str | None) -> None:
    assert root_of(contract) == expected


def test_root_of_passes_an_unrecognisable_code_through() -> None:
    """Never invent a root: a code we cannot parse is its own family."""
    assert root_of("H26") == "H26"


# --------------------------------------------------------------------------- #
# DatasetSummary
# --------------------------------------------------------------------------- #


def _two_contract_bars() -> BarFrame:
    frame = pl.concat(
        [
            synth.daily_series("CLG26", [date(2026, 3, 3), date(2026, 3, 4)], seed=1),
            synth.daily_series("CLH26", [date(2026, 3, 4)], seed=2),
        ],
        how="vertical",
    )
    return synth.as_bar_frame(frame, Frequency.DAILY)


def test_summary_reads_the_facts_a_rule_quotes() -> None:
    summary = DatasetSummary.from_bars(_two_contract_bars())
    assert summary.contracts == ("CLG26", "CLH26")
    assert summary.exchanges == ("CME",)
    assert summary.frequency is Frequency.DAILY
    assert summary.row_count == 3
    assert summary.first_session == date(2026, 3, 3)
    assert summary.last_session == date(2026, 3, 4)


def test_summary_survives_more_contracts_than_exchanges() -> None:
    """Forty contracts over six exchanges is the real corpus; unequal-length distinct
    lists cannot share a frame, so this is the shape that used to blow up."""
    frames = [
        synth.daily_series(f"CL{code}26", [date(2026, 3, 3)], seed=i).with_columns(
            pl.lit("CME" if i % 2 else "NYMEX").alias(C.EXCHANGE)
        )
        for i, code in enumerate("GHJKM")
    ]
    bars = synth.as_bar_frame(pl.concat(frames, how="vertical"), Frequency.DAILY)
    summary = DatasetSummary.from_bars(bars)
    assert len(summary.contracts) == 5
    assert summary.exchanges == ("CME", "NYMEX")


def test_summary_of_an_empty_frame_knows_only_the_frequency() -> None:
    summary = DatasetSummary.from_bars(BarFrame.empty(Frequency.MINUTE))
    assert summary.contracts == ()
    assert summary.row_count == 0
    assert summary.first_session is None
    assert summary.frequency is Frequency.MINUTE


def test_contracts_for_root_is_the_denominator_for_four_of_five() -> None:
    summary = DatasetSummary(contracts=("CLG26", "CLH26", "ESH26"))
    assert summary.contracts_for_root("CL") == ("CLG26", "CLH26")
    assert summary.contracts_for_root("cl") == ("CLG26", "CLH26")
    assert summary.contracts_for_root("ZC") == ()
    assert summary.contracts_for_root(None) == ()


def test_sole_exchange_refuses_to_guess_when_the_dataset_is_mixed() -> None:
    assert DatasetSummary(exchanges=("CME",)).sole_exchange() == "CME"
    assert DatasetSummary(exchanges=("CME", "ICEUS")).sole_exchange() is None
    assert DatasetSummary().sole_exchange() is None


def test_summary_serialises_to_json_safe_values() -> None:
    summary = DatasetSummary.from_bars(_two_contract_bars())
    data = summary.to_dict()
    assert data["frequency"] == "daily"
    assert data["first_session"] == "2026-03-03"
    assert json.loads(json.dumps(data)) == data


def test_summary_without_a_frequency_serialises_it_as_null() -> None:
    assert DatasetSummary().to_dict()["frequency"] is None


def test_the_bar_schema_is_what_the_summary_reads() -> None:
    """A guard on the accessors above: they name real columns."""
    assert {C.CONTRACT, C.EXCHANGE, C.SESSION_DATE} <= set(BAR_SCHEMA.names())
