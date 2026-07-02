# nebula

A lightweight provenance-and-organization layer for measurement scripts,
analysis code, and the data they produce. Built for the case where you
have many small measurement sessions across possibly-related projects,
and want to be able to answer "what produced this file, and what did it
depend on" months later without a rigid, ever-breaking folder taxonomy.

## Core ideas

- **Sessions, not projects.** Each unit of work gets a folder,
  `S-XXXX`, filed under `<archive_root>/<year>/<month>/`. The bare numeric
  ID carries no date info (the folder location does), so cross-refs stay
  short: `S-0152/diode.graf`, not `2026-04-11_S-0152/diode.graf`.
- **Per-artifact sidecars, not one folder-level metadata file.** Every
  data file gets a `<filename>.meta.json` recording which script/commit
  produced it and what it was derived from. This means one folder can
  legitimately hold output from several script versions or several
  scripts entirely (e.g. a raw oscilloscope dump and its later `.graf`
  conversion), without the metadata becoming ambiguous about which claim
  applies to which file.
- **`session.yaml` is the one human-edited file** — tags, description,
  open/closed status. Everything else is machine-written and atomic.
- **A session's fixed guarantee is the date it was started, not
  single-writer exclusivity.** Several scripts can pile related
  measurements into one day's session for readability: appending is
  allowed while a session is still open, *and* to any session created
  today even if a previous script already closed it (that just reopens it
  and flips the status back to open). The one thing kept frozen is a
  session **closed on a previous day** — reopening one takes a deliberate
  `nebula.reopen()` (or the picker's `/reopen <id> --force`), so you can't
  silently rewrite last week's record by reflex.
- **Multiple independent archives** (e.g. a postdoc data archive and a
  separate personal/startup archive) can cross-reference each other via a
  small registry (`~/.nebula/archives.yaml`) and an `archive|session/file`
  ref syntax.
- **The SQLite index is disposable.** It's rebuilt from scratch by
  walking `session.yaml` + `*.meta.json` files. The filesystem is the
  source of truth; delete `index.db` and rebuild any time.

## Quick example

```python
import nebula

with nebula.session("postdoc", tags=["RP23D"], description="S21 characterization, sample #7") as s:
    scope_data = acquire()
    save_csv(scope_data, s.artifact_path("scope_trace_raw.csv"))
    s.write_meta_for("scope_trace_raw.csv", inputs={"bias_current_mA": {"start": 0, "stop": 10, "step": 0.5}})
    run_id = s.id
# session closes here.
```

A later, separate script step in the *same session* (e.g. a conversion
pass, or a second related measurement) uses `append_to` / passes
`run_id=` instead of leaving the `with` block. This works whether the
session is still open or was already closed earlier the same day:

```python
with nebula.session("postdoc", run_id="S-0300") as s:  # today's session, open or closed
    graf_data = convert(s.artifact_path("scope_trace_raw.csv"))
    save_graf(graf_data, s.artifact_path("raw.graf"))
    s.write_meta_for("raw.graf", derived_from=["scope_trace_raw.csv"])
```

If the target session was closed on a *previous* day, `append_to` refuses
it. Either reopen it deliberately with `nebula.reopen("postdoc", "S-0300")`
if you really mean to extend it, or start a new session and link back with
`related_runs` / `derived_from` instead
(`s.write_meta_for("raw.graf", derived_from=["postdoc|S-0300/scope_trace_raw.csv"])`).

Calling `nebula.session(...)` with **no** `run_id` pops an interactive CLI
picker listing the sessions you can append to (today's, plus anything
still open), so you can add to a run in progress or type `/new` to start
fresh. Pass `new_session=True` to skip the prompt and always start clean.

### Holding a session open across midnight

A run of related measurements sometimes spans midnight, but each script
invocation closes the session on exit — and a session closed on a
*previous* day is frozen. A **hold** keeps a session appendable regardless
of date until it expires or you release it, independent of its open/closed
status:

```
nebula hold <archive> S-0300 2h     # hold for 2 hours, then exit
nebula hold <archive> S-0300        # hold until you stop the command (Ctrl-C)
nebula release <archive> S-0300     # clear the hold  (alias: nebula close)
```

The hold is recorded in `session.yaml`, so it survives across separate
script runs (and reboots). `nebula show` / `nebula ls` flag a held session,
and it shows up in the interactive picker even after its start day. The
same thing is available programmatically as `nebula.hold(archive, run_id,
seconds=...)` and `nebula.release(archive, run_id)`.

Rebuilding the index and checking for crashed/abandoned sessions:

```python
from nebula import index

index.rebuild("postdoc")  # or a Path, for an unregistered archive
conn = index.open_index("postdoc")
stale = index.flag_stale_open_sessions(conn)
```

Cross-archive reference, once both archives are registered:

```python
s.add_related_run("postdoc|S-0300/diode.graf")
```

## Multi-machine / multi-archive setup

Every session call (`nebula.session`, `nebula.new`, `nebula.append_to`,
`nebula.reopen`) takes an `archive` argument, and its **type** decides how
it's resolved:

- Pass a **`str`** and it's treated as a name registered in
  `~/.nebula/archives.yaml` (or `$NEBULA_REGISTRY`) — looked up via
  `nebula register <name> <root>`. Unknown names raise `KeyError`
  immediately rather than silently creating a folder somewhere
  unexpected.
- Pass a **`Path`** and it's used as a literal filesystem root, no
  registry involved — useful for scratch/ad hoc archives you don't want
  to register.

```python
with nebula.session("postdoc", tags=["RP23D"], description="...") as s:
    ...
```

This is what makes multi-machine setups painless: if `postdoc` lives at
a different mount point on your desktop vs. your laptop, you register it
once per machine and every script that says `nebula.session("postdoc", ...)`
just works, unmodified, on either. The CLI is more lenient than the
Python API — a bare string is tried against the registry first and
falls back to being treated as a literal path, so `nebula ls postdoc`
and `nebula ls /some/scratch/dir` both work from the terminal.

## Layout

```
src/nebula/
    refs.py       # Ref dataclass + parse_ref/format_ref (single canonical parser)
    registry.py   # multi-archive registry (~/.nebula/archives.yaml)
    sidecar.py    # atomic JSON sidecar I/O + session.yaml I/O
    session.py    # Session, new()/append_to()/reopen()/session() context manager
    index.py      # SQLite index rebuild (fully regeneratable)
    graph.py      # upstream()/downstream() provenance traversal, cross-archive aware
    cli.py        # `nebula` command-line tool
    picker.py     # optional PyQt5 session picker (not imported by default)
```

## CLI

```
nebula rebuild <archive>                           # rebuild the index from sidecars
nebula ls <archive> [--tag T] [--status S] [--today]
nebula show <archive> <run_id>                     # full detail incl. derived_from graph
nebula upstream <archive> <run_id> <file>          # trace an artifact back to its inputs
nebula downstream <archive> <run_id> <file> [--also-search ARCHIVE ...]
nebula stale <archive> [--hours N]                 # find abandoned "open" sessions
nebula archives                                    # list registered archives
nebula register <name> <root> [--git-org ORG]      # add an archive to the registry
```

`<archive>` is either a registered name (see `nebula archives`) or a
literal path.

`downstream` only searches archives you tell it to (via `--also-search`),
since a derived artifact could in principle live in any registered archive
and scanning all of them by default would be expensive and surprising.

## Status

Core session/sidecar/ref/index/graph logic is implemented and covered by
43 unit tests (ref parsing, session lifecycle, git provenance capture
against a real repo, index rebuild, single- and cross-archive graph
traversal, registry-name resolution, registry persistence). The CLI has
been smoke-tested end to end against a real archive, including the new
str-name-vs-Path resolution. The picker is functional but not yet
battle-tested against a real Qt event loop.

