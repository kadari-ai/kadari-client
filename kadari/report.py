"""Render the spend model as a self-contained HTML page (and as plain text).

A **pure render**: it adds no data to the dict :mod:`kadari.analyze` produced, so it cannot
report anything the analysis did not compute. Everything it emits is either a number from
that dict or a fixed string from this file.

Three layout rules exist to stop the page from being able to overstate, and they are
structural rather than editorial:

1. **The headline never appears without its qualifier.** The covered share of calls is
   rendered in the same block as the total, not in a footnote — because a footnote can be
   skipped and a screenshot can crop.
2. **The price-differential ceiling is visually marked as an estimate** (amber, dashed, a
   badge) and its ``_label`` is printed adjacent to the number. It is the figure a reader
   is most likely to remember as a promise, so the caveat is not allowed to drift away
   from it.
3. **A section that has no data is omitted, not zeroed.** No timestamps means no trend
   chart, rather than a flat line at zero implying a month of no spend.

Output is inert: no JavaScript, no external assets, no web fonts, no network. Opening it
never phones home, it prints cleanly, and rendering the same dict twice produces the same
bytes (rule 9).
"""

from __future__ import annotations

from . import charts as ch
from .theme import BASE_CSS

SUBMIT_URL = "https://kadari.ai/submit"


# ── HTML ─────────────────────────────────────────────────────────────────────
def render_html(model: dict, *, title: str = "Your LLM spend") -> str:
    """The whole page, as one string."""
    cov, sp = model["coverage"], model["spend"]
    body = "".join(filter(None, [
        _header(model, title),
        _hero(model),
        _tiles(model),
        _sample(model),
        _by_model(model),
        _trend(model),
        _concentration(model),
        _provenance(model),
        _at_stake(model),
        _limits(model),
        _warnings(model),
        _footer(model),
    ]))
    return ("<!DOCTYPE html>\n<html lang=\"en\"><head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{ch.esc(title)} &middot; Kadari</title>\n"
            f"<style>{BASE_CSS}</style>\n</head>\n<body><div class=\"wrap\">\n"
            f"{body}\n</div></body></html>\n")


def _header(m: dict, title: str) -> str:
    return (f"<header class=\"glass head fx\"><div>"
            f"<div class=\"brand\">kadari<b>.</b></div>"
            f"<div class=\"sub\">{ch.esc(title)}</div></div>"
            f"<div class=\"sub\">list prices as of {ch.esc(m['prices']['as_of'])}"
            f" &middot; {_read_count(m['coverage'])} read</div></header>")


def _read_count(cov: dict) -> str:
    """"N calls" — or, when the source aggregates, what was actually read."""
    if cov.get("aggregated"):
        return (f"{cov['n_calls']:,} aggregated row(s) covering "
                f"{cov['n_requests']:,} calls")
    return f"{cov['n_calls']:,} calls"


def _hero(m: dict) -> str:
    """Total spend, and — in the same block, never a footnote — what it covers."""
    cov, sp = m["coverage"], m["spend"]
    unit = "rows" if cov.get("aggregated") else "calls"
    qual = (f"Across {cov['n_priced_calls']:,} of {cov['n_calls']:,} {unit} "
            f"({ch.pct(cov['priced_call_share'])} of your log) that we hold a published "
            f"price for.")
    if cov.get("aggregated"):
        qual += (f" This source is aggregated: each row summarises many calls, "
                 f"{cov['n_requests']:,} in total. The spend is the export's own token "
                 f"counts, so the total stands; the per-call shape does not, and is not "
                 f"shown.")
    if cov["n_unpriced_calls"]:
        n = cov["n_unpriced_calls"]
        qual += (f" The other {n:,} {'is' if n == 1 else 'are'} counted but not costed — "
                 f"{'its' if n == 1 else 'their'} dollars are excluded from this figure "
                 f"rather than counted as zero.")
    if m["log"]["sample"] is not None and m["log"]["sample"] < 1.0:
        qual += (f" This log was captured at a {ch.pct(m['log']['sample'])} sample, so it "
                 f"is a slice of your traffic, not all of it.")
    return (f"<section class=\"glass hero fx d1\">"
            f"<h2>Observed spend</h2>"
            f"<div class=\"stat\">{ch.esc(ch.usd(sp['priced_usd']))}</div>"
            f"<p class=\"qual\">{ch.esc(qual)}</p></section>")


