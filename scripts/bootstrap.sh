#!/bin/sh
# clique bootstrap: venv install + `clique` on PATH, one line for joiners.
#
# Server-first (no GitHub account, no PAT needed when the clique is up):
#   export CLIQUE_SERVER=http://<server-ip>:7777
#   curl -fsSL $CLIQUE_SERVER/join.sh | sh        # preferred: server's own bundle
#   # or: CLIQUE_SERVER=http://<server-ip>:7777 sh scripts/bootstrap.sh
#
# GitHub fallback (server unreachable, or dev checkout with history):
#   export CLIQUE_GITHUB_TOKEN=github_pat_...     # contents:read on hongnoul/tcj
#   curl -fsSL -H "Authorization: Bearer $CLIQUE_GITHUB_TOKEN" \
#     https://raw.githubusercontent.com/hongnoul/tcj/main/scripts/bootstrap.sh | sh
# or from a clone:  sh scripts/bootstrap.sh
set -eu

REPO_URL="https://github.com/hongnoul/tcj"
INSTALL_DIR="${CLIQUE_HOME:-$HOME/.clique/app}"
BIN_DIR="${CLIQUE_BIN:-$HOME/.local/bin}"

main() {
    echo ""
    echo "   ( )--( )--( )"
    echo "    |    |    |     clique installer"
    echo "   ( )--( )--( )   idle laptops, one pool"
    echo ""

    OS="$(uname -s)"
    case "$OS" in
        Linux)  os="linux" ;;
        Darwin) os="macos" ;;
        *)      os="$OS" ;;
    esac
    ARCH="$(uname -m)"
    case "$ARCH" in
        x86_64|amd64)   arch="x86_64" ;;
        aarch64|arm64)  arch="aarch64" ;;
        *)              arch="$ARCH" ;;
    esac
    log "detected ${os}/${arch}"

    need git
    need curl
    need tar

    # find python >= 3.11
    PY=""
    for cand in python3.13 python3.12 python3.11 python3; do
        if command -v "$cand" >/dev/null 2>&1; then
            if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)'; then
                PY="$cand"; break
            fi
        fi
    done
    [ -n "$PY" ] || err "python 3.11+ required (have: $(command -v python3 || echo none))"
    log "using $($PY --version 2>&1)"

    # headless-safe: never prompt for credentials (fail fast instead of hanging
    # with no tty, e.g. ssh < /dev/null or systemd unit).
    export GIT_TERMINAL_PROMPT=0
    export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new}"

    # Private-repo support: export CLIQUE_GITHUB_TOKEN (fine-grained PAT, contents:read)
    # on machines with no other GitHub credentials. Injected via git's
    # GIT_CONFIG_* env (Authorization header), so the token never lands in the
    # remote URL, and plain `git clone` / `git pull` pick it up with no arg plumbing.
    AUTH_HEADER=""
    if [ -n "${CLIQUE_GITHUB_TOKEN:-}" ]; then
        AUTH_HEADER="Authorization: Bearer ${CLIQUE_GITHUB_TOKEN}"
        export GIT_CONFIG_COUNT=1
        export GIT_CONFIG_KEY_0="http.https://github.com/.extraheader"
        export GIT_CONFIG_VALUE_0="$AUTH_HEADER"
        # Never fall back to interactive credential helpers on a headless box:
        # a bad/missing token must fail fast, not hang or silently use stale creds.
        export GIT_ASKPASS="$(command -v false)"
        log "using CLIQUE_GITHUB_TOKEN for auth"
    fi

    # get source: use cwd if it is a clone, else server bundle (no GitHub),
    # else clone/pull (tarball fallback)
    if [ -f "pyproject.toml" ] && grep -q '^name = "clique"' pyproject.toml 2>/dev/null; then
        SRC_DIR="$(pwd)"
        log "using local checkout: $SRC_DIR"
    elif [ -n "${CLIQUE_SERVER:-}" ]; then
        # Server-first: git history comes from the clique itself
        # ($SERVER/repo.bundle), so teammates need no GitHub account or PAT.
        log "fetching source from clique server ${CLIQUE_SERVER}..."
        fetch_server_bundle "$INSTALL_DIR" "${CLIQUE_SERVER}" || exit 1
        SRC_DIR="$INSTALL_DIR"
    else
        if [ -d "$INSTALL_DIR/.git" ]; then
            log "updating $INSTALL_DIR..."
            if ! git -C "$INSTALL_DIR" pull --ff-only < /dev/null; then
                err "git pull --ff-only failed in $INSTALL_DIR (fix: git -C \"$INSTALL_DIR\" fetch origin && git -C \"$INSTALL_DIR\" reset --hard origin/main)"
            fi
        elif [ -e "$INSTALL_DIR" ]; then
            if [ -f "$INSTALL_DIR/pyproject.toml" ]; then
                # tarball install from a previous fallback: refresh it
                log "refreshing tarball install..."
                fetch_tarball "$INSTALL_DIR" "$AUTH_HEADER" || exit 1
            else
                err "$INSTALL_DIR exists but is not a clique checkout; move it aside and retry"
            fi
        else
            mkdir -p "$(dirname "$INSTALL_DIR")"
            log "cloning $REPO_URL..."
            CLONE_LOG="${TMPDIR:-/tmp}/clique-clone-$$.log"
            if git clone --depth 1 "$REPO_URL" "$INSTALL_DIR" < /dev/null 2>"$CLONE_LOG"; then
                rm -f "$CLONE_LOG"
            else
                echo "error: git clone failed:" >&2
                cat "$CLONE_LOG" 2>/dev/null || true
                rm -f "$CLONE_LOG"
                echo "" >&2
                echo "NOTE: this repo (hongnoul/tcj) is PRIVATE." >&2
                echo "The headless machine has no GitHub credentials, so git cannot even" >&2
                echo "ask for a username (no tty) and aborts. Fix: create a fine-grained" >&2
                echo "PAT (github.com/settings/tokens, contents:read on hongnoul/tcj)." >&2
                echo "If you fetched this script without a token, re-fetch it WITH the token:" >&2
                echo "  export CLIQUE_GITHUB_TOKEN=github_pat_..." >&2
                echo "  curl -fsSL -H \"Authorization: Bearer \$CLIQUE_GITHUB_TOKEN\" \\" >&2
                echo "    https://raw.githubusercontent.com/hongnoul/tcj/main/scripts/bootstrap.sh | sh" >&2
                echo "Or make the repo public." >&2
                echo "" >&2
                diag_clone_failure
                fetch_tarball "$INSTALL_DIR" "$AUTH_HEADER" || {
                    echo "retry with: GIT_CURL_VERBOSE=1 git clone \"$REPO_URL\" \"$INSTALL_DIR\"" >&2
                    exit 1
                }
            fi
        fi
        SRC_DIR="$INSTALL_DIR"
    fi

    # venv + install
    log "creating virtualenv..."
    "$PY" -m venv "$SRC_DIR/.venv"
    log "installing clique (this takes a minute)..."
    "$SRC_DIR/.venv/bin/pip" install -q -U pip
    "$SRC_DIR/.venv/bin/pip" install -q -e "$SRC_DIR"

    # symlink entry points onto PATH
    log "linking..."
    mkdir -p "$BIN_DIR"
    for cmd in clique clique-agent clique-server; do
        ln -sf "$SRC_DIR/.venv/bin/$cmd" "$BIN_DIR/$cmd"
    done

    "$BIN_DIR/clique" --help >/dev/null 2>&1 || err "install check failed ('clique --help')"
    log "installed clique to ${BIN_DIR}/clique"

    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *)
            echo ""
            warn "${BIN_DIR} is not in your PATH"
            echo "  add it to your shell config:"
            echo ""
            echo "    export PATH=\"${BIN_DIR}:\$PATH\""
            echo ""
            ;;
    esac

    echo ""
    log "ready. run 'clique onboard --dry' to check, then 'clique onboard' to join."
    echo ""
    echo "  join a clique:   clique join --server http://<server-ip>:7777 --runtime echo --param-b 7"
    echo "  or with a model: clique join --server http://<server-ip>:7777 --runtime openai-compat --model-name qwen2.5-coder:7b --param-b 7"
    echo ""
}

