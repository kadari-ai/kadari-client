#!/usr/bin/env python3
"""Validate that the shipped `kadari` capture client is a buildable, IP-free package.

Run from CI (`make client`). Two layers, both zero-network and deterministic:

  1. BUILD  -- if `build` + `setuptools` are importable, run a real, NON-isolated
     `python -m build` into a temp dir (no network: --no-isolation reuses the
     installed backend) and assert a wheel + sdist are produced and that the wheel
     contains `kadari/py.typed` and **none** of Kadari's engine modules. If the
     build backend is absent (as in a stdlib-only CI), fall back to a STDLIB
     PACKAGING SMOKE that checks the same invariants statically:
       - pyproject parses and declares the metadata a distribution needs;
       - the package imports in ISOLATION with only the client dir on the path
         (proves zero-dependency), while an engine import FAILS there (proves no
         engine is bundled / importable from the shipped artifact);
       - the PEP 561 `py.typed` marker and the LICENSE file are present.
  2. GUARD  -- mechanically re-assert the no-engine-code / no-network property over
     the client source (the same property `tests/test_kadari_client.py` covers,
     surfaced here so CI shows it ran even outside the unittest suite).

Exit 0 on success, non-zero (with a reason) on any failure. Stdlib only.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # client/
PKG = HERE / "kadari"
# NOTE: there is deliberately no `SRC` here any more. The guard used to read the sibling
# engine tree to learn what to forbid, which made it strictly weaker in the one place it
# matters most -- the standalone public repo, where that sibling does not exist. It now
# depends on nothing outside this directory and runs at full strength after the split.


# ── The engine-name rule: an ALLOWLIST of the harmless, never a list of the secret ───
#
# Every engine package is named `kadari_<something>`; the client itself is the single
# underscore-free `kadari`. So the rule needs no list of engine names at all -- it forbids
# the SHAPE, and enumerates only the handful of `kadari_*` tokens that legitimately ship.
#
# This inversion is the point. The guard used to hold a denylist derived from `../src`,
# with a hard-coded fallback for when that directory is absent -- which is precisely the
# standalone public repo, i.e. exactly when the guard is load-bearing and exactly where the
# list would be published. Seven engine package names in a permanently-public file, in a
# file the guard then had to exempt from its own textual scan so it would not trip over
# itself. Naming what must stay secret in order to check that it stayed secret is a shape
# worth not having.
#
# The allowlist below is strictly STRONGER than the denylist it replaces, not merely
# quieter: a new engine package is caught the day it is created, with nobody remembering to
# add it anywhere. It also lets the guard scan itself, since it no longer contains anything
# it is looking for.
_ENGINE_NAME_RE = re.compile(r"kadari_[a-z0-9_]+")

# `kadari_*` tokens that are NOT engine packages and do legitimately appear in shipped
# files. Each is a filename, a JSON key or a documented artifact name -- none is a module,
# and none reveals anything about the engine. Adding to this list is a deliberate act: a
# new token fails the guard until it is justified here.
BENIGN_KADARI_TOKENS = frozenset({
    "kadari_capture",       # the default capture-log filename, in docs and docstrings
    "kadari_log",           # the `//` manifest key inside a capture log
    "kadari_submission",    # the manifest key + filename suffix `kadari bundle` writes
    "kadari_demo_report",   # the default output filename of `kadari demo`
    "kadari_client",        # only as `tests/test_kadari_client.py`, the wire-contract test
})


def engine_name_hits(text: str) -> list[str]:
    """Every `kadari_*` token in ``text`` that is not on the benign allowlist.

    One function so the textual scan, the wheel scan and the tests cannot drift apart in
    what they consider a leak."""
    return sorted({m for m in _ENGINE_NAME_RE.findall(text)
                   if m not in BENIGN_KADARI_TOKENS})


def is_engine_module(root: str) -> bool:
    """Is this imported root module an engine package? Every engine package is
    `kadari_<something>`; the shipped client is the underscore-free `kadari`."""
    return root.startswith("kadari_")


# Root modules that open (or shell out to) the network. The client opens NO connection
# (rule 10 / local-first), so importing any of these -- under any alias or `from` form --
# is forbidden. Matched on the IMPORTED module root via AST, so `from requests import get`
# and `import asyncio as aio` are both caught.
#
# Three groups, and the second is the one the first version of this list forgot: a module
# does not have to speak a protocol to reach the network, it only has to hand off to
# something that does. `subprocess` was listed from the start; `multiprocessing` (which
# spawns and talks over sockets), `socketserver`, `pty` and `ctypes` (arbitrary libc, so
# arbitrary syscalls) are the same hole under other names.
NETWORK_MODULES = frozenset({
    # 1. speaks a protocol
    "socket", "ssl", "asyncio", "selectors", "http", "urllib", "ftplib", "smtplib",
    "poplib", "imaplib", "telnetlib", "nntplib", "xmlrpc", "webbrowser", "socketserver",
    "wsgiref", "asyncore", "asynchat", "smtpd",
    # 2. hands off to something that does
    "subprocess", "multiprocessing", "ctypes", "pty",
    # 3. third-party clients and provider SDKs (the client is zero-dependency, so any of
    #    these is already a packaging violation -- listed so it fails as the RIGHT one)
    "requests", "httpx", "aiohttp", "urllib3", "websocket", "websockets", "grpc",
    "pycurl", "httplib2", "curl_cffi", "paramiko", "boto3", "botocore", "zmq", "socks",
    "tornado", "twisted", "flask", "fastapi", "starlette", "uvicorn",
    "openai", "anthropic", "google", "cohere", "mistralai", "ollama",
})

# Calls that resolve a module at RUNTIME, so the AST import scan above cannot see what
# they load: `__import__("soc" + "ket")` imports the same module as `import socket` and
# looks like nothing at all. The shipped client imports statically -- it is ~15 stdlib-only
# modules with no plugin surface -- so the rule is that these do not appear, rather than a
# doomed attempt to evaluate their arguments.
DYNAMIC_IMPORT_CALLS = frozenset({"__import__", "import_module", "load_module",
                                  "exec_module", "module_from_spec"})

# Metadata a real distribution needs (task 1): without these a wheel is publishable
# but anaemic. We assert presence, not specific values.
REQUIRED_PROJECT_FIELDS = (
    "name", "version", "description", "requires-python", "readme", "license",
    "authors", "keywords", "classifiers",
)


def _fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"packaging_check: FAIL -- {msg}", file=sys.stderr)
    raise SystemExit(1)


def _ok(msg: str) -> None:
    print(f"packaging_check: OK -- {msg}")


def _load_pyproject() -> dict:
    with open(HERE / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def _imported_roots(tree: ast.AST) -> set[str]:
    """Root module names imported by a source file (`import a.b` / `from a.b import c`
    -> {'a'}; a relative `from . import x` contributes nothing)."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:      # absolute import only
                roots.add(node.module.split(".")[0])
    return roots


