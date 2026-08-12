"""What changed in `kadari/prices.json`, and is it plausible?

`packaging_check.py` proves the price table is well FORMED. This proves a change to it is
believable. The two are different questions, and only the first was being asked.

That gap is not theoretical: planting seven defects and running the CI command showed six
caught (transposed input/output, negative rate, malformed date bound, ladder pointing at an
unpriced rung, cache read dearer than uncached, unparseable JSON) and one straight through --
a rate multiplied by ten. Structure cannot catch a well-formed wrong NUMBER, because there is
nothing structurally wrong with it.

It matters here more than it would elsewhere. This repository invites corrections to
`prices.json` as pull requests, so the numbers take outside commits; they become the dollar
figures on a customer's report, and the spend projection that a hard cost cap is enforced
against. A rate ten times too low does not read as an error anywhere downstream -- it reads
as a cheap model, and the cap that was supposed to stop the run lets it spend through.

Two rules, both diff-based, run only on pull requests:

1. **No rate moves by 5x or more.** Providers cut prices, sometimes steeply, but a published
   rate card does not move by an order of magnitude between two versions of this file. At 5x
   the false-positive rate is ~0 and the fat-finger shape -- a slipped decimal, a copied
   neighbouring cell -- is exactly what lands above it. This does not say the change is
   wrong; it says a human must confirm it against the provider's published card and record
   that they did.

2. **A rate change must move `as_of`.** `as_of` is the date the table claims to have been
   checked, and it is displayed to customers. Letting the numbers change while the date
   stands still turns it into decoration -- the table would claim a freshness nobody
   re-established (P3: the landscape is perishable, treat it as data that gets re-measured).

Deliberately NOT a rule: a bound on absolute magnitude. "No rate above $X" dates the moment a
provider ships something expensive, and a stale bound fails honest PRs until someone widens
it -- at which point it protects nothing. The relative check has no such expiry.

Run by `.github/workflows/ci.yml` on pull requests. Not shipped: this file lives at the
repository root, not in `kadari/`, so it is absent from both the sdist and the wheel.

    python price_diff_check.py --base <ref>     # compares HEAD's file against <ref>'s
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

REL = "kadari/prices.json"

# 5x, and the reasoning is in the module docstring: high enough that a real price cut never
# trips it, low enough that a slipped decimal (10x) always does.
MAX_FACTOR = 5.0

RATE_KEYS = ("input_per_m", "output_per_m", "cached_input_per_m", "cache_write_per_m")

def git_show(ref: str, path: str) -> str | None:
    """The file's contents at `ref`, or None if it did not exist there."""
    proc = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def rates_of(doc: dict) -> dict[tuple[str, int, str], float]:
    """Flatten to {(model, period_index, key): value} so a diff is a dict comparison.

    Indexing periods positionally is deliberate. Inserting a period genuinely re-dates the
    ones after it, and that SHOULD surface for review rather than being silently matched up
    by date -- an inserted period is how a historical call quietly changes price.
    """
    out = {}
    for name, entry in (doc.get("models") or {}).items():
        if not isinstance(entry, dict):
            continue
        for i, period in enumerate(entry.get("rates") or []):
            if not isinstance(period, dict):
                continue
            for key in RATE_KEYS:
                v = period.get(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[(name, i, key)] = float(v)
    return out


def check(base_doc: dict, head_doc: dict) -> list[str]:
    """Every problem with the change from `base_doc` to `head_doc`, as human sentences.

    Returns rather than prints, and holds no module state, so the rules can be exercised
    directly on two dicts -- no git repository, no network, no temp tree.
    """
    problems: list[str] = []
    fail = problems.append
    base, head = rates_of(base_doc), rates_of(head_doc)

    changed = sorted(k for k in base.keys() & head.keys() if base[k] != head[k])
    removed = sorted(base.keys() - head.keys())

    for name, i, key in changed:
        old, new = base[(name, i, key)], head[(name, i, key)]
        # A rate going to or from zero is a category change, not a movement -- treat it as
        # unbounded rather than dividing by zero and reporting `inf`, which reads like a bug.
        if old == 0 or new == 0:
            fail(f"{name}[{i}].{key}: {old} -> {new}. A rate moving to or from zero changes "
                 f"whether we price this model at all, which is a deliberate decision and "
                 f"not a price correction. Say so in the pull request.")
            continue
        factor = max(new / old, old / new)
        if factor >= MAX_FACTOR:
            fail(f"{name}[{i}].{key} moved {factor:.1f}x ({old} -> {new}). No published rate "
                 f"card has ever moved that far between two revisions of this file, so this "
                 f"reads as a transcription error -- a slipped decimal or a copied "
                 f"neighbouring cell. Confirm it against the provider's published prices and "
                 f"say in the pull request that you did. These numbers become a customer's "
                 f"dollar figures and the projection a hard spend cap is enforced against, so "
                 f"a wrong one is not a cosmetic defect.")

    for name, i, key in removed:
        fail(f"{name}[{i}].{key} was removed ({base[(name, i, key)]}). Dropping a rate makes "
             f"calls it used to price report as unpriced; if that is intended, it is a schema "
             f"change to state, not a diff to slip through.")

    if changed or removed:
        base_as_of, head_as_of = base_doc.get("as_of"), head_doc.get("as_of")
        if base_as_of == head_as_of:
            fail(f"rates changed but `as_of` is unchanged ({head_as_of}). That field is the "
                 f"date this table claims to have been checked and it is shown to customers, "
                 f"so leaving it still while the numbers move makes it decoration. Re-check "
                 f"the provider's page, then move `as_of` and the matching `sources[].checked`.")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True,
                    help="the ref to compare against, e.g. origin/main")
    args = ap.parse_args()

    base_raw = git_show(args.base, REL)
    if base_raw is None:
        print(f"price-diff: {REL} does not exist at {args.base} -- nothing to compare, "
              f"so the table is new and packaging_check.py owns it.")
        return 0

    try:
        with open(REL, "r", encoding="utf-8") as fh:
            head_doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        # packaging_check.py owns malformed JSON and says so better; do not double-report.
        print(f"price-diff: cannot read {REL} ({exc}) -- packaging_check.py covers this.")
        return 0
    try:
        base_doc = json.loads(base_raw)
    except json.JSONDecodeError:
        print(f"price-diff: {REL} at {args.base} is not valid JSON -- nothing to compare.")
        return 0

    problems = check(base_doc, head_doc)

    if problems:
        print(f"price-diff: FAIL -- {len(problems)} issue(s) in the change to {REL}\n")
        for msg in problems:
            print(f"  * {msg}\n")
        print("If a flagged change is genuinely right, it still needs a human to confirm it "
              "against the provider's published rate card. This gate exists because six of "
              "seven planted defects were caught by structure and this one was not.")
        return 1

    print(f"price-diff: OK -- the change to {REL} is plausible "
          f"(no rate moves {MAX_FACTOR:g}x or more; `as_of` moves when rates do)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
