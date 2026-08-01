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

### Manual operations: importing, editing, and checking data

Not all data comes from a tracked script — a coworker emails you a dataset,
or you export a file from an instrument by hand. Bringing files in through
nebula (rather than a bare `cp`) writes a proper sidecar with **honest
`external` provenance** (a free-text origin, the importing user, and a
sha256), so a hand-added file is a first-class citizen, not an orphan:

```
nebula import <archive> S-0300 report.csv --from "emailed by Jane 2026-07-01"
nebula import-new <archive> a.csv b.csv --tags shared --description "coworker dataset"
nebula reconcile <archive> [S-0300]    # adopt files you already copied in by hand
```

Editing existing data is soft and audited. Deletes move the file (and its
sidecar) to a `.trash/` inside the session — nothing is hard-removed — and
every change is logged to the session's `history`. Deletes refuse to break
a `derived_from` link unless you `--force`:

```
nebula rm <archive> S-0300 bad.csv --reason "failed calibration"
nebula replace <archive> S-0300 raw.csv rescanned.csv --reason "rescan"
nebula rm-session <archive> S-0300 --reason "duplicate"   # whole folder -> archive .trash/
```

Because the filesystem is the source of truth and can be edited outside
nebula, `nebula check` (fsck) verifies an archive is internally consistent
and **suggests the command to fix each problem it finds**. It reports
orphans, sidecars whose artifact is gone (and vice-versa), **sha256 drift**,
unreadable sidecars / `session.yaml`, dangling or self-referential
`derived_from`, dangling `related_runs`, `run_id`/folder mismatches, invalid
status, garbled holds, duplicate ids, and (as info) cross-archive refs it
can't reach. It exits non-zero if any *error*-level problem is found:

```
$ nebula check <archive>
[error] checksum_mismatch S-0001/raw.csv: sha256 314cbe44... != recorded 7a8988e9...
    fix: if the edit was intentional: 'nebula reseal <archive> S-0001 raw.csv'; otherwise restore the original bytes
[error] orphan S-0001/notes.dat: file has no sidecar
    fix: nebula reconcile <archive> S-0001
```

`nebula reseal <archive> <run_id> <file>` re-records an artifact's checksum
from its current bytes — the blessed fix when a checksum mismatch is an
edit you meant to make.

All of these are also available programmatically: `nebula.import_file`,
`import_new`, `adopt_file`, `delete_file`, `replace_file`, `reseal`,
`delete_session`, `find_orphan_files`, and `nebula.check_archive`.

### Nebula Navigator (GUI)

A Finder-like browser over an archive. Instead of raw files it shows one
*box* per logical artefact, with **DATA** and **META** slots so a missing
half of the pair (an orphan file or a stray sidecar) is obvious at a glance.
The left column lists sessions and flags any with problems.

