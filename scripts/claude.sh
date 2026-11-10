#!/bin/bash
#
# Run Claude Code against a Lumen instance instead of Anthropic's API.
#
# Lumen exposes an Anthropic-compatible Messages API, so Claude Code only needs
# its base URL and API key pointed at Lumen and a model that Lumen serves.
#
# Required environment (set in your shell rc file by running ./lumen.sh, which
# logs in to Lumen and stores a key):
#   LUMEN_BASE_URL  Base URL of the Lumen instance
#   LUMEN_API_KEY   Lumen API key
#
# Usage: scripts/claude.sh [claude arguments...]

set -eu

# Fail early with a clear message if the Lumen settings are missing.
: "${LUMEN_BASE_URL:?not set, run lumen.sh to log in and open a new shell}"
: "${LUMEN_API_KEY:?not set, run lumen.sh to log in and open a new shell}"

# Non-Anthropic models don't support Anthropic-specific features, so turn them
# off: experimental beta headers, adaptive/extended thinking, and the context
# window check that rejects model names Claude Code doesn't recognize.
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
export CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1
export MAX_THINKING_TOKENS=0
export CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1

# Point Claude Code at Lumen.
export ANTHROPIC_BASE_URL="${LUMEN_BASE_URL}"
export ANTHROPIC_API_KEY="${LUMEN_API_KEY}"

# Model served by Lumen.
export ANTHROPIC_MODEL=glm-5.3

# Prefer the native installer location, then the system-wide one, then PATH.
for claude in "${HOME}/.local/bin/claude" /usr/local/bin/claude; do
  if [ -e "${claude}" ]; then
    exec "${claude}" "$@"
  fi
done
exec claude "$@"
