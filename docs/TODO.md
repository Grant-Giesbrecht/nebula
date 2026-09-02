# TODO

Small, concrete work items. Anything needing a design argument gets its own
roadmap document instead — see the index at the bottom.

Format: one heading per item, with enough context that it can be picked up
cold. Delete an item when it ships.

---

## ~~The question was asked before the answer could matter~~ — DONE 2026-09-02

`nebula.session(archive, tags=[...], description="...")` made the caller
collect both **before** the picker ran, then discarded them, silently,
whenever the user picked an existing session — which already has its own.
Half the time the question cost typing and had no effect, and nothing
said so. Reported from `examples/ex2_measurement.py`, where the tag
picker ran at the top of the script for exactly this reason.

Two separate problems turned out to be tangled together, and the fix is
mostly about telling them apart:

- **Session tags describe the run**, so they only exist for a session
  being *created*. They are now collected by
  `session_select.ask_new_session_metadata`, called after the choice and
  only on the `/new` branch. `ask` is tri-state: `None` asks for whatever
  was not supplied, `True` always asks (pre-filling), `False` restores
  the old never-ask behaviour. Non-interactive runs never prompt, so
  unattended scripts are unchanged.
- **Artifact tags describe the file**, and there was no way to set them
  from Python at all. Per-file tags already existed as
  `annotations.yaml` — what `nebula annotate` edits and
  `nebula search "user_tag:..."` reads — but only the CLI and the
  Navigator could write them, which in practice meant they were not
  written. `Session.artifact(tags=..., comment=...)`,
  `write_meta_for(...)` and `Session.annotate(...)` now reach it, plus
  `artifact_tags=` on the session for "everything this script writes".

Points worth keeping:

- Tags are **not** sidecar fields, and the temptation to make them one
  should be resisted: a sidecar records what happened and is never
  rewritten, so a label you change your mind about cannot live there.
  `test_tags_go_in_annotations_not_the_sidecar` pins this.
- They are validated at the `artifact()` **call**, not at block exit, so
  a typo'd tag costs a traceback before the measurement runs rather than
  after an hour of sweeping is on disk.
- They are stored under the name **on disk**. Overwrite protection may
  have written `raw-001.csv`, and tagging `raw.csv` would then describe
  the previous run's file — the same trap `_redirect_ref` exists for on
  the lineage side.
- `artifact_tags` survives appending to an existing session; session tags
  cannot. That asymmetry is the whole reason the two arguments are
  separate, and is why `artifact_tags` is still safe to ask for up front.
- The discard, where it still happens (`run_id=` given, or an existing
  session picked), is now *reported* rather than silent.

`examples/ex3_provenance.py` is new: every field `artifact()` records,
end to end, verified by running it.

## ~~Asset settings have no UI~~ — DONE 2026-08-10

Shipped as the "Asset defaults" section of the archive-management dialog
(`assetDefaultsHTML` / `wireAssetDefaults` in `main.js`). Notes worth
keeping, since they were the parts that needed care:

- Validation went into the **backend**, not the form:
  `model._coerced_asset_settings` enforces the ladder, the integer types,
  the `auto` exclusion and the enums, so the docstring's promise that "a
  stale front-end cannot write nonsense" is now actually kept. The form had
  been the only thing standing between a typo and `archive.yaml`.
- `model.asset_settings_preview` answers "which `auto` assets would move",
  and the form shows it live before saving. It returns validation failures
  as *data* rather than raising, because the form previews on every
  keystroke and passes through invalid intermediate states constantly.
- Sizes render in the largest unit that divides evenly, so a value entered
  as 256 MB comes back as 256 MB rather than 0.25 GB.
- `cap_action: drop` warns in-line and needs a confirmation.

Covered by 9 tests in `tests/test_assets.py` and 24 jsdom assertions.

**Original item, kept for context:**

**Status: OPEN.** Raised 2026-08-03. Backend is done; only the form is missing.

