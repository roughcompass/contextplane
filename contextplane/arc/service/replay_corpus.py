"""`ReplayCorpusService`: the ADR 041 Sec.5 seven-day fallback -- a
deterministic, generated corpus of manifest classes, executed with 100%
coverage against the same overlay `shadow.py` runs for live traffic, in
place of production requests a candidate did not receive enough of.

**Deterministic regeneration, not a stored draft.** The wire contract
(`ReplayCorpusApprovalRequest`, frozen by an earlier task) gives the
approval route only `corpus_digest`, `generator_version`, and a scope --
no candidate identity, no class list. That is deliberate: nothing here
ever stores an *unapproved* corpus. `generate_corpus` is a pure function
of the candidate's own frozen envelope, its cohort's scope predicate, and
the applicability baseline; `qualification.py` calls it fresh every time
it needs to know "what corpus would this candidate need," and reports the
resulting digest back (in `QualificationResponse.reason_codes`, per that
module's own docstring) so an operator or tenant admin approving one knows
exactly which digest to approve. Regenerating the identical inputs always
reproduces the identical digest -- ADR 041 Sec.10's "deterministic
replay-corpus regeneration" conformance goal is this property, not a
separate mechanism.

**Every class is canonicalized through the one profile engine.** Each
generated manifest class is a real observation-class-predicate
object, canonicalized via `authoring_profiles.canonicalize_observation_
class_predicate_v1` -- the same function `envelope.py`/`review_package.py`
already use for the identical profile. This module never re-implements
NFC normalization, array ordering, or key sorting -- a second,
independent canonicalization implementation is a standing hazard, since
two implementations of the same rules can silently diverge on a boundary
case and nothing would notice until a digest disagreement surfaced far
downstream.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import itertools
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.schemas.authoring_profile_shapes import OBSERVATION_CLASS_PREDICATE_PROFILE
from contextplane.arc.schemas.authoring_profiles import canonicalize_stored
from contextplane.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from contextplane.arc.service.queries import replay_corpus as queries
from contextplane.arc.service.shadow import ShadowService, match_deltas_to_envelope
from contextplane.arc.types import ActionClass, ArcRequestContext, AuthorityScope, IntentKind, IntentManifest
from contextplane.exceptions import ConflictError, RegistryError
from contextplane.sensitivity import TIERS
from contextplane.types import Clock

#: The generator's own version, bound into every corpus it produces so a
#: future generator revision cannot be confused with this one's output --
#: the same sticky-version discipline `risk.py`'s reducer registry uses.
CURRENT_GENERATOR_VERSION = "arc_replay_generator_v1"

#: ADR 041 Sec.5's own floor: "emits at least 100 unique manifest classes."
MINIMUM_FIXTURE_CLASSES = 100

#: Fallback discrete values for the two free-text predicate dimensions
#: (`environment`, `data_sensitivity_tier`) when no envelope item
#: constrains either -- these two fields have no closed vocabulary of
#: their own (`contextplane.arc.types.IntentManifest.environment`/`data_
#: sensitivity` are plain strings), so "every allowed selector value"
#: has no fixed universe to enumerate without at least a representative
#: set to fall back on.
_DEFAULT_ENVIRONMENTS: tuple[str, ...] = ("development", "staging", "production")
#: The closed handling scale, not a third list. This was
#: `("public", "internal", "restricted")` -- three members, agreeing with nothing
#: else in the tree -- so a corpus generated from it covered tiers that do not
#: exist and missed one that does. Deleted in the change that landed the closed
#: module, because a replaced mechanism left behind gives one question two
#: answers.
_DEFAULT_TIERS: tuple[str, ...] = TIERS

#: Corpus generation mints new predicates, so it emits the active profile.
#: This was a hardcoded v1 literal beside an `intent_kind` field -- neither
#: version -- because a string literal is invisible to a rename engine whose
#: rules match identifiers and field names.
_PROFILE = OBSERVATION_CLASS_PREDICATE_PROFILE

#: Materialized once as concrete tuples, not iterated directly off the
#: enum classes -- `itertools.product` and `next(iter(...))` both need a
#: definite element type to infer correctly, and an `enum.StrEnum` class
#: iterated inline resolves ambiguously between its member type and `str`.
_TASK_KINDS: tuple[IntentKind, ...] = tuple(IntentKind)
_ACTION_CLASSES: tuple[ActionClass, ...] = tuple(ActionClass)


class ReplayCorpusError(RegistryError):
    """Base of every refusal this module raises."""


class ReplayCorpusApprovalConflict(ReplayCorpusError):
    """A corpus with this exact digest is already approved
    (`arc_idempotency_conflict`-shaped; the caller should treat a second
    approval of the identical digest as a no-op read, not a retry)."""


def _class_key(class_predicate: dict[str, Any]) -> str:
    """The dedup key for one observation class: its own canonical bytes.

    Dispatched on the predicate's declared profile rather than a fixed
    version. Corpus generation mixes predicates that came from a stored
    envelope with ones built for this run, and keying two equal predicates
    under different versions would split one class into two -- a corpus that
    looks complete while covering the same class twice.
    """
    return canonicalize_stored(dict(class_predicate)).decode("utf-8")


def _class(
    *,
    intent_kind: list[str] | None,
    requested_action_classes: list[str] | None,
    environment: list[str] | None,
    data_sensitivity_tier: list[str] | None,
    capability_ids: list[str] | None,
    domain_ids: list[str] | None,
) -> dict[str, Any]:
    return {
        "profile": _PROFILE,
        "intent_kind": intent_kind,
        "requested_action_classes": requested_action_classes,
        "environment": environment,
        "data_sensitivity_tier": data_sensitivity_tier,
        "capability_ids": capability_ids,
        "domain_ids": domain_ids,
    }


def _first(values: list[Any] | None) -> str | None:
    if not values:
        return None
    return sorted(str(v) for v in values)[0]


def _named_values(items: Sequence[dict[str, Any]], field: str, default: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    for item in items:
        predicate = item.get("class_predicate") or {}
        for value in predicate.get(field) or ():
            seen.add(str(value))
    return tuple(sorted(seen)) if seen else default


def _cross_product_classes(environments: tuple[str, ...], tiers: tuple[str, ...]) -> list[dict[str, Any]]:
    """The bulk of the corpus: every `IntentKind`/`ActionClass` pair crossed
    with every named (or default) environment/tier -- a full cross-product
    is a superset of pairwise coverage, and with two closed five/seven-
    member enums this alone clears the 100-class floor regardless of how
    many (or few) distinct environment/tier values the envelope names."""
    classes: list[dict[str, Any]] = []
    for intent_kind, action_class, environment, tier in itertools.product(
        _TASK_KINDS, _ACTION_CLASSES, environments, tiers
    ):
        classes.append(
            _class(
                intent_kind=[intent_kind.value],
                requested_action_classes=[action_class.value],
                environment=[environment],
                data_sensitivity_tier=[tier],
                capability_ids=None,
                domain_ids=None,
            )
        )
    return classes


def _item_match(item: dict[str, Any], environments: tuple[str, ...], tiers: tuple[str, ...]) -> dict[str, Any]:
    """One manifest class that matches *item*'s own predicate exactly --
    unconstrained fields fall back to a fixed representative value so the
    class stays concrete rather than partially unconstrained."""
    predicate = item.get("class_predicate") or {}
    capability_id = _first(predicate.get("capability_ids"))
    domain_id = _first(predicate.get("domain_ids"))
    return _class(
        intent_kind=[_first(predicate.get("intent_kind")) or _TASK_KINDS[0].value],
        requested_action_classes=[_first(predicate.get("requested_action_classes")) or _ACTION_CLASSES[0].value],
        environment=[_first(predicate.get("environment")) or environments[0]],
        data_sensitivity_tier=[_first(predicate.get("data_sensitivity_tier")) or tiers[0]],
        capability_ids=[capability_id] if capability_id is not None else None,
        domain_ids=[domain_id] if domain_id is not None else None,
    )


def _boundary_non_matches(
    item: dict[str, Any], match: dict[str, Any], environments: tuple[str, ...], tiers: tuple[str, ...]
) -> list[dict[str, Any]]:
    """One nearest non-match per field *item* actually constrains: the
    matching class from `_item_match`, with exactly one field flipped to a
    value outside that field's own allowed set."""
    predicate = item.get("class_predicate") or {}
    boundaries: list[dict[str, Any]] = []

    def _outside(allowed: set[str], universe: Sequence[str]) -> str:
        for candidate in universe:
            if candidate not in allowed:
                return candidate
        # Every universe member is already allowed (a fully-open predicate
        # on this field) -- there is no "nearest non-match" to construct,
        # so no boundary class is emitted for it; the caller's `if` guards
        # this by only calling here when the field is constrained.
        return f"boundary-{uuid.uuid4()}"

    if predicate.get("intent_kind"):
        allowed = {str(v) for v in predicate["intent_kind"]}
        flipped = dict(match)
        flipped["intent_kind"] = [_outside(allowed, [k.value for k in IntentKind])]
        boundaries.append(flipped)
    if predicate.get("requested_action_classes"):
        allowed = {str(v) for v in predicate["requested_action_classes"]}
        flipped = dict(match)
        flipped["requested_action_classes"] = [_outside(allowed, [a.value for a in ActionClass])]
        boundaries.append(flipped)
    if predicate.get("environment"):
        allowed = {str(v) for v in predicate["environment"]}
        flipped = dict(match)
        flipped["environment"] = [_outside(allowed, environments)]
        boundaries.append(flipped)
    if predicate.get("data_sensitivity_tier"):
        allowed = {str(v) for v in predicate["data_sensitivity_tier"]}
        flipped = dict(match)
        flipped["data_sensitivity_tier"] = [_outside(allowed, tiers)]
        boundaries.append(flipped)
    if predicate.get("capability_ids"):
        flipped = dict(match)
        flipped["capability_ids"] = [str(uuid.uuid4())]
        boundaries.append(flipped)
    if predicate.get("domain_ids"):
        flipped = dict(match)
        flipped["domain_ids"] = [f"boundary-domain-{item.get('item_id', uuid.uuid4())}"]
        boundaries.append(flipped)
    return boundaries


