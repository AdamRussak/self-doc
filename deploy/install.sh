#!/usr/bin/env bash
# self-docs deploy-kit installer.
#
# Installs self-docs from pre-built GHCR images (self-docs-ingestion,
# self-docs-mcp-server) plus this deploy kit's docker-compose.yml and the
# upstream db/init/*.sql, into a fresh install directory. No clone of the
# self-docs repository is required. This script never builds an image; it
# only pulls, renders config, and starts the published containers.
#
# Usage:
#   deploy/install.sh                                   # defaults: registry
#                                                        # default model, into
#                                                        # ./self-docs
#   deploy/install.sh --dir /opt/self-docs \
#       --model bge-base-en-v1.5 --port-api 9080 --port-mcp 9081
#   deploy/install.sh --model BAAI/bge-base-en-v1.5 --version 1.2.3
#   deploy/install.sh --dry-run --model mxbai-embed-large-v1 --dir /tmp/try
#   deploy/install.sh --source-dir /path/to/self-docs-checkout --dir ./kit
#
# CLI:
#   --dir <path>          install directory              (default: ./self-docs)
#   --model <name|slug>   model to install                (default: registry default)
#   --version <X.Y.Z>     pin image version               (default: moving <slug> tag)
#   --owner <ghcr-owner>  GHCR namespace                   (default: adamrussak)
#   --port-api <n>        host port for ingestion/admin    (default: 8080)
#   --port-mcp <n>        host port for mcp-server         (default: 8081)
#   --source-dir <path>   read kit + SQL from a local dir instead of the network
#   --dry-run             render files only, zero docker invocations
#   --no-start            render + pull + verify labels, do not `up`
#   --no-verify-labels    skip the image/model label check
#   --force               overwrite a non-empty install dir
#   -h | --help            show this help and exit
#
# Exit codes:
#   0 — success (or --help)
#   1 — runtime failure (network/docker/compose/label mismatch, etc.)
#   2 — usage or preflight failure (bad flags, unknown model, docker v1 only,
#       port in use, non-empty dir without --force, etc.)

set -euo pipefail

SCRIPT_NAME="install.sh"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="https://raw.githubusercontent.com/AdamRussak/self-doc/main"
# MODELS_TSV is resolved later (three-way lookup: --source-dir, script-adjacent,
# or fetched from BASE_URL) — see resolve_models_tsv() below. It is deliberately
# NOT hardcoded to "${SCRIPT_DIR}/models.tsv": the documented user flow is
# downloading just this one file and running it, with nothing else on disk.

# --- output helpers ---------------------------------------------------------

die_usage() {
    echo "${SCRIPT_NAME}: error: $*" >&2
    exit 2
}

die_runtime() {
    echo "${SCRIPT_NAME}: error: $*" >&2
    exit 1
}

warn() {
    echo "${SCRIPT_NAME}: warning: $*" >&2
}

print_help() {
    cat <<'EOF'
Usage: install.sh [OPTIONS]

Install self-docs from pre-built GHCR images into a fresh install directory.

Options:
  --dir <path>          install directory              (default: ./self-docs)
  --model <name|slug>   model to install                (default: registry default)
  --version <X.Y.Z>     pin image version               (default: moving <slug> tag)
  --owner <ghcr-owner>  GHCR namespace                   (default: adamrussak)
  --port-api <n>        host port for ingestion/admin    (default: 8080)
  --port-mcp <n>        host port for mcp-server         (default: 8081)
  --source-dir <path>   read kit + SQL from a local dir instead of the network
  --dry-run             render files only, zero docker invocations
  --no-start            render + pull + verify labels, do not `up`
  --no-verify-labels    skip the image/model label check
  --force               overwrite a non-empty install dir
  -h, --help            show this help and exit

Exit codes:
  0  success (or --help)
  1  runtime failure
  2  usage or preflight failure
EOF
}

# --- defaults ----------------------------------------------------------------

DIR="./self-docs"
MODEL_ARG=""
VERSION=""
OWNER="adamrussak"
PORT_API="8080"
PORT_MCP="8081"
SOURCE_DIR=""
DRY_RUN=false
NO_START=false
NO_VERIFY_LABELS=false
FORCE=false

# --- arg parsing -------------------------------------------------------------
# require_opt_value rejects a missing value AND a value that merely looks like
# another option (e.g. `--dir --force`), which would otherwise be taken
# literally as DIR="--force" and later break dirname/ls in confusing ways.
# None of this script's option values ever legitimately start with '-'.

