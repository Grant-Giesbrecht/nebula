# Installing tools and building Nebula Navigator

Nebula Navigator (`navigator-tauri/`) is a [Tauri](https://tauri.app) app: a
Rust core plus the OS's native webview for the UI, talking to a Python
"sidecar" process that runs the real `nebula` logic. Building it needs three
things installed first — **Rust**, **Node.js/npm**, and a couple of
OS-level webview dependencies — none of which are part of the repo checkout.

This doc covers installing those tools and producing a build. For the
day-to-day development workflow (live-reloading Python, the frozen-bridge
gotcha, the bridge protocol, etc.) see
[`navigator-tauri/README.md`](../navigator-tauri/README.md).

## 1. Install Rust

Tauri's core is Rust, so the stable toolchain is required.

```
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

Restart your shell (or `source "$HOME/.cargo/env"`), then confirm it's on
`PATH`:

```
rustc --version
cargo --version
```

Nebula's `src-tauri/Cargo.toml` targets `rust-version = "1.77"` — rustup's
stable channel is well ahead of that, so no version pinning is needed.

## 2. Install Node.js and npm

Node is only used to run the Tauri CLI (`@tauri-apps/cli`) — the frontend
itself is plain HTML/JS with no bundler or build step. Node.js 18 or newer
is required.

- **macOS (Homebrew):** `brew install node`
- **Any OS:** download an installer from [nodejs.org](https://nodejs.org)
  (the LTS release), or use a version manager like
  [nvm](https://github.com/nvm-sh/nvm) / [fnm](https://github.com/Schniz/fnm)

Confirm:

```
node --version   # should print v18 or higher
npm --version
```

## 3. Install Tauri's system dependencies

Tauri wraps the OS's native webview, which has its own build-time
dependencies per platform.

**macOS**

```
xcode-select --install
```

This installs the Xcode command-line tools (a compiler toolchain plus the
system frameworks Tauri links against). No separate webview package is
needed — macOS ships WKWebView.

**Linux**

Package names vary by distribution; on Debian/Ubuntu:

```
sudo apt update
sudo apt install libwebkit2gtk-4.1-dev build-essential curl wget file \
  libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev
```

See the [Tauri Linux prerequisites
page](https://v2.tauri.app/start/prerequisites/#linux) for other
distributions — package names and the webkit2gtk version differ enough
between distros that it's worth checking there directly.

**Windows**

Install the [Microsoft C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
(the "Desktop development with C++" workload) and [WebView2](https://developer.microsoft.com/microsoft-edge/webview2/)
— WebView2 ships preinstalled on current Windows 10/11, so this is usually
already satisfied.

## 4. Install the Tauri CLI

The Tauri CLI is a project dependency, not a separate global install — it's
declared in `navigator-tauri/package.json` and fetched by `npm install`:

```
cd navigator-tauri
npm install
```

This pulls in `@tauri-apps/cli`, exposed through the `npm run dev` /
`npm run build` / `npm run tauri` scripts. There's no need to `npm install
-g @tauri-apps/cli` separately.

## 5. Make the `nebula` Python package importable

The Rust core doesn't reimplement Nebula's logic — it spawns a Python
process that imports `nebula.navigator.api` and speaks JSON over
stdin/stdout. For development, that Python needs the package installed:

```
cd ..            # repo root
pip install -e .
python -m nebula.navigator.api ping '{}'   # should print {"ok": true, ...}
```

(A **built/bundled** app instead ships a frozen Python interpreter — see
Building a distributable, below — so an end user of the finished app needs
none of this. It's only required for running the app from source.)

## Running it in development

With Rust, Node, and `nebula` all installed:

```
cd navigator-tauri
npm install          # if not already done
npm run dev           # compiles the Rust core and opens the window
```

This uses whatever Python bridge is on disk (or a live system Python if none
has been frozen yet — see [the sidecar-staleness
gotcha](../navigator-tauri/README.md#️-gotcha-once-you-have-built-a-sidecar-npm-run-dev-stops-using-your-live-python)
if you're editing Python and `npm run dev` seems to ignore your changes).

## Building a distributable

The shipped app is self-contained: it bundles its own Python, so the
machine that eventually runs it needs neither Python nor `nebula` installed.
That takes two steps, in order:

```
cd navigator-tauri
pip install pyinstaller     # one-time, freezes the Python bridge
./build-sidecar.sh          # produces src-tauri/binaries/nebula-bridge-<triple>
npm run build                # produces the .app / .dmg (or platform equivalent)
```

- `build-sidecar.sh` runs PyInstaller over `sidecar/bridge.py` and installs
  the frozen binary where Tauri's `externalBin` config
  (`src-tauri/tauri.conf.json`) expects it.
- Re-run `build-sidecar.sh` whenever the Python backend changes —
  `npm run build` alone will happily re-ship a stale frozen bridge.
- The frozen bridge is architecture-specific (the triple is `rustc`'s host
  triple), so a build made on Apple Silicon won't run on an Intel Mac, and
  vice versa.
- On macOS the resulting bundle is ad-hoc signed only — it runs fine on the
  machine that built it, but a `.dmg` handed to someone else will be
  quarantined by Gatekeeper until signed with a Developer ID and notarized.

## Quick reference

| Goal | Command |
|---|---|
| Install Rust | `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh` |
| Install Node/npm | `brew install node` (macOS) or an installer from nodejs.org |
| Install macOS build deps | `xcode-select --install` |
| Fetch the Tauri CLI | `cd navigator-tauri && npm install` |
| Make `nebula` importable | `pip install -e .` (from repo root) |
| Run in development | `cd navigator-tauri && npm run dev` |
| Build a distributable | `./build-sidecar.sh && npm run build` |

For the full development workflow — live Python reloading, window/tab
architecture, the bridge protocol, keyboard shortcuts — see
[`navigator-tauri/README.md`](../navigator-tauri/README.md).
