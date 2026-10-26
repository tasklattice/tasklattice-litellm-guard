# Release test gate

The release workflow calls CI before either architecture can publish an image.
Tests exercise LiteLLM 1.106.4 with the baked Provider, not a mounted replacement.
The native dashboard production build includes TypeScript validation.

## Provider tests

The image builder executes all 47 tests in `litellm/v1.106.4/tests/`:

- Connection validation, strict configuration, admin-only management routes,
  credential encryption, rotation, deletion and initialization rollback.
- Real HTTP non-streaming allow, transform, policy block, outage fallback,
  authentication failure, missing credentials and per-request timeout settings.
- Ordered WebSocket delivery, buffering, backpressure, transformed output,
  malformed protocol frames, limits, upstream EOF, fail-closed behavior and
  safe error mapping. Tests use actual LiteLLM Router and proxy wrappers.
- First-frame cancellation and cleanup under AnyIO cancellation, including a
  Router generator which has acquired its connection but has not started.

```bash
docker build -t tali-litellm:test .
```

## Database and Guard integration

Guard is pinned to **v0.3.12**, commit
`2e95e8a3eef2933c023a5a5688c12b14bef57cc9`. The source revision and released
Controller/Runner image digests are recorded in `integration/guard-version.json`.
CI and the local script read the same dependency file. CI checks out the public
repository and installs its frozen `uv.lock` using Python 3.13. Local use
requires a clean checkout at the same commit:

```bash
# In tasklattice-guard:
uv sync --all-extras --frozen
# In this repository:
GUARD_CHECKOUT=../tasklattice-guard \
GUARD_TEST_RELAY_IMAGE=tali-litellm:test \
  bash scripts/test-guard-integration.sh
```

The gate includes:

- Released Guard v0.3.12 Controller and Runner containers with a fresh
  PostgreSQL database and test-only signing keys. The Runner's shipped
  `guardctl tenant provision` duplicates Default, selects a local keyword Policy,
  validates it, compiles and publishes a signed Artifact, creates an Endpoint
  and Router, and waits for the rollout. Repeating the command must reuse the
  same resources. No Guardrail, Artifact or runtime state is injected into the
  database by the test.
- The actual LiteLLM management API verifies that real Guard Endpoint and uses
  its CLI-issued credential. Tests cover failed verification, masked reads,
  encrypted database values, settings-only updates, forbidden generic
  create/edit, real Endpoint credential rotation/revocation and both deletion
  paths. Revoked credentials must stop inference before reaching the model;
  the new credential must restore both input and output protection.
- Real inference through the CLI-provisioned Guardrail: safe non-streaming and
  streaming completion, input rejection before any model request, output
  rejection, full-buffered streaming with zero blocked content released, and
  all these behaviors again after credential rotation.
- Real LiteLLM HTTP/SSE -> real Guard Runner -> real NeMo execution of frozen,
  signed artifacts. All three delivery modes run safe completion, late block,
  detector outage, cancellation before the first content frame and cancellation
  during output; full buffering additionally tests first-frame timeout. Actual
  upstream generator closure and bytes reaching the client are asserted.
- Guard's Runner API, artifact execution and TCP stream safety suites.

The combined gate runs 61 tests (including the CLI lifecycle/inference case),
in addition to the 47 Provider tests in the image build. Frozen artifact tests
remain for detailed fault/cancellation coverage alongside the fresh CLI path.

Only test containers, isolated Docker networks and temporary files are used.
No existing Guard configuration or Relay database is modified. Docker Desktop
uses its host routing; Linux CI uses host networking on ephemeral loopback
ports for the Runner stream tests. Management tests use a private bridge.

The CLI path uses real local keyword enforcement and a controlled business
model HTTP endpoint; it has no fake Guard verification peer or detector.
The frozen-artifact stream cases use controlled model and detector endpoints.
These tests verify integration and enforcement semantics, not live model
classification accuracy or every upstream LiteLLM provider. They require no
paid model keys. The controlled business model is a standalone fixture script.
