# Relational data in the Navigator — roadmap

Parked while user-organisation work (collections / saved searches / tag
vocabulary) happens first. This is the design discussion written down, not a
commitment to build in this order.

Status key: **WANTED** = Grant has said yes; **OPEN** = needs a decision;
**IDEA** = worth having, not yet argued for.

---

## The constraint everything here obeys

Three properties define how nebula stores things, and any relational feature
has to work with them rather than around them:

1. **The filesystem is the truth.** `index.db` is a disposable cache that can
   be deleted and rebuilt (`index.py`). Anything that only works with a fresh
   index is a degraded feature, not a broken archive.
2. **A session is a time bucket, not a topic.** `data/<year>/S-<yy>-<nnnn>/`,
   with the id minted at creation.
3. **A ref is a `(session, filename)` pair.** `derived_from`, `related_runs`,
   `model.lineage`, and `check`'s dangling-ref scan all assume a file's
   identity *is* where it lives and what it is called.

Consequence: **organisation and relationships are additive views over
immutable storage, never rearrangement of it.** Moving files into folders
breaks provenance links silently, and cross-archive refs can't even be found
to rewrite. Same reason `nebula rename` stays contentious (see Open
questions).

---

## What exists today

| Capability | Where | Surfaced in the GUI? |
|---|---|---|
| `derived_from` refs on artifacts | `sidecar.py`, written by `s.artifact(derived_from=…)` | yes — Lineage section, one hop each way |
| One-hop upstream/downstream | `model.lineage()` — filesystem scan, index-independent | yes — clickable, marks missing/unreachable targets |
| Transitive upstream/downstream, cross-archive | `graph.py` (`upstream()`, `downstream()`) — **index-backed** | **no — never called by the GUI** |
| `related_runs` (session ↔ session) | `session.yaml`, `SessionMeta.add_related_run()` | chips only — not resolved, not clickable |
| `source` (`script` / `external`) + `origin` | `produced_by` | shown per file; **not filterable or searchable as a facet** |
| Captured source snapshots | `codestore.py`, `code/` | Captured-source panel + restore |
| Dangling-ref reporting | `check.py` | Archive panel → Run check |
| Ref parsing/formatting (incl. `archive|session/file`) | `refs.py` — single canonical parser | used by lineage + import validation |

---

## Work items

### 1. `related_runs` is a dead end — **DONE** (2026-08-01)

Session-level refs render as plain chips: not resolved, not clickable, no
indication whether the target exists. Meanwhile artifact-level lineage rows
already resolve, mark missing/unreachable targets, and navigate on click.

**Done:** `session_info` resolves them through `_resolve_ref()`, and the
panel renders them with the same row component as lineage (↔ arrow) — struck
through when missing, marked separately when the archive is unreachable, and
clickable when the target is there.

### 2. Transitive lineage — **DONE** (2026-08-01)

`model.lineage()` deliberately walks the filesystem (so it is right even with
a stale index) but stops at one hop. `graph.py` already does transitive
traversal and resolves cross-archive refs via the registry.

```
Lineage
  ← raw.tome            S-26-0031
      ← cal.json        S-26-0028
  → proc.graf           S-26-0034
      → figure-3.png    S-26-0040   ✎ paper:2026
```

**Do:** traverse index-first via `graph.py`, falling back to the filesystem
scan when the index can't be trusted, with a depth cap and an "expand".

**The index groundwork is done** (2026-08-01). What changed:

- `index.ensure_fresh()` sweeps by per-session stat signature, so the index
  is current without anyone remembering to rebuild — `graph.py` traversals
  now open through `open_fresh()`.
- `derived_from(ref_file, ref_session)` is indexed, so the *downstream*
  direction is no longer a full table scan per hop.
- `graph.py` pools one connection per archive per traversal, so a deep walk
  costs the graph, not the graph times the archive.
- Session paths are stored relative, so nodes resolve to real paths on
  whichever machine the archive is mounted on.

That removes the performance half of the depth question: hops are now
cheap. What remains is a *display* decision — how much to show at once —
which argues for putting depth in the GUI (default 3, with expand) rather
than in `archive.yaml`, where it would be an archive-wide answer to a
per-question choice.

**Shipped** as `model.provenance_tree()` + the Relations tab: multi-hop in
both directions, index-first with a sidecar-scan fallback (the view says
which it used), a depth control (1/2/3/5/10/all, default 3), and a "go
deeper" affordance on any node whose children were withheld.

### 3. Session-level provenance view — **DONE** (2026-08-01), form: tree

Everything today is per-file. Nothing answers "what did this run produce, and
what came of it" in one look.

**Option A — indented tree.** Reuses the existing lineage row rendering,
no layout code, works in a narrow dock panel, degrades gracefully with many
files. ~90% of the value.

**Option B — node-link diagram.** SVG DAG laid out left-to-right by
timestamp. Shows fan-in/fan-out honestly (a tree duplicates a node that has
two parents), and is the natural seed of the timeline canvas in item 5.
Costs layout code to maintain and needs the main area, not the dock.

**Chosen: A.** `provenance_tree(archive, run_id)` with no filename makes
every artefact the session produced a top-level branch, each with its own
"Built from" and "Used by". Because the tree is walked with one shared
visited set per branch, a diamond appears under both parents but expands
only once and the repeat is labelled *shown above* — an indented tree
cannot draw reconvergence, so it says so rather than duplicating a subtree
(which also stops a wide DAG from blowing up).

