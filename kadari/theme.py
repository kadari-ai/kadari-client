"""The visual system, as CSS. One token block, shared by every surface that renders it.

Kadari's identity is glossy translucent amber glass on a warm-white canvas. The tokens
below are the hand-mirrored form of the design system; both the free local report and the
concierge report import them from here rather than each keeping a copy, because three
hand-maintained copies of a colour ramp is three chances for them to drift apart.

## Colour is assigned by the job it does, and the assignment was validated, not eyeballed

**Amber is brand chrome, never a data state.** That is the whole reason the honesty
colours are green and amber-dark: if the brand colour also meant "verified", nothing on
the page could distinguish decoration from claim.

* **Magnitude** (spend by model, spend per day, concentration, cost-per-call) uses ONE
  hue, ``--chart``. These are single-series charts: bigger means more, and identity is
  carried by the axis label, not by colour. A categorical palette here would imply a
  distinction that isn't in the data.
* **The honesty axis** — measured versus estimated — is the only place two hues carry
  identity: ``--proven`` green (metered, solid) against ``--est`` amber (estimated,
  dashed). Distinguished by hue AND texture AND label, never colour alone.
* **Status** colours stay reserved. They never become "series 3".

``--chart`` is ``#bd7a1c`` rather than the brand ``--accent`` ``#c9821f`` for one measured
reason: at the warm-white surface the brand amber lands at 2.98:1, just under the 3:1
floor a filled mark needs. The darker step clears it. The green/amber honesty pair passes
every check including protanopia separation (ΔE 9.1).

**Light only, deliberately.** The system commits to a single warm-white look — it is
print-first, and a report is a document someone forwards, prints and files. Inventing a
dark variant here would fork a system whose whole point is that its two surfaces don't
fork. Print rules flatten the glass to ink on white rather than dropping to a second theme.
"""

from __future__ import annotations

# ── Tokens ───────────────────────────────────────────────────────────────────
# Transcribed by hand from Kadari's design system (a Tailwind v4 `@theme` block) rather
# than imported or built, so a rendered report keeps zero runtime dependencies and opens
# with no network. Values are duplicated on purpose: the alternative is a build step or a
# dependency, and this file is read by people auditing what the report will do.
TOKENS = """
:root{
 --ink:#1c1a16;--ink-soft:#6b6458;--line:#ece4d6;--bg:#fbf9f4;--surface:#fff;
 --surface-2:#f6f1e8;--accent:#c9821f;--accent-ink:#8f5d10;--accent-bg:#fbf2e2;
 --chart:#bd7a1c;--chart-soft:#f0e3cd;
 --est:#b8860b;--est-ink:#8a6608;--est-bg:#fdf6e6;
 --proven:#1a7f4b;--proven-ink:#136437;--proven-bg:#eaf6ef;
 --warn:#9a6a00;--warn-bg:#fbf3e0;--alarm:#b3261e;--alarm-bg:#fdeceb;
 --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
 --glass:rgba(255,252,246,.62);--glass-amber:rgba(252,242,225,.64);--glass-2:rgba(255,255,255,.45);
 --glass-stroke:rgba(255,255,255,.72);--glass-edge:rgba(120,84,18,.14);--blur:20px;
 --amber-glow:rgba(224,150,47,.28);
 --shadow-1:0 1px 2px rgba(60,42,12,.06);
 --shadow-2:0 1px 2px rgba(60,42,12,.06),0 16px 36px -18px rgba(120,84,18,.26);
 --shadow-lift:0 2px 6px rgba(60,42,12,.08),0 26px 52px -20px rgba(120,84,18,.34);
 --grad-accent:linear-gradient(135deg,#e0962f,#9a6309);
 --grad-sheen:linear-gradient(180deg,rgba(255,255,255,.72),rgba(255,255,255,.06) 44%,rgba(255,255,255,0) 70%);
 --aurora:radial-gradient(52% 42% at 16% 4%,rgba(224,150,47,.18),transparent 60%),
  radial-gradient(44% 38% at 88% 2%,rgba(201,130,31,.13),transparent 58%),
  radial-gradient(48% 42% at 82% 98%,rgba(26,127,75,.06),transparent 62%),
  radial-gradient(52% 46% at 2% 98%,rgba(224,150,47,.09),transparent 60%);
 --fs-hero:40px;--fs-stat:34px;--fs-h2:11.5px;--fs-body:15px;--fs-cap:12.5px;
 --r-lg:20px;--r-md:14px;--r-sm:9px;--ease:cubic-bezier(.22,.61,.36,1)}
"""

