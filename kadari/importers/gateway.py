"""JSONL from a proxy or gateway (LiteLLM, Helicone and similar).

Unlike a billing export, a gateway sits in the request path, so it usually logged the
request and the response as well as the token counts. When it did, the imported log is
genuinely evaluable — the same as one captured with the client. When it did not, the
absence is marked rather than papered over with empty strings.

Field layouts vary by gateway and by version, so each value is looked up through a list of
plausible paths rather than one. A row that yields no model is skipped and counted; a row
that yields no text is kept for spend and marked as text-free.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import logs
from ..capture import OMITTED_LABEL
from ..timestamps import to_iso

_MODEL_PATHS = (("model",), ("request", "model"), ("body", "model"),
                ("request_body", "model"), ("metadata", "model"), ("response", "model"))
_IN_PATHS = (("usage", "input_tokens"), ("usage", "prompt_tokens"),
             ("response", "usage", "input_tokens"), ("response", "usage", "prompt_tokens"),
             ("prompt_tokens",), ("input_tokens",), ("promptTokens",))
_OUT_PATHS = (("usage", "output_tokens"), ("usage", "completion_tokens"),
              ("response", "usage", "output_tokens"),
              ("response", "usage", "completion_tokens"),
              ("completion_tokens",), ("output_tokens",), ("completionTokens",))
_TS_PATHS = (("ts",), ("timestamp",), ("start_time",), ("startTime",), ("created_at",),
             ("createdAt",), ("time",), ("request", "timestamp"))
_MSG_PATHS = (("messages",), ("request", "messages"), ("body", "messages"),
              ("request_body", "messages"))
_RESP_PATHS = (("response",), ("response_body",), ("completion",), ("output",))


def _dig(row, path):
    cur = row
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _first(row, paths):
    for p in paths:
        v = _dig(row, p)
        if v is not None:
            return v
    return None


def _int(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v) if v >= 0 else None


def _last_user_text(messages) -> str:
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [b["text"] for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str)]
                if parts:
                    return "\n".join(parts)
    return ""


def _answer_text(resp) -> str:
    """The assistant's answer, across the two response shapes gateways forward."""
    if isinstance(resp, str):
        return resp
    if not isinstance(resp, dict):
        return ""
    choices = resp.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        msg = choices[0].get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
    content = resp.get("content")
    if isinstance(content, list):
        parts = [b["text"] for b in content
                 if isinstance(b, dict) and b.get("type") == "text"
                 and isinstance(b.get("text"), str)]
        if parts:
            return "\n".join(parts)
    return ""


def read(path):
    path = Path(path)
    out, warnings = [], []
    skipped = bad_json = 0
    with_text = 0
    unreadable_ts = 0
    seq = 0
    for line in logs.split_records(path.read_text(encoding="utf-8")):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            bad_json += 1
            continue
        if not isinstance(row, dict):
            bad_json += 1
            continue
        model = _first(row, _MODEL_PATHS)
        if not isinstance(model, str) or not model:
            skipped += 1
            continue
        seq += 1
        text = _last_user_text(_first(row, _MSG_PATHS))
        answer = _answer_text(_first(row, _RESP_PATHS))
        rec = {"id": str(row.get("id") or row.get("request_id") or f"gw-{seq}"),
               "model": model}
        if text:
            rec["input"] = text
        else:
            rec["input"] = ""
            rec["input_omitted"] = "not_in_source"
        if answer:
            rec["output"] = answer
            with_text += 1 if text else 0
        else:
            rec["output"] = OMITTED_LABEL
            rec["output_omitted"] = "not_in_source"
        in_tok, out_tok = _int(_first(row, _IN_PATHS)), _int(_first(row, _OUT_PATHS))
        if in_tok is not None and out_tok is not None:
            rec["usage"] = {"input_tokens": in_tok, "output_tokens": out_tok}
        # Through the SAME normaliser the tabular importer uses. Hand-rolling it here
        # dropped every integer epoch (`start_time` is LiteLLM's own spelling and is
        # listed in `_TS_PATHS` above) and turned a string epoch into `'1782000000Z'` --
        # a stamp that is not a date, which then selects a rate period by string
        # comparison. Undated is a gap the report states; misdated is a silent wrong bill.
        raw_ts = _first(row, _TS_PATHS)
        ts = to_iso(raw_ts)
        if ts:
            rec["ts"] = ts
        elif raw_ts is not None:
            unreadable_ts += 1
        out.append(rec)
    if bad_json:
        warnings.append(f"{bad_json} line(s) were not JSON objects and were skipped")
    if skipped:
        warnings.append(f"{skipped} row(s) named no model and were skipped")
    if unreadable_ts:
        warnings.append(
            f"{unreadable_ts} row(s) carried a timestamp we could not read as a date, so "
            f"they are treated as undated: they count towards spend at CURRENT rates and "
            f"are absent from the per-day chart. Guessing a date would risk pricing them "
            f"in the wrong rate period, which is a wrong bill rather than a missing bar.")
    if out and not with_text:
        warnings.append(
            "None of these rows carried both the request text and the model's answer, so "
            "this log supports a spend report but not a savings evaluation. Many gateways "
            "log the bodies only when request/response logging is switched on.")
    elif with_text < len(out):
        warnings.append(f"{len(out) - with_text} of {len(out)} rows carry no usable "
                        f"request/answer pair; they count for spend and are skipped by an "
                        f"evaluation.")
    return out, warnings
