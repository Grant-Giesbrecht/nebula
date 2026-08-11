# Identity and ownership — roadmap

**Status: OPEN.** Raised 2026-08-03. Nothing here is built; this is the
problem written down while it is fresh, in the same spirit as
`relational-data-roadmap.md`.

Status key: **WANTED** = Grant has said yes; **OPEN** = needs a decision;
**IDEA** = worth having, not yet argued for.

---

## The problem

A nebula URI names an owner:

    nebula://grant@ncsu.edu/postdoc/S-26-0152/diode.graf

The owner segment exists because archive names are not globally unique — two
colleagues can each keep a `measurements` archive, so without an owner a
cross-archive ref is ambiguous (`refs.py` module docstring).

But **that owner is entirely self-asserted.** Today it comes from exactly two
places, neither of which checks anything:

- `~/.nebula/identity.yaml`, written by `nebula whoami --set` or the GUI
  dialog. `identity.clean_user()` validates only that the string has no
  whitespace, `/` or `|` — i.e. that it is *URI-shaped*, not that it is *true*.
- `archive.yaml`'s `user:` field, which travels with the archive and wins over
  the local identity (`config.archive_identity`).

So anyone can type `grant@ncsu.edu`, and every ref their archive emits will
claim to be Grant's. Nothing in `check`, `transfer`, or fragment import
notices. The consequence is not hypothetical once fragments circulate: a
citation in someone's paper can point at an archive whose stated owner never
touched it.

## What ownership currently protects

Worth being precise, because it is less than it looks:

- **Resolution**, not authenticity. The `user` segment is how a ref to someone
  else's archive finds a path on this machine (`registry`, `identity`
  docstrings). It is a routing key.
- Fragments preserve the *source* archive's declared name and owner
  (`config.py` — a fragment must resolve under the name its author used) so a
  citation stays valid across an export. That is a correctness property about
  *names*, and it inherits the trust problem wholesale.

## Why this is hard to bolt on later

Ownership is baked into strings already on disk: every `derived_from` with a
`user`, every collection entry, every exported fragment's `archive.yaml`.
Whatever verification arrives has to either (a) validate those strings after
the fact, or (b) add a parallel field and treat the old one as a display name.
(b) gets harder the more archives exist. This is the argument for deciding the
*shape* soon even if nothing is implemented.

---

## Identifier shape: `value@authority` — **BUILT** 2026-08-10

Implemented in `identity.py` (`parse_identity`, `validate_identity`,
`describe_identity`, `AUTHORITIES`), surfaced by `nebula whoami` and the
Navigator identity dialog, covered by `tests/test_identity.py`. Still no
verification of any kind — see option 3 and question 5.

Decided 2026-08-10. No one identity provider covers everybody: a
non-researcher or a new researcher has no ORCID, someone who has never
heard of it will not sign up for an opaque number to try a data tool, and
an instrument PC is not a person at all. So the URI must carry *several*
kinds of identity without caring which.

Read every identity as **who**, issued by **which authority**, joined by
`@`:

| Tier | Identity | How the authority vouches |
|---|---|---|
| Unverified | `grant@local` | nothing — and says so |
| Institution | `grant@ncsu.edu` | email round-trip |
| Developer | `Grant-Giesbrecht@github.com` | OAuth |
| Researcher | `0000-0003-2885-4801@orcid.org` | OAuth; MOD 11-2 offline |
| Self-certifying | `k7f3a…@key` | signature (option 3 below) |
| Hub-issued | `grant@nebulahub.org` | the hub issued it |

    nebula://0000-0003-2885-4801@orcid.org/postdoc/S-26-0152/diode.graf

**This needs no parser change.** `refs.py` already documents
`nebula://grant@ncsu.edu/postdoc/…` as the canonical URI form, and
`identity.clean_user` rejects only spaces, `/` and `|` — `@` and `.` are
already legal. What is missing is a *convention* and a validator.

Why this shape:

- **Email stops being a special case.** `grant@ncsu.edu` already *is* a
  domain-scoped identity; the domain is the authority that issued it. The
  no-ORCID-no-GitHub fallback therefore needs no new syntax.
- **Parsing is one rule: split on the last `@`.** ORCIDs, GitHub handles
  and domains contain none. An email contains exactly one and splitting it
  yields the right answer anyway.
- It is the RFC 3986 `userinfo@host` authority production, so a generic URI
  parser hands back `.username` / `.hostname` for free, and a ref stays one
  copy-pasteable token in BibTeX, an issue, or a chat message.
