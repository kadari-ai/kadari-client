"""Local capture: observe the LLM calls you ALREADY make, write them to a local log.

This is the entire customer-facing surface of Kadari. It is deliberately a thin
**capture client**: it records the call you already made (the request input, the
premium output you already paid for, any structured tool call it emitted, and the
token usage) and appends one JSONL line to a local path YOU control. An agentic call
that *acts* rather than speaks is recorded as faithfully as one that answers in prose:
the tool call is the decision, so it is kept verbatim, never inferred and never
summarised. It never re-runs the call, never uploads, and carries
**none** of Kadari's analysis engine -- scoring, the risk dial, the probe/prove logic,
the classifier model, and the benchmarks all stay server-side. So this package is safe
to read: there is no IP in it, by design. (Kadari's moat is the accumulating measured
dataset + neutrality, not this glue -- constitution §11.)

    from kadari import LiveRecorder
    rec = LiveRecorder("kadari_capture.jsonl")     # a local path you control
    resp = client.messages.create(...)              # the call you already make
    rec.record_anthropic(id=req_id, input=user_text, response=resp.to_dict())

Or wrap the call once and forget it (see ``kadari.wrap``).

THE ONE DELIBERATE INVERSION -- fail OPEN on the host, fail CLOSED on the data.
Recording is a side-channel: a bug here must NEVER raise into your production call
path, so ``record()`` swallows its own errors (warn + optional ``on_error``) and
returns ``False`` instead of raising, UNLESS ``strict=True``. The DATA stays
fail-closed: every line is validated to the stable wire schema BEFORE it is written,
and a request ``id`` already seen this session is refused, so a captured log always
loads back cleanly. Fail open on the HOST, fail closed on the DATA.

Privacy: the local log necessarily contains your request content (that is what gets
scored later) and stays on your machine -- nothing leaves it. Use ``redact=`` to scrub
inputs -- and tool-call argument values -- before they are written. Stdlib only, no
network, no third-party dependency.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import threading
import uuid
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import _safeio

# ── The stable wire contract (rule 6: the SDK contract is near-immutable) ─────
# A captured line is a flat JSON object the Kadari engine ingests verbatim:
#   {"id","model","input","output","usage":{"input_tokens","output_tokens",
#    "cached_input_tokens","cache_write_tokens"},"tool_calls":[{"name","arguments"}],"ts"}
# Everything after the first five fields is OPTIONAL and ADDITIVE: older readers ignore
# what they don't know, every existing field keeps its name, type and meaning, and a log
# written with the defaults of an older version still loads unchanged. Added in 0.2.0:
# ``tool_calls``. Added in 0.3.0: ``ts``, ``input_omitted``, ``output_omitted``, and the
# ``//``-prefixed manifest line (both readers already skip ``//`` lines, so it is
# invisible to every reader that predates it). Added in 0.4.0: the two cache counts.
#
# THE CACHE COUNTS ARE DISJOINT FROM ``input_tokens``, AND THAT IS THE WHOLE CONTRACT.
# ``input_tokens`` is the UNCACHED billable input; the three add up to the prompt. It is
# written this way because the providers disagree with each other and one of them has to
# be normalised at the edge:
#   * Anthropic already reports disjoint buckets -- ``input_tokens`` excludes both
#     ``cache_read_input_tokens`` and ``cache_creation_input_tokens``. Passed through.
#   * OpenAI reports ``prompt_tokens`` INCLUSIVE of
#     ``prompt_tokens_details.cached_tokens`` ("cached input tokens are part of the
#     request's total input tokens"). Normalised here by subtraction, ONCE, on the way in.
# Do the normalisation anywhere else and you get two conventions in one file, which is the
# same class of defect as two timestamp parsers: it does not fail, it just misprices.
# The convention matches ``prices.cost_of``, which charges the three buckets separately.
# These few helpers are the ONLY validation the client needs; the engine re-validates
# the same shape on its side (a contract test in the engine repo guards against drift).
WIRE_VERSION = "0.4"
MAX_RECORD_CHARS = 200_000   # denial-of-local-resource ceiling (fail closed above it)

# How long a record may wait for the writer lock before it is dropped instead. Recording is
# a side-channel; it is allowed to lose a row and never allowed to hold the caller. Five
# seconds is far above any honest contention on a local append and far below the point where
# a wedged lock would look like a hung request.
LOCK_TIMEOUT_S = 5.0

# ── fork safety ──────────────────────────────────────────────────────────────────
# A `threading.Lock` held at `fork()` is inherited LOCKED by the child, and the thread that
# would have released it does not exist there -- so the child's first `record()` waits on a
# lock nobody can ever unlock. Pre-fork servers (gunicorn, celery, uwsgi) hit this whenever
# a call is recorded before workers are forked. The LOCK_TIMEOUT_S above already stops that
# being fatal, but a five-second stall on every call in every worker is its own outage, so
# the lock is replaced outright in the child, where exactly one thread exists and doing so
# is safe. A WeakSet so a recorder that goes out of scope is not kept alive by this.
_LIVE_RECORDERS: "weakref.WeakSet" = weakref.WeakSet()


def _reset_locks_after_fork() -> None:
    for recorder in list(_LIVE_RECORDERS):
        recorder._lock = threading.Lock()


if hasattr(os, "register_at_fork"):     # absent on Windows
    os.register_at_fork(after_in_child=_reset_locks_after_fork)

# When a record is too large to write whole, we shed CONTENT before we shed the CALL.
# Dropping the call entirely would under-count spend exactly where the money is -- the
# largest prompts -- and a spend figure that is quietly biased low is worse than one that
# is visibly incomplete. So the text goes and the priced facts (model + usage) stay, with
# the reason recorded. We never truncate: half a prompt is evidence of nothing, and a
# downstream check that "verified" a decision against a truncated input would be lying.
OMITTED_LABEL = "kadari:omitted"

# Tool-call arguments are ATTACKER-INFLUENCEABLE content (rule 7: request content is
# data, never instructions -- and never trusted structure either). We parse them, we
# never execute or interpret them, and we bound them: a nesting bomb would otherwise
# blow the stack inside json/redaction and cost us the whole call.
MAX_ARGUMENT_DEPTH = 32

# When a response carries a tool call but NO prose, ``output`` -- a required non-empty
# field -- becomes ``tool:<name>``. The prefix is a safety device, not decoration: a
# bare tool name (``electronics``) could resolve through a taxonomy alias map and make
# an agentic call look like an in-scope classification. Prefixed, it can only ever
# resolve to UNKNOWN, so the call is counted for SPEND and never probed (fail closed).
TOOL_LABEL_PREFIX = "tool:"


class CaptureError(ValueError):
    """Raised internally on a malformed record (fail closed on the data). Never
    propagates to the host call path unless ``strict=True`` (see ``record``)."""


def _default_on_error(exc: Exception) -> None:
    try:
        print(f"kadari: dropped a record (fail-open): {exc}", file=sys.stderr)
    except Exception:  # noqa: BLE001 -- a dead/broken stderr must not break the host path
        pass


def _new_id() -> str:
    """A fresh unique id when the caller does not supply one (uniqueness is required:
    a captured log is rejected wholesale on a duplicate id)."""
    return uuid.uuid4().hex


def _unwrap_label(text: str) -> str:
    """Normalise a model output to a bare categorical label. A classifier response may
    be a bare string or a single-field structured-output object (``{"category":"X"}``);
    unwrap the latter so the written ``output`` is the label the engine expects. A
    multi-field object is left raw (the engine's scope gate treats it as out-of-scope
    rather than guess)."""
    text = text.strip()
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    if isinstance(obj, dict) and len(obj) == 1:
        (only_value,) = obj.values()
        if isinstance(only_value, str) and only_value:
            return only_value
    if isinstance(obj, str) and obj:
        return obj
    return text


def _is_json_safe(value, depth: int = 0) -> bool:
    """Is ``value`` representable as JSON *exactly*, within the depth bound?

    Rejects (rather than silently mangles) what ``json.dumps`` would coerce or emit
    invalidly: a non-string dict key, a non-finite float (``NaN``/``Infinity`` are not
    JSON and would poison a stricter reader), an SDK object a ``.model_dump()`` left
    behind (datetime, Decimal), or a structure nested past ``MAX_ARGUMENT_DEPTH``.
    Recording something *false* about a decision is worse than recording that we could
    not record it -- the caller turns a rejection into an honest ``arguments_omitted``."""
    if depth > MAX_ARGUMENT_DEPTH:
        return False
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_safe(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json_safe(v, depth + 1) for k, v in value.items())
    return False


def _tool_arguments(raw) -> tuple[dict | None, str | None]:
    """Normalise one tool call's arguments -> ``(payload, omitted_reason)``, exactly one
    of which is set.

    Providers disagree on the shape: Anthropic's ``tool_use.input`` is already an object,
    OpenAI's ``function.arguments`` is a JSON-encoded **string** that a model can and does
    emit malformed. We parse the string form, and on anything we cannot represent
    faithfully we keep the tool NAME (the signature -- *which* decision was made) and say
    why the payload is missing. We never truncate: a half-written argument value would let
    a downstream check 'verify' a decision against corrupted evidence (AP-01)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):   # malformed JSON, or a nesting bomb
            return None, "unparsed"
    if not isinstance(raw, dict):
        return None, "not_an_object"
    if not _is_json_safe(raw):
        return None, "unserializable"
    return raw, None


