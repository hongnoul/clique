#!/bin/sh
# clique bootstrap: venv install + `clique` on PATH, one line for joiners.
#   curl -fsSL https://raw.githubusercontent.com/hongnoul/tcj/main/scripts/bootstrap.sh | sh
# or from a clone:  sh scripts/bootstrap.sh
set -eu

REPO_URL="https://github.com/hongnoul/tcj"
INSTALL_DIR="${CLIQUE_HOME:-$HOME/.clique/app}"
BIN_DIR="${CLIQUE_BIN:-$HOME/.local/bin}"

# find python >= 3.11
PY=""
for cand in python3.13 python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)'; then
            PY="$cand"; break
        fi
    fi
done
[ -n "$PY" ] || { echo "error: python 3.11+ required"; exit 1; }
echo "using $($PY --version)"

# headless-safe: never prompt for credentials (fail fast instead of hanging
# with no tty, e.g. ssh < /dev/null or systemd unit).
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new}"

command -v git >/dev/null 2>&1 || {
    echo "error: git not found. install it first:"
    echo "  arch:  sudo pacman -S --needed git python"
    echo "  mac:   xcode-select --install"
    exit 1
}

TARBALL_URL="https://github.com/hongnoul/tcj/archive/refs/heads/main.tar.gz"

# Private-repo support: export CLIQUE_GITHUB_TOKEN (fine-grained PAT, contents:read)
# on machines with no other GitHub credentials. If unset and the clone fails
# with an auth error, we tell the user exactly that (this repo is private).
if [ -n "${CLIQUE_GITHUB_TOKEN:-}" ]; then
    AUTH_REPO_URL="https://oauth2:${CLIQUE_GITHUB_TOKEN}@github.com/hongnoul/tcj"
    AUTH_TARBALL_HEADER="Authorization: Bearer ${CLIQUE_GITHUB_TOKEN}"
else
    AUTH_REPO_URL="$REPO_URL"
    AUTH_TARBALL_HEADER=""
fi

diag_clone_failure() {
    # Best-effort diagnostics; every probe is guarded so `set -e` can't kill us.
    echo "--- diagnostics (copy this whole block when asking for help) ---"
    echo "date: $(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)"
    echo "tty: $(tty 2>&1 || true)"
    echo "git: $(git --version 2>&1 || echo MISSING)"
    echo "curl: $(curl --version 2>/dev/null | head -n 1 || echo MISSING)"
    echo "tar: $(tar --version 2>/dev/null | head -n 1 || echo MISSING)"
    for v in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY no_proxy; do
        eval "val=\${$v:-}" || val=""
        if [ -n "$val" ]; then echo "proxy: $v is set"; fi
    done
    echo "dns github.com: $(getent hosts github.com 2>&1 | head -n 2 || echo 'getent failed')"
    echo "tls github.com: $(curl -fsSL -o /dev/null -w '%{http_code}' --max-time 15 https://github.com 2>&1 || echo FAILED)"
    echo "--- end diagnostics ---"
}

fetch_tarball() {
    # Fallback when git is broken/missing-ca/blocked: no git needed, just curl+tar.
    dest="$1"
    command -v curl >/dev/null 2>&1 || { echo "error: curl not found, can't fetch tarball"; return 1; }
    command -v tar >/dev/null 2>&1 || { echo "error: tar not found, can't unpack tarball"; return 1; }
    rm -rf "$dest"
    mkdir -p "$dest"
    echo "git clone failed; trying tarball"
    if [ -n "$AUTH_TARBALL_HEADER" ]; then
        DL_OK=$(curl -fsSL -H "$AUTH_TARBALL_HEADER" --max-time 120 "$TARBALL_URL" | tar -xz --strip-components=1 -C "$dest" 2>&1) || DL_OK="failed: $DL_OK"
    else
        DL_OK=$(curl -fsSL --max-time 120 "$TARBALL_URL" | tar -xz --strip-components=1 -C "$dest" 2>&1) || DL_OK="failed: $DL_OK"
    fi
    if [ -z "$DL_OK" ]; then
        echo "note: installed from tarball (no .git history; re-run bootstrap to refresh)"
        return 0
    fi
    echo "error: tarball fallback also failed ($DL_OK)"
    return 1
}

# get source: use cwd if it is a clone, else clone/pull (tarball fallback)
if [ -f "pyproject.toml" ] && grep -q '^name = "clique"' pyproject.toml 2>/dev/null; then
    SRC_DIR="$(pwd)"
else
    if [ -d "$INSTALL_DIR/.git" ]; then
        if ! git -C "$INSTALL_DIR" pull --ff-only; then
            echo "error: 'git pull --ff-only' failed in $INSTALL_DIR"
            echo "fix with: git -C \"$INSTALL_DIR\" fetch origin && git -C \"$INSTALL_DIR\" reset --hard origin/main"
            exit 1
        fi
    elif [ -e "$INSTALL_DIR" ]; then
        if [ -f "$INSTALL_DIR/pyproject.toml" ]; then
            # tarball install from a previous fallback: refresh it
            fetch_tarball "$INSTALL_DIR" || exit 1
        else
            echo "error: $INSTALL_DIR exists but is not a clique checkout; move it aside and retry"
            exit 1
        fi
    else
        mkdir -p "$(dirname "$INSTALL_DIR")"
        CLONE_LOG="${TMPDIR:-/tmp}/clique-clone-$$.log"
        if git clone --depth 1 "$AUTH_REPO_URL" "$INSTALL_DIR" < /dev/null 2>"$CLONE_LOG"; then
            rm -f "$CLONE_LOG"
        else
            echo "error: git clone failed:"
            cat "$CLONE_LOG" 2>/dev/null || true
            rm -f "$CLONE_LOG"
            echo
            echo "NOTE: this repo (hongnoul/tcj) is PRIVATE."
            echo "The headless machine has no GitHub credentials, so git cannot even"
            echo "ask for a username (no tty) and aborts. Fix: create a fine-grained"
            echo "PAT (github.com/settings/tokens, contents:read on hongnoul/tcj) and run:"
            echo "  export CLIQUE_GITHUB_TOKEN=github_pat_..."
            echo "  curl -fsSL https://raw.githubusercontent.com/hongnoul/tcj/main/scripts/bootstrap.sh | sh"
            echo "Or make the repo public."
            echo
            diag_clone_failure
            fetch_tarball "$INSTALL_DIR" || {
                echo "retry with: GIT_CURL_VERBOSE=1 git clone \"$REPO_URL\" \"$INSTALL_DIR\""
                exit 1
            }
        fi
    fi
    SRC_DIR="$INSTALL_DIR"
fi

# venv + install
"$PY" -m venv "$SRC_DIR/.venv"
"$SRC_DIR/.venv/bin/pip" install -q -U pip
"$SRC_DIR/.venv/bin/pip" install -q -e "$SRC_DIR"

# symlink entry points onto PATH
mkdir -p "$BIN_DIR"
for cmd in clique clique-agent clique-server; do
    ln -sf "$SRC_DIR/.venv/bin/$cmd" "$BIN_DIR/$cmd"
done

"$BIN_DIR/clique" --help >/dev/null || { echo "error: install check failed"; exit 1; }

echo "installed: $BIN_DIR/clique"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: add to PATH ->  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc && exec zsh" ;;
esac
echo
echo "join a clique:   clique join --server http://<server-ip>:7777 --runtime echo --param-b 7"
echo "or with a model: clique join --server http://<server-ip>:7777 --runtime openai-compat --model-name qwen2.5-coder:7b --param-b 7"