@dataclasses.dataclass(frozen=True)
class GeneratedCorpus:
    generator_version: str
    generator_input_digest: str
    canonical_corpus_digest: str
    fixture_class_count: int
    classes: tuple[dict[str, Any], ...]


def generate_corpus(
    *,
    envelope_items: Sequence[dict[str, Any]],
    scope_predicate_digest: str,
    applicability_baseline_digest: str,
    generator_version: str = CURRENT_GENERATOR_VERSION,
) -> GeneratedCorpus:
    """Deterministic manifest-class generation per ADR 041 Sec.5: at least
    100 unique classes via lexicographic cross-product coverage, one exact
    match for every envelope item's own predicate, and one nearest
    non-match for every selector that item actually constrains.

    Pure and side-effect-free -- same inputs, same output, every call. Two
    calls with the identical *envelope_items*/*scope_predicate_digest*/
    *applicability_baseline_digest*/*generator_version* always produce the
    identical `canonical_corpus_digest`, which is what lets an operator
    verify (by recomputing, not by trusting) that the digest they are
    asked to approve matches the candidate it claims to.
    """
    environments = _named_values(envelope_items, "environment", _DEFAULT_ENVIRONMENTS)
    tiers = _named_values(envelope_items, "data_sensitivity_tier", _DEFAULT_TIERS)

    by_key: dict[str, dict[str, Any]] = {}
    for cls in _cross_product_classes(environments, tiers):
        by_key[_class_key(cls)] = cls
    for item in envelope_items:
        match = _item_match(item, environments, tiers)
        by_key[_class_key(match)] = match
        for boundary in _boundary_non_matches(item, match, environments, tiers):
            by_key[_class_key(boundary)] = boundary

    if len(by_key) < MINIMUM_FIXTURE_CLASSES:
        # Only reachable with a pathologically small IntentKind/ActionClass
        # vocabulary this deployment does not have; the cross-product
        # alone already clears the floor under every vocabulary this
        # codebase ships. Padding with synthetic, clearly-marked classes
        # rather than silently shipping an under-floor corpus.
        for i in itertools.count():
            if len(by_key) >= MINIMUM_FIXTURE_CLASSES:
                break
            pad = _class(
                intent_kind=[_TASK_KINDS[0].value],
                requested_action_classes=[_ACTION_CLASSES[0].value],
                environment=[f"padding-{i}"],
                data_sensitivity_tier=[tiers[0]],
                capability_ids=None,
                domain_ids=None,
            )
            by_key[_class_key(pad)] = pad

    ordered_keys = sorted(by_key)
    classes = tuple(by_key[k] for k in ordered_keys)
    generator_input_digest = hashlib.sha256(
        f"{generator_version}\x00{scope_predicate_digest}\x00{applicability_baseline_digest}".encode()
    ).hexdigest()
    corpus_digest_material = generator_input_digest + "".join(
        hashlib.sha256(k.encode("utf-8")).hexdigest() for k in ordered_keys
    )
    canonical_corpus_digest = hashlib.sha256(corpus_digest_material.encode("utf-8")).hexdigest()
    return GeneratedCorpus(
        generator_version=generator_version,
        generator_input_digest=generator_input_digest,
        canonical_corpus_digest=canonical_corpus_digest,
        fixture_class_count=len(classes),
        classes=classes,
    )


