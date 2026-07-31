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

On first launch, click **“Open archive…”** (top-left) and pick an archive
directory, or a registered archive by path. The choice is remembered in
`localStorage` for next time.

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

Ops: `ping`, `resolve`, `list_sessions`, `list_items`, `sidecar_display`,
`importable_sessions`, `frozen_sessions`, `import_new`, `import_file`,
`open_path`, `file_manager_name`. Test any of them without the GUI:

```
python -m nebula.navigator.api list_sessions '{"archive": "/path/to/archive"}'
```
