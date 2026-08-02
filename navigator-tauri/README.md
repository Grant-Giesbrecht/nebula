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
- The session list is **grouped by day** (Today / Yesterday / weekday /
  Month Year), and the **calendar** link in its header shows a GitHub-style
  activity strip: one cell per day shaded by session count, sized to fit the
  current rail width. Click a day to filter the list, click again to clear.
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
- The rail has three tabs: **Sessions**, **Collections** and **Searches**.
  Collection names are permissive — spaces, parentheses, `#`, `&` are all
  fine, and the name *is* the filename and the ref segment. Banned:
  `/ \ | : * ? " < >` (path/ref separators and characters Windows rejects),
  trailing dots, and reserved device names. **Renaming** renames for real:
  the file moves and every `collections/<old>` ref is rewritten, so a folder
  is never orphaned and no old name lingers.
  Collections render as a **tree**: only collections nothing else contains
  are roots, and a nested one appears under its parent behind a twisty
  (expansion is remembered). Opening one shows its entries in the main area
  with breadcrumbs; a nested collection reads as a **folder** by name, not
  as its `collections/<name>` ref. Clicking a file selects it **in place** —
  properties in the detail bar, double-click to open, right-click to reveal
  — without switching to Sessions. Right-clicking empty space offers *New
  folder here…*. Missing targets are struck through, unreachable archives
  marked separately; **✕** removes an entry. A file's
  detail bar gains **Add to collection** (existing or new, with a note) and
  shows which collections it already belongs to — click one to open it.
  **Searches** lists saved searches: click to run, **+ save** stores the
  current search with its fields and dates.
- **Multi-select** in the file list: Cmd/Ctrl-click toggles, Shift-click
  extends a range, and *Add to collection* then takes the whole selection
  (each ref carries its own session, so a selection spanning search results
  works).
- **Right-click** menus: a file offers add-to-collection, reveal in the file
  manager, open, metadata, reseal and delete; a collection in the rail
  offers *New folder inside…* (a nested collection — "folder" is just what
  it is called in that context) and delete; a collection entry offers
  add-to-collection, reveal, and remove-from-this-collection. The webview's
  own context menu is suppressed everywhere except text inputs.
- **Archive** (toolbar) opens the management panel: session/artefact counts,
  index age with a *Rebuild* button, `nebula check` (checksum verification
  optional, since it re-hashes everything), the code store's size with a
  **dry-run-first** `gc`, and the effective `archive.yaml` settings. A stale
  index or a session left open from an earlier day is called out here.
