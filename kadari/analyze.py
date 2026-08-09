"""Turn observed calls into a spend model. Pure, deterministic, no I/O, no network.

This module computes every dollar figure Kadari shows anyone, in either direction: the
free report a stranger renders locally and the spend surfaces of the report we hand a
customer both come through here. That is the point — two renderers may draw the same
number differently without anyone being misled, but two renderers *computing* different
numbers for the same log is a glass-box bug.

The honesty rules are structural, not editorial. They are properties of the returned dict
rather than things a report author has to remember:

* ``spend.priced_usd`` covers **priced calls only**. A model we hold no rate for is never
  charged $0.00 into a total — its calls and tokens are counted under ``coverage`` and its
  dollars are simply absent. A total that quietly excludes 30% of the traffic while
  looking complete is worse than one that says so.
* **Measured and estimated spend are separate subtotals** and are never pre-summed into a
  field a renderer could mistake for a metered figure (AP-01).
* **A sampled log is labelled, and never silently scaled.** Extrapolation, when the sample
  rate is known, is a separate block with its own label — never folded into the observed
  total.
* ``at_stake`` is **price arithmetic, not a saving.** It is the customer's own token volume
  at another published rate: the size of the question, not an answer to it. Whether a
  cheaper model would have been acceptable is a quality question this module cannot see,
  and the field carries that sentence with it so it cannot be separated from the number.
"""

from __future__ import annotations

from collections import defaultdict

from .logs import Call, LogMeta
from .prices import PriceTable
from .timestamps import iso_date

_ESTIMATE_LABEL = (
    "ESTIMATE from published list rates and your token counts -- not a saving, and not a "
    "claim that the smaller model would have been acceptable on your task. That is the "
    "question Kadari measures; this is only its size."
)
_SPEND_LABEL = "OBSERVED spend: your token counts at published list rates."
_CONCENTRATION_POINTS = (0.01, 0.05, 0.10, 0.25, 0.50)
_PERCENTILES = (0.50, 0.90, 0.99)


