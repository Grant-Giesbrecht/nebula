# URI grammar: archive ids, and one spelling for refs

**Status: BUILT** 2026-09-01. Decided in conversation the same day; this
document is both the argument and the specification.

Companion to `identity-trust-roadmap.md`, which settled the *owner* segment
(`value@authority`) and left two things open: the archive segment is mutable
with no trail, and there were two unrelated ref spellings joined by an
arbitrary `|`. This settles both.

---

## The two problems

**1. An archive's name can change, and nothing records that it did.**

Artifacts have a rename log (`manual.rename_file`), so a ref to an old
filename still resolves. Collections have one. Archives had neither: change
`archive.yaml`'s `name` and every URI ever written into that archive dangles,
silently, with no way back.

This became urgent the moment `nebula uri` and the Navigator's "Get URI"
started handing people strings to keep. Before that, nothing could go stale
because nothing was ever held.

**2. `|` was arbitrary.**

    postdoc|S-26-0152/diode.graf          compact, cross-archive
    nebula://grant@ncsu.edu/postdoc/…     URI

Two grammars, one meaning, and a separator chosen for no better reason than
that it was unused. Nothing about `|` says "archive boundary" to a reader
who has not been told.

## The shape

An archive carries an **immutable id**, minted once and never changed, and
the readable name rides alongside it in the same segment:

    nebula://grant@github.com/postdoc~0fe/S-26-1234/artifact.tome
                              ^^^^^^^ ^^^
                              label   id

This is the slug pattern — Stack Overflow's `/questions/12345/why-does-…`,
Notion pages, Linear issues. An opaque id carries identity; a human slug
rides along and is *ignored on read*. Rename the thing, the old link works.

One rule makes it correct, and everything else follows from it:

> **The id is authoritative. The label is decorative, and is never compared
> or resolved on.**

`refs.Ref` therefore splits the segment into `archive` (the label) and
`archive_id`, and `Ref.identity()` — the thing dedup and cycle detection use
— omits the label whenever an id is present. Without that rule,
`postdoc~0fe` and `thesis~0fe` are two different refs to one object, which
reintroduces exactly the failure the identity roadmap uses to argue hub names
out of the URI: "the same archive mirrored on two hubs would get two URIs, so
`derived_from` edges pointing at one object would stop comparing equal."

### The id is a number, not a token

`0fe`, `00fe`, `fe` and `0FE` are **the same id** — hex, with leading zeros
optional, exactly as `7` and `0007` are one number. So:

- Normalisation is purely lexical: parse as hex, render canonically. No
  registry, no resolution, no ambiguity. `refs.py` can do it alone, which
  keeps the grammar/facts split (`refs` = grammar, `uris` = what is on disk)
  intact.
- The namespace has no width. `0fe` and `1a2b3c` were always in one space;
  you just had not reached the large numbers yet. Nothing ever has to be
  lengthened, so no id ever changes.

The alternative considered was a fixed-width random token with git-style
*prefix* matching. Rejected: prefixes are ambiguous, so they need the
registry to resolve and a "did you mean…" error state, and a ref cannot be
compared without resolving it first. The integer reading gets the same
short-ids-now-headroom-later property with none of that.

**Canonical rendering** is lowercase hex, zero-padded to a minimum of three
characters, growing naturally past that. `fe` → `0fe`; `1a2b3c` stays as it
is. Since equality is on the value, this is purely cosmetic — it just keeps
archive segments looking uniform.

`~0` is reserved as a sentinel for "no id" and is never minted.

### Minting, and when to widen

Ids are drawn at random from a range, not allocated sequentially: a counter
would hand two archives created on two machines the same id, and the whole
point is that the id survives leaving this machine.

Ranges are one hex digit wide each:

| Width | Range | Capacity |
|---|---|---|
| 3 | `0x100`–`0xFFF` | 3 840 |
| 4 | `0x1000`–`0xFFFF` | 61 440 |
| 5 | `0x10000`–`0xFFFFF` | 983 040 |

**Widening is computed, not discovered.** Probing until you happen to fail
tells you a range is full only after it has already cost you: as occupancy
approaches 1 the expected number of draws grows without bound, and a full
range never terminates at all. Instead, occupancy is counted directly —
`archive_id.mint` walks the taken ids once (there are tens of them, not
millions), and picks the narrowest range holding fewer than `LOAD_FACTOR`
of its capacity. At a load factor of 0.5 each draw then succeeds with
probability > 0.5, so minting is two draws in expectation regardless of how
many archives exist.

Someone with eight archives gets clean 3-character ids forever. Someone with
four hundred drifts into 4 characters without anyone deciding anything, and
every id already written keeps working, because a shorter id is not a
truncation of a longer one — it is a smaller number.

### The one case the check cannot cover

Minting checks the ids already known to this machine. An archive created
somewhere that does not know about your others — a lab PC, a fresh laptop, a
fragment you adopt — can collide.

It is rare, it is detectable, and it has a defined repair: the collision
surfaces the moment both archives meet on one machine (`nebula register`,
`nebula receive`, `nebula check`), and whichever archive has fewer external
refs re-mints. Re-minting is a real break — refs already written to the
re-minted archive dangle — which is why the check exists at all rather than
letting two archives quietly share an id forever.

## One grammar

`|` is gone. What is left is `/`-separated segments, where **you may drop a
prefix of them and the missing ones mean "here"** — an absolute path versus a
relative one:

```
raw.csv                                                 this session
S-26-0152/raw.csv                                       this archive
collections/paper-2026                                  this archive
assets/AF-26-0017                                       this archive
nebula://postdoc~0fe/S-26-0152/raw.csv                  another archive of mine
nebula://grant@github.com/postdoc~0fe/S-26-0152/raw.csv someone else's
```

