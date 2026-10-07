# LiteLLM with the TaskLattice Guard Guardrail Provider.
#
# Each supported LiteLLM release has its own reviewed overlay directory under
# litellm/v<version>. The upstream image tag and the overlay directory must
# name the same release; apply-overlay.py refuses an unexpected source tree.
ARG LITELLM_VERSION=1.87.0
ARG LITELLM_IMAGE=litellm/litellm-database:v${LITELLM_VERSION}

FROM ${LITELLM_IMAGE} AS tasklattice-litellm-builder
ARG LITELLM_VERSION

USER root
COPY litellm/v${LITELLM_VERSION} /tmp/tasklattice-litellm-overlay
RUN python /tmp/tasklattice-litellm-overlay/apply-overlay.py --root /app \
    && python /tmp/tasklattice-litellm-overlay/verify-overlay.py --root /app \
    && LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH=/app python -m unittest discover -s /tmp/tasklattice-litellm-overlay/tests \
    && cd /app/ui/litellm-dashboard \
    && npm ci --ignore-scripts \
    && npm run build \
    && rm -rf /app/litellm/proxy/_experimental/out \
    && mkdir -p /app/litellm/proxy/_experimental/out \
    && cp -R out/. /app/litellm/proxy/_experimental/out/

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
