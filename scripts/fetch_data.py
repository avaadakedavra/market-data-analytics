#!/usr/bin/env python
"""Download the evaluation sample from HuggingFace and verify it against the manifest.

    uv run python scripts/fetch_data.py --all
    uv run python scripts/fetch_data.py --roots ES CL --frequencies minute

The revision is pinned so the corpus is reproducible: every claim in PLAN.md was
measured against exactly these bytes. After downloading, every requested file is
checked against the published `checksums.sha256`.

Two files in the dataset — `README.md` and `dataset.json` — do **not** match the
manifest (the docs were edited after the manifest was cut). They are warned about, not
failed on; every data file is verified strictly.

The script is idempotent and offline-friendly: if everything requested is already
present and hashes correctly, it verifies and exits without touching the network.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

REPO_ID = "lynx1231/historical-futures-data-sample"
REVISION = "29efdfa21c5a5b2d7aa306397385cf116e011559"
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
MANIFEST_NAME = "checksums.sha256"
FREQUENCIES = ("daily", "minute")

#: Repository metadata always fetched alongside the bar files (catalogue, schema,
#: vendor per-file diagnostics used as golden tests, licence notice).
METADATA_PATTERNS = (
    "*.md",
    "*.json",
    "*.csv",
    "*.sha256",
    "catalog.parquet",
    "files.parquet",
    "examples/*",
)

#: Documentation files known to diverge from the manifest — warn, never fail.
CHECKSUM_EXEMPT = frozenset({"README.md", "dataset.json"})


class VerificationError(RuntimeError):
    """Raised when a data file's digest does not match the manifest."""


# --------------------------------------------------------------------------- #
# pure helpers (unit-tested without network)
# --------------------------------------------------------------------------- #
def match_pattern(path: str, pattern: str) -> bool:
    """Segment-aware glob match.

    Unlike `fnmatch`, a `*` here does not cross a `/`; `**` matches any number of
    segments. That keeps `*.json` meaning "a JSON file at the repo root".
    """

    def walk(parts: Sequence[str], pats: Sequence[str]) -> bool:
        if not pats:
            return not parts
        head, rest = pats[0], pats[1:]
        if head == "**":
            return any(walk(parts[i:], rest) for i in range(len(parts) + 1))
        if not parts:
            return False
        return fnmatchcase(parts[0], head) and walk(parts[1:], rest)

    return walk(path.split("/"), pattern.split("/"))


def build_patterns(
    roots: Sequence[str] | None,
    frequencies: Sequence[str] | None,
) -> list[str]:
    """Allow-patterns for the requested slice of the dataset, metadata included."""
    freqs = list(frequencies) if frequencies else list(FREQUENCIES)
    unknown = [f for f in freqs if f not in FREQUENCIES]
    if unknown:
        raise ValueError(f"unknown frequencies {unknown}; expected {list(FREQUENCIES)}")

    patterns = list(METADATA_PATTERNS)
    for freq in freqs:
        if roots:
            patterns += [f"data/{freq}/*/{root.upper()}/*.parquet" for root in roots]
        else:
            patterns.append(f"data/{freq}/**/*.parquet")
    return patterns


def parse_manifest(text: str) -> dict[str, str]:
    """Parse a `sha256sum`-style manifest into `{relative path: digest}`."""
    manifest: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        digest, _, name = stripped.partition("  ")
        if not name:
            digest, _, name = stripped.partition(" ")
        name = name.strip().lstrip("*")
        if name:
            manifest[name] = digest.strip().lower()
    return manifest


def sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    """Streaming SHA-256 of a file (the parquet files are up to ~10 MB each)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def select_expected(manifest: dict[str, str], patterns: Iterable[str]) -> dict[str, str]:
    """The manifest entries matching any of `patterns`."""
    pats = list(patterns)
    return {
        name: digest
        for name, digest in manifest.items()
        if any(match_pattern(name, pat) for pat in pats)
    }


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of verifying a set of files against the manifest."""

    verified: list[str]
    mismatched: list[str]
    exempt_mismatched: list[str]
    missing: list[str]

    @property
    def ok(self) -> bool:
        """True when no strictly-verified file is missing or corrupt."""
        return not self.mismatched and not self.missing


