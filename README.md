# kadari — the local capture client

`kadari` is the only thing Kadari ships to you. It's a **thin, zero-dependency Python
library** that records the LLM calls you *already make* to a **local** log. Kadari's
engine (which stays on Kadari's side) then analyses that log and shows you, with proof,
where you could safely route to a smaller in-family model — without ever re-running a
call or crossing a quality bar you set.

This package carries **none of Kadari's analysis engine** — no scoring, no classifier
model, no benchmarks. It is capture glue, by design. Read every line.

## Install

Zero runtime dependencies (stdlib only):

```
pip install kadari
```

Or from a local checkout of **this package** — the directory holding the `pyproject.toml`
whose first line reads `name = "kadari"` — or from a wheel handed to you:

```
pip install /path/to/kadari
# or:  pip install kadari-0.3.0-py3-none-any.whl
```

*The path is spelled out rather than written as `pip install .` on purpose. This README
ships in two places — the public `kadari` repository, where the package is the repository
root, and inside Kadari's engine monorepo, where it sits one level down. A bare `.` means a
different package in each, and in the monorepo it means the engine.*

## Use

```python
from kadari import LiveRecorder, wrap

rec = LiveRecorder("kadari_capture.jsonl")   # a local path you control

# Option A — record explicitly, right after a call you already make:
resp = client.messages.create(model="claude-opus-4-8", messages=[{"role": "user", "content": text}])
rec.record_anthropic(id=req_id, input=text, response=resp.to_dict())
# OpenAI: rec.record_openai(id=req_id, input=text, response=resp.to_dict())

# Option B — wrap the call once and capture every call automatically:
create = wrap(client.chat.completions.create, provider="openai", recorder=rec)
resp = create(model="gpt-5.4-nano", messages=[{"role": "user", "content": text}])
```

## See what it costs

You don't need to capture anything to try it. The package ships a synthetic sample:

```
kadari demo -o report.html
```

Then on your own log:

```
kadari analyze kadari_capture.jsonl -o report.html
```

You get a single self-contained HTML file — no JavaScript, no network, opens offline and
prints clean — showing spend by model, spend per day, how concentrated the bill is, what
share of it we could actually price, and how much of it is metered rather than estimated.

**Already have months of billing history?** Import it instead of instrumenting anything:

```
kadari import openai-usage-export.csv --from openai-usage -o kadari_capture.jsonl
kadari analyze kadari_capture.jsonl -o report.html
```

Sources: `openai-usage`, `anthropic-usage`, `csv` (any table with model and token
columns — names are matched loosely), and `gateway` (JSONL from LiteLLM, Helicone and
similar). One thing worth knowing before you try: **a provider usage export has no prompt
text.** It makes a complete spend report and it cannot be evaluated for savings, because
an evaluation has to re-run your calls and compare against the answer you already paid
for. A gateway log often does carry the text, in which case it works for both.

## The part the free report can't do

The report tells you what you spent, and — from published rates — what the same tokens
would have cost on the smallest model in your provider's family. That difference is a
**ceiling**, not a saving. Nothing local has looked at whether the smaller model would
have given an acceptable answer on your task.

That is the question Kadari measures. If you want it answered:

```
kadari bundle kadari_capture.jsonl --redact
```

This writes a zip **next to your log and sends nothing** — the client opens no network
connection, and it is not going to start now. It prints exactly what went into the
archive, runs a preflight that tells you up front whether the log is one we can actually
evaluate, and points you at the upload page. You decide whether to send it.

Set expectations before you do: the first report's *proven* savings figure is **$0.00 by
design**. Until enough of your own calls have been checked, we have not earned the right
to claim a number on your task, and we would rather say so than estimate one.

## Safety contract

- **Fail open on the host, fail closed on the data.** Recording is a side-channel: a bug
  here will **never** raise into your production call path — `record()` warns and returns
  `False` instead of throwing (unless you pass `strict=True`). What *does* get written is
  always valid and loadable.
- **Local-first.** This library never opens a network connection. Your prompts and outputs
  stay in the local file you named; nothing is uploaded by `kadari`.
- **Owner-only on disk.** "Local" is not the same as "private", so since 0.3.1 every file
  this tool creates — the capture log, the `bundle` zip, the rendered report, an imported
  log — is created `0600`, inside directories created `0700`. Before 0.3.1 they inherited
  the process umask, which on the usual default meant **world-readable**: any other account
  on a shared host, a sidecar sharing the volume, or a backup agent running as another uid
  could read your production prompts.
  A file you created yourself keeps whatever mode you gave it — tightening applies only to
  files `kadari` creates, because a mode you set deliberately is a decision, not a mistake.
  The log path is also never followed through a symlink, and must be a regular file.
- **Privacy controls.** `redact=` scrubs each input — and each tool-call argument value —
  before it is written; `sample=` records only a fraction of calls to bound log size.
  `redact=` applies to what is *written*; `kadari bundle --redact` additionally scrubs the
  log, the manifest and the text report on the way into the archive. Both are best-effort
  pattern matching (emails, IPs, card- and phone-like digit runs, token-shaped strings) —
  a convenience, **not** a compliance control. Neither can recognise a name or an internal
  identifier it has no pattern for.

```python
rec = LiveRecorder("kadari_capture.jsonl", sample=0.1, redact=my_scrubber)
```

## Wire format — the stable SDK contract

The on-disk JSONL log is the **contract** between this client and Kadari's engine. It is
**near-immutable**: backward-compatible within a major version; a breaking change requires a
new major version and a documented migration path (Kadari rule 6). One JSON object per line:

```json
// {"kadari_log": {"version": "0.4", "client": "kadari/0.3.0", "sample": 1.0, "timestamps": true, "created": "2026-08-05T09:12:44Z"}}
{"id": "req-123", "model": "gpt-5.4-nano", "input": "...", "output": "Electronics",
 "usage": {"input_tokens": 64, "output_tokens": 2}, "ts": "2026-08-05T09:12:44Z"}
```

| field | type | required | meaning |
|-------|------|----------|---------|
| `id` | string | yes | unique per log — a captured log is **rejected wholesale** on a duplicate id. If you don't pass one, a uuid is generated, and a reused id is refused at write time so one retry can't make the whole log unloadable. |
| `model` | string | yes | the premium provider model string you actually called (e.g. `gpt-5.5`, `claude-opus-4-8`) — priced verbatim. |
| `input` | string | yes (may be empty) | the request text that gets scored/re-classified cheaply. |
| `output` | string | yes | the label or prose the premium model already returned. The provider adapters (`record_openai`/`record_anthropic`/`wrap`) unwrap a single-field structured object `{"category": "X"}` to `X`; the raw `record(output=...)` path writes the string **verbatim**, so pass a bare label there. When a response carries a tool call and **no** prose, the adapters write `tool:<name>` (see below). |
| `usage` | object | optional | `{"input_tokens", "output_tokens"}`, both non-negative integers. Attached **only when both are present** — a half block is dropped, since the engine honours usage only if both are set. When omitted, Kadari estimates spend from text length **and marks the figure as estimated** — measured and estimated tokens are never summed into one number presented as measured. |
| `usage.cached_input_tokens` | int | optional (since 0.4.0) | prompt tokens served from the provider's cache. **Disjoint from `input_tokens`** — see below. |
| `usage.cache_write_tokens` | int | optional (since 0.4.0) | prompt tokens written *into* the cache. Also disjoint from `input_tokens`. |
| `ts` | string | optional (since 0.3.0) | second-resolution UTC, `2026-08-05T09:12:44Z`. On by default; `LiveRecorder(timestamps=False)` turns it off. Without it a report can describe what you spend on, but never how it is trending. |
| `input_omitted` | string | optional (since 0.3.0) | why `input` is empty — currently only `oversize`. See "Records too large to write" below. Valid only when `input` is empty. |
| `output_omitted` | string | optional (since 0.3.0) | why `output` is the sentinel `kadari:omitted`. Valid only alongside that sentinel. |
| `tool_calls` | array | optional (since 0.2.0) | the structured decision(s) the model already emitted, in provider order. Never written empty — a log with no tool call is byte-identical to one written before this field existed. |
| `tool_calls[].name` | string | yes (per entry) | the tool/function name — *which* decision was made. |
| `tool_calls[].arguments` | object | optional | the arguments, verbatim, with their original JSON types. Present unless `arguments_omitted` is. |
| `tool_calls[].arguments_omitted` | string | optional | why the payload is absent: `oversize`, `unparsed`, `not_an_object`, `unserializable`. Mutually exclusive with `arguments`. **Readers must tolerate a reason they don't recognise.** |

Comment lines (`//…`) and blank lines are ignored by the reader.

### The cache buckets are disjoint from `input_tokens`

`input_tokens` means the **uncached billable input**. The three input counts are mutually
exclusive and add up to the prompt:

```
prompt = input_tokens + cached_input_tokens + cache_write_tokens
```

This matters because the two providers report the same facts under opposite conventions,
and one of them has to be normalised somewhere:

- **Anthropic** already reports disjoint buckets — its `input_tokens` excludes both
  `cache_read_input_tokens` and `cache_creation_input_tokens`. Passed through unchanged.
- **OpenAI** reports `prompt_tokens` **inclusive** of
  `prompt_tokens_details.cached_tokens`. `record_openai` subtracts, once, on the way in.

`record_openai` / `record_anthropic` / `wrap` handle this for you. If you are wiring a
provider by hand through the raw `record()` call, pass the **uncached remainder** as
`input_tokens` — not the prompt total. Getting it backwards double-counts the cached
prefix, and on cache-heavy traffic that prefix is most of the bill.

A cache read bills at roughly a tenth of an uncached input token and a cache write at
more than one, so these are never summed into a single "input tokens" figure: the sum
would correspond to no charge on your invoice. Both fields are optional and are **never
zero-filled** — an absent count means "not reported", whereas a `0` would assert the
traffic was uncached. A log written before 0.4.0 therefore loads unchanged.

### The manifest line — what the log says about itself

Since 0.3.0 a **fresh** log opens with a single `//`-prefixed JSON line describing the log
rather than any one call. Because both readers already skipped `//` lines, this is
invisible to every reader that predates it.

`sample` is the field that matters. A log captured at `sample=0.1` holds a tenth of your
traffic and therefore a tenth of your spend — and without the manifest it is
indistinguishable from a complete one, so any total computed from it is wrong by 10× with
nothing on the page to say so. A log with **no** manifest reports its sample rate as
*unknown*, never as 1.0: "we weren't told" and "we were told it's complete" are different
claims and we won't collapse them.

The manifest is written only into an empty file. If you re-open an existing log with a
different `sample`, the original manifest still stands and will misdescribe the new
records — **give each sampling regime its own log file.**

### Records too large to write

A record that would exceed the 200,000-character line ceiling sheds **content** before it
sheds the **call**, in this order: tool-call arguments → `input` → `output`. Each step
records why (`arguments_omitted` / `input_omitted` / `output_omitted`), and the priced
facts — `model` and `usage` — survive every step.

The reason is arithmetic, not tidiness. Dropping the record entirely would remove your
largest prompts from the total, and those are your most expensive calls — so the error
would be both silent and biased low, which is the worst combination a spend figure can
have. Nothing is ever **truncated**: half a prompt is evidence of nothing, and a later
check that "verified" a decision against a truncated input would be lying. If even the
priced skeleton won't fit, the record is refused outright and nothing partial is written.

**Compatibility guarantees within a major version:**

- the five REQUIRED fields above keep their names, types and meaning;
- new **optional** fields may be added; readers ignore unknown fields, so a newer client log
  still loads in an older engine and vice-versa;
- no required field is removed or repurposed, and no field type changes.

### Tool calls — the structured decision

If your premium call already emits a **tool call** (an agentic support agent deciding to
issue a refund, look up an order, escalate), that tool call *is* the decision — and it is
the part worth measuring. The adapters record it automatically; nothing to configure:

```json
{"id": "req-9", "model": "gpt-5.5", "input": "where is my order 118?",
 "output": "I've started your refund — 3–5 business days.",
 "usage": {"input_tokens": 812, "output_tokens": 96},
 "tool_calls": [{"name": "refund_order", "arguments": {"order_id": "A-118", "amount_cents": 4000}}]}
```

- **A reply that explains *and* acts keeps both halves** — prose in `output`, decision in
  `tool_calls`. Nothing about `output` changed for calls that already captured.
- **A reply that only acts** has no prose, so `output` becomes `tool:<first tool name>` —
  `output` is required and non-empty, and the `tool:` prefix keeps an agentic call from
  being mistaken for a classification label.
- **Every tool call is recorded**, in the order the provider returned them — not just the first.
- **We only record a decision the model already emitted.** Kadari never infers one from
  prose: an inferred decision is a guess wearing a proof's clothing.
- Arguments are recorded **verbatim or not at all** — never truncated. If they can't be
  represented faithfully (malformed JSON from the model, a value that isn't JSON, a payload
  too large for the line ceiling), the entry keeps the tool `name` and states the reason in
  `arguments_omitted`. Losing the payload never loses the call.
- `redact=` **applies to argument values** as well as `input` (keys and tool names are
  structure, not content, and are left alone). One consequence worth knowing: if you redact
  a value that also appears in the reply text, Kadari's checks can no longer match the two —
  which is the honest outcome, not a silent one.

This is enforced, not promised: the client never imports engine code, but a contract test in
the engine repo (`tests/test_kadari_client.py`) writes a log with **this** client and asserts
it loads unchanged through the engine's reader — so the two halves can never silently drift.
The engine accepts one additional **input** shape (a nested Anthropic Messages
`{"id","request","response"}` transcript); this client always emits the flattened shape above.
