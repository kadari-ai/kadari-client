"""Read logs Kadari did not write, so the tool is useful before you instrument anything.

The realistic first user has months of billing history and no reason yet to add a library
to their production path. Making them instrument code before they can see a single number
is the wrong order — so anything that records model and token counts can produce a spend
report, and the capture client becomes the thing they reach for once they want the
evaluation too.

**One property is designed in rather than discovered: provider usage exports carry no
prompt text.** They are perfectly good for spend and cannot be evaluated for savings,
because an evaluation re-runs your calls and compares against the answer you already paid
for — and an export has neither the call nor the answer. Every importer therefore marks
absent text explicitly (``input_omitted`` / ``output_omitted``) rather than writing empty
strings that would later look like a call whose prompt happened to be blank. The preflight
reads those marks and refuses the submission up front, instead of us discovering it after
someone has uploaded a file and waited.

Importers are lenient about *shape* and strict about *meaning*: column names are matched
case-insensitively against a list of known spellings, but a row whose model or token
counts cannot be identified is skipped and reported, never guessed at.
"""

from __future__ import annotations

from . import gateway, tabular

SOURCES = {
    "openai-usage": ("OpenAI usage export (CSV or JSON). Token counts only — spend "
                     "report yes, savings evaluation no.", tabular.read_openai),
    "anthropic-usage": ("Anthropic usage export (CSV or JSON). Token counts only — spend "
                        "report yes, savings evaluation no.", tabular.read_anthropic),
    "csv": ("Any CSV with model and token-count columns; names are matched loosely.",
            tabular.read_generic),
    "gateway": ("JSONL from a proxy or gateway (LiteLLM, Helicone and similar). Often "
                "carries the prompt text too, in which case it IS evaluable.",
                gateway.read),
}


def read(source: str, path):
    """``(rows, warnings)`` in Kadari's wire format. Raises ``KeyError`` on a bad source."""
    if source not in SOURCES:
        raise KeyError(source)
    return SOURCES[source][1](path)


def describe() -> str:
    return "\n".join(f"  {name:<17} {desc}" for name, (desc, _) in SOURCES.items())
