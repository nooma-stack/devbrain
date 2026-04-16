#!/bin/bash
# ============================================================================
# DevBrain Clean Reinstall — for testing/development
# ============================================================================
#
# Wipes existing DevBrain install state so the curl|bash one-liner runs
# from a known-clean starting point. Useful for testing installer changes
# without a fresh machine.
#
# Default: removes DevBrain repo + shims + Postgres data.
# With --full: also removes Ollama models, Homebrew, and CLT (full reset).
#
# Usage (sync to target machine and run):
#   bash reinstall.sh             # quick reset (preserves Homebrew/Ollama/CLT)
#   bash reinstall.sh --full      # nuclear reset
#   bash reinstall.sh --yes       # skip confirmation prompt
#
# Or directly from GitHub:
#   curl -fsSL https://raw.githubusercontent.com/nooma-stack/devbrain/main/scripts/reinstall.sh | bash
# ============================================================================

set -euo pipefail

DEVBRAIN_HOME="${DEVBRAIN_HOME:-$HOME/devbrain}"
PKRELAY_HOME="${PKRELAY_HOME:-$HOME/pkrelay}"

# Anchor CWD to $HOME so that when DEVBRAIN_HOME gets deleted mid-script,
# subshells (e.g., spawned by the Homebrew uninstaller) don't spam
# "getcwd: No such file or directory" noise. The working dir going
# invalid is harmless because we only ever use absolute paths — but it's
# ugly output that can mask real errors.
cd "$HOME"

FULL_RESET=false
AUTO_YES=false

for arg in "$@"; do
    case "$arg" in
        --full) FULL_RESET=true ;;
        --yes|-y) AUTO_YES=true ;;
    esac
done

# ─── Colors ─────────────────────────────────────────────────────────────────

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
DIM='\033[0;37m'
RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET} $1"; }
info() { echo -e "  ${CYAN}→${RESET} $1"; }
skip() { echo -e "  ${DIM}• $1${RESET}"; }

ask_yn() {
    if $AUTO_YES; then return 0; fi
    local prompt="$1 [y/N]: "
    local answer
    if [[ -r /dev/tty ]]; then
        read -rp "  $prompt" answer </dev/tty
    else
        read -rp "  $prompt" answer
    fi
    [[ "$answer" =~ ^[Yy] ]]
}

# ─── Banner & confirmation ──────────────────────────────────────────────────

echo ""
echo -e "${BOLD}DevBrain Clean Reinstall${RESET}"
echo -e "${DIM}For testing the installer from a clean state.${RESET}"
echo ""

echo "This will remove:"
echo -e "  ${RED}✗${RESET} $DEVBRAIN_HOME (the cloned repo)"
echo -e "  ${RED}✗${RESET} ~/.local/bin/devbrain  ~/.local/bin/install-devbrain"
echo -e "  ${RED}✗${RESET} /opt/homebrew/bin/devbrain  /opt/homebrew/bin/install-devbrain (if present)"
echo -e "  ${RED}✗${RESET} /usr/local/bin/devbrain  /usr/local/bin/install-devbrain (if present)"
echo -e "  ${RED}✗${RESET} devbrain-db Docker container + volume (loses all DevBrain DB data)"
echo -e "  ${RED}✗${RESET} ~/Library/LaunchAgents/com.devbrain.ingest.plist (launchd service)"

if $FULL_RESET; then
    echo ""
    echo -e "${YELLOW}--full flag set — also removing:${RESET}"
    echo -e "  ${RED}✗${RESET} Ollama models (snowflake-arctic-embed2, qwen2.5:7b — ~10GB to redownload)"
    echo -e "  ${RED}✗${RESET} Homebrew itself (will be reinstalled fresh)"
    echo -e "  ${RED}✗${RESET} Xcode Command Line Tools (will be reinstalled fresh, slow)"
fi

echo ""
echo "Will NOT touch:"
echo -e "  ${GREEN}✓${RESET} ~/.claude/ (Claude Code login)"
echo -e "  ${GREEN}✓${RESET} ~/devbrain/profiles/ if present (per-dev profiles, future feature)"
echo -e "  ${GREEN}✓${RESET} Anything outside the items listed above"
echo ""

if ! ask_yn "Continue with clean reinstall?"; then
    echo ""
    info "Aborted."
    exit 0
fi

# ─── Step 1: Stop running services ─────────────────────────────────────────

echo ""
echo -e "${BOLD}[1] Stopping running services${RESET}"

if launchctl list 2>/dev/null | grep -q com.devbrain.ingest; then
    info "Unloading launchd ingest service..."
    launchctl unload ~/Library/LaunchAgents/com.devbrain.ingest.plist 2>/dev/null || true
    ok "Unloaded"
else
    skip "No launchd ingest service running"
fi

if docker ps 2>/dev/null | grep -q devbrain-db; then
    info "Stopping devbrain-db container..."
    if [[ -f "$DEVBRAIN_HOME/docker-compose.yml" ]]; then
        (cd "$DEVBRAIN_HOME" && docker compose down -v 2>/dev/null) || docker stop devbrain-db
    else
        docker stop devbrain-db 2>/dev/null || true
    fi
    docker rm devbrain-db 2>/dev/null || true
    docker volume rm devbrain_devbrain-pgdata 2>/dev/null || true
    ok "Container + volume removed"
else
    skip "No devbrain-db container running"
fi

# ─── Step 2: Remove shims ──────────────────────────────────────────────────

echo ""
echo -e "${BOLD}[2] Removing global command shims${RESET}"