require_opt_value() {
    local opt="$1" val="${2-}" has_val="$3"
    [[ "$has_val" -eq 1 ]] || die_usage "${opt} requires an argument"
    [[ "$val" != -* ]] || die_usage "${opt} requires an argument, got option-like value '${val}'"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)
            require_opt_value "--dir" "${2-}" "$([[ $# -ge 2 ]] && echo 1 || echo 0)"
            DIR="$2"; shift 2 ;;
        --model)
            require_opt_value "--model" "${2-}" "$([[ $# -ge 2 ]] && echo 1 || echo 0)"
            MODEL_ARG="$2"; shift 2 ;;
        --version)
            require_opt_value "--version" "${2-}" "$([[ $# -ge 2 ]] && echo 1 || echo 0)"
            VERSION="$2"; shift 2 ;;
        --owner)
            require_opt_value "--owner" "${2-}" "$([[ $# -ge 2 ]] && echo 1 || echo 0)"
            OWNER="$2"; shift 2 ;;
        --port-api)
            require_opt_value "--port-api" "${2-}" "$([[ $# -ge 2 ]] && echo 1 || echo 0)"
            PORT_API="$2"; shift 2 ;;
        --port-mcp)
            require_opt_value "--port-mcp" "${2-}" "$([[ $# -ge 2 ]] && echo 1 || echo 0)"
            PORT_MCP="$2"; shift 2 ;;
        --source-dir)
            require_opt_value "--source-dir" "${2-}" "$([[ $# -ge 2 ]] && echo 1 || echo 0)"
            SOURCE_DIR="$2"; shift 2 ;;
        --dry-run)
            DRY_RUN=true; shift ;;
        --no-start)
            NO_START=true; shift ;;
        --no-verify-labels)
            NO_VERIFY_LABELS=true; shift ;;
        --force)
            FORCE=true; shift ;;
        -h|--help)
            print_help
            exit 0 ;;
        *)
            die_usage "unknown argument: $1 (see --help)" ;;
    esac
done

# --- input validation (treat --dir/--owner/--model/--version as untrusted) --

[[ -n "$DIR" ]] || die_usage "--dir must not be empty"

