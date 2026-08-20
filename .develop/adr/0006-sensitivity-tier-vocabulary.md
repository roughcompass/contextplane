# 0006 — One closed handling scale at the bottom layer; ARC's two stay separate

**Status:** Accepted 2026-08-19

## Context

An Autonomy Envelope is scoped by data sensitivity, so the envelope work needs a
sensitivity vocabulary it can name. The tree has four lists of tier names, and
they disagree.

**Vocabulary A — the handling scale.** `public, internal, confidential,
restricted`, canonically at `contextplane/context/schemas/trust.py`. It is
defined as a `Literal` and a `frozenset`, written out twice with no derivation
between them. The **order** is not in the definition at all — a frozenset has
none — so it is re-declared as a module-level tuple in three more places:
`contextplane/context/evaluation/judge.py` (public, re-exported, imported across
modules), `contextplane/sharing/authorization.py` (private), and
`contextplane/workspaces/recall.py` (private). The names are re-typed once more
as a Pydantic `Literal` in `contextplane/api/schemas/signals.py`, in a file that
already imports from `trust.py` and could have derived it, and four more times as
raw SQL in migrations feeding five CHECK constraints. Nothing in `tests/`
asserts any of these agree.

**Vocabulary B — ARC's `content_classification`.** A different four-member scale
whose top member is `regulated`, not `restricted`, in
`contextplane/arc/vocabularies.py`. It has a DB CHECK generated from the Python
constant, and a second cross-column CHECK giving `regulated` operational meaning
(it requires encrypted storage). Its own section comment calls it "ordered from
least to most restricted" while shipping a frozenset, so the order it claims
exists nowhere in the program. Its wire surface closes the field at **three**
values, silently dropping `regulated` — the tier the database has a dedicated
encryption constraint for cannot be submitted through the authoring profile.

**Vocabulary C — ARC's `data_sensitivity`.** Deliberately open: a bare string,
length-bounded on the wire, no CHECK on `arc_applicability_rules.data_sensitivity_tiers`,
and the code says so in prose. Used only as unordered set membership and
equality; nothing in ARC ranks two sensitivity values. It is also mirrored into
the host's signed attestation payload and hashed into the manifest-claims digest.

**Vocabulary D — a three-member fallback.** `_DEFAULT_TIERS = ("public",
"internal", "restricted")` in `contextplane/arc/service/replay_corpus.py`,
agreeing with nothing.

The plan framed this as a placement problem: pick between the bottom layer beside
`ranking` and the existing `config_grammar` layer. Placement is the smaller half.
The larger half is that three of the four consumers that cannot reach `trust.py`
did not work around the layering — they grew their own copy, and one of them grew
a whole second scale. The layer contract is 22 deep with `exhaustive = true`, and
the consumers that matter sit well below `context` (layer 6): `service` is 9,
`sharing` is 13, `retention` is 15, `arc` is 7.

One thing the duplication is *not* is a latent equality bug. The three order
tuples hold an identical literal. What they do not share is the rule for a label
that is not in the list: `judge.py` and `recall.py` rank an unreadable label as
most restrictive, and `sharing/authorization.py` refuses to rank it at all. A
shared constant unifies the names and the order and leaves that disagreement
exactly where it is.

## Decision

**Vocabulary A is closed, ordered, and moves to a new bottom-layer module,
`contextplane/sensitivity.py`, declared as a `|` sibling of `ranking`.**

The placement was verified rather than reasoned about. With the module present,
`layers` line changed to `"ranking | sensitivity"`, and one entry added to the
ARC front-door contract's `source_modules`, `lint-imports` reports 3 contracts
kept and 0 broken over 493 files. With probe imports added from
`contextplane/sharing/authorization.py` (layer 13) and
`contextplane/arc/vocabularies.py` (layer 7) — the two consumers furthest apart —
the contract still reports 3 kept, 0 broken. Both edits were reverted; what
remains is the fact that the placement works and costs exactly two lines of
`pyproject.toml`.