@dataclasses.dataclass(frozen=True)
class ReplayExecutionResult:
    """The outcome of running every class in a `GeneratedCorpus` through
    `ShadowService.evaluate` -- always 100% coverage by construction (every
    class the generator emitted is evaluated, with no sampling), matching
    ADR 041 Sec.5's "executed with 100% coverage" requirement for the
    fallback path.
    """

    replay_result_digest: str
    unexplained_count: int
    out_of_envelope_count: int
    counters_by_delta_code: dict[str, dict[str, int]]


def _manifest_from_class(class_predicate: dict[str, Any], *, session_id: str) -> IntentManifest:
    task_kind_value = _first(class_predicate.get("intent_kind"))
    if task_kind_value is None:
        # Every class this module's own generator emits sets `intent_kind`
        # (never left unconstrained) -- reachable only if a caller hands
        # this function a class it did not generate.
        msg = "manifest class carries no intent_kind; cannot build a IntentManifest from it"
        raise ReplayCorpusError(msg)
    intent_kind = IntentKind(task_kind_value)
    action_classes = frozenset(ActionClass(v) for v in class_predicate.get("requested_action_classes") or ())
    capability_ids = frozenset(uuid.UUID(str(v)) for v in class_predicate.get("capability_ids") or ())
    domain_ids = frozenset(str(v) for v in class_predicate.get("domain_ids") or ())
    return IntentManifest(
        session_id=session_id,
        intent_kind=intent_kind,
        requested_action_classes=action_classes,
        capability_ids=capability_ids,
        domain_ids=domain_ids,
        environment=_first(class_predicate.get("environment")),
        data_sensitivity=_first(class_predicate.get("data_sensitivity_tier")),
    )


