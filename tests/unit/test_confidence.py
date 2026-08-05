"""What a confidence number means, and the properties that keep it meaningful.

Most of these are properties rather than examples, because the arithmetic is only
worth having if certain things are true of it for every input: it is bounded, it
never lets volume beat authority, it never raises authority, and it can be
re-derived from what is stored beside it.

The published bucket boundaries are tested as a contract. A caller thresholding at
0.8 is asserting something specific, and if a boundary moves silently every such
caller's filter changes meaning without anyone editing it.
"""

from __future__ import annotations

import math

import pytest

from registry.service.memory.claim_authority import SOURCE_AUTHORITY_ORDER, SOURCE_AUTHORITY_RANK
from registry.service.memory.confidence import (
    BASE_CONFIDENCE_BY_AUTHORITY,
    BUCKET_CONFIRMED,
    BUCKET_LOWER_BOUNDS,
    BUCKET_MODERATE,
    BUCKET_SEMANTICS,
    BUCKET_STRONG,
    BUCKET_UNRELIABLE,
    BUCKET_WEAK,
    CORROBORATION_WEIGHT_BY_RANK,
    DECAY_FLOOR,
    MAX_CONFIDENCE,
    ConfidencePolicy,
    EvidenceClass,
    bucket_for,
    corroborating_mass,
    recompute,
    score,
)


def _classes(n: int, rank: int, *, group: str | None = None) -> list[EvidenceClass]:
    return [EvidenceClass(key=f"k{i}", group=group or f"g{i}", authority_rank=rank) for i in range(n)]


# --- the published scale ------------------------------------------------------


def test_the_buckets_partition_the_range_without_gaps_or_overlap() -> None:
    """A value falling in no bucket, or in two, makes the published semantics a
    lie for that value."""
    bounds = [lower for _, lower in BUCKET_LOWER_BOUNDS]
    assert bounds == sorted(bounds, reverse=True)
    assert bounds[-1] == 0.0
    for value in (0.0, 0.19, 0.2, 0.44, 0.45, 0.69, 0.7, 0.84, 0.85, 0.98, 1.0):
        assert bucket_for(value) in BUCKET_SEMANTICS


def test_no_bucket_is_narrower_than_the_accuracy_a_check_can_verify() -> None:
    """A narrower bucket would claim a resolution no evaluation could confirm — a
    caller choosing between two numbers provably meaning the same thing."""
    tolerance = 0.15
    bounds = [lower for _, lower in BUCKET_LOWER_BOUNDS] + [1.0]
    widths = [round(bounds[i] - bounds[i + 1], 10) for i in range(len(bounds) - 2)]
    for width in widths:
        assert width >= tolerance, f"bucket width {width} is below the verifiable tolerance"


def test_a_boundary_value_belongs_to_the_bucket_above_it() -> None:
    """Half-open upward, so a caller thresholding exactly on a boundary knows
    which side they get."""
    assert bucket_for(0.85) == BUCKET_CONFIRMED
    assert bucket_for(0.8499) == BUCKET_STRONG
    assert bucket_for(0.70) == BUCKET_STRONG
    assert bucket_for(0.6999) == BUCKET_MODERATE
    assert bucket_for(0.45) == BUCKET_MODERATE
    assert bucket_for(0.20) == BUCKET_WEAK
    assert bucket_for(0.0) == BUCKET_UNRELIABLE


def test_every_bucket_publishes_what_it_licenses() -> None:
    """A number without a stated meaning is one every caller interprets
    differently."""
    for name, _ in BUCKET_LOWER_BOUNDS:
        assert BUCKET_SEMANTICS[name].strip()


def test_certainty_is_not_reachable() -> None:
    """A scale on which something reaches 1.0 has nowhere left to express "and
    this one was checked twice"."""
    assert MAX_CONFIDENCE < 1.0
    best = score(authority="owner_human", corroborators=_classes(50, 0), is_confirmed=True)
    assert best is not None
    assert best.value <= MAX_CONFIDENCE


# --- base scores and the ladder ------------------------------------------------


