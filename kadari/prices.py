"""Published list prices, and the arithmetic that turns tokens into dollars.

This is the ONLY place a token count becomes money. It ships inside the client rather
than the engine on purpose: the free report a stranger renders on their laptop and the
report we hand a customer must never quote different dollars for the same log, and the
cheapest way to guarantee that is for both to run the same function over the same table.
Two renderers drawing the same number differently is cosmetic; two renderers *computing*
different numbers is a glass-box bug.

Three properties everything else depends on:

* **Unknown models return ``None``, never zero and never a raise.** A public tool meets
  models we have never priced -- new releases, fine-tunes, private deployments, a typo.
  Charging those $0.00 into a headline total is how you confidently report $12 to someone
  who spent $400. The caller is expected to count the call, omit its dollars, and say so.
* **Rates are dated, because a spend report prices HISTORICAL calls.** Providers run
  introductory pricing and reprice; a flat snapshot silently misprices anything that
  predates the change. Each model carries a list of periods and ``rate(model, on=...)``
  picks the one in force on that date.
* **Nothing is inferred.** No family heuristics on model-name prefixes, no "close enough"
  substitutions, no averaging. If the table does not say it, we do not know it. The
  ``aliases`` map is not an exception to this: it is transcribed data (the provider's own
  "API ID / alias" columns), one line per id, resolved by exact lookup. There is no
  date-stripping rule and no prefix rule, so an id nobody transcribed stays unpriced.

Prices decay continuously (P3); the table is a dated snapshot with its sources recorded,
and every report rendered from it states ``as_of`` and how much of the log it could price.

Stdlib only, deterministic, no network.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path

from .timestamps import iso_date

DEFAULT_PATH = Path(__file__).with_name("prices.json")


class PriceTableError(ValueError):
    """Raised only for a malformed TABLE (a build/packaging fault), never for an unknown
    model (an ordinary fact about the world, reported as ``None``)."""


@dataclass(frozen=True)
class Rate:
    """One model's price over one period, USD per million tokens."""

    input_per_m: float
    output_per_m: float
    cached_input_per_m: float | None = None
    label: str | None = None          # e.g. "introductory"
    valid_from: str | None = None     # inclusive ISO date, None = open at the start
    valid_to: str | None = None       # inclusive ISO date, None = open at the end