diag_clone_failure() {
    # Best-effort diagnostics; every probe is guarded so `set -e` can't kill us.
    echo "--- diagnostics (copy this whole block when asking for help) ---" >&2
    echo "date: $(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)" >&2
    echo "tty: $(tty 2>&1 || true)" >&2
    echo "git: $(git --version 2>&1 || echo MISSING)" >&2
    echo "curl: $(curl --version 2>/dev/null | head -n 1 || echo MISSING)" >&2
    echo "tar: $(tar --version 2>/dev/null | head -n 1 || echo MISSING)" >&2
    for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY no_proxy; do
        eval "val=\${$v:-}" || val=""
        if [ -n "$val" ]; then echo "proxy: $v is set" >&2; fi
    done
    echo "dns github.com: $(getent hosts github.com 2>&1 | head -n 2 || echo 'getent failed')" >&2
    echo "tls github.com: $(curl -fsSL -o /dev/null -w '%{http_code}' --max-time 15 https://github.com 2>&1 || echo FAILED)" >&2
    echo "--- end diagnostics ---" >&2
}

fetch_server_bundle() {
    # Server-first source: clone full git history from the clique itself
    # ($SERVER/repo.bundle). No GitHub account, no PAT, no ssh key.
    # Falls back to $SERVER/app.tgz (snapshot, no history) when the
    # server has no git (tarball installs report server_sha=unknown).
    dest="$1"
    srv="$2"
    case "$dest" in
        ""|"/"|"$HOME"|"$HOME/" ) echo "error: refusing to unpack bundle into '$dest'" >&2; return 1;;
    esac
    tmpfile="${TMPDIR:-/tmp}/clique-repo-$$.bundle"
    rm -f "$tmpfile"
    if ! curl -fsSL --max-time 120 "$srv/repo.bundle" -o "$tmpfile"; then
        echo "error: bundle download failed ($srv/repo.bundle unreachable)" >&2
        rm -f "$tmpfile"
        return 1
    fi
    if od -An -N2 -tx1 "$tmpfile" | tr -d ' \n' | grep -q '^1f8b'; then
        # gzip fallback: server has no git history, serve snapshot instead
        log "server has no git history; installing snapshot..."
        rm -rf "$dest"
        mkdir -p "$dest"
        if tar -xz -C "$dest" -f "$tmpfile"; then
            rm -f "$tmpfile"
            return 0
        fi
        echo "error: snapshot unpack failed" >&2
        rm -f "$tmpfile"
        return 1
    fi
    rm -rf "$dest"
    if git clone -q "$tmpfile" "$dest" < /dev/null 2>/dev/null; then
        rm -f "$tmpfile"
        log "cloned history from clique server (no GitHub involved)"
        return 0
    fi
    echo "error: bundle clone failed" >&2
    head -c 200 "$tmpfile" | tr -d '\0' | head -n 3 || true
    rm -f "$tmpfile"
    return 1
}