def _tool_entry(name: str, arguments, omitted: str | None) -> dict:
    entry: dict = {"name": name}
    if omitted is not None:
        entry["arguments_omitted"] = omitted
    else:
        entry["arguments"] = arguments
    return entry


def _normalise_tool_calls(raw_calls) -> list[dict]:
    """Coerce caller-supplied or adapter-extracted tool calls to the wire shape.

    Shared by ``record()`` and both provider adapters so the raw path and the adapters
    cannot drift (the same reason ``_count_or_none`` is shared). Idempotent: re-running
    it over already-normalised entries preserves them, including an existing
    ``arguments_omitted`` reason. An entry with no usable name yields nothing -- a
    nameless tool block is a malformed provider response, not a decision."""
    entries: list[dict] = []
    if not isinstance(raw_calls, (list, tuple)):
        return entries
    for call in raw_calls:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        if not isinstance(name, str) or not name:
            continue
        if "arguments" not in call and isinstance(call.get("arguments_omitted"), str) \
                and call["arguments_omitted"]:
            entries.append(_tool_entry(name, None, call["arguments_omitted"]))
            continue
        args, omitted = _tool_arguments(call.get("arguments"))
        entries.append(_tool_entry(name, args, omitted))
    return entries


def _redact_values(value, redact: Callable[[str], str]):
    """Apply the caller's redactor to every string LEAF of a tool-call payload.

    Keys and tool names are deliberately NOT redacted: they are schema (the decision's
    field names / which action was taken), and scrubbing them would destroy the very
    structure the decision consists of. Values are content, and content can carry PII --
    so the privacy control the customer configured must reach them (rule 10). Depth is
    already bounded by ``_is_json_safe``, so this walk is bounded too."""
    if isinstance(value, str):
        out = redact(value)
        if not isinstance(out, str):
            raise CaptureError("redact must return a string")
        return out
    if isinstance(value, list):
        return [_redact_values(v, redact) for v in value]
    if isinstance(value, dict):
        return {k: _redact_values(v, redact) for k, v in value.items()}
    return value