The scheme marks where the path starts:

> **`nebula://` whenever you name an archive or a user. Bare when you are
> inside this archive.**

Filesystems do not make you write `file:///` for a relative path, and
neither does this. That matters because the overwhelmingly common ref in real
code is a bare filename — `derived_from=["raw.csv"]` — and
`derived_from=["nebula://raw.csv"]` would be both longer and misleading, with
a filename sitting where a hostname goes.

### Reading the first segment

`nebula://A/B/…` is decided in this order, and every branch has a reason:

1. **`A` contains `@`** → `A` is a user. This is the `value@authority`
   production; nothing else in a URI contains `@`.
2. **`A` contains `~`** → `A` is an archive, and the user is implicit ("me").
   Nothing else contains `~`, which is why archive names must not.
3. **`B` is session-shaped (`S-`/`I-…`) or a reserved segment
   (`collections`, `assets`)** → this was meant to be an archive that omitted
   its id. **Error**, saying so, rather than misreading `A` as a user.
4. **Otherwise** → `A` is a user (a bare, unqualified one) and `B` is the
   archive. This is the legacy form, and it is why the id is *mandatory* in
   the user-omitted spelling: without rule 3 as a guard,
   `nebula://postdoc/S-26-0152` could be read either way.

Rule 4 is the reason old refs keep working. `identity.py` deliberately
permits a bare `grant` (read as `grant@local`) and argues against rewriting
it, since that would change every URI already pointing at that archive. So
`nebula://grant/postdoc/S-26-0152` must stay readable, and does.

### Archive names

`~` has to be impossible in a name for rule 2 to hold, and names were
essentially unvalidated before this — `refs._check_segment` only ruled out
`/`. `config.clean_archive_name` now applies the same treatment
`collection.clean_name` already gave collections: no `~`, no `/` `\` `|` `:`
`*` `?` `"` `<` `>`, no whitespace, no control characters, not a Windows
reserved name, 120 characters. It explains what is wrong rather than just
refusing.

Reading stays permissive, as everywhere else in nebula: an archive whose
name predates the rule still opens and still resolves. Only *new* names are
checked.

## Migration

Nothing on disk has to change, and nothing that already resolves stops.

- **`|` is still parsed, and never emitted.** `format_ref` writes the URI
  form; `parse_ref` accepts the old one indefinitely. Refs already written
  keep working and get rewritten the next time anything reformats them. Same
  approach as the `archives.yaml` → `registry.yaml` rename, which still reads
  the old filename.
- **A URI with no id in its archive segment is still parsed** (rules 1 and
  4), so every ref written before today resolves unchanged.
- **Ids are minted lazily.** `config.ensure_archive_id` mints one for an
  archive that has none, called wherever an archive is created, registered,
  scanned or asked for a URI. No migration command to run and nothing to
  remember.
- **Resolution prefers the id and falls back to the name.**
  `Registry.find(name, user, archive_id=…)` matches on the id when the ref
  carries one and the registry knows it, and on the name otherwise — so a
  ref written today into an archive whose id you have not seen yet still
  finds it.
- **The index gained `ref_archive_id`** (schema 5, one rebuild).

## What this does not fix

**The owner segment.** Move from `@ncsu.edu` to `@orcid.org` and every URI
still dangles for anyone whose `contacts.yaml` lacks your succession trail.
An archive id is orthogonal; see "Petnames and identity trails".

**Stale labels.** A ref written in 2026 carries the label the archive had in
2026. It still *resolves*, because resolution uses the id, but it displays
the old name until something reformats it. A relabel-on-display pass is
possible later; it is cosmetic, and doing it eagerly would mean resolving
every ref just to print it.

**Session id typos.** `S-`/`AF-` ids still carry no check digit, so a typo
landing on a real *different* session is still undetectable — the limit
`transfer._plan_repairs` already states. Weighed and deferred: the blast
radius touches every session directory name on disk, it can only ever apply
to new ids, and it does not depend on anything here.

## Implementation

| Piece | Where |
|---|---|
| id arithmetic, minting, range widening | `nebula/archive_id.py` |
| `id:` on disk, lazy minting, name validation | `nebula/config.py` |
| grammar, `Ref.archive_id`, `Ref.identity()` | `nebula/refs.py` |
| id in stored refs | `nebula/sidecar.py` |
| `ref_archive_id`, schema 5 | `nebula/index.py` |
| id-first archive lookup | `nebula/registry.py` |
| minting, resolution, `nebula uri` | `nebula/uris.py`, `nebula/cli.py` |
| id-aware traversal | `nebula/graph.py`, `nebula/navigator/model.py` |
| renaming an archive | `nebula config <archive> --name` |
| collision reporting | `Registry.find_id_collisions`, shown by `nebula archives` |

Tests: `tests/test_archive_id.py` (the arithmetic and the widening rule),
`tests/test_refs.py` (the grammar), `tests/test_uris.py` (survival across a
rename, a move, and both at once), plus additions to `test_index.py`,
`test_graph.py` and `test_cli_uri.py`.

Two things the tests caught that the design did not anticipate:

* `Registry.find` matching on an id had to prefer an entry that is
  *actually on disk*. One archive can have several registry entries -- an
  alias, the `<user>-<name>` fallback, or a stale one left behind when it
  moved -- and returning the first match reported "not mounted" from a path
  nobody uses any more.
* `find_id_collisions` has to group by archive **root**, not by entry, for
  the same reason: several nicknames for one directory are aliases, which is
  normal. Two *directories* claiming one id is the real collision.
