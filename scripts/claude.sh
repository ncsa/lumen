#!/bin/bash
#
# Run Claude Code against a Lumen instance instead of Anthropic's API.
#
# Lumen exposes an Anthropic-compatible Messages API, so Claude Code only needs
# its base URL and API key pointed at Lumen and a model that Lumen serves.
#
# Settings come from the environment, else from the [lumen] section of
# $LUMEN_CONFIG_DIR/config.ini (default ~/.config/lumen/config.ini), which
# ./lumen.sh writes when it logs in to Lumen:
#   LUMEN_BASE_URL   (base_url)    Base URL of the Lumen instance
#   LUMEN_API_KEY    (api_key)     Lumen API key
#   ANTHROPIC_MODEL  (chat_model)  A model Lumen serves (see /v1/models);
#                                  chat_model is added to config.ini by hand
# Optional:
#   ANTHROPIC_DEFAULT_HAIKU_MODEL  Model for Claude Code's background tasks;
#                                  defaults to ANTHROPIC_MODEL
#
# Usage: ANTHROPIC_MODEL=<model> scripts/claude.sh [claude arguments...]
#
# This is a starting point to copy and adapt. docs/guides/anthropic.md explains
# the CLAUDE_CODE_* and MAX_THINKING_TOKENS switches set below.

set -eu

# Prints the value of key $1 in the [lumen] section of config.ini, or nothing.
ini_get() {
  [ -r "${LUMEN_INI}" ] || return 0
  awk -v want="$1" '
    /^[ \t]*\[/ { insec = ($0 ~ /^[ \t]*\[lumen\][ \t]*$/); next }
    insec && !/^[ \t]*[#;]/ && match($0, /[=:]/) {
      k = substr($0, 1, RSTART - 1); v = substr($0, RSTART + 1)
      gsub(/^[ \t]+|[ \t\r]+$/, "", k); gsub(/^[ \t]+|[ \t\r]+$/, "", v)
      if (k == want) { print v; exit }
    }
  ' "${LUMEN_INI}"
}
LUMEN_INI="${LUMEN_CONFIG_DIR:-${HOME}/.config/lumen}/config.ini"

# The environment wins over config.ini.
LUMEN_BASE_URL="${LUMEN_BASE_URL:-$(ini_get base_url)}"
LUMEN_API_KEY="${LUMEN_API_KEY:-$(ini_get api_key)}"
ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-$(ini_get chat_model)}"

# Fail early with a clear message if the Lumen settings are missing.
: "${LUMEN_BASE_URL:?not set, run lumen.sh to log in (or set base_url in ${LUMEN_INI})}"
: "${LUMEN_API_KEY:?not set, run lumen.sh to log in (or set api_key in ${LUMEN_INI})}"

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

# Model served by Lumen; the Haiku model (background tasks) defaults to it.
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:?not set, set it (or chat_model in ${LUMEN_INI}) to a model Lumen serves (see /v1/models)}"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-${ANTHROPIC_MODEL}}"

# Prefer the native installer location, then the system-wide one, then PATH.
for claude in "${HOME}/.local/bin/claude" /usr/local/bin/claude; do
  if [ -e "${claude}" ]; then
    exec "${claude}" "$@"
  fi
done
exec claude "$@"