def _drop_arguments(entries: list[dict]) -> list[dict]:
    """Keep every tool NAME, drop every payload -- the last honest step before an
    oversize line would cost us the entire call."""
    return [{"name": e["name"], "arguments_omitted": "oversize"} for e in entries]


def _req_str(data: dict, field: str, *, allow_empty: bool = False) -> str:
    v = data.get(field)
    if not isinstance(v, str) or (not v and not allow_empty):
        kind = "string" if allow_empty else "non-empty string"
        raise CaptureError(f"{field!r} is required and must be a {kind}")
    return v


def _count_or_none(v) -> int | None:
    """A provider token count -> ``int | None``. Accepts an integral float (some SDKs
    serialise ``1234.0``); anything else (non-integral, negative, bool, None) becomes
    None so the engine falls back to its honest estimate instead of dropping the call."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, float) and v.is_integer() and v >= 0:
        return int(v)
    return None


def _client_version() -> str:
    """The shipped package version, read lazily so this module stays import-cycle free."""
    from . import __version__
    return __version__


def _utc_now() -> str:
    """Second-resolution UTC timestamp, e.g. ``2026-08-05T14:03:11Z``.

    NOTE the deliberate exception this makes: a record is otherwise a pure function of the
    response (rule 9), and a wall-clock read is not. That is acceptable here because a
    timestamp is an *observation* about the call, not part of the decision being replayed
    -- rule 9 governs decisions. It is injectable (``LiveRecorder(_now=...)``) so tests and
    determinism checks stay exact, and it can be turned off entirely (``timestamps=False``).
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_row(row: dict) -> None:
    """Validate a flat capture row against the wire contract (fail closed)."""
    _req_str(row, "id")
    _req_str(row, "model")
    _req_str(row, "input", allow_empty=True)
    _req_str(row, "output")
    if "ts" in row:
        _req_str(row, "ts")
    # An omission reason is a claim about missing content -- it must not be attachable to
    # content that is actually present, or a reader could not trust either field.
    if "input_omitted" in row:
        _req_str(row, "input_omitted")
        if row["input"]:
            raise CaptureError("'input_omitted' is only valid when 'input' is empty")
    if "output_omitted" in row:
        _req_str(row, "output_omitted")
        if row["output"] != OMITTED_LABEL:
            raise CaptureError(
                f"'output_omitted' is only valid when 'output' is {OMITTED_LABEL!r}")
    usage = row.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise CaptureError("usage must be an object when present")
        for k in ("input_tokens", "output_tokens"):
            v = usage.get(k)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise CaptureError(f"usage.{k} must be a non-negative integer")
        # The cache counts are optional (a log from a caller that never caches simply has
        # none) but never zero-filled: an absent count means "not reported", and writing a
        # 0 we did not observe would claim the customer's traffic was uncached.
        for k in ("cached_input_tokens", "cache_write_tokens"):
            if k in usage:
                v = usage.get(k)
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    raise CaptureError(f"usage.{k} must be a non-negative integer")
    tool_calls = row.get("tool_calls")
    if tool_calls is not None:
        # Never written empty: a log with no tool call is byte-identical to a pre-0.2.0
        # one, so the new field can only ever mean "a decision was actually recorded".
        if not isinstance(tool_calls, list) or not tool_calls:
            raise CaptureError("tool_calls must be a non-empty array when present")
        for entry in tool_calls:
            if not isinstance(entry, dict):
                raise CaptureError("each tool_calls entry must be an object")
            _req_str(entry, "name")
            has_args = "arguments" in entry
            if has_args and not isinstance(entry["arguments"], dict):
                raise CaptureError("tool_calls[].arguments must be an object when present")
            if "arguments_omitted" in entry:
                if has_args:
                    raise CaptureError("a tool_calls entry carries either 'arguments' or "
                                       "'arguments_omitted', never both")
                _req_str(entry, "arguments_omitted")
            elif not has_args:
                raise CaptureError("a tool_calls entry needs 'arguments' or "
                                   "'arguments_omitted' (fail closed: never imply an "
                                   "empty decision payload we did not observe)")


