"""Inline-SVG charts. No JavaScript, no external assets, no network, deterministic bytes.

Every function is a pure function of numbers already computed by :mod:`kadari.analyze` --
a chart adds no measurement, it only draws one. That is what lets the report be a *pure
render*: it cannot disagree with the dict it came from, because it has nothing of its own
to disagree with.

Four constraints shape everything here, and each has a reason that is not aesthetic:

* **No ``xmlns``, ``xlink`` or any ``http``** — inline SVG inside HTML5 needs none of
  them, and each one embeds a URL that makes a "this file opens no connection" claim
  harder to verify by eye. The self-containment test greps for exactly these.
* **Geometry is formatted to fixed decimals** so the same input renders the same bytes on
  every machine and every run (rule 9). Float repr differences would otherwise show up as
  spurious diffs in a file customers are invited to archive and compare.
* **Every data-derived string is escaped**, including model names, which arrive from a log
  we did not write and must be treated as data, never as markup (rule 7).
* **``<title>`` on every mark.** It is the accessible name, and in a document with no
  JavaScript it is also the entire hover layer — the browser renders it as a native
  tooltip for free. It is the honest ceiling on interactivity for an inert file.

Colour assignment is documented in :mod:`kadari.theme`: one hue for magnitude, and exactly
one two-hue chart for the measured-versus-estimated honesty axis.
"""

from __future__ import annotations

import html
import math

# Marks: thin, 4px rounded ends, a 2px gap between adjacent fills so segments read as
# separate rather than as one shape that happens to change colour.
_BAR_H = 15.0
_BAR_R = 4.0
_GAP = 2.0
_MIN_TICK_PX = 34.0   # smallest gap that keeps two date labels from touching


def esc(s) -> str:
    return html.escape(str(s))


def usd(x: float) -> str:
    """Money, at a precision that does not imply more than we know."""
    ax = abs(x)
    if ax >= 1000:
        return f"${x:,.0f}"
    if ax >= 1:
        return f"${x:,.2f}"
    return f"${x:.4f}"


def compact(n: float) -> str:
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= limit:
            return f"{n / limit:.1f}{suffix}".replace(".0", "")
    return f"{n:.0f}"


def pct(x: float) -> str:
    """A share, rounded so that it can never overstate at either boundary.

    368 of 369 calls is 99.7%, and rendering that as "100%" would tell a reader the log
    was fully covered when one call was not. So a value strictly below 1 never prints as
    100%, and a value strictly above 0 never prints as 0% -- the rounding is allowed to
    lose precision, never to change the claim."""
    if x <= 0.0:
        return "0%"
    if x >= 1.0:
        return "100%"
    v = x * 100.0
    if v >= 99.5:                       # would round up to a false 100%
        return f"{math.floor(v * 10) / 10:.1f}%"
    if v < 0.05:                        # would round down to a false 0%
        return "<0.1%"
    if v < 1.0:
        return f"{math.ceil(v * 10) / 10:.1f}%"
    return f"{v:.0f}%"


def _svg(body: str, *, w: float, h: float, title: str, cls: str = "chart") -> str:
    return (f"<svg class=\"{cls}\" viewBox=\"0 0 {w:.0f} {h:.0f}\" role=\"img\" "
            f"preserveAspectRatio=\"xMinYMin meet\">"
            f"<title>{esc(title)}</title>{body}</svg>")


def _rect(x, y, w, h, fill, *, r=_BAR_R, extra="", tip=None) -> str:
    """One mark. ``tip`` becomes a ``<title>`` child -- the accessible name, and the whole
    hover layer in a document with no JavaScript."""
    open_tag = (f"<rect x=\"{x:.2f}\" y=\"{y:.2f}\" width=\"{max(0.0, w):.2f}\" "
                f"height=\"{h:.2f}\" rx=\"{r:.1f}\" fill=\"{fill}\"{extra}")
    if tip is None:
        return open_tag + "/>"
    return f"{open_tag}><title>{esc(tip)}</title></rect>"