if ! [[ "$OWNER" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
    die_usage "--owner '${OWNER}' is not a valid GHCR namespace (lowercase alnum, '.', '_', '-' only)"
fi

is_valid_port() {
    [[ "$1" =~ ^[0-9]+$ ]] && [[ "$1" -ge 1 ]] && [[ "$1" -le 65535 ]]
}
is_valid_port "$PORT_API" || die_usage "--port-api '${PORT_API}' is not a valid port number (1-65535)"
is_valid_port "$PORT_MCP" || die_usage "--port-mcp '${PORT_MCP}' is not a valid port number (1-65535)"
[[ "$PORT_API" != "$PORT_MCP" ]] || die_usage "--port-api and --port-mcp must differ (both '${PORT_API}')"

if [[ -n "$VERSION" ]]; then
    VERSION_NUM="${VERSION#v}"
    if ! [[ "$VERSION_NUM" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        die_usage "--version '${VERSION}' must look like X.Y.Z (optionally prefixed with 'v')"
    fi
fi

if [[ -n "$SOURCE_DIR" ]]; then
    [[ -d "$SOURCE_DIR" ]] || die_usage "--source-dir '${SOURCE_DIR}' is not a directory"
fi

# curl is required whenever this script might need the network: to fetch the
# manifest itself (case 3 below) and, later, db/init/*.sql + docker-compose.yml
# — in every case unless --source-dir is given. Checked here, up front, so the
# manifest fetch below can rely on it already having been enforced.
if [[ -z "$SOURCE_DIR" ]] && ! command -v curl >/dev/null 2>&1; then
    die_usage "curl is required to fetch the deploy kit from the network (or pass --source-dir to install from a local checkout)"
fi

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/self-docs-install.XXXXXX")"
cleanup_tmp_root() { rm -rf "$TMP_ROOT"; }
trap cleanup_tmp_root EXIT

# --- resolve deploy/models.tsv (three-way lookup) ----------------------------
# The documented flow is "download just install.sh and run it" — nothing else
# on disk. So the manifest is resolved in this priority order, not required
# to sit next to install.sh:
#   1. --source-dir given          -> <source-dir>/deploy/models.tsv
#      (explicit local checkout: offline/airgapped installs and tests. If the
#      manifest isn't there, that source-dir is not a usable checkout — fail,
#      do not silently fall through to the network.)
#   2. ${SCRIPT_DIR}/models.tsv exists -> use it
#      (whole deploy/ directory was downloaded/cloned alongside install.sh.)
#   3. otherwise                   -> fetch from BASE_URL
#      (the "just install.sh" flow; needs curl, already enforced above.)
resolve_models_tsv() {
    if [[ -n "$SOURCE_DIR" ]]; then
        local from_source="${SOURCE_DIR%/}/deploy/models.tsv"
        if [[ ! -f "$from_source" ]]; then
            # exit 2, not 1: every other --source-dir problem (not a
            # directory at all, etc.) is a usage/preflight failure, so a
            # --source-dir that exists but isn't a usable checkout should be
            # consistent with that, not the odd one out at exit 1.
            die_usage "manifest not found at '${from_source}' — --source-dir '${SOURCE_DIR}' does not look like a self-docs checkout (expected deploy/models.tsv there)"
        fi
        MODELS_TSV="$from_source"
        return
    fi
    if [[ -f "${SCRIPT_DIR}/models.tsv" ]]; then
        MODELS_TSV="${SCRIPT_DIR}/models.tsv"
        return
    fi
    MODELS_TSV="${TMP_ROOT}/models.tsv"
    if ! curl -fsSL "${BASE_URL}/deploy/models.tsv" -o "${MODELS_TSV}.tmp"; then
        rm -f -- "${MODELS_TSV}.tmp"
        die_runtime "failed to fetch deploy/models.tsv from ${BASE_URL} (check network connectivity, or pass --source-dir); it was not found next to install.sh either"
    fi
    mv -- "${MODELS_TSV}.tmp" "$MODELS_TSV"
}
resolve_models_tsv

# --- load deploy/models.tsv --------------------------------------------------
# Parser hazards handled deliberately:
#   - query_prompt has a semantically significant trailing space; IFS is set
#     to '|' only (not the default IFS), so `read` never trims it.
#   - passage_prompt is empty on 3/4 rows (adjacent pipes); IFS='|' never
#     collapses consecutive delimiters, unlike awk -F'|'/cut -s/tr -s.
#
# SECURITY: this file crosses a trust boundary — the three-way lookup above
# can fetch it from the network, so every field is validated immediately
# after parsing, before ANY use (sed/awk program text, the SQL DDL rendered
# into 01_schema.sql, the Docker image tag, or .env). A hostile/malformed row
# is rejected with exit 2 naming the offending row and field; nothing is
# silently coerced or truncated.

SLUG_RE='^[a-z0-9][a-z0-9._-]*$'
MODEL_RE='^[A-Za-z0-9][A-Za-z0-9._/-]*$'
DIM_RE='^[0-9]+$'
MEM_RE='^[0-9]+[bkmgBKMG]?$'
BOOL_RE='^(true|false)$'

SLUGS=(); MODELS=(); DIMS=(); MEM_INGS=(); MEM_MCPS=(); QPROMPTS=(); PPROMPTS=(); IS_DEFAULTS=()

MANIFEST_ROW=0
while IFS='|' read -r slug model dim mem_ing mem_mcp qprompt pprompt is_default; do
    [[ -n "$slug" ]] || continue
    MANIFEST_ROW=$((MANIFEST_ROW + 1))

    [[ "$slug" =~ $SLUG_RE ]] \
        || die_usage "manifest ${MODELS_TSV} row ${MANIFEST_ROW}: invalid 'slug' field '${slug}' (must match ${SLUG_RE})"
    [[ "$model" =~ $MODEL_RE ]] \
        || die_usage "manifest ${MODELS_TSV} row ${MANIFEST_ROW}: invalid 'model' field '${model}' (must match ${MODEL_RE})"
    [[ "$dim" =~ $DIM_RE ]] \
        || die_usage "manifest ${MODELS_TSV} row ${MANIFEST_ROW}: invalid 'dim' field '${dim}' (must be a positive integer)"
    [[ "$mem_ing" =~ $MEM_RE ]] \
        || die_usage "manifest ${MODELS_TSV} row ${MANIFEST_ROW}: invalid 'mem_ingestion' field '${mem_ing}' (must be a Docker memory limit like '1500m' or '3g')"
    [[ "$mem_mcp" =~ $MEM_RE ]] \
        || die_usage "manifest ${MODELS_TSV} row ${MANIFEST_ROW}: invalid 'mem_mcp' field '${mem_mcp}' (must be a Docker memory limit like '1500m' or '3g')"
    if [[ "$qprompt" == *'"'* ]] || [[ "$qprompt" =~ [[:cntrl:]] ]]; then
        die_usage "manifest ${MODELS_TSV} row ${MANIFEST_ROW}: 'query_prompt' field contains a double-quote or control character"
    fi
    if [[ "$pprompt" == *'"'* ]] || [[ "$pprompt" =~ [[:cntrl:]] ]]; then
        die_usage "manifest ${MODELS_TSV} row ${MANIFEST_ROW}: 'passage_prompt' field contains a double-quote or control character"
    fi
    [[ "$is_default" =~ $BOOL_RE ]] \
        || die_usage "manifest ${MODELS_TSV} row ${MANIFEST_ROW}: invalid 'is_default' field '${is_default}' (must be 'true' or 'false')"

    SLUGS+=("$slug")
    MODELS+=("$model")
    DIMS+=("$dim")
    MEM_INGS+=("$mem_ing")
    MEM_MCPS+=("$mem_mcp")
    QPROMPTS+=("$qprompt")
    PPROMPTS+=("$pprompt")
    IS_DEFAULTS+=("$is_default")
done < "$MODELS_TSV"

[[ "${#SLUGS[@]}" -gt 0 ]] || die_runtime "manifest ${MODELS_TSV} is empty or unparsable"

list_valid_models() {
    local i
    echo "Valid models (slug or full name):" >&2
    for i in "${!SLUGS[@]}"; do
        printf '  %-24s  %s\n' "${SLUGS[$i]}" "${MODELS[$i]}" >&2
    done
}

# --- resolve the model --------------------------------------------------------

SEL_INDEX=""
if [[ -z "$MODEL_ARG" ]]; then
    for i in "${!SLUGS[@]}"; do
        if [[ "${IS_DEFAULTS[$i]}" == "true" ]]; then
            SEL_INDEX="$i"
            break
        fi
    done
    [[ -n "$SEL_INDEX" ]] || die_runtime "manifest ${MODELS_TSV} has no is_default=true row"
else
    for i in "${!SLUGS[@]}"; do
        if [[ "$MODEL_ARG" == "${SLUGS[$i]}" || "$MODEL_ARG" == "${MODELS[$i]}" ]]; then
            SEL_INDEX="$i"
            break
        fi
    done
    if [[ -z "$SEL_INDEX" ]]; then
        echo "${SCRIPT_NAME}: error: unknown --model '${MODEL_ARG}'" >&2
        list_valid_models
        exit 2
    fi
fi

SEL_SLUG="${SLUGS[$SEL_INDEX]}"
SEL_MODEL="${MODELS[$SEL_INDEX]}"
SEL_DIM="${DIMS[$SEL_INDEX]}"
SEL_MEM_ING="${MEM_INGS[$SEL_INDEX]}"
SEL_MEM_MCP="${MEM_MCPS[$SEL_INDEX]}"
SEL_QPROMPT="${QPROMPTS[$SEL_INDEX]}"
SEL_PPROMPT="${PPROMPTS[$SEL_INDEX]}"

# --- resolve the image tag ---------------------------------------------------
# Never "latest": latest tracks the registry's current default model, which
# can change to a different model/dimension in a future release and would
# silently swap the embedding model under an existing database. The tag is
# always either the model's own slug (moving, but always paired with that
# one model) or an explicit version pinned to that slug.

if [[ -n "$VERSION" ]]; then
    IMAGE_TAG="v${VERSION_NUM}-${SEL_SLUG}"
else
    IMAGE_TAG="${SEL_SLUG}"
fi

IMAGE_INGESTION="ghcr.io/${OWNER}/self-docs-ingestion:${IMAGE_TAG}"
IMAGE_MCP="ghcr.io/${OWNER}/self-docs-mcp-server:${IMAGE_TAG}"

# --- preflight ----------------------------------------------------------------

case "$(uname -m)" in
    x86_64|aarch64|arm64) ;;
    *) warn "unrecognized architecture '$(uname -m)'; self-docs images are published for x86_64 and arm64/aarch64 only" ;;
esac

if [[ "$(id -u)" -eq 0 ]]; then
    warn "running as root (UID 0); this installer does not require root"
fi

# Compressed image-pair size (ingestion+mcp-server) per model, for the
# disk-space warning. Approximate, verified against the published images.
pair_size_gb() {
    case "$1" in
        bge-small-en-v1.5) printf '0.42' ;;
        bge-base-en-v1.5) printf '0.70' ;;
        mxbai-embed-large-v1) printf '1.9' ;;
        multilingual-e5-large) printf '3.0' ;;
        *) printf '1.0' ;;
    esac
}

