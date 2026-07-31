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

### Which Python gets used

The core does not just run `python3` -- a bundled `.app` launched from
Finder gets a minimal `PATH` (`/usr/bin:/bin:/usr/sbin:/sbin`) where
`python3` is Apple's 3.9 stub, which has no `nebula`. Instead it probes a
list of candidates and picks **the first one that can actually
`import nebula.navigator.api`**:

1. `$NEBULA_PYTHON`, if set (used exclusively -- no fallback)
2. `python3` from `PATH` (so an activated venv wins in a terminal)
3. `/Library/Frameworks/Python.framework/Versions/*/bin/python3`
4. `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, `/usr/bin/python3`

To pin an interpreter:

```
NEBULA_PYTHON=/path/to/.venv/bin/python npm run dev
```

If no candidate has `nebula`, the window still opens and reports the list
it tried; Python's stderr is drained and appended to bridge errors, so a
traceback in the child shows up in the UI instead of a bare "Broken pipe".

## Build a distributable

```
npm run build        # produces a .app/.dmg (macOS), .msi (Windows), etc.
```

## Packaging note (the one real gap)

`npm run build` bundles the **webview + Rust binary**, but the shipped app
still calls whatever `python3` (or `$NEBULA_PYTHON`) resolves to at runtime.
For a self-contained app you have two options:

1. **Freeze the bridge** with PyInstaller into a single executable and ship
   it as a real Tauri sidecar binary:
   ```
   pip install pyinstaller
   pyinstaller --onefile --name nebula-bridge \
       -p ../src -c src/nebula/navigator/api.py
   ```
   Then register it under `bundle.externalBin` in `tauri.conf.json` and
   change `Bridge::spawn` to launch the sidecar instead of `python3 -m …`.
2. **Require a Python** on the target machine (fine for a lab of known
   workstations) — document `pip install nebula-archive` as a prerequisite.

Option 1 is the path to a "download and run" app; option 2 is zero extra
work if every user already has the `nebula` CLI.

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