**The order lives in the value.** The module exports an ordered tuple, and
`CLASSIFICATIONS`-style set membership is derived from it rather than written out
beside it. The present shape — a `Literal`, a `frozenset` written out again, and
three private tuples restating the order — is four statements of one fact, and
the order is the one a reader cannot get from the canonical definition.

**The module refuses to rank a name it does not know, and does not decide what a
caller should do about that.** `rank()` raises. The two call sites that today
treat an unreadable label as most restrictive keep doing so, at the call site,
where it is visible; the one that refuses to rank keeps refusing. Folding either
rule into the vocabulary would change a security-relevant decision in two or three
places as a side effect of moving a constant, and the two rules are both
defensible — "unknown means treat as the most sensitive thing" and "unknown means
I cannot answer" are different correct answers to different questions.

**`config_grammar` is rejected as a home, and so is adding to its file.** Its
docstring scopes it to "strings an operator writes, values `Settings` holds", and
it has already turned away one module for not fitting. Putting handling tiers
there would also make them read as operator-configurable, which is the exact
escape hatch ARC's vocabulary module was written to close. As a *layer* it is
also strictly worse: `config_grammar` sits at 21 with `exceptions`, `metrics` and
`types` as `|` siblings, and siblings are mutually unreachable — so the new module
would still be unable to import `contextplane.exceptions`, while additionally
being unreachable from four modules. Identical cost, less reach.

**The module defines its own error type rather than raising `RegistryError`.**
`exceptions` is a layer above the bottom, so it is unreachable — the same
constraint `contextplane/ranking.py` records having hit, and the same answer.
This is a known and accepted price of bottom-layer placement, recorded in
ADR-0004 as well.

**Vocabulary B is not merged into A.** ARC's `regulated` is not a spelling
variant of `restricted`: it has a cross-column CHECK requiring encrypted storage
that `restricted` does not have. Merging means a breaking change on
`SignalReference.classification` and its siblings, a data migration for stored
`restricted` rows, and giving the encryption rule reach beyond ARC — three
decisions, none of which this ADR was asked to make. The two scales stay
separate, each named in its own module, and the ADR records the reason so the
next reader does not rediscover the question.

The wire/DB disagreement inside Vocabulary B — the authoring profile closes the
field at three values while the database has four — is a defect this ADR
surfaces and does not fix. It is filed as its own work because it is a wire
contract change with an `openapi.json` drift gate behind it.

**Vocabulary C stays open, deliberately, and this is recorded as a decision
rather than a gap.** `data_sensitivity` is mirrored into the host's signed
attestation payload and hashed into the manifest-claims digest, so closing it
turns every host sending an unlisted value into `403 blocked_manifest_unverified`
rather than a validation error. It is also used purely as set membership;
imposing an order on it would give ranking semantics to a dimension nothing
ranks. An envelope scoped by sensitivity therefore scopes by **Vocabulary A**,
which is closed, and not by ARC's manifest field, which is not.

**Vocabulary D is deleted.** `_DEFAULT_TIERS` is a three-member corpus-generation
fallback that agrees with no other list in the tree; it either uses the closed
scale or the corpus is generated over tiers that do not exist. The supersession
rule applies — it goes in the same change that lands the module.

**A conformance test asserts the module against the five DB CHECK constraints**,
in the shape of `tests/conformance/test_arc_closed_vocabularies.py`, which does
this for ARC's two columns. Nothing in `tests/` asserts anything about
Vocabulary A today, in any of its six Python spellings or five SQL ones. The
gate is the reason the consolidation is worth doing at all: without it, one
module replaces six copies and nothing stops a seventh.

## Assumptions

1. **No consumer needs to raise `RegistryError` from the vocabulary.** If one
   does, the bottom-layer placement is wrong and the module belongs above
   `exceptions` with a narrower consumer set. `ranking.py` has lived with this
   for its whole life, which is the evidence such consumers are rare.
2. **The three unknown-label rules stay as they are.** If a future reader
   converges them, that is a behaviour change to a redaction decision and a
   cross-org sharing ceiling, and it needs its own record.
