#!/usr/bin/env bash
# Lumen CLI login (OAuth device flow) + model catalog sync into the opencode
# config (opencode.json or opencode.jsonc).
set -euo pipefail

RC_BLOCK_BEGIN="# >>> lumen >>>"
RC_BLOCK_END="# <<< lumen <<<"

usage() {
  cat <<'EOF'
usage: lumen.sh [-s SERVER] [-k|--keyname NAME] [--author LABEL]
                [--client-id ID] [--ttl DURATION] [--opencode | --no-opencode]
                [--relogin] [--no-aliases] [-h|--help]

  -s, --server URL     Lumen base URL (default: $LUMEN_BASE_URL or
                       https://lumen.ncsa.illinois.edu)
  -k, --keyname NAME   Name for the requested API key (default: opencode)
      --author LABEL   Label stored with the request (default: $USER)
      --client-id ID   client_id sent to Lumen (default: lumen-cli)
      --ttl DURATION   Ask for a key that expires after DURATION: 0 (never)
                       or a number of hours, days or weeks, e.g. 12h, 30d,
                       8w (default: the server's maximum). Only used when a
                       new key is requested; the approver can change it
      --opencode       Sync the model catalog into the opencode config
                       (default): $OPENCODE_CONFIG, else
                       ~/.config/opencode/opencode.json, else
                       ~/.config/opencode/opencode.jsonc, created if missing
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

# Strips control characters so text from the server (or a bad URL) cannot
# send terminal escape sequences to the user's terminal.
sanitize() { printf '%s' "$1" | tr -d '\000-\037\177'; }

# One rule for every key source: login response, keys.json, LUMEN_API_KEY.
KEY_RE='^sk_[A-Za-z0-9_-]+$'

DEFAULT_SERVER="https://lumen.ncsa.illinois.edu"
SERVER="${LUMEN_BASE_URL:-$DEFAULT_SERVER}"
KEYNAME="opencode"
AUTHOR="${USER:-$(id -un)}"
CLIENT_ID="lumen-cli"
TTL=""
OPENCODE=1
RELOGIN=0
NO_ALIASES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--server)   SERVER="${2:?missing value for $1}"; shift 2 ;;
    -k|--keyname)  KEYNAME="${2:?missing value for $1}"; shift 2 ;;
    --author)      AUTHOR="${2:?missing value for $1}"; shift 2 ;;
    --client-id)   CLIENT_ID="${2:?missing value for $1}"; shift 2 ;;
    --ttl)         TTL="${2:?missing value for $1}"; shift 2 ;;
    --opencode)    OPENCODE=1; shift ;;
    --no-opencode) OPENCODE=0; shift ;;
    --relogin)     RELOGIN=1; shift ;;
    --no-aliases)  NO_ALIASES=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

# --ttl in seconds, the unit the server takes. The digit limit keeps the
# arithmetic far from overflow; the server enforces its own maximum.
TTL_SECONDS=""
if [[ -n "$TTL" ]]; then
  [[ "$TTL" =~ ^(0|[1-9][0-9]{0,5}([hdw]))$ ]] \
    || die "invalid --ttl: $(sanitize "$TTL") (use 0, or e.g. 12h, 30d, 8w)"
  case "${BASH_REMATCH[2]}" in
    h) TTL_SECONDS=$(( ${TTL%h} * 3600 )) ;;
    d) TTL_SECONDS=$(( ${TTL%d} * 86400 )) ;;
    w) TTL_SECONDS=$(( ${TTL%w} * 604800 )) ;;
    *) TTL_SECONDS=0 ;;
  esac
fi

SERVER="${SERVER%/}"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v jq >/dev/null 2>&1 || die "jq is required"

