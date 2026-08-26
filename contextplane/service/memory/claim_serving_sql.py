"""Every statement the claim read runs, and the comments that explain them.

Not a style split. `claim_serving.py` crossed the repository's size ceiling, and
this is the seam that was already there: the module held a service and a hundred
and forty lines of SQL that the service happens to execute. The statements are
edited for different reasons than the code around them — a temporal rule changes
here, a serving guarantee changes there — and reviewing one no longer means
scrolling past the other.

They stay module-level constants rather than becoming methods or a builder: every
one is fixed text with `:param` binds, which is what makes the `S608` suppressions
below true statements rather than assertions of good intent.
"""

from __future__ import annotations

from contextplane.service.retrieval._query_primitives import any_term_tsquery

_SERVABLE_AS_OF = """
    c.status IN ('staged', 'superseded')
AND c.consolidated_at IS NOT NULL
AND c.quarantined_at IS NULL
AND c.created_at <= :as_of
AND (c.t_invalidated_at IS NULL OR c.t_invalidated_at > :as_of)
"""

# Split from the FROM clause because the lexical arm needs `DISTINCT ON` in front of the
# projection and a ranking column after it.
_PROJECTION = """c.claim_id, c.subject_entity_id, c.predicate, c.value_jsonb AS value,
       c.claim_category, c.confidence, c.source_authority, c.asserted_valid_from,
       c.asserted_valid_to, c.confirms_claim_id, c.created_at,
       c.confidence_scored_at, c.confidence_hold_until, c.namespace,
       c.visibility, c.owning_tenant_id, subject.name AS subject_name"""

#: What a claim is *about*, by name, joined once rather than looked up per claim.
#:
#: A served claim named its subject with a UUID and nothing else, and an agent
#: holding one cannot tell what it is about. Asked *"who owns salt design system
#: and what lifecycle state is it in?"*, the model said so twice in its own
#: answer: *"it's not clear that this entity ID actually corresponds to the Salt
#: Design System capability (the canonical Salt entity has a different ID)"*, and
#: *"again it's uncertain this entity ID maps to Salt Design System
#: specifically."* It was right, and it had to compare two UUIDs by eye to work it
#: out — which is the one kind of reasoning a language model is worst at, on a
#: surface whose entire purpose is assembling context a model can use.
#:
#: `LEFT JOIN`, because a claim whose subject has not resolved is exactly the case
#: the curation queue exists for; it keeps its `NULL` name and stays servable.
#:
#: Discloses nothing new. The subject id is already in the payload, the caller can
#: already resolve it through the catalog, and claims are already filtered by
#: subject visibility before they are served — so the name is reachable by anyone
#: receiving the row, and withholding it only made the row harder to use.
_SUBJECT_JOIN = """
  LEFT JOIN entities subject
    ON subject.entity_id = c.subject_entity_id
   AND subject.tenant_id = c.owning_tenant_id
"""

_SELECT = f"""
SELECT {_PROJECTION}
  FROM memory_claims c
{_SUBJECT_JOIN}
"""  # noqa: S608 - _PROJECTION is a fixed, module-level column list, not caller input; every actual value below is bound via :param

# The ranked arms join the shared index. The discriminator lives in the join predicate, so
# a fact's vector cannot reach a claim answer even though both kinds share one table.
_INDEX_JOIN = f"""
  FROM memory_claims c
  JOIN embeddings emb
    ON emb.target_type = 'claim' AND emb.target_id = c.claim_id
{_SUBJECT_JOIN}
"""

_QUERY_SQL = f"""
{_SELECT}
 WHERE c.owning_tenant_id = :tid
   AND {_SERVABLE_AS_OF}
   AND c.claim_category = ANY(:categories)
   AND (CAST(:subject AS UUID) IS NULL OR c.subject_entity_id = CAST(:subject AS UUID))
   AND (CAST(:pred AS TEXT) IS NULL OR c.predicate = CAST(:pred AS TEXT))
   AND (CAST(:cat AS TEXT) IS NULL OR c.claim_category = CAST(:cat AS TEXT))
   AND (CAST(:ns AS TEXT) IS NULL OR c.namespace LIKE CAST(:ns AS TEXT) || '%')
 ORDER BY c.asserted_valid_from DESC, c.claim_id
 LIMIT :limit
"""

