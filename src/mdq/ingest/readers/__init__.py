"""File readers: bytes on disk → a *raw* lazy frame, and nothing more.

A reader knows one thing: how to turn a file of its format into a `pl.LazyFrame` whose
columns are exactly the source's columns. It does **no** renaming, no type coercion and
no validation — all of that is `mdq.ingest.normalise`'s job, because that is where a
bad value can be attributed to a row id and a reason.

Adding a format is "add a file, register, done":

```python
@register_reader(".json")
class JsonReader:
    def read(self, source: ReadSource) -> pl.LazyFrame:
        return pl.scan_ndjson(source)
```

Dropping that module into this package is enough: `load_readers()` imports every
submodule with `pkgutil`, so nothing anywhere else has to be edited.

One reserved convention lets a reader report a line it could not parse at all without
widening the protocol: it may emit a `MALFORMED_LINE_COLUMN` column holding the raw
text of the offending line (null for every line that parsed). Normalisation turns those
rows into `MALFORMED_LINE` rejects and drops the column.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import IO, ClassVar, Protocol, TypeVar, cast

import polars as pl

from mdq.ingest.errors import UnsupportedFormatError

__all__ = [
    "MALFORMED_LINE_COLUMN",
    "READERS",
    "ReadSource",
    "Reader",
    "ReaderRegistry",
    "extension_of",
    "load_readers",
    "read_raw",
    "register_reader",
    "supported_extensions",
]

#: Anything a reader accepts: a path, or an in-memory buffer from an upload.
ReadSource = str | Path | IO[bytes] | bytes

#: Reserved column through which a reader reports an unparseable source line.
MALFORMED_LINE_COLUMN = "__mdq_malformed_line"


class Reader(Protocol):
    """Reads one family of file formats into a raw lazy frame."""

    #: Lower-case extensions, leading dot included, that this reader claims.
    extensions: ClassVar[frozenset[str]]

    def read(self, source: ReadSource) -> pl.LazyFrame:
        """Return the file's columns, untouched, as a lazy frame."""
        ...


#: Unbound on purpose: the decorator is what gives a reader class its `extensions`
#: attribute, so the class is not yet a structural `Reader` when it is decorated.
R = TypeVar("R", bound=type)


def extension_of(source: str | Path) -> str:
    """The lower-case extension of a path, leading dot included."""
    return Path(source).suffix.lower()


class ReaderRegistry:
    """Extension → reader. One instance per process; see `READERS`."""

    def __init__(self) -> None:
        self._by_extension: dict[str, Reader] = {}

    def register(self, reader: Reader, extensions: Iterable[str]) -> None:
        """Claim `extensions` for `reader`.

        Raises:
            ValueError: if an extension is malformed or already claimed by a
                different reader — a silent overwrite would make which reader wins
                depend on import order.
        """
        for raw in extensions:
            ext = raw.lower()
            if not ext.startswith(".") or len(ext) < 2:
                raise ValueError(f"extension {raw!r} must look like '.csv'")
            existing = self._by_extension.get(ext)
            if existing is not None and type(existing) is not type(reader):
                raise ValueError(
                    f"extension {ext!r} is already registered to "
                    f"{type(existing).__name__}; cannot also give it to "
                    f"{type(reader).__name__}"
                )
            self._by_extension[ext] = reader

    def get(self, extension: str) -> Reader:
        """The reader claiming `extension`.

        Raises:
            UnsupportedFormatError: if nothing claims it.
        """
        load_readers()
        ext = extension.lower()
        reader = self._by_extension.get(ext)
        if reader is None:
            raise UnsupportedFormatError(
                f"no reader for {ext or '<no extension>'!r}; "
                f"supported formats: {', '.join(sorted(self._by_extension))}"
            )
        return reader

    def for_path(self, source: str | Path) -> Reader:
        """The reader for a path, chosen by its extension."""
        return self.get(extension_of(source))

    def extensions(self) -> frozenset[str]:
        """Every registered extension."""
        load_readers()
        return frozenset(self._by_extension)


#: The process-wide registry.
READERS = ReaderRegistry()


def register_reader(*extensions: str) -> Callable[[R], R]:
    """Class decorator: register a reader for `extensions`.

    The decorated class also gets `extensions` set, so the `Reader` protocol is
    satisfied by the declaration itself rather than by a second, duplicated literal.
    """

    def decorate(cls: R) -> R:
        claimed = frozenset(ext.lower() for ext in extensions)
        cls.extensions = claimed  # type: ignore[attr-defined]
        READERS.register(cast(Reader, cls()), claimed)
        return cls

    return decorate


_loaded = False


def load_readers() -> None:
    """Import every reader module in this package exactly once.

    Import, not configuration, is what registers a reader, so this is what makes
    "drop a module in the package" sufficient.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    for module in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{module.name}")


def supported_extensions() -> frozenset[str]:
    """Every extension the platform can ingest."""
    return READERS.extensions()


def read_raw(source: str | Path) -> pl.LazyFrame:
    """Read `source` with the reader claiming its extension.

    Raises:
        UnsupportedFormatError: if no reader claims the extension.
    """
    return READERS.for_path(source).read(source)