@dataclass(frozen=True)
class PriceTable:
    """A loaded, validated price snapshot."""

    as_of: str
    sources: tuple[dict, ...]
    models: dict[str, dict]
    ladders: dict[str, tuple[str, ...]]
    providers: dict[str, dict]
    aliases: dict[str, str] = field(default_factory=dict)
    path: str | None = None

    # ── lookup ────────────────────────────────────────────────────────────────
    def canonical(self, model: str) -> str:
        """The ``models`` key ``model`` refers to, resolving one transcribed alias hop.

        A provider response reports the id it actually served, and for Anthropic's pre-4.6
        generation that is a dated snapshot (``claude-haiku-4-5-20251001``) rather than the
        name in the price table. Without this, the most ordinary real capture there is --
        the client's own ``record_anthropic``, copying the response's ``model`` verbatim --
        reads as entirely unpriced.

        Exactly one hop, and only for an id the table does not already list itself: a real
        model always wins over an alias, so a stale map entry can never shadow a rate we
        actually hold. Unknown ids come back unchanged and go on to be reported unpriced."""
        if model in self.models:
            return model
        return self.aliases.get(model, model)

    def is_priced(self, model: str) -> bool:
        return self.canonical(model) in self.models

    def provider_of(self, model: str) -> str | None:
        entry = self.models.get(self.canonical(model))
        return entry.get("provider") if entry else None

    def rate(self, model: str, *, on: str | None = None) -> Rate | None:
        """The rate in force for ``model`` on ISO date ``on`` (default: ``as_of``).

        ``None`` means we hold no price for this model -- the caller must report the call
        as unpriced, not as free. A well-formed table covers all dates for every model it
        lists (first period open at the start, last open at the end, which ``validate()``
        enforces), so a listed model never returns ``None`` for a date reason alone."""
        entry = self.models.get(self.canonical(model))
        if entry is None:
            return None
        # Period selection below is a STRING comparison, so a malformed `on` does not fail
        # -- it silently picks a period. `'2026/07/01'` sorts after `'2026-08-31'` (`/` is
        # 0x2F, `-` is 0x2D), which prices a July call at the post-introductory rate. A
        # caller should never get here with a non-date (`kadari.timestamps` is the one
        # normaliser), so treat it as "no date given" rather than trust it.
        when = (iso_date(on) if on else None) or self.as_of
        for period in entry["rates"]:
            lo, hi = period.get("from"), period.get("to")
            if (lo is None or when >= lo) and (hi is None or when <= hi):
                return Rate(
                    input_per_m=float(period["input_per_m"]),
                    output_per_m=float(period["output_per_m"]),
                    cached_input_per_m=(float(period["cached_input_per_m"])
                                        if period.get("cached_input_per_m") is not None else None),
                    label=period.get("label"),
                    valid_from=lo, valid_to=hi,
                )
        return None

    def cost_of(self, model: str, input_tokens: int, output_tokens: int, *,
                on: str | None = None, cached_input_tokens: int = 0,
                cache_write_tokens: int = 0) -> float | None:
        """USD for one call, or ``None`` if the model is unpriced.

        Cached input is charged at the provider's published cached rate when the table
        gives one, and otherwise at the provider's cache-read multiple of the input rate.
        When neither is known, cached tokens are charged at the FULL input rate -- an
        over-count, which is the safe direction for a spend figure and the same convention
        the live adapter already uses."""
        r = self.rate(model, on=on)
        if r is None:
            return None
        prov = self.providers.get(self.provider_of(model) or "", {})
        if r.cached_input_per_m is not None:
            cached_rate = r.cached_input_per_m
        elif isinstance(prov.get("cache_read_multiplier"), (int, float)):
            cached_rate = r.input_per_m * float(prov["cache_read_multiplier"])
        else:
            cached_rate = r.input_per_m
        write_mult = prov.get("cache_write_multiplier")
        write_rate = (r.input_per_m * float(write_mult)
                      if isinstance(write_mult, (int, float)) else r.input_per_m)
        total = (max(0, input_tokens) * r.input_per_m
                 + max(0, output_tokens) * r.output_per_m
                 + max(0, cached_input_tokens) * cached_rate
                 + max(0, cache_write_tokens) * write_rate)
        return total / 1_000_000.0

    # ── ladders (used only for the price-differential ceiling) ────────────────
    def ladder_of(self, model: str) -> tuple[str, ...] | None:
        entry = self.models.get(self.canonical(model))
        name = entry.get("ladder") if entry else None
        return self.ladders.get(name) if name else None

    def cheapest_rung(self, model: str, *, on: str | None = None) -> str | None:
        """The lowest-priced rung of ``model``'s own intra-provider ladder.

        ``None`` when the model has no ladder we have reasoned about -- in which case no
        ceiling is computed for it, rather than one invented from a guess. Ladders never
        cross providers (ADR-0001): the customer keeps their provider, contract and DPA,
        so a cross-provider comparison would be a number they cannot act on."""
        rungs = self.ladder_of(model)
        if not rungs:
            return None
        priced = [(self.rate(r, on=on), r) for r in rungs]
        candidates = [(rt.input_per_m + rt.output_per_m, name)
                      for rt, name in priced if rt is not None]
        return min(candidates)[1] if candidates else None


