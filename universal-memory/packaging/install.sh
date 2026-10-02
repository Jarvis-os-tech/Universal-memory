#!/bin/sh
# Install the Universal Memory CLI.
#
#   ./packaging/install.sh                 # to ~/.local/bin
#   PREFIX=/usr/local/bin ./packaging/install.sh
#
# Idempotent: safe to re-run. Creates the store and config on first use.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
src="$here/src/universal_memory.py"
prefix="${PREFIX:-$HOME/.local/bin}"

[ -f "$src" ] || { echo "cannot find $src" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required" >&2; exit 1; }

install -d "$prefix"
install -m 0755 "$src" "$prefix/universal-memory"
ln -sf "$prefix/universal-memory" "$prefix/umem"

"$prefix/universal-memory" init || true

echo "installed $prefix/universal-memory (alias: umem)"
echo
echo "next:"
echo "  universal-memory whoami"
echo "  universal-memory put shared:hello 'first memory'"
echo "  universal-memory serve    # MCP endpoint on 127.0.0.1:8787"
echo "  universal-memory web      # dashboard  on 127.0.0.1:8788"
