"""``kadari`` — read a log, see what it cost, and package it if you want it evaluated.

Five verbs, no configuration, no account, no network:

  ``kadari demo``                     a real report from a bundled sample, in one command
  ``kadari analyze LOG -o report.html`` your own log
  ``kadari import --from SOURCE F``   convert a provider export or gateway log first
  ``kadari bundle LOG``               package it for submission (writes a file; sends nothing)
  ``kadari version``                  what you are running, and how old the prices are

``demo`` comes first deliberately: someone evaluating a stranger's tool should be able to
see exactly what it produces before pointing it at their own production traffic.

Nothing here opens a browser or a socket — the guard forbids importing the modules that
could, and `bundle` writes a file and prints where it is rather than transmitting it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, analyze, bundle, logs, preflight, prices, report
from .capture import WIRE_VERSION

_SAMPLE = Path(__file__).with_name("sample_capture.jsonl")
EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 2, 3


def _load_table(path):
    try:
        return prices.load(path)
    except prices.PriceTableError as exc:
        _die(f"price table: {exc}")


def _die(msg: str):
    print(f"kadari: {msg}", file=sys.stderr)
    raise SystemExit(EXIT_ERROR)


def _read(path, table):
    p = Path(path)
    if not p.exists():
        _die(f"no such log: {p}")
    calls, meta, warns = logs.read(p)
    if not calls:
        detail = f" ({warns[0]})" if warns else ""
        _die(f"no usable calls in {p}{detail}.\n"
             f"       If this is a provider usage export or a gateway log, convert it "
             f"first:\n"
             f"         kadari import {p} --from SOURCE -o capture.jsonl\n"
             f"       Sources: `kadari import --help`.")
    return calls, meta, warns


def _model(path, table):
    calls, meta, warns = _read(path, table)
    return analyze.analyze(calls, table=table, meta=meta, warnings=warns), calls


# ── verbs ────────────────────────────────────────────────────────────────────
def cmd_analyze(args) -> int:
    table = _load_table(args.prices)
    model, _ = _model(args.log, table)
    if args.json:
        print(json.dumps(model, indent=2, sort_keys=True))
        return EXIT_OK
    print(report.render_text(model))
    if args.out:
        out = Path(args.out)
        out.write_text(report.render_html(model), encoding="utf-8")
        # Deliberately NOT opened for you: launching a browser needs a module the client
        # is forbidden to import, and a tool that promises to open no connection should
        # not be starting programs either.
        print(f"Wrote {out}  — open it in any browser; it works offline.")
    return EXIT_OK


def cmd_demo(args) -> int:
    """The whole tool, on data we ship, so nobody has to risk their own log to evaluate it."""
    table = _load_table(args.prices)
    if not _SAMPLE.exists():
        _die("the bundled sample is missing from this installation")
    model, _ = _model(_SAMPLE, table)
    print(report.render_text(model))
    out = Path(args.out or "kadari_demo_report.html")
    out.write_text(report.render_html(model, title="Sample spend report"), encoding="utf-8")
    print(f"Wrote {out}  — this is synthetic data shipped with the package, not yours.")
    print("Point `kadari analyze` at your own log to see the real thing.")
    return EXIT_OK


def cmd_import(args) -> int:
    from . import importers
    try:
        rows, warns = importers.read(args.source, args.file)
    except KeyError:
        _die(f"unknown source {args.source!r}. Available:\n{importers.describe()}")
    except OSError as exc:
        _die(f"could not read {args.file}: {exc}")
    if not rows:
        for w in warns:
            print(f"  (!) {w}", file=sys.stderr)
        _die(f"nothing importable in {args.file}")
    out = Path(args.out)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("// " + json.dumps({"kadari_log": {
            # Read from the one constant, never spelled again here. An import writes the
            # same records `capture` does -- including, since 0.4, the cache buckets --
            # so a literal pinned beside it silently drifts the moment the format moves,
            # and a manifest that understates its own version tells a reader fields may
            # not appear that already do.
            "version": WIRE_VERSION, "client": f"kadari/{__version__}",
            "imported_from": args.source, "source_file": Path(args.file).name,
            "sample": 1.0 if args.complete else None,
            # Written into the log, not just printed. `analyze` runs later, often much
            # later, and reads only the file -- so a terminal-only warning meant a report
            # rendered from an import whose dollar-dominant rows were REFUSED still said
            # "100% of your log", in the durable HTML and in the submission bundle. A
            # caveat that does not survive into the artifact is not a caveat.
            "warnings": warns,
        }}, ensure_ascii=False) + "\n")
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Imported {len(rows):,} record(s) from {args.file} → {out}")
    for w in warns:
        print(f"  (!) {w}")
    if not args.complete:
        print("  (!) The sample rate is recorded as UNKNOWN. If this export covers all of "
              "your traffic,\n      re-run with --complete so the report can say so; "
              "otherwise totals stay honest about\n      being a slice.")
    print(f"\nNow: kadari analyze {out} -o report.html")
    return EXIT_OK


def cmd_bundle(args) -> int:
    table = _load_table(args.prices)
    model, calls = _model(args.log, table)
    pf = preflight.check(calls, table=table)
    print(preflight.render_text(pf))
    if not pf.submittable:
        print("\nNot packaged. Sending this would only produce a disappointing email.",
              file=sys.stderr)
        return EXIT_REFUSED
    # Next to the LOG, which is what the docstring promises and what a sender expects.
    # `.name` dropped the directory, so `kadari bundle /var/log/prod.jsonl` wrote a zip of
    # production prompts into whatever directory the command was run from -- frequently a
    # repo, where `git add .` would commit it into permanent history.
    log_path = Path(args.log)
    out = Path(args.out) if args.out else \
        log_path.parent / (log_path.with_suffix("").name + "_kadari_submission.zip")
    manifest = bundle.build(args.log, out, report_text=report.render_text(model),
                            preflight=pf.as_dict(), prices_as_of=table.as_of,
                            redact=args.redact, note=args.note)
    print(bundle.render_manifest(manifest, out, size_bytes=out.stat().st_size))
    print(f"Nothing has been sent. Upload it yourself at {report.SUBMIT_URL} when you "
          f"are ready.")
    return EXIT_OK


def cmd_version(args) -> int:
    table = _load_table(args.prices)
    print(f"kadari {__version__}")
    print(f"price table: {len(table.models)} models, as of {table.as_of}")
    for s in table.sources:
        print(f"  {s.get('provider', '?'):<12} checked {s.get('checked', '?')}  "
              f"{s.get('url', '')}")
    print("Prices decay. If a figure matters, re-check the source before relying on it.")
    return EXIT_OK


# ── wiring ───────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kadari",
        description="See what your LLM traffic costs. Runs locally; sends nothing.",
        epilog="Docs and the savings evaluation: " + report.SUBMIT_URL)
    p.add_argument("--prices", metavar="FILE",
                   help="use your own rate table instead of the shipped snapshot "
                        "(negotiated pricing, or a model we do not list)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="render a spend report from a capture log")
    a.add_argument("log")
    a.add_argument("-o", "--out", metavar="FILE", help="also write a self-contained HTML report")
    a.add_argument("--json", action="store_true", help="print the underlying spend model")
    a.set_defaults(func=cmd_analyze)

    d = sub.add_parser("demo", help="render a report from the bundled sample log")
    d.add_argument("-o", "--out", metavar="FILE")
    d.set_defaults(func=cmd_demo)

    i = sub.add_parser("import", help="convert a provider export or gateway log")
    i.add_argument("file")
    i.add_argument("--from", dest="source", required=True, metavar="SOURCE")
    i.add_argument("-o", "--out", default="kadari_capture.jsonl")
    i.add_argument("--complete", action="store_true",
                   help="assert this export covers ALL of your traffic, not a sample")
    i.set_defaults(func=cmd_import)

    b = sub.add_parser("bundle", help="package a log for evaluation (writes a file; sends nothing)")
    b.add_argument("log")
    b.add_argument("-o", "--out", metavar="FILE")
    b.add_argument("--redact", action="store_true",
                   help="scrub emails, IPs and card/phone-like digit runs first "
                        "(best effort, not a compliance control)")
    b.add_argument("--note", metavar="TEXT", help="a note to include in the manifest")
    b.set_defaults(func=cmd_bundle)

    v = sub.add_parser("version", help="version, and how old the price table is")
    v.set_defaults(func=cmd_version)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:                     # `| head` should not print a traceback
        return EXIT_OK
    except KeyboardInterrupt:
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
