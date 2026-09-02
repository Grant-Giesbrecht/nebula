"""
Smoke-run every script in examples/.

The hole this fills: `examples/` is documentation users copy from, and
nothing in the suite ever executed it. `ex4.py` shipped with two runtime
errors -- `tags="bulk_data"` (refused by the list-of-strings guard, and
only *after* the measurement loop had run) and a bare `tags` name that was
never defined, passed positionally into a keyword-only parameter. Both are
the kind of mistake the library's own tests cannot see, because they are
mistakes in the *calling* code: `test_artifact_tags.py` already pins that
`tags="drift, twpa"` raises, which is precisely why the example that did
it was broken.

Running them is the only check worth having here. A parse-only check would
have caught neither, and an import-only check would have caught neither.
The scripts are written to run unattended -- input_tag() returns its
`initial` on closed stdin, and session() creates rather than prompts -- so
stdin is /dev/null and a script that stops to ask a question fails the
test by hanging, which is also worth knowing.

Each example is run against a throwaway archive in a subprocess, so the
NEBULA_* isolation the fixtures set up is inherited rather than reasoned
about, and a script that calls sys.exit() or scribbles on process-global
state cannot affect the rest of the suite.

One example (`measure_vccs_warmup.py`) genuinely requires a human: it asks
a free-text question and then loops until someone types EXIT. It is
detected as such and skipped from the run, but still compiled -- and it is
detected by *what it calls*, not by name, so a new interactive example is
handled the day it lands instead of silently failing the suite.
"""

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nebula.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
EXAMPLE_SCRIPTS = sorted(EXAMPLES_DIR.glob("*.py"))


def _toplevel_imports(path: Path):
    """Root module names imported by `path`, at any depth.

    ast rather than a regex because `from stardust.algorithm import ...`
    and a conditional import inside a function are both real, and both
    have to be found for the skip below to be honest.
    """
    roots = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _missing_dependency(path: Path):
    """The first import an example needs that this machine hasn't got.

    The examples are written for a working lab install (stardust,
    pylogfile, constellation, ...), which a CI box or a contributor's
    checkout may not have. Skipping the example that needs one beats
    either failing it -- a red suite that says nothing about nebula -- or
    hardcoding a list of scripts to ignore, which silently stops covering
    a new example the day it is added.
    """
    for name in sorted(_toplevel_imports(path)):
        try:
            if importlib.util.find_spec(name) is None:
                return name
        except (ImportError, ValueError):
            return name
    return None


#: Calls that block on a person. `input_tag()` and `session()` are absent
#: on purpose: they are the prompts nebula owns, and both are specified to
#: fall back to their non-interactive answer on closed stdin -- which is
#: exactly the behaviour this test should be exercising, not skipping.
_BLOCKING_PROMPTS = {"input", "inputimeout"}


def _needs_a_human(path: Path):
    """The first blocking prompt an example makes, if any.

    Detected from the call, not from a list of script names: a list stops
    being true the moment someone adds an example, and the failure mode is
    a suite that hangs for the full subprocess timeout with no hint why.
    """
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _BLOCKING_PROMPTS:
                return node.func.id
    return None


@pytest.mark.parametrize("script", EXAMPLE_SCRIPTS, ids=lambda p: p.name)
def test_example_script_compiles(script):
    """The one check that covers every example, interactive or not."""
    compile(script.read_text(), str(script), "exec")


@pytest.mark.parametrize("script", EXAMPLE_SCRIPTS, ids=lambda p: p.name)
def test_example_script_runs_clean(script, tmp_path):
    blocking = _needs_a_human(script)
    if blocking:
        pytest.skip(f"{script.name} calls {blocking}() -- interactive by design")

    missing = _missing_dependency(script)
    if missing:
        pytest.skip(f"{script.name} needs {missing!r}, which isn't installed")

    # `intake --auto` registers the fixed 'auto-intake' nickname the
    # examples hardcode; the same root is passed as argv[1] for the ones
    # that take an archive argument instead (ex3).
    archive_root = tmp_path / "archive"
    main(["intake", str(archive_root), "--auto"])
    intake_root = next(archive_root.iterdir())

    env = dict(os.environ)
    # Test the working copy, not whatever nebula happens to be installed.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))

    proc = subprocess.run(
        [sys.executable, str(script), str(intake_root)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        cwd=tmp_path,          # anything written relative to cwd is throwaway
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"{script.name} exited {proc.returncode}\n"
        f"--- stdout (tail) ---\n{proc.stdout[-2000:]}\n"
        f"--- stderr (tail) ---\n{proc.stderr[-2000:]}")


def test_every_example_is_actually_covered():
    """The parametrization is a glob, so a new example is picked up on its
    own -- but an empty glob would make this whole file pass vacuously."""
    assert EXAMPLE_SCRIPTS, f"no example scripts found under {EXAMPLES_DIR}"
