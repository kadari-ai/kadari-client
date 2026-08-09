"""Can Kadari actually evaluate this log? Answered mechanically, before anyone sends it.

A spend report works on almost anything. A *savings* evaluation does not: it re-runs a
sample of the customer's calls on a cheaper model and compares the answers to the ones
they already paid for, so it needs the request text, the answer, a model we can price, and
enough eligible volume for the result to mean anything.

Finding that out after a log has been uploaded wastes the customer's time and ours, and it
is the failure the concierge journey already names as most likely. So the check runs
locally, before the upload, on facts the log either has or does not.

Two rules govern the verdict:

* **It warns; it does not moralise.** Every finding says what it means for the evaluation,
  not whether the customer did something wrong. Most findings are survivable.
* **It refuses exactly one thing:** a log with no request text at all. That is not a
  judgement call — there is literally nothing to re-run, so a submission could only
  produce a disappointing email.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# The distribution-free cold-start budget: earning the first step of the risk dial takes
# roughly this many clean probes. Below it a run cannot produce a backed number -- not
# because of cost (it is cents) but because of statistical sufficiency.
MIN_ELIGIBLE_CALLS = 73

# Above this, a distinct-output set stops looking like a closed label set and starts
# looking like free-form prose, which is a different (and currently out-of-scope) problem.
MAX_TAXONOMY_LABELS = 60
MAX_LABEL_CHARS = 80


@dataclass(frozen=True)
class Finding:
    level: str            # "block" | "warn" | "note"
    code: str
    message: str


@dataclass
class Preflight:
    submittable: bool
    findings: list[Finding] = field(default_factory=list)
    n_calls: int = 0
    n_with_text: int = 0
    n_eligible: int = 0
    baselines: list[str] = field(default_factory=list)
    candidate_labels: list[str] = field(default_factory=list)
    label_cardinality: int = 0

    def as_dict(self) -> dict:
        return {
            "submittable": self.submittable,
            "n_calls": self.n_calls,
            "n_with_text": self.n_with_text,
            "n_eligible": self.n_eligible,
            "baselines": self.baselines,
            "candidate_labels": self.candidate_labels,
            "label_cardinality": self.label_cardinality,
            "findings": [{"level": f.level, "code": f.code, "message": f.message}
                         for f in self.findings],
        }


def check(calls, *, table) -> Preflight:
    """Run the mechanical checks over already-read calls."""
    calls = tuple(calls)
    findings: list[Finding] = []
    n = len(calls)
    with_text = [c for c in calls if c.input_chars > 0 and c.output
                 and not c.input_omitted and not c.output_omitted]
    priced_with_text = [c for c in with_text if table.is_priced(c.model)]

    if n == 0:
        findings.append(Finding("block", "empty_log", "The log contains no calls."))
        return Preflight(False, findings)

    # ── the one refusal ──────────────────────────────────────────────────────
    if not with_text:
        findings.append(Finding(
            "block", "no_text",
            "No call in this log carries both the request text and the answer. An "
            "evaluation re-runs your calls on a smaller model and compares against the "
            "answer you already paid for, so there is nothing here to re-run. This is "
            "normal for a provider usage export, which reports token counts only — "
            "capture with the client to get an evaluable log."))
        return Preflight(False, findings, n_calls=n)

    if len(with_text) < n:
        findings.append(Finding(
            "note", "partial_text",
            f"{n - len(with_text):,} of {n:,} calls carry no usable text and would be "
            f"counted for spend but skipped by the evaluation."))

    # ── priceable ────────────────────────────────────────────────────────────
    unpriced = sorted({c.model for c in with_text if not table.is_priced(c.model)})
    if unpriced:
        findings.append(Finding(
            "warn", "unpriced_models",
            f"No published price on record for {', '.join(unpriced[:6])}"
            f"{' and others' if len(unpriced) > 6 else ''}. Those calls can still be "
            f"evaluated for quality, but no dollar figure can be attached to them."))

    # ── baselines ────────────────────────────────────────────────────────────
    counts = Counter(c.model for c in priced_with_text or with_text)
    baselines = [m for m, _ in counts.most_common()]
    if len(baselines) > 1:
        top = ", ".join(f"{m} ({c:,})" for m, c in counts.most_common(4))
        findings.append(Finding(
            "warn", "mixed_baseline",
            f"This log mixes {len(baselines)} models: {top}. An evaluation runs against "
            f"one baseline at a time — one dial per baseline, by design — so we will "
            f"either split it or ask which workload you care about."))

    # ── volume ───────────────────────────────────────────────────────────────
    eligible = len(priced_with_text) or len(with_text)
    if eligible < MIN_ELIGIBLE_CALLS:
        findings.append(Finding(
            "warn", "thin_volume",
            f"{eligible:,} evaluable calls. Earning the first step of the risk dial takes "
            f"roughly {MIN_ELIGIBLE_CALLS} clean probes, so a smaller log will show you "
            f"the machinery but is unlikely to earn a backed number. The binding "
            f"constraint is statistical sufficiency, not cost — the probes are cents."))
    elif counts and counts.most_common(1)[0][1] < MIN_ELIGIBLE_CALLS:
        findings.append(Finding(
            "note", "thin_per_baseline",
            f"No single model has {MIN_ELIGIBLE_CALLS} evaluable calls, so the first run "
            f"is likely to report the machinery rather than a backed number."))

    # ── shape: does this look like a closed label set? ───────────────────────
    labels = Counter(c.output.strip() for c in with_text if c.output.strip())
    short = {lbl for lbl in labels if len(lbl) <= MAX_LABEL_CHARS}
    candidate = sorted(short, key=lambda s: (-labels[s], s))[:MAX_TAXONOMY_LABELS]
    if len(short) > MAX_TAXONOMY_LABELS or len(short) < len(labels) * 0.5:
        findings.append(Finding(
            "note", "open_ended_output",
            f"{len(labels):,} distinct answers, most of them long. That reads as "
            f"free-form output rather than a closed label set. Kadari's measured claim "
            f"today covers classification and extraction — work with a verifiable "
            f"structured answer underneath — so tell us what the task is and we will say "
            f"honestly whether we can measure it."))
    elif candidate:
        findings.append(Finding(
            "note", "derived_taxonomy",
            f"The {len(short)} distinct answers below look like a closed label set. If "
            f"that is your taxonomy, confirming it is all the setup we need from you."))

    return Preflight(
        submittable=True, findings=findings, n_calls=n, n_with_text=len(with_text),
        n_eligible=eligible, baselines=baselines[:12],
        candidate_labels=candidate, label_cardinality=len(labels))


def render_text(p: Preflight) -> str:
    icon = {"block": "REFUSED", "warn": "(!)", "note": " - "}
    L = [f"submittable : {'yes' if p.submittable else 'NO'}",
         f"calls       : {p.n_calls:,}   with usable text: {p.n_with_text:,}   "
         f"evaluable: {p.n_eligible:,}"]
    if p.baselines:
        L.append(f"models      : {', '.join(p.baselines[:6])}"
                 + (" ..." if len(p.baselines) > 6 else ""))
    if p.candidate_labels:
        shown = ", ".join(p.candidate_labels[:12])
        L.append(f"labels seen : {p.label_cardinality} distinct — {shown}"
                 + (" ..." if len(p.candidate_labels) > 12 else ""))
    for f in p.findings:
        L.append(f"{icon.get(f.level, '   ')} {f.message}")
    return "\n".join(L)
