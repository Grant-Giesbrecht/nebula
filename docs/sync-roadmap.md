# Syncing, backup and multi-machine use — roadmap

**Status: OPEN.** Discussion 2026-08-10, written up 2026-08-12. Almost
nothing here is built. This captures the argument while it is fresh, in the
same spirit as `relational-data-roadmap.md`.

Status key: **WANTED** = Grant has said yes; **OPEN** = needs a decision;
**IDEA** = worth having, not yet argued for.

---

## The goal, in Grant's words

> When it's used in a moderate-good practice (like MEGA), instead of
> breaking quietly, it breaks loudly and fixably.

That is the whole brief. Nebula is not going to stop anyone putting an
archive in a cloud-sync folder, and telling them not to is not a design.
The question is what nebula can do so the failure modes announce
themselves instead of corrupting provenance in silence.

## Why an archive is harder to sync than a folder of files

An archive is not just bytes on disk; it holds *invariants between*
files. Sync tools do not know about any of them:

- **Ids are allocated by looking at the folder tree.** Two machines
  offline at once can both mint `S-26-0153` — see `_next_free` /
  `allocate_asset_id`. Neither is wrong locally; the merge is silently
  ambiguous.
- **A sidecar and its artefact are two files** written atomically
  (mkstemp + `os.replace`) but synced independently. A sync tool can land
  one and not the other, producing a `stray` or `orphan` that looks like
  corruption but is only a partial sync.
- **`index.db` is a SQLite file** in the middle of the synced tree. Two
  machines writing it produces conflicted copies at best, and it is
  *disposable* — it should never have been a sync candidate at all.
- **Content-addressed stores are write-once**, which is the one part that
  syncs cleanly: same name means same bytes, so a conflict is impossible
  by construction. Worth noting because it says where the danger is *not*.

## Three scenarios Grant raised

**1. A working archive with a remote backup.** Data collected on a laptop
or a flash drive, backed up to a NAS or server. Wants bidirectional
detection ("has the remote moved on?") and the ability to revert. This is
the closest to solved: it is backup, not sync, and borg/restic already do
it well. What is missing is *verification after a restore*.

**2. Cloud sync (MEGA, Dropbox, iCloud).** What Grant actually does today.
Convenient, works most of the time, and has exactly the race conditions
above. The realistic aim is detection and loud failure, not prevention.

**3. Git.** Tempting because the mental model matches. Fails on the data:
git is bad at large binaries, and the content-addressed store would be a
second content-addressed store inside the first. Possibly useful for
`archive.yaml` and session metadata alone, but a split repo where the
metadata is versioned and the data is not is its own trap.

## The client-server turn — **OPEN**

Grant, 2026-08-10, after working through the above:

> I think this might be the wrong way of going about it. We're inventing
> something very complicated. What if the standard archive lives on a
> server, clients send commands, the server runs them through the nebula
> API against its own archive, and changes are broadcast to clients?

This is the strongest idea in the discussion and it dissolves most of the
problem: one point of truth, so no id collisions, no partial writes, no
conflicted `index.db`, and access control becomes the network's job rather
than the filesystem's.

Grant's own stated costs, both real:

- **Streaming large files to clients** is not obvious. A 100 GB dataset
  cannot be an RPC response.
- **Two very different implementations behind one UI.** Navigator must
  look and feel identical on a local archive and a networked one, even
  though one is a filesystem walk and the other is a network round trip.

Unresolved, and the reason nothing is being built yet. See "Questions to
settle".

## Tiers this has to serve

Carried over from `identity-trust-roadmap.md`, because the same three
tiers apply and the same test holds — **moving between them must not
rewrite a single URI already written**:

- **A. Local only.** Laptop or flash drive, borg/restic backup, integrity
  entirely the user's responsibility.
- **B. Self-hosted server.** A lab server as single point of truth, several
  machines pushing and pulling.
- **C. Hub.** Public resolution and namespace issuance.

Nothing in this document should make A harder. Most nebula use is A, and
an archive that needs a server to open is a worse tool.

---

## Work items that stand alone — **WANTED**

These three came out of the discussion, are independent of the
client-server decision, and are worth doing whatever that decision is.
None of them is built.

### 0. ~~Multiple locations per archive~~ — **DONE** 2026-08-12

The registry now lists *locations* per archive, most-preferred first, and
resolution takes the first that is actually there. So an unplugged external
drive falls back to the NAS copy rather than failing.

- `~/.nebula/archives.yaml` is now `~/.nebula/registry.yaml`. It was one
  character from an archive's own `archive.yaml`, which was a standing
  source of confusion. The old name is still read when the new one is
  absent, and `nebula archives` renames it in place.
