"""kadari -- the local capture client.

The ONLY thing Kadari ships to a customer: a thin, zero-dependency library that records
the LLM calls you already make to a local log, which Kadari's (server-side) engine then
analyses for safe, proven cost savings. It carries no scoring, no model, no benchmarks --
none of Kadari's IP. Capture happens in your environment; nothing is uploaded by this
library. Stdlib only.

    from kadari import LiveRecorder, wrap
    rec = LiveRecorder("kadari_capture.jsonl")

    # Option A -- record explicitly after a call you already make:
    rec.record_openai(id=req_id, input=user_text, response=resp.to_dict())

    # Option B -- wrap the call once and capture automatically:
    create = wrap(client.chat.completions.create, provider="openai", recorder=rec)
    resp = create(model="gpt-5.4-nano", messages=[{"role": "user", "content": user_text}])

See the README for the privacy model and the (stable) on-disk format.
"""

from __future__ import annotations

from .capture import CaptureError, LiveRecorder
from .wrap import wrap

__version__ = "0.3.1"
__all__ = ["LiveRecorder", "wrap", "CaptureError", "__version__"]
