#!/usr/bin/env bash
# Validate a release tag (v<litellm>-guard.<n>) and print build parameters.
set -euo pipefail

tag=${1:?Usage: check-release-tag.sh v<litellm-version>-guard.<n>}
if [[ ! "$tag" =~ ^v([0-9]+\.[0-9]+\.[0-9]+)-guard\.([1-9][0-9]*)$ ]]; then
  echo "Release tag must look like v1.87.0-guard.1, got: $tag" >&2
  exit 2
fi
litellm_version=${BASH_REMATCH[1]}
provider_version=${BASH_REMATCH[2]}
root=$(cd "$(dirname "$0")/.." && pwd)
if [[ ! -d "$root/litellm/v$litellm_version" ]]; then
  echo "No reviewed overlay for LiteLLM $litellm_version (expected litellm/v$litellm_version)" >&2
  exit 2
fi
echo "litellm_version=$litellm_version"
echo "provider_version=$provider_version"
echo "image_tag=$litellm_version-guard.$provider_version"