def test_base_scores_strictly_decrease_across_the_authority_ladder() -> None:
    """The ladder is the only ordering over these tiers. A base table ordered
    differently would eventually have a rule built on the wrong one."""
    scored = [t for t in SOURCE_AUTHORITY_ORDER if t in BASE_CONFIDENCE_BY_AUTHORITY]
    values = [BASE_CONFIDENCE_BY_AUTHORITY[t] for t in scored]
    assert values == sorted(values, reverse=True)
    assert len(set(values)) == len(values), "two tiers share a base score"


def test_an_owner_tier_always_starts_above_every_observer_tier() -> None:
    """Ownership-major, matching the authority ladder. An inversion here would
    contradict the rule that only owners assert authoritative facts."""
    owners = [v for t, v in BASE_CONFIDENCE_BY_AUTHORITY.items() if t.startswith("owner_")]
    observers = [v for t, v in BASE_CONFIDENCE_BY_AUTHORITY.items() if t.startswith("observer_")]
    assert min(owners) > max(observers)


def test_an_unresolved_subject_has_no_score_rather_than_a_low_one() -> None:
    """A number would assert a determination nobody made, and nothing would mark
    it stale once curation links the claim."""
    assert score(authority="unattributed") is None


def test_the_strongest_base_is_not_yet_the_confirmed_bucket() -> None:
    """An owner asserting something first-hand is the strongest thing the write
    path can produce, but the top bucket is for a human who reviewed this
    particular claim — a different event."""
    best_base = BASE_CONFIDENCE_BY_AUTHORITY["owner_human"]
    assert bucket_for(best_base) == BUCKET_STRONG


# --- corroboration -------------------------------------------------------------


def test_corroboration_weights_strictly_decrease_with_authority() -> None:
    """Otherwise there are two differently-ordered ladders and a rule will
    eventually be built on the wrong one."""
    ranks = sorted(CORROBORATION_WEIGHT_BY_RANK)
    weights = [CORROBORATION_WEIGHT_BY_RANK[r] for r in ranks]
    assert weights == sorted(weights, reverse=True)


def test_corroboration_raises_confidence_with_diminishing_returns() -> None:
    counts = (0, 1, 2, 3, 5)
    values = [
        score(authority="owner_extraction", corroborators=_classes(n, 1)).value  # type: ignore[union-attr]
        for n in counts
    ]
    assert values == sorted(values), "more agreement must not lower confidence"
    gains = [values[i + 1] - values[i] for i in range(len(values) - 1)]
    assert gains[0] > gains[1], "the second source must be worth less than the first"


def test_volume_never_beats_authority() -> None:
    """The property that keeps corroboration from becoming a way to launder weak
    sources into a strong score."""
    many_weak = score(authority="owner_extraction", corroborators=_classes(5, 5))
    two_strong = score(authority="owner_extraction", corroborators=_classes(2, 1))
    assert many_weak is not None and two_strong is not None
    assert many_weak.value < two_strong.value


def test_corroboration_is_bounded_below_the_ceiling() -> None:
    """Unbounded corroboration would let repetition reach certainty."""
    saturated = score(authority="owner_extraction", corroborators=_classes(500, 0))
    assert saturated is not None
    assert saturated.value < MAX_CONFIDENCE


def test_evidence_sharing_an_independence_key_counts_once() -> None:
    """Several turns of one conversation are one source however many rows they
    occupy. This is the requirement's own named case."""
    same = [EvidenceClass(key="session-1", group="actor-1", authority_rank=1) for _ in range(5)]
    mass, count = corroborating_mass(same)
    assert count == 1
    assert mass == CORROBORATION_WEIGHT_BY_RANK[1]


def test_one_actor_across_many_sessions_is_capped() -> None:
    """Separate sessions are separate occasions and do corroborate, but an agent
    re-observing a fact every session must not ratchet to the ceiling off one
    source."""
    many_sessions = [EvidenceClass(key=f"session-{i}", group="actor-1", authority_rank=1) for i in range(10)]
    _, count = corroborating_mass(many_sessions)
    assert count == 2


def test_different_actors_are_not_capped_together() -> None:
    """The cap is per source, not global. Ten independent people agreeing is real
    corroboration."""
    distinct = [EvidenceClass(key=f"session-{i}", group=f"actor-{i}", authority_rank=1) for i in range(10)]
    _, count = corroborating_mass(distinct)
    assert count == 10