- It matches the fediverse `user@instance` convention people increasingly
  recognise.
- `@local` implements option 1 *in the syntax itself* — the URI admits it
  is unverified, so `check` and the GUI badge can nag without a parallel
  field. (`local` is reserved by RFC 6762, so it cannot collide with a real
  authority.)
- `format_ref` already elides the user segment for same-user refs, so the
  long form only ever appears on genuinely foreign refs — which is exactly
  where it is wanted.

### Why not bracketed authorities

The alternative considered was
`nebula://[orcid.org/0000-0003-2885-4801]/postdoc/…`. Rejected:
`nebula://[…]` is *exactly* the IPv6-literal production in the authority
position of RFC 3986, so a generic URI parser will try to read the ORCID as
an IP address and fail. It also invents a delimiter where one already fits.

## Resolution is not identity — **WANTED**

A hub name must **not** appear in the URI (i.e. not
`nebula://[nebulahub.org]/[orcid.org/…]/…`):

- It encodes *where a thing is hosted* into an identifier meant to say
  *what it is*. Move hubs and every citation ever written breaks — the same
  failure mode that rules out institutional email as a permanent identity.
- The same archive mirrored on two hubs would get two URIs, so
  `derived_from` edges pointing at one object would stop comparing equal.
- The package-manager analogy argues the same way: `pip install numpy` does
  not name `pypi.org`. The index is configuration, so pointing at a mirror
  rewrites nothing.

So: **identity in the URI, resolution in local config.** An ordered
resolver list — local paths, then known lab servers, then hubs — with
nebula caching which resolver answered. This extends the existing archive
registry rather than replacing it.

A hub still appears in the *identity* scheme as a namespace authority
(`grant@nebulahub.org`), which is the answer for someone with no ORCID, no
institutional email and no GitHub. Two jobs, one hub; only the second
belongs in the URI.

## What a hub can and cannot enforce — **IDEA**

The distinction worth holding onto: **authenticating the author is
tractable; authenticating the assertion is not.**

A hub *can* prove that whoever pushed an archive controls the identity it
claims, and each tier above has an off-the-shelf flow — ORCID and GitHub
both offer free OAuth, `.edu` by email round-trip, its own namespace by
construction. It can also check that a cited target resolves.

It *cannot* know whether file X really derives from person Y, or whether Y
exists but has never been seen on that hub. Same as GitHub having no idea
whether a README is true. The useful feature there is not verification but
**inbound citation visibility**: let the cited party see who claims to
derive from them, and dispute it.

## Options, roughly in order of cost

### 1. Honest labelling — **BUILT** 2026-08-10

Stop implying verification we do not do. Show self-asserted identity as
self-asserted: the GUI badge and `nebula whoami` say "unverified", and an
imported fragment displays its owner as *claimed by the archive*. Costs
nothing, prevents the worst misreading, and does not constrain later options.

Done:

- `Identity.verified` is a single property every display path reads, never
  an assumption spread across call sites.
- `nebula whoami` prints authority and status; the Navigator badge turns
  amber with a `local` marker when the identity names no authority.
- `check` reports `unqualified_owner` / `no_owner` at info level. Info,
  never error: a bare name is a valid way to work alone. The wording says
  nothing about verification, because reporting only the unqualified ones
  would imply the rest had been checked.
- `identity.describe_owner` adds the one thing `describe_identity` cannot
  know — whether an owner is *someone else's assertion that arrived with
  the data*. `config.archive_identity` carries it as `user_claimed` /
  `user_display` / `user_note`, so the CLI, the Navigator and `transfer`
  all say the same thing about a name nothing has checked.
- A transfer plan carries `source_owner`, so the moment you decide to take
  someone's data in is the moment you are told the name attached to it is
  unverified. Shown before the session list, since that list can be long.

An owner counts as *claimed* when the archive declares one and it is not
this machine's user. A fragment you exported yourself is therefore not
labelled — you did assert it.

### 2. Trust-on-first-use registry — **IDEA**

When an archive from a new owner is first registered or a fragment imported,
record the (owner, archive) pair locally. Warn when a later import claims the
same owner with a different fingerprint. Catches accident and casual
impersonation; catches nothing deliberate. Local-only, no infrastructure.

### 3. Signed archives / fragments — **OPEN**

A keypair per user; `archive.yaml` carries a public key, and an exported
fragment is signed over its manifest. `check` verifies. Real authenticity,
and the first thing here that survives an adversary. Costs: key management,
key distribution, key loss, and a signature that must be recomputed whenever
a sealed thing legitimately changes. Probably too much for a lab tool unless
fragments start circulating widely.

