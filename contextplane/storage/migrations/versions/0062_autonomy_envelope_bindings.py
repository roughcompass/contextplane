"""An autonomy envelope is a `policy` revision, and a binding says whose it is.

An envelope answers "what is this agent principal allowed to do". The artifact
half of that needs no new schema: `ck_arc_artifacts_kind` already admits
`policy` (`0001_baseline_schema.py:2420`), and the authority matrix goes in
`arc_applicability_rules`, which already exists. **The binding is the part
nothing in the tree has.** Without it an envelope is indistinguishable from any
other `policy` artifact -- there is no column anywhere that says this revision
governs that principal -- so the matrix cannot be the half built first.

**`autonomy_envelope` is deliberately not added as an artifact kind.** A new
kind is worth a CHECK-constraint migration only if envelopes must be listable
as their own class, and nothing needs that yet: no service logic branches on
kind, and this table is itself the index of which revisions are envelopes.

**Not to be confused with `arc_expected_impact_envelopes`.** That is a
blast-radius forecast attached to an authoring proposal version -- how many
selections change if this revision activates -- and it is about a document, not
a principal. Two unrelated meanings of "envelope" now live in one namespace,
which is why every name here says `autonomy` rather than relying on context.

**The principal is an IAM workload identity: an `(issuer, subject)` pair, no
foreign key.** That is the settled ARC idiom -- `created_by`, `opened_by`,
`author`, `admitted_by` and `actor` are all bare TEXT pairs. `actors` is
deliberately not the referent: it is tenant-local, its `actor_kind` admits only
`human` and `sync_worker`, and forcing an agent workload into that table would
make an authority binding depend on a row whose purpose is attribution.

**Which revision an envelope binds to is enforced declaratively, not by the
service.** `artifact_kind` is carried on the binding, pinned to `policy` by a
CHECK, and tied to the real artifact by a composite foreign key; a second
composite key ties the revision to that same artifact. The two unique
constraints those keys need are added here. Enforcing this in Python instead
would leave the database willing to bind a principal to a `runbook`, whose
applicability rules were written for corpus selection and would silently become
that principal's authority -- an authority-widening bug with no error at the
point it happens.

**The binding names a revision, never an artifact.** E1 pairs "instant suspend"
with "governed widen (full ARC pipeline)". If a binding named the artifact, a
widen would take effect the moment a new revision activated, and a principal's
authority would change as a side effect of somebody publishing. Naming the
revision makes the widen a separate, recorded act: close one binding, open
another.

**Suspend is a status flip, not a delete, and it does not free the principal's
slot.** The interval stays open and the row keeps saying who it governs, so
reinstating is one more flip and the history reads as a suspension rather than a
gap. The exclusion constraint deliberately ignores `state`: see the comment on
it below, which records the escalation that the `state = 'active'` version
allowed. A governed widen is therefore revoke-then-grant, both authorized at the
envelope's own scope.
"""

from __future__ import annotations

from alembic import op

revision = "0062_autonomy_envelope_bindings"
down_revision: str | None = "0061_arc_entity_scope"
branch_labels: str | None = None
depends_on: str | None = None

#: Postgres will only point a foreign key at columns carrying a unique
#: constraint, so the two composite keys below each need one first. Both are
#: redundant as constraints -- each is a superset of its table's primary key --
#: and exist for two reasons rather than one.
#:
#: The obvious one is that they make the kind check expressible in SQL. The
#: second is that making `kind` part of a key changes how Postgres locks it: a
#: referencing insert takes `FOR KEY SHARE` on the artifact row, and an
#: `UPDATE ... SET kind` now needs `FOR UPDATE`, so the two conflict. That
#: closes the window between the service reading `kind` and inserting the
#: binding, which a `BEFORE INSERT` trigger would have left open.
#:
#: **`ADD CONSTRAINT ... UNIQUE` takes an `AccessExclusiveLock` and builds its
#: index with a blocking scan.** The non-blocking form is `CREATE UNIQUE INDEX
#: CONCURRENTLY` followed by `ADD CONSTRAINT ... USING INDEX`, which cannot run
#: inside a transaction and so would need an autocommit block here. It is not
#: used: this service has never been released, so both tables are empty in every
#: deployment that will ever run this revision, and the lock is held for
#: microseconds. A migration adding a unique key to a populated `arc_revisions`
#: later would need the concurrent form.
_ARTIFACT_KIND_UNIQUE = """
ALTER TABLE arc_artifacts
    ADD CONSTRAINT uq_arc_artifacts_id_kind UNIQUE (artifact_id, kind)
"""

