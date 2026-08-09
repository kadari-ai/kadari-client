"""Read a captured log back into the facts a spend report is computed from.

Deliberately narrow: this reads only what pricing needs -- which model, how many tokens,
when, and how much of that we actually *know* versus estimated. It is not a general
ingestion layer and it makes no judgement about whether a call is analysable; that is a
separate question answered elsewhere with far more context.

Everything here fails SOFT and says so. A cost report is something a stranger runs on a
log we have never seen, assembled by tools we do not control; refusing the whole file over
one bad line would be the wrong trade, and silently skipping lines would be worse. So bad
records are skipped, counted, and returned as warnings the report displays.

Stdlib only, deterministic, no network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .prices import approx_output_tokens, approx_tokens
from .timestamps import to_iso

MAX_RECORD_CHARS = 200_000        # mirrors the writer's ceiling
MAX_WARNINGS = 50                 # bound what we accumulate on a pathological file


@dataclass(frozen=True)
class Call:
    """One observed, already-paid-for call, reduced to its priceable facts."""

    id: str
    model: str
    # The UNCACHED billable input. The two cache buckets below are DISJOINT from it and
    # from each other (the wire contract in `kadari.capture` normalises both providers to
    # that shape on the way in), so the three together are the prompt. Anything that sums
    # them into one "input" figure and prices it at the input rate is wrong in whichever
    # direction the traffic happens to cache.
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    # True when the counts above are OUR estimate from text length rather than the
    # provider's metered usage. Never let these two be summed into one figure presented
    # as measured -- that is the whole reason the flag exists.
    tokens_estimated: bool = False
    ts: str | None = None                 # ISO-8601 UTC, second resolution
    input_omitted: str | None = None      # content shed at write time, reason recorded
    output_omitted: str | None = None
    # Kept for the submission preflight, which has to answer "is this log something the
    # engine could actually evaluate?" -- a question that turns entirely on whether the
    # request text and the answer the customer already paid for are both present.
    input_chars: int = 0
    output: str = ""
    # How many real requests this ONE record stands for. Always 1 for a captured call. A
    # provider usage export is aggregated -- one line per model per bucket -- and expanding
    # it into per-call records would fabricate calls nobody observed, so the bucket stays
    # one record and says how many calls it covers. Everything that describes a CALL (the
    # median, the priciest, the concentration curve) is meaningless above 1 and must be
    # suppressed rather than shown describing a whole day as a single call.
    n_requests: int = 1


@dataclass(frozen=True)
class LogMeta:
    """What the log says about ITSELF (its `//` manifest line), or all-``None``.

    ``sample=None`` means UNKNOWN, never 1.0. A log that never told us its sampling rate
    is not a log that told us it was complete, and a spend total is only meaningful if a
    reader knows which of those they are holding."""

    sample: float | None = None
    wire_version: str | None = None
    client: str | None = None
    created: str | None = None
    timestamps: bool | None = None
    # Set when the log came from `kadari import` rather than live capture. The warnings the
    # import raised are carried here because they qualify every total computed later: a row
    # the importer REFUSED (an Anthropic usage export whose input is split across three
    # differently-billed fields, say) is money that is not in this file, and a report that
    # cannot see that says "100% of your log" over a fraction of the bill.
    imported_from: str | None = None
    source_file: str | None = None
    import_warnings: tuple[str, ...] = ()


def read(path: str | Path) -> tuple[tuple[Call, ...], LogMeta, tuple[str, ...]]:
    """``(calls, meta, warnings)`` from a JSONL capture log.

    Duplicate ids are kept, not rejected: the engine refuses them because a duplicate
    breaks its per-call reasoning, but for *spend* a repeated id is still a call that was
    billed, and dropping it would under-count. It is surfaced as a warning instead."""
    calls: list[Call] = []
    warnings: list[str] = []
    meta = LogMeta()
    seen: set[str] = set()
    dupes = 0
    bad_ts: list[int] = []

    def warn(msg: str) -> None:
        if len(warnings) < MAX_WARNINGS:
            warnings.append(msg)

    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        return (), meta, (f"could not read {path}: {exc}",)

    in_header = True
    for i, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("//"):
            if in_header:
                parsed = _parse_manifest(line)
                if parsed is not None:
                    meta = parsed
            continue
        in_header = False
        if len(line) > MAX_RECORD_CHARS:
            warn(f"line {i}: {len(line)} chars exceeds the {MAX_RECORD_CHARS} ceiling; skipped")
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            # A torn FINAL line is the one benign case (the writer appends non-atomically,
            # so a crash can only truncate the tail). Anywhere else it is real corruption
            # and the reader says so rather than quietly shrinking the total.
            where = "final line (truncated write?)" if i == len(lines) else f"line {i}"
            warn(f"{where}: invalid JSON, skipped ({exc})")
            continue
        call = _parse(row, i, warn, bad_ts)
        if call is None:
            continue
        if call.id in seen:
            dupes += 1
        seen.add(call.id)
        calls.append(call)

    if dupes:
        warn(f"{dupes} record(s) repeat an earlier id; counted for spend, since a repeated "
             f"id is still a call that was billed")
    if bad_ts:
        warn(f"{len(bad_ts):,} record(s) carried a 'ts' we could not read as a date "
             f"(first at line {bad_ts[0]}); they are treated as UNDATED — priced at "
             f"current rates and absent from the per-day chart. A date we guessed at could "
             f"select the wrong rate period, which would be a wrong total rather than a "
             f"missing bar.")
    # Import warnings lead the list rather than trail it: they describe what never reached
    # this file at all, so they qualify every figure computed from what did.
    if meta.import_warnings:
        src = f" from {meta.source_file}" if meta.source_file else ""
        source = meta.imported_from or "an unnamed source"
        warnings[:0] = [f"when this log was imported ({source}{src}): {w}"
                        for w in meta.import_warnings]
    return tuple(calls), meta, tuple(warnings[:MAX_WARNINGS])


def _parse_manifest(line: str) -> LogMeta | None:
    try:
        obj = json.loads(line[2:])
    except (json.JSONDecodeError, ValueError):
        return None                       # an ordinary comment, not our manifest
    meta = obj.get("kadari_log") if isinstance(obj, dict) else None
    if not isinstance(meta, dict):
        return None
    sample = meta.get("sample")
    return LogMeta(
        sample=float(sample) if isinstance(sample, (int, float))
        and not isinstance(sample, bool) and 0.0 <= sample <= 1.0 else None,
        wire_version=_opt_str(meta.get("version")),
        client=_opt_str(meta.get("client")),
        created=_opt_str(meta.get("created")),
        timestamps=meta.get("timestamps") if isinstance(meta.get("timestamps"), bool) else None,
        imported_from=_opt_str(meta.get("imported_from")),
        source_file=_opt_str(meta.get("source_file")),
        import_warnings=tuple(w for w in (meta.get("warnings") or ())
                              if isinstance(w, str) and w)[:MAX_WARNINGS],
    )


def _parse(row, i: int, warn, seen_bad_ts: list) -> Call | None:
    if not isinstance(row, dict):
        warn(f"line {i}: not a JSON object, skipped")
        return None
    call_id = _opt_str(row.get("id"))
    model = _opt_str(row.get("model"))
    if not model:
        warn(f"line {i}: no 'model', so the call cannot be priced; skipped")
        return None
    text = row.get("input") if isinstance(row.get("input"), str) else ""
    output = row.get("output") if isinstance(row.get("output"), str) else ""
    in_tok, out_tok, cached_tok, written_tok = _usage(row.get("usage"))
    estimated = in_tok is None or out_tok is None
    if estimated:
        # Falls back to text length. NOTE this reads LOW on a record whose text was shed
        # for size -- another reason an estimate must never be shown as a measurement.
        # No cache buckets here either: an estimate from prompt length cannot know which
        # part of that prompt was served from a cache, and splitting it would be fiction.
        in_tok, out_tok = approx_tokens(text), approx_output_tokens(output)
    # A `ts` is a RATE SELECTOR, not decoration: rate periods are compared as strings, so a
    # value that merely looks date-shaped can select the wrong period and misprice the call
    # with nothing on the page to say so. Anything we cannot read as a date is dropped to
    # undated (priced at current rates, counted under `n_undated_calls`) and reported.
    raw_ts = row.get("ts")
    ts = to_iso(raw_ts)
    if ts is None and raw_ts is not None:
        seen_bad_ts.append(i)
    return Call(
        id=call_id or f"line-{i}",
        model=model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cached_input_tokens=cached_tok,
        cache_write_tokens=written_tok,
        tokens_estimated=estimated,
        ts=ts,
        input_omitted=_opt_str(row.get("input_omitted")),
        output_omitted=_opt_str(row.get("output_omitted")),
        input_chars=len(text),
        output=output,
        n_requests=_count(row.get("n_requests")),
    )


def _usage(usage) -> tuple[int | None, int | None, int, int]:
    """``(input, output, cached, cache_write)``, or ``(None, None, 0, 0)`` when the block
    is absent or unusable.

    All-or-nothing on the metered PAIR, on purpose: half a usage block is not a
    measurement, and mixing one metered count with one estimated count inside a single call
    would make the per-call provenance flag a lie.

    The cache counts are deliberately NOT part of that pair. They are additive extras --
    absent means "this caller reported no cache activity", which is the overwhelmingly
    common case and must not invalidate a perfectly good input/output measurement. A
    malformed cache count is read as absent for the same reason: losing a cache bucket
    understates the bill for that call, while dropping the whole block downgrades a metered
    call to a text-length guess. The first is a smaller, and visible, loss."""
    if not isinstance(usage, dict):
        return None, None, 0, 0
    out = []
    for key in ("input_tokens", "output_tokens"):
        v = usage.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            return None, None, 0, 0
        out.append(int(v))
    cached, written = (_cache_count(usage.get(k))
                       for k in ("cached_input_tokens", "cache_write_tokens"))
    return out[0], out[1], cached, written


def _cache_count(v) -> int:
    """One cache bucket -> a non-negative int; anything unreadable reads as zero."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        return 0
    return int(v)


def _count(v) -> int:
    """``n_requests`` -> at least 1. An absent, malformed or zero value means "one record,
    one call", which is what every captured log is."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 1
    return max(1, int(v))


def _opt_str(v) -> str | None:
    return v if isinstance(v, str) and v else None