The archive-wide asset defaults are readable and writable over the bridge but
appear nowhere in the GUI, so they can only be changed by hand-editing
`archive.yaml`. Agreed during the asset design discussion that these should be
reachable from a settings menu.

**What exists**

- `nebula.navigator.api` ops `asset_settings` / `set_asset_settings`
  (`api.py`), backed by `model.asset_settings` / `model.set_asset_settings`.
- `asset_settings` already returns display-ready values: raw ints plus
  `*_human` strings, and the valid enums (`policies`, `cap_actions`) so the
  form does not have to hardcode them.
- `set_asset_settings` applies only known keys, so a stale front-end cannot
  write nonsense into `archive.yaml`.

**What to build**

A section in the archive-management dialog (`arcScrim` / `renderArchivePanel`
in `main.js`, around the existing panels) covering the seven settings:

| Key | Meaning |
|---|---|
| `policy` | default snapshot policy for small files — may not be `auto` |
| `periodic_above` | size at which the default becomes `periodic` |
| `manual_above` | size at which the default becomes `manual`, and the ceiling above which an *automatic* snapshot downgrades to observed |
| `period_days` | minimum gap between periodic snapshots |
| `max_snapshots` | retained snapshot count, 0 = uncapped |
| `max_snapshot_bytes` | retained snapshot bytes, 0 = uncapped |
| `cap_action` | `mark` (keep the record, flag the blob for gc) or `drop` |

**Things the form must get right**

- The two size thresholds are a *ladder*: `periodic_above` must be below
  `manual_above`, or the periodic rung is unreachable. Validate, or the
  setting silently does nothing.
- `policy` here is the ladder's bottom rung, so `auto` is not a legal value —
  `config.ArchiveSettings.from_dict` already rejects it, but the form should
  not offer it in the first place.
- Changing a threshold moves every asset whose own policy is `auto`, because
  `auto` re-resolves on read. That is the intended behaviour, but it is a
  bigger blast radius than a settings form usually has, so say so: show how
  many assets would change policy before saving.
- `cap_action: drop` discards snapshot *records*, which is not recoverable.
  Worth a confirmation rather than a bare dropdown.

## ~~The name on screen was not a name you could type~~ — DONE 2026-09-01

`nebula archives` prints the name each archive *declares* (the portable
one); every command resolved by registry *nickname* only. So a user read
`intake_name`, typed it, and got `unknown archive 'intake_name'. Known
archives: ['nebula_reg_name']` — a machine saying the word it had just
printed is not a word. `nebula register --remove` had it worst, since the
name it wanted was the one thing the listing did not show.

Fixed at the choke points rather than per command:

- `Registry.lookup` / `resolve_one` accept a nickname, a declared name or
  an archive id. An exact nickname wins (it is the file's unique key, and
  the escape hatch that makes two same-named archives separable); otherwise
  a name matching several *different* archives is refused with their
  nicknames, never guessed at. Several entries for *one* archive are not
  ambiguous — they are several doors into one room.
- `get`, `try_get` and `_resolve_archive_cli` all go through it. `try_get`
  is documented as "like get()" and now literally is: the two accepting
  different names is the exact shape of this bug.
- `--remove` forgets *every* entry pointing at that archive. Removing one
  alias left the others, so it looked like it had done nothing.
- Unknown-name errors list the names that would have worked, not just the
  registry's keys.
- `nebula archives` appends the owner when two archives declare the same
  name, since two identical-looking rows leave no way to say which is
  which; `--remove` says when others still declare the name it just took.