def _tiles(m: dict) -> str:
    cov, sp, dist = m["coverage"], m["spend"], m["distribution"]
    # On an aggregated source a row is a bucket, not a call, so the tile says so and the
    # call count comes from the request totals the export carried.
    tiles = ([("Requests", f"{cov['n_requests']:,}"),
              ("Aggregated rows", f"{cov['n_calls']:,}")] if cov.get("aggregated") else
             [("Calls", f"{cov['n_calls']:,}")])
    tiles += [("Input tokens", ch.compact(sp["tokens"]["input"])),
              ("Output tokens", ch.compact(sp["tokens"]["output"]))]
    # Shown only when the traffic actually had cache activity, and shown as their OWN
    # tiles rather than folded into "Input tokens". They are disjoint buckets billed at
    # different rates, so a reader who adds them up gets the prompt size, while a report
    # that had added them for them would be quoting a number that matches no line on the
    # invoice. Absent buckets stay absent: a "0" we never observed would assert the
    # traffic was uncached.
    if sp["tokens"]["cached_input"]:
        tiles.append(("Cached input", ch.compact(sp["tokens"]["cached_input"])))
    if sp["tokens"]["cache_write"]:
        tiles.append(("Cache writes", ch.compact(sp["tokens"]["cache_write"])))
    if dist["n"]:
        tiles.append(("Median call", ch.usd(dist["per_call_usd"]["p50"])))
        tiles.append(("Priciest call", ch.usd(dist["per_call_usd"]["max"])))
    if m["time"]:
        tiles.append(("Days covered", str(len(m["time"]["buckets"]))))
    cells = "".join(f"<div class=\"tile\"><div class=\"k\">{ch.esc(k)}</div>"
                    f"<div class=\"v\">{ch.esc(v)}</div></div>" for k, v in tiles)
    return f"<section class=\"grid fx d2\">{cells}</section>"


def _sample(m: dict) -> str:
    """Extrapolation from a known sample — its own block, never folded into the total."""
    ex = m["extrapolation"]
    if not ex:
        return ""
    return (f"<section class=\"glass panel fx d2\">"
            f"<h2>If that sample is representative <span class=\"badge est\">Projected"
            f"</span></h2>"
            f"<div class=\"stat\">{ch.esc(ch.usd(ex['projected_usd']))}</div>"
            f"<p class=\"note est\">{ch.esc(ex['_label'])}</p></section>")


def _by_model(m: dict) -> str:
    rows = m["spend"]["by_model"]
    if not rows:
        return ""
    chart = ch.hbars([(r["model"], r["usd"]) for r in rows], title="Spend by model")
    # The cache columns appear only when some model in the table used the cache. On
    # traffic that never caches they would be a column of zeros inviting the reader to
    # wonder what they missed -- and their absence keeps a pre-cache log rendering exactly
    # as it always did.
    show_cache = any(r["cached_input_tokens"] or r["cache_write_tokens"] for r in rows)
    cache_head = ("<th class=\"r\">Cached</th><th class=\"r\">Writes</th>"
                  if show_cache else "")
    body = "".join(
        f"<tr><td>{ch.esc(r['model'])}</td><td>{ch.esc(r['provider'] or '—')}</td>"
        f"<td class=\"r\">{r['calls']:,}</td>"
        f"<td class=\"r\">{ch.esc(ch.compact(r['input_tokens']))}</td>"
        + (f"<td class=\"r\">{ch.esc(ch.compact(r['cached_input_tokens']))}</td>"
           f"<td class=\"r\">{ch.esc(ch.compact(r['cache_write_tokens']))}</td>"
           if show_cache else "")
        + f"<td class=\"r\">{ch.esc(ch.compact(r['output_tokens']))}</td>"
        f"<td class=\"r\">{ch.esc(ch.usd(r['usd']))}</td></tr>" for r in rows)
    return (f"<section class=\"glass panel fx d3\"><h2>Where the money goes</h2>"
            f"<div class=\"scroll\">{chart}</div>"
            f"<div class=\"scroll\"><table><thead><tr><th>Model</th><th>Provider</th>"
            f"<th class=\"r\">Calls</th><th class=\"r\">In</th>{cache_head}"
            f"<th class=\"r\">Out</th>"
            f"<th class=\"r\">Spend</th></tr></thead><tbody>{body}</tbody></table></div>"
            f"</section>")