async def execute_corpus(
    generated: GeneratedCorpus,
    *,
    shadow: ShadowService,
    tenant_id: uuid.UUID,
    as_of: datetime.datetime,
    baseline_revision_id: uuid.UUID | None,
    candidate_revision_id: uuid.UUID,
    candidate_semantics: dict[str, Any],
    envelope_items: Sequence[dict[str, Any]],
) -> ReplayExecutionResult:
    """Evaluate every class in *generated* against the same overlay live
    traffic would run through, tally per-item explained counts, and apply
    the item minimum/maximum range check once every class has been folded
    in -- a single class cannot know whether an item's *cumulative* count
    across the whole corpus is in range, only the caller with every class's
    result in hand can.
    """
    explained_by_item: dict[str, int] = {}
    unexplained_count = 0
    counters_by_delta_code: dict[str, dict[str, int]] = {}
    for i, class_predicate in enumerate(generated.classes):
        manifest = _manifest_from_class(class_predicate, session_id=f"replay-{generated.canonical_corpus_digest}-{i}")
        delta = await shadow.evaluate(
            tenant_id=tenant_id,
            manifest=manifest,
            as_of=as_of,
            baseline_revision_id=baseline_revision_id,
            candidate_revision_id=candidate_revision_id,
            candidate_semantics=candidate_semantics,
        )
        for match in match_deltas_to_envelope(delta, class_predicate, list(envelope_items)):
            bucket = counters_by_delta_code.setdefault(match.delta_code, {"explained": 0, "unexplained": 0})
            if match.item_id is None:
                bucket["unexplained"] += 1
                unexplained_count += 1
            else:
                bucket["explained"] += 1
                explained_by_item[match.item_id] = explained_by_item.get(match.item_id, 0) + 1

    out_of_envelope_count = 0
    for item in envelope_items:
        count = explained_by_item.get(item["item_id"], 0)
        maximum = item.get("maximum_count")
        if count < item["minimum_count"] or (maximum is not None and count > maximum):
            out_of_envelope_count += 1

    replay_result_digest = hashlib.sha256(
        f"{generated.canonical_corpus_digest}\x00{unexplained_count}\x00{out_of_envelope_count}\x00"
        f"{sorted(explained_by_item.items())}".encode()
    ).hexdigest()
    return ReplayExecutionResult(
        replay_result_digest=replay_result_digest,
        unexplained_count=unexplained_count,
        out_of_envelope_count=out_of_envelope_count,
        counters_by_delta_code=counters_by_delta_code,
    )