# Only https, except plain http for loopback development servers. Reject
# path traversal: a base URL with .. or empty segments would corrupt the
# endpoints and keys.json host keys built from it. The character whitelist
# keeps anything but a plain host[:port] and path out of the URLs and the
# rc block built from it.
SERVER_RE='^https?://[][A-Za-z0-9.:-]+(/[A-Za-z0-9._~/-]*)?$'
validate_server() {
  local url="$1" scheme hostpath hostport host shown
  shown=$(sanitize "$url")
  [[ "$url" == *"://"* ]] || die "invalid server URL: $shown"
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
        *) die "refusing plain http for non-local server: $shown (use https)" ;;
      esac
      ;;
    *) die "unsupported URL scheme in: $shown" ;;
  esac
  [[ -n "$hostport" ]] || die "invalid server URL: $shown"
  [[ "$url" =~ $SERVER_RE ]] || die "invalid characters in server URL: $shown"
  [[ "$path" != *".."* ]] || die "server URL path must not contain '..': $shown"
  if [[ "${path%/}" == *//* ]]; then
    die "server URL path must not contain empty segments: $shown"
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

# Single-quotes $1 for an rc file so the shell reading it never expands it.
# An embedded ' becomes '\'' (sh, csh) or \' (fish); fish also needs \
# doubled. A character loop avoids ${//} replacement quirks of bash 3.2.
rc_quote() {
  local s="$1" out="" c i
  for (( i = 0; i < ${#s}; i++ )); do
    c="${s:i:1}"
    if [[ "$2" == fish ]]; then
      case "$c" in
        "'") out+="\\'" ;;
        "\\") out+="\\\\" ;;
        *) out+="$c" ;;
      esac
    elif [[ "$c" == "'" ]]; then
      out+="'\\''"
    else
      out+="$c"
    fi
  done
  printf "'%s'" "$out"
}

install_rc_key() {
  local shell rc tmp key url
  shell=$(basename "${SHELL:-}")
  key=$(rc_quote "$1" "$shell")
  url=$(rc_quote "$2" "$shell")
  local -a lines
  case "$shell" in
    bash)
      rc="$HOME/.bashrc"
      lines=("export LUMEN_API_KEY=$key" "export LUMEN_BASE_URL=$url")
      ;;
    zsh)
      rc="$HOME/.zshrc"
      lines=("export LUMEN_API_KEY=$key" "export LUMEN_BASE_URL=$url")
      ;;
    csh|tcsh)
      rc="$HOME/.cshrc"
      lines=("setenv LUMEN_API_KEY $key" "setenv LUMEN_BASE_URL $url")
      ;;
    fish)
      rc="$HOME/.config/fish/config.fish"
      lines=("set -gx LUMEN_API_KEY $key" "set -gx LUMEN_BASE_URL $url")
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
  # The rc file now holds the key in plaintext: restrict it to the owner
  # before writing, as keys.json is. ls -lL works on both GNU and BSD.
  if [[ -e "$rc" ]]; then
    local mode
    mode=$(ls -lLd "$rc")
    if [[ "${mode:4:1}" == r || "${mode:7:1}" == r ]]; then
      echo "warning: $rc was readable by other users; setting it to mode 600" >&2
    fi
  else
    (umask 077 && : > "$rc")
  fi
  chmod 600 "$rc"
  cat "$tmp" > "$rc"
  rm -f "$tmp"
}

# Prints $1 if it is a plain positive integer, else the default $2. Server
# values reach $(( )), which would evaluate expressions (and command
# substitutions in array subscripts) hidden in them. An optional $3 caps the
# result, so the server cannot make the client wait arbitrarily long.
positive_int() {
  local v="$2"
  [[ "$1" =~ ^[1-9][0-9]{0,8}$ ]] && v="$1"
  if [[ -n "${3:-}" ]] && (( v > $3 )); then v="$3"; fi
  printf '%s' "$v"
}

MAX_INTERVAL=60
MAX_EXPIRES_IN=1800
MAX_RETRY_AFTER=60