class LiveRecorder:
    """Append observed LLM calls to a local JSONL log, in-process and fail-open.

    One instance owns one log file. Writes are append-only and lock-guarded so concurrent
    threads never interleave a line; each call is handed to the OS on return. There is no
    fsync, so a hard crash / power-loss can truncate the FINAL line in flight -- the engine
    ingest tolerates exactly that one torn trailing line, so prior records are never lost.
    For multiple PROCESSES, give each its own log path.

    Long-lived use: an instance keeps an in-memory set of recorded ids (so a retried id
    can't make the whole log unloadable -- the engine rejects a duplicate id wholesale).
    That set grows with the number of DISTINCT ids recorded; for a recorder embedded in a
    busy long-running process, recycle it per log file (e.g. daily) -- which also bounds the
    on-disk log -- so neither the set nor the file grows without limit. ``sample=`` bounds
    both together.

    A fresh log opens with a ``//``-prefixed JSON **manifest line** recording the wire
    version, the client version and — load-bearing — the ``sample`` rate in force. Without
    it a sampled log is indistinguishable from a complete one, and any spend computed from
    it is silently wrong by a factor of ``1/sample``. Both readers already skip ``//``
    lines, so this is invisible to anything that predates it. The manifest is written only
    when the file is empty; if you re-open an existing log with a *different* ``sample``,
    the original manifest still stands and will misdescribe the new records — give each
    sampling regime its own log file.

    Options:
      * ``strict`` -- raise instead of swallowing (tests/dev).
      * ``sample`` -- record only this fraction of calls (0..1) to bound log growth.
        Recorded in the manifest so a reader knows what it is holding.
      * ``timestamps`` -- attach a second-resolution UTC ``ts`` to each record (default
        on). Without it a report can describe composition but never trend. Turn it off if
        call timing is itself sensitive.
      * ``redact`` -- a ``str -> str`` applied to the INPUT, and to every string VALUE of a
        recorded tool call's arguments, before they are written (scrub PII). Tool names and
        argument KEYS are left alone: they are the decision's structure, not its content.
        It runs inside the fail-open guard, so a faulty redactor never breaks the host call
        -- but a redactor that raises drops that record rather than writing un-redacted
        content (fail closed on the data).
      * ``on_error`` -- called with the dropped record's exception (default: warn to stderr).
      * ``_now`` -- injectable clock (tests/determinism); returns the ``ts`` string.
    """

    def __init__(self, path: str | Path, *, strict: bool = False,
                 sample: float = 1.0, redact: Callable[[str], str] | None = None,
                 max_chars: int = MAX_RECORD_CHARS, timestamps: bool = True,
                 on_error: Callable[[Exception], None] | None = None,
                 _now: Callable[[], str] | None = None) -> None:
        if not 0.0 <= sample <= 1.0:
            raise ValueError("sample must be in [0, 1]")
        self.path = Path(path)
        self.strict = strict
        self.sample = sample
        self.redact = redact
        self.max_chars = max_chars
        self.timestamps = timestamps
        self.on_error = on_error or _default_on_error
        self._now = _now or _utc_now
        self._lock = threading.Lock()
        _LIVE_RECORDERS.add(self)       # so a fork can re-create the lock in the child
        self._seen_ids: set[str] = set()
        self._manifest_done = False
        self._rng = random.Random()   # sampling only; never touches the recorded data

    def _manifest_line(self) -> str:
        """The ``//`` header describing the log itself (see the class docstring)."""
        meta = {"version": WIRE_VERSION, "client": f"kadari/{_client_version()}",
                "sample": self.sample, "timestamps": self.timestamps}
        if self.timestamps:
            meta["created"] = self._now()
        return "// " + json.dumps({"kadari_log": meta}, ensure_ascii=False) + "\n"

    # ── raw record (provider-agnostic) ───────────────────────────────────────
    def record(self, *, input: str, output: str, model: str, id: str | None = None,
               input_tokens: int | None = None, output_tokens: int | None = None,
               cached_input_tokens: int | None = None,
               cache_write_tokens: int | None = None,
               tool_calls: list[dict] | None = None) -> bool:
        """Append one observed call. Returns True iff a line was written.

        Fail-OPEN by default: any error (bad data, oversize, unwritable path, a faulty
        redactor) is swallowed (``on_error`` + return False) so it never propagates into
        the host's production path. ``strict=True`` re-raises instead. ``id`` defaults to
        a fresh uuid; pass your own request/correlation id if you have a unique one.

        ``cached_input_tokens`` / ``cache_write_tokens`` are the cache buckets, and they
        must be DISJOINT from ``input_tokens`` (see the wire contract at the top of this
        module). If you are wiring a provider by hand rather than via
        ``record_openai`` / ``record_anthropic``, check which convention your provider
        reports in: pass the uncached remainder as ``input_tokens``, not the prompt total.
        Getting this backwards double-counts the cached prefix, which on cache-heavy
        traffic is most of the bill.

        ``tool_calls`` records the structured decision(s) the model already emitted, as
        ``[{"name": "refund_order", "arguments": {...}}]`` -- ``arguments`` may be the
        object itself or the JSON string OpenAI hands you; both normalise the same way.
        Pass what the model returned, verbatim; never a decision you inferred from prose."""
        try:
            if self.sample < 1.0 and self._rng.random() >= self.sample:
                return False   # sampled out -- not an error
            call_id = id if id is not None else _new_id()
            text = self.redact(input) if self.redact is not None else input
            if not isinstance(text, str):
                raise CaptureError("redact must return a string")
            entries = _normalise_tool_calls(tool_calls)
            if entries and self.redact is not None:
                # The redactor the customer configured must reach argument VALUES too --
                # they carry order ids, emails, addresses. Skipping them would open a new
                # PII channel straight past a control they already set (rule 10).
                entries = [dict(e, arguments=_redact_values(e["arguments"], self.redact))
                           if "arguments" in e else e for e in entries]
            row: dict = {"id": call_id, "model": model, "input": text, "output": output}
            if self.timestamps:
                row["ts"] = self._now()
            # Attach usage ONLY when BOTH counts are usable: the engine honors a usage block
            # only if both are set, so a partial block would be silently dropped. Coerce
            # through the SAME normaliser the provider adapters use (an integral float like
            # 1234.0 -> 1234; a non-integral/negative/bool/None -> dropped) so the raw
            # record() path and record_openai/record_anthropic agree, and a single odd count
            # drops just the usage block (the engine falls back to an estimate) instead of
            # the whole call.
            in_tok, out_tok = _count_or_none(input_tokens), _count_or_none(output_tokens)
            if in_tok is not None and out_tok is not None:
                row["usage"] = {"input_tokens": in_tok, "output_tokens": out_tok}
                # Attached only when actually observed. These ride ALONGSIDE the
                # all-or-nothing pair rather than joining it: a provider that meters
                # input/output but reports no cache counts is the ordinary case, and
                # dropping its whole usage block over an absent cache field would throw
                # away a real measurement in favour of a text-length guess.
                for key, value in (("cached_input_tokens", cached_input_tokens),
                                   ("cache_write_tokens", cache_write_tokens)):
                    count = _count_or_none(value)
                    if count is not None:
                        row["usage"][key] = count
            if entries:
                row["tool_calls"] = entries
            _validate_row(row)
            line = json.dumps(row, ensure_ascii=False)
            if len(line) > self.max_chars and entries:
                # Adding decisions must never SUBTRACT calls. A big argument payload would
                # otherwise push a call that captured fine before 0.2.0 over the shared line
                # ceiling and drop it whole. Shed the payloads, keep every tool name, say so.
                row["tool_calls"] = _drop_arguments(entries)
                _validate_row(row)
                line = json.dumps(row, ensure_ascii=False)
            # Shed CONTENT before shedding the CALL (see OMITTED_LABEL). Losing the text
            # loses what we could analyse; losing the record loses what it COST, and biases
            # the spend total low precisely on the biggest calls.
            if len(line) > self.max_chars and row["input"]:
                row["input"] = ""
                row["input_omitted"] = "oversize"
                _validate_row(row)
                line = json.dumps(row, ensure_ascii=False)
            if len(line) > self.max_chars and row["output"] != OMITTED_LABEL:
                row["output"] = OMITTED_LABEL
                row["output_omitted"] = "oversize"
                _validate_row(row)
                line = json.dumps(row, ensure_ascii=False)
            if len(line) > self.max_chars:
                raise CaptureError(
                    f"record is {len(line)} chars (> {self.max_chars}); refusing to "
                    f"write an abnormally large line")
            # Bounded, never indefinite. Blocking is a worse failure than dropping: a
            # dropped record costs one row in a spend report, a held lock costs the host's
            # request. Anything that wedges this lock -- a fork that raced a write, a
            # writer stalled on a full disk -- times out into the ordinary fail-open path.
            if not self._lock.acquire(timeout=LOCK_TIMEOUT_S):
                raise CaptureError(
                    f"could not acquire the recorder lock within {LOCK_TIMEOUT_S}s; "
                    f"dropping this record rather than holding the caller")
            try:
                if call_id in self._seen_ids:
                    raise CaptureError(
                        f"duplicate id {call_id!r}: already recorded this session "
                        f"(a captured log is rejected on a repeated id)")
                _safeio.secure_makedirs(self.path.parent)
                # Manifest first, and only into an EMPTY file: appending one mid-log would
                # describe records it does not cover.
                header = ""
                if not self._manifest_done:
                    self._manifest_done = True
                    if not self.path.exists() or self.path.stat().st_size == 0:
                        header = self._manifest_line()
                # append = crash-safe; owner-only on creation, and never through a symlink
                # or onto a pipe (see kadari._safeio).
                with _safeio.secure_open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(header + line + "\n")
                self._seen_ids.add(call_id)
            finally:
                self._lock.release()
            return True
        except Exception as exc:  # noqa: BLE001 -- fail OPEN: never break the host call
            if self.strict:
                raise
            return self._notify(exc)

    def _notify(self, exc: Exception) -> bool:
        """Run ``on_error`` without letting a faulty handler -- or a dead stderr in the
        default -- propagate into the host's production path. Always returns False."""
        try:
            self.on_error(exc)
        except Exception:  # noqa: BLE001 -- a broken hook must not break the host call
            pass
        return False

    # ── provider convenience adapters ────────────────────────────────────────
    def record_anthropic(self, *, input: str, response: dict, id: str | None = None) -> bool:
        """Record from an Anthropic Messages response body (a dict / ``.to_dict()``)."""
        try:
            model, output, in_tok, out_tok, cached, written, tools = _from_anthropic(response)
        except Exception as exc:  # noqa: BLE001 -- fail open, same contract as record()
            if self.strict:
                raise
            return self._notify(exc)
        return self.record(id=id, input=input, output=output, model=model,
                           input_tokens=in_tok, output_tokens=out_tok,
                           cached_input_tokens=cached, cache_write_tokens=written,
                           tool_calls=tools)

    def record_openai(self, *, input: str, response: dict, id: str | None = None) -> bool:
        """Record from an OpenAI Chat Completions response body (a dict / ``.to_dict()``)."""
        try:
            model, output, in_tok, out_tok, cached, written, tools = _from_openai(response)
        except Exception as exc:  # noqa: BLE001 -- fail open, same contract as record()
            if self.strict:
                raise
            return self._notify(exc)
        return self.record(id=id, input=input, output=output, model=model,
                           input_tokens=in_tok, output_tokens=out_tok,
                           cached_input_tokens=cached, cache_write_tokens=written,
                           tool_calls=tools)


