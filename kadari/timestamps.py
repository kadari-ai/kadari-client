"""The ONE place a source's idea of "when" becomes a date we are willing to price at.

Every importer meets a different spelling — an epoch integer, an epoch string, a date-only
column, ``2026-07-01 00:05:00``, an RFC-3339 stamp with an offset — and each one that
hand-rolls its own parser eventually invents a stamp that is not a date at all. That is not
a cosmetic bug here, because a timestamp in this codebase is a **rate selector**: prices
carry dated periods, the period comparison is a string comparison, and ``'2026/07/01'``
sorts *after* ``'2026-08-31'`` because ``/`` (0x2F) sorts after ``-`` (0x2D). A July Claude
Sonnet 5 call would price at the post-introductory $3/$15 instead of $2/$10 — spend
overstated 50%, with nothing on the page to say so.

So the rule is: **normalise or refuse — never launder.** A value we cannot read as a date
becomes ``None``, the call is treated as undated, and the report prices it at current rates
and says which calls those were. An undated call is a visible gap; a wrongly-dated one is
an invisible error, and only one of those can be caught by a reader.

Stdlib only, deterministic, no network.
"""

from __future__ import annotations

import datetime

# Epoch values outside this window are not timestamps we are willing to guess at -- they
# are far more likely a duration, a row id, or a count that happened to land in a column
# whose name looked temporal.
_EPOCH_MIN = 1_000_000_000          # 2001-09-09
_EPOCH_MAX = 4_102_444_800          # 2100-01-01


def to_iso(value) -> str | None:
    """A source's timestamp -> ``YYYY-MM-DDTHH:MM:SSZ``, or ``None`` if it is not a date.

    Accepted: epoch seconds or milliseconds (as a number or an all-digit string), a
    date-only ``YYYY-MM-DD`` (which becomes midnight UTC — the report buckets by day, so
    the precision kept is the precision the source had), and an ISO-8601 date-time with
    ``T`` or a space separator, with or without a trailing ``Z``/offset.

    Everything else is ``None``. Notably ``2026/07/01`` is rejected rather than repaired:
    the separator is unambiguous but the field ORDER is not (is ``01/07/2026`` January or
    July?), and a spend report that silently picks one is wrong for half the world."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.isdigit():
        # Seconds and milliseconds are the two forms in the wild; anything else is a
        # number we have no business reading as a time.
        if len(s) == 13:
            return _from_epoch(int(s) / 1000.0)
        if len(s) == 10:
            return _from_epoch(float(s))
        return None
    s = s.replace(" ", "T", 1)
    if len(s) == 10:
        return s + "T00:00:00Z" if _valid_date(s) else None
    if len(s) < 19 or s[10] != "T":
        return None
    # An offset is honoured rather than dropped: `2026-07-01T00:05:00+02:00` is
    # 2026-06-30 in UTC, and a report that buckets by day would otherwise put it on the
    # wrong one -- and, at a rate-period boundary, price it at the wrong rate.
    try:
        dt = datetime.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        # 3.10's parser is stricter than the shapes exports emit (fractional seconds of
        # odd length, a trailing offset we do not need). Fall back to the plain
        # second-resolution prefix, which is all the report ever uses.
        if not _valid_date(s[:10]) or not _valid_time(s[11:19]):
            return None
        return s[:19] + "Z"
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt.replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def iso_date(ts) -> str | None:
    """The ``YYYY-MM-DD`` a stamp should be priced at, or ``None`` for 'current rates'.

    Deliberately stricter than ``ts[:10]``: slicing an unvalidated string hands whatever
    the log happened to contain straight to the rate-period comparison, which is exactly
    the laundering this module exists to stop."""
    if not isinstance(ts, str) or len(ts) < 10 or not _valid_date(ts[:10]):
        return None
    return ts[:10]


def _from_epoch(secs: float) -> str | None:
    if not _EPOCH_MIN <= secs <= _EPOCH_MAX:
        return None
    try:
        return (datetime.datetime.fromtimestamp(secs, datetime.timezone.utc)
                .replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    except (ValueError, OSError, OverflowError):
        return None


def _valid_date(s: str) -> bool:
    """A real calendar date in ``YYYY-MM-DD`` form — not merely ten plausible characters."""
    try:
        datetime.date.fromisoformat(s)
    except ValueError:
        return False
    return True


def _valid_time(s: str) -> bool:
    try:
        datetime.time.fromisoformat(s)
    except ValueError:
        return False
    return True