device_login() {
  local interval expires_in deadline approved_by
  local -a fields=("client_id=$CLIENT_ID" "name=$KEYNAME" "author=$AUTHOR")
  [[ -z "$TTL_SECONDS" ]] || fields+=("ttl=$TTL_SECONDS")
  if ! http_post "$SERVER/oauth/device_authorization" "${fields[@]}"; then
    die "cannot reach $SERVER"
  fi
  if [[ "$RESP_CODE" != "200" ]]; then
    die "login request rejected ($RESP_CODE): $(sanitize "$(jq -r '.error_description // .error // "unknown error"' <<<"$RESP_BODY" 2>/dev/null || echo "$RESP_BODY")")"
  fi
  local device_code user_code
  device_code=$(jq -r .device_code <<<"$RESP_BODY")
  user_code=$(sanitize "$(jq -r .user_code <<<"$RESP_BODY")")
  interval=$(positive_int "$(jq -r '.interval // 5' <<<"$RESP_BODY")" 5 "$MAX_INTERVAL")
  expires_in=$(positive_int "$(jq -r '.expires_in // 600' <<<"$RESP_BODY")" 600 "$MAX_EXPIRES_IN")
  echo "To authorize '$KEYNAME' for $CLIENT_ID:"
  echo "  1. open $(sanitize "$(jq -r .verification_uri <<<"$RESP_BODY")")"
  echo "  2. enter code $user_code"
  local vfull
  vfull=$(sanitize "$(jq -r '.verification_uri_complete // empty' <<<"$RESP_BODY")")
  # Only open links on the server itself, never another scheme or host.
  if [[ "$vfull" == "$SERVER/"* ]]; then
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
      [[ "$key" =~ $KEY_RE ]] || die "unexpected token response"
      approved_by=$(sanitize "$(jq -r '.approved_by // "an account"' <<<"$RESP_BODY")")
      echo "Approved by $approved_by."
      # Older servers send no expires_at; null means the key never expires.
      local expires
      expires=$(sanitize "$(jq -r 'if has("expires_at") then (.expires_at // "never") else empty end' <<<"$RESP_BODY")")
      if [[ "$expires" == never ]]; then
        echo "The key never expires."
      elif [[ -n "$expires" ]]; then
        echo "The key expires at $expires."
      fi
      store_key "$key"
      export LUMEN_API_KEY="$key"
      return 0
    fi
    local error
    error=$(jq -r '.error // empty' <<<"$RESP_BODY" 2>/dev/null || true)
    case "$error" in
      authorization_pending) sleep 0 ;;
      slow_down)
        interval=$((interval + 5))
        (( interval > MAX_INTERVAL )) && interval=$MAX_INTERVAL
        sleep "$interval"
        ;;
      access_denied) die "authorization denied" ;;
      expired_token) die "authorization expired" ;;
      invalid_grant) die "authorization request is no longer valid" ;;
      *)
        if [[ "$RESP_CODE" == "429" ]]; then
          local ra
          ra=$(awk 'tolower($1)=="retry-after:"{gsub(/\r/,"",$2); print $2}' "$TMPDIR_LUMEN/post_headers" 2>/dev/null || true)
          sleep "$(positive_int "$ra" $((interval + 5)) "$MAX_RETRY_AFTER")"
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
    if [[ "$LUMEN_API_KEY" =~ $KEY_RE ]]; then
      return 0
    fi
    echo "warning: ignoring malformed LUMEN_API_KEY (unset it or run lumen.sh --relogin)" >&2
  fi
  if [[ $RELOGIN -eq 0 && -f "$KEYS_JSON" ]]; then
    local k
    k=$(jq -r --arg h "$HOST" '.[$h] // empty' "$KEYS_JSON" 2>/dev/null || true)
    if [[ "$k" =~ $KEY_RE ]]; then
      export LUMEN_API_KEY="$k"
      return 0
    fi
    [[ -z "$k" ]] || echo "warning: ignoring malformed key for $HOST in $KEYS_JSON (run lumen.sh --relogin to replace it)" >&2
  fi
  device_login
}

# Sets OPENCODE_FILE to the opencode config to update: $OPENCODE_CONFIG,
# else opencode.json (it wins when both exist), else opencode.jsonc. A
# missing file is created, owner-only, with just the schema line.
resolve_opencode_config() {
  local dir="$HOME/.config/opencode"
  if [[ -n "${OPENCODE_CONFIG:-}" ]]; then
    OPENCODE_FILE="$OPENCODE_CONFIG"
  elif [[ -f "$dir/opencode.json" ]]; then
    OPENCODE_FILE="$dir/opencode.json"
  else
    OPENCODE_FILE="$dir/opencode.jsonc"
  fi
  if [[ ! -f "$OPENCODE_FILE" ]]; then
    mkdir -p "$(dirname "$OPENCODE_FILE")"
    (umask 077 && printf '{"$schema": "https://opencode.ai/config.json"}\n' > "$OPENCODE_FILE")
    echo "Created $OPENCODE_FILE"
  fi
}

# JSONC -> JSON for jq: drops // and /* */ comments and trailing commas
# before } or ], but never inside strings (so "https://..." survives). With
# check=1 it prints nothing and exits 0 only if the input has comments.
JSONC_AWK='
  function out(s) { if (!check) printf "%s", s }
  {
    line = $0 "\n"; n = length(line)
    for (i = 1; i <= n; i++) {
      c = substr(line, i, 1)
      if (blk) { if (c == "*" && substr(line, i + 1, 1) == "/") { blk = 0; i++ }; continue }
      if (str) {
        out(c)
        if (esc) esc = 0
        else if (c == "\\") esc = 1
        else if (c == "\"") str = 0
        continue
      }
      if (c == "/" && substr(line, i + 1, 1) == "/") { found = 1; if (check) exit; i = n - 1; continue }
      if (c == "/" && substr(line, i + 1, 1) == "*") { found = 1; if (check) exit; blk = 1; i++; continue }
      if (pend != "") {
        if (c ~ /[ \t\r\n]/) { pend = pend c; continue }
        if (c == "}" || c == "]") pend = substr(pend, 2)
        out(pend); pend = ""
      }
      if (c == ",") { pend = c; continue }
      if (c == "\"") str = 1
      out(c)
    }
  }
  END { if (check) exit !found; out(pend) }