check_disk_space() {
    local parent="$DIR"
    while [[ ! -d "$parent" ]]; do
        parent="$(dirname -- "$parent")"
    done
    local pair_gb
    pair_gb="$(pair_size_gb "$SEL_SLUG")"
    local avail_kb
    # `|| true`: if `df` itself fails (exotic filesystem, unmounted path,
    # etc.), the pipeline must not abort the whole install under
    # `set -e`+pipefail — it should just fall through to the empty-avail_kb
    # check below and skip this best-effort warning.
    avail_kb="$(df -Pk -- "$parent" 2>/dev/null | awk 'NR==2 {print $4}' || true)"
    if [[ -z "$avail_kb" ]]; then
        return 0
    fi
    # SECURITY: every piece of shell data (including $parent, which derives
    # from --dir / an ancestor of it) must be passed via -v, never spliced
    # into the awk program text — splicing lets a crafted path (awk
    # metacharacters, unbalanced quotes/braces) escape into awk code and
    # reach system(). All four values below are -v bound; none are
    # interpolated into the single-quoted program string.
    awk -v avail_kb="$avail_kb" -v pair_gb="$pair_gb" -v slug="$SEL_SLUG" -v parent="$parent" 'BEGIN {
        need_kb = pair_gb * 3 * 1024 * 1024
        if (avail_kb < need_kb) {
            printf "install.sh: warning: only %.1f GB free at %s; recommend >= %.1f GB (~3x the ~%s GB compressed image pair for %s)\n", avail_kb/1024/1024, parent, need_kb/1024/1024, pair_gb, slug > "/dev/stderr"
        }
    }'
}
check_disk_space

# (curl availability was already checked above, before the manifest lookup.)

# Non-empty install dir requires --force. Checked before any file is written
# or modified, and independent of --dry-run/--no-start, so a bare re-run
# against a populated directory never touches an existing .env.
if [[ -d "$DIR" ]] && [[ -n "$(ls -A -- "$DIR" 2>/dev/null)" ]] && [[ "$FORCE" != true ]]; then
    die_usage "install directory '${DIR}' already exists and is not empty; re-run with --force to overwrite, or choose a different --dir"
fi

# Target dir (or nearest existing ancestor) must be writable.
target_parent="$DIR"
while [[ ! -d "$target_parent" ]]; do
    target_parent="$(dirname -- "$target_parent")"
done
[[ -w "$target_parent" ]] || die_usage "'${target_parent}' is not writable"