#: Claims that became serveable inside a window, newest-consolidated first.
#:
#: Ordered by `consolidated_at` rather than by `asserted_valid_from`, and that is
#: the whole reason this is its own statement. A caller asking "what became
#: reviewable since I last looked" is asking about *review* time; ordering that
#: window by assertion time and then applying a bound would drop the most
#: recently reviewed claims in favour of the most recently asserted ones, which
#: is a different answer wearing the same shape.
_CONSOLIDATED_SINCE_SQL = f"""
{_SELECT}
 WHERE c.owning_tenant_id = :tid
   AND {_SERVABLE_AS_OF}
   AND c.claim_category = ANY(:categories)
   AND c.consolidated_at > CAST(:after AS TIMESTAMPTZ)
   AND c.consolidated_at <= CAST(:as_of AS TIMESTAMPTZ)
 ORDER BY c.consolidated_at DESC, c.claim_id
 LIMIT :limit
"""


_BY_ID_SQL = f"""
{_SELECT}
 WHERE c.claim_id = :cid
   AND {_SERVABLE_AS_OF}
"""


_ARM_FILTERS = """
   AND c.owning_tenant_id = :tid
   AND c.claim_category = ANY(:categories)
   AND (CAST(:cat AS TEXT) IS NULL OR c.claim_category = CAST(:cat AS TEXT))
   AND (CAST(:ns AS TEXT) IS NULL OR c.namespace LIKE CAST(:ns AS TEXT) || '%')
   -- Filtered on the index row as well as the claim, so the planner prunes to one hash
   -- partition. Without it every ranked query scans all of them.
   AND emb.tenant_id = :tid
"""

_SEMANTIC_ARM_SQL = f"""
SELECT {_PROJECTION}
{_INDEX_JOIN}
 WHERE {_SERVABLE_AS_OF}
   AND emb.model_id = CAST(:model AS TEXT)
{_ARM_FILTERS}
 ORDER BY emb.vector <=> CAST(:vec AS VECTOR)
 LIMIT :limit
"""

# Matched against the same text the semantic arm embedded, so the two arms rank the same
# thing by different means rather than two different things.
#
# Reads the stored `ts_vector` generated column and its GIN index. The claim-scoped index
# this replaced had no stored tsvector, so it tokenised every candidate row twice per
# request -- once to match and once to rank.
#
# `DISTINCT ON` is load-bearing. The lexical arm deliberately does not filter `model_id`
# (text is text, whatever produced the vector), so with two models indexed a claim would
# appear once per model and fusion would count its weight twice.
# Any of the query's terms, ranked by how many of them a claim carries. The
# conjunction `plainto_tsquery` builds made this arm answer a question only when
# the asker already phrased it as keywords — see `any_term_tsquery`, which entity
# search's lexical arm shares so the two parse a prompt the same way.
_ANY_TERM = any_term_tsquery("q")

_LEXICAL_ARM_INNER = f"""
SELECT DISTINCT ON (c.claim_id) {_PROJECTION},
       ts_rank(emb.ts_vector, {_ANY_TERM}) AS lex_rank
{_INDEX_JOIN}
 WHERE {_SERVABLE_AS_OF}
{_ARM_FILTERS}
   AND emb.ts_vector @@ {_ANY_TERM}
 ORDER BY c.claim_id,
          ts_rank(emb.ts_vector, {_ANY_TERM}) DESC
"""

# `DISTINCT ON` requires its key to lead the ORDER BY, so relevance ordering is applied
# outside it.
_LEXICAL_ARM_SQL = f"""
SELECT * FROM (
{_LEXICAL_ARM_INNER}
) ranked
 ORDER BY lex_rank DESC, claim_id
 LIMIT :limit
"""  # noqa: S608 - _LEXICAL_ARM_INNER is itself built only from fixed module-level SQL text and :param binds, not caller input
