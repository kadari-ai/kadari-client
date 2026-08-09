"""Usage exports: CSV or JSON rows of model + token counts, with no prompt text.

Column names differ between providers, between a console download and an API response,
and between versions of each. Rather than pin one spelling and break on the next export,
each field is matched case-insensitively against a list of known spellings. That is
lenient about *shape*; it stays strict about *meaning* — a row whose model or token counts
cannot be identified is skipped and reported, never inferred from position or guessed.

Token counts are read into the wire format's three DISJOINT input buckets (uncached /
cache-read / cache-write). Which convention an export uses is decided by column NAME, in
``_input_buckets``, because the two live conventions are numerically indistinguishable and
choosing wrong misprices silently — in opposite directions per provider.

Every row produced here is explicitly marked as carrying no text (``input_omitted`` /
``output_omitted`` = ``not_in_source``) rather than being written with empty strings. The
difference matters downstream: an empty string looks like a call whose prompt happened to
be blank, while the mark says the source never had one — which is what lets the preflight
refuse a submission before it is uploaded rather than after.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from ..capture import OMITTED_LABEL
from ..timestamps import to_iso

# Known spellings, in preference order. Matching is case-insensitive and ignores
# separators, so `n_context_tokens_total`, `Input Tokens` and `promptTokens` all land.
_MODEL = ("model", "modelid", "modelname", "snapshotid", "engine", "deployment")
_INPUT = ("inputtokens", "ncontexttokenstotal", "prompttokens", "contexttokens",
          "ninputtokens", "usageinputtokens", "tokensin", "inputtokencount")
_OUTPUT = ("outputtokens", "ngeneratedtokenstotal", "completiontokens",
           "noutputtokens", "usageoutputtokens", "tokensout", "outputtokencount")
_TS = ("timestamp", "starttime", "startingat", "createdat", "created", "date", "day",
       "bucketstart", "requesttime", "time")
_COUNT = ("nrequests", "requests", "numrequests", "nummodelrequests", "count", "calls",
          "numrequestsmade")
# A column that says "uncached" in its name is the DISJOINT remainder -- the prompt minus
# whatever the cache served. Anthropic's usage report is the live case. Matched separately
# from `_INPUT` precisely because the name is what tells us which convention we are in:
# see `_input_buckets`, where getting that wrong is the whole risk.
_UNCACHED_INPUT = ("uncachedinputtokens", "inputtokensuncached", "uncachedprompttokens",
                   "uncachedinput", "uncachedtokens")
_CACHE_READ = ("cachereadinputtokens", "cachedinputtokens", "inputcachedtokens",
               "cachedtokens", "cachereadtokens", "cacheread", "promptcachedtokens",
               "inputtokenscached", "cachereadinput")
_CACHE_WRITE = ("cachecreationinputtokens", "cachecreationtokens", "cachewritetokens",
                "cachewriteinputtokens", "cachecreation", "cachewrite",
                "inputtokenscachewrite")


def _norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _pick(headers, candidates):
    normed = {_norm(h): h for h in headers}
    for c in candidates:
        if c in normed:
            return normed[c]
    return None


def _int(v):
    """A token count, or ``None`` if the cell is not one.

    ``float()`` happily accepts ``inf`` and ``nan``, and ``int(inf)`` raises
    ``OverflowError`` -- which is not caught here and is not caught by the caller, so one
    such cell in a two-million-row export took down the entire import with a traceback.
    A cell we cannot read is one skipped-and-reported row, never a crash."""
    try:
        f = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):    # NaN / +-inf
        return None
    return int(f) if f >= 0 else None


class _Incoherent(Exception):
    """A row whose cache columns cannot be reconciled with its input column."""


def _input_buckets(raw: dict, keys: list) -> tuple[int | None, int, int, bool]:
    """``(uncached_input, cache_read, cache_write, saw_unreadable_write)``.

    Returns the three DISJOINT buckets the wire format wants, from an export written in
    whichever of the two provider conventions this file happens to be in. Which convention
    we are in is read off the COLUMN NAME, never off the numbers:

      * A column naming itself *uncached* (Anthropic's usage report) already IS the
        remainder. Taken as-is; nothing is subtracted.
      * A generic ``input_tokens`` / ``prompt_tokens`` column sitting beside a cached
        column is the OpenAI convention, where the total is INCLUSIVE of the cached part.
        Subtracted once, here.
      * A generic input column with no cache column beside it is left exactly as it was,
        so every export that imported before still imports identically.

    Inferring the convention from the arithmetic instead would be guessing: both readings
    satisfy ``cached <= input``, so the numbers cannot distinguish them, and picking wrong
    is invisible -- it misprices by the size of the cached prefix, in a different direction
    per provider. The one case the arithmetic DOES rule on is ``cached > input``, which no
    inclusive export can produce; rather than quietly re-read such a row as disjoint we
    raise ``_Incoherent`` and the caller skips and reports it. A skipped row is a hole the
    coverage figures already know how to describe; a silently re-interpreted one is not."""
    uk = _pick(keys, _UNCACHED_INPUT)
    rk, wk = _pick(keys, _CACHE_READ), _pick(keys, _CACHE_WRITE)
    cache_read = (_int(raw.get(rk)) or 0) if rk else 0
    # A cache-creation column we matched but cannot read as a single count -- Anthropic
    # reports it split by TTL, and those tiers price differently, so summing them would
    # invent a rate. Reported rather than silently treated as no cache activity.
    cache_write, unreadable_write = 0, False
    if wk:
        parsed = _int(raw.get(wk))
        if parsed is None and raw.get(wk) not in (None, ""):
            unreadable_write = True
        else:
            cache_write = parsed or 0
    if uk:
        # An *uncached* column is a claim that the input was SPLIT. If none of the pieces
        # it was split from is present, the file has handed us one slice of the prompt and
        # kept the rest: importing it would price that slice and report full coverage
        # beside it -- the $2.55 day read as $0.90. So the row is refused, which leaves a
        # hole the coverage figures describe out loud, rather than a total that is quietly
        # short by however much of the prompt was cached.
        if not (rk or wk):
            return None, 0, 0, unreadable_write
        return _int(raw.get(uk)), cache_read, cache_write, unreadable_write
    ik = _pick(keys, _INPUT)
    total = _int(raw.get(ik)) if ik else None
    if total is None or not (rk or wk):
        return total, cache_read, cache_write, unreadable_write
    if cache_read > total:
        raise _Incoherent
    return total - cache_read, cache_read, cache_write, unreadable_write


def _date(v) -> str | None:
    """Delegate to the one shared normaliser (see :mod:`kadari.timestamps`).

    This used to be its own parser and quietly produced stamps that were not dates --
    ``2026/07/01 00:05:00`` came back as ``'2026/07/01T00:05:00Z'``, which then selected
    the WRONG rate period, because period selection is a string comparison and ``/`` sorts
    after ``-``. A shared normaliser is not a tidiness preference here: two parsers means
    two chances to invent a date, and the gateway importer had invented a different one."""
    return to_iso(v)


def _rows_from(path: Path):
    """Yield dict rows from a CSV, a JSON array, a JSON object with a data/results key, or
    JSONL — because 'the usage export' is at least four different files in practice."""
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            obj = None
        if isinstance(obj, list):
            yield from (r for r in obj if isinstance(r, dict))
            return
        if isinstance(obj, dict):
            for key in ("data", "results", "usage", "buckets", "items"):
                if isinstance(obj.get(key), list):
                    for r in obj[key]:
                        if isinstance(r, dict):
                            # Some shapes nest the real rows one level deeper. OpenAI's
                            # `/organization/usage/completions` is the live case, and the
                            # nesting is not cosmetic: the BUCKET carries `start_time`
                            # while the inner `results[]` carry the model and the token
                            # counts. Yielding the inner rows alone threw the time away,
                            # so an imported billing export -- the one case that is
                            # entirely historical -- lost its trend chart AND priced every
                            # call at today's rate. The bucket's own scalar fields are
                            # carried down onto each inner row instead, and an inner value
                            # always wins so nothing is overwritten.
                            inner = r.get("results")
                            if isinstance(inner, list):
                                carried = {k: v for k, v in r.items()
                                           if k != "results"
                                           and isinstance(v, (str, int, float))
                                           and not isinstance(v, bool)}
                                for x in inner:
                                    if isinstance(x, dict):
                                        yield {**carried, **x}
                            else:
                                yield r
                    return
            yield obj
            return
        for line in text.splitlines():                    # JSONL
            line = line.strip()
            if line and not line.startswith("//"):
                try:
                    r = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(r, dict):
                    yield r
        return
    yield from csv.DictReader(text.splitlines())


def read_generic(path, *, default_model: str | None = None):
    """``(rows, warnings)`` in Kadari's wire format."""
    path = Path(path)
    out, warnings = [], []
    skipped_model = skipped_tokens = partial_tokens = aggregated = 0
    incoherent = unreadable_writes = 0
    unmatched_headers: set[str] = set()
    seq = 0
    for raw in _rows_from(path):
        keys = list(raw.keys())
        mk, ok = _pick(keys, _MODEL), _pick(keys, _OUTPUT)
        model = (str(raw[mk]).strip() if mk and raw.get(mk) else None) or default_model
        if not model:
            skipped_model += 1
            continue
        try:
            in_tok, cache_read, cache_write, bad_write = _input_buckets(raw, keys)
        except _Incoherent:
            incoherent += 1
            continue
        unreadable_writes += 1 if bad_write else 0
        out_tok = _int(raw.get(ok)) if ok else None
        # BOTH counts are required, the same all-or-nothing rule `kadari.logs._usage`
        # applies to a capture record. Half a usage block is not a measurement, and the
        # tempting `in_tok or 0` is not a fallback -- it is a guess, silently priced.
        # The cache buckets are NOT part of that pair: they are additive extras that
        # default to zero, exactly as they do on the capture side. What used to fail here
        # was the Anthropic usage report, whose input arrives split three ways and so
        # matched no single input column at all; `_input_buckets` now reads all three and
        # keeps them disjoint, which is the only shape that prices them correctly.
        if in_tok is None and out_tok is None:
            skipped_tokens += 1
            continue
        if in_tok is None or out_tok is None:
            partial_tokens += 1
            # EVERY column we recognised, not just the ones we needed here -- otherwise
            # the timestamp and request-count columns we matched perfectly well are listed
            # back to the user as things we could not read, which sends them looking in
            # the wrong place for a column that is not the problem.
            # An *uncached* column stranded without its cache columns IS the thing to
            # look at, so it is listed rather than treated as matched -- we recognised
            # the name and still could not make a prompt out of it.
            matched = {h for h in (mk, ok, _pick(keys, _INPUT),
                                   _pick(keys, _CACHE_READ), _pick(keys, _CACHE_WRITE),
                                   _pick(keys, _TS), _pick(keys, _COUNT)) if h}
            unmatched_headers.update(h for h in keys if h not in matched)
            continue
        ts = _date(raw.get(_pick(keys, _TS))) if _pick(keys, _TS) else None
        # A usage export is usually AGGREGATED -- one line per model per bucket, with a
        # request count. Expanding it into one record per call would fabricate calls we
        # never saw, so the bucket stays one record and the count is preserved in the id.
        # ...and it is recorded on the row as well as in the id, because nothing downstream
        # can read an id. Without it `coverage.n_calls` reports "2 calls read" over 8,300
        # requests, the Median/Priciest-call tiles describe a whole day as one call, and the
        # concentration curve is a curve over buckets wearing the word "calls".
        ck = _pick(keys, _COUNT)
        n_req = _int(raw.get(ck)) if ck else None
        seq += 1
        row = {
            "id": f"import-{seq}" + (f"-x{n_req}" if n_req and n_req > 1 else ""),
            "model": model,
            "input": "",
            "input_omitted": "not_in_source",
            "output": OMITTED_LABEL,
            "output_omitted": "not_in_source",
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        }
        # Written only when non-zero, matching the capture client: an absent count means
        # "not reported", while a 0 we never observed would assert the traffic was
        # uncached. It also keeps a cache-free export byte-identical to what it was.
        if cache_read:
            row["usage"]["cached_input_tokens"] = cache_read
        if cache_write:
            row["usage"]["cache_write_tokens"] = cache_write
        if ts:
            row["ts"] = ts
        if n_req and n_req > 1:
            row["n_requests"] = n_req
            aggregated += 1
        out.append(row)
    if skipped_model:
        warnings.append(f"{skipped_model} row(s) named no model column "
                        f"(looked for: {', '.join(_MODEL[:4])}…) and were skipped")
    if skipped_tokens:
        warnings.append(f"{skipped_tokens} row(s) carried no recognisable token counts "
                        f"and were skipped")
    if partial_tokens:
        cols = ", ".join(sorted(unmatched_headers)[:6]) or "none"
        warnings.append(
            f"{partial_tokens} row(s) named ONE token count but not the other, so they "
            f"were skipped rather than costed with a zero on the missing side — a zero "
            f"there would under-report the bill while the report still said it had "
            f"priced every call. A column naming *uncached* input counts as half a row "
            f"too, unless the cache columns it was split from are there beside it. "
            f"Columns we could not match: {cols}. "
            f"Tell us the export you have and we will add it.")
    if incoherent:
        warnings.append(
            f"{incoherent} row(s) reported more cached input tokens than input tokens, "
            f"which no export we know how to read can mean, so they were skipped. Read "
            f"one way the cached tokens are part of the input total; read the other they "
            f"sit beside it. Both readings are ordinary, they differ by the size of your "
            f"cached prefix, and nothing in the file says which one it is — so we skipped "
            f"the rows rather than pick one and price your traffic on a coin toss.")
    if unreadable_writes:
        warnings.append(
            f"{unreadable_writes} row(s) carried a cache-creation column we could not read "
            f"as a single number — Anthropic reports cache writes split by how long they "
            f"live, and the tiers bill at different rates, so adding them up would invent "
            f"a price. Those writes are missing from the totals, which makes the spend "
            f"figure a floor for those rows. Send us the export and we will read the split "
            f"properly.")
    if aggregated:
        total_req = sum(r.get("n_requests", 1) for r in out)
        warnings.append(
            f"This export is AGGREGATED: {aggregated:,} of {len(out):,} row(s) each "
            f"summarise many calls ({total_req:,} requests in total). Each stays one "
            f"record — expanding a row into per-call records would fabricate calls nobody "
            f"observed — so the spend totals are right while the per-CALL surfaces are "
            f"not: the median and priciest figures and the concentration curve are "
            f"suppressed rather than shown describing buckets as if they were calls.")
    if out:
        warnings.append(
            "This source reports token counts only — it has no prompt text and no model "
            "answers. That is enough for a full spend report and NOT enough for a savings "
            "evaluation, which has to re-run your calls and compare against the answer "
            "you already paid for. Capture with the client when you want that.")
    if not out:
        warnings.append(f"No usable rows found in {path.name}. Expected columns naming a "
                        f"model and input/output token counts.")
    return out, warnings


def read_openai(path):
    return read_generic(path)


def read_anthropic(path):
    return read_generic(path)