check_port_free() {
    local port="$1" label="$2"
    if command -v lsof >/dev/null 2>&1; then
        if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            die_usage "port ${port} (${label}) is already in use on this host"
        fi
    elif (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
        exec 3>&- 3<&- 2>/dev/null || true
        die_usage "port ${port} (${label}) appears to be in use on this host"
    fi
}

require_docker_v2() {
    command -v docker >/dev/null 2>&1 || die_usage "docker CLI not found on PATH"
    local out
    if ! out="$(docker compose version 2>&1)"; then
        if command -v docker-compose >/dev/null 2>&1; then
            die_usage "found legacy 'docker-compose' (v1) but no 'docker compose' (v2) plugin; this installer requires Docker Compose v2"
        fi
        die_usage "'docker compose version' failed: ${out}"
    fi
    if ! grep -qE 'v2\.' <<<"$out"; then
        die_usage "'docker compose version' did not report v2 (got: ${out})"
    fi
    docker info >/dev/null 2>&1 || die_usage "docker daemon is not reachable (is it running?)"
}

if [[ "$DRY_RUN" != true ]]; then
    check_port_free "$PORT_API" "--port-api"
    check_port_free "$PORT_MCP" "--port-mcp"
    require_docker_v2
fi

# --- fetch helper --------------------------------------------------------------
# Fetches must fail loudly and never leave a truncated file: download/copy to
# a temp file in the destination directory, then mv (atomic, same filesystem)
# only on success.

fetch_file() {
    local rel="$1" dest="$2"
    local tmp
    tmp="$(mktemp "${dest}.XXXXXX")"
    if [[ -n "$SOURCE_DIR" ]]; then
        if ! cp -- "${SOURCE_DIR%/}/${rel}" "$tmp" 2>/dev/null; then
            rm -f -- "$tmp"
            die_runtime "failed to read '${rel}' from --source-dir '${SOURCE_DIR}'"
        fi
    else
        if ! curl -fsSL "${BASE_URL}/${rel}" -o "$tmp"; then
            rm -f -- "$tmp"
            die_runtime "failed to fetch '${rel}' from ${BASE_URL} (check network connectivity, or pass --source-dir)"
        fi
    fi
    mv -- "$tmp" "$dest"
    # `mktemp` creates its file 0600 and `mv` preserves that mode — harmless
    # for docker-compose.yml, but fatal for db/init/*.sql on Linux: ./db/init
    # is bind-mounted straight into the postgres container's
    # /docker-entrypoint-initdb.d, and postgres runs as a non-root uid (not
    # the installing user's uid), so a 0600 file it doesn't own is
    # unreadable. Worse, postgres never re-scans db/init on a non-empty
    # volume, so a `chmod`-and-retry after the fact does not repair an
    # already-half-initialized volume — only `down -v` (data loss) does. Both
    # the --source-dir (cp) and network (curl) branches above land at 0600
    # for the same reason, so this fix applies uniformly to both.
    chmod -- 0644 "$dest"
}

generate_secret() {
    local secret=""
    if command -v openssl >/dev/null 2>&1; then
        secret="$(openssl rand -hex 32)"
    elif [[ -r /dev/urandom ]]; then
        secret="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    else
        die_runtime "cannot generate secrets: neither openssl nor /dev/urandom is available"
    fi
    if [[ "${#secret}" -ne 64 ]]; then
        die_runtime "secret generation produced ${#secret} characters, expected 64 hex characters"
    fi
    printf '%s' "$secret"
}

# --- render the install directory ----------------------------------------------

mkdir -p -- "${DIR}/db/init"

# 02_sources_config.sql, 03_fix_embedding_dim.sql, 04_upload_sources.sql and
# docker-compose.yml are byte-identical across every model in the manifest —
# model-independent — so they're safe to fetch/write here, before the
# --force + surviving-volume decision below. docker-compose.yml specifically
# MUST already be on disk at this point: detect_pgdata_volume_status() (below)
# needs it to resolve this directory's Compose project name.
#
# 01_schema.sql is the ONE file that varies per model (`vector(N)`), and its
# render is deliberately placed AFTER that decision block instead of here —
# see the comment down there for why.
for f in 02_sources_config.sql 03_fix_embedding_dim.sql 04_upload_sources.sql; do
    fetch_file "db/init/${f}" "${DIR}/db/init/${f}"
done

fetch_file "deploy/docker-compose.yml" "${DIR}/docker-compose.yml"

ENV_PATH="${DIR}/.env"

get_env_value() {
    # Prints the value of KEY=... from FILE (last match wins, dotenv-style),
    # or empty if the file/key doesn't exist. Never fails under `set -e`
    # +pipefail: the `|| true` is deliberately attached to the pipeline
    # itself (not a separate statement) so a non-matching grep can't abort
    # the script here.
    local key="$1" file="$2" val=""
    if [[ -f "$file" ]]; then
        val="$(grep "^${key}=" "$file" 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
    fi
    printf '%s' "$val"
}

# --- --force + a surviving pgdata volume: preserve POSTGRES_PASSWORD --------
# `pgdata` is a Docker-managed named volume; a plain `docker compose down`
# does NOT remove it (only `down -v` does), and postgres only ever consults
# POSTGRES_PASSWORD while initializing an EMPTY volume — it never re-applies
# it afterward. So if --force overwrites .env with a freshly generated
# password while the old volume survives, ingestion/mcp-server come up
# pointed at the new password, but postgres still only accepts the old one:
# auth fails, and there is no way back to the already-indexed corpus short of
# `down -v` (i.e. deleting it).
#
# Chosen fix: PRESERVE the existing POSTGRES_PASSWORD across --force rather
# than refuse outright. Refusing would make --force useless for its main
# legitimate purpose (re-rendering docker-compose.yml/.env/schema for an
# existing install) any time a volume happens to still exist, which is the
# common case, not an edge case. Detection order, matching "preserve only
# when a volume actually exists, fresh --force into a truly empty case still
# gets a new password":
#   1. No prior .env in this directory (or it has no POSTGRES_PASSWORD line)
#      -> nothing to preserve; generate fresh, same as any first install.
#   2. A prior .env exists AND `docker volume inspect` (via the project name
#      Compose itself resolves for this directory, never guessed at by
#      pattern-matching bash) CONFIRMS the "pgdata" volume still exists
#      -> preserve the old password; this is the documented, expected path.
#   3. A prior .env exists AND the volume is CONFIRMED to no longer exist
#      (e.g. the user already ran `down -v`) -> a fresh password is safe;
#      generate one, exactly like a first install.
#   4. A prior .env exists but volume existence could not be determined at
#      all (docker missing, --dry-run, `docker compose config` failed) ->
#      err toward not bricking a possibly-surviving volume: preserve, and
#      say so, rather than silently guessing "not there".
detect_pgdata_volume_status() {
    # Returns (via exit status): 0 = volume confirmed to exist,
    # 1 = confirmed NOT to exist, 2 = could not be determined either way.
    command -v docker >/dev/null 2>&1 || return 2
    [[ -f "${DIR}/docker-compose.yml" ]] || return 2
    local cfg project
    cfg="$( (cd "$DIR" && docker compose config --format json) 2>/dev/null || true )"
    [[ -n "$cfg" ]] || return 2
    # `|| true` guards the pipeline itself: if `cfg` has no "name" field
    # (unexpected, but not impossible on an old Compose release), grep's
    # non-zero exit must not abort the script here — an empty $project
    # correctly falls through to "could not be determined" below instead.
    #
    # The leading `^[^"]*` (not a greedy `.*`) is deliberate: `docker compose
    # config --format json` happens to pretty-print with "name" as the first
    # key today, but that's an implementation detail, not a guarantee. Against
    # compact/single-line JSON (e.g. `... | jq -c`), a greedy `.*"name"` would
    # match the *last* occurrence of the literal text "name" in the whole
    # document instead of the intended top-level key — silently resolving the
    # wrong project name, and therefore the wrong volume, and therefore
    # concluding "confirmed absent" when a volume is actually present. Anchor
    # from the start of the line and stop at the first `"name"` instead.
    project="$(printf '%s' "$cfg" | grep -m1 '"name"' | sed -E 's/^[^"]*"name"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/' || true)"
    [[ -n "$project" ]] || return 2
    if docker volume inspect "${project}_pgdata" >/dev/null 2>&1; then
        return 0
    else
        return 1
    fi
}