'
jsonc_to_json() { awk -v check=0 "$JSONC_AWK" "$1"; }
jsonc_has_comments() { awk -v check=1 "$JSONC_AWK" "$1"; }

sync_models() {
  resolve_opencode_config
  local config="$OPENCODE_FILE"

  local models_json code
  if ! models_json=$(curl -sS -H "Authorization: Bearer $LUMEN_API_KEY" -w $'\n%{http_code}' "$SERVER/v1/models"); then
    die "failed to fetch $SERVER/v1/models — check the key with: lumen.sh --relogin"
  fi
  code="${models_json##*$'\n'}"
  models_json="${models_json%$'\n'*}"
  [[ "$code" != "401" ]] || die "key expired or revoked — run lumen.sh --relogin"
  if [[ "$code" != 2?? ]]; then
    die "failed to fetch $SERVER/v1/models ($code) — check the key with: lumen.sh --relogin"
  fi

  local new_models model_count old_models added removed tmp
  local skip_aliases_json="false"
  [[ $NO_ALIASES -eq 1 ]] && skip_aliases_json="true"
  new_models=$(printf '%s' "$models_json" | jq --argjson skip_aliases "$skip_aliases_json" '
    [.data[]
     | select(($skip_aliases | not) or (.parent == null))
     # OpenCode chats in text, so skip models that take no text input (e.g. audio-only); unknown modalities are kept.
     | select(((.input_modalities // []) | length) == 0 or ((.input_modalities | index("text")) != null))
     | {
       key: .id,
       value: (
         {name: (.id + " via Lumen" + (if .parent then " (alias of " + .parent + ")" else "" end))}
         + {limit: (({context: .max_model_len} + (if .max_output_tokens then {output: .max_output_tokens} else {} end)))}
         + {cost: {input: .input_cost_per_million, output: .output_cost_per_million}}
         # Capability flags (issue #79): OpenCode derives image support from
         # modalities.input, not from attachment, and spells the function-call
         # flag tool_call. Unknown fields stay omitted, except tool_call:
         # OpenCode assumes tool calling unless the config says false, so it
         # is always written.
         + (if ((.input_modalities // []) | length) > 0 or ((.output_modalities // []) | length) > 0
            then {modalities: (
              (if ((.input_modalities // []) | length) > 0 then {input: .input_modalities} else {} end)
              + (if ((.output_modalities // []) | length) > 0 then {output: .output_modalities} else {} end)
            )}
            else {} end)
         + (if ((.input_modalities // []) | index("image")) != null then {attachment: true} else {} end)
         + {tool_call: (.supports_function_calling == true)}
         + (if .supports_reasoning == true then {reasoning: true} else {} end)
       )
     }] | from_entries')

  model_count=$(printf '%s' "$new_models" | jq 'length')
  if [[ "$model_count" -eq 0 ]]; then
    die "Lumen returned no models; leaving $config untouched"
  fi

  old_models=$(jsonc_to_json "$config" | jq '.provider.lumen.models // {}')

  added=$(comm -23 <(jq -r 'keys[]' <<<"$new_models") <(jq -r 'keys[]' <<<"$old_models"))
  removed=$(comm -13 <(jq -r 'keys[]' <<<"$new_models") <(jq -r 'keys[]' <<<"$old_models"))

  # The rewrite is plain JSON, so comments are lost: keep the original once.
  if jsonc_has_comments "$config"; then
    if [[ -e "$config.bak" ]]; then
      echo "warning: comments in $config are not kept when it is rewritten (an earlier copy is in $config.bak)" >&2
    else
      cp -p "$config" "$config.bak"
      echo "warning: comments in $config are not kept when it is rewritten; the original is saved as $config.bak" >&2
    fi
  fi

  tmp=$(mktemp "$config.tmp.XXXXXX")
  trap 'rm -f "$tmp"' EXIT

  jsonc_to_json "$config" | jq --argjson models "$new_models" --arg base "$SERVER" '
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
  ' > "$tmp"
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