_REVISION_ARTIFACT_UNIQUE = """
ALTER TABLE arc_revisions
    ADD CONSTRAINT uq_arc_revisions_id_artifact UNIQUE (revision_id, artifact_id)
"""

_BINDINGS = """
CREATE TABLE arc_autonomy_envelope_bindings (
    binding_id        UUID PRIMARY KEY,
    tenant_id         UUID NOT NULL REFERENCES tenants(tenant_id),

    -- The envelope. `artifact_id` and `artifact_kind` are carried so the two
    -- composite foreign keys below can say, in SQL, that this revision belongs
    -- to an artifact whose kind is `policy`.
    revision_id       UUID NOT NULL,
    artifact_id       UUID NOT NULL,
    artifact_kind     TEXT NOT NULL,

    -- The IAM workload identity this envelope governs.
    principal_issuer  TEXT NOT NULL,
    principal_subject TEXT NOT NULL,

    state             TEXT NOT NULL,

    -- The interval the binding is in force. `effective_to` NULL means open,
    -- which is what a binding nothing has replaced looks like.
    effective_from    TIMESTAMPTZ NOT NULL,
    effective_to      TIMESTAMPTZ,

    -- A suspension is its own act and carries its own record. Kept apart from
    -- `reason`, which says why the binding was granted.
    suspended_at        TIMESTAMPTZ,
    suspension_reason   TEXT,

    actor             TEXT NOT NULL,
    reason            TEXT NOT NULL,
    audit_reference   TEXT,
    recorded_at       TIMESTAMPTZ NOT NULL,

    CONSTRAINT ck_arc_envelope_bindings_state CHECK (state IN ('active', 'suspended')),

    -- The whole point of carrying the two denormalised columns.
    CONSTRAINT ck_arc_envelope_bindings_kind CHECK (artifact_kind = 'policy'),
    CONSTRAINT fk_arc_envelope_bindings_artifact FOREIGN KEY (artifact_id, artifact_kind)
        REFERENCES arc_artifacts (artifact_id, kind),
    CONSTRAINT fk_arc_envelope_bindings_revision FOREIGN KEY (revision_id, artifact_id)
        REFERENCES arc_revisions (revision_id, artifact_id),

    -- `>=`, not `>`. An equal pair is an *empty* interval, which is the honest
    -- record of a binding revoked before it ever took effect -- granted and
    -- withdrawn in the same instant, or future-dated and cancelled first.
    -- `tstzrange(x, x)` is empty and `&&` is false for an empty range, so such
    -- a row also stops competing for its principal's slot without any special
    -- case in the exclusion constraint. Granting a zero-length envelope is
    -- still refused, by the service, where it is a caller error rather than an
    -- outcome.
    CONSTRAINT ck_arc_envelope_bindings_interval CHECK (
        effective_to IS NULL OR effective_to >= effective_from
    ),

    -- A suspended binding says when and why, and an active one says neither.
    -- Written as an equivalence so both directions are one statement: a state
    -- flip that forgets the record, and a record left behind by a reinstate,
    -- are the same constraint violation.
    CONSTRAINT ck_arc_envelope_bindings_suspension_recorded CHECK (
        (state = 'suspended') = (suspended_at IS NOT NULL AND suspension_reason IS NOT NULL)
    ),

    CONSTRAINT ck_arc_envelope_bindings_principal_present CHECK (
        char_length(btrim(principal_issuer)) > 0 AND char_length(btrim(principal_subject)) > 0
    ),

    -- The rule this table exists to enforce: one principal, one envelope over
    -- any given window.
    --
    -- **Not restricted to `state = 'active'`, and that restriction was a
    -- privilege escalation.** The first version carried `WHERE (state =
    -- 'active')` on the argument that a suspension should free the slot so a
    -- governed widen could grant a replacement over the same window. But
    -- suspending is authorized at tenant scope while granting is authorized at
    -- the envelope's, so freeing the slot handed a tenant admin a two-step
    -- route to a state they could not reach in one: suspend the binding a
    -- deployment operator made to a global envelope, then grant a
    -- tenant-scoped envelope of their own authoring to the same principal.
    -- Each step passes its own check; the sequence replaces deployment-mandated
    -- governance with self-authored governance. Authorizing per operation says
    -- nothing about traces.
    --
    -- An *open interval* now reserves the principal whatever the state, so
    -- suspension is purely a narrowing and the widen path is revoke-then-grant
    -- -- and revoking is authorized at the envelope's scope, which is where the
    -- decision belongs. An empty interval (`effective_to = effective_from`)
    -- overlaps nothing and so reserves nothing, which is what makes a withdrawn
    -- grant free its slot.
    CONSTRAINT ex_arc_envelope_bindings_one_per_principal EXCLUDE USING gist (
        tenant_id WITH =,
        principal_issuer WITH =,
        principal_subject WITH =,
        tstzrange(effective_from, effective_to) WITH &&
    )
)
"""