def _dynamic_import_calls(tree: ast.AST) -> set[str]:
    """Names called in this file that resolve a module at runtime (see
    ``DYNAMIC_IMPORT_CALLS``). Matched on the called name -- `__import__(...)`,
    `importlib.import_module(...)`, `spec.loader.exec_module(...)` -- because the argument
    is an expression we would have to EXECUTE to know what it loads, and a guard does not
    run the code it is guarding."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.id if isinstance(func, ast.Name) else
                func.attr if isinstance(func, ast.Attribute) else None)
        if name in DYNAMIC_IMPORT_CALLS:
            found.add(name)
    return found


# Directories that are build output, never input: they are regenerated from the tracked
# tree and are excluded from BOTH the guard and the hygiene check. `.gitignore` is what
# keeps them out of a public repo (see `check_public_repo_hygiene`); the guard's job is the
# source they are built from.
_TRANSIENT_DIRS = frozenset({
    "build", "dist", "__pycache__", ".git", ".venv", "venv", ".pytest_cache",
    ".ruff_cache", ".mypy_cache",
})

# There is deliberately NO exemption from the textual engine-name scan. There used to be
# one -- this file, because holding the list of forbidden names was its job -- and an
# exemption is a hole whether or not anyone has walked through it: nothing prevented a
# second engine reference from being added to an exempt file. Now that the rule is a shape
# rather than a list (see `_ENGINE_NAME_RE`), the guard contains nothing it is looking for
# and can be checked by itself, like every other file that ships.

# Exempt from the NETWORK-import check only. This file is the CI harness, not part of the
# wheel (`[tool.setuptools.packages.find]` ships `kadari*` alone), and it must shell out to
# run the isolated-import subprocess and `python -m build`. The exemption is scoped to the
# network rule: the engine-import and engine-name rules still apply to everything, and
# every file that actually ships is checked under all three.
#
# Keyed on the path RELATIVE TO `HERE`, not on the basename. Basename-keyed, the exemption
# covered any file called `packaging_check.py` anywhere in the tree -- including inside the
# package that ships -- so `kadari/packaging_check.py` could have imported `socket` and
# passed. An exemption is a hole whether or not anyone has walked through it; this one is
# now exactly one file wide.
_NETWORK_SCAN_EXEMPT = frozenset({"packaging_check.py"})   # POSIX paths, relative to HERE

# Binary/opaque suffixes the textual scan cannot meaningfully read. Kept short on purpose:
# anything not listed here is scanned as UTF-8, and a file that will not decode is a
# failure (an unreadable file is an unchecked file).
_BINARY_SUFFIXES = frozenset({".whl", ".tar.gz", ".gz", ".zip", ".png", ".jpg", ".jpeg",
                              ".gif", ".ico", ".pdf", ".woff", ".woff2", ".ttf", ".so",
                              ".dylib", ".pyc"})


def _shipped_files() -> list[Path]:
    """Every file in the client tree that is source rather than build output.

    Deliberately the WHOLE tree, not ``kadari/**/*.py``. `client/` is the directory that
    becomes the public repository, so the guard's unit of protection is the directory, not
    the importable package: `prices.json` and `sample_capture.jsonl` ship as package-data,
    `README.md` becomes the PyPI long description, and any `.md` sitting here rides along
    into public history at the moment of the split. A guard that reads only Python is a
    guard that reads none of those."""
    out = []
    for path in sorted(HERE.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _TRANSIENT_DIRS or part.endswith(".egg-info")
               for part in path.relative_to(HERE).parts):
            continue
        out.append(path)
    return out


def guard_no_engine_no_network() -> None:
    """Mechanical IP/network guard over everything the client tree ships. Two layers:
      * AST imports (Python only) -- no engine package and no network module is IMPORTED
        (catches aliased / `from x import y` forms a substring scan would miss);
      * textual scan (EVERY file) -- no engine package NAME appears anywhere: in a string,
        a comment, a JSON value, a markdown paragraph, or package metadata.
    Neither layer holds a LIST of engine packages: the rule is the `kadari_*` SHAPE plus a
    small allowlist of benign tokens, so a package created tomorrow is covered with nothing
    to update. (It used to derive a denylist from `../src`, which meant it degraded to a
    frozen fallback in exactly the standalone repo where it is load-bearing -- and forced an
    exemption for this file, since a guard naming what it protects cannot scan itself.)

    ``rglob``, not ``glob``: a subpackage (``kadari/importers/tabular.py``) is shipped in
    the wheel exactly like a top-level module, so scanning only the top level would let an
    engine import or a socket-opening module ride along completely unchecked.

    Every file, not only ``*.py``: the earlier version scanned Python alone, and an engine
    reference planted in ``prices.json`` passed the whole check green. Both shipped data
    files and the README reach the public unaltered, so "what ships" is the right unit --
    and the two internal runbooks that used to live here (the PyPI release runbook and the
    concierge onboarding guide, which between them named all seven engine packages and the
    engine CLI) now live under ``operations/`` where this directory cannot sweep them up.
    """
    files = _shipped_files()
    py_files = [p for p in files if p.suffix == ".py"]
    if not py_files:
        _fail("no client source files found under kadari/")
    for path in files:
        rel = path.relative_to(HERE)
        if path.suffix in _BINARY_SUFFIXES:
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            _fail(f"{rel} cannot be read as text and is therefore unchecked "
                  f"({exc}); add its suffix to _BINARY_SUFFIXES only if it truly ships "
                  f"opaque bytes")
        if path.suffix == ".py":
            try:
                tree = ast.parse(src, filename=str(path))
            except SyntaxError as exc:
                _fail(f"{rel} does not parse: {exc}")
            roots = _imported_roots(tree)
            exempt_from_network = rel.as_posix() in _NETWORK_SCAN_EXEMPT
            bad_engine = sorted(r for r in roots if is_engine_module(r))
            bad_net = sorted(roots & NETWORK_MODULES
                             if not exempt_from_network else set())
            bad_dyn = sorted(_dynamic_import_calls(tree)
                             if not exempt_from_network else set())
            if bad_engine:
                _fail(f"{rel} IMPORTS engine package(s) {bad_engine} -- the shipped "
                      f"client must carry none of Kadari's engine IP")
            if bad_net:
                _fail(f"{rel} IMPORTS network module(s) {bad_net} -- the client opens "
                      f"no connection (rule 10 / local-first)")
            if bad_dyn:
                _fail(f"{rel} resolves a module at RUNTIME via {bad_dyn} -- the import "
                      f"scan above cannot see what that loads, and the shipped client has "
                      f"no plugin surface that needs it")
        hits = engine_name_hits(src)
        if hits:
            _fail(f"{rel} references engine package name(s) {hits} -- no engine IP (incl. "
                  f"by reference) may ship in the client. Every `kadari_*` name is an "
                  f"engine package; if this one is a filename or a JSON key rather than a "
                  f"module, add it to BENIGN_KADARI_TOKENS and say why")
    _ok(f"no-engine-code / no-network guard clean across {len(files)} shipped file(s), "
        f"{len(py_files)} of them Python (rule: no `kadari_*` name may ship, "
        f"{len(BENIGN_KADARI_TOKENS)} benign tokens allowlisted; "
        f"network modules: {len(NETWORK_MODULES)}, "
        f"runtime-import calls: {len(DYNAMIC_IMPORT_CALLS)})")


def check_metadata() -> None:
    data = _load_pyproject()
    project = data.get("project")
    if not isinstance(project, dict):
        _fail("pyproject.toml has no [project] table")
    missing = [f for f in REQUIRED_PROJECT_FIELDS if f not in project]
    if missing:
        _fail(f"[project] is missing required metadata: {', '.join(missing)}")
    if project.get("dependencies"):
        _fail(f"client must be zero-dependency; found {project['dependencies']!r}")
    if "build-system" not in data:
        _fail("pyproject.toml has no [build-system] table")
    _ok(f"pyproject metadata complete for {project['name']} {project['version']} "
        f"(license={project['license']}, deps={project.get('dependencies', [])})")


def check_version_consistency() -> None:
    """The package version is declared in pyproject AND as kadari.__version__; a release
    bumps one and forgets the other unless they are gated equal here."""
    proj_version = _load_pyproject()["project"]["version"]
    init_version = None
    tree = ast.parse((PKG / "__init__.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__version__" \
                        and isinstance(node.value, ast.Constant):
                    init_version = node.value.value
    if init_version is None:
        _fail("kadari/__init__.py declares no __version__ literal")
    if init_version != proj_version:
        _fail(f"version drift: pyproject={proj_version!r} but "
              f"kadari.__version__={init_version!r}")
    _ok(f"version consistent across pyproject + __init__ ({proj_version})")


def check_marker_and_license() -> None:
    if not (PKG / "py.typed").exists():
        _fail("kadari/py.typed (PEP 561 marker) is missing")
    if not (HERE / "LICENSE").exists():
        _fail(f"LICENSE is missing from {HERE}")
    _ok("py.typed marker + LICENSE present")


def check_price_table() -> None:
    """The price table is shipped DATA, and a report is only as honest as its provenance.

    Asserts the file parses, is declared as package-data (absent from the wheel it would
    install fine and silently report every call as unpriced), carries an ``as_of`` and a
    source URL per provider, and that every ladder rung is itself priced -- a ladder
    pointing at an unpriced rung would compute a price-differential ceiling from a rate
    that does not exist."""
    path = PKG / "prices.json"
    if not path.exists():
        _fail("kadari/prices.json is missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        _fail(f"kadari/prices.json does not parse: {exc}")
    for key in ("as_of", "sources", "models", "ladders"):
        if key not in data:
            _fail(f"kadari/prices.json has no {key!r}")
    if not data["sources"] or not all(
            isinstance(s, dict) and s.get("url") and s.get("checked") for s in data["sources"]):
        _fail("every entry in prices.json 'sources' needs a url and a checked date (P3)")
    models = data["models"]
    for ladder, rungs in data["ladders"].items():
        missing = [r for r in rungs if r not in models]
        if missing:
            _fail(f"prices.json ladder {ladder!r} names unpriced rung(s) {missing}")
    pkg_data = _load_pyproject().get("tool", {}).get("setuptools", {}).get(
        "package-data", {}).get("kadari", [])
    for data_file in ("prices.json", "sample_capture.jsonl"):
        if data_file not in pkg_data:
            _fail(f"{data_file} is not in [tool.setuptools.package-data]; it would not ship")
    if not (PKG / "sample_capture.jsonl").exists():
        _fail("kadari/sample_capture.jsonl is missing; `kadari demo` would fail")
    scripts = _load_pyproject().get("project", {}).get("scripts", {})
    if scripts.get("kadari") != "kadari.cli:main":
        _fail("the `kadari` console script is not declared in [project.scripts]")
    _ok(f"price table valid: {len(models)} models, {len(data['ladders'])} ladders, "
        f"as_of {data['as_of']}")


def check_public_repo_hygiene() -> None:
    """`client/` is the tree that becomes a public repository, and its first commit is
    permanent (ADR-0006). The root `.gitignore` does not travel through that split, so this
    directory carries its own -- and this asserts it still covers the artifacts that are
    sitting here right now (`build/`, `dist/`, `*.egg-info/`, `__pycache__/`, `.DS_Store`).

    Checked mechanically rather than trusted, because the failure is silent and one-way:
    a `git add .` that sweeps in `build/lib/kadari/` publishes a stale second copy of every
    source file, and `.DS_Store` publishes local directory metadata. Neither can be undone
    by a later commit -- history is the artifact."""
    path = HERE / ".gitignore"
    if not path.exists():
        _fail(f".gitignore is missing from {HERE}; the public split would commit build "
              "artifacts and .DS_Store into a permanent first commit")
    patterns = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()}
    # `*.jsonl` and `*.zip` are the two that carry CUSTOMER data rather than build noise:
    # a capture log is production traffic, and a `kadari bundle` zip is that traffic
    # compressed -- binary, so the textual engine-name scan cannot even read it.
    required = ("__pycache__/", "*.py[cod]", "build/", "dist/", "*.egg-info/",
                ".DS_Store", ".env", "*.key", "*.jsonl", "*.zip")
    missing = [p for p in required if p not in patterns]
    if missing:
        _fail(f".gitignore no longer ignores {missing}; those would enter public "
              f"history at the split")
    _ok(f"public-repo hygiene: .gitignore covers {len(required)} required "
        f"pattern(s)")


def check_isolated_import() -> None:
    """Import the client with ONLY the client dir on the path: proves it is truly
    zero-dependency, and that no engine module is importable from the shipped tree.

    Run with cwd=client/ and PYTHONPATH stripped, so the only project code reachable is
    `kadari` itself (for `python -c`, sys.path[0] is the cwd). `import kadari` must
    succeed, and the shipped tree must offer NO importable `kadari_*` module.

    That leak check enumerates the tree rather than probing one hard-coded engine name.
    Two reasons, and the second is the one that matters: enumerating catches an engine
    package nobody thought to probe for, and it means this file -- which ships -- does not
    have to write an engine package name down in order to check that none is present."""
    code = (
        "import kadari, kadari.capture, kadari.wrap\n"
        "import kadari.prices, kadari.logs, kadari.analyze, kadari.report\n"
        "import kadari.charts, kadari.theme, kadari.cli, kadari.preflight\n"
        "import kadari.bundle, kadari.importers\n"
        "from kadari import LiveRecorder, wrap, CaptureError, __version__\n"
        "kadari.prices.load()  # the shipped table must load from the installed package\n"
        "assert kadari.cli.build_parser() is not None\n"
        "assert kadari.cli._SAMPLE.exists(), 'sample log missing from the package'\n"
        # Scoped to the shipped tree itself, not to sys.path at large: the property is
        # "no engine module is IN what we publish", and a developer who happens to have
        # the engine installed in their environment is not a packaging defect.
        "import os, pkgutil\n"
        "leaked = sorted(m.name for m in pkgutil.iter_modules([os.getcwd()])\n"
        "                if m.name.startswith('kadari_'))\n"
        "assert not leaked, 'engine module(s) importable from the client tree: %r' % leaked\n"
        "print('import-ok', __version__)"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, cwd=str(HERE), env=env)
    if proc.returncode != 0:
        _fail(f"isolated import failed:\n{proc.stdout}\n{proc.stderr}")
    _ok(f"client imports in isolation, zero-dep, no engine leak ({proc.stdout.strip()})")


def real_build_available() -> bool:
    return (importlib.util.find_spec("build") is not None
            and importlib.util.find_spec("setuptools") is not None)


def real_build() -> None:
    """A genuine, non-isolated (zero-network) wheel+sdist build, with IP checks on the
    produced wheel. Only runs when the build backend is installed."""
    with tempfile.TemporaryDirectory() as d:
        proc = subprocess.run(
            [sys.executable, "-m", "build", "--no-isolation",
             "--wheel", "--sdist", "--outdir", d, str(HERE)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            _fail(f"`python -m build` failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
        out = Path(d)
        wheels = list(out.glob("*.whl"))
        sdists = list(out.glob("*.tar.gz"))
        if not wheels or not sdists:
            _fail(f"build produced wheel={bool(wheels)} sdist={bool(sdists)} (need both)")
        names = zipfile.ZipFile(wheels[0]).namelist()
        if not any(n.endswith("kadari/py.typed") for n in names):
            _fail("built wheel does not contain kadari/py.typed")
        if not any(n.endswith("kadari/prices.json") for n in names):
            _fail("built wheel does not contain kadari/prices.json (every dollar "
                  "figure would report as unpriced)")
        if not any(n.endswith("kadari/sample_capture.jsonl") for n in names):
            _fail("built wheel does not contain kadari/sample_capture.jsonl "
                  "(`kadari demo` -- the first thing anyone runs -- would fail)")
        for n in names:
            if is_engine_module(n.split("/")[0]):
                _fail(f"built wheel leaks engine module {n!r}")
        _ok(f"real build clean: {wheels[0].name} + {sdists[0].name} (no engine in wheel)")


def main() -> int:
    print("== kadari client packaging check (zero-network, deterministic) ==")
    guard_no_engine_no_network()
    check_metadata()
    check_version_consistency()
    check_marker_and_license()
    check_price_table()
    check_public_repo_hygiene()
    check_isolated_import()
    if real_build_available():
        real_build()
    else:
        _ok("build backend (build+setuptools) not installed -> stdlib packaging "
            "smoke only (metadata + isolated import + marker checks above)")
    print("== kadari client packaging check: ALL GREEN ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