# ── magnitude: one hue, identity carried by the row label ────────────────────
def hbars(rows, *, value_fmt=usd, title="Distribution", label_w=190.0,
          track=True) -> str:
    """Horizontal bars, one per row. ``rows`` = ``[(label, value)]``.

    Normalised to the row maximum so a sub-cent value still reads as an intentional mark
    rather than an empty track."""
    rows = [(str(k), float(v)) for k, v in rows]
    if not rows:
        return ""
    vmax = max((v for _, v in rows), default=0.0) or 1.0
    bw, pad, row_h = 300.0, 6.0, 26.0
    h = pad * 2 + row_h * len(rows)
    w = label_w + bw + 76.0
    out = []
    for i, (label, v) in enumerate(rows):
        y = pad + i * row_h
        fill_w = max(0.0, v / vmax) * bw
        out.append(
            f"<text x=\"0\" y=\"{y + 15:.1f}\" class=\"bl\">{esc(_clip(label, 26))}</text>")
        if track:
            out.append(_rect(label_w, y + 4, bw, _BAR_H, "var(--chart-soft)"))
        out.append(_rect(label_w, y + 4, fill_w, _BAR_H, "var(--chart)",
                         tip=f"{label}: {value_fmt(v)}"))
        out.append(f"<text x=\"{w:.0f}\" y=\"{y + 16:.1f}\" text-anchor=\"end\" "
                   f"class=\"bv\">{esc(value_fmt(v))}</text>")
    return _svg("".join(out), w=w, h=h, title=title)


