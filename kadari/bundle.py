"""Package a log for submission — locally, with the contents printed before it moves.

Nothing here opens a network connection. It writes a zip next to the log and tells you
exactly what went into it; uploading is a separate, deliberate act you perform yourself.
That is the whole reason this file exists rather than a ``kadari submit`` command: the
client's strongest claim is that it never transmits anything, and a tool that could
transmit has a weaker claim even on the runs where it doesn't.

The bundle carries three things, and nothing else:

* the capture log (optionally scrubbed),
* a manifest naming every file, its size, and the preflight verdict,
* the text report, so we can see the same numbers the sender was looking at.

The manifest is printed to the terminal, not just written into the archive. Someone about
to hand over production prompts should be able to read what they are handing over without
unzipping anything.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

from . import __version__, _safeio, logs

# Best-effort pattern scrubbing. Stated plainly: this is a convenience, NOT a compliance
# control. It catches the shapes that show up in support and catalog traffic; it cannot
# know that "Project Bluebird" is a customer name. A caller who needs a guarantee should
# redact at capture time with a scrubber that understands their data (`redact=`).
_PATTERNS = (
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("phone", re.compile(r"\+?\d[\d ()-]{8,}\d")),
    ("secret", re.compile(r"\b(?:sk|pk|ghp|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b")),
)


def redact_text(text: str) -> tuple[str, dict]:
    """Scrub the known shapes; return the text and a count of what was replaced."""
    hits: dict[str, int] = {}
    for name, rx in _PATTERNS:
        text, n = rx.subn(f"[{name} redacted]", text)
        if n:
            hits[name] = hits.get(name, 0) + n
    return text, hits


def _scrubber(hits: dict):
    """A recursive scrubber that accumulates its hit counts into ``hits``."""

    def scrub(v):
        if isinstance(v, str):
            out, h = redact_text(v)
            for k, n in h.items():
                hits[k] = hits.get(k, 0) + n
            return out
        if isinstance(v, list):
            return [scrub(x) for x in v]
        if isinstance(v, dict):
            # Keys are structure; VALUES are content, all of them.
            #
            # This used to exempt any value stored under the key "name", to protect tool
            # names from being scrubbed. It protected nothing: `_redact_row` only ever hands
            # this function `e["arguments"]`, so a tool's own `name` never reaches it. What
            # the exemption actually did was pass through every ARGUMENT called `name` --
            # `{"name": "Jane Smith"}` -- verbatim, out of an archive whose manifest says
            # `"redacted": true`. `name` is close to the most likely key for exactly the PII
            # a sender is redacting for.
            return {k: scrub(v[k]) for k in v}
        return v

    return scrub


def _redact_row(row: dict) -> tuple[dict, dict]:
    hits: dict[str, int] = {}
    scrub = _scrubber(hits)
    out = dict(row)
    for field in ("input", "output"):
        if isinstance(out.get(field), str):
            out[field] = scrub(out[field])
    if isinstance(out.get("tool_calls"), list):
        out["tool_calls"] = [
            {**e, "arguments": scrub(e["arguments"])} if isinstance(e.get("arguments"), dict)
            else e for e in out["tool_calls"]]
    return out, hits


def build(log_path: str | Path, out_path: str | Path, *, report_text: str,
          preflight: dict, prices_as_of: str, redact: bool = False,
          note: str | None = None) -> dict:
    """Write the submission zip. Returns the manifest (also written into the archive)."""
    log_path, out_path = Path(log_path), Path(out_path)
    raw = log_path.read_text(encoding="utf-8")

    hits: dict[str, int] = {}
    unparseable = 0
    if redact:
        lines = []
        # NOT `splitlines()` -- see `logs.split_records`. It used to be, and a prompt
        # containing U+2028 was torn in half, leaving the call as two corrupt lines inside
        # an archive whose manifest said "redacted". The plain path never had the bug, so
        # --redact destroyed data that doing nothing preserved.
        for line in logs.split_records(raw):
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                lines.append(line)
                continue
            try:
                row = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                # A torn trailing line is the DOCUMENTED crash mode of the writer, so this
                # branch is reached by ordinary use -- and it used to pass the line through
                # verbatim, inside an archive whose manifest said `"redacted": true`. The
                # scrubber does not need valid JSON to work on text, so it runs anyway, and
                # the count is disclosed: a claim of redaction must cover every byte in the
                # file or it is not a claim, it is an average.
                unparseable += 1
                scrubbed, h = redact_text(line)
                for k, n in h.items():
                    hits[k] = hits.get(k, 0) + n
                lines.append(scrubbed)
                continue
            row, h = _redact_row(row)
            for k, n in h.items():
                hits[k] = hits.get(k, 0) + n
            lines.append(json.dumps(row, ensure_ascii=False))
        payload = "\n".join(lines) + "\n"
        # The preflight verdict travels INTO the manifest, and it quotes up to 60 verbatim
        # model outputs as `candidate_labels` plus their counts. Those are the same strings
        # the log carries, so leaving them raw shipped un-redacted content in the one file
        # a sender reads to decide whether to upload -- beside the word "redacted".
        # Scrubbed with the SAME function, so the guarantee is identical to the log's.
        preflight = _scrubber(hits)(preflight)
        # And the report, for the same reason. It is aggregates rather than prompts, so the
        # exposure is far smaller than the log's -- but it carries model names taken
        # verbatim from the log, the archive says "redacted" without qualifying WHICH files,
        # and a claim that holds for two of three files is not a claim. One function, one
        # guarantee, every byte in the zip.
        report_text, h = redact_text(report_text)
        for k, n in h.items():
            hits[k] = hits.get(k, 0) + n
    else:
        payload = raw

    manifest = {
        "kadari_submission": {
            "client": f"kadari/{__version__}",
            "prices_as_of": prices_as_of,
            "source_log": log_path.name,
            "redacted": bool(redact),
            "redaction_note": (
                "Best-effort pattern scrubbing (emails, IPs, card-like and phone-like "
                "digit runs, token-shaped strings). A convenience, NOT a compliance "
                "control -- it cannot recognise names or identifiers it has no pattern "
                "for." if redact else None),
            "redacted_counts": hits or None,
            # Stated, not implied. A line the reader could not parse is still a line that
            # was scrubbed and shipped, and a sender deciding whether to upload production
            # prompts is entitled to know one was in there.
            "unparseable_lines": unparseable or None,
            "note": note,
            "contents": [
                {"name": "capture.jsonl",
                 "what": "your captured calls: request text, the answer you already paid "
                         "for, token usage, timestamps"},
                {"name": "report.txt",
                 "what": "the spend report you saw locally, so we read the same numbers"},
                {"name": "manifest.json", "what": "this file"},
            ],
            "preflight": preflight,
        }
    }
    manifest_json = json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True)

    # The densest file of customer data this tool produces, so it is created owner-only and
    # never through a symlink -- same rules as the log (see kadari._safeio).
    _safeio.secure_makedirs(out_path.parent)
    with _safeio.secure_open(out_path, "wb") as raw_out, \
            zipfile.ZipFile(raw_out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        # Fixed timestamps: a bundle built twice from the same log is byte-identical, so a
        # sender can verify for themselves that nothing varies between runs.
        for name, data in (("capture.jsonl", payload),
                           ("report.txt", report_text),
                           ("manifest.json", manifest_json)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return manifest


def render_manifest(manifest: dict, out_path: str | Path, *, size_bytes: int) -> str:
    """What the sender reads before deciding to upload."""
    m = manifest["kadari_submission"]
    L = ["", f"Wrote {out_path}  ({size_bytes / 1024:.0f} KB)", "",
         "It contains exactly:"]
    for item in m["contents"]:
        L.append(f"  {item['name']:<16} {item['what']}")
    L.append("")
    if m["redacted"]:
        counts = m.get("redacted_counts") or {}
        detail = ", ".join(f"{k} x{v}" for k, v in sorted(counts.items())) or "nothing matched"
        L += [f"Scrubbed: {detail}.", f"  {m['redaction_note']}"]
        if m.get("unparseable_lines"):
            L += [f"  {m['unparseable_lines']} line(s) were not valid JSON (a torn final "
                  f"line is the usual cause). They were scrubbed as raw text and are "
                  f"included; the manifest records the count."]
        L += [""]
    else:
        L += ["NOT scrubbed. The request text is included verbatim -- that is what makes "
              "the log", "evaluable, and it is also your production prompts. Re-run with "
              "--redact to scrub", "the obvious shapes first.", ""]
    return "\n".join(L)
