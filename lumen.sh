#!/usr/bin/env bash
# Lumen CLI login (OAuth device flow) + model catalog sync into opencode.json.
set -euo pipefail

RC_BLOCK_BEGIN="# >>> lumen >>>"
RC_BLOCK_END="# <<< lumen <<<"

usage() {
  cat <<'EOF'
usage: lumen.sh [-s SERVER] [-k|--keyname NAME] [--author LABEL]
                [--client-id ID] [--opencode | --no-opencode] [--relogin]
                [--no-aliases] [-h|--help]

  -s, --server URL     Lumen base URL (default: $LUMEN_BASE_URL or
                       https://lumen.ncsa.illinois.edu)
  -k, --keyname NAME   Name for the requested API key (default: opencode)
      --author LABEL   Label stored with the request (default: $USER)
      --client-id ID   client_id sent to Lumen (default: lumen-cli)
      --opencode       Sync the model catalog into opencode.json (default)
      --no-opencode    Only authenticate and store the key
      --relogin        Request a new key even if one is stored; the newly
                       issued key always replaces the stored one
      --no-aliases     Skip alias entries when syncing models

An $LUMEN_API_KEY from the environment (or the rc-file block) is only used
for the server it was issued for ($LUMEN_BASE_URL, default: the production
server), so keys are never sent to the wrong server. Because the login writes
both variables, a plain run targets the server you last logged in to.
Environment: LUMEN_API_KEY, LUMEN_BASE_URL, OPENCODE_CONFIG, LUMEN_CONFIG_DIR.
EOF
}

die() { echo "lumen.sh: $*" >&2; exit 1; }

DEFAULT_SERVER="https://lumen.ncsa.illinois.edu"
SERVER="${LUMEN_BASE_URL:-$DEFAULT_SERVER}"
KEYNAME="opencode"
AUTHOR="${USER:-$(id -un)}"
CLIENT_ID="lumen-cli"
OPENCODE=1
RELOGIN=0
NO_ALIASES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--server)   SERVER="${2:?missing value for $1}"; shift 2 ;;
    -k|--keyname)  KEYNAME="${2:?missing value for $1}"; shift 2 ;;
    --author)      AUTHOR="${2:?missing value for $1}"; shift 2 ;;
    --client-id)   CLIENT_ID="${2:?missing value for $1}"; shift 2 ;;
    --opencode)    OPENCODE=1; shift ;;
    --no-opencode) OPENCODE=0; shift ;;
    --relogin)     RELOGIN=1; shift ;;
    --no-aliases)  NO_ALIASES=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

SERVER="${SERVER%/}"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v jq >/dev/null 2>&1 || die "jq is required"

# Only https, except plain http for loopback development servers. Reject
# path traversal: a base URL with .. or empty segments would corrupt the
# endpoints and keys.json host keys built from it.
validate_server() {
  local url="$1" scheme hostpath hostport host
  [[ "$url" == *"://"* ]] || die "invalid server URL: $url"
  scheme="${url%%://*}"
  hostpath="${url#*://}"
  hostport="${hostpath%%/*}"
  local path="${hostpath#"$hostport"}"
  case "$scheme" in
    https) : ;;
    http)
      host="${hostport%:*}"
      case "$host" in
        localhost|127.0.0.1|\[::1\]|::1) : ;;
        *) die "refusing plain http for non-local server: $url (use https)" ;;
      esac
      ;;
    *) die "unsupported URL scheme in: $url" ;;
  esac
  [[ -n "$hostport" ]] || die "invalid server URL: $url"
  [[ "$path" != *".."* ]] || die "server URL path must not contain '..': $url"
  if [[ "${path%/}" == *//* ]]; then
    die "server URL path must not contain empty segments: $url"
  fi
}
validate_server "$SERVER"

HOST="${SERVER#*://}"
HOST="${HOST%%/*}"
LDIR="${LUMEN_CONFIG_DIR:-"$HOME/.config/lumen"}"
KEYS_JSON="$LDIR/keys.json"

# POSTs form-encoded fields; sets RESP_BODY (body) and RESP_CODE (http code).
http_post() {
  local url="$1" raw
  shift
  local args=()
  local field
  for field in "$@"; do args+=(--data-urlencode "$field"); done
  local out="$TMPDIR_LUMEN/post_headers"
  raw=$(curl -sS --max-time 15 -D "$out" -X POST "$url" "${args[@]}" -w $'\n%{http_code}') || return 1
  RESP_CODE="${raw##*$'\n'}"
  RESP_BODY="${raw%$'\n'*}"
  return 0
}

store_key() {
  # Always store without asking: store_key only runs after a login the user
  # just completed (or a deliberate --relogin), so the freshly issued key is
  # by definition the one they want; prompting could leave a key that the
  # server already revoked kept on disk.
  mkdir -p "$LDIR"
  chmod 700 "$LDIR"
  local tmp
  tmp=$(mktemp "$LDIR/.keys.tmp.XXXXXX")
  if [[ -f "$KEYS_JSON" ]]; then
    jq --arg h "$HOST" --arg k "$1" '.[$h] = $k' "$KEYS_JSON" > "$tmp"
  else
    printf '{}\n' | jq --arg h "$HOST" --arg k "$1" '.[$h] = $k' > "$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$KEYS_JSON"
  install_rc_key "$1" "$SERVER"
}

install_rc_key() {
  local shell rc tmp
  shell=$(basename "${SHELL:-}")
  local -a lines
  case "$shell" in
    bash)
      rc="$HOME/.bashrc"
      lines=("export LUMEN_API_KEY=\"$1\"" "export LUMEN_BASE_URL=\"$2\"")
      ;;
    zsh)
      rc="$HOME/.zshrc"
      lines=("export LUMEN_API_KEY=\"$1\"" "export LUMEN_BASE_URL=\"$2\"")
      ;;
    csh|tcsh)
      rc="$HOME/.cshrc"
      lines=("setenv LUMEN_API_KEY \"$1\"" "setenv LUMEN_BASE_URL \"$2\"")
      ;;
    fish)
      rc="$HOME/.config/fish/config.fish"
      lines=("set -gx LUMEN_API_KEY \"$1\"" "set -gx LUMEN_BASE_URL \"$2\"")
      ;;
    *)
      echo "unknown shell '$shell': key stored in $KEYS_JSON only" >&2
      return 0
      ;;
  esac
  tmp=$(mktemp)
  if [[ -f "$rc" ]]; then
    awk -v b="$RC_BLOCK_BEGIN" -v e="$RC_BLOCK_END" '$0==b{s=1;next} $0==e{s=0;next} !s{print}' "$rc" > "$tmp"
  fi
  {
    printf '%s\n' "$RC_BLOCK_BEGIN"
    printf '%s\n' "${lines[@]}"
    printf '%s\n' "$RC_BLOCK_END"
  } >> "$tmp"
  cat "$tmp" > "$rc"
  rm -f "$tmp"
}

