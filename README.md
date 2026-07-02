# nebula

A lightweight provenance-and-organization layer for measurement scripts,
analysis code, and the data they produce. Built for the case where you
have many small measurement sessions across possibly-related projects,
and want to be able to answer "what produced this file, and what did it
depend on" months later without a rigid, ever-breaking folder taxonomy.

## Core ideas

- **Sessions, not projects.** Each unit of work gets a folder,
  `S-XXXX`, filed under `<store_root>/<year>/<month>/`. The bare numeric
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
- **Sessions are single-day by creation, immutable once closed.** A
  script that starts before midnight and finishes after is fine — it's
  one continuous invocation, so it keeps writing into the same session.
  What's not allowed is casually reopening an old, closed session; link
  to it instead via `related_runs` / `derived_from`.
- **Multiple independent stores** (e.g. a postdoc data store and a
  separate personal/startup store) can cross-reference each other via a
  small registry (`~/.nebula/stores.yaml`) and a `store|session/file`
  ref syntax.
- **The SQLite index is disposable.** It's rebuilt from scratch by
  walking `session.yaml` + `*.meta.json` files. The filesystem is the
  source of truth; delete `index.db` and rebuild any time.

## Quick example

```python
import nebula

STORE = "/nas/nist-data"

with nebula.session(STORE, tags=["RP23D"], description="S21 characterization, sample #7") as s:
    scope_data = acquire()
    save_csv(scope_data, s.artifact_path("scope_trace_raw.csv"))
    s.write_meta_for("scope_trace_raw.csv", inputs={"bias_current_mA": {"start": 0, "stop": 10, "step": 0.5}})

# ... later, same day, a separate conversion step appends to the same session:
with nebula.session(STORE, run_id=s.id) as s:
    graf_data = convert(s.artifact_path("scope_trace_raw.csv"))
    save_graf(graf_data, s.artifact_path("raw.graf"))
    s.write_meta_for("raw.graf", derived_from=["scope_trace_raw.csv"])
```

Rebuilding the index and checking for crashed/abandoned sessions:

```python
from nebula import index

index.rebuild(STORE)
conn = index.open_index(STORE)
stale = index.flag_stale_open_sessions(conn)
```

Cross-store reference, once both stores are registered:

```python
s.add_related_run("postdoc|S-0300/diode.graf")
```

## Layout

```
src/nebula/
    refs.py       # Ref dataclass + parse_ref/format_ref (single canonical parser)
    registry.py   # multi-store registry (~/.nebula/stores.yaml)
    sidecar.py    # atomic JSON sidecar I/O + session.yaml I/O
    session.py    # Session, new()/append_to()/reopen()/session() context manager
    index.py      # SQLite index rebuild (fully regeneratable)
    picker.py     # optional PyQt5 session picker (not imported by default)
```

## Status

Early scaffolding — core session/sidecar/ref/index logic is implemented
and unit-testable without any hardware or GUI dependency. The picker is
functional but not yet battle-tested against a real event loop.
