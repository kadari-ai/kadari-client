"""``wrap`` -- instrument a provider call once and capture every call automatically.

Instead of calling ``recorder.record_*`` after every request, wrap the provider's
*create* function once and use the returned callable in its place:

    from kadari import LiveRecorder, wrap
    rec = LiveRecorder("kadari_capture.jsonl")

    create = wrap(client.chat.completions.create, provider="openai", recorder=rec)
    resp = create(model="gpt-5.4-nano", messages=[{"role": "user", "content": text}])
    #  -> the real call runs untouched; the observation is recorded as a side-channel

The real call is **never** interfered with: ``wrap`` calls through to the original and
returns its result unchanged. All capture work is wrapped so a recording failure can
never break your production call (fail open on the host), exactly like ``record``.
"""

from __future__ import annotations

from typing import Callable

from .capture import LiveRecorder


def wrap(create: Callable, *, provider: str, recorder: LiveRecorder,
         id_from: Callable | None = None) -> Callable:
    """Return a drop-in replacement for ``create`` that records each call.

    ``provider`` is ``"openai"`` or ``"anthropic"``. ``id_from`` optionally derives a
    stable id from the response (e.g. ``lambda r: r.id``); otherwise a fresh uuid is used.
    """
    if provider not in ("openai", "anthropic"):
        raise ValueError("provider must be 'openai' or 'anthropic'")

    def wrapped(*args, **kwargs):
        resp = create(*args, **kwargs)        # the real call -- untouched, result returned as-is
        try:
            input_text = _extract_input(kwargs)
            body = _to_dict(resp)
            rid = id_from(resp) if id_from is not None else None
            # The id only needs to be a non-empty unique string. Coerce a stringifiable
            # id_from result; on None/empty/uncoercible, fall back to the auto-uuid rather
            # than DROP the call -- an imperfect optional id derivation must not lose data.
            if rid is not None:
                try:
                    rid = str(rid) or None
                except Exception:  # noqa: BLE001 -- a weird __str__ must not lose the call
                    rid = None
            if provider == "anthropic":
                recorder.record_anthropic(id=rid, input=input_text, response=body)
            else:
                recorder.record_openai(id=rid, input=input_text, response=body)
        except Exception as exc:  # noqa: BLE001 -- capture must never break the host call
            if recorder.strict:
                raise
            recorder._notify(exc)
        return resp

    return wrapped


def _extract_input(kwargs: dict) -> str:
    """The request input we score later -- the last user turn of ``messages`` (both the
    string and content-block forms), falling back to a ``prompt``/``input`` kwarg."""
    messages = kwargs.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = [b["text"] for b in content
                             if isinstance(b, dict) and b.get("type") == "text"
                             and isinstance(b.get("text"), str)]
                    if parts:
                        return "\n".join(parts)
    for key in ("input", "prompt"):
        v = kwargs.get(key)
        if isinstance(v, str):
            return v
    return ""


def _to_dict(resp) -> dict:
    """Normalise an SDK response object to a plain dict (``.to_dict()`` /
    ``.model_dump()`` / already a dict)."""
    if isinstance(resp, dict):
        return resp
    for attr in ("to_dict", "model_dump"):
        fn = getattr(resp, attr, None)
        if callable(fn):
            out = fn()
            if isinstance(out, dict):
                return out
    raise TypeError("could not convert response to a dict (no .to_dict()/.model_dump())")