def analyze(calls, *, table: PriceTable, meta: LogMeta | None = None,
            warnings=()) -> dict:
    """Build the spend model. ``calls`` is any iterable of :class:`~kadari.logs.Call`."""
    calls = tuple(calls)
    meta = meta or LogMeta()
    # Costs are held PER RECORD, aligned with `calls` -- never keyed by id. A log may
    # repeat an id (the engine refuses those; for spend a repeated id is still a call that
    # was billed), and an id-keyed map merges them: the merged total then gets attributed
    # once per record downstream, so a two-record duplicate renders twice the headline in
    # the daily chart and collapses to a single fat "call" in the percentiles. `None`
    # marks a record we hold no price for.
    record_usd: list[float | None] = []
    per_model: dict[str, dict] = {}
    unpriced: dict[str, dict] = {}
    measured_usd = estimated_usd = 0.0
    n_measured = n_estimated = 0
    n_unpriced_writes = unpriced_write_tokens = 0
    n_input_omitted = n_output_omitted = 0
    n_dated = 0
    omitted_reasons: dict[str, int] = {}
    # An aggregated source (a provider usage export: one row per model per bucket) is kept
    # unexpanded, so a RECORD is not a CALL. Everything below that describes a call has to
    # know that, or the report shows a whole day as one $47 call and draws a concentration
    # curve over buckets under the word "calls".
    aggregated = any(c.n_requests > 1 for c in calls)

    for c in calls:
        if c.tokens_estimated:
            n_estimated += 1
        else:
            n_measured += 1
        n_input_omitted += 1 if c.input_omitted else 0
        n_output_omitted += 1 if c.output_omitted else 0
        # WHY the text is missing, not just that it is. "Too large to store whole" is true
        # of a capture that shed content at the ceiling and false of a usage export, which
        # never had prompt text to begin with -- and telling a customer their prompts were
        # dropped for size when the source simply has none is invented provenance.
        for reason in (c.input_omitted, c.output_omitted):
            if reason:
                omitted_reasons[reason] = omitted_reasons.get(reason, 0) + 1
        n_dated += 1 if c.ts else 0

        usd = table.cost_of(c.model, c.input_tokens, c.output_tokens, on=_date_of(c),
                            cached_input_tokens=c.cached_input_tokens,
                            cache_write_tokens=c.cache_write_tokens)
        record_usd.append(usd)
        # A cache-write premium we hold no rate for is money we can SEE but cannot price.
        # It is counted here so the report can say so, rather than being folded into the
        # headline at whatever rate happened to be handy (AP-01/AP-03). See
        # `_write_rate_unknown` for why OpenAI is the case that hits this.
        #
        # `usd is not None` is load-bearing: this counter's whole claim is that the total
        # is a FLOOR for these calls. A call on an UNPRICED model is not floored by the
        # total, it is absent from it -- already said, once, by the unpriced-models table.
        # Counting it here too would have the page caveat the same call twice, in two
        # ways that contradict each other.
        if usd is not None and c.cache_write_tokens and _write_rate_unknown(table, c.model):
            n_unpriced_writes += 1
            unpriced_write_tokens += c.cache_write_tokens
        bucket = unpriced if usd is None else per_model
        row = bucket.setdefault(c.model, {
            "model": c.model, "provider": table.provider_of(c.model),
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cached_input_tokens": 0, "cache_write_tokens": 0, "usd": 0.0,
        })
        row["calls"] += 1
        row["input_tokens"] += c.input_tokens
        row["output_tokens"] += c.output_tokens
        row["cached_input_tokens"] += c.cached_input_tokens
        row["cache_write_tokens"] += c.cache_write_tokens
        if usd is None:
            continue
        row["usd"] += usd
        if c.tokens_estimated:
            estimated_usd += usd
        else:
            measured_usd += usd

    priced_calls = sum(r["calls"] for r in per_model.values())
    total_usd = measured_usd + estimated_usd

    out = {
        "prices": {
            "as_of": table.as_of,
            "sources": list(table.sources),
            "path": table.path,
        },
        "log": {
            "n_records": len(calls),
            "sample": meta.sample,
            "sample_known": meta.sample is not None,
            "wire_version": meta.wire_version,
            "client": meta.client,
            "created": meta.created,
            "imported_from": meta.imported_from,
            "source_file": meta.source_file,
        },
        "coverage": _coverage(calls, priced_calls, unpriced, n_measured, n_estimated,
                              n_input_omitted, n_output_omitted, n_dated, aggregated,
                              omitted_reasons, n_unpriced_writes, unpriced_write_tokens),
        "spend": {
            "_label": _SPEND_LABEL,
            "priced_usd": _r(total_usd),
            "measured_usd": _r(measured_usd),
            "estimated_usd": _r(estimated_usd),
            "by_model": _sorted_rows(per_model),
            "by_provider": _by_provider(per_model),
            "tokens": {
                "input": sum(r["input_tokens"] for r in per_model.values()),
                "output": sum(r["output_tokens"] for r in per_model.values()),
                # Reported separately, never merged into `input`. They bill at different
                # rates (a cache read is a tenth of an input token; a cache write is more
                # than one), so a single summed "input tokens" figure would not correspond
                # to any charge on the customer's invoice.
                "cached_input": sum(r["cached_input_tokens"] for r in per_model.values()),
                "cache_write": sum(r["cache_write_tokens"] for r in per_model.values()),
            },
        },
        "extrapolation": _extrapolation(meta, total_usd, len(calls)),
        "distribution": _distribution(record_usd, aggregated),
        "time": _time_series(calls, record_usd),
        "at_stake": _at_stake(calls, table, record_usd, total_usd),
        "warnings": list(warnings),
    }
    return out