device_login() {
  local interval expires_in deadline approved_by
  if ! http_post "$SERVER/oauth/device_authorization" \
      "client_id=$CLIENT_ID" "name=$KEYNAME" "author=$AUTHOR"; then
    die "cannot reach $SERVER"
  fi
  if [[ "$RESP_CODE" != "200" ]]; then
    die "login request rejected ($RESP_CODE): $(jq -r '.error_description // .error // "unknown error"' <<<"$RESP_BODY" 2>/dev/null || echo "$RESP_BODY")"
  fi
  local device_code user_code
  device_code=$(jq -r .device_code <<<"$RESP_BODY")
  user_code=$(jq -r .user_code <<<"$RESP_BODY")
  interval=$(jq -r '.interval // 5' <<<"$RESP_BODY")
  expires_in=$(jq -r '.expires_in // 600' <<<"$RESP_BODY")
  echo "To authorize '$KEYNAME' for $CLIENT_ID:"
  echo "  1. open $(jq -r .verification_uri <<<"$RESP_BODY")"
  echo "  2. enter code $user_code"
  local vfull
  vfull=$(jq -r '.verification_uri_complete // empty' <<<"$RESP_BODY")
  if [[ -n "$vfull" ]]; then
    if command -v open >/dev/null 2>&1; then open "$vfull" >/dev/null 2>&1 || true
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$vfull" >/dev/null 2>&1 || true
    fi
  fi
  deadline=$(( $(date +%s) + expires_in ))
  while :; do
    sleep "$interval"
    (( $(date +%s) >= deadline )) && die "timed out waiting for authorization"
    if ! http_post "$SERVER/oauth/token" \
        "grant_type=urn:ietf:params:oauth:grant-type:device_code" \
        "device_code=$device_code" "client_id=$CLIENT_ID"; then
      continue  # transient network failure; keep polling until the deadline
    fi
    if [[ "$RESP_CODE" == "200" ]]; then
      local key
      key=$(jq -r '.access_token // empty' <<<"$RESP_BODY")
      [[ -n "$key" ]] || die "unexpected token response"
      approved_by=$(jq -r '.approved_by // "an account"' <<<"$RESP_BODY")
      echo "Approved by $approved_by."
      store_key "$key"
      export LUMEN_API_KEY="$key"
      return 0
    fi
    local error
    error=$(jq -r '.error // empty' <<<"$RESP_BODY" 2>/dev/null || true)
    case "$error" in
      authorization_pending) sleep 0 ;;
      slow_down) interval=$((interval + 5)); sleep "$interval" ;;
      access_denied) die "authorization denied" ;;
      expired_token) die "authorization expired" ;;
      invalid_grant) die "authorization request is no longer valid" ;;
      *)
        if [[ "$RESP_CODE" == "429" ]]; then
          local ra
          ra=$(awk 'tolower($1)=="retry-after:"{gsub(/\r/,"",$2); print $2}' "$TMPDIR_LUMEN/post_headers" 2>/dev/null || true)
          sleep "${ra:-$((interval + 5))}"
        else
          sleep "$interval"
        fi
        ;;
    esac
  done
}