B stays open as the seed of the timeline canvas (item 5); the tree already
consumes the shape a node-link view would need, so nothing is wasted.

### 4. Calendar / activity view — **4a/4b DONE** (2026-08-01)

Three layers, cheapest first; they stack rather than compete.

**4a. Date grouping in the rail** — sticky headers over the existing session
list. No new data, no new view.

```
SESSIONS                    12 of 34
─ Today ──────────────────────────
  📁 S-26-0034  VCCS warm-up    2 ⚠
─ Yesterday ──────────────────────
  📁 S-26-0032  overnight soak
─ July 2026 ──────────────────────
  📁 S-26-0031  diode IV
```

**4b. GitHub-style activity strip** — one cell per day, shaded by session (or
artifact) count. Click a day to filter; drag a range to drive the existing
date filter with a picture instead of two text fields.

```
2026   J F M A M J  J  A
  Mon  · · ▪ ▪ ▪ ▪▪ ▪▫
  Wed  · ▪ ▪ · ▪ ▪▫ ▪▪     ▫ 1–2 sessions
  Fri  · · ▪ ▪ ▪ ▪▪ ▫·     ▪ 3+
```

Fits nebula specifically: the archive is *already* bucketed by year and every
session carries a creation timestamp, so this renders `data/<year>/` — it is
not a new index.

**Done:** `model.activity()` buckets sessions per *local* day (parsed, not
string-sliced, for exactly the offset reason below). The rail groups sessions
under Today / Yesterday / weekday / Month Year, and the **calendar** toggle
shows a trailing-weeks strip sized to the current rail width; clicking a day
filters the list, clicking it again clears. Only item 5 (the timeline canvas
with lineage arcs) is left here.

### 5. Timeline canvas with lineage arcs — **IDEA**

Time on the x-axis, one row per session, artifacts as marks, and
`derived_from` edges drawn *between* rows — a file in August deriving from a
raw run in March shows as an arc across the archive. This is the payoff of
items 2–4 together, not a separate feature; only worth it if item 3 lands as
a node-link diagram whose layout code can be shared.

### 6. `source` as a facet — **DONE** (2026-08-11)

"Show me every hand-imported file in this archive" is a natural audit
question and is currently unanswerable in the GUI. One more checkbox group in
the ⇅ Sort popover (script / external / unrecorded) and one more field in
`search_items`. Note that `source_recorded: false` (old sidecars that predate
the field) must stay distinguishable from a real `script` — the panel already
does this with the `script?` chip.

Built: `Item.source_recorded` (read from the raw sidecar JSON, since
parsing is what fills the default in and loses the distinction),
`model.SOURCE_FACETS`, `item_source_facet`, and a `sources=` filter on
`search_items`. Three checkboxes under "How it got here" in the advanced
search popover.

It is a *filter*, not a search term, so it narrows on its own: selecting
only "Imported by hand" with an empty query lists every externally-imported
artefact. `sources=None` means no filter and `sources=[]` (every box
unticked) means nothing matches -- the two must not collapse, or unticking
everything would quietly read as no restriction whenever a query was also
present.

---

## Since parked

- **Nebula URIs landed** (`refs.py`): `nebula://<user>/<archive>/<session>/<file>`
  and `.../collections/<name>`, accepted anywhere a ref is — including
  `derived_from` and `related_runs`, which now keep the owner on disk.
  Item 1 below (`related_runs` resolved + clickable in the GUI) is still
  outstanding; the data is richer now, so it is worth doing well.
- **Collections** (`collection.py`) already resolve nested entries and mark
  missing/unreachable targets — the same rendering item 1 needs, and
  probably the same GUI component.

## Open questions

- **Cross-archive traversal** — `graph.py` needs an index and the registry.
  Show unreachable archives as "unresolved" nodes (as `_resolve_ref` already
  does), or hide them?
- **Tag vocabulary** (deferred by Grant, 2026-08-01): canonical tags,
  aliases, a GUI selector, plus "never show/use tag X" and "replace X with
  Y" rules.
- **`nebula rename`** — the one organisational fix that requires touching
  refs. Doable in the project's spirit (rewrite same-archive refs, record the
  old name like `original_name` already does for duplicates, log it in
  session history), but cross-archive refs would dangle with no way to find
  them. Would want a dry run and an explicit decision on that residual risk.

---

## Sequencing

1. ~~`related_runs` resolved + clickable (item 1)~~ — **done**
2. ~~Rail date grouping + activity strip (item 4a, 4b)~~ — **done**
3. ~~Index freshness, relative paths, reverse edge index, year seals~~ —
   **done** (2026-08-01); the prerequisite for cheap multi-hop traversal.
4. ~~Transitive lineage with a depth cap (item 2)~~ — **done**
5. ~~Session provenance view, indented tree (item 3)~~ — **done**
6. ~~`source` facet (item 6)~~ — **done** (2026-08-11)
7. Timeline canvas (item 5) — **blocked, needs a decision.** Its own entry
   makes it conditional on item 3 landing as a node-link diagram whose
   layout code could be shared. Item 3 shipped as an indented *tree*, so
   that precondition never held. It needs either a redesign or dropping.