#: "Which principals does this envelope govern" -- the read an operator runs
#: before revoking a revision, and the read that answers whether it is safe to.
#: It also serves the referential-integrity probe Postgres runs on the revision
#: side whenever an `arc_revisions` row is updated or deleted.
_REVERSE_INDEX = """
CREATE INDEX ix_arc_envelope_bindings_revision
    ON arc_autonomy_envelope_bindings (revision_id)
"""

#: The artifact side of the same story, and this one exists *only* for it.
#: Nothing reads bindings by artifact -- a caller with an artifact wants its
#: revisions first. But without an index the referential-integrity probe on
#: every `arc_artifacts` update or delete sequentially scans this whole table,
#: which is the standard cost of an unindexed foreign key and the standard fix.
_ARTIFACT_INDEX = """
CREATE INDEX ix_arc_envelope_bindings_artifact
    ON arc_autonomy_envelope_bindings (artifact_id)
"""

#: The hot read: resolve this request's principal to its envelope. Ordered
#: tenant-first because every lookup is already inside one tenant.
_LOOKUP_INDEX = """
CREATE INDEX ix_arc_envelope_bindings_principal
    ON arc_autonomy_envelope_bindings (tenant_id, principal_issuer, principal_subject)
"""


def upgrade() -> None:
    # `btree_gist` supplies `=` for uuid and text inside a gist operator class,
    # which the exclusion constraint needs. Installed by 0050 and deliberately
    # re-asserted rather than assumed: this migration is the second dependant,
    # and a conditional create is cheaper than a failure that reads as a bug in
    # the constraint.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(_ARTIFACT_KIND_UNIQUE)
    op.execute(_REVISION_ARTIFACT_UNIQUE)
    op.execute(_BINDINGS)
    op.execute(_REVERSE_INDEX)
    op.execute(_ARTIFACT_INDEX)
    op.execute(_LOOKUP_INDEX)


def downgrade() -> None:
    op.execute("DROP TABLE arc_autonomy_envelope_bindings")
    op.execute("ALTER TABLE arc_revisions DROP CONSTRAINT uq_arc_revisions_id_artifact")
    op.execute("ALTER TABLE arc_artifacts DROP CONSTRAINT uq_arc_artifacts_id_kind")
    # `btree_gist` is left installed: 0050's constraint still depends on it.
