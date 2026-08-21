"""ARC domain types: request identity, closed vocabularies, directives, rules.

Nothing here does I/O. The selection engine is a pure function over these types
and a snapshot of active revisions, which is what makes determinism a property
test rather than an integration test that has to hold a database still.

Two themes worth knowing before reading:

- **Vocabularies are closed and unknown values are rejected.** A host able to
  invent a task kind could name a lower-risk one and escape an obligation that
  matched the real one.
- **Constraints normalize to a comparable form.** Every operator reduces to a
  modality plus a set, so conflict detection is one intersection rather than a
  table of pairwise special cases — a table is where the missing combination
  hides.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import hashlib
import uuid
from typing import Any

from contextplane.exceptions import RegistryError
from contextplane.types import TenantContext

# Claim the validated issuer is read from. `validate_oidc_token` has already
# checked it against `OIDC_ISSUER_ALLOWLIST` by the time ARC sees it.
_ISSUER_CLAIM = "iss"


@dataclasses.dataclass(frozen=True)
class ArcRequestContext:
    """Per-request ARC identity, wrapping the tenant context the middleware built.

    Four additions over `TenantContext`, each with a specific consumer:

    - **`oidc_issuer`** — global lifecycle writes are authorized by an exact
      `{issuer, subject}` pair in a deployment-managed allowlist, because the
      `admin` role is tenant-scoped and cannot serve as a deployment trust root.
      `TenantContext` retains the subject but not the issuer, which is the whole
      reason this type exists.
    - **`host_id`** — the registered agent host. Attestation verification binds a
      challenge to it, and a receipt records it.
    - **`token_restriction_digest`** — bound into MCP preflight state so a
      restriction change invalidates the session rather than silently widening
      what a live connection can do.
    - **`mcp_session_id`** — the live MCP connection, when the request arrived
      over MCP rather than REST. Server-assigned; never a caller-supplied string,
      because a caller-chosen session id would let one caller observe or hijack
      another's preflight state.

    Frozen, matching `TenantContext`: a request identity that service code can
    edit is not an identity.
    """

    tenant: TenantContext
    oidc_issuer: str
    host_id: str | None = None
    token_restriction_digest: str | None = None
    mcp_session_id: str | None = None

    # -- pass-throughs, so service code need not reach through `.tenant` --------

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.tenant_id

    @property
    def actor_id(self) -> uuid.UUID:
        return self.tenant.actor_id

    @property
    def roles(self) -> list[str]:
        return self.tenant.roles

    @property
    def oidc_subject(self) -> str:
        return self.tenant.oidc_subject

    @property
    def operator_identity(self) -> tuple[str, str]:
        """The `(issuer, subject)` pair a deployment-operator allowlist matches on.

        Exact, case-sensitive comparison is the caller's job — this only supplies
        the pair. Returning a tuple rather than a formatted string keeps callers
        from inventing their own separator and comparing the wrong things.
        """
        return (self.oidc_issuer, self.tenant.oidc_subject)

    @property
    def is_mcp_session(self) -> bool:
        return self.mcp_session_id is not None

    @classmethod
    def from_validated_claims(
        cls,
        tenant: TenantContext,
        claims: dict[str, Any],
        *,
        host_id: str | None = None,
        token_restriction_digest: str | None = None,
        mcp_session_id: str | None = None,
    ) -> ArcRequestContext:
        """Build from the claims `validate_oidc_token` already returned.

        ARC deliberately does not decode the token itself. A second parser is a
        second place for the two to disagree about `iss`, and the whole value of
        the issuer here is that it is the *validated* one — checked against the
        allowlist by the code that owns that check.

        Raises `ValueError` when the issuer claim is absent or empty, rather than
        defaulting: an ARC context with no issuer cannot authorize a global
        operation, and a silent empty string would compare unequal to every
        allowlist entry and look like a permissions problem instead of a wiring
        bug.
        """
        issuer = claims.get(_ISSUER_CLAIM)
        if not isinstance(issuer, str) or not issuer:
            msg = (
                "ArcRequestContext requires a validated 'iss' claim; got "
                f"{issuer!r}. The caller should pass the claims payload returned "
                "by validate_oidc_token, not a re-parsed token."
            )
            raise ValueError(msg)

        return cls(
            tenant=tenant,
            oidc_issuer=issuer,
            host_id=host_id,
            token_restriction_digest=token_restriction_digest,
            mcp_session_id=mcp_session_id,
        )


# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------
#
# Closed on purpose. If a host could invent a task kind or action class, it could
# name a lower-risk value and escape an obligation that matched the real one —
# so an unknown value is a rejection, never a pass-through.


class IntentKind(enum.StrEnum):
    READ_ONLY = "read_only"
    CODE_CHANGE = "code_change"
    DEPENDENCY_CHANGE = "dependency_change"
    CONFIGURATION_CHANGE = "configuration_change"
    SECURITY_SENSITIVE_CHANGE = "security_sensitive_change"
    DATA_ACCESS = "data_access"
    DEPLOYMENT = "deployment"


class ActionClass(enum.StrEnum):
    MERGE = "merge"
    DEPLOY = "deploy"
    PRODUCTION_CONFIGURATION_MUTATION = "production_configuration_mutation"
    SECRET_RELEASE = "secret_release"  # noqa: S105 - an ActionClass enum label naming a governed action kind, not a secret value itself
    DATA_EXPORT = "data_export"


class AuthorityScope(enum.StrEnum):
    """Ordered widest to narrowest. The order is load-bearing for precedence."""

    GLOBAL = "global"
    TENANT = "tenant"
    DOMAIN = "domain"
    ENTITY = "entity"
    INTENT = "intent"

    @property
    def rank(self) -> int:
        """Position in the precedence order; lower is higher authority."""
        return _SCOPE_ORDER.index(self)


_SCOPE_ORDER: tuple[AuthorityScope, ...] = (
    AuthorityScope.GLOBAL,
    AuthorityScope.TENANT,
    AuthorityScope.DOMAIN,
    AuthorityScope.ENTITY,
    AuthorityScope.INTENT,
)


class DirectiveType(enum.StrEnum):
    REQUIRE = "require"
    PROHIBIT = "prohibit"
    VERIFY = "verify"
    ESCALATE = "escalate"
    CITATION_ONLY = "citation_only"

    @property
    def is_action_protecting(self) -> bool:
        """Whether this type can make an action ready or blocked.

        `citation_only` cannot: it may be retrieved and cited, but it carries no
        comparable constraint, so it has nothing to conflict with and nothing to
        enforce.
        """
        return self is not DirectiveType.CITATION_ONLY


class Modality(enum.StrEnum):
    REQUIRE = "require"
    PROHIBIT = "prohibit"


class ConstraintOperator(enum.StrEnum):
    EQUALS = "equals"
    IN_SET = "in_set"
    NOT_IN_SET = "not_in_set"
    PRESENT = "present"


class SatisfactionMode(enum.StrEnum):
    AUTHORIZED_RETRIEVAL = "authorized_retrieval"
    SIGNED_RESULT = "signed_result"


class ResolutionStatus(enum.StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class DetailAudience(enum.StrEnum):
    ALL_MATCHED_ACTORS = "all_matched_actors"
    TENANT_ADMIN_AUDITOR = "tenant_admin_auditor"
    REGISTERED_GATEWAY_ONLY = "registered_gateway_only"


class ArcVocabularyError(RegistryError):
    """A value outside a closed ARC vocabulary.

    Named distinctly from ``contextplane.exceptions.VocabularyError`` (a
    different, catalog-domain exception this class does not subclass) —
    two closed-vocabulary violations that happen to share a name would
    otherwise be indistinguishable to an `except` clause importing the
    wrong one.
    """


def parse_intent_kind(value: str) -> IntentKind:
    try:
        return IntentKind(value)
    except ValueError as exc:
        msg = (
            f"unknown task kind {value!r}; the vocabulary is closed so a host "
            "cannot name a lower-risk value to escape an obligation"
        )
        raise ArcVocabularyError(msg) from exc


def parse_action_class(value: str) -> ActionClass:
    try:
        return ActionClass(value)
    except ValueError as exc:
        msg = f"unknown action class {value!r}; the vocabulary is closed"
        raise ArcVocabularyError(msg) from exc


def parse_directive_type(value: str) -> DirectiveType:
    """Parse one *persisted* `arc_directives.directive_type` value.

    Matches `parse_intent_kind`/`parse_action_class`'s own fail-closed shape:
    a value outside this closed vocabulary raises `ArcVocabularyError`
    rather than the bare `ValueError` a caller reading a stored row has no
    reason to expect from a small closed-vocabulary parse -- the database's
    own CHECK constraint should make an unrecognized stored value
    unreachable, but "should" is not "does", and the loud, typed failure is
    the correct outcome if the two ever drift.
    """
    try:
        return DirectiveType(value)
    except ValueError as exc:
        msg = f"unknown persisted directive type {value!r}; the vocabulary is closed"
        raise ArcVocabularyError(msg) from exc


#: The authoring surface's wire `directive_type` vocabulary, mapped to the
#: persisted `DirectiveType` each literal materialises as. This is the one
#: definition both `submission.py::_directive_row` (writing the persisted
#: `arc_directives` row) and `shadow.py::_directive_from_dict` (building the
#: domain object a shadow evaluation runs `select()` over) translate a
#: candidate's own wire literal through, so a value one of them would accept
#: and the other would not can never happen.
#:
#: `verify_before_action` is a deliberate two-name design, not two competing
#: vocabularies: it is the wire schema's self-documenting, product-facing
#: name for the same obligation `DirectiveType.VERIFY` persists under a
#: short storage token. `citation_only` needs no entry of its own beyond
#: this dict's identity mapping -- the two vocabularies already share that
#: one literal outright.
_WIRE_DIRECTIVE_TYPE_TRANSLATION: dict[str, DirectiveType] = {
    "citation_only": DirectiveType.CITATION_ONLY,
    "verify_before_action": DirectiveType.VERIFY,
}


def parse_wire_directive_type(value: str) -> DirectiveType:
    """Translate one authoring-surface wire `directive_type` literal into
    the persisted `DirectiveType` it materialises as.

    Fails closed on anything else, including a persisted-only member such
    as `require`/`prohibit`/`escalate`: the authoring surface has never
    been able to author those, so a candidate document naming one is not a
    translation gap, it is an unrecognized wire value -- the same
    conservative failure `parse_intent_kind`/`parse_action_class` already
    give a caller for their own closed vocabularies.
    """
    try:
        return _WIRE_DIRECTIVE_TYPE_TRANSLATION[value]
    except KeyError as exc:
        msg = (
            f"unknown authoring directive_type {value!r}; the authoring surface's wire "
            "vocabulary is closed to citation_only and verify_before_action"
        )
        raise ArcVocabularyError(msg) from exc


# ---------------------------------------------------------------------------
# Conflict subject and normalized constraint
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, order=True)
class ConflictSubjectKey:
    """What a directive constrains — six fields, no modality, no value.

    Splitting the subject from the constraint is the point. A single key mixing
    both left "same key, incompatible constraints" undefined, which is exactly
    the case conflict detection exists to catch: two directives about the same
    thing that cannot both be satisfied.
    """

    schema_version: str
    namespace: str
    subject_selector: str
    operation: str
    action_class: str
    target_selector: str

    def canonical_tuple(self) -> tuple[str, ...]:
        """The identity. A digest may index this but never defines it."""
        return (
            self.schema_version,
            self.namespace,
            self.subject_selector,
            self.operation,
            self.action_class,
            self.target_selector,
        )

    def digest(self) -> str:
        """Index only — `arc_conflict_domains.conflict_subject_digest`.

        Length-prefixed so no two different field splits produce one digest.
        """
        parts = self.canonical_tuple()
        message = b"".join(len(p.encode()).to_bytes(4, "big") + p.encode() for p in parts)
        return hashlib.sha256(message).hexdigest()


@dataclasses.dataclass(frozen=True)
class NormalizedConstraint:
    """A directive's constraint, reduced to a comparable form.

    `values` is a frozenset for every operator so intersection is one code path
    rather than a table of pairwise special cases — a table is where the missing
    combination hides.
    """

    modality: Modality
    operator: ConstraintOperator
    values: frozenset[str]

    @classmethod
    def parse(cls, modality: str, operator: str, raw_value: str | None) -> NormalizedConstraint:
        """Normalize a stored constraint.

        `in_set` and `not_in_set` accept a comma-separated list; whitespace around
        members is stripped and order is discarded, because `"a, b"` and `"b,a"`
        mean the same thing and must not compare as different constraints.
        """
        try:
            mod = Modality(modality)
            op = ConstraintOperator(operator)
        except ValueError as exc:
            raise ArcVocabularyError(f"bad constraint {modality!r}/{operator!r}") from exc

        if op is ConstraintOperator.PRESENT:
            if raw_value:
                msg = "the 'present' operator takes no value"
                raise ArcVocabularyError(msg)
            return cls(modality=mod, operator=op, values=frozenset())

        if raw_value is None or raw_value == "":
            msg = f"operator {op!s} requires a value"
            raise ArcVocabularyError(msg)

        if op in (ConstraintOperator.IN_SET, ConstraintOperator.NOT_IN_SET):
            members = frozenset(v.strip() for v in raw_value.split(",") if v.strip())
            if not members:
                msg = f"operator {op!s} requires at least one member"
                raise ArcVocabularyError(msg)
            return cls(modality=mod, operator=op, values=members)

        return cls(modality=mod, operator=op, values=frozenset({raw_value}))


@dataclasses.dataclass(frozen=True)
class Directive:
    """A directive as the selection engine sees it.

    `conflict_subject` and `constraint` are both present or both absent. A
    directive missing the comparable shape is `citation_only` by definition — it
    can be cited but cannot make an action ready or blocked.
    """

    directive_id: uuid.UUID
    revision_id: uuid.UUID
    directive_type: DirectiveType
    source_anchor: str
    conflict_subject: ConflictSubjectKey | None = None
    constraint: NormalizedConstraint | None = None
    satisfaction_mode: SatisfactionMode | None = None
    verification_max_age_seconds: int | None = None
    accepted_verifier_classes: frozenset[str] = frozenset()
    required_evidence_type: str | None = None
    delegable_exception: bool = False

    def __post_init__(self) -> None:
        comparable = self.conflict_subject is not None and self.constraint is not None
        if self.directive_type.is_action_protecting and not comparable:
            msg = (
                f"directive {self.directive_id} is {self.directive_type!s} but "
                "carries no conflict subject and constraint; without the "
                "comparable shape it is citation_only and cannot protect an action"
            )
            raise ArcVocabularyError(msg)
        if self.satisfaction_mode is SatisfactionMode.SIGNED_RESULT and (
            not self.accepted_verifier_classes or self.required_evidence_type is None
        ):
            msg = (
                f"directive {self.directive_id} requires a signed result but names "
                "no accepted verifier classes or evidence type, so nothing could "
                "ever satisfy it"
            )
            raise ArcVocabularyError(msg)

    @property
    def is_enforceable(self) -> bool:
        return self.directive_type.is_action_protecting


@dataclasses.dataclass(frozen=True)
class ApplicabilityRule:
    """A declarative predicate over a task manifest.

    Every selector is a frozenset so matching is set membership rather than list
    scanning, and so two rules differing only in selector order are equal.
    """

    rule_id: uuid.UUID
    revision_id: uuid.UUID
    scope: AuthorityScope
    is_mandatory: bool = True
    target_tenant_id: uuid.UUID | None = None
    entity_ids: frozenset[uuid.UUID] = frozenset()
    entity_labels: frozenset[str] = frozenset()
    domain_ids: frozenset[str] = frozenset()
    intent_kinds: frozenset[IntentKind] = frozenset()
    action_classes: frozenset[ActionClass] = frozenset()
    environments: frozenset[str] = frozenset()
    data_sensitivity_tiers: frozenset[str] = frozenset()
    effective_from: datetime.datetime | None = None
    effective_until: datetime.datetime | None = None

    def __post_init__(self) -> None:
        if self.scope is AuthorityScope.TENANT and self.target_tenant_id is None:
            msg = f"rule {self.rule_id} is tenant-scoped but names no target tenant"
            raise ArcVocabularyError(msg)
        if self.scope is AuthorityScope.ENTITY and not (self.entity_ids or self.entity_labels):
            msg = f"rule {self.rule_id} is entity-scoped but names no entity"
            raise ArcVocabularyError(msg)


@dataclasses.dataclass(frozen=True)
class IntentManifest:
    """The attested description of what the agent is about to do.

    Selection reads only this and the active revisions. `intent_summary` is
    deliberately absent: it is optional search text, excluded from mandatory
    selection, so including it here would let free text influence which
    obligations apply.
    """

    session_id: str
    intent_kind: IntentKind
    requested_action_classes: frozenset[ActionClass] = frozenset()
    entity_ids: frozenset[uuid.UUID] = frozenset()
    domain_ids: frozenset[str] = frozenset()
    environment: str | None = None
    data_sensitivity: str | None = None
    repository_identity: str | None = None
