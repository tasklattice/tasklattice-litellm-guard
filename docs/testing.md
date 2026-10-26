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

Guard is pinned to commit `2e95e8a3eef2933c023a5a5688c12b14bef57cc9`.
CI checks out that public repository and installs its frozen `uv.lock` using
Python 3.13. Local use requires a clean checkout at the same commit:

```bash
# In tasklattice-guard:
uv sync --all-extras --frozen
# In this repository:
GUARD_CHECKOUT=../tasklattice-guard \
GUARD_TEST_RELAY_IMAGE=tali-litellm:test \
  bash scripts/test-guard-integration.sh
```

The gate includes:

- An isolated PostgreSQL 17 and the actual LiteLLM management API: failed
  verification, masked reads, encrypted database values, settings-only update,
  failed and successful secret rotation, forbidden generic create/edit, and
  both standard and Provider-specific deletion with credential cleanup.
- Real LiteLLM HTTP/SSE -> real Guard Runner -> real NeMo execution of frozen,
  signed artifacts. All three delivery modes run safe completion, late block,
  detector outage, cancellation before the first content frame and cancellation
  during output; full buffering additionally tests first-frame timeout. Actual
  upstream generator closure and bytes reaching the client are asserted.
- Guard's Runner API, artifact execution and TCP stream safety suites.

Only test containers, an isolated Docker network and temporary files are used.
No existing Guard configuration or Relay database is modified. Docker Desktop
uses its host routing; Linux CI uses host networking on ephemeral loopback
ports for the Runner stream tests. Management tests use a private bridge.

The model and detector HTTP endpoints are controlled fixtures. These tests
verify integration and enforcement semantics, not live model classification
accuracy or every upstream LiteLLM provider. They require no paid model keys.