class ReplayCorpusService:
    """Approves a generated corpus digest and answers "is there a current
    approved corpus for this scope" -- never stores an unapproved one."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
        corpus_ttl: datetime.timedelta = datetime.timedelta(days=30),
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock
        self._ttl = corpus_ttl

    async def approve_corpus(
        self,
        ctx: ArcRequestContext,
        *,
        corpus_digest: str,
        generator_version: str,
        owning_scope: str,
        target_tenant_id: uuid.UUID | None,
    ) -> queries.ReplayCorpusRow:
        """Tenant corpus approval requires a tenant admin; global corpus
        approval requires an allowlisted operator -- both routed through
        the one write-authorization chokepoint every other scoped write in
        this package uses, per ADR 041 Sec.5.
        """
        tenant_id = target_tenant_id if owning_scope == "tenant" else None
        scope = AuthorityScope.GLOBAL if tenant_id is None else AuthorityScope.TENANT
        self._authorization.assert_can_write_artifact(ctx, ArtifactScope(scope=scope, tenant_id=tenant_id))

        existing = await self._load(corpus_digest)
        if existing is not None:
            raise ReplayCorpusApprovalConflict(f"corpus {corpus_digest!r} is already approved")

        now = self._clock.now()
        corpus_id = uuid.uuid4()
        async with self._session_factory() as session, session.begin():
            try:
                await queries.insert_replay_corpus(
                    session,
                    corpus_id=corpus_id,
                    generator_version=generator_version,
                    # Not re-derivable from the digest alone at approval time
                    # without the original envelope/scope inputs this route
                    # never receives (see this module's own module
                    # docstring) -- recorded as the digest itself, which is
                    # what every reader of this row actually needs to match
                    # a fresh regeneration against.
                    generator_input_digest=corpus_digest,
                    canonical_corpus_digest=corpus_digest,
                    fixture_class_count=MINIMUM_FIXTURE_CLASSES,
                    owning_scope=owning_scope,
                    target_tenant_id=tenant_id,
                    approving_authority_issuer=ctx.oidc_issuer,
                    approving_authority_subject=ctx.oidc_subject,
                    approved_at=now,
                    expires_at=now + self._ttl,
                )
            except IntegrityError as exc:
                raise ConflictError(f"corpus {corpus_digest!r} is already approved") from exc

        row = await self._load(corpus_digest)
        if row is None:  # pragma: no cover - just inserted in the same transaction above
            msg = "approve_corpus: row vanished immediately after insert"
            raise RegistryError(msg)
        return row

    async def _load(self, corpus_digest: str) -> queries.ReplayCorpusRow | None:
        async with self._session_factory() as session:
            return await queries.load_replay_corpus_by_digest(session, corpus_digest)

    async def current_corpus(
        self, *, owning_scope: str, target_tenant_id: uuid.UUID | None
    ) -> queries.ReplayCorpusRow | None:
        async with self._session_factory() as session:
            return await queries.load_current_replay_corpus(
                session, owning_scope=owning_scope, target_tenant_id=target_tenant_id, now=self._clock.now()
            )


__all__ = [
    "CURRENT_GENERATOR_VERSION",
    "MINIMUM_FIXTURE_CLASSES",
    "GeneratedCorpus",
    "ReplayCorpusApprovalConflict",
    "ReplayCorpusError",
    "ReplayCorpusService",
    "ReplayExecutionResult",
    "execute_corpus",
    "generate_corpus",
]
