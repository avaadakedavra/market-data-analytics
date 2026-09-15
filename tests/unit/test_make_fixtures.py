"""The fixture generator.

The hand-authored CSVs are a *contract* between `scripts/make_fixtures.py` (which
declares which row carries which defect) and `tests/unit/test_normalise.py` (which
asserts those exact row ids). These tests make the two halves fail together rather than
letting a regenerated fixture silently move a defect to a different line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from make_fixtures import (
    CSV_FIXTURES,
    MALFORMED_DEFECTS,
    MALFORMED_ROWS,
    build_malformed_csv,
    main,
    write_csv_fixtures,
)
from support.fixtures import FIXTURES_DIR

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", sorted(CSV_FIXTURES))
def test_the_committed_csvs_are_what_the_generator_produces(name: str) -> None:
    expected = CSV_FIXTURES[name]()
    assert (FIXTURES_DIR / name).read_text(encoding="utf-8") == expected


def test_the_generator_is_deterministic(tmp_path: Path) -> None:
    first = {p.name: p.read_bytes() for p in write_csv_fixtures(tmp_path)}
    second = {p.name: p.read_bytes() for p in write_csv_fixtures(tmp_path)}
    assert first == second


def test_the_declared_defects_are_the_ones_the_file_actually_carries() -> None:
    lines = build_malformed_csv().splitlines()[1:]
    assert len(lines) == MALFORMED_ROWS
    declared = {defect.row_id for defect in MALFORMED_DEFECTS}
    assert declared == {5, 9, 13, 17, 21}
    assert lines[21].count(",") == lines[0].count(",") + 1  # the ragged line
    assert lines[13].startswith(",")  # the blank contract
    assert '"6,123.50"' in lines[17]  # the thousands separator


def test_the_generator_runs_without_data_raw_and_reports_its_footprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--csv-only", "--out", str(tmp_path)]) == 0
    printed = capsys.readouterr().out
    assert "budget" in printed
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(CSV_FIXTURES)


def test_the_generator_says_what_to_run_when_the_real_data_is_absent(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"fetch_data\.py"):
        main(["--out", str(tmp_path), "--data-dir", str(tmp_path / "nothing")])