def verify(
    data_dir: Path,
    expected: dict[str, str],
    *,
    exempt: frozenset[str] = CHECKSUM_EXEMPT,
) -> VerifyResult:
    """Hash every expected file under `data_dir` and compare with the manifest."""
    verified: list[str] = []
    mismatched: list[str] = []
    exempt_mismatched: list[str] = []
    missing: list[str] = []
    for name in sorted(expected):
        path = data_dir / name
        if not path.is_file():
            (exempt_mismatched if name in exempt else missing).append(name)
            continue
        if sha256_of(path) == expected[name]:
            verified.append(name)
        elif name in exempt:
            exempt_mismatched.append(name)
        else:
            mismatched.append(name)
    return VerifyResult(verified, mismatched, exempt_mismatched, missing)


def read_manifest(data_dir: Path) -> dict[str, str] | None:
    """The local manifest, or None when the dataset has not been fetched yet."""
    path = data_dir / MANIFEST_NAME
    if not path.is_file():
        return None
    return parse_manifest(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
def download(data_dir: Path, patterns: Sequence[str], revision: str = REVISION) -> None:
    """Snapshot the pinned revision into `data_dir` (skips files already cached)."""
    from huggingface_hub import snapshot_download  # imported late: dev-only dependency

    data_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=REPO_ID,
        repo_type="dataset",
        revision=revision,
        local_dir=str(data_dir),
        allow_patterns=list(patterns),
    )


def fetch(
    data_dir: Path,
    patterns: Sequence[str],
    *,
    revision: str = REVISION,
    force: bool = False,
) -> VerifyResult:
    """Ensure the requested slice is present and valid; download only if needed."""
    manifest = read_manifest(data_dir)
    if manifest is not None and not force:
        expected = select_expected(manifest, patterns)
        result = verify(data_dir, expected)
        if result.ok and expected:
            print(f"up to date: {len(result.verified)} files already present and verified")
            return result
        print(
            f"fetching: {len(result.missing)} missing, {len(result.mismatched)} corrupt "
            f"of {len(expected)} requested files"
        )

    download(data_dir, patterns, revision=revision)

    manifest = read_manifest(data_dir)
    if manifest is None:
        raise VerificationError(f"{MANIFEST_NAME} missing from {data_dir} after download")
    return verify(data_dir, select_expected(manifest, patterns))


def report(result: VerifyResult) -> None:
    """Print the verification outcome."""
    print(f"verified {len(result.verified)} files against {MANIFEST_NAME}")
    for name in result.exempt_mismatched:
        print(f"  WARNING: {name} absent or differs from the manifest (docs file, ignored)")
    for name in result.missing:
        print(f"  ERROR: {name} is missing", file=sys.stderr)
    for name in result.mismatched:
        print(f"  ERROR: {name} does not match its published digest", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--roots", nargs="+", metavar="ROOT", help="Product roots, e.g. ES CL ZN.")
    parser.add_argument(
        "--frequencies",
        nargs="+",
        choices=FREQUENCIES,
        metavar="FREQ",
        help="daily and/or minute (default: both).",
    )
    parser.add_argument(
        "--all", action="store_true", help="Fetch every root and frequency (the default)."
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Destination directory."
    )
    parser.add_argument("--revision", default=REVISION, help="Pinned dataset revision.")
    parser.add_argument("--force", action="store_true", help="Re-download even if files verify.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns a process exit code."""
    args = build_parser().parse_args(argv)
    roots = None if args.all else args.roots
    frequencies = None if args.all else args.frequencies
    patterns = build_patterns(roots, frequencies)

    scope = f"roots={roots or 'all'} frequencies={frequencies or list(FREQUENCIES)}"
    print(f"{REPO_ID}@{args.revision[:12]} -> {args.data_dir}  ({scope})")

    result = fetch(args.data_dir, patterns, revision=args.revision, force=args.force)
    report(result)
    if not result.ok:
        print("checksum verification FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
