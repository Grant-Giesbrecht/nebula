# TODO

Small, concrete work items. Anything needing a design argument gets its own
roadmap document instead — see the index at the bottom.

Format: one heading per item, with enough context that it can be picked up
cold. Delete an item when it ships.

---

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
  and the three hosting tiers. Nearest-term step is adopting the convention
  plus honest labelling.