# ── Layout, motion, print ────────────────────────────────────────────────────
_LAYOUT = """
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);position:relative;
 font:var(--fs-body)/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 -webkit-font-smoothing:antialiased}
body::before{content:"";position:fixed;inset:0;z-index:-1;
 background:var(--aurora),var(--bg);background-attachment:fixed}
.wrap{max-width:1000px;margin:0 auto;padding:0 24px 72px}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.glass{position:relative;background:var(--glass);border:1px solid var(--glass-edge);
 border-top-color:var(--glass-stroke);border-left-color:var(--glass-stroke);
 border-radius:var(--r-md);box-shadow:var(--shadow-2);
 backdrop-filter:blur(var(--blur)) saturate(150%);
 -webkit-backdrop-filter:blur(var(--blur)) saturate(150%);overflow:hidden}
.glass::before{content:"";position:absolute;inset:0;pointer-events:none;
 background:var(--grad-sheen);opacity:.65}
.glass>*{position:relative}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){
 .glass{background:var(--surface)}}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
.fx{animation:rise .6s var(--ease) both}
.d1{animation-delay:.04s}.d2{animation-delay:.10s}.d3{animation-delay:.16s}
.d4{animation-delay:.22s}.d5{animation-delay:.28s}.d6{animation-delay:.34s}
.head{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;
 flex-wrap:wrap;padding:18px 22px;margin:24px 0 20px}
.brand{font-weight:650;letter-spacing:-.01em;font-size:19px}
.brand b{color:var(--accent-ink)}
.sub{color:var(--ink-soft);font-size:var(--fs-cap)}
.panel{padding:20px 22px;margin:0 0 16px}
h2{font-size:var(--fs-h2);text-transform:uppercase;letter-spacing:.09em;
 color:var(--ink-soft);margin:0 0 14px;font-weight:650}
.hero{padding:26px 22px 22px;margin:0 0 16px}
.stat{font-size:var(--fs-hero);font-weight:680;letter-spacing:-.02em;line-height:1.05;
 font-family:var(--mono);font-variant-numeric:tabular-nums}
.qual{color:var(--ink-soft);font-size:var(--fs-cap);margin:8px 0 0;max-width:62ch}
.cap{color:var(--ink-soft);font-size:var(--fs-cap);margin:10px 0 0;max-width:78ch}
.grid{display:flex;flex-wrap:wrap;gap:14px}
.tile{flex:1 1 190px;padding:14px 16px;background:var(--surface);
 border:1px solid var(--line);border-radius:var(--r-sm)}
.tile .k{color:var(--ink-soft);font-size:11px;text-transform:uppercase;
 letter-spacing:.07em;font-weight:650}
.tile .v{font-size:22px;font-weight:660;margin-top:4px;font-family:var(--mono);
 font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:14px;margin-top:6px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
th{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink-soft)}
td.r,th.r{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.chart{display:block;width:100%;height:auto;max-width:100%;overflow:visible}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;
 font-weight:650;letter-spacing:.04em;text-transform:uppercase;vertical-align:2px}
.badge.est{background:var(--est-bg);color:var(--est-ink);border:1px dashed var(--est)}
.badge.ok{background:var(--proven-bg);color:var(--proven-ink);border:1px solid var(--proven)}
.badge.warn{background:var(--warn-bg);color:var(--warn);border:1px solid var(--warn)}
.note{border-left:3px solid var(--line);padding:2px 0 2px 13px;margin:12px 0 0;
 color:var(--ink-soft);font-size:var(--fs-cap);max-width:78ch}
.note.est{border-left-color:var(--est);border-left-style:dashed}
.note.warn{border-left-color:var(--warn)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:var(--fs-cap);
 color:var(--ink-soft);margin:0 0 10px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;
 margin-right:6px;vertical-align:-1px}
.cta{margin-top:22px;padding:20px 22px}
.cta h3{margin:0 0 8px;font-size:17px;letter-spacing:-.01em}
a{color:var(--accent-ink);text-underline-offset:2px}
.cta p{margin:0 0 8px;color:var(--ink-soft);font-size:14px;max-width:70ch}
.foot{color:var(--ink-soft);font-size:11.5px;margin:26px 0 0;text-align:center}
svg text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.bl{font-size:12px;fill:var(--ink)}
.bv{font-size:12px;fill:var(--ink-soft);font-family:var(--mono)}
.ax{font-size:10.5px;fill:var(--ink-soft)}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media print{
 body::before{display:none}
 body{background:#fff}
 .glass,.hero,.head,.panel,.tile{background:#fff!important;box-shadow:none!important;
  backdrop-filter:none!important;border:1px solid var(--line)!important}
 .glass::before{display:none}
 .wrap{max-width:none;padding:0}
 .panel,.hero{break-inside:avoid}}
"""

BASE_CSS = TOKENS + _LAYOUT