def _trend(m: dict) -> str:
    """Omitted entirely when nothing is dated — a flat line at zero would invent a shape
    the data does not have."""
    t = m["time"]
    if not t:
        return ""
    chart = ch.columns([(b["bucket"], b["usd"]) for b in t["buckets"]],
                       title="Spend per day")
    note = ""
    if t["n_undated_calls"]:
        note = (f"<p class=\"cap\">{t['n_undated_calls']:,} call(s) carry no timestamp and "
                f"are absent from this chart, though they are included in the totals "
                f"above.</p>")
    return (f"<section class=\"glass panel fx d3\"><h2>Spend per day &middot; "
            f"{ch.esc(t['first'])} to {ch.esc(t['last'])}</h2>"
            f"<div class=\"scroll\">{chart}</div>{note}</section>")


def _concentration(m: dict) -> str:
    dist = m["distribution"]
    if dist.get("aggregated"):
        # Absent AND explained. A silently missing section reads as "your traffic has no
        # interesting shape"; this says the source could not answer the question.
        return (f"<section class=\"glass panel fx d4\"><h2>How concentrated it is</h2>"
                f"<p class=\"note warn\">{ch.esc(dist['_note'])}</p></section>")
    if not dist["n"] or len(dist["concentration"]) < 2:
        return ""
    points = [(c["top_call_share"], c["spend_share"]) for c in dist["concentration"]]
    head = dist["concentration"][0]
    lead = (f"The priciest {ch.pct(head['top_call_share'])} of your calls "
            f"({head['n_calls']:,}) account for {ch.pct(head['spend_share'])} of the bill.")
    rows = "".join(
        f"<tr><td>Top {ch.esc(ch.pct(c['top_call_share']))}</td>"
        f"<td class=\"r\">{c['n_calls']:,}</td>"
        f"<td class=\"r\">{ch.esc(ch.pct(c['spend_share']))}</td></tr>"
        for c in dist["concentration"])
    return (f"<section class=\"glass panel fx d4\"><h2>How concentrated it is</h2>"
            f"<p class=\"qual\">{ch.esc(lead)}</p>"
            f"<div class=\"scroll\">{ch.concentration(points)}</div>"
            f"<table><thead><tr><th>Slice</th><th class=\"r\">Calls</th>"
            f"<th class=\"r\">Share of spend</th></tr></thead><tbody>{rows}</tbody></table>"
            f"</section>")


def _provenance(m: dict) -> str:
    """The one two-hue chart: what was metered versus what we estimated."""
    sp, cov = m["spend"], m["coverage"]
    if not cov["n_estimated_token_calls"]:
        return ""
    chart = ch.split_bar([
        ("Metered by the provider", sp["measured_usd"], "var(--proven)", False),
        ("Estimated from text length", sp["estimated_usd"], "var(--est)", True),
    ], title="Metered versus estimated spend")
    return (f"<section class=\"glass panel fx d4\"><h2>How much of this is measured</h2>"
            f"<div class=\"legend\">"
            f"<span><i style=\"background:var(--proven)\"></i>Metered &mdash; the "
            f"provider's own token counts</span>"
            f"<span><i style=\"background:var(--est);opacity:.5;border:1px dashed "
            f"var(--est)\"></i>Estimated &mdash; inferred from text length</span></div>"
            f"<div class=\"scroll\">{chart}</div>"
            f"<p class=\"note\">{cov['n_estimated_token_calls']:,} of "
            f"{cov['n_calls']:,} calls carried no usage block, so their tokens are our "
            f"estimate rather than a measurement. The two are shown separately and are "
            f"never added together into a figure presented as metered.</p></section>")


def _family(ladder: str | None) -> str:
    """Name a ladder as the GROUP it is, never as the bare token.

    Several ladders are named after a rung inside them -- `gpt-5.5` is both a ladder and a
    model on it -- so `gpt-5.5` printed bare in a table whose next column holds
    `gpt-5.4-nano` reads as one more model id. It is not: the row aggregates every rung of
    that family the log touched, so its `Paid` figure covers calls the reader would look
    for under several different model names in the by-model table above."""
    return f"{ladder} family" if ladder else "—"