# ── blocks ───────────────────────────────────────────────────────────────────
def _write_rate_unknown(table: PriceTable, model: str) -> bool:
    """True when we hold no published rate for this model's cache WRITES.

    Anthropic publishes one (a 1.25x multiple of the input rate, in the table's
    ``providers`` block), so its cache writes price exactly. OpenAI began billing writes
    with GPT-5.6 and documents the same 1.25x multiple, but has not documented whether it
    REPLACES the uncached input charge for those tokens or is added on top of it -- and
    their own reported examples sum the parts to more than the whole, which they have
    acknowledged as an accounting bug. Two readings, a 25% spread, no way to choose.

    So we do not choose. `cost_of` falls back to the plain input rate (the conservative of
    the two readings), and the call is counted here so the report can state that a write
    premium is excluded and the figure is a floor for that traffic. Adding an
    ``openai.cache_write_multiplier`` to the table is a one-line change the day the
    arithmetic is published -- and this counter goes quiet on its own when it is."""
    prov = table.providers.get(table.provider_of(model) or "", {})
    return not isinstance(prov.get("cache_write_multiplier"), (int, float))


def _coverage(calls, priced_calls, unpriced, n_measured, n_estimated,
              n_input_omitted, n_output_omitted, n_dated, aggregated,
              omitted_reasons, n_unpriced_writes=0, unpriced_write_tokens=0) -> dict:
    """What the spend figure does and does not cover.

    This block exists so a headline can never stand alone. Every total in ``spend`` is
    conditional on these counts, and a renderer that shows one without the other is
    showing a number that looks more complete than it is."""
    n = len(calls)
    return {
        "n_calls": n,
        "n_priced_calls": priced_calls,
        "priced_call_share": _r(priced_calls / n, 4) if n else 0.0,
        "n_unpriced_calls": n - priced_calls,
        "unpriced_models": _sorted_rows(unpriced, drop_usd=True),
        "n_measured_token_calls": n_measured,
        "n_estimated_token_calls": n_estimated,
        "n_input_omitted": n_input_omitted,
        "n_output_omitted": n_output_omitted,
        "omitted_reasons": dict(sorted(omitted_reasons.items())),
        "n_dated_calls": n_dated,
        "n_undated_calls": n - n_dated,
        # `n_calls` counts RECORDS, which is the same thing for every captured log. When
        # the source is aggregated it is not, so both numbers are stated and the flag says
        # which surfaces stopped being about calls.
        "aggregated": aggregated,
        "n_requests": sum(c.n_requests for c in calls),
        # Cache writes we observed but hold no published premium for. Non-zero means the
        # spend figure is a FLOOR for those calls, not an estimate of them -- stated as a
        # count so a reader can size the gap instead of being told it exists.
        "n_unpriced_cache_write_calls": n_unpriced_writes,
        "unpriced_cache_write_tokens": unpriced_write_tokens,
    }


def _extrapolation(meta: LogMeta, total_usd: float, n_calls: int) -> dict | None:
    """Scale-up for a known sample rate — a SEPARATE block, never folded into the total.

    Returns ``None`` when the log is complete or when the rate is unknown. Unknown is the
    important case: a log that never recorded its sampling rate could be a 1% sample, and
    inventing a factor of 1.0 for it would be a fabricated hundred-fold under-count."""
    if meta.sample is None or meta.sample >= 1.0 or meta.sample <= 0.0:
        return None
    factor = 1.0 / meta.sample
    return {
        "_label": (f"PROJECTED from a {meta.sample:.0%} sample by scaling the observed "
                   f"total {factor:.0f}x. It is arithmetic on the sample rate the log "
                   f"recorded, not additional measurement."),
        "sample": meta.sample,
        "factor": _r(factor, 4),
        "projected_usd": _r(total_usd * factor),
        "projected_calls": int(round(n_calls * factor)),
    }