# ── token estimation (only when the provider gave us no usage block) ─────────
def approx_tokens(text: str, *, prompt_overhead_tokens: int = 80) -> int:
    """Coarse input-token estimate: ~4 chars/token plus fixed request overhead.

    Deliberately CONSERVATIVE (rounds up) so a projected bill errs high, never low. Lives
    here, beside the rates, because it is the other half of the same arithmetic -- if the
    client and the engine estimated tokens differently they would report different dollars
    for the same log, which is the exact drift this module exists to prevent.

    An estimate is never a measurement: whatever consumes this must keep the two apart
    (see ``tokens_estimated``) and must not present their sum as metered (AP-01)."""
    return prompt_overhead_tokens + -(-len(text) // 4)   # ceil division


def approx_output_tokens(output: str) -> int:
    """Coarse output-token estimate from the returned text (at least one token)."""
    return max(1, -(-len(output) // 4))


def load(path: str | Path | None = None) -> PriceTable:
    """Load and validate a price table (the shipped one by default)."""
    p = Path(path) if path is not None else DEFAULT_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PriceTableError(f"cannot read price table {p}: {exc}") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise PriceTableError(f"price table {p} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PriceTableError(f"price table {p} must be a JSON object")
    for key in ("as_of", "models", "ladders"):
        if key not in raw:
            raise PriceTableError(f"price table {p} has no {key!r}")
    table = PriceTable(
        as_of=str(raw["as_of"]),
        sources=tuple(raw.get("sources") or ()),
        models={k: v for k, v in raw["models"].items()},
        ladders={k: tuple(v) for k, v in raw["ladders"].items()},
        providers=dict(raw.get("providers") or {}),
        aliases=dict(raw.get("aliases") or {}),
        path=str(p),
    )
    validate(table)
    return table


def _day_after(iso: str) -> str:
    """``2026-08-31`` -> ``2026-09-01``. The bound already parsed as a date."""
    return (datetime.date.fromisoformat(iso) + datetime.timedelta(days=1)).isoformat()


def _boundary_dates(table: PriceTable) -> tuple[str, ...]:
    """Every date on which some rate in this table changes, plus ``as_of``.

    Rates are piecewise-constant, so any property that holds across a table holds
    everywhere if it holds at these dates -- there is no third value between two
    boundaries to check."""
    dates = {table.as_of}
    for entry in table.models.values():
        for period in entry.get("rates") or ():
            for key in ("from", "to"):
                if period.get(key):
                    dates.add(period[key])
                    dates.add(_day_after(period[key]))
    return tuple(sorted(dates))


def validate(table: PriceTable) -> None:
    """Fail loudly on a malformed table -- it is shipped data, and a wrong rate is a wrong
    dollar figure with our name on it.

    The date-coverage rule is the load-bearing one: every model's periods must be ordered
    and must span all dates (first open at the start, last open at the end). That turns
    "no rate for this date" from a silent runtime ambiguity into a build-time failure, so
    ``rate()`` returning ``None`` can mean exactly one thing -- we do not price this model.

    The shape rules exist because ``--prices`` takes a hand-edited table. Structure alone
    does not catch a TRANSPOSITION: swap a model's input and output rates, or its cached
    and uncached rates, and the file stays perfectly well-formed while every dollar it
    produces is wrong -- and wrong quietly, with our name on it. So the invariants every
    published rate card has held are asserted rather than assumed: output costs at least
    as much as input, a cache read costs no more than an uncached read, and a ladder is
    written premium-first with each rung at or below the one above it on BOTH legs."""
    for name, entry in table.models.items():
        if not isinstance(entry, dict) or not entry.get("rates"):
            raise PriceTableError(f"{name}: no rates")
        periods = entry["rates"]
        if periods[0].get("from") is not None:
            raise PriceTableError(f"{name}: first rate period must be open at the start "
                                  f"(from: null), else historical calls cannot be priced")
        if periods[-1].get("to") is not None:
            raise PriceTableError(f"{name}: last rate period must be open at the end "
                                  f"(to: null), else future calls cannot be priced")
        for i, period in enumerate(periods):
            for key in ("input_per_m", "output_per_m"):
                v = period.get(key)
                if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                    raise PriceTableError(f"{name}[{i}]: {key} must be a non-negative number")
            in_m, out_m = period["input_per_m"], period["output_per_m"]
            if out_m < in_m:
                raise PriceTableError(
                    f"{name}[{i}]: output_per_m ({out_m}) is below input_per_m ({in_m}); "
                    f"no published rate card has ever charged less for generated tokens "
                    f"than for read ones, so this is a transposed pair. If a provider "
                    f"genuinely inverts it, that is a deliberate schema change, not a "
                    f"silent load")
            cached = period.get("cached_input_per_m")
            if cached is not None:
                if not isinstance(cached, (int, float)) or isinstance(cached, bool) \
                        or cached < 0:
                    raise PriceTableError(f"{name}[{i}]: cached_input_per_m must be a "
                                          f"non-negative number when present")
                if cached > in_m:
                    raise PriceTableError(
                        f"{name}[{i}]: cached_input_per_m ({cached}) exceeds input_per_m "
                        f"({in_m}); a cache read that costs more than the uncached read it "
                        f"replaces is a transposition, and it would turn the cache "
                        f"buckets -- most of the prompt on cache-heavy traffic -- into an "
                        f"overcharge")
            for key in ("from", "to"):
                v = period.get(key)
                if v is not None and iso_date(v) != v:
                    raise PriceTableError(
                        f"{name}[{i}]: {key} {v!r} is not a YYYY-MM-DD date. Periods are "
                        f"selected by STRING comparison, so a malformed bound does not "
                        f"fail -- it silently selects the wrong rate")
            if i and periods[i - 1].get("to") is None:
                raise PriceTableError(f"{name}[{i}]: unreachable -- the previous period is "
                                      f"open-ended")
            if i and period.get("from") is None:
                raise PriceTableError(f"{name}[{i}]: only the first period may be open at "
                                      f"the start")
            if i and period["from"] != _day_after(periods[i - 1]["to"]):
                raise PriceTableError(
                    f"{name}[{i}]: starts {period['from']} but the previous period ends "
                    f"{periods[i - 1]['to']}; periods must run in order and meet exactly. "
                    f"A gap leaves dates unpriced (so `rate()` would return None for a "
                    f"model we DO price) and an overlap prices them twice, resolved by "
                    f"whichever period happens to be listed first")
        ladder = entry.get("ladder")
        if ladder is not None and ladder not in table.ladders:
            raise PriceTableError(f"{name}: unknown ladder {ladder!r}")
    for alias, target in table.aliases.items():
        if not isinstance(alias, str) or not isinstance(target, str) or not alias or not target:
            raise PriceTableError(f"alias {alias!r}: both the id and its target must be "
                                  f"non-empty strings")
        if target not in table.models:
            raise PriceTableError(f"alias {alias!r} points at {target!r}, which is not a "
                                  f"priced model -- an alias may only name a rate we hold")
        if alias in table.models:
            raise PriceTableError(f"alias {alias!r} is also a priced model in its own right; "
                                  f"remove one, or a reader cannot tell which rate applies")
        if target in table.aliases:
            raise PriceTableError(f"alias {alias!r} -> {target!r} chains through another "
                                  f"alias; resolution is one hop, so write the final target")
    for ladder, rungs in table.ladders.items():
        missing = [r for r in rungs if r not in table.models]
        if missing:
            raise PriceTableError(f"ladder {ladder!r} names unpriced rung(s) {missing}")
        providers = {table.provider_of(r) for r in rungs}
        if len(providers) > 1:
            raise PriceTableError(
                f"ladder {ladder!r} spans providers {sorted(providers)} -- routing is "
                f"intra-provider only; a cross-provider comparison is not actionable")
        # Premium-first, and still premium-first on every date the table can price at.
        # Checked at each period boundary (plus `as_of`) because that is where the ordering
        # can flip: an introductory rate expiring on one rung and not another is exactly
        # the shape that makes a ladder non-monotone for a window nobody looked at.
        for when in _boundary_dates(table):
            rates = [(r, table.rate(r, on=when)) for r in rungs]
            for (upper, hi), (lower, lo) in zip(rates, rates[1:]):
                if hi is None or lo is None:               # covered by the rule above
                    continue
                for leg in ("input_per_m", "output_per_m"):
                    if getattr(lo, leg) > getattr(hi, leg):
                        raise PriceTableError(
                            f"ladder {ladder!r} is not premium-first on {when}: "
                            f"{lower} {leg} {getattr(lo, leg)} is above {upper}'s "
                            f"{getattr(hi, leg)}. Rungs are written most-expensive-first; "
                            f"a pair out of order here is how a transposed rate reaches a "
                            f"report that calls the top of a ladder its cheap rung")
