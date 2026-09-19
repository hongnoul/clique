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

# get source: use cwd if it is a clone, else clone/pull
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
        echo "error: $INSTALL_DIR exists but is not a git clone; move it aside and retry"
        exit 1
    else
        mkdir -p "$(dirname "$INSTALL_DIR")"
        if ! git clone --depth 1 "$REPO_URL" "$INSTALL_DIR" < /dev/null; then
            echo "error: git clone failed (network? private repo? proxy?)"
            echo "retry with: GIT_CURL_VERBOSE=1 git clone \"$REPO_URL\" \"$INSTALL_DIR\""
            exit 1
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
