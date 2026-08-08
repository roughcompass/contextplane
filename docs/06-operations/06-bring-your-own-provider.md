# Bring Your Own Extraction Provider

How to point session extraction at your own model endpoint — an internal gateway, a self-hosted model, or a vendor this product has never heard of. Four paths, in increasing order of effort. For what extraction is and how strategies work, see [session extraction](../04-guides/05-session-extraction.md); for every variable named here, see the [configuration reference](../05-reference/03-configuration.md).

---

## Decide which path you need

| Your endpoint | Path |
|---|---|
| Anthropic's API, directly | `EXTRACTION_PROVIDER=anthropic` and a key. Done — see the [guide](../04-guides/05-session-extraction.md). |
| A gateway that re-serves an Anthropic- or OpenAI-shaped API | Keep the matching built-in adapter; point it at the gateway with the transport settings below. |
| Anything speaking the OpenAI chat-completions dialect — vLLM, Ollama, LiteLLM, Azure, Groq, Together, most internal gateways | `EXTRACTION_PROVIDER=openai` plus the transport settings. |
| A wire format neither adapter speaks | Write an adapter, ship it as a Python package with an entry point, run the contract suite in its CI. The rest of this page. |

Prefer the OpenAI-compatible adapter over a bespoke one whenever the endpoint offers that dialect, even loosely: the adapter already forces structured output through a required tool call, refuses free-form text, accounts usage honestly, and is exercised by this repo's own test suite on every commit. A bespoke adapter re-earns those properties by hand.

## Point an existing adapter at a compatible gateway

Four settings define the transport, whichever provider is selected. Unset, each one means "the adapter's own default", which is why an unconfigured deployment keeps working unchanged.

```bash
EXTRACTION_PROVIDER=openai
EXTRACTION_BASE_URL=https://llm-gateway.internal/v1
EXTRACTION_API_KEY=<credential>            # secret; see routing note below
EXTRACTION_AUTH_HEADER=Authorization       # header the credential travels in
EXTRACTION_AUTH_TEMPLATE=Bearer {key}      # how it is spelled inside that header
EXTRACTION_EXTRA_HEADERS=X-Team:platform   # anything else the gateway requires; secret
```

Rules the settings enforce at startup, not at first use:

- `EXTRACTION_AUTH_TEMPLATE` must contain `{key}` exactly once. Zero occurrences means a credential was pasted into a non-secret setting bound for a ConfigMap; the app refuses to boot rather than let it ship.
- `EXTRACTION_BASE_URL` may not carry userinfo (`https://user:secret@…`) for the same reason.
- `EXTRACTION_EXTRA_HEADERS` may not set the auth header a second time, nor any header the transport owns (`Host`, `Content-Type`, `Content-Length`, `Transfer-Encoding`, `Connection`). `anthropic-version` is deliberately overridable — pinning a vendor API version is a legitimate use.
- Redirects are never followed. A compromised gateway answering `302` would otherwise be handed the credential on the next hop.

Two operational warnings, both easy to underestimate:

- **A base-URL change redirects every tenant at once.** Extraction is deployment-wide; there is no per-tenant endpoint. Treat `EXTRACTION_BASE_URL` as change-controlled: review it like a firewall rule, not a tuning knob.
- **`HTTPS_PROXY` in the process environment silently re-routes extraction traffic.** The HTTP client honours the standard proxy variables. If the platform injects a proxy for egress control, extraction calls go through it — including to an internal gateway you expected to reach directly. Check the process environment before debugging "the gateway never saw the request".

## Write an adapter

An adapter is a class satisfying the extraction provider protocol: a `provider_id`, a `default_model_id`, and an `async extract(request)` returning candidates with honest usage accounting. The smallest complete example ships in this repository at [`tests/fixtures/extraction_thirdparty/acme_extraction/`](../../tests/fixtures/extraction_thirdparty/acme_extraction/__init__.py) — deliberately minimal, so what the contract actually costs an implementer is visible.

The division of enforcement is the part to internalise before writing code:

**The platform enforces these on every provider's output, yours included.** Nothing an adapter does can opt out — the staging path checks each candidate before it becomes a claim:

- **Citation** — every candidate must cite event IDs from the batch the provider was given. A citation outside the batch is treated as fabrication and refused.
- **Boundary forgery** — event bodies are wrapped in a per-request delimiter (`request.boundary`); output reproducing that delimiter is refused as an escape attempt.
- **Scalar values** — a candidate whose value is a list or object is refused before any content check reads it.
- **PII** — values and excerpts are scanned on the way out, not only on the way in.

**These are the adapter's obligations, and only the contract suite verifies them:**

- Force structured output (a required tool/function call, or your wire format's equivalent) and refuse free-form prose rather than parse it.
- Build the prompt through the request's containment fields — wrap event bodies with `request.boundary`, never a delimiter of your own. An adapter that mints its own is checked against a string it never used, which reads as a working defence and is not one.
- Account usage honestly: report all token counts or none, and report *unknown* as absent, never as zero.
- Classify errors with `is_retriable` set the way the drain expects — retries are the drain's job, not the adapter's.

That split is why running the suite in the adapter's CI is a deployment requirement, not a suggestion: the platform cannot see an adapter that quietly stopped forcing structured output, but the suite fails on it.

## Declare the entry point

Context Plane discovers adapters from installed distributions — no fork, no registration call:

```toml
# pyproject.toml of your adapter package
[project.entry-points."contextplane.extraction_providers"]
acme = "acme_extraction:build"
```

`build` is a callable taking `Settings` and returning the provider. The rules Context Plane enforces at startup:

- The entry-point **name is the selector** an operator sets (`EXTRACTION_PROVIDER=acme`) and must be lowercase token characters. The constructed provider's `provider_id` must match it.
- Duplicate names — including shadowing a built-in like `openai` — fail startup naming both distributions. Install order never decides who receives the credential.
- Discovery reads metadata only; **your package is imported only when selected**. A `noop` deployment executes none of your code.
- If the selected provider fails to import or build, startup fails loudly. It never falls back to `noop` — a fallback would look exactly like a working deployment that produces no claims.

## Plugin-owned configuration

Variables your adapter reads itself follow the `EXTRACTION_PLUGIN_*` naming convention (for example `EXTRACTION_PLUGIN_ACME_REGION`). The convention marks ownership: this repository's configuration gates neither validate nor document those names, and nothing here will catch a typo in one — your adapter should fail loudly on its own missing config at build time, for the same reason Context Plane fails loudly on a missing provider.

## Run the contract suite before deploying

The suite ships in the product package; your CI needs the package plus the test runner:

```bash
pip install "registry[contract-suite]"
```

```python
# test_acme_contract.py — in your adapter's repository
from contextplane.extraction.contract_suite import NetworkedExtractionProviderContract

from acme_extraction import build


class TestAcmeProviderContract(NetworkedExtractionProviderContract):
    @staticmethod
    def make_provider():
        return build(settings_for_tests())
```

Subclass it; do not copy it. A subclass inherits every check, including the ones added after you shipped — which is the point. Use `NetworkedExtractionProviderContract` for a model-backed adapter (it adds forced-structured-output, containment, and error-taxonomy checks); `ExtractionProviderContract` alone is the in-process tier used by providers with no model behind them. Wire the suite into the adapter's CI so it runs on every change to the adapter, not once at adoption.

Before first production traffic, run one end-to-end check against the real gateway from a staging deployment: set the transport variables, drive one session through extraction, and confirm a claim lands. The suite proves the adapter's contract; only a live call proves the gateway's.