OLD_POSTGRES_PASSWORD=""
OLD_EMBEDDING_MODEL_NAME=""
OLD_EMBEDDING_DIM=""
if [[ "$FORCE" == true ]]; then
    OLD_POSTGRES_PASSWORD="$(get_env_value "POSTGRES_PASSWORD" "$ENV_PATH")"
    OLD_EMBEDDING_MODEL_NAME="$(get_env_value "EMBEDDING_MODEL_NAME" "$ENV_PATH")"
    OLD_EMBEDDING_DIM="$(get_env_value "EMBEDDING_DIM" "$ENV_PATH")"

    # Detection runs UNCONDITIONALLY on --force (not gated on whether an old
    # password happens to exist): a volume can survive with a missing,
    # unreadable, or hand-edited .env just as easily as with an intact one,
    # and "no old password found" must never silently fall through to
    # regenerating one against a volume that might still be there.
    VOL_STATUS=2
    if [[ "$DRY_RUN" != true ]]; then
        # `detect_pgdata_volume_status`'s 1/2 return codes are normal,
        # expected outcomes here, not errors — it MUST be called as an `if`
        # condition (exempt from `set -e`), never as a bare statement, or a
        # non-zero return would abort the whole script instead of being
        # handled below.
        if detect_pgdata_volume_status; then
            VOL_STATUS=0
        else
            VOL_STATUS=$?
        fi
    fi

    case "$VOL_STATUS" in
        1)
            # Confirmed absent: nothing survives to disagree with, so both a
            # fresh password AND a changed model/dim are safe here — this is
            # exactly the "fresh --force into a directory with no surviving
            # volume" case and must behave like a first install.
            POSTGRES_PASSWORD="$(generate_secret)"
            ;;
        *)
            # 0 = volume CONFIRMED present. Anything else (2) = existence
            # could not be determined (docker unavailable, --dry-run, or
            # `docker compose config` failed) — treated the same as "assume
            # it might be present" for BOTH checks below, on the same
            # fail-closed logic already used for the password: refusing on a
            # false alarm is recoverable (retry with docker available, or
            # confirm manually); silently proceeding into either a bricked
            # password or a live dimension mismatch is not.
            if [[ "$VOL_STATUS" == 0 ]]; then
                VOL_DESC="an existing pgdata volume was found for this install"
            else
                VOL_DESC="a pgdata volume could not be ruled out for this install (docker unavailable, --dry-run, or 'docker compose config' could not resolve this directory — e.g. a missing/incomplete .env)"
            fi

            # --- model/dimension change against a (possibly-)surviving volume ---
            # Postgres NEVER re-applies db/init to a non-empty volume, so a
            # model change here would leave the live `vector(N)` column at
            # the OLD dimension while the image label, .env, and freshly
            # rendered schema all move to the NEW one — every insert/query
            # then fails (or worse, silently corrupts) while the installer
            # itself reports success. This is the one case --force must
            # never paper over; there is no safe default to fall back to.
            #
            # Gated on BOTH fields being non-empty, not just present-and-
            # differing: an .env that lost EMBEDDING_MODEL_NAME/EMBEDDING_DIM
            # (hand-edited, truncated, etc.) but kept POSTGRES_PASSWORD is the
            # same class of gap as the password check below — we no longer
            # know what model the live volume was initialized for, so we
            # cannot claim compatibility by omission either.
            if [[ -z "$OLD_EMBEDDING_MODEL_NAME" ]] || [[ -z "$OLD_EMBEDDING_DIM" ]]; then
                die_usage "${VOL_DESC}, but its EMBEDDING_MODEL_NAME/EMBEDDING_DIM could not be recovered from ${ENV_PATH} (missing, unreadable, or hand-edited); proceeding without knowing what model the live volume was initialised for risks a silent vector-dimension mismatch. Run 'docker compose down -v' in ${DIR} then re-run this installer (destroys the index), or install into a fresh --dir."
            elif [[ "$OLD_EMBEDDING_MODEL_NAME" != "$SEL_MODEL" ]] || [[ "$OLD_EMBEDDING_DIM" != "$SEL_DIM" ]]; then
                die_usage "${VOL_DESC}, initialised for '${OLD_EMBEDDING_MODEL_NAME}' (dim ${OLD_EMBEDDING_DIM}); installing '${SEL_MODEL}' (dim ${SEL_DIM}) against it would leave the live schema at vector(${OLD_EMBEDDING_DIM}) — Postgres never re-applies db/init to a non-empty volume. Run 'docker compose down -v' in ${DIR} then re-run this installer (destroys the index), or install into a fresh --dir."
            fi

            # --- password recovery -----------------------------------------
            if [[ -z "$OLD_POSTGRES_PASSWORD" ]]; then
                die_usage "${VOL_DESC}, but its POSTGRES_PASSWORD could not be recovered from ${ENV_PATH} (missing, unreadable, or no POSTGRES_PASSWORD= line); generating a new one here would lock you out of the existing data instead of failing loudly. Run 'docker compose down -v' in ${DIR} then re-run this installer (destroys the index), or install into a fresh --dir."
            fi

            POSTGRES_PASSWORD="$OLD_POSTGRES_PASSWORD"
            if [[ "$VOL_STATUS" == 0 ]]; then
                warn "--force: ${VOL_DESC}; preserving POSTGRES_PASSWORD from ${ENV_PATH} instead of generating a new one (postgres only applies POSTGRES_PASSWORD when initializing an EMPTY volume — a new one here would lock you out of the existing data)"
            else
                warn "--force: ${VOL_DESC}; preserving the existing POSTGRES_PASSWORD from ${ENV_PATH} as a precaution. For a guaranteed-fresh password on a guaranteed-fresh volume, run 'docker compose down -v' in ${DIR} first."
            fi
            ;;
    esac