def columns(rows, *, value_fmt=usd, title="Over time", max_labels=12) -> str:
    """Vertical columns for an ordered series. ``rows`` = ``[(label, value)]``.

    Labels thin out rather than overlapping: a tick every ``n`` columns, always including
    the first and last, so the axis stays readable at any series length."""
    rows = [(str(k), float(v)) for k, v in rows]
    if not rows:
        return ""
    vmax = max((v for _, v in rows), default=0.0) or 1.0
    n = len(rows)
    plot_h, pad_t, pad_b = 130.0, 16.0, 30.0
    w = max(360.0, min(920.0, n * 26.0))
    col_w = (w / n) - _GAP
    h = plot_h + pad_t + pad_b
    # Tick spacing is driven by BOTH the label budget and the pixel pitch, then anchored
    # from the END of the series -- so the last column is always labelled and every gap is
    # exactly `step` columns wide. Special-casing "always label the last one" is what
    # produces a collision: the tail label can land one column from its neighbour.
    pitch = w / n
    step = max(1, -(-n // max_labels), -(-_MIN_TICK_PX // pitch) if pitch else 1)
    step = int(step)
    out = [f"<line x1=\"0\" y1=\"{pad_t + plot_h:.1f}\" x2=\"{w:.1f}\" "
           f"y2=\"{pad_t + plot_h:.1f}\" stroke=\"var(--line)\" stroke-width=\"1\"/>"]
    for i, (label, v) in enumerate(rows):
        ch = max(1.5, (v / vmax) * plot_h)
        x = i * (w / n)
        out.append(_rect(x, pad_t + plot_h - ch, col_w, ch, "var(--chart)", r=3.0,
                         tip=f"{label}: {value_fmt(v)}"))
        # The last column is always labelled, but only when it clears the previous
        # stepped label -- otherwise the two collide at a spacing of one column width.
        if (n - 1 - i) % step == 0:
            out.append(f"<text x=\"{x + col_w / 2:.1f}\" y=\"{pad_t + plot_h + 14:.0f}\" "
                       f"text-anchor=\"middle\" class=\"ax\">{esc(_short_day(label))}</text>")
    out.append(f"<text x=\"0\" y=\"{pad_t - 5:.0f}\" class=\"ax\">peak "
               f"{esc(value_fmt(vmax))}</text>")
    return _svg("".join(out), w=w, h=h, title=title)


def concentration(points, *, title="Spend concentration") -> str:
    """Cumulative share of spend against share of calls — the shape that changes minds.

    Drawn as a step area from the analyzer's sampled points, with an equal-spend diagonal
    for reference: the further the curve bows above the line, the more the bill is
    decided by a handful of calls. ``points`` = ``[(call_share, spend_share)]``."""
    pts = [(float(a), float(b)) for a, b in points if a is not None and b is not None]
    if not pts:
        return ""
    pts = [(0.0, 0.0)] + sorted(pts) + [(1.0, 1.0)]
    w, h, pad = 420.0, 190.0, 26.0
    px = lambda f: pad + f * (w - pad * 2)          # noqa: E731
    py = lambda f: h - pad - f * (h - pad * 2)      # noqa: E731
    poly = " ".join(f"{px(a):.2f},{py(b):.2f}" for a, b in pts)
    area = f"{px(0):.2f},{py(0):.2f} {poly} {px(1):.2f},{py(0):.2f}"
    out = [
        f"<polygon points=\"{area}\" fill=\"var(--chart)\" opacity=\".13\"/>",
        f"<line x1=\"{px(0):.2f}\" y1=\"{py(0):.2f}\" x2=\"{px(1):.2f}\" y2=\"{py(1):.2f}\" "
        f"stroke=\"var(--line)\" stroke-width=\"2\" stroke-dasharray=\"4 4\"/>",
        f"<polyline points=\"{poly}\" fill=\"none\" stroke=\"var(--chart)\" "
        f"stroke-width=\"2\" stroke-linejoin=\"round\" stroke-linecap=\"round\"/>",
    ]
    for a, b in pts[1:-1]:
        out.append(f"<circle cx=\"{px(a):.2f}\" cy=\"{py(b):.2f}\" r=\"4\" "
                   f"fill=\"var(--chart)\" stroke=\"var(--surface)\" stroke-width=\"2\">"
                   f"<title>top {esc(pct(a))} of calls = {esc(pct(b))} of spend</title>"
                   f"</circle>")
    out += [
        f"<text x=\"{px(0):.2f}\" y=\"{h - 7:.0f}\" class=\"ax\">fewest calls</text>",
        f"<text x=\"{px(1):.2f}\" y=\"{h - 7:.0f}\" text-anchor=\"end\" class=\"ax\">"
        f"all calls</text>",
        f"<text x=\"{px(0):.2f}\" y=\"{py(1) - 8:.0f}\" class=\"ax\">all spend</text>",
    ]
    return _svg("".join(out), w=w, h=h, title=title)


# ── the one two-hue chart: the measured / estimated honesty axis ─────────────
_MIN_SEG_PX = 3.0   # below this a segment is not a mark, it is a hairline nobody sees


def _split_widths(values, avail: float) -> list[float]:
    """Segment widths that sum to ``avail``, with every segment at least ``_MIN_SEG_PX``.

    The floor has to be paid for out of the other segments rather than added on top. It
    used to be a bare ``max(3.0, share)``, so a 99.99%/0.01% split produced 638px + 3px
    inside a 640px viewBox and the small segment was drawn at x=640 -- entirely off the
    canvas, clipped away. On the ONE chart reserved for the metered-versus-estimated axis,
    the segment that vanishes is the disclosure (AP-01), and the caption underneath went on
    describing a mark that was not there.

    Raising a segment to the floor can push another below it, so the allocation repeats
    until it settles (at most once per segment). If the floors cannot fit at all, every
    segment gets an equal share -- a bar that says "many tiny slices" is honest; one that
    silently drops some is not."""
    n = len(values)
    if not n:
        return []
    if n * _MIN_SEG_PX >= avail:
        return [avail / n] * n
    floored: set[int] = set()
    while True:
        rest = avail - _MIN_SEG_PX * len(floored)
        pool = sum(v for i, v in enumerate(values) if i not in floored) or 1.0
        widths = [_MIN_SEG_PX if i in floored else (v / pool) * rest
                  for i, v in enumerate(values)]
        new = {i for i, wd in enumerate(widths) if wd < _MIN_SEG_PX}
        if not new - floored:
            return widths
        floored |= new


def split_bar(segments, *, title="Composition", value_fmt=usd) -> str:
    """A single stacked bar. ``segments`` = ``[(label, value, css_var, dashed)]``.

    Reserved for the honesty axis. Segments are separated by a real surface gap and each
    is directly labelled, so the split survives being printed in greyscale or read by
    someone who cannot separate the two hues — colour is never the only carrier."""
    segs = [(str(k), float(v), c, bool(d)) for k, v, c, d in segments if float(v) > 0]
    if not segs:
        return ""
    total = sum(v for _, v, _, _ in segs) or 1.0
    w, bar_y = 640.0, 8.0
    h = bar_y + _BAR_H + 34.0
    widths = _split_widths([v for _, v, _, _ in segs], w - _GAP * (len(segs) - 1))
    x = 0.0
    out = []
    for (label, v, colour, dashed), seg_w in zip(segs, widths):
        extra = (f" stroke=\"{colour}\" stroke-width=\"1.5\" stroke-dasharray=\"5 3\""
                 f" fill-opacity=\".22\"" if dashed else "")
        out.append(_rect(x, bar_y, seg_w, _BAR_H, colour, extra=extra,
                         tip=f"{label}: {value_fmt(v)} ({pct(v / total)})"))
        # A narrow trailing segment would push a left-anchored label off the edge, so
        # labels in the right-hand third anchor to the segment's END instead.
        far = x > w * 0.66
        anchor = f" text-anchor=\"end\"" if far else ""
        lx = min(x + seg_w, w) if far else min(x, w)
        out.append(f"<text x=\"{lx:.2f}\" y=\"{bar_y + _BAR_H + 18:.1f}\"{anchor} "
                   f"class=\"bl\">{esc(label)}</text>")
        out.append(f"<text x=\"{lx:.2f}\" y=\"{bar_y + _BAR_H + 32:.1f}\"{anchor} "
                   f"class=\"bv\">{esc(value_fmt(v))} &middot; {esc(pct(v / total))}</text>")
        x += seg_w + _GAP
    return _svg("".join(out), w=w, h=h, title=title)


def compare_bars(rows, *, value_fmt=usd, title="Comparison") -> str:
    """Two marks per row for an observed-versus-reference comparison.
    ``rows`` = ``[(label, observed, reference)]``.

    The reference mark is dashed and outlined rather than filled: it is arithmetic about a
    price that was never paid, and it should not look like a measurement sitting beside
    one."""
    rows = [(str(k), float(a), float(b)) for k, a, b in rows]
    if not rows:
        return ""
    vmax = max((max(a, b) for _, a, b in rows), default=0.0) or 1.0
    label_w, bw, row_h, pad = 170.0, 300.0, 44.0, 6.0
    w, h = label_w + bw + 96.0, pad * 2 + row_h * len(rows)
    out = []
    for i, (label, obs, ref) in enumerate(rows):
        y = pad + i * row_h
        out.append(f"<text x=\"0\" y=\"{y + 15:.1f}\" class=\"bl\">"
                   f"{esc(_clip(label, 22))}</text>")
        out.append(_rect(label_w, y + 3, (obs / vmax) * bw, 13, "var(--chart)", r=3.5,
                         tip=f"{label} observed: {value_fmt(obs)}"))
        out.append(f"<text x=\"{w:.0f}\" y=\"{y + 13:.1f}\" text-anchor=\"end\" "
                   f"class=\"bv\">{esc(value_fmt(obs))}</text>")
        out.append(_rect(label_w, y + 20, (ref / vmax) * bw, 13, "var(--est)", r=3.5,
                         extra=" fill-opacity=\".20\" stroke=\"var(--est)\""
                               " stroke-width=\"1.5\" stroke-dasharray=\"5 3\"",
                         tip=f"{label} at the cheapest rung: {value_fmt(ref)}"))
        out.append(f"<text x=\"{w:.0f}\" y=\"{y + 30:.1f}\" text-anchor=\"end\" "
                   f"class=\"bv\">{esc(value_fmt(ref))}</text>")
    return _svg("".join(out), w=w, h=h, title=title)


# ── helpers ──────────────────────────────────────────────────────────────────
def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _short_day(s: str) -> str:
    """``2026-08-05`` -> ``08-05``; anything else is left alone."""
    return s[5:] if len(s) == 10 and s[4] == "-" else s