def test_the_cap_keeps_the_strongest_evidence() -> None:
    """A cap that dropped whichever rows happened to be stored last would make
    the score depend on insertion order."""
    mixed = [
        EvidenceClass(key="a", group="actor-1", authority_rank=5),
        EvidenceClass(key="b", group="actor-1", authority_rank=0),
        EvidenceClass(key="c", group="actor-1", authority_rank=1),
    ]
    mass, count = corroborating_mass(mixed)
    assert count == 2
    assert mass == CORROBORATION_WEIGHT_BY_RANK[0] + CORROBORATION_WEIGHT_BY_RANK[1]


def test_an_unresolved_source_corroborates_nothing() -> None:
    """It has no subject to corroborate anything about."""
    mass, _ = corroborating_mass(_classes(3, 6))
    assert mass == 0.0


def test_corroboration_never_changes_the_authority_tier() -> None:
    """Independent sources agreeing tells you the claim is more likely correct. It
    does not tell you the claim came from somewhere it did not."""
    plain = score(authority="observer_inference")
    corroborated = score(authority="observer_inference", corroborators=_classes(5, 0))
    assert plain is not None and corroborated is not None
    assert plain.inputs.authority == corroborated.inputs.authority == "observer_inference"


# --- contradiction and confirmation --------------------------------------------


def test_a_disagreement_lowers_confidence() -> None:
    clean = score(authority="owner_extraction")
    contested = score(authority="owner_extraction", is_contested=True)
    assert clean is not None and contested is not None
    assert contested.value < clean.value


def test_a_disagreement_never_pushes_a_score_below_the_floor() -> None:
    """Repeated disagreement approaches the floor without crossing it, which is
    what keeps the stored value a valid upper bound for ageing."""
    contested = score(authority="observer_inference", is_contested=True)
    assert contested is not None
    assert contested.value >= DECAY_FLOOR


def test_confirmation_replaces_the_machine_estimate_rather_than_adjusting_it() -> None:
    """Averaging would make a confirmation worth less the weaker the original
    source was, which is backwards — the human looked at the claim, not at the
    source."""
    from_weak = score(authority="observer_inference", is_confirmed=True)
    from_strong = score(authority="owner_human", is_confirmed=True)
    assert from_weak is not None and from_strong is not None
    assert from_weak.value == from_strong.value


def test_a_confirmed_claim_reaches_the_confirmed_bucket() -> None:
    confirmed = score(authority="observer_inference", is_confirmed=True)
    assert confirmed is not None
    assert confirmed.bucket == BUCKET_CONFIRMED


def test_a_contested_confirmation_drops_out_of_the_confirmed_bucket() -> None:
    """A confirmed claim that is contested is not confirmed-and-uncontested, and
    the bucket should say so. This is why the penalty is applied last."""
    both = score(authority="owner_human", is_confirmed=True, is_contested=True)
    assert both is not None
    assert both.bucket != BUCKET_CONFIRMED


def test_the_disagreement_penalty_is_modest_on_purpose() -> None:
    """The contested mark carries the consequence — such a claim cannot be
    promoted and always needs review. A large score penalty on top would count
    the same fact twice."""
    clean = score(authority="owner_extraction")
    contested = score(authority="owner_extraction", is_contested=True)
    assert clean is not None and contested is not None
    assert contested.value > clean.value * 0.5


# --- the provider's own number --------------------------------------------------


def test_an_uncalibrated_provider_score_is_recorded_but_not_applied() -> None:
    """Nothing has checked what the number predicts, so using it would launder an
    unexamined figure into an authoritative-looking signal. Recorded because a
    mapping can only ever be fitted from raw scores paired with judged outcomes."""
    scored = score(authority="owner_inference", provider_confidence=0.99)
    baseline = score(authority="owner_inference")
    assert scored is not None and baseline is not None
    assert scored.value == baseline.value
    assert scored.inputs.provider_confidence == 0.99
    assert scored.inputs.provider_applied is False


