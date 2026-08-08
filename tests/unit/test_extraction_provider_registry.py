"""Bringing a provider this codebase has never heard of.

These tests are about the registry's *policy*: which names are legal, what
happens when two packages claim one, and what a broken supplier does to
startup. They drive discovery through a substituted `entry_points`, which is
the right level for policy and the wrong level for packaging -- it would prove
nothing about whether a real `.dist-info` is found. The proof that real
packaging works is a separate test that materialises an actual distribution.

The recurring assertion is that nothing here fails quietly. Every broken
supplier raises, because the alternative -- skip it and carry on -- yields a
deployment that reports healthy and extracts nothing, and an operator who goes
looking for the fault in extraction rather than in an install.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from contextplane.config import Settings
from contextplane.extraction.factory import build_provider
from contextplane.extraction.local_rules import LocalRulesProvider
from contextplane.extraction.provider import ExtractionRequest, ExtractionResult, NoOpProvider
from contextplane.extraction.provider_registry import (
    BUILT_IN_PROVIDERS,
    ENTRY_POINT_GROUP,
    ProviderDiscoveryError,
    build_third_party,
    discovered_providers,
    provider_names,
    reset_discovery_cache,
)


@pytest.fixture(autouse=True)
def _forget_discovery() -> Iterator[None]:
    """Discovery is cached for the process, so each test starts from nothing."""
    reset_discovery_cache()
    yield
    reset_discovery_cache()


def _settings(provider: str) -> Settings:
    """Settings naming *provider*, including names `Settings` will not validate.

    Construction still validates the selector against a frozen literal set, so
    a supplied name is assigned afterwards rather than passed in. That is the
    gap this registry exists to close and a separate task closes: until the
    selector is validated against these names instead of a literal set, a
    deployment cannot actually reach a provider it installed. Nothing about
    what is asserted below depends on which of the two writes the field.
    """
    url = "postgresql+asyncpg://x/y"
    settings = Settings(
        database_url=url,
        pgbouncer_url=url,
        scheduler_jobstore_url=url,
        extraction_provider="noop",
    )
    settings.extraction_provider = provider
    return settings


class _Distribution:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version


class _Point:
    """An entry point, with the three things the registry actually reads."""

    def __init__(self, name: str, *, dist: _Distribution | None = None, loads: Any = None) -> None:
        self.name = name
        self.dist = dist
        self._loads = loads
        self.loaded = False

    def load(self) -> Any:
        self.loaded = True
        if isinstance(self._loads, Exception):
            raise self._loads
        return self._loads


class _SuppliedProvider:
    """The shape a third party is expected to hand back."""

    def __init__(self, provider_id: str = "acme") -> None:
        self.provider_id = provider_id

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:  # pragma: no cover
        raise NotImplementedError


def _install(monkeypatch: pytest.MonkeyPatch, *points: _Point) -> None:
    monkeypatch.setattr(
        "contextplane.extraction.provider_registry.entry_points",
        lambda group: list(points) if group == ENTRY_POINT_GROUP else [],
    )


def _acme(provider_id: str = "acme") -> _Point:
    return _Point(
        "acme",
        dist=_Distribution("acme-extraction", "2.1.0"),
        loads=lambda settings: _SuppliedProvider(provider_id),
    )


# --- what a supplied provider gets -------------------------------------------


def test_an_installed_provider_becomes_a_selectable_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _acme())
    assert "acme" in provider_names()
    assert BUILT_IN_PROVIDERS <= provider_names()


def test_the_factory_builds_a_provider_it_has_never_heard_of(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the task: a name from outside this repo resolves to
    running code, through the same call every built-in goes through."""
    _install(monkeypatch, _acme())
    provider = build_provider(_settings("acme"))
    assert isinstance(provider, _SuppliedProvider)


def test_discovery_reads_names_without_importing_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment running noop executes no third-party code at all. Reading a
    name out of installed metadata must not be what imports it."""
    point = _acme()
    _install(monkeypatch, point)

    assert "acme" in provider_names()
    assert isinstance(build_provider(_settings("noop")), NoOpProvider)

    assert not point.loaded


def test_only_the_selected_provider_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two installed suppliers, one selected. The other must not be imported --
    it is code from a package this deployment chose not to run."""
    selected = _acme()
    bystander = _Point("other", dist=_Distribution("other-pkg", "1.0"), loads=ImportError("boom"))
    _install(monkeypatch, selected, bystander)

    build_provider(_settings("acme"))

    assert selected.loaded
    assert not bystander.loaded


def test_the_supplying_distribution_is_recorded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Which package supplied the code holding the credential is the first
    question asked when a third-party provider misbehaves."""
    _install(monkeypatch, _acme())
    with caplog.at_level("INFO"):
        build_provider(_settings("acme"))
    assert "acme-extraction 2.1.0" in caplog.text


# --- names are the key, provider_id is not -----------------------------------


def test_the_local_selector_keeps_its_differing_provider_id() -> None:
    """`local` builds a provider calling itself `local-rules`, and that stays
    true. provider_id is a persisted calibration key; aligning it with the
    selector would silently repartition mappings already published."""
    provider = build_provider(_settings("local"), env={})
    assert isinstance(provider, LocalRulesProvider)
    assert provider.provider_id == "local-rules"


def test_a_supplied_provider_whose_id_disagrees_with_its_name_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Held to a stricter rule than the grandfathered built-in. The selector
    names the metric label and provider_id is the calibration key: when a new
    provider makes those two differ, nothing joins them again afterwards."""
    _install(monkeypatch, _acme(provider_id="acme-labs-v2"))
    with pytest.raises(ProviderDiscoveryError, match="persisted calibration key"):
        build_provider(_settings("acme"))