else
    POSTGRES_PASSWORD="$(generate_secret)"
fi

# 01_schema.sql is always rendered from the template (one code path for
# every model) — never copied verbatim, since the committed repo copy is
# only a rendering for the registry-default model.
#
# Deliberately rendered HERE — after the --force + surviving-volume decision
# above, not alongside the other three (model-independent) db/init/*.sql
# files earlier — because it is the one file that DOES vary per model. A
# refused --force (model/dim mismatch, or an unrecoverable password) must
# leave EVERY file on disk exactly as it was, `.env` included: rendering
# this first and refusing second used to leave a directory with the OLD
# `.env` sitting next to a NEW, rejected model's `01_schema.sql` — inert
# against the surviving (non-empty) volume, but a live trap the moment an
# operator ran the documented `docker compose down -v && docker compose up
# -d` recovery sequence, since an EMPTY volume is exactly when postgres DOES
# apply db/init. Rendering after the refusal point means a refusal now
# leaves the directory byte-for-byte as it was before this run.
#
# SECURITY (belt and braces): SEL_DIM already passed DIM_RE ("^[0-9]+$") above,
# but this substitution is deliberately done with awk's -v + gsub() rather
# than `sed "s/.../${SEL_DIM}/g"`. A sed s-command splices its replacement
# into program text — a value containing '/', 'e', 'w' etc. can break out of
# the s-command entirely (GNU sed's `e`/`w` flags execute shell commands /
# write files). awk -v binds the value as data, never as program text, so
# there is no delimiter or flag character that could ever escape it, even if
# the upstream validation above were ever loosened or bypassed.
SCHEMA_TEMPLATE_TMP="${DIR}/db/init/.01_schema.sql.template.fetched"
fetch_file "db/init/01_schema.sql.template" "$SCHEMA_TEMPLATE_TMP"
awk -v dim="$SEL_DIM" '{ gsub(/__EMBEDDING_DIM__/, dim); print }' "$SCHEMA_TEMPLATE_TMP" > "${DIR}/db/init/01_schema.sql"
rm -f -- "$SCHEMA_TEMPLATE_TMP"
# Explicit, not left to the ambient umask: must match the other three
# db/init/*.sql files (0644) so postgres — running as a non-root,
# non-installing-user uid on Linux — can read all four via the ./db/init
# bind mount, not just whichever ones happened to land readable by luck.
chmod -- 0644 "${DIR}/db/init/01_schema.sql"
if grep -q '__EMBEDDING_DIM__' "${DIR}/db/init/01_schema.sql"; then
    die_runtime "rendered 01_schema.sql still contains __EMBEDDING_DIM__ (template placeholder mismatch)"
fi

SYNC_TOKEN="$(generate_secret)"
MCP_TOKEN="$(generate_secret)"
TZ_VALUE="${TZ:-UTC}"

