#!/usr/bin/env bash
set -euo pipefail
AK2_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
unset OPENAI_API_KEY CODEX_API_KEY
export UV_CACHE_DIR="${UV_CACHE_DIR:-$AK2_DIR/.local/uv-cache}"
exec uv run --locked --project "$AK2_DIR" ak2-agent "$@"
