"""The saved prompt's shape and the wire contract's, held equal.

E22-T15. `PromptRequestV1` duplicates `ContextResolveRequest` on purpose: `api`
sits above `context` in this package's layering, so the service that stores a
prompt cannot import the wire model, and a lower layer reaching upward is what
stops the resolver being usable by a transport nobody has written yet.

**This file is the only reason that duplication is safe.** Without it the two
would be a second definition nobody reconciles, and the way it goes wrong is
specific and quiet: the contract gains a field, prompts silently cannot use it,
and every saved prompt resolves as though the caller had not asked. That is
exactly how `instruction_digest` would have been lost had it landed a wave
later.

So the check runs in both directions and neither is optional. A field on the
wire and not here is a capability prompts lost. A field here and not on the wire
is a shape the resolver has no parameter for, which fails at run time rather
than at the moment somebody saved it.
"""

from __future__ import annotations

import dataclasses

from contextplane.api.schemas.context import DEFAULT_ARM_LIMIT, MAX_ARM_LIMIT, ContextResolveRequest
from contextplane.context.evaluation import prompt_request
from contextplane.context.evaluation.prompt_request import PromptRequestV1

#: Wire fields a saved prompt deliberately does not carry, each with the reason.
#: Empty today. An entry here is a claim that a caller can express something a
#: prompt cannot, which is a decision rather than an omission.
_NOT_SAVEABLE: dict[str, str] = {}


def _wire_fields() -> set[str]:
    return set(ContextResolveRequest.model_fields)


def _saved_fields() -> set[str]:
    return {field.name for field in dataclasses.fields(PromptRequestV1)}


def test_every_field_a_caller_can_send_is_one_a_prompt_can_save() -> None:
    """The direction that loses a capability silently.

    A field on the wire and not here means a saved prompt resolves as though the
    caller had not asked for it — on every run, with nothing saying so.
    """
    missing = sorted(_wire_fields() - _saved_fields() - set(_NOT_SAVEABLE))

    assert not missing, (
        f"`ContextResolveRequest` carries {missing} and a saved prompt cannot. Add the field to "
        "`PromptRequestV1`, or record here why a prompt may not express it."
    )


def test_every_field_a_prompt_saves_is_one_the_contract_has() -> None:
    """The other direction, which fails later and reads as a system fault.

    A field here and not on the wire is a shape the resolver has no parameter
    for. The run records it as a failure, which points at the service rather than
    at the prompt.
    """
    extra = sorted(_saved_fields() - _wire_fields())

    assert not extra, f"`PromptRequestV1` carries {extra}, which `ContextResolveRequest` does not"


def test_the_bounds_are_the_same_number() -> None:
    """A prompt the contract would refuse must be refused when it is saved.

    Two limits that disagree produce a set whose prompts pass validation and then
    fail every run, which is the worst available combination: legible at save
    time, illegible afterwards.
    """
    assert prompt_request.MAX_ARM_LIMIT == MAX_ARM_LIMIT
    assert prompt_request.DEFAULT_ARM_LIMIT == DEFAULT_ARM_LIMIT


def test_the_exclusion_list_is_empty_or_explained() -> None:
    """Anti-vacuity for `_NOT_SAVEABLE`.

    An exclusion with no reason is an assertion nobody has to defend, and this
    file's whole value is that the duplication has to be argued for each time it
    widens.
    """
    unexplained = sorted(name for name, reason in _NOT_SAVEABLE.items() if not reason.strip())

    assert not unexplained, f"these exclusions carry no reason: {unexplained}"
    assert (
        set(_NOT_SAVEABLE) <= _wire_fields()
    ), "an exclusion names a field the contract does not have, so it is protecting nothing"
