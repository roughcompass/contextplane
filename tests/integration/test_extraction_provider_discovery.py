"""Discovery, proved against real packaging rather than a patched lookup.

A registry only ever populated by first-party adapters imported from the same
package has not been shown to work for the case it was built for. Until an
adapter that lives outside this repo is actually found, loaded and refused on
its merits, the corporate-provider path is a claim -- and the first organisation
to try it is the one that discovers it does not work.

**`entry_points` is never monkeypatched here.** Patching it would assert that
the code calls a function, which nobody doubted. What is in question is whether
a package installed the ordinary way is visible to it: whether the `.dist-info`
directory, the `entry_points.txt` inside it, the group name and the
`module:attribute` target line up well enough for `importlib.metadata` to hand
back something loadable. Only real metadata on a real `sys.path` answers that.

So each test materialises a fixture package into `tmp_path` alongside a
generated `.dist-info`, puts that directory on `sys.path`, and takes it off
again afterwards. Nothing is installed into the developer's virtualenv, and
nothing leaks into a sibling test -- the discovery cache is reset on the way in
and on the way out, because it is process-global and a stale entry would make
the next test pass for the wrong reason.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from registry.config import Settings
from registry.extraction import provider_registry
from registry.extraction.contract_suite import ExtractionProviderContract
from registry.extraction.provider_registry import (
    ENTRY_POINT_GROUP,
    ProviderDiscoveryError,
    build_third_party,
    is_third_party,
    provider_names,
    reset_discovery_cache,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "extraction_thirdparty"


def _settings() -> Settings:
    url = "postgresql+asyncpg://x/y"
    return Settings(database_url=url, pgbouncer_url=url, scheduler_jobstore_url=url)


def _write_dist_info(site: Path, *, distribution: str, version: str, entry_points: str) -> None:
    """Generate the metadata a real install would have left behind.

    Deliberately hand-written rather than produced by a build backend. What is
    being tested is that discovery reads *metadata on disk*, and the smallest
    honest version of that is the three files `importlib.metadata` actually
    consults: `METADATA` for the name and version it reports, `entry_points.txt`
    for the group, and `RECORD` so the directory is a well-formed distribution
    rather than a suggestive-looking folder.
    """
    dist_info = site / f"{distribution}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(entry_points, encoding="utf-8")
    (dist_info / "RECORD").write_text("", encoding="utf-8")


@pytest.fixture
def install(tmp_path: Path):
    """Put a fixture package on `sys.path` as though it had been pip-installed.

    The discovery cache is reset on the way in and on the way out. It is
    process-global, so a stale one would let a later test see a provider that is
    no longer on the path -- which passes, and proves the opposite of what it
    claims.
    """
    created: list[Path] = []

    def _install(package: str, *, distribution: str, selector: str, attribute: str = "build") -> Path:
        site = tmp_path / f"site-{selector}"
        site.mkdir(parents=True, exist_ok=True)
        shutil.copytree(_FIXTURES / package, site / package)
        _write_dist_info(
            site,
            distribution=distribution,
            version="1.0.0",
            entry_points=f"[{ENTRY_POINT_GROUP}]\n{selector} = {package}:{attribute}\n",
        )
        sys.path.insert(0, str(site))
        created.append(site)
        reset_discovery_cache()
        return site

    try:
        yield _install
    finally:
        for site in created:
            if str(site) in sys.path:
                sys.path.remove(str(site))
        # Modules imported from the scratch path must go too, or a later test
        # importing the same name gets the copy from a directory that no longer
        # exists on the path.
        for name in ("acme_extraction", "acme_mismatch", "acme_broken"):
            sys.modules.pop(name, None)
        reset_discovery_cache()


# --- The happy path -----------------------------------------------------------


def test_an_installed_adapter_becomes_a_selectable_provider(install) -> None:
    """The whole point: a package this repo has never heard of turns into a
    legal value for `EXTRACTION_PROVIDER`, discovered from its metadata."""
    install("acme_extraction", distribution="acme-extraction", selector="acme")

    assert "acme" in provider_names()
    assert is_third_party("acme")


def test_the_discovered_adapter_loads_and_builds(install) -> None:
    install("acme_extraction", distribution="acme-extraction", selector="acme")

    provider = build_third_party("acme", _settings())

    assert provider.provider_id == "acme"
    assert callable(provider.extract)


def test_discovery_does_not_import_the_package_to_find_it(install) -> None:
    """Enumeration reads metadata; only the *selected* provider is imported.

    A registry that imported every installed adapter to list them would run
    third-party code on every startup, including the code of adapters nobody
    selected.
    """
    install("acme_extraction", distribution="acme-extraction", selector="acme")
    sys.modules.pop("acme_extraction", None)

    assert "acme" in provider_names()
    assert "acme_extraction" not in sys.modules, "listing providers imported one"

    build_third_party("acme", _settings())
    assert "acme_extraction" in sys.modules, "building the selected provider did not import it"


def test_a_deployment_with_nothing_installed_sees_only_the_built_ins() -> None:
    """The negative control. Without it, every assertion above could be passing
    because the names were built in all along."""
    reset_discovery_cache()

    names = provider_names()

    assert "acme" not in names
    assert names == provider_registry.BUILT_IN_PROVIDERS


# --- The smoke-check rejection path -------------------------------------------


def test_an_adapter_whose_declared_id_disagrees_is_refused(install) -> None:
    """Installs cleanly, imports cleanly, builds cleanly, and is still wrong.

    The selector names the metric label; `provider_id` is a persisted
    calibration key. A deployment running this would file its call counts under
    `mismatch` and its calibration rows under `acme`, and nothing would join
    them again. Nothing short of this check notices.
    """
    install("acme_mismatch", distribution="acme-mismatch", selector="mismatch")

    with pytest.raises(ProviderDiscoveryError) as caught:
        build_third_party("mismatch", _settings())

    message = str(caught.value)
    assert "mismatch" in message
    assert "acme" in message, "the refusal must name what was declared, not only that it was wrong"


def test_the_refusal_names_the_distribution_that_supplied_it(install) -> None:
    """An operator reading this has to know which package to go and fix, and
    the selector alone does not say -- that is the thing that was wrong."""
    install("acme_mismatch", distribution="acme-mismatch", selector="mismatch")

    with pytest.raises(ProviderDiscoveryError) as caught:
        build_third_party("mismatch", _settings())

    assert "acme-mismatch" in str(caught.value)


# --- The failed-import path ---------------------------------------------------


def test_an_adapter_that_raises_at_import_is_fatal(install) -> None:
    """Fatal rather than a fallback to no extraction.

    Falling back would leave a deployment that asked for a model producing
    nothing while reporting healthy, which is indistinguishable from a working
    deployment whose sessions contain nothing to extract.
    """
    install("acme_broken", distribution="acme-broken", selector="broken")

    with pytest.raises(ProviderDiscoveryError) as caught:
        build_third_party("broken", _settings())

    message = str(caught.value)
    assert "broken" in message
    assert "ImportError" in message, "the refusal must carry what actually went wrong"
    assert "libacme" in message, "the adapter's own explanation has to survive to the operator"


def test_a_broken_adapter_is_still_discovered(install) -> None:
    """Discovery reads metadata, so a package that cannot import is still
    listed. Selecting it is what fails, and it fails loudly."""
    install("acme_broken", distribution="acme-broken", selector="broken")

    assert "broken" in provider_names()


# --- The fixture adapter against the shipped contract -------------------------


class TestAcmeAdapterContract(ExtractionProviderContract):
    """The fixture doubles as the worked example, so it meets the real contract.

    An example that passed nothing would teach an implementer the wrong shape.
    This runs the in-process tier -- the adapter calls no network, so the
    networked tier would fail on facts that are correct for what it is.

    Imported directly rather than through discovery: this asserts the adapter is
    a good adapter, which is a different question from whether packaging works,
    and mixing the two would leave neither clearly tested.
    """

    @staticmethod
    def make_provider():
        sys.path.insert(0, str(_FIXTURES))
        try:
            from acme_extraction import build
        finally:
            sys.path.remove(str(_FIXTURES))
        return build(None)
