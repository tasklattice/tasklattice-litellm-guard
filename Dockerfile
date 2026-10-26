# LiteLLM with the TaskLattice Guard Guardrail Provider.
ARG LITELLM_VERSION=1.106.4
ARG LITELLM_IMAGE=ghcr.io/berriai/litellm-database:v${LITELLM_VERSION}@sha256:db36453d25e76c9eb689d7d51b8e5fa48843243d7a86ed1284cf574ce0107b3f
ARG UI_BUILD_IMAGE=node:24.19-alpine3.24@sha256:d32cdf619f63fe0471182d08996dd516c6275bb5fd31ae06e55a570bd9e1ad43

FROM ${UI_BUILD_IMAGE} AS tasklattice-litellm-ui
ARG LITELLM_VERSION
RUN apk add --no-cache python3
WORKDIR /app
# Upstream's runtime image no longer includes the UI source. The matching
# release source is used only in this build stage and never enters the runtime.
ADD --checksum=sha256:dc58da933f25e8efec12348a4821b71e15e358d97093821576073fee37c49589 https://codeload.github.com/BerriAI/litellm/tar.gz/refs/tags/v${LITELLM_VERSION} /tmp/upstream.tar.gz
RUN tar -xzf /tmp/upstream.tar.gz --strip-components=1 -C /app && rm /tmp/upstream.tar.gz
RUN --mount=type=cache,target=/root/.npm \
    cd /app/ui/litellm-dashboard && npm ci --ignore-scripts --no-audit --no-fund
COPY litellm/v${LITELLM_VERSION} /tmp/tasklattice-litellm-overlay
ENV TASKLATTICE_LITELLM_SOURCE_VERSION=${LITELLM_VERSION} \
    NEXT_TELEMETRY_DISABLED=1
RUN python3 /tmp/tasklattice-litellm-overlay/apply-overlay.py --root /app \
    && python3 /tmp/tasklattice-litellm-overlay/verify-overlay.py --root /app --files-only
RUN cd /app/ui/litellm-dashboard && npm run build

FROM ${LITELLM_IMAGE} AS tasklattice-litellm-builder
ARG LITELLM_VERSION
USER root
COPY --from=tasklattice-litellm-ui /app/litellm /app/litellm
COPY --from=tasklattice-litellm-ui /app/ui/litellm-dashboard/src /app/ui/litellm-dashboard/src
COPY litellm/v${LITELLM_VERSION} /tmp/tasklattice-litellm-overlay
RUN LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH=/app \
    python /tmp/tasklattice-litellm-overlay/verify-overlay.py --root /app \
    && LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH=/app \
    python -m unittest discover -s /tmp/tasklattice-litellm-overlay/tests \
    && rm -rf /app/litellm/proxy/_experimental/out
COPY --from=tasklattice-litellm-ui /app/ui/litellm-dashboard/out /app/litellm/proxy/_experimental/out

FROM ${LITELLM_IMAGE}
ARG LITELLM_VERSION
ARG PROVIDER_VERSION=dev
ARG SOURCE_REVISION=unknown

# Guard checks these labels before wiring a gateway: the output-stream protocol
# version must match the one its Runner speaks (runner/output_streaming.py).
LABEL org.opencontainers.image.title="tali-litellm" \
      org.opencontainers.image.description="LiteLLM with the TaskLattice Guard Guardrail Provider" \
      org.opencontainers.image.source="https://github.com/tasklattice/tasklattice-litellm-guard" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${LITELLM_VERSION}-guard.${PROVIDER_VERSION}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      io.tasklattice.litellm.version="${LITELLM_VERSION}" \
      io.tasklattice.guard.provider-version="${PROVIDER_VERSION}" \
      io.tasklattice.guard.output-stream-protocol="1"

USER root
# LiteLLM otherwise downloads the latest model cost map from GitHub during
# import. The pinned upstream image already contains a version-matched backup;
# use it by default so this image starts deterministically in disconnected
# environments. A runtime may explicitly override this environment variable.
ENV PYTHONPATH=/app \
    LITELLM_LOCAL_MODEL_COST_MAP=True
COPY --from=tasklattice-litellm-builder /app/litellm /app/litellm
# The Wolfi base already ships ca-certificates-bundle. Installing the separate
# ca-certificates package from the rolling repository pulls a newer OpenSSL
# whose libcrypto conflicts with the base's openssl.cnf, so add only what is
# missing (libatomic, required by the Prisma engine).
RUN test -s /etc/ssl/certs/ca-certificates.crt \
    && apk add --no-cache libatomic \
    && chown -R 65532:65532 /app/.venv/lib/python3.13/site-packages/prisma \
    && python -c 'import json; from importlib.resources import files; cost_map = json.loads(files("litellm").joinpath("model_prices_and_context_window_backup.json").read_text(encoding="utf-8")); assert len(cost_map) > 1000, "LiteLLM bundled model cost map is missing or incomplete"'

# LiteLLM rewrites static UI routes on first import. Do that while the
# image is writable so the non-root runtime can serve extensionless /ui routes.
RUN litellm --version

USER 65532:65532
RUN prisma generate --schema /app/.venv/lib/python3.13/site-packages/prisma/schema.prisma