def _distribution(record_usd: list[float | None], aggregated: bool = False) -> dict:
    """Per-call cost percentiles and the concentration curve.

    Concentration is the one shape that reliably changes behaviour: "the top 1% of your
    calls are 40% of your bill" tells someone exactly where to look, and it needs no
    quality claim to be true. One record is one call here, so ``n`` agrees with
    ``coverage.n_priced_calls`` and the curve is a curve over calls.

    That premise fails on an aggregated source, where a record is a whole bucket. Rather
    than relabel the axis and hope a reader notices, the block goes empty and says why --
    the same rule the renderer follows everywhere else: a section without the data it
    claims to show is omitted, not drawn with something else in it."""
    if aggregated:
        return {"n": 0, "per_call_usd": {}, "concentration": [], "aggregated": True,
                "_note": "Suppressed: this source is aggregated (one row per model per "
                         "bucket), so a record is not a call. A median, a priciest call "
                         "and a concentration curve computed over buckets would describe "
                         "a whole day as one call. Capture with the client for per-call "
                         "shape."}
    costs = sorted((u for u in record_usd if u is not None), reverse=True)
    total = sum(costs)
    if not costs:
        return {"n": 0, "per_call_usd": {}, "concentration": []}
    concentration = []
    for share in _CONCENTRATION_POINTS:
        k = max(1, int(round(len(costs) * share)))
        concentration.append({
            "top_call_share": share,
            "n_calls": k,
            "spend_share": _r(sum(costs[:k]) / total, 4) if total else 0.0,
        })
    asc = list(reversed(costs))
    return {
        "n": len(costs),
        "per_call_usd": {
            "min": _r(asc[0]),
            **{f"p{int(p * 100)}": _r(_percentile(asc, p)) for p in _PERCENTILES},
            "max": _r(asc[-1]),
            "mean": _r(total / len(costs)),
        },
        "concentration": concentration,
    }


def _time_series(calls, record_usd: list[float | None]) -> dict | None:
    """Daily spend, when the log carries timestamps.

    ``None`` rather than a flat line when nothing is dated: a chart with no time axis is
    not a chart of zero, and drawing one would invent a shape the data does not have.

    Each record contributes its OWN cost exactly once, so the columns sum to the headline.
    A chart whose bars add up to more than the total above them is a report arguing with
    itself, and the reader has no way to tell which half is wrong."""
    dated = [(c, u) for c, u in zip(calls, record_usd) if c.ts]
    if not dated:
        return None
    buckets: dict[str, dict] = {}
    for c, usd in dated:
        day = c.ts[:10]
        b = buckets.setdefault(day, {"bucket": day, "calls": 0, "usd": 0.0})
        b["calls"] += c.n_requests      # requests, not rows -- an aggregated row is many
        b["usd"] += usd or 0.0
    rows = [{**b, "usd": _r(b["usd"])} for b in sorted(buckets.values(),
                                                       key=lambda r: r["bucket"])]
    return {
        "granularity": "day",
        "n_dated_calls": len(dated),
        "n_undated_calls": len(calls) - len(dated),
        "first": rows[0]["bucket"],
        "last": rows[-1]["bucket"],
        "buckets": rows,
    }