def _at_stake(m: dict) -> str:
    """The price-differential ceiling. Marked as an estimate, with its caveat adjacent."""
    at = m["at_stake"]
    if not at:
        return ""
    # "...so the rest of your bill is outside this question" is false when there is no
    # rest, and a reader who spots one wrong clause is right to distrust the number beside
    # it. The two cases are genuinely different facts, so they get different sentences.
    share = at["share_of_priced_spend"]
    covers = ("Those calls are all of the spend we could price." if share >= 1.0 else
              f"Those calls are {ch.esc(ch.pct(share))} of the spend we could price, so "
              f"the rest of your bill is outside this question entirely.")
    rows = [(_family(r["ladder"]), r["observed_usd"], r["at_cheapest_usd"])
            for r in at["by_ladder"]]
    detail = "".join(
        f"<tr><td>{ch.esc(_family(r['ladder']))}</td>"
        f"<td>{ch.esc(r['cheapest_rung'])}</td><td class=\"r\">{r['calls']:,}</td>"
        f"<td class=\"r\">{ch.esc(ch.usd(r['observed_usd']))}</td>"
        f"<td class=\"r\">{ch.esc(ch.usd(r['at_cheapest_usd']))}</td></tr>"
        for r in at["by_ladder"])
    return (f"<section class=\"glass panel fx d5\">"
            f"<h2>The size of the question <span class=\"badge est\">Estimate</span></h2>"
            f"<p class=\"qual\">You spent {ch.esc(ch.usd(at['observed_usd']))} on calls "
            f"that have a smaller rung in the same provider family. The identical tokens "
            f"at that rung's published rate would be "
            f"{ch.esc(ch.usd(at['at_cheapest_usd']))} &mdash; a difference of "
            f"<strong>{ch.esc(ch.usd(at['difference_usd']))}</strong>. {covers}</p>"
            f"<div class=\"legend\">"
            f"<span><i style=\"background:var(--chart)\"></i>What you paid</span>"
            f"<span><i style=\"background:var(--est);opacity:.4;border:1px dashed "
            f"var(--est)\"></i>Same tokens, cheapest rung</span></div>"
            f"<div class=\"scroll\">{ch.compare_bars(rows, title='Price differential')}"
            f"</div>"
            f"<p class=\"note est\">{ch.esc(at['_label'])}</p>"
            f"<div class=\"scroll\"><table><thead><tr><th>Family</th>"
            f"<th>Cheapest rung</th><th class=\"r\">Calls</th><th class=\"r\">Paid</th>"
            f"<th class=\"r\">At that rung</th></tr></thead><tbody>{detail}</tbody>"
            f"</table></div></section>")


def _limits(m: dict) -> str:
    """What this file cannot answer — and the offer to answer it."""
    at = m["at_stake"]
    hook = ("That difference is the ceiling on what routing could ever save you. What it "
            "does not tell you is how much of it you could take without your output "
            "getting worse — because nothing in this file has looked at whether a smaller "
            "model would have given an acceptable answer on your task."
            if at else
            "Nothing in this file has looked at whether a smaller model would have given "
            "an acceptable answer on your task.")
    return (f"<section class=\"glass cta fx d6\">"
            f"<h3>What this file can't tell you</h3>"
            f"<p>{ch.esc(hook)}</p>"
            f"<p>That is the question Kadari measures. We re-run a small, cost-capped "
            f"sample of your calls on the smaller rung and check the answers against the "
            f"ones you already paid for — so the number you get back is one we can show "
            f"the working for, not one we modelled.</p>"
            # A plain anchor: inert until someone clicks it. Opening this file still
            # fetches nothing -- which is the property the self-containment test checks,
            # rather than the mere absence of the string "http".
            f"<p>Send us the log and we will do it: "
            f"<a href=\"{ch.esc(SUBMIT_URL)}\"><strong>{ch.esc(SUBMIT_URL)}</strong></a>."
            f"</p>"
            f"<p class=\"cap\">Worth knowing before you do: the first report's "
            f"<em>proven</em> savings figure is <strong>$0.00</strong> by design. Until "
            f"enough of your own calls have been checked, we have not earned the right to "
            f"claim a number on your task, and we would rather say so than estimate one."
            f"</p></section>")


