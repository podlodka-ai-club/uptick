#!/usr/bin/env bash
set -euo pipefail

AK_AGENT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
    echo 'Нужен uv. На macOS установи: brew install uv' >&2
    exit 1
fi

# This launcher always uses the saved ChatGPT session, never environment API keys.
unset OPENAI_API_KEY CODEX_API_KEY
exec uv run --locked --project "$AK_AGENT_DIR" ak-agent "$@"