def _at_stake(calls, table: PriceTable, record_usd: list[float | None],
              total_usd: float) -> dict | None:
    """The same tokens at the cheapest rung of the caller's own provider ladder.

    Public-rate arithmetic and nothing more. It deliberately reuses the observed token
    counts rather than modelling what a different model might have emitted — inventing a
    length would be a second, hidden assumption on top of the one this number already
    makes. Calls already on the cheapest rung contribute nothing; models with no ladder we
    have reasoned about are excluded rather than guessed at.

    Carries ``_label`` so the caveat travels with the number and cannot be laid out apart
    from it."""
    by_ladder: dict[str, dict] = {}
    total_premium = total_floor = 0.0
    for c, priced in zip(calls, record_usd):
        if priced is None:
            continue
        cheapest = table.cheapest_rung(c.model, on=_date_of(c))
        # Compare canonically: a log records the id the provider served
        # (`claude-haiku-4-5-20251001`), while a ladder names table keys
        # (`claude-haiku-4-5`). Raw-string equality would read a call that is ALREADY on
        # the cheapest rung as one with somewhere cheaper to go.
        if cheapest is None or cheapest == table.canonical(c.model):
            continue
        # Both sides carry the cache buckets. Dropping them here would (a) make `premium`
        # disagree with the same call's cost in `spend`, so the report's own two halves
        # would quote different dollars for one call, and (b) overstate the gap, since a
        # cached prompt stays cached on the cheaper rung -- the discount does not vanish
        # because the model got smaller.
        floor = table.cost_of(cheapest, c.input_tokens, c.output_tokens, on=_date_of(c),
                              cached_input_tokens=c.cached_input_tokens,
                              cache_write_tokens=c.cache_write_tokens)
        premium = table.cost_of(c.model, c.input_tokens, c.output_tokens, on=_date_of(c),
                                cached_input_tokens=c.cached_input_tokens,
                                cache_write_tokens=c.cache_write_tokens)
        if floor is None or premium is None or floor >= premium:
            continue
        entry = table.models.get(table.canonical(c.model), {})
        row = by_ladder.setdefault(entry.get("ladder"), {
            "ladder": entry.get("ladder"), "cheapest_rung": cheapest,
            "calls": 0, "observed_usd": 0.0, "at_cheapest_usd": 0.0,
        })
        row["calls"] += 1
        row["observed_usd"] += premium
        row["at_cheapest_usd"] += floor
        total_premium += premium
        total_floor += floor
    if not by_ladder:
        return None
    rows = []
    for row in sorted(by_ladder.values(), key=lambda r: -r["observed_usd"]):
        rows.append({**row, "observed_usd": _r(row["observed_usd"]),
                     "at_cheapest_usd": _r(row["at_cheapest_usd"]),
                     "difference_usd": _r(row["observed_usd"] - row["at_cheapest_usd"])})
    return {
        "_label": _ESTIMATE_LABEL,
        "observed_usd": _r(total_premium),
        "at_cheapest_usd": _r(total_floor),
        "difference_usd": _r(total_premium - total_floor),
        "share_of_priced_spend": _r(total_premium / total_usd, 4) if total_usd else 0.0,
        "by_ladder": rows,
    }


# ── helpers ──────────────────────────────────────────────────────────────────
def _date_of(c: Call) -> str | None:
    """The ISO date a call should be priced at, or ``None`` for 'current rates'.

    Validated rather than sliced: `logs.read` already drops an unreadable ``ts`` to
    ``None``, and this is the second lock on the same door — the value goes straight into
    a rate-period comparison, where a non-date does not fail but silently selects."""
    return iso_date(c.ts)


def _by_provider(per_model: dict[str, dict]) -> list[dict]:
    agg: dict[str, dict] = defaultdict(
        lambda: {"provider": None, "calls": 0, "input_tokens": 0,
                 "output_tokens": 0, "cached_input_tokens": 0,
                 "cache_write_tokens": 0, "usd": 0.0})
    for row in per_model.values():
        key = row["provider"] or "unknown"
        a = agg[key]
        a["provider"] = key
        for f in ("calls", "input_tokens", "output_tokens", "cached_input_tokens",
                  "cache_write_tokens", "usd"):
            a[f] += row[f]
    return [{**a, "usd": _r(a["usd"])} for a in sorted(agg.values(), key=lambda r: -r["usd"])]


def _sorted_rows(rows: dict[str, dict], *, drop_usd: bool = False) -> list[dict]:
    """Deterministic ordering: spend desc, then model name — so two runs over the same log
    produce byte-identical output (rule 9)."""
    out = []
    for row in sorted(rows.values(), key=lambda r: (-r["usd"], r["model"])):
        row = dict(row)
        if drop_usd:
            row.pop("usd", None)          # unpriced rows have no dollars, by definition
        else:
            row["usd"] = _r(row["usd"])
        out.append(row)
    return out


def _percentile(ascending: list[float], q: float) -> float:
    """Nearest-rank percentile over an ascending list (no interpolation, so the value
    reported is always a real call's cost)."""
    if not ascending:
        return 0.0
    k = max(1, min(len(ascending), int(-(-len(ascending) * q // 1))))
    return ascending[k - 1]


def _r(x: float, places: int = 6) -> float:
    return round(x, places)