## Cleanup

 - `nebula -h` order in which commands are listed seems arbitrary. Change order to something like alphabetical.
 - `nebula archives` prints a `note: ...`. We're still developing the standard and have not deployed, so do not include any notes about prior unreleased versions.

 - It is possibel (although you're not supposed to) to write to an intake session that has been imported. If you then unlock and re-import that intake, the now modified session is skipped and the added files are missed. This is a real gap, although admittedly someone would have gone out of their way to misuse the intake system.

---

# Quality of life

Not urgent, nothing blocked on them, and none gets harder by waiting.

## Tag vocabulary

**Status: DEFERRED.** Raised 2026-08-01, deferred again 2026-08-12.

`tags.py` already prevents divergence at *entry* time: `input_tag()` lets a
script's prompt browse, search and TAB-complete the archive's existing tags
so the user reuses `warmup` instead of inventing `warm-up`. Nothing repairs
divergence that already happened.

Wanted, roughly in order of value:

- **Canonical tags and aliases** — declare `warmup` canonical and `warm-up`
  an alias, so old sessions stay findable without rewriting history.
- **A GUI selector.** The completion is CLI-only, so tags typed in the
  Navigator bypass the one safeguard that exists. Arguably the most
  valuable item here, since it stops *new* divergence.
- **"Never show/use tag X"** — retire a tag without deleting it.
- **"Replace X with Y"** — the actual repair. The design question is
  whether it rewrites `session.yaml` files or keeps a mapping resolved at
  read time; the second is reversible and the first is not.

Deferred because it is a quality-of-life fix on data already recorded, and
aliases can be applied retroactively — waiting costs nothing.

---

## Google-style docstrings across the repo

`src/nebula/tags.py` was converted 2026-09-02: every function there now has
a summary line, the existing prose, and `Args:` / `Returns:` / `Raises:`
sections. The rest of the package has not been touched, so the convention
is currently one file deep.

Where it stands across `src/nebula` (29 files, 809 functions/classes):

- **336 have no docstring at all** — worst offenders `api.py` (57),
  `cli.py` (44), `model.py` (25), `transfer.py` (20), `session.py` (19),
  `assets.py` (18).
- **456 more have prose but no `Args:` section**, which is the bulk of the
  work: the description usually exists and is good, it just needs the
  parameters, return value and raised exceptions broken out.

Worth keeping in mind when it's picked up:

- The prose is the valuable part and should survive. The conversion is
  *addition* — summary line, keep the existing explanation as the body,
  then the sections. Do not compress a paragraph that explains *why* into
  a one-line `Args:` entry.
- Private helpers count. `tags.py` documented `_split_tags` and
  `_format_prompt` alongside the public surface; a reader picking up a
  module cold needs those most.
- Tests were deliberately left alone — the names carry the intent and most
  of the suite has no docstrings today.
- Sensible order is by reader value, not by count: `session.py` and
  `api.py` are what a user meets first; `cli.py` is largely argparse
  wiring where a one-liner is often the honest answer.
- Nothing enforces this. If it should stay true, that is a separate
  question (a lint rule such as pydocstyle/ruff `D`) and should be decided
  once the bulk conversion is done, not before.

Not urgent: no behaviour depends on it, and it does not get harder by
waiting.

---

## Index

Larger questions live in their own documents:

- `relational-data-roadmap.md` — lineage, provenance views. Node-link
  diagram and timeline canvas are future work, parked 2026-08-12.
- `sync-roadmap.md` — backup, cloud sync and the client-server question.
  Three items are ready to build regardless of that decision: sync-conflict
  detection in `check`, a creating-machine field, and moving `index.db` out
  of the synced tree.
- `identity-trust-roadmap.md` — the owner segment of a nebula URI is
  self-asserted and unverified. Settles the identifier *shape*
  (`value@authority`, so ORCID / GitHub / email / hub ids coexist) and why
  hub names stay out of the URI. Also covers ref repair on intake import
  and the three hosting tiers. The URI surface (`nebula uri`, `ref_user` in
  the index, owner-aware resolution, the save report, the Navigator's "Get
  URI") shipped 2026-09-01, and archive ids settled URI stability under
  rename the same day. Still open: **owner** stability across a change of
  authority.
- `uri-grammar.md` — the ref grammar as it now stands: one `/`-separated
  form with a droppable prefix, and `label~id` archive segments whose id is
  a number, not a token. Both the argument and the specification.
