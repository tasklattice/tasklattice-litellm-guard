# TaskLattice LiteLLM Guard

LiteLLM with the **TaskLattice Guard** Guardrail Provider. This repository is the
single source of the Provider and publishes one image:

```text
ghcr.io/tasklattice/tali-litellm:<litellm-version>-guard.<n>
```

Both TaskLattice Relay (as its model gateway) and TaskLattice Guard (as its
LiteLLM integration test gateway) consume this image by pinned tag. Neither
repository carries a copy of the Provider.

## Layout

| Path | Purpose |
| --- | --- |
| `litellm/v<version>/` | Reviewed overlay for exactly one upstream LiteLLM release: Provider source, `apply-overlay.py`, `verify-overlay.py`, Provider tests and its [design notes](litellm/v1.106.4/README.md) |
| `Dockerfile` | Applies the overlay to `ghcr.io/berriai/litellm-database:v<version>`, verifies it, runs the Provider tests and rebuilds the dashboard |
| `scripts/check-release-tag.sh` | Release tag validation shared by CI and the release workflow |

Supported LiteLLM release: **1.106.4**. Only this reviewed version is retained.

## Versioning

Image tags carry two axes: the upstream LiteLLM release and the Provider
revision for that release, `1.106.4-guard.1`, `1.106.4-guard.2`, ... A tag is never
overwritten. There is no `latest`; consumers pin an exact tag.

Each image also carries labels that consumers check before use:

| Label | Meaning |
| --- | --- |
| `io.tasklattice.litellm.version` | Upstream LiteLLM release |
| `io.tasklattice.guard.provider-version` | Provider revision `n` |
| `io.tasklattice.guard.output-stream-protocol` | Guard output-stream WebSocket protocol version spoken by the Provider (`start`/`ready` frame `version`) |

Guard's runtime protocol is owned by TaskLattice Guard. When Guard changes the
output-stream protocol, this repository publishes a new `-guard.N` with the
matching label, and Guard's compatibility table names the minimum Provider tag.

## Build locally

```bash
docker build -t ghcr.io/tasklattice/tali-litellm:dev .
# Builder stage only: apply + verify + Provider unit tests, no image
docker build --target tasklattice-litellm-builder .
```

The first build recompiles the LiteLLM dashboard and takes several minutes.

## Release

Push a tag `v<litellm-version>-guard.<n>` from `main`:

```bash
git tag v1.106.4-guard.1 && git push origin v1.106.4-guard.1
```

`.github/workflows/release.yml` validates the tag against `litellm/v<version>`,
runs the complete [release test gate](docs/testing.md), builds amd64 and arm64
images, publishes the multi-architecture manifest and creates a GitHub release.
Publishing cannot start until the test gate passes. It refuses to overwrite an
existing tag.

## Supporting a new LiteLLM release

1. Copy `litellm/v<old>` to `litellm/v<new>` and review every patch in
   `apply-overlay.py` against the new upstream tree; it refuses unexpected
   source.
2. Replace the CI matrix entry and update `LITELLM_VERSION`, the upstream image
   digest, and the source archive checksum in `Dockerfile`. Rebuild the native UI
   and run the Provider tests against that exact runtime.
3. Remove the previous version directory and release `v<new>-guard.1`. This
   repository supports only the current reviewed release.