The Navigator is a **native desktop app** built with [Tauri](https://tauri.app),
living in [`navigator-tauri/`](navigator-tauri/README.md). It is not a Python
entry point: it ships as a self-contained `.app`/`.dmg` with a frozen Python
bridge inside, so the machine running it needs neither Python nor `nebula`
installed.

```
cd navigator-tauri
./build-sidecar.sh     # freeze the Python bridge (needs pyinstaller)
npm run build          # produces .app / .dmg
```

See [`navigator-tauri/README.md`](navigator-tauri/README.md) for the
development loop and prerequisites.

The GUI uses `nebula` purely as a library: `nebula.navigator.model` is the
toolkit-independent data layer, and `nebula.navigator.api` exposes it over
line-delimited JSON on stdin/stdout. Neither imports a GUI toolkit, so both
stay unit-testable without a display, and the archive logic is shared with
the CLI rather than duplicated. Edit actions (reconcile, reseal, replace,
delete) hang off the same manual-ops API.

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

An archive is browsable by hand -- four entries at the root, no hidden
directories, and no month nesting:

```
<archive>/
    archive.yaml            # per-archive settings (optional)
    index.db                # rebuildable cache
    code/                   # captured source (blobs + manifests)
    data/
        2026/
            S-26-0001/      # session id carries its own two-digit year
            S-26-0002/
        2025/
            S-25-0184/
```

Session numbering restarts each year, which is what lets the CLI take a
bare number: `nebula show <archive> 12` resolves to `S-26-0012` against the
current year. `26-0012` works too; a full `S-26-0012` is always accepted.

```
src/nebula/
    refs.py       # Ref dataclass + parse_ref/format_ref (single canonical parser)
    registry.py   # multi-archive registry (~/.nebula/archives.yaml)
    sidecar.py    # atomic JSON sidecar I/O + session.yaml I/O
    session.py    # Session, new()/append_to()/reopen()/session() context manager
    index.py      # SQLite index rebuild (fully regeneratable)
    codestore.py  # content-addressed snapshot of the source that ran (code/)
    annotations.py # mutable user tags/comments (<session>/annotations.yaml)
    config.py     # per-archive settings (<archive>/archive.yaml)
    graph.py      # upstream()/downstream() provenance traversal, cross-archive aware
    cli.py        # `nebula` command-line tool
    picker.py     # optional PyQt5 session picker (not imported by default)
```

## Overwrite protection

A write that would land on an existing artifact never silently destroys it.
The archive's `on_overwrite` policy decides what happens instead:

| `on_overwrite` | What a colliding write does |
|---|---|
| `duplicate` *(default)* | writes `raw-001.csv` beside `raw.csv`, recording the name that was asked for |
| `overwrite` | replaces the existing file |
| `cancel` | raises `FileExistsError` and writes nothing |

```
nebula config <archive> --on-overwrite duplicate|overwrite|cancel
```

The rename is recorded on the new file's sidecar as `original_name` and
`duplicate_index`, so "this was automatic, and here is what I asked for"
survives in the metadata rather than only in the filename. An unrecognised
policy in `archive.yaml` falls back to `duplicate` rather than being
trusted.

This applies to `s.artifact()` and to manual imports alike -- a coworker's
second copy of `raw.csv` lands beside the first instead of being refused.
`artifact_path()` stays a pure path helper and is deliberately *not*
overwrite-aware.

In the Navigator, duplicates render under **the name that was asked for**
with a `2 of 3` badge showing write order; hovering reveals the real
filename and why it was renamed. The `.meta.json` rows still show the name
on disk, so a file can always be found.

## User tags and comments

Two kinds of metadata, deliberately kept apart:

| | Recorded at creation | Added later |
|---|---|---|
| Where | `session.yaml` tags, `*.meta.json` sidecars | `annotations.yaml` |
| What | provenance, checksums, inputs, session tags | your tags + one comment |
| Who writes it | nebula, automatically | you, whenever you like |
| Mutable? | **no** — a claim about the run | yes, on a whim |

Annotating never modifies a sidecar or `session.yaml`. Both sessions and
individual artifacts can carry user tags and a comment:

```yaml
# <session>/annotations.yaml
version: 1
session:
  tags: [thesis-ch3]
  comment: |
    Warm-up drifted for the first 20 min.
artifacts:
  vccs_warm_up.tome:
    tags: [shows-drift, paper:2026]
    comment: the run that showed the phenomenon
```

The point is finding things later: tags are short and searchable, the
comment is long-form and less convenient to search but says whatever you
need it to.

**Tag rules.** Outer whitespace is stripped and internal runs collapse to a
single space. Case is preserved (`RP23D` stays `RP23D`). Colons, hyphens
and underscores are all valid, so namespaced tags like `paper:2026` and
`rp23-warmup` work. Commas, newlines and tabs are rejected — a comma is
the separator wherever tags are typed. Duplicates are dropped.

```
nebula annotate <archive> <run_id> [file] --add-tags "a,b" --comment "..."
nebula annotate <archive> <run_id> [file] --set-tags "" --rm-tags "a"
nebula annotate <archive> <run_id>                       # show, change nothing
```

Omit `file` to annotate the session itself. In the Navigator, both panels
have a **Your notes** editor, and the search options gain *Your tags* and
*Your comments* checkboxes. `nebula check` reports (as info, not an error)
annotations naming a file that is no longer in the session — a comment
outliving its file is worth knowing about, but deleting it would break
restoring that file from `.trash/`.

Concurrency is intentionally simple: atomic write-then-replace, and
last-write-wins between machines. Two people annotating the same session
through a syncing folder at the same moment will lose one side's edit.

## Captured source code

Git records *which commit* a script ran at; it cannot record what ran when
the working tree was dirty — and in practice most real runs are dirty. So
every save also snapshots the first-party source behind it into a
content-addressed store, **in addition to** the usual
`repo`/`commit`/`dirty`/`entry_point` fields:

```
<archive>/.code/blobs/<aa>/<bb>/<sha256>            one copy per file version
<archive>/.code/manifests/<aa>/<bb>/<sha256>.json   {entry, files: {path: blob}}
```

The sidecar carries one short id (`produced_by.code`), plus per-repo git
state for every repo that contributed code (`produced_by.repos`). Because
a manifest is hashed over its own content, a run whose code is unchanged
reuses the same id and writes **nothing** — storage tracks how often your
code changes, not how often you run it.

What gets captured: the entry script, plus every already-imported module
that lives in a git repo and isn't stdlib/site-packages. Nebula's own
package is deliberately excluded — it is the instrument, not the
experiment. Files over `code_max_file_bytes` are skipped.

The file is optional — a missing `<archive>/archive.yaml` means defaults.
Inspect or change it with `nebula config`:

```
nebula config <archive>                          # show effective settings
nebula config <archive> --capture-code false     # write archive.yaml
nebula config <archive> --max-file-bytes 500000
```

`NEBULA_CAPTURE_CODE=0` overrides the file for a single run; `nebula
config` reports when that is in effect and never writes the override into
the archive.

To get the code back, the Navigator's **Restore files…** button (under
*Captured source*) writes a snapshot out as a directory tree at the
original repo-relative paths, plus a `SNAPSHOT.txt` naming the entry point
and the repos involved. Restores never overwrite: a second restore of the
same snapshot lands beside the first. `nebula check` reports
dangling code refs and missing blobs; `nebula gc` sweeps snapshots no
sidecar references — dry-run by default, and it treats trashed sessions as
live so a restore stays honest.

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
nebula config <archive> [--capture-code B] [--max-file-bytes N]   # archive settings
nebula check <archive> [--no-checksums]            # fsck, incl. dangling code refs
nebula gc <archive> [--delete] [--ignore-trash]    # sweep unreferenced captured code
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
str-name-vs-Path resolution. The interactive terminal session picker
(`nebula.session_select`) is covered by its own tests.

