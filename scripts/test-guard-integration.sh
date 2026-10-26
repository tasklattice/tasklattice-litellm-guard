#!/usr/bin/env bash
set -euo pipefail
repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${GUARD_CHECKOUT:?Set GUARD_CHECKOUT to the pinned TaskLattice Guard checkout}"
: "${GUARD_TEST_RELAY_IMAGE:?Set GUARD_TEST_RELAY_IMAGE to the built image}"
expected_revision=2e95e8a3eef2933c023a5a5688c12b14bef57cc9
actual_revision="$(git -C "$GUARD_CHECKOUT" rev-parse HEAD)"
if [[ "$actual_revision" != "$expected_revision" ]] || [[ -n "$(git -C "$GUARD_CHECKOUT" status --porcelain --untracked-files=no)" ]]; then
  echo "Guard integration requires clean revision $expected_revision" >&2
  exit 1
fi
cd "$GUARD_CHECKOUT"
.venv/bin/python -m pytest -q -s -o asyncio_mode=auto \
  "$repository_root/integration/e2e" \
  tests/test_runner_api.py \
  tests/data_plane/test_artifact_execution.py \
  tests/data_plane/test_stream_safety_network.py