for shim in /opt/homebrew/bin/devbrain /opt/homebrew/bin/install-devbrain \
            /usr/local/bin/devbrain /usr/local/bin/install-devbrain \
            "$HOME/.local/bin/devbrain" "$HOME/.local/bin/install-devbrain"; do
    if [[ -L "$shim" || -e "$shim" ]]; then
        rm -f "$shim"
        ok "Removed $shim"
    fi
done

# ─── Step 3: Remove launchd plist ──────────────────────────────────────────

echo ""
echo -e "${BOLD}[3] Removing launchd plist${RESET}"

PLIST="$HOME/Library/LaunchAgents/com.devbrain.ingest.plist"
if [[ -f "$PLIST" ]]; then
    rm -f "$PLIST"
    ok "Removed $PLIST"
else
    skip "No plist installed"
fi

# ─── Step 4: Remove cloned repo ────────────────────────────────────────────

echo ""
echo -e "${BOLD}[4] Removing cloned repo${RESET}"

if [[ -d "$DEVBRAIN_HOME" ]]; then
    info "Removing $DEVBRAIN_HOME..."
    rm -rf "$DEVBRAIN_HOME"
    ok "Removed"
else
    skip "$DEVBRAIN_HOME not present"
fi

# ─── Step 5 (--full only): Ollama models ───────────────────────────────────

if $FULL_RESET; then
    echo ""
    echo -e "${BOLD}[5] Removing Ollama models (--full)${RESET}"
    if command -v ollama &>/dev/null; then
        for model in snowflake-arctic-embed2 qwen2.5:7b; do
            if ollama list 2>/dev/null | grep -q "${model%%:*}"; then
                info "Removing $model..."
                ollama rm "$model" 2>/dev/null || true
                ok "Removed $model"
            fi
        done
    else
        skip "Ollama not installed"
    fi

    # ─── Step 6 (--full only): Docker Desktop ──────────────────────────────
    # Order matters — quit + uninstall Docker Desktop via brew BEFORE
    # removing Homebrew itself. Otherwise /Applications/Docker.app and
    # Docker's data dirs can survive the Homebrew uninstall.
    echo ""
    echo -e "${BOLD}[6] Removing Docker Desktop (--full)${RESET}"
    if [[ -d /Applications/Docker.app ]]; then
        info "Quitting Docker Desktop..."
        osascript -e 'quit app "Docker"' 2>/dev/null || true
        sleep 2
        if command -v brew &>/dev/null; then
            info "Uninstalling Docker cask..."
            brew uninstall --cask docker --force 2>/dev/null || true
        fi
        if [[ -d /Applications/Docker.app ]]; then
            info "Removing Docker.app manually..."
            sudo rm -rf /Applications/Docker.app
        fi
        # Clean up Docker's data directories
        for dir in \
            "$HOME/Library/Application Support/Docker Desktop" \
            "$HOME/Library/Containers/com.docker.docker" \
            "$HOME/Library/Group Containers/group.com.docker" \
            "$HOME/Library/Caches/com.docker.docker" \
            "$HOME/Library/Logs/Docker Desktop" \
            "$HOME/Library/Preferences/com.docker.docker.plist" \
            "$HOME/.docker"; do
            if [[ -e "$dir" ]]; then
                rm -rf "$dir" 2>/dev/null || sudo rm -rf "$dir" 2>/dev/null || true
            fi
        done
        ok "Docker Desktop + data directories removed"
    else
        skip "Docker Desktop not installed"
    fi

    # ─── Step 7 (--full only): Homebrew ────────────────────────────────────
    echo ""
    echo -e "${BOLD}[7] Removing Homebrew (--full)${RESET}"
    if command -v brew &>/dev/null; then
        warn "Uninstalling Homebrew. This may take a moment..."
        if [[ -r /dev/tty ]]; then
            NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh)" </dev/tty || true
        else
            NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/uninstall.sh)" || true
        fi
        # Force-remove any leftover /opt/homebrew directory
        if [[ -d /opt/homebrew ]]; then
            info "Cleaning up /opt/homebrew..."
            sudo rm -rf /opt/homebrew
        fi
        # Clean up shell rc lines added by the DevBrain installer
        for rc in "$HOME/.zprofile" "$HOME/.bash_profile"; do
            if [[ -f "$rc" ]]; then
                # Remove our two markers' blocks (brew shellenv + local/bin)
                sed -i.bak '/# Added by DevBrain installer/,+1d' "$rc" 2>/dev/null || true
                rm -f "${rc}.bak"
            fi
        done
        ok "Homebrew uninstalled and shell rc cleaned"
    else
        skip "Homebrew not installed"
    fi

    # ─── Step 8 (--full only): Xcode CLT ───────────────────────────────────
    echo ""
    echo -e "${BOLD}[8] Removing Xcode Command Line Tools (--full)${RESET}"
    if [[ -d /Library/Developer/CommandLineTools ]]; then
        warn "Removing CLT requires sudo..."
        sudo rm -rf /Library/Developer/CommandLineTools
        ok "CLT removed"
    else
        skip "CLT not installed"
    fi
fi

# ─── Done ──────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}━━━ Clean reinstall complete ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo "  Now run the DevBrain installer to verify the full one-liner works:"
echo ""
echo -e "    ${CYAN}curl -fsSL https://raw.githubusercontent.com/nooma-stack/devbrain/main/scripts/install.sh | bash${RESET}"
echo ""

if ask_yn "Run the installer now?"; then
    echo ""
    exec bash -c "curl -fsSL https://raw.githubusercontent.com/nooma-stack/devbrain/main/scripts/install.sh | bash"
fi