def _warnings(m: dict) -> str:
    cov, warns = m["coverage"], m["warnings"]
    bits = []
    if cov["unpriced_models"]:
        rows = "".join(
            f"<tr><td>{ch.esc(r['model'])}</td><td class=\"r\">{r['calls']:,}</td>"
            f"<td class=\"r\">{ch.esc(ch.compact(r['input_tokens']))}</td>"
            f"<td class=\"r\">{ch.esc(ch.compact(r['output_tokens']))}</td></tr>"
            for r in cov["unpriced_models"])
        bits.append(
            f"<p class=\"note warn\">We hold no published price for the models below, so "
            f"their calls are counted in the volume figures and excluded from every "
            f"dollar figure. If you know the rate, pass your own table with "
            f"<span class=\"mono\">--prices</span>.</p>"
            f"<div class=\"scroll\"><table><thead><tr><th>Model</th>"
            f"<th class=\"r\">Calls</th><th class=\"r\">In</th><th class=\"r\">Out</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>")
    if cov["n_input_omitted"] or cov["n_output_omitted"]:
        n = max(cov["n_input_omitted"], cov["n_output_omitted"])
        reasons = cov.get("omitted_reasons") or {}
        # The reason is read off the records, not assumed. "Too large to store whole" is
        # true of a capture that shed content at the size ceiling and simply false of a
        # usage export, which never carried prompt text at all.
        if set(reasons) == {"not_in_source"}:
            why = ("their source never carried the request text or the model's answer — a "
                   "usage export reports token counts only")
        elif "not_in_source" in reasons:
            why = ("their text is missing for mixed reasons: "
                   + ", ".join(f"{k} ×{v:,}" for k, v in reasons.items()))
        else:
            why = "they were too large to store whole, so their text was dropped at capture time"
        bits.append(
            f"<p class=\"cap\">{n:,} record(s) carry no text: {ch.esc(why)}. Their model "
            f"and token counts survived, so they are priced normally — but an evaluation "
            f"has nothing to re-run for them.</p>")
    if cov.get("n_unpriced_cache_write_calls"):
        # A FLOOR, said in those words. We hold no published write premium for this
        # provider, so those tokens are charged at the plain input rate -- the lower of
        # the two readings of an arithmetic the provider has not documented. Stating the
        # count lets a reader size the gap instead of being told one exists (AP-01): the
        # alternative, folding in a guessed multiplier, would put a number we invented
        # inside a figure the page calls measured.
        bits.append(
            f"<p class=\"note warn\">"
            f"{cov['n_unpriced_cache_write_calls']:,} call(s) wrote "
            f"{ch.esc(ch.compact(cov['unpriced_cache_write_tokens']))} token(s) into a "
            f"prompt cache on a provider that has not published what that write costs on "
            f"top of the ordinary input charge. Those tokens are billed here at the plain "
            f"input rate, so the total above is a <strong>floor</strong> for these calls "
            f"and not an estimate of them. We would rather under-state and say so than "
            f"guess a premium and present it as measured.</p>")
    if warns:
        items = "".join(f"<li>{ch.esc(w)}</li>" for w in warns[:20])
        bits.append(f"<p class=\"cap\">While reading the log:</p>"
                    f"<ul class=\"cap\">{items}</ul>")
    if not bits:
        return ""
    return (f"<section class=\"glass panel fx d6\"><h2>Caveats</h2>{''.join(bits)}"
            f"</section>")


def _footer(m: dict) -> str:
    src = " &middot; ".join(
        f"{ch.esc(s.get('provider', '?'))} rates checked {ch.esc(s.get('checked', '?'))}"
        for s in m["prices"]["sources"])
    client = m["log"]["client"] or "kadari"
    return (f"<p class=\"foot\">Rendered locally by {ch.esc(client)} from your own log. "
            f"No network, no JavaScript, nothing uploaded.<br>{src}<br>"
            f"Prices are a dated snapshot and decay; re-check before relying on a figure."
            f"</p>")