3. **Nothing currently depends on the four copies being able to drift.** They hold
   identical literals today, so the consolidation is behaviour-preserving. This
   was checked by reading the four tuples, not by a test — there is no test.
4. **The tenant vocabulary mechanism stays rejected for tiers.** ARC's module
   records the reason: a tenant able to add its own tier could define one weaker
   than the handling rules assume. One of the two originally recorded grounds no
   longer holds — global rows are representable now — and the surviving ground is
   sufficient.

## Alternatives rejected

**Leave placement alone; declare `trust.py` canonical and accept the copies.**
The cheapest option, and honest that nothing is broken at runtime today. Rejected
because the copies are the mechanism by which this got to four vocabularies: each
one was a reasonable local response to a module being unreachable, and the fourth
was a whole second scale. Cost accepted: two lines of `pyproject.toml` and a move.

**Add the constants to the existing `config_grammar.py` file.** Rejected on the
file's own stated scope, and because it would make the tiers read as operator
configuration.

**A new layer 23 below `ranking` rather than a sibling.** Would let `ranking`
import the vocabulary. Nothing wants that, and a layer whose only property is
"even lower" invites the next module to go lower still. Sibling placement says
the two are independent, which is true.

**Adopt `regulated` as the top tier and merge to one four-member scale.** Buys
one scale and extends the encryption rule beyond ARC. Rejected as out of scope: a
breaking wire change plus a data migration, decided as a side effect of an
envelope-scoping question.

**Close `data_sensitivity` against the chosen tier set.** Rejected: a hard break
on attested hosts, delivered as a `403` on a signed payload rather than a
validation error, in exchange for closing a dimension nothing ranks.

## Consequences

Six Python spellings of the handling scale become one, and the order stops being
a private tuple three modules maintain in parallel. A seventh copy now has a
place to not be.

The repository ships **three** sensitivity vocabularies after this ADR — the
closed handling scale, ARC's closed content classification, and ARC's open
manifest sensitivity — where a reader expects one. That is stated out loud here
rather than left to be discovered, and each has its reason recorded in its own
module.

The new module cannot use this codebase's error type, cannot import
`contextplane.types`, and is the second module in the tree with that constraint.
It also has to be covered by tests or carry a reasoned coverage exemption, since
`coverage-exemptions` and `test-coverage` are required gates.

Its docstring cannot cite this ADR by number: `scripts/check_no_doc_refs.py`
forbids `ADR-\d+` in shipped code. The reasoning goes in the module in its own
words, which is the rule this repository already applies everywhere else.

A defect is now on the record and not fixed: ARC's authoring profile cannot
submit the one content classification the database has an encryption rule for.

## Dissent

*On not merging A and B.* A reviewer could reasonably say that shipping two
four-member sensitivity scales whose top members are near-synonyms is the actual
problem, and that an ADR which closes one and leaves the other beside it has
tidied the smaller mess. The counter is scope: merging is a wire break plus a
migration plus a decision about whether encryption-at-rest follows `restricted`
data outside ARC, and bundling those into a placement decision is how a
three-line change becomes unreviewable. The objection stands on its merits, and
the merge should be its own ADR with those three questions asked directly.

*On leaving the unknown-label rules divergent.* Two of three sites treat an
unreadable classification as most restrictive; the third refuses to answer. That
is a difference in a security decision, sitting in code that a reader would
reasonably assume behaves the same way. Converging them was declined here to keep
this ADR behaviour-preserving, but "we consolidated the vocabulary and left the
semantics inconsistent" is a defensible criticism, and the divergence is now
easier to see and therefore easier to argue about.

*On the bottom layer at all.* One view is that a domain vocabulary at the bottom
of the import graph is a smell — the bottom layer should hold mechanism, not
meaning — and that the real finding is that `sharing` and `retention` making
classification decisions seven layers below where classification is defined is the
layering being wrong, not the constant being misplaced. That is probably true, and
it is a much larger change than this ADR. Recorded so the next person to look at
the layer list knows the question was asked.