resolve_key() {
  # An inherited key belongs to the server that issued it; keys exported by a
  # previous login set LUMEN_BASE_URL alongside the key. The :- fallback keeps
  # hand-exported keys (no LUMEN_BASE_URL) working against the default server,
  # and the %-strip matches hand-set values that carry a trailing slash
  # (SERVER was already stripped).
  local want="${LUMEN_BASE_URL:-$DEFAULT_SERVER}"
  if [[ $RELOGIN -eq 0 && -n "${LUMEN_API_KEY:-}" \
        && "${want%/}" == "$SERVER" ]]; then
    return 0
  fi
  if [[ $RELOGIN -eq 0 && -f "$KEYS_JSON" ]]; then
    local k
    k=$(jq -r --arg h "$HOST" '.[$h] // empty' "$KEYS_JSON" 2>/dev/null || true)
    if [[ -n "$k" ]]; then
      export LUMEN_API_KEY="$k"
      return 0
    fi
  fi
  device_login
}

sync_models() {
  local config="${OPENCODE_CONFIG:-"$HOME/.config/opencode/opencode.json"}"
  [[ -f "$config" ]] || die "config not found: $config"

  local models_json
  if ! models_json=$(curl -fsS -H "Authorization: Bearer $LUMEN_API_KEY" "$SERVER/v1/models"); then
    die "failed to fetch $SERVER/v1/models — check the key with: lumen.sh --relogin"
  fi

  local new_models model_count old_models added removed tmp
  local skip_aliases_json="false"
  [[ $NO_ALIASES -eq 1 ]] && skip_aliases_json="true"
  new_models=$(printf '%s' "$models_json" | jq --argjson skip_aliases "$skip_aliases_json" '
    [.data[]
     | select(($skip_aliases | not) or (.parent == null))
     | {
       key: .id,
       value: (
         {name: (.id + " via Lumen" + (if .parent then " (alias of " + .parent + ")" else "" end))}
         + {limit: (({context: .max_model_len} + (if .max_output_tokens then {output: .max_output_tokens} else {} end)))}
         + {cost: {input: .input_cost_per_million, output: .output_cost_per_million}}
         # Capability flags (issue #79): OpenCode derives image support from
         # modalities.input, not from attachment, and spells the function-call
         # flag tool_call. Omit anything the server does not know.
         + (if ((.input_modalities // []) | length) > 0 or ((.output_modalities // []) | length) > 0
            then {modalities: (
              (if ((.input_modalities // []) | length) > 0 then {input: .input_modalities} else {} end)
              + (if ((.output_modalities // []) | length) > 0 then {output: .output_modalities} else {} end)
            )}
            else {} end)
         + (if ((.input_modalities // []) | index("image")) != null then {attachment: true} else {} end)
         + (if .supports_function_calling == true then {tool_call: true} else {} end)
         + (if .supports_reasoning == true then {reasoning: true} else {} end)
       )
     }] | from_entries')

  model_count=$(printf '%s' "$new_models" | jq 'length')
  if [[ "$model_count" -eq 0 ]]; then
    die "Lumen returned no models; leaving $config untouched"
  fi

  old_models=$(jq '.provider.lumen.models // {}' "$config")

  added=$(comm -23 <(jq -r 'keys[]' <<<"$new_models") <(jq -r 'keys[]' <<<"$old_models"))
  removed=$(comm -13 <(jq -r 'keys[]' <<<"$new_models") <(jq -r 'keys[]' <<<"$old_models"))

  tmp=$(mktemp "$config.tmp.XXXXXX")
  trap 'rm -f "$tmp"' EXIT

  jq --argjson models "$new_models" --arg base "$SERVER" '
    (if .provider and .provider.lumen then .provider.lumen else null end) as $lumen
    | .provider = ((.provider // {}) + {lumen: (
        if $lumen then
          $lumen + {models: $models}
        else
          {
            npm: "@ai-sdk/openai-compatible",
            name: "Lumen",
            options: {baseURL: ($base + "/v1"), apiKey: "{env:LUMEN_API_KEY}"},
            models: $models
          }
        end
      )})
    | if ((.enabled_providers // []) | index("lumen")) == null
      then .enabled_providers = ((.enabled_providers // []) + ["lumen"])
      else .
      end
  ' "$config" > "$tmp"
  mv "$tmp" "$config"
  trap 'rm -rf "$TMPDIR_LUMEN"' EXIT

  if [[ -n "$removed" ]]; then
    echo "Removed models:"
    printf '%s\n' "$removed" | sed 's/^/  - /'
  fi
  if [[ -n "$added" ]]; then
    echo "Added models:"
    printf '%s\n' "$added" | sed 's/^/  + /'
  fi
  echo "Updated $model_count models in $config"
}

TMPDIR_LUMEN=$(mktemp -d)
trap 'rm -rf "$TMPDIR_LUMEN"' EXIT

resolve_key

if [[ $OPENCODE -eq 0 ]]; then
  echo "LUMEN_API_KEY=$LUMEN_API_KEY"
  exit 0
fi

sync_models