def test_the_audit_record_states_that_the_provider_was_not_applied() -> None:
    """Positively, not by omission. An absent field reads as "fine"."""
    scored = score(authority="owner_inference", provider_confidence=0.99)
    assert scored is not None
    assert scored.inputs.as_json()["provider_applied"] is False


# --- auditability ---------------------------------------------------------------


@pytest.mark.parametrize("authority", list(BASE_CONFIDENCE_BY_AUTHORITY))
@pytest.mark.parametrize("contested", [False, True])
@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("corroborators", [0, 1, 3])
def test_a_stored_score_is_re_derivable_from_its_stored_inputs(
    authority: str, contested: bool, confirmed: bool, corroborators: int
) -> None:
    """The definition of auditable that matters: a reader takes the record beside
    a claim and arrives at the same number."""
    scored = score(
        authority=authority,
        corroborators=_classes(corroborators, 1),
        is_contested=contested,
        is_confirmed=confirmed,
    )
    assert scored is not None
    assert recompute(scored.inputs) == scored.value


def test_the_inputs_record_names_the_scorer() -> None:
    """Without it, a change to the arithmetic makes every historical score
    unreproducible and turns a calibration set into a mixture of numbers from
    different functions."""
    scored = score(authority="owner_human")
    assert scored is not None
    assert scored.inputs.scorer_version


# --- tenant policy --------------------------------------------------------------


def test_the_default_policy_is_the_shipped_weighting() -> None:
    """An absent policy row means defaults, so a tenant that configures nothing
    scores normally."""
    default = ConfidencePolicy()
    assert default.base_by_authority == BASE_CONFIDENCE_BY_AUTHORITY


def test_a_policy_may_move_the_weights() -> None:
    tighter = ConfidencePolicy(
        base_by_authority={
            "owner_human": 0.70,
            "owner_extraction": 0.60,
            "owner_inference": 0.50,
            "observer_human": 0.40,
            "observer_extraction": 0.30,
            "observer_inference": 0.20,
        }
    )
    scored = score(authority="owner_human", policy=tighter)
    assert scored is not None
    assert scored.inputs.base == 0.70


def test_a_policy_that_inverts_the_ladder_is_refused() -> None:
    """A configuration where a non-owner outranks an owner contradicts the rule
    that only owners assert authoritative facts — and would do so silently."""
    with pytest.raises(ValueError, match="strictly decrease"):
        ConfidencePolicy(
            base_by_authority={
                "owner_human": 0.30,
                "owner_extraction": 0.62,
                "owner_inference": 0.45,
                "observer_human": 0.42,
                "observer_extraction": 0.32,
                "observer_inference": 0.23,
            }
        )


def test_a_policy_with_two_equal_tiers_is_refused() -> None:
    """Equal bases make two tiers indistinguishable in the score while the ladder
    still orders them, so the two disagree about which is stronger."""
    with pytest.raises(ValueError, match="strictly decrease"):
        ConfidencePolicy(
            base_by_authority={
                "owner_human": 0.62,
                "owner_extraction": 0.62,
                "owner_inference": 0.45,
                "observer_human": 0.42,
                "observer_extraction": 0.32,
                "observer_inference": 0.23,
            }
        )


def test_every_scored_authority_tier_has_a_corroboration_weight() -> None:
    """A tier that can hold a claim but not corroborate one would silently
    contribute nothing when it agreed."""
    for tier in BASE_CONFIDENCE_BY_AUTHORITY:
        assert SOURCE_AUTHORITY_RANK[tier] in CORROBORATION_WEIGHT_BY_RANK


def test_the_score_is_bounded_for_every_reachable_combination() -> None:
    """Brute force over the inputs, because a bound argued rather than checked is
    a bound that eventually fails on an input nobody considered."""
    for authority in BASE_CONFIDENCE_BY_AUTHORITY:
        for n in (0, 1, 7):
            for rank in CORROBORATION_WEIGHT_BY_RANK:
                for contested in (False, True):
                    for confirmed in (False, True):
                        scored = score(
                            authority=authority,
                            corroborators=_classes(n, rank),
                            is_contested=contested,
                            is_confirmed=confirmed,
                        )
                        assert scored is not None
                        assert DECAY_FLOOR <= scored.value <= MAX_CONFIDENCE
                        assert not math.isnan(scored.value)
