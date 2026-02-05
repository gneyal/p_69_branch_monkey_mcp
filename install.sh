#!/bin/bash
set -e

# Kompany Relay Installer
# Usage: curl -fsSL https://kompany.dev/install.sh | bash

REPO="git+https://github.com/gneyal/p_69_branch_monkey_mcp.git"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
DIM='\033[38;2;107;114;128m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${BOLD}Kompany Relay${NC} — Installer"
echo -e "${DIM}Connects your machine to kompany.dev${NC}"
echo ""

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo -e "  Python     ${RED}not found${NC}"
    echo ""
    echo "  Python 3 is required. Install from https://python.org"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo -e "  Python     ${GREEN}${PY_VERSION}${NC}"

# Check / install uv
if ! command -v uvx &>/dev/null; then
    echo -e "  uv         ${DIM}installing...${NC}"
    curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

    if ! command -v uvx &>/dev/null; then
        echo -e "  uv         ${RED}failed${NC}"
        echo ""
        echo "  Install uv manually: https://docs.astral.sh/uv/"
        exit 1
    fi
fi

UV_VERSION=$(uv --version 2>/dev/null | head -1 || echo "unknown")
echo -e "  uv         ${GREEN}${UV_VERSION}${NC}"
echo ""

# Run the relay (uvx auto-installs the package)
# </dev/tty ensures stdin comes from the terminal even when piped through curl
exec uvx --from "$REPO" branch-monkey-relay "$@" </dev/tty
