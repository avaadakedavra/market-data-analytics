"""The data fetcher's pure parts: pattern selection and checksum verification.

Nothing here touches the network. One golden test runs the real verification against
`data/raw` when it happens to be present.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fetch_data import (
    CHECKSUM_EXEMPT,
    REVISION,
    build_parser,
    build_patterns,
    main,
    match_pattern,
    parse_manifest,
    report,
    select_expected,
    sha256_of,
    verify,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("data/daily/CME/ES/ESH26.parquet", "data/daily/*/ES/*.parquet", True),
        ("data/minute/CME/ES/ESH26.parquet", "data/daily/*/ES/*.parquet", False),
        ("data/daily/NYMEX/CL/CLG26.parquet", "data/daily/*/ES/*.parquet", False),
        ("data/daily/CME/ES/ESH26.parquet", "data/daily/**/*.parquet", True),
        ("schema.json", "*.json", True),
        # A single star must not cross a directory separator, or "*.json" would
        # drag in every nested file.
        ("examples/thing.json", "*.json", False),
        ("examples/load_with_pandas.py", "examples/*", True),
    ],
)
def test_match_pattern_is_segment_aware(path: str, pattern: str, expected: bool) -> None:
    assert match_pattern(path, pattern) is expected


def test_build_patterns_defaults_to_everything() -> None:
    patterns = build_patterns(None, None)
    assert "data/daily/**/*.parquet" in patterns
    assert "data/minute/**/*.parquet" in patterns
    assert "*.json" in patterns


def test_build_patterns_narrows_to_roots_and_frequencies() -> None:
    patterns = build_patterns(["es", "CL"], ["daily"])
    assert "data/daily/*/ES/*.parquet" in patterns
    assert "data/daily/*/CL/*.parquet" in patterns
    assert not any("minute" in p for p in patterns)


def test_build_patterns_rejects_an_unknown_frequency() -> None:
    with pytest.raises(ValueError, match="unknown frequencies"):
        build_patterns(None, ["hourly"])


def test_parse_manifest_reads_sha256sum_format() -> None:
    manifest = parse_manifest(
        "# comment\naa11  data/daily/CME/ES/ESH26.parquet\nbb22 *README.md\n\n"
    )
    assert manifest == {"data/daily/CME/ES/ESH26.parquet": "aa11", "README.md": "bb22"}


def test_select_expected_filters_by_pattern() -> None:
    manifest = {
        "data/daily/CME/ES/ESH26.parquet": "a",
        "data/minute/CME/ES/ESH26.parquet": "b",
        "README.md": "c",
    }
    selected = select_expected(manifest, build_patterns(["ES"], ["daily"]))
    assert set(selected) == {"data/daily/CME/ES/ESH26.parquet", "README.md"}


def _write(root: Path, name: str, body: bytes) -> str:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def test_sha256_of_streams_the_file(tmp_path: Path) -> None:
    digest = _write(tmp_path, "f.bin", b"x" * (2 << 20))
    assert sha256_of(tmp_path / "f.bin", chunk_size=1024) == digest


def test_verify_separates_good_corrupt_missing_and_exempt(tmp_path: Path) -> None:
    good = _write(tmp_path, "data/daily/CME/ES/ESH26.parquet", b"good")
    _write(tmp_path, "data/daily/CME/ES/ESM26.parquet", b"tampered")
    _write(tmp_path, "README.md", b"edited after the manifest was cut")

    result = verify(
        tmp_path,
        {
            "data/daily/CME/ES/ESH26.parquet": good,
            "data/daily/CME/ES/ESM26.parquet": "0" * 64,
            "data/daily/CME/ES/ESU26.parquet": "1" * 64,
            "README.md": "2" * 64,
        },
    )

    assert result.verified == ["data/daily/CME/ES/ESH26.parquet"]
    assert result.mismatched == ["data/daily/CME/ES/ESM26.parquet"]
    assert result.missing == ["data/daily/CME/ES/ESU26.parquet"]
    # The documentation files legitimately differ — warn, never fail.
    assert result.exempt_mismatched == ["README.md"]
    assert not result.ok


def test_verify_passes_when_every_data_file_matches(tmp_path: Path) -> None:
    digest = _write(tmp_path, "data/daily/CME/ES/ESH26.parquet", b"good")
    _write(tmp_path, "dataset.json", b"{}")
    result = verify(
        tmp_path,
        {"data/daily/CME/ES/ESH26.parquet": digest, "dataset.json": "f" * 64},
    )
    assert result.ok
    assert result.exempt_mismatched == ["dataset.json"]


def test_exempt_set_is_only_the_two_documentation_files() -> None:
    assert {"README.md", "dataset.json"} == CHECKSUM_EXEMPT


def test_report_prints_warnings_and_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, "README.md", b"edited")
    report(verify(tmp_path, {"README.md": "0" * 64, "data/x.parquet": "1" * 64}))
    captured = capsys.readouterr()
    assert "WARNING: README.md" in captured.out
    assert "ERROR: data/x.parquet is missing" in captured.err


def test_main_is_idempotent_and_offline_when_everything_verifies(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = _write(tmp_path, "data/daily/CME/ES/ESH26.parquet", b"good")
    (tmp_path / "checksums.sha256").write_text(
        f"{digest}  data/daily/CME/ES/ESH26.parquet\n", encoding="utf-8"
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not hit the network when files already verify")

    monkeypatch.setattr("fetch_data.download", _explode)

    for _ in range(2):  # idempotent
        assert main(["--roots", "ES", "--frequencies", "daily", "--data-dir", str(tmp_path)]) == 0
    assert "up to date" in capsys.readouterr().out


def test_main_reports_failure_on_a_corrupt_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, "data/daily/CME/ES/ESH26.parquet", b"tampered")
    (tmp_path / "checksums.sha256").write_text(
        f"{'0' * 64}  data/daily/CME/ES/ESH26.parquet\n", encoding="utf-8"
    )
    calls: list[object] = []
    monkeypatch.setattr("fetch_data.download", lambda *a, **_k: calls.append(a))

    assert main(["--roots", "ES", "--data-dir", str(tmp_path)]) == 1
    assert calls, "a corrupt file must trigger a re-download attempt"


def test_parser_defaults() -> None:
    args = build_parser().parse_args([])
    assert args.roots is None and args.frequencies is None
    assert args.revision == REVISION
    assert not args.all


@pytest.mark.golden
def test_real_data_dir_verifies(raw_data_dir: Path) -> None:
    """The acceptance check, when the (gitignored) real sample is present."""
    if not (raw_data_dir / "checksums.sha256").is_file():
        pytest.skip("data/raw not fetched")
    manifest = parse_manifest((raw_data_dir / "checksums.sha256").read_text(encoding="utf-8"))
    expected = select_expected(manifest, build_patterns(["ES"], ["daily"]))
    result = verify(raw_data_dir, expected)
    assert result.ok
    assert len(result.verified) >= 5
