"""Bar frequency.

`Frequency` is the *only* place frequency-specific timing knowledge lives: it carries
the nominal spacing between consecutive bars (`bar_period`). Everything else in the
codebase keys off that rather than branching on `if frequency is MINUTE`.
"""

from __future__ import annotations

from datetime import timedelta
from enum import Enum

__all__ = ["Frequency"]


class Frequency(Enum):
    """The two bar frequencies the platform understands.

    ``bar_period`` is the nominal spacing between consecutive bars of a frequency:

    * ``MINUTE`` bars are exactly one minute apart *within* a session.
    * ``DAILY`` bars have **no** fixed period — the gap between two daily bars depends
      on weekends and holidays — so ``bar_period`` is ``None`` and callers must fall
      back to session-based reasoning.
    """

    DAILY = ("daily", None)
    MINUTE = ("minute", timedelta(minutes=1))

    bar_period: timedelta | None

    def __new__(cls, value: str, bar_period: timedelta | None) -> Frequency:
        obj = object.__new__(cls)
        obj._value_ = value
        obj.bar_period = bar_period
        return obj

    def __str__(self) -> str:
        return str(self.value)

    @classmethod
    def parse(cls, value: str | Frequency) -> Frequency:
        """Case-insensitive lookup by name or value (``"MINUTE"``, ``"minute"``)."""
        if isinstance(value, cls):
            return value
        wanted = str(value).strip().lower()
        for member in cls:
            if member.value == wanted:
                return member
        raise ValueError(f"unknown frequency {value!r}; expected one of {[f.value for f in cls]}")
