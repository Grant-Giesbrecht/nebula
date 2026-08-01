# Nebula Navigator — Tauri edition

A native desktop build of the Navigator, using [Tauri](https://tauri.app):
a Rust core + a system webview for the UI, with **all of Nebula's logic
reused as-is from Python**. Nothing in the archive/session/sidecar layer is
reimplemented — the Rust side just brokers JSON to a Python "sidecar"
process.

```
┌────────────────────┐   line-delimited JSON    ┌──────────────────────────┐
│  Webview (HTML/JS) │  ── invoke('bridge') ──▶ │  Rust core (main.rs)     │
│  src/index.html    │                          │  owns 1 child process    │
│  src/main.js       │  ◀── JSON response ────  │        │ stdin/stdout     │
└────────────────────┘                          └────────┼─────────────────┘
                                                          ▼
                                       python -m nebula.navigator.api
                                       (reuses model.py / manual.py / osutil.py)
```

## Why this architecture

Nebula's real logic is ~4,000 lines of tested Python. A Tauri "backend" is
Rust, so a port has to decide what to do with that Python. This build keeps
100% of it: `src/nebula/navigator/api.py` is a thin JSON adapter over the
existing `model`/`manual`/`osutil` modules, so every backend behaviour
(status classification, checksums, imports, "open in Finder") stays in one
place shared with the CLI. The trade-off is that the app bundles a Python
runtime — see **Packaging** below.

## Layout

| Path | What it is |
|------|-----------|
| `src/index.html`, `main.js`, `styles.css` | The webview UI. No bundler — uses Tauri's global API (`window.__TAURI__`). |
| `src-tauri/src/main.rs` | Rust core. Spawns the Python bridge and exposes one `bridge` command. |
| `src-tauri/tauri.conf.json` | Window + bundle config. `withGlobalTauri` is on so the frontend needs no build step. |
| `src-tauri/capabilities/default.json` | Permissions (core + file-open dialog). |
| `../src/nebula/navigator/api.py` | The Python sidecar (lives in the main package, reused by tests). |

## Prerequisites (one-time)

None of these are in the base repo checkout — install them first.

1. **Rust** (stable): https://rustup.rs
   ```
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
   ```
2. **Node.js 18+** (only to run the Tauri CLI): https://nodejs.org
3. **Tauri system deps** (macOS): Xcode command-line tools —
   `xcode-select --install`. (Linux/Windows: see the Tauri prerequisites page.)
4. **The `nebula` package importable by the Python the app will launch:**
   ```
   cd ..            # repo root
   pip install -e .
   python -m nebula.navigator.api ping '{}'   # should print {"ok": true, ...}
   ```

## Run it (development)

```
cd navigator-tauri
npm install          # fetches the @tauri-apps/cli
npm run dev          # compiles the Rust core and opens the window
```

On first launch, use the archive switcher at the top of the left rail: the
**＋** button opens an archive directory, and any archive registered in
`~/.nebula/archives.yaml` is already listed under "Registered". Several
archives can be open at once — the switcher picks which one is active, and
**✕** closes the active one. Open archives, the active one, the pane widths
and which panels are showing are all remembered in `localStorage`.

Other things worth knowing about the window:

- **Drag and drop** files onto the window to import them — same dialog as
  the **Import files…** button, with the files already chosen. They go into
  the archive currently selected in the switcher.
- **Search sessions** (top of the rail) filters the list you already have
  loaded, so it is instant. Its **⚙** sets which fields are searched
  (titles / IDs / tags) and which sessions are listed at all (open /
  closed / crashed, clean / dirty — "dirty" meaning the session has
  problem items). The gear highlights whenever those options are
  narrowing things, so a filter can't silently hide sessions.
- **Search artefacts** (above the file list) searches the *whole archive*,
  not just the open session, via the `search_items` op. Terms are ANDed:
  `diode csv` finds csv files in diode-tagged sessions. **⚙ Advanced**
  picks the searched fields (filenames, session tags, origin/source,
  session id & title) and a created-between date range; a date range alone
  is a valid search. Results grow a Session column — click a run id to
  jump to that session and leave search mode.
- **Session info** (toolbar) toggles a panel showing `session.yaml` —
  status, tags, hold, related runs and the manual-operation history —
  rendered, not raw. The **{ }** button in its header shows the YAML source.
- Opening a sidecar shows the same kind of rendered view of the
  `.meta.json` (provenance, checksum, derived-from, inputs), with **{ }**
  for the raw JSON. **Entry point** is a link: the filename opens your
  local checkout, the **↗** opens the same file on the host pinned to the
  recorded commit. Under **Captured source**, *Restore files…* writes the
  whole snapshot back out as a real directory tree.
- The rail, the right-hand dock, and the split between the two docked
  panels are all **draggable**.

### Keyboard shortcuts

Cmd on macOS, Ctrl on Windows/Linux (`hasMod()` in `main.js` picks the
modifier; tooltips are rewritten to ⌘ / ⇧⌘ at boot on macOS):

| Shortcut | Action |
| --- | --- |
| `Mod-Shift-M` | Show the selected file's metadata; closes the panel if open |
| `Mod-Shift-S` | Toggle the session info panel |
| `Mod-R`       | Reload archive + session info (re-runs an active search) |
| `Mod-Shift-I` | Import files… |
| `Mod-O`       | Open the selection in the default app |

⚠️ On macOS these live *around* the menu bar, not in it: AppKit dispatches
menu accelerators before the webview gets a `keydown`, so any key the menu
claims is unreachable from `main.js`. That is why metadata is
`Cmd-Shift-M` — plain `Cmd-M` belongs to Window ▸ Minimize. `main.rs`
installs a deliberately small menu (app / Edit / Window); **keep Edit**,
since WKWebView's copy, paste and select-all are driven by those items.
If you add a menu item, check its accelerator against the table above.

### ⚠️ Gotcha: once you have built a sidecar, `npm run dev` stops using your live Python

`tauri dev` copies `src-tauri/binaries/nebula-bridge-<triple>` into
`target/debug/` alongside the dev executable, exactly as it does for a
release bundle. The core finds it there and uses it. So after the first
`./build-sidecar.sh`, **`npm run dev` runs the frozen bridge, and your edits
to `src/nebula/**.py` have no effect on the running app** no matter how many
times you restart it. The symptom is backend changes that appear to do
nothing.

Override it while working on Python — this takes priority over the sidecar:

```
NEBULA_PYTHON=python3 npm run dev
```

That reads `src/nebula/` live (assuming an editable `pip install -e .`), so
Python edits apply on app restart with no re-freeze. Reasonable defaults:

| What you're changing | Command |
|---|---|
| Python backend | `NEBULA_PYTHON=python3 npm run dev` |
| Rust core or frontend | `npm run dev` (uses whatever sidecar is on disk) |
| Testing the frozen bridge as shipped | `./build-sidecar.sh && npm run dev` |
| Building the distributable | `./build-sidecar.sh && npm run build` |

To opt out entirely for a while, delete `src-tauri/binaries/` — the core
falls back to a system Python, and `npm run build` will tell you the
sidecar is missing.

### Which bridge gets used

`Bridge::spawn()` tries, in order:

1. **`$NEBULA_PYTHON`**, if set — used exclusively, no fallback.
2. **The bundled sidecar**, `nebula-bridge` next to the running executable
   (`Contents/MacOS/` in an `.app`, `target/<profile>/` under `tauri dev`).
3. **A system Python** that can actually `import nebula.navigator.api`,
   probed in this order:
   1. `python3` from `PATH` (so an activated venv wins in a terminal)
   2. `/Library/Frameworks/Python.framework/Versions/*/bin/python3`
   3. `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, `/usr/bin/python3`

Step 3 probes rather than trusting `PATH` because a bundled `.app` launched
from Finder gets a minimal `PATH` (`/usr/bin:/bin:/usr/sbin:/sbin`) where
`python3` is Apple's 3.9 stub, which has no `nebula`.

Whatever is chosen must answer a `ping` handshake before startup completes,
so a broken bridge fails immediately with a real message instead of
surfacing later as "Broken pipe". If nothing works the window still opens
and reports every candidate it tried; Python's stderr is drained and
appended to bridge errors, so a traceback in the child reaches the UI.

## Build a distributable

The shipped app is **fully self-contained** — it carries its own Python, so
the target machine needs neither Python nor `nebula` installed. That takes
two steps, in order:

```
pip install pyinstaller     # one-time
./build-sidecar.sh          # freeze the Python bridge (~9 MB, ~30 s)
npm run build               # produces .app / .dmg
```

`build-sidecar.sh` runs PyInstaller over `sidecar/bridge.py` and installs
the result as `src-tauri/binaries/nebula-bridge-<target-triple>`. The triple
suffix is Tauri's `externalBin` convention (it lets one bundle carry
per-platform binaries); Tauri strips it when copying into the app, landing
it at `Nebula Navigator.app/Contents/MacOS/nebula-bridge` — right beside the
main executable, which is where `sidecar_path()` in `main.rs` looks.

Re-run `build-sidecar.sh` whenever the Python changes; `npm run build` alone
will happily re-ship a stale frozen bridge. The same staleness trap applies
to `npm run dev` — see [the gotcha above](#️-gotcha-once-you-have-built-a-sidecar-npm-run-dev-stops-using-your-live-python).

### Notes on the frozen build

- **Flet is excluded deliberately.** `nebula.navigator.__init__.launch()`
  imports the Flet view lazily, but PyInstaller follows function-body
  imports and would otherwise try to embed all of `Flet.app` (which fails
  to package outright). The bridge never calls `launch()`.
- **The frozen bridge is architecture-specific.** The triple in the filename
  is `rustc`'s host triple; an arm64 build will not run on an Intel Mac. For
  a universal app, build the sidecar for both triples on the matching
  hardware and ship both files.
- **Gatekeeper.** The bundle is ad-hoc signed only. On *your* machine it
  runs fine; a `.dmg` handed to someone else will be quarantined until it is
  signed with a Developer ID and notarized. That is a code-signing step,
  separate from bundling.

## The bridge protocol (for reference)

One JSON object per line, request and response:

```
-> {"id": 7, "op": "list_items", "args": {"session_path": "…", "verify": false}}
<- {"id": 7, "ok": true, "data": [ … ]}
<- {"id": 7, "ok": false, "error": "…", "error_type": "FileNotFoundError"}
```

Ops: `ping`, `resolve`, `list_archives`, `list_sessions`, `list_items`,
`sidecar_display`, `sidecar_info`, `session_info`, `search_items`,
`lineage`, `resolve_refs`, `code_info`, `restore_code`, `entry_point_link`,
`open_url`,
`importable_sessions`,
`frozen_sessions`, `import_new`, `import_file`, `open_path`,
`file_manager_name`. Test any of them without the GUI:

```
python -m nebula.navigator.api list_sessions '{"archive": "/path/to/archive"}'
```