- A location is a local `path` or a remote `url`. **Remote locations are
  recorded but never reachable** — nebula has no client, and the honest
  behaviour is to report "remote; no client yet" rather than a ✗ that reads
  as broken. Recording one is still useful: it says where the archive also
  lives, and it means the file format does not have to change when a client
  does exist.
- `nebula archives <name> --add-location <path|url> [--label ...] [--prefer]`.
  Appended by default: a newly added location is usually a backup, and
  silently preferring it over the working copy would be a surprise.
- The last location cannot be removed. An entry with nowhere to look is not
  a registration, it is a puzzle.

`ArchiveConfig.root` is now a property returning the first available
location, so every existing caller gained the fallback without changing.

This is the "ordered resolver list" from `identity-trust-roadmap.md`, for
archives. The identity-side half of it — resolving a *URI* through a list
of servers — still needs the client-server decision below.

### 1. Move `index.db` out of the synced tree

It is a disposable cache, rebuildable from the filesystem at any time
(`index.py` says so in its own docstring), and it is the single most
likely thing to produce a sync conflict — a binary SQLite file written by
every read that finds a stale signature.

Put it under a machine-local path (`~/.nebula/index/<archive-key>.db`), the
way the registry and identity already live outside archives because they
describe *this machine* rather than the archive.

Care needed: `check`, `rebuild`, the Navigator's index panel and the year
seals all know where it lives, and an existing archive has one in the old
place. Wants a migration that moves it and leaves nothing behind to be
found later and half-used.

### 2. Teach `check` to recognise sync-conflict files

Every sync tool has a signature for a file it could not merge:

    raw.csv (conflicted copy 2026-08-10)      Dropbox / MEGA
    raw.csv.sync-conflict-20260810-...        Syncthing
    raw 2.csv                                 iCloud

Today these land in a session folder and read as ordinary artefacts with
no sidecar — reported as `orphan`, which is true but says the wrong thing.
It looks like nebula lost a sidecar when in fact the sync tool duplicated a
file. A dedicated `sync_conflict` issue naming the tool and the original
would turn a confusing error into an actionable one. This is the single
cheapest step toward "breaks loudly and fixably".

### 3. Record the creating machine in `session.yaml`

One field, written at `nebula.new`. Costs nothing and answers the question
every one of the failure modes above raises: *which machine wrote this?*
Without it, a duplicate-id collision is a mystery; with it, it is a
five-second diagnosis. It also gives `check` something to say when two
sessions share an id — which it already detects (`duplicate_id`) but can
only describe as "two folders share this id".

---

## Smaller ideas — **IDEA**

- **A lock file with a machine name and heartbeat.** Advisory only, since
  nothing can enforce it across a sync tool, but "postdoc-laptop has had
  this archive open since 14:02" prevents most of the damage by telling a
  human before they start.
- **`nebula verify` as a restore check.** Item A's real risk is a silently
  corrupt restore. `check --verify-checksums` already does the work; what
  is missing is it being the obvious, documented thing to run after a
  restore.
- **Id allocation with a machine-scoped suffix while offline**, reconciled
  on sync. Avoids collisions at the cost of uglier ids. Probably worse
  than the disease for tier A, but worth writing down.

## Questions to settle before building anything

1. **Is the client-server model the target, or a fallback for tier B
   only?** Everything else here is unaffected by the answer, which is why
   the three items above are safe to build now.
2. **What does a client do with a 100 GB artefact?** Stream on demand,
   cache locally, or never fetch it and operate on metadata alone? This is
   the question that decides whether the model is viable at all.
3. **Does Navigator hide the difference, or show it?** Grant's stated
   requirement is that local and networked look identical. The honest
   counter-argument is that a network round trip *is* different — latency,
   partial failure, someone else's concurrent edit — and hiding that
   reproduces exactly the quiet-breakage this document exists to prevent.
4. **What is the smallest useful server?** A read-only resolver that
   answers "where does this URI live?" is far less work than a full
   command broker, and it might be enough for the citation use case that
   motivates the URI scheme in the first place.

## Nearest-term step

The three **WANTED** items, in order: sync-conflict detection in `check`
(cheapest, and directly serves "breaks loudly"), then the machine field,
then moving `index.db`. None of them commits to an answer on
client-server, and all three make the eventual answer easier to reach.

## See also

- `identity-trust-roadmap.md` — the three hosting tiers, and why hub names
  stay out of URIs.
- `relational-data-roadmap.md` — cross-archive traversal, which shares the
  "is that archive reachable?" problem.