# ── plain text (pipes, terminals, and anyone who does not want a browser) ────
def render_text(m: dict) -> str:
    cov, sp = m["coverage"], m["spend"]
    L = ["=" * 72,
         "KADARI SPEND REPORT  (local; your log, published list prices, no network)",
         "=" * 72,
         f"prices as of : {m['prices']['as_of']}",
         f"calls read   : {cov['n_calls']:,}"
         + (f"  ({cov['n_requests']:,} calls, aggregated into {cov['n_calls']:,} row(s) "
            f"by the source)" if cov.get("aggregated") else ""),
         "",
         f"OBSERVED SPEND  {ch.usd(sp['priced_usd'])}",
         f"  covers {cov['n_priced_calls']:,}/{cov['n_calls']:,} calls "
         f"({ch.pct(cov['priced_call_share'])}) that we hold a published price for"]
    if cov["n_unpriced_calls"]:
        L.append(f"  (!) {cov['n_unpriced_calls']:,} call(s) on unpriced models are "
                 f"counted but NOT costed -- excluded, not zero:")
        for r in cov["unpriced_models"]:
            L.append(f"        {r['model']:<34} x{r['calls']:,}")
    if cov["n_estimated_token_calls"]:
        L.append(f"  metered   {ch.usd(sp['measured_usd'])}   "
                 f"estimated {ch.usd(sp['estimated_usd'])}  "
                 f"({cov['n_estimated_token_calls']:,} call(s) had no usage block)")
    if sp["tokens"]["cached_input"] or sp["tokens"]["cache_write"]:
        # Beside the totals, never inside them -- the three buckets bill at three rates.
        L.append(f"  tokens    in {ch.compact(sp['tokens']['input'])} uncached  "
                 f"+ {ch.compact(sp['tokens']['cached_input'])} cached  "
                 f"+ {ch.compact(sp['tokens']['cache_write'])} written  "
                 f"/ out {ch.compact(sp['tokens']['output'])}")
    if cov.get("n_unpriced_cache_write_calls"):
        L.append(f"  (!) {cov['n_unpriced_cache_write_calls']:,} call(s) wrote "
                 f"{ch.compact(cov['unpriced_cache_write_tokens'])} token(s) to a prompt "
                 f"cache on a provider that has not published the write premium; charged "
                 f"at the plain input rate, so this total is a FLOOR for those calls")
    if m["log"]["sample"] is not None and m["log"]["sample"] < 1.0:
        L.append(f"  (!) captured at a {ch.pct(m['log']['sample'])} sample -- a slice of "
                 f"your traffic, not all of it")
    ex = m["extrapolation"]
    if ex:
        L += ["", f"IF THAT SAMPLE IS REPRESENTATIVE  {ch.usd(ex['projected_usd'])}  "
                  f"[PROJECTED]", f"  {ex['_label']}"]
    L += ["", "BY MODEL"]
    for r in sp["by_model"]:
        L.append(f"  {r['model']:<30} {r['calls']:>7,} calls  {ch.usd(r['usd']):>12}")
    dist = m["distribution"]
    if dist.get("aggregated"):
        L += ["", "PER CALL", f"  (not available) {dist['_note']}"]
    elif dist["n"]:
        p = dist["per_call_usd"]
        L += ["", "PER CALL",
              f"  median {ch.usd(p['p50'])}   p90 {ch.usd(p['p90'])}   "
              f"p99 {ch.usd(p['p99'])}   max {ch.usd(p['max'])}"]
        for c in dist["concentration"]:
            L.append(f"  top {ch.pct(c['top_call_share']):>4} of calls "
                     f"({c['n_calls']:,}) = {ch.pct(c['spend_share'])} of spend")
    t = m["time"]
    if t:
        L += ["", f"PER DAY  {t['first']} .. {t['last']}"]
        for b in t["buckets"][-14:]:
            L.append(f"  {b['bucket']}  {b['calls']:>6,} calls  {ch.usd(b['usd']):>12}")
    at = m["at_stake"]
    if at:
        L += ["", "THE SIZE OF THE QUESTION  [ESTIMATE -- not a saving]",
              f"  paid {ch.usd(at['observed_usd'])} on calls with a smaller rung in the "
              f"same family ({ch.pct(at['share_of_priced_spend'])} of priced spend)",
              f"  the same tokens at that rung: {ch.usd(at['at_cheapest_usd'])}  "
              f"(difference {ch.usd(at['difference_usd'])})",
              f"  {at['_label']}"]
    L += ["", "WHAT THIS FILE CANNOT TELL YOU",
          "  Whether a smaller model would have given an acceptable answer on YOUR task.",
          "  That is what Kadari measures, against the outputs you already paid for.",
          f"  {SUBMIT_URL}",
          "  The first report's PROVEN savings figure is $0.00 by design -- until enough",
          "  of your own calls are checked, we have not earned a claim on your task.",
          ""]
    if m["warnings"]:
        L += ["WHILE READING THE LOG"] + [f"  - {w}" for w in m["warnings"][:20]] + [""]
    return "\n".join(L)
