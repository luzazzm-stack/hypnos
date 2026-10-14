#!/bin/sh
# Hypnos v0.9 (memory architecture) installer for macOS and Linux.
#
#   curl -fsSL https://agent-memory-architecture-chi.vercel.app/install.sh | sh
#   (a coding agent installing it for you:  ... | sh -s -- --yes)
#
# Installs uv (and through it Python 3.12) if missing, installs the memory, then runs `hypnos setup`, which connects
# every coding agent it finds (Claude Code, opencode, Codex, Gemini CLI, Cursor, Windsurf) and asks for a System One model.
# Options after `--` go to `hypnos setup`: --yes  --systemone jev|laya|none  --jev-key KEY  --agents claude,opencode
# Uninstall:  hypnos setup --uninstall && uv tool uninstall hypnos
set -eu

REPO="${HYPNOS_REPO:-luzazzm-stack/hypnos}"
REF="${HYPNOS_REF:-main}"
SOURCE="${HYPNOS_SOURCE:-https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF}"

say() { printf '%s\n' "$*"; }
fail() { say "Install failed: $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

fetch() {  # fetch URL FILE, with curl or wget
    if have curl; then curl -fsSL --retry 3 "$1" -o "$2"
    elif have wget; then wget -q "$1" -O "$2"
    else fail "neither curl nor wget is installed"; fi
}

say "Hypnos v0.9: installing..."
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# 1. uv (installs Python for us; no system Python needed)
if ! have uv; then
    say "- installing uv (Python package manager)"
    tmp_uv=$(mktemp)
    fetch https://astral.sh/uv/install.sh "$tmp_uv"
    sh "$tmp_uv" --quiet || sh "$tmp_uv"
    rm -f "$tmp_uv"
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    have uv || fail "uv did not install; see https://docs.astral.sh/uv/getting-started/installation/"
fi

# 2. the memory itself (from the public repo, or a local folder in HYPNOS_SOURCE for testing)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if [ -d "$SOURCE" ]; then
    src="$SOURCE"
else
    say "- downloading"
    fetch "$SOURCE" "$work/src.tar.gz"
    mkdir "$work/src"
    tar -xzf "$work/src.tar.gz" -C "$work/src"
    src=$(find "$work/src" -maxdepth 2 -type d -name hypnos | head -n 1)
    src=${src%/hypnos}
fi
[ -f "$src/hypnos/pyproject.toml" ] || fail "package not found in $SOURCE"

say "- installing the memory (Python 3.12 is downloaded by uv if needed)"
if ! uv tool install --quiet --force --python 3.12 "$src/hypnos[mcp,vectors]" 2>/dev/null; then
    say "  (meaning search is not available on this machine; installing without it)"
    uv tool install --quiet --force --python 3.12 "$src/hypnos[mcp]" || fail "uv tool install failed"
fi
uv tool update-shell >/dev/null 2>&1 || true
export PATH="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin"):$PATH"
have hypnos || fail "hypnos is not on PATH; open a new terminal and run: hypnos setup"

# 3. connect the agents. Questions are asked on the terminal even though this script is piped into sh.
if [ -t 1 ] && [ -r /dev/tty ] && ! printf '%s ' "$@" | grep -q -- '--yes'; then
    hypnos setup "$@" </dev/tty
else
    hypnos setup --yes "$@"
fi