### 4. Institutional identity — **IDEA**

Delegate to something that already verifies people (ORCID, an institutional
SSO, a DOI-issuing service). Fits the actual use case — these are academic
artefacts and the citation audience already trusts ORCID — and avoids
inventing a PKI. Needs network access at some point, which every other part
of nebula deliberately avoids.

Under `value@authority` this is no longer an all-or-nothing choice: each
authority in the tier table is a separate, independently addable
verification, and none of them is a precondition for using nebula. ORCID
becomes an *upgrade* attached when someone publishes, not an entry fee.

---

## Ref repair on intake import — **WANTED**

Raised 2026-08-10. Refs written by hand while fleshing out an intake
archive will contain typos. Offer to fix them at merge time, when the
archive is being reviewed anyway.

**Integration note:** intake merge *already* rewrites refs, because
provisional `I-` ids are reallocated to permanent `S-` ids on import.
Repair must ride that same pass. Two independent things rewriting refs with
different notions of correctness is how a merge corrupts provenance.

Shape: `check` already emits `dangling_ref` / `dangling_asset_ref`. Merge
collects those, proposes candidates by edit distance over known ids, and
asks for confirmation.

**Limit, stated plainly:** this only catches refs that *fail* to resolve. A
typo that happens to land on a real, different session is undetectable.
That is the argument for check digits on `S-` / `AF-` ids — noted, not
proposed; ORCID has one for exactly this reason.

## Hosting tiers, and the test they impose — **IDEA**

Three intended ways to run nebula. The design test is that **moving between
them must not rewrite a single URI already written.**

**A. Local only.** Archive on a laptop or flash drive, backed up with borg
or restic, integrity entirely the user's responsibility. Nothing networked,
ever. Their real risk is a silently corrupt restore, so this is where
"break loudly" cashes out: `check` must be strong enough that verifying a
restore is one command. Identity is settable offline — the ORCID checksum
earns its keep here.

**B. Self-hosted server.** A lab server as single point of truth, several
machines pushing and pulling. Needs authenticated push, a
fast-forward-or-reject rule rather than a silent merge, and `index.db`
moved out of the synced tree. This is where the open sync questions live.

**C. Hub.** Public resolution, namespace issuance, and OAuth verification
of foreign namespaces. Notably it needs no URI format that B did not
already need.

Checking the proposal against these: identity stays `0000-…@orcid.org`
throughout, and only the resolver list grows (local path → + lab server →
+ hub). No ref changes. That is the payoff for keeping the hub out of the
URI, and the property to hold the whole design to.

## Questions to settle before building anything

1. **What is the threat?** Casual mistake (two people, one shared machine,
   forgot to set the name) or actual impersonation? Only the first is worth
   solving today, and it is solved by option 1 plus a nag.
2. **Does verification belong to the *archive* or the *fragment*?** An archive
   is edited constantly and cannot carry a stable signature cheaply. A
   fragment is a frozen export — signing that is tractable.
3. **What happens to unverified archives?** They must keep working. Anything
   that refuses to open an archive with no verified owner makes nebula useless
   for the person who just wants to save a CSV.
4. ~~**Is the owner segment even the right identifier?**~~ *Settled
   2026-08-10:* yes, kept, but restructured as `value@authority` above. The
   `AF-` parallel still holds — the authority is the stable part and the
   local label is display — but a wholly opaque owner id was rejected as
   unreadable in a citation.
5. **Does anyone need to verify *who* made a fragment, or only to tell two
   people apart?** This decides whether option 3 (keys) is worth building.
   Telling people apart is satisfied by `value@authority` alone. If real
   verification matters even slightly, generate keys early — retrofitting
   identity onto refs already written is the expensive path.
6. **What happens when someone changes authority?** A succession record
   ("this identity supersedes that one") is needed under *every* option
   here, including the do-nothing one, because people move institutions and
   acquire ORCIDs late. It is what makes upgrading a tier possible without
   breaking old refs.

## Nearest-term step

Options 1 and the `value@authority` convention are both complete — see the
BUILT markers above. Nothing further here is scheduled.

The next self-contained piece is **ref repair on intake import**, which is
independent of everything else in this document.

Keys, hub, and tiers B and C still wait for real answers to questions 1
and 5. Until those are answered, the honest position is the one now
implemented: nebula knows *what* an identity claims to be and *who*
claimed it, and checks neither.
