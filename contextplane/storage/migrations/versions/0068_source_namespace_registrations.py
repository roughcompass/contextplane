"""A replayed stream declares its handling tier once, at registration.

E1's last unbuilt clause: "stream-scoped action-class and sensitivity
declarations at source-namespace registration". E2-T2 gave the stream an
identity -- the `(source_system, source_namespace)` pair on a replayed session
event -- and this is the surface those declarations live on.

**Not `arc_source_connectors`, which is the obvious candidate and the wrong
one.** That table registers how ARC *fetches* a document it was pointed at:
schemes, hosts, media types, verifier ids, a byte ceiling, a credential ref. A
replayed conversational turn is pushed to us by an exporter and fetched from
nowhere -- it has no scheme and no host. Reusing the table would mean every
replay source inventing an `allowed_schemes` value to satisfy a constraint that
describes a different act, which is the category error E2-T2 declined to make
one table over when it took `assertion_provenance`'s vocabulary without its
foreign key.

**Nor `memory_source_governance`, which is the closer call and still wrong for
two reasons.** It is keyed on `sync_sources.source_id`, and a `sync_source` is a
connector *we* run: it carries a `schedule`, a `credentials_ref` and a `config`,
and `sync_runs` records us going and getting things. A replay exporter is not
scheduled by us and holds no credential of ours -- it authenticates as a
principal and pushes. Every registration surface in the tree so far describes
something we fetch from, and this is the first that describes something that
pushes to us, which is why none of them fit.

The second reason is the axis. That table's `authority_tier` is the claim
authority ladder -- `owner_human` through `unattributed`, the governed magnitude
`source-authority-ladder@1` -- which says how much a claim from this source is
*worth*. Handling sensitivity says how carefully its content must be *treated*.
A payroll export is highly sensitive and may be a weak authority; an owner's
OpenAPI sync is the strongest authority and entirely public. Two quantities that
happen to share a table are two quantities somebody eventually averages.

**The tier is the closed scale, and the CHECK is generated from it.**
`contextplane/sensitivity.py` holds the canonical ordered handling vocabulary, so
the constraint is built from `TIERS` rather than typed out again -- a fifth tier
is one edit there and not a migration hunt. ARC's own `data_sensitivity` stays
the open string it is: it is mirrored into a host's signed attestation and hashed
into the manifest-claims digest, so closing it would invalidate signatures over
values already sent. The two vocabularies stay separate deliberately and this
does not merge them.

**Sensitivity only. No action class, deliberately.** The clause names both, and a
stream plainly has one handling tier -- everything out of a payroll export is
payroll-sensitive. It does not plainly have one action class: a chat export
carries questions, decisions and tool traces alike, so a column here would be a
field an operator has to guess at, and a recorded guess is worse than a recorded
nothing. If a stream ever does have a single action class, adding the column is
another migration and no data moves.

**Unregistered is already the strict answer, so nothing here defaults.** A stream
with no row leaves the manifest's `data_sensitivity` unset, and
`selection._declared_sensitivity` reads absent as most restrictive -- a rule
written because a host sending an unknown tier or none escaped every rule that
named one. So an unregistered stream gets the strictest envelope until somebody
registers it, which is the pressure pointing the right way, and it arrives
without a second copy of a rule that already exists.
"""

from __future__ import annotations

from alembic import op

from contextplane.sensitivity import TIERS

revision = "0068_source_namespace_registrations"
down_revision: str | None = "0067_session_event_external_provenance"
branch_labels: str | None = None
depends_on: str | None = None

#: Built from the vocabulary rather than typed beside it. `sensitivity.py` sits
#: on the bottom import layer, so a migration may read it, and the alternative --
#: four quoted strings here -- would be one more place the scale is restated and
#: the one nothing keeps in step. Restating it is the mistake that module was
#: created to end: the names had been written out in nine places, and the order
#: in none of the canonical ones.
_TIER_LIST = ", ".join(f"'{tier}'" for tier in TIERS)

_TABLE = f"""
CREATE TABLE memory_source_namespaces (
    tenant_id          UUID NOT NULL REFERENCES tenants(tenant_id),
    source_system      TEXT NOT NULL,
    source_namespace   TEXT NOT NULL,

    -- The declaration this table exists for. NOT NULL: a registration whose
    -- tier is unstated is a row that says a stream was considered and nothing
    -- was decided, which reads to a later operator as a decision.
    data_sensitivity   TEXT NOT NULL,

    -- Who said so and when. A handling tier is a governance claim and an
    -- auditor asking "who decided payroll was restricted" should not have to
    -- reach for the audit log to find out whether anybody did.
    registered_by      UUID NOT NULL REFERENCES actors(actor_id),
    registered_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason             TEXT NOT NULL,

    PRIMARY KEY (tenant_id, source_system, source_namespace),

    CONSTRAINT ck_msn_sensitivity CHECK (data_sensitivity IN ({_TIER_LIST})),
    CONSTRAINT ck_msn_system_len CHECK (char_length(source_system) BETWEEN 1 AND 200),
    CONSTRAINT ck_msn_namespace_len CHECK (char_length(source_namespace) BETWEEN 1 AND 200),
    -- Long enough to be a sentence. A tier recorded with "prod" beside it is a
    -- value nobody can review, and this is the same bar the magnitude registry
    -- holds a governed number to.
    CONSTRAINT ck_msn_reason_len CHECK (char_length(reason) BETWEEN 20 AND 2000)
)
"""

#: The lookup the write path performs, which is the primary key read backwards
#: for one tenant. Declared so a tenant listing its registrations does not scan.
_INDEX = "CREATE INDEX ix_msn_tenant ON memory_source_namespaces (tenant_id, source_system)"

#: The tier the decision actually judged the act at, recorded beside the verdict.
#:
#: Added here rather than in a follow-up because without it the declaration this
#: migration introduces is unobservable: an advisory record says a principal was
#: refused and not what handling tier the matrix matched on, so "was this act
#: judged as restricted because the stream said so, or because nobody had
#: declared it" has no answer. Those two are the same verdict and different
#: facts, and the second is an operator's omission somebody should fix.
#:
#: Nullable, and null means the manifest carried no tier -- which the selection
#: engine reads as the most restrictive. Storing `restricted` instead would erase
#: exactly the distinction the column is for.
_ADVISORY_SENSITIVITY = """
ALTER TABLE arc_envelope_advisory_records ADD COLUMN data_sensitivity TEXT
"""


def upgrade() -> None:
    op.execute(_TABLE)
    op.execute(_INDEX)
    op.execute(_ADVISORY_SENSITIVITY)


def downgrade() -> None:
    op.execute("ALTER TABLE arc_envelope_advisory_records DROP COLUMN data_sensitivity")
    op.execute("DROP TABLE memory_source_namespaces")