fetch_tarball() {    # Fallback when git is broken/missing-ca/blocked: no git needed, just curl+tar.
    # Downloads to a temp file first, validates the gzip magic, then unpacks, so
    # an HTML error page can never be mistaken for a source tree.
    dest="$1"
    auth="$2"
    command -v curl >/dev/null 2>&1 || { echo "error: curl not found, can't fetch tarball" >&2; return 1; }
    command -v tar >/dev/null 2>&1 || { echo "error: tar not found, can't unpack tarball" >&2; return 1; }
    case "$dest" in
        ""|"/"|"$HOME"|"$HOME/" ) echo "error: refusing to unpack tarball into '$dest'" >&2; return 1;;
    esac
    tmpfile="${TMPDIR:-/tmp}/clique-tarball-$$.tgz"
    rm -f "$tmpfile"
    if [ -n "$auth" ]; then
        if ! curl_err=$(curl -fsSL -H "$auth" --max-time 120 "$TARBALL_URL" -o "$tmpfile" 2>&1); then
            echo "error: tarball download failed ($curl_err)" >&2
            rm -f "$tmpfile"
            return 1
        fi
    elif ! curl -fsSL --max-time 120 "$TARBALL_URL" -o "$tmpfile"; then
        echo "error: tarball download failed" >&2
        rm -f "$tmpfile"
        return 1
    fi
    # gzip magic 1f 8b, checked portably (script runs under POSIX sh, not bash;
    # BSD od separates bytes with two spaces, so strip whitespace first)
    if ! od -An -N2 -tx1 "$tmpfile" | tr -d ' \n' | grep -q '^1f8b'; then
        echo "error: tarball download is not gzip (bad token? private repo?)" >&2
        head -c 300 "$tmpfile" | tr -d '\0' | head -n 5 || true
        rm -f "$tmpfile"
        return 1
    fi
    rm -rf "$dest"
    mkdir -p "$dest"
    if tar -xz --strip-components=1 -C "$dest" -f "$tmpfile"; then
        rm -f "$tmpfile"
        if [ -f "$dest/pyproject.toml" ] && grep -q '^name = "clique"' "$dest/pyproject.toml" 2>/dev/null; then
            log "installed from tarball (no .git history; re-run bootstrap to refresh)"
            return 0
        fi
        echo "error: tarball unpacked but is not a clique checkout" >&2
        return 1
    fi
    echo "error: tarball unpack failed" >&2
    rm -f "$tmpfile"
    return 1
}

TARBALL_URL="https://github.com/hongnoul/tcj/archive/refs/heads/main.tar.gz"

log()  { printf '  \033[32m>\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
err()  { printf '  \033[31mx\033[0m %s\n' "$1" >&2; exit 1; }

need() {
    if ! command -v "$1" >/dev/null 2>&1; then
        case "$1" in
            git) err "requires 'git' — install it first (arch: sudo pacman -S --needed git python; mac: xcode-select --install)" ;;
            *)   err "requires '$1' — install it first" ;;
        esac
    fi
}

main "$@"