# --- collisions are refused, not resolved ------------------------------------


def test_shadowing_a_built_in_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise install order decides which code receives the credential and
    every tenant's session text."""
    shadow = _Point("anthropic", dist=_Distribution("sneaky", "0.1"), loads=lambda settings: None)
    _install(monkeypatch, shadow)
    with pytest.raises(ProviderDiscoveryError, match="already ships"):
        provider_names()


def test_two_distributions_claiming_one_name_are_refused_naming_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message has to name both, or the operator cannot tell which install
    to undo."""
    first = _Point("acme", dist=_Distribution("acme-extraction", "2.1.0"), loads=lambda settings: None)
    second = _Point("acme", dist=_Distribution("acme-fork", "0.9.0"), loads=lambda settings: None)
    _install(monkeypatch, first, second)

    with pytest.raises(ProviderDiscoveryError) as exc:
        provider_names()

    assert "acme-extraction 2.1.0" in str(exc.value)
    assert "acme-fork 0.9.0" in str(exc.value)


@pytest.mark.parametrize("name", ["Acme", "ACME", "acme provider", "acme/v2", ""])
def test_a_name_the_selector_could_never_match_is_refused(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """The selector is lowercased before it is matched, so an uppercase entry
    would install cleanly and be unreachable -- a provider that looks present
    and cannot be selected is worse than one that refuses to install."""
    _install(monkeypatch, _Point(name, dist=_Distribution("odd", "1.0"), loads=lambda settings: None))
    with pytest.raises(ProviderDiscoveryError):
        provider_names()


# --- broken suppliers fail loudly --------------------------------------------


def test_a_failed_import_is_fatal_rather_than_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is the selected provider. Falling back to no extraction would report
    healthy while producing nothing."""
    _install(monkeypatch, _Point("acme", dist=_Distribution("acme-extraction", "2.1.0"), loads=ImportError("no")))
    with pytest.raises(ProviderDiscoveryError, match="failed to import"):
        build_provider(_settings("acme"))


def test_an_entry_point_that_is_not_callable_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Point("acme", dist=_Distribution("acme-extraction", "2.1.0"), loads="not a function"))
    with pytest.raises(ProviderDiscoveryError, match="not a callable"):
        build_provider(_settings("acme"))


def test_a_builder_taking_no_settings_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Checked before it is called, because the TypeError a bad call raises
    names an argument count rather than the packaging mistake behind it."""
    _install(monkeypatch, _Point("acme", dist=_Distribution("acme-extraction", "2.1.0"), loads=lambda: None))
    with pytest.raises(ProviderDiscoveryError, match="no positional argument"):
        build_provider(_settings("acme"))


def test_an_object_that_is_not_a_provider_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the first thing anyone notices is a queue that stopped
    draining, a long way from the install that caused it."""
    _install(
        monkeypatch, _Point("acme", dist=_Distribution("acme-extraction", "2.1.0"), loads=lambda settings: object())
    )
    with pytest.raises(ProviderDiscoveryError, match="extract"):
        build_provider(_settings("acme"))


def test_unreadable_metadata_fails_loudly_rather_than_emptying_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupted `.dist-info` anywhere in the environment breaks enumeration
    itself. Returning nothing would report the provider as not installed, which
    sends the operator to entirely the wrong place."""

    def _explode(group: str) -> list[_Point]:
        raise ValueError("bad metadata in some unrelated package")

    monkeypatch.setattr("contextplane.extraction.provider_registry.entry_points", _explode)

    with pytest.raises(ProviderDiscoveryError, match="unreadable"):
        provider_names()


# --- discovery cost ----------------------------------------------------------


def test_metadata_is_enumerated_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Settings()` is constructed per instance across the test suite, and
    re-reading every installed package each time is pure cost."""
    calls = 0

    def _counted(group: str) -> list[_Point]:
        nonlocal calls
        calls += 1
        return [_acme()]

    monkeypatch.setattr("contextplane.extraction.provider_registry.entry_points", _counted)

    provider_names()
    provider_names()
    discovered_providers()

    assert calls == 1


# --- the unknown name ---------------------------------------------------------


def test_an_unknown_name_names_the_legal_ones_and_the_entry_point_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two ways out, both actionable: pick an installed name, or learn where a
    supplied one has to be declared."""
    _install(monkeypatch)
    with pytest.raises(ValueError, match=ENTRY_POINT_GROUP):
        build_provider(_settings("typo"))


def test_building_a_name_that_was_never_discovered_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    with pytest.raises(ProviderDiscoveryError, match="not an installed third-party provider"):
        build_third_party("acme", _settings("acme"))
