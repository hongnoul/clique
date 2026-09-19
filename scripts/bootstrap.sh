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

# get source: use cwd if it is a clone, else clone/pull
if [ -f "pyproject.toml" ] && grep -q '^name = "clique"' pyproject.toml 2>/dev/null; then
    SRC_DIR="$(pwd)"
else
    if [ -d "$INSTALL_DIR/.git" ]; then
        git -C "$INSTALL_DIR" pull --ff-only
    else
        mkdir -p "$(dirname "$INSTALL_DIR")"
        git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
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