- Per-file maintenance sits in the detail bar: **Reseal** (re-record a
  checksum after an intended edit), **Write sidecar** (adopt a file dropped
  in by hand), and **Delete** (soft-delete to the session's `.trash/`).
  The session panel has **Hold / Release** and **Move session to trash**.
  Every destructive action confirms first and says what it will and won't
  undo; `delete` offers a forced retry if another artefact still derives
  from the file.

### Tabs

Finder-style tabs over one window. A tab owns a **location** — which rail
tab is showing, which session or collection is open, the search text, the
selection — while the archive and the loaded session list stay shared,
because those describe the machine's state rather than a place you
navigated to. Switching tabs freezes the location you are leaving and thaws
the one you are entering (`browseState()` / `restoreBrowse()` in `main.js`).

The strip stays hidden while there is only one tab, since a lone tab is
just "the window". New tabs open beside their opener, not at the end.
Cmd/Ctrl-click a session, or use the right-click menu on a session or
collection, to open it in a new tab. Tabs are persisted as locations, not
as loaded data, so a restored tab pointing at a session that has since gone
simply lands on the archive with nothing open.

**Multiple windows.** `File ▸ New Window` (`Mod-N`) opens another window on
the same archive — one process, one Python bridge, since `bridge` is an
app-wide Tauri command. Each window keeps its own tabs, saved under its
window label, so two windows never overwrite each other's state.

Tabs can be dragged **between** windows, and dragging one out of the strip
and dropping it on empty space detaches it into a new window. A webview
cannot see a pointer that has left it, so at drop time the front-end asks
the Rust side which window is under the cursor (`window_at_cursor`, using
screen coordinates and preferring the focused window when they overlap) and
hands the tab over as a `nebula://accept` event (`send_to_window`). Files
dragged into another window arrive the same way and open the collection
picker there. Right-clicking a tab offers *Move to a new window* as a
keyboard-and-mouse-free fallback.

### Selecting files

The file list behaves like a file manager's:

- **Arrow keys** move the selection, **Shift** extends it, **Home/End** jump
  to the ends and **PageUp/PageDown** move by a screenful — computed from the
  actual row height, so it is right in grid view and at any window size.
- **Mod-A** selects every file in view. It used to select the window's text,
  which is never what you want here.
- **Click and drag on empty space** sweeps a marquee over the rows it
  crosses; Shift or Mod adds to the existing selection instead of replacing
  it. Dragging *from a row* moves files instead, so the two gestures never
  compete — the decision is made by where the gesture starts.
- Keys are ignored while a text box has focus, so typing in a search box
  still types.

### Filing files into collections

Four ways, in ascending order of ceremony:

1. **Drag** the selection onto a collection — in the rail tree, in the
   collection view, or in another window.
2. **Add to &lt;name&gt;** on the right-click menu: one click into the
   collection you used last.
3. **Add to ▸** lists the five most recent, most recent first.
4. **Add to collection…** opens the picker, which is a **tree** rather than a
   dropdown, since collections nest and a flat list of names cannot show that
   `figures` lives inside `paper-2026`. It opens with the last-used collection
   selected and its ancestors expanded.

### The Relations tab

`Show relations` (right-click a session or file, or `Mod-Shift-L`) opens a
tab showing the provenance tree, rooted either at one artefact or — with no
file — at every artefact a session produced.

Both directions are shown: **Built from** (upstream, walking `derived_from`)
and **Used by** (downstream, the reverse edge). Depth defaults to 3; a node
whose children were withheld offers *go deeper*. Missing files are struck
through with the reason, archives that can't be reached are marked
separately and are not clickable, and a node reachable by two paths expands
once with the repeat labelled *shown above* — an indented tree can't draw a
diamond, so it admits to one instead.

The footer says whether the answer came **from the index** or was **read
from sidecars**. Both are correct; only one is fast, and the view shouldn't
imply the wrong one. See `model.provenance_tree()`.

### The Index window

`Archive ▸ Inspect index…`, or right-click a session ▸ `Show in index`, opens
a read-only tab onto `index.db` itself: every table (`sessions`,
`artifacts`, `derived_from`, `related_runs`, `year_seals`, `meta`) with row
counts, real column names, a contains-filter and paging.

It is a dump rather than a report, on purpose — the point is to see what the
index actually holds, including the columns that make it work: `rel_path`
(relative, so the archive is movable), `sig` (the stat signature freshness
is judged by), and `year` (the bucket a seal can skip).

**It never sweeps on its own.** A status display that quietly repaired what
it was describing could never show you a problem, so `Sweep` and `Rebuild`
are buttons, and `Sweep` reports exactly what it did.

### Signatures and locks in the session pane

The session info pane ends with **Index & locks**, which puts the session's
live signature next to the one the index recorded and says whether they
agree — the same comparison `ensure_fresh` makes, visible rather than
implied. Below it are the two things that are *claims* rather than data:

- **Hold** — held indefinitely, held until a time, or not held.
- **Year seal** — whether this session's year is sealed, and, importantly,
  whether the index has verified that seal yet. Only then do sweeps skip
  the year, and the pane says so, because a skipped year is a year the
  index has stopped checking.

### Archive kinds and transfers

The titlebar carries a **kind badge** for anything that is not a plain
standard archive — `intake`, `fragment`, or `merged — locked` — because the
difference is invisible in the file list but changes what is allowed.

`Archive ▸ Merge intake… / Adopt fragment… / Export…` and the right-click
menus on sessions, collections and files all open the same **plan dialog**.
It asks the backend what *would* move and shows it before anything does:
session count, file count, bytes, the rename map (`I-26-0001 → S-26-0044`),
foreign data listed separately with its size, partial sessions with how much
was omitted, skipped sessions with why, and anything that will not resolve in
the result. Nothing runs until Continue. Toggling "include data from other
archives" re-plans, because it changes the size — which is the number the
dialog exists to show.

### Menus

`main.rs` installs app / **File** / Edit / View / Window. The split is by
what a thing *does*, not by what happened to be built first:

- **File** — New Archive…, New Window, New Tab, Duplicate Tab, Open
  Archive…, Import Files…, Import Intake Archive…, Import Fragment…, Adopt
  from Fragment…, Export Fragment…, Open Selected File, Close Tab. Anything
  that brings data in or sends it out lives here.
- **Edit** — the native items (**keep them**: WKWebView's copy, paste and
  select-all are driven by them) plus *Add to Collection…*, which edits a
  collection rather than the view.
- **View** — what is on screen: the three rail tabs, file metadata, session
  info, relations, archive management, the index inspector, reload.
- **Window** — tab navigation and the native window items.
- The app menu carries **Set Your Name…**, since identity is about you
  rather than about any archive.

### Panels in their own window

The sidecar and session panels have a **⇱ pop-out** button that opens them
as a separate OS window, and a **⇲ Dock** button in that window to put them
back. Buttons rather than a drag on purpose: while the OS is moving a
window there is no "drag ended" event to hang a drop on, so a drag would
mean debouncing `Moved` events and guessing where the pointer was — for a
gesture used a handful of times a day.

A torn-off panel has a **follow** checkbox. With it on (the default) the
panel tracks whatever is selected in the main window — which is what it was
doing in the dock a moment earlier, so continuing is the least surprising
behaviour. Turning it off pins the panel to what it is showing, which is
what you want when comparing two sidecars side by side. The choice is
remembered per panel kind.

Following is cheap and quiet: the main window announces its selection on a
120ms debounce (arrowing down a list would otherwise fire on every row),
and a panel ignores an announcement that names what it is already showing.
Ticking the box asks the main window what is selected right now, so it
takes effect immediately rather than on the next click. A selection with
nothing to show — a session with no file selected — renders as a state,
not an error. **Dock hands back what the panel moved to**, not what it was
opened with.

The subject travels in the window's URL (`index.html?panel=sidecar&…`), so
the window is self-sufficient the moment it boots — no waiting for the
webview to be ready and no race between "the window exists" and "the window
knows what to show". It then makes exactly the same backend calls the
docked panel makes, through one shared loader, because a second code path
is how the two versions drift apart.

Edits broadcast: saving notes emits `nebula://changed` to every window, so a
torn-off panel showing the same file refreshes rather than quietly going
stale. Two windows editing one `annotations.yaml` is still last-write-wins,
which is the archive's model everywhere else.

**A capability note worth knowing**: `capabilities/default.json` scopes
permissions by window label. It listed only `main`, which would have left
every extra window — `File ▸ New Window` included — unable to invoke
anything at all, bridge included. It now covers `main`, `nav-*` and
`panel-*`, and grants `core:window:allow-close` so a panel window can close
itself when docked. Add new window-label prefixes there or they will boot
into a dead app.

### Dialogs

Every dialog is **movable**: its header is a drag handle, so a box can be
pushed aside to see what is underneath. Movement is clamped so the header
always stays reachable — a dialog dragged off the top could never be
dragged back — and the position resets when the dialog is reopened, since a
box that remembers where it was put is a box that opens off-screen after
the window is resized.

**Mod-Enter confirms** the open dialog's *primary* action. Deliberately the
primary one: Enter should never delete, so a dialog whose main action is
destructive still needs the mouse. With no dialog open, Mod-Enter in a
notes editor saves it — the same "commit what I just typed" gesture one
layer out. Plain Enter is left alone, because a comment box needs newlines.

### Selection is opt-in

Dragging in the file list used to smear a text selection across every label
it passed. The rule is now the one native apps use: **chrome is not text**.
`body` is `user-select: none`, and selection is turned back on only where
there is something worth copying — inputs, `pre`/`code`/`.mono`, the panel
bodies, the index table, the status bar. During any drag (marquee, file,
tab, dialog, splitter) it is forced off entirely.

### Two toolbars

The window has an **app toolbar** as its top row — New archive, Open,
Import, Export, Archive management, then the open archive's path, the
archive-kind badge, the no-user warning and the theme toggle on the right —
and a
**file toolbar** inside the browsing pane for things that act on the file
list: reload, list/grid, sort, view options, session info, import files.
The split is by scope: the top bar acts on archives, the lower one on what
is being looked at.

There is no separate title row — the archive name is in the OS title bar
and the toolbar, which buys back ~38px of vertical space. The toolbar is
padded left to clear the traffic lights and is itself a window-drag region,
with the controls in it excluded.

### View options

The file toolbar's **View** button opens a dialog for what the lists
*show* (nothing here changes which rows exist — that is search and sort):

- **File browser** — your tags, a mark where you left a comment, duplicate
  write order, metadata (`.meta.json`) rows, and checksum verification.
- **Session list** — your tags, the tags recorded when the session was
  created, a comment mark, item/problem counts, and day grouping.

Verification is the only switch that re-lists, since it is the only one
that makes the backend re-read files — and it re-runs whichever view is
open, a session listing or a search.

**Comments are marked, never quoted.** A comment is a paragraph and a list
row is not the place for one, so it renders as a small note glyph with the
text in the tooltip. **Tags are clickable**: choosing one opens a new tab
searching the archive for it, leaving whatever you were doing intact.

### Creating archives

The toolbar's **New archive** button and `File ▸ New Archive…`
(`Mod-Shift-N`) open a dialog that writes the whole skeleton —
`archive.yaml`, `data/`, `code/` — with its rules chosen up front rather
than discovered later: kind (standard or intake), name, folder, owner,
whether to snapshot source on every save, whether to keep the index current
as sessions close, whether to register it, and what a colliding write should
do.

An intake archive's name field is disabled, because a timestamp *is* the
name — that is what makes "saved in `intake_2026_07_31_190230/I-26-0001`"
resolvable after a merge.

### Importing

The toolbar's **Import** button (and the File menu) distinguishes four
things that are genuinely different:

| | what it does |
|---|---|
| **Files…** | adds files to the open session |
| **Intake archive…** | *merges* it into this archive, renaming its sessions |
| **Fragment…** | *files* it under `$NEBULA_HOME/fragments/<owner>/<archive>` — nothing is added to your archive |
| **Adopt from Fragment…** | copies sessions out of a fragment into this archive, under your own ids |

Importing a fragment previews first, listing each archive it would file —
including ones a colleague forwarded, which land under *their* author — and
reports any session already installed with different content, which is kept
rather than replaced.

### When no user name is set

A red **no user set** badge sits in the titlebar until one is. Clicking it
explains why it matters: archive names are not unique, so the user segment
is what makes `nebula://grant/postdoc/S-26-0012` unambiguous — without it, a
reference to your archive is ambiguous on anyone else's machine. The
new-archive dialog repeats the warning, since the owner is written into
`archive.yaml` at creation time. `nebula init` prints the same warning.

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
| `Mod-Shift-C` | Add the selection to a collection |
| `Mod-1/2/3`   | Sessions / Collections / Saved searches |
| `Mod-T`       | New tab |
| `Mod-D`       | Duplicate the current tab |
| `Mod-W`       | Close tab (the last one stays open) |
| `Mod-Shift-]` / `Mod-Shift-[` | Next / previous tab |
| `Ctrl-Tab`    | Cycle tabs (Ctrl on every platform, including macOS) |
| `Mod-Shift-L` | Show relations for the selection |
| `Mod-Shift-N` | New archive… |
| `Mod-Shift-O` | Open archive… |
| `Mod-Shift-E` | Export a fragment… |

On macOS these are listed in the **View** menu so they can be found without
memorising them. That has an implementation consequence worth knowing:
AppKit dispatches menu accelerators *before* the webview gets a `keydown`,
so a menu item cannot be a mere label — once it owns an accelerator, the
keyboard path stops firing. Each item therefore emits `menu://action` with
its id, and the front-end runs the same handler (`MENU_ACTIONS` in
`main.js`, ids matching `install_menu()` in `main.rs` — keep them in sync).
The `keydown` handler still serves Windows/Linux, where no menu is installed.

`main.rs` installs a deliberately small menu (app / Edit / View / Window);
**keep Edit**, since WKWebView's copy, paste and select-all are driven by
those items. Plain `Cmd-M` belongs to Window ▸ Minimize, which is why
metadata is `Cmd-Shift-M`. Archive Management is in the menu without an
accelerator.

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
`open_url`, `get_annotations`, `set_annotation`, `archive_stats`,
`rebuild_index`, `check`, `gc`, `delete_file`, `reseal`, `adopt_file`,
`delete_session`, `hold`, `release`, `list_collections`, `collection_tree`,
`create_collection`, `delete_collection`, `collection_add`, `collection_remove`,
`collections_containing`, `list_views`, `run_view`, `save_view`, `delete_view`,
`importable_sessions`,
`frozen_sessions`, `import_new`, `import_file`, `open_path`,
`file_manager_name`. Test any of them without the GUI:

```
python -m nebula.navigator.api list_sessions '{"archive": "/path/to/archive"}'
```