# ── response extractors (shared label convention with the engine ingest) ──────
def _output_label(prose: str | None, tools: list[dict]) -> str:
    """The required, non-empty ``output`` for one response.

    Prose wins when there is any: a reply that both explains AND acts keeps its prose in
    ``output`` and its decision in ``tool_calls`` -- unchanged semantics for every call
    that already captured, and exactly the (decision, prose) pair the render mode needs.
    A response that only ACTS has no prose, so ``output`` names the action taken."""
    if isinstance(prose, str) and prose.strip():
        return _unwrap_label(prose)
    if tools:
        return f"{TOOL_LABEL_PREFIX}{tools[0]['name']}"
    raise CaptureError("response carries neither output text nor a named tool call")


def _text_parts(content) -> str | None:
    """Join every text part of a content-block list, in order (``None`` if there are none).

    All of them, not the first: the prose is the measured object for the render mode, so
    silently keeping half of a two-block reply would bias it. Order is preserved -- a
    record is a pure function of the response (rule 9)."""
    if not isinstance(content, list):
        return None
    parts = [b["text"] for b in content
             if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
    return "\n".join(parts) if parts else None


def _from_anthropic(
    response: dict,
) -> tuple[str, str, int | None, int | None, int | None, int | None, list[dict]]:
    """Extract the priceable facts from an Anthropic Messages response.

    Anthropic's buckets are ALREADY disjoint -- ``input_tokens`` is the uncached
    remainder, with cache reads and cache writes reported beside it -- so the counts pass
    through untouched. That is not an accident of this adapter; it is the convention the
    wire format adopted, precisely because it is the one that can represent both providers
    without losing information."""
    if not isinstance(response, dict):
        raise CaptureError("anthropic response must be a dict (pass resp.to_dict())")
    model = response.get("model")
    if not isinstance(model, str):
        raise CaptureError("anthropic response missing model")
    content = response.get("content")
    text = _text_parts(content)
    tools = _normalise_tool_calls(
        [{"name": b.get("name"), "arguments": b.get("input")} for b in content
         if isinstance(b, dict) and b.get("type") == "tool_use"]
        if isinstance(content, list) else [])
    output = _output_label(text, tools)
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    return (model, output,
            _count_or_none(usage.get("input_tokens")), _count_or_none(usage.get("output_tokens")),
            _count_or_none(usage.get("cache_read_input_tokens")),
            _count_or_none(usage.get("cache_creation_input_tokens")),
            tools)


def _from_openai(
    response: dict,
) -> tuple[str, str, int | None, int | None, int | None, int | None, list[dict]]:
    """Extract the priceable facts from an OpenAI Chat Completions response.

    THE SUBTRACTION HERE IS THE POINT. OpenAI's ``prompt_tokens`` is inclusive of
    ``prompt_tokens_details.cached_tokens``, so handing it to a per-bucket price function
    charges the cached prefix twice -- once at the full input rate and once at the cached
    rate. On cache-heavy traffic the cached prefix IS the prompt, so the reported bill runs
    several times the real one. We subtract once, here, at the edge.

    ``cache_write_tokens`` (GPT-5.6 and later) is carried but NOT subtracted, because
    whether it names a distinct bucket or overlaps the uncached remainder is not something
    OpenAI has documented -- their own examples report parts that exceed the whole, which
    they have acknowledged as an accounting bug. Subtracting on that basis could drive
    ``input_tokens`` negative on real traffic. So the count rides along as an observation,
    the report prices what is specified and says the write premium is not included, and
    nobody has to guess (P1). Revisit when the arithmetic is published, not before."""
    if not isinstance(response, dict):
        raise CaptureError("openai response must be a dict (pass resp.to_dict())")
    model = response.get("model")
    if not isinstance(model, str):
        raise CaptureError("openai response missing model")
    choices = response.get("choices")
    message = choices[0].get("message") if isinstance(choices, list) and choices \
        and isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    # ``content`` is a string or null on Chat Completions; a content-block list is
    # tolerated because some SDK dumps / gateways emit one, and capturing the prose beats
    # dropping the call over a shape difference.
    text = content if isinstance(content, str) else _text_parts(content)
    raw_calls = message.get("tool_calls") if isinstance(message, dict) else None
    tools = _normalise_tool_calls(
        [{"name": c["function"].get("name"), "arguments": c["function"].get("arguments")}
         for c in raw_calls
         if isinstance(c, dict) and isinstance(c.get("function"), dict)]
        if isinstance(raw_calls, list) else [])
    output = _output_label(text, tools)
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    details = usage.get("prompt_tokens_details")
    cached = _count_or_none(details.get("cached_tokens")) if isinstance(details, dict) else None
    prompt = _count_or_none(usage.get("prompt_tokens"))
    uncached = prompt
    if prompt is not None and cached is not None:
        if cached > prompt:
            # Incoherent: the cached subset cannot exceed the total it is a subset of.
            # Drop BOTH counts rather than clamp -- a clamp would silently invent an
            # all-cached call (billing the whole prompt at a tenth of the rate) out of a
            # block we have just proven we cannot read. Without an input count the record
            # falls back to a text-length estimate, which is flagged as an estimate and so
            # can never masquerade as metered.
            uncached, cached = None, None
        else:
            uncached = prompt - cached
    return (model, output,
            uncached, _count_or_none(usage.get("completion_tokens")),
            cached, _count_or_none(usage.get("cache_write_tokens")),
            tools)