OLD_UMASK="$(umask)"
umask 077
{
    echo "# self-docs standalone deployment kit — generated by deploy/install.sh"
    echo "# model: ${SEL_MODEL} (slug: ${SEL_SLUG}, dim: ${SEL_DIM})"
    echo "# image tag: ${IMAGE_TAG}"
    echo
    echo "SELF_DOCS_OWNER=${OWNER}"
    echo "SELF_DOCS_IMAGE_TAG=${IMAGE_TAG}"
    echo
    echo "POSTGRES_USER=self_docs"
    echo "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}"
    echo "POSTGRES_DB=self_docs"
    echo
    echo "SYNC_TOKEN=${SYNC_TOKEN}"
    echo "MCP_TOKEN=${MCP_TOKEN}"
    echo
    echo "EMBEDDING_MODEL_NAME=${SEL_MODEL}"
    echo "EMBEDDING_DIM=${SEL_DIM}"
    # Quoted so the (possibly trailing-space, possibly empty) prompt values
    # survive docker compose's .env parsing unchanged.
    printf 'EMBEDDING_QUERY_PROMPT="%s"\n' "$SEL_QPROMPT"
    printf 'EMBEDDING_PASSAGE_PROMPT="%s"\n' "$SEL_PPROMPT"
    echo
    echo "INGESTION_MEM_LIMIT=${SEL_MEM_ING}"
    echo "MCP_MEM_LIMIT=${SEL_MEM_MCP}"
    echo
    echo "SELF_DOCS_API_PORT=${PORT_API}"
    echo "SELF_DOCS_MCP_PORT=${PORT_MCP}"
    echo
    echo "TZ=${TZ_VALUE}"
} > "$ENV_PATH"
umask "$OLD_UMASK"
chmod -- 600 "$ENV_PATH"

echo "${SCRIPT_NAME}: wrote ${DIR}/db/init/{01_schema.sql,02_sources_config.sql,03_fix_embedding_dim.sql,04_upload_sources.sql}"
echo "${SCRIPT_NAME}: wrote ${DIR}/docker-compose.yml"
echo "${SCRIPT_NAME}: wrote ${ENV_PATH} (mode 600) for model '${SEL_MODEL}' (dim ${SEL_DIM}), image tag '${IMAGE_TAG}'"

if [[ "$DRY_RUN" == true ]]; then
    echo "${SCRIPT_NAME}: --dry-run: no docker invocations made; stopping here"
    exit 0
fi

# --- pull + verify -------------------------------------------------------------

if ! (cd "$DIR" && docker compose pull); then
    die_runtime "docker compose pull failed"
fi

get_label() {
    local ref="$1" label="$2"
    docker image inspect "$ref" --format "{{index .Config.Labels \"${label}\"}}" 2>/dev/null || printf ''
}

verify_image_labels() {
    local ref="$1"
    local got_model got_dim
    got_model="$(get_label "$ref" "io.self-docs.embedding-model")"
    got_dim="$(get_label "$ref" "io.self-docs.embedding-dim")"
    # Fail closed: an absent/empty label (command failure OR missing key)
    # must never pass silently.
    if [[ -z "$got_model" ]] || [[ -z "$got_dim" ]]; then
        die_runtime "image '${ref}' has no io.self-docs.* labels — this tag predates the per-model image matrix, use >= v0.1.0"
    fi
    if [[ "$got_model" != "$SEL_MODEL" ]]; then
        die_runtime "image '${ref}' is labeled for model '${got_model}', expected '${SEL_MODEL}' — refusing to pair a mismatched model with this schema"
    fi
    if [[ "$got_dim" != "$SEL_DIM" ]]; then
        die_runtime "image '${ref}' is labeled dim=${got_dim}, expected dim=${SEL_DIM} — refusing to pair a mismatched dimension with this schema"
    fi
}

if [[ "$NO_VERIFY_LABELS" != true ]]; then
    verify_image_labels "$IMAGE_INGESTION"
    verify_image_labels "$IMAGE_MCP"
else
    warn "--no-verify-labels: skipping the image/model label check"
fi

if [[ "$NO_START" == true ]]; then
    echo "${SCRIPT_NAME}: --no-start: pulled and verified, not starting. Run 'docker compose up -d' in ${DIR} when ready."
    exit 0
fi

# --- start -----------------------------------------------------------------

# `docker compose up -d --wait` (Compose v2 native health-wait) fails non-zero
# on timeout or on any service reporting unhealthy. That MUST be a hard
# failure here, not a warning: the stack exists on disk at this point, but
# printing "self-docs is up." would lie to the caller (and to any script
# checking our exit code). Distinct from a usage/preflight error (exit 2) —
# this is a runtime failure (exit 1); --force is not the fix.
if ! (cd "$DIR" && docker compose up -d --wait --wait-timeout 240); then
    echo "${SCRIPT_NAME}: error: containers were created in ${DIR} but did not report healthy within the wait timeout." >&2
    (cd "$DIR" && docker compose ps) >&2 || true
    echo "${SCRIPT_NAME}: error: inspect with 'docker compose logs' in ${DIR} to see why (e.g. embedding model load, Postgres connectivity, or — check ingestion/mcp-server logs for 'password authentication failed' — a POSTGRES_PASSWORD/pgdata volume mismatch). The stack already exists on disk — re-running with --force is not the fix; diagnose and restart the containers directly (e.g. 'docker compose restart' or 'docker compose down' then investigate) once fixed." >&2
    exit 1
fi

echo
echo "self-docs is up."
echo "  Admin UI:     http://127.0.0.1:${PORT_API}/admin"
echo "  MCP endpoint: http://127.0.0.1:${PORT_MCP}/mcp"
echo "  Trigger a sync:"
echo "    curl -X POST http://127.0.0.1:${PORT_API}/sync -H \"Authorization: Bearer \$(grep -m1 '^SYNC_TOKEN=' ${ENV_PATH} | cut -d= -f2-)\""
echo "  SYNC_TOKEN and MCP_TOKEN live in ${ENV_PATH} (mode 600)."
