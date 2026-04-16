#!/bin/bash
# ============================================================================
# DevBrain Installer
# ============================================================================
#
# Installs all DevBrain dependencies, builds components, and verifies the
# install via `devbrain doctor`. Idempotent — safe to re-run after updates
# or partial failures. Each step checks whether work is needed before acting.
#
# Usage:
#   ./scripts/install.sh              # Interactive (prompts for optional steps)
#   ./scripts/install.sh --yes        # Accept all defaults (non-interactive)
#   ./scripts/install.sh --no-pkrelay # Skip PKRelay prompt
#
# Requirements: macOS (Apple Silicon or Intel) or Linux (Debian/Ubuntu).
# Other Linux distros may need manual dep installation — see INSTALL.md.
# ============================================================================

set -euo pipefail

# ─── Configuration ──────────────────────────────────────────────────────────

# When run from inside a clone, infer DEVBRAIN_HOME from the script location.
# When run via curl|bash (no $BASH_SOURCE path), default to $HOME/devbrain
# and clone the repo first. Resolve symlinks so the shim
# (/opt/homebrew/bin/install-devbrain) correctly locates the real repo.
if [[ -n "${BASH_SOURCE[0]:-}" ]] && [[ -f "${BASH_SOURCE[0]}" ]]; then
    _SRC_TARGET="${BASH_SOURCE[0]}"
    while [[ -L "$_SRC_TARGET" ]]; do
        _SRC_DIR="$(cd "$(dirname "$_SRC_TARGET")" && pwd)"
        _SRC_TARGET="$(readlink "$_SRC_TARGET")"
        [[ "$_SRC_TARGET" != /* ]] && _SRC_TARGET="$_SRC_DIR/$_SRC_TARGET"
    done
    SCRIPT_DIR="$(cd "$(dirname "$_SRC_TARGET")" && pwd)"
    DEFAULT_HOME="$(cd "$SCRIPT_DIR/.." && pwd)"
    unset _SRC_TARGET _SRC_DIR
else
    DEFAULT_HOME="$HOME/devbrain"
fi
DEVBRAIN_HOME="${DEVBRAIN_HOME:-$DEFAULT_HOME}"
DEVBRAIN_REPO="${DEVBRAIN_REPO:-https://github.com/nooma-stack/devbrain.git}"
DEVBRAIN_BRANCH="${DEVBRAIN_BRANCH:-main}"

PKRELAY_HOME="${PKRELAY_HOME:-$HOME/pkrelay}"
PKRELAY_REPO="https://github.com/nooma-stack/pkrelay.git"
OLLAMA_MODELS=("snowflake-arctic-embed2" "qwen2.5:7b")
AUTO_YES=false
SKIP_PKRELAY=false
SKIP_SETUP=false
SKIP_SHIMS=false

for arg in "$@"; do
    case "$arg" in
        --yes|-y) AUTO_YES=true ;;
        --no-pkrelay) SKIP_PKRELAY=true ;;
        --no-setup) SKIP_SETUP=true ;;
        --no-shims) SKIP_SHIMS=true ;;
    esac
done

# ─── Formatting ─────────────────────────────────────────────────────────────

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
DIM='\033[0;37m'
RESET='\033[0m'

step_num=0

step() {
    step_num=$((step_num + 1))
    echo ""
    echo -e "${BOLD}[$step_num] $1${RESET}"
}

ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
skip() { echo -e "  ${DIM}• $1 (already installed)${RESET}"; }
warn() { echo -e "  ${YELLOW}⚠${RESET} $1"; }
fail() { echo -e "  ${RED}✗${RESET} $1"; }
info() { echo -e "  ${CYAN}→${RESET} $1"; }
desc() { echo -e "  ${DIM}$1${RESET}"; }

ask() {
    if $AUTO_YES; then return 0; fi
    local prompt="$1 [Y/n]: "
    local answer
    # Read from /dev/tty so prompts work even when stdin is a pipe (curl|bash).
    if [[ -r /dev/tty ]]; then
        read -rp "  $prompt" answer </dev/tty
    else
        read -rp "  $prompt" answer
    fi
    [[ -z "$answer" || "$answer" =~ ^[Yy] ]]
}

ask_no() {
    if $AUTO_YES; then return 1; fi
    local prompt="$1 [y/N]: "
    local answer
    if [[ -r /dev/tty ]]; then
        read -rp "  $prompt" answer </dev/tty
    else
        read -rp "  $prompt" answer
    fi
    [[ "$answer" =~ ^[Yy] ]]
}

# ─── Bootstrap (clone repo if running remotely) ─────────────────────────────

is_in_devbrain_repo() {
    [[ -f "$DEVBRAIN_HOME/scripts/install.sh" ]] && \
    [[ -f "$DEVBRAIN_HOME/bin/devbrain" ]] && \
    [[ -d "$DEVBRAIN_HOME/factory" ]]
}

ensure_macos_clt() {
    # Xcode Command Line Tools include git. On a fresh macOS they're absent.
    # We trigger the installer and auto-poll for completion so users don't
    # have to re-run this script after CLT installs.
    if command -v git &>/dev/null; then
        return 0
    fi

    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo "  MACOS COMMAND LINE TOOLS REQUIRED"
    echo "════════════════════════════════════════════════════════════"
    echo ""
    info "git is needed to clone DevBrain and isn't installed yet."
    info "Triggering the Xcode Command Line Tools installer now."
    echo ""
    warn "IMPORTANT — the macOS popup may appear BEHIND this terminal:"
    warn "  • Press Cmd+Tab or check Mission Control if you don't see it"
    warn "  • Click 'Install' (NOT 'Get Xcode' — that's 12GB of overkill)"
    warn "  • Accept the license, then wait ~5-10 minutes for download"
    echo ""
    info "This script will automatically resume once git is available."
    echo ""

    # Trigger the installer dialog (returns immediately; install happens async)
    xcode-select --install 2>/dev/null || true

    # Alert the user: sound + desktop notification in case they're AFK
    afplay /System/Library/Sounds/Glass.aiff 2>/dev/null &
    osascript -e 'display notification "Accept the Command Line Tools install dialog" with title "DevBrain Installer" sound name "Glass"' 2>/dev/null || true

    # Poll every 5s until git appears, with progress indication
    local waited=0
    local max_wait=1800  # 30 minutes
    echo -n "  Waiting for Command Line Tools install to complete"
    while ! command -v git &>/dev/null; do
        sleep 5
        waited=$((waited + 5))
        if (( waited % 30 == 0 )); then
            local mins=$((waited / 60))
            local secs=$((waited % 60))
            echo ""
            echo -n "  Still waiting... ($mins min $secs sec). If no dialog appeared, run 'xcode-select --install' in another terminal"
        else
            echo -n "."
        fi
        if (( waited >= max_wait )); then
            echo ""
            fail "Gave up waiting after 30 minutes."
            fail "Install manually: xcode-select --install"
            fail "Then re-run this installer."
            exit 1
        fi
    done
    echo ""
    ok "Command Line Tools installed (after ${waited}s)"
    echo ""
}

bootstrap_clone() {
    echo ""
    echo -e "${BOLD}DevBrain Bootstrap${RESET}"
    echo -e "${DIM}Running via curl|bash — cloning the repo first.${RESET}"
    echo ""

    # On macOS, ensure git is available (may block up to 30 min for CLT install).
    # On Linux, git must be pre-installed via apt/yum/etc.
    if ! command -v git &>/dev/null; then
        if [[ "$OSTYPE" == darwin* ]]; then
            ensure_macos_clt
        else
            fail "git is required. Install it (e.g. 'sudo apt-get install git') and re-run."
            exit 1
        fi
    fi

    if [[ -d "$DEVBRAIN_HOME" ]]; then
        if [[ -d "$DEVBRAIN_HOME/.git" ]]; then
            info "DevBrain already cloned at $DEVBRAIN_HOME — pulling latest..."
            if ! (cd "$DEVBRAIN_HOME" && git fetch --quiet 2>/dev/null && git checkout --quiet "$DEVBRAIN_BRANCH" 2>/dev/null && git pull --ff-only --quiet 2>/dev/null); then
                warn "Pull failed — repo may have local changes. Continuing with existing checkout."
            else
                ok "Updated to latest $DEVBRAIN_BRANCH"
            fi
        else
            fail "$DEVBRAIN_HOME exists but is not a git repo."
            fail "Set DEVBRAIN_HOME=/different/path or remove the existing directory."
            exit 1
        fi
    else
        info "Cloning DevBrain ($DEVBRAIN_BRANCH) to $DEVBRAIN_HOME..."
        git clone --quiet --branch "$DEVBRAIN_BRANCH" "$DEVBRAIN_REPO" "$DEVBRAIN_HOME"
        ok "Cloned"
    fi

    echo ""
    info "Re-executing installer from cloned repo..."
    echo ""

    # Redirect stdin to /dev/tty so interactive prompts (sudo passwords,
    # Homebrew license acceptance, our own ask() prompts) work even though
    # we were originally invoked via curl|bash (which has a pipe as stdin).
    if [[ -r /dev/tty ]]; then
        exec bash "$DEVBRAIN_HOME/scripts/install.sh" "$@" </dev/tty
    else
        # No controlling TTY available (e.g., CI). Require --yes for unattended.
        warn "No /dev/tty available — running non-interactively. Set --yes if needed."
        exec bash "$DEVBRAIN_HOME/scripts/install.sh" "$@"
    fi
}

# ─── OS Detection ──────────────────────────────────────────────────────────

detect_os() {
    case "$OSTYPE" in
        darwin*)
            OS="macos"
            ARCH="$(uname -m)"
            ;;
        linux*)
            OS="linux"
            ARCH="$(uname -m)"
            ;;
        msys*|cygwin*|win32*)
            echo ""
            echo "────────────────────────────────────────────────────────────"
            echo "  Windows detected"
            echo "────────────────────────────────────────────────────────────"
            echo ""
            echo "DevBrain's installer requires macOS or Linux. On Windows,"
            echo "the recommended path is to run this inside WSL2 (Windows"
            echo "Subsystem for Linux):"
            echo ""
            echo "  1. Enable WSL2:"
            echo "       wsl --install -d Ubuntu"
            echo "     (then restart Windows, open Ubuntu from Start menu)"
            echo ""
            echo "  2. Inside the Ubuntu terminal, run the installer:"
            echo "       curl -fsSL https://raw.githubusercontent.com/nooma-stack/devbrain/main/scripts/install.sh | bash"
            echo ""
            echo "Docker Desktop for Windows uses WSL2 as its backend anyway,"
            echo "so this integrates cleanly with your existing Docker setup."
            echo ""
            exit 1
            ;;
        *)
            echo "Unsupported OS: $OSTYPE"
            echo "DevBrain supports macOS and Linux. See INSTALL.md for details."
            exit 1
            ;;
    esac
}

ensure_rosetta_on_apple_silicon() {
    # Apple Silicon Macs benefit from Rosetta 2 for running x86_64 Docker
    # containers and the occasional x86_64 CLI tool. macOS Tahoe doesn't
    # ship Rosetta by default — Docker Desktop prompts for it on first
    # launch. Pre-installing here gets that out of the way before Docker
    # needs it, and saves the first-run Docker prompt.
    if [[ "$OS" != "macos" ]] || [[ "$ARCH" != "arm64" ]]; then
        return 0
    fi

    # Check if Rosetta is already installed/working
    if /usr/bin/pgrep -q oahd 2>/dev/null; then
        return 0  # oahd is the Rosetta daemon — its presence means installed
    fi
    # Fallback check: can we actually run an x86_64 binary?
    if arch -x86_64 /usr/bin/true 2>/dev/null; then
        return 0
    fi

    step "Rosetta 2 (Apple Silicon x86_64 emulation)"
    desc "Rosetta lets Apple Silicon Macs run x86_64 binaries under"
    desc "emulation. Docker Desktop uses it for Intel-based container"
    desc "images (which is most public Docker images on Docker Hub)."
    desc "Pre-installing here avoids Docker prompting you for it later."

    info "Installing Rosetta 2 (silent, ~1 minute)..."
    if /usr/sbin/softwareupdate --install-rosetta --agree-to-license 2>&1 | tail -3; then
        ok "Rosetta 2 installed"
    else
        warn "Rosetta install returned non-zero. Docker may prompt later."
    fi
}

# ─── Dependency Installers ─────────────────────────────────────────────────

_detect_existing_python_tooling() {
    # Detect Python setups that could be affected by adding brew shellenv
    # to the shell rc (which prepends /opt/homebrew/bin to PATH and would
    # change which python3 the user gets in new shells).
    #
    # Returns a newline-separated list (possibly empty) on stdout.
    # Uses a plain string instead of a bash array for macOS bash 3.2
    # compatibility — `${array[@]}` on an empty array triggers an
    # "unbound variable" error under `set -u`.
    local found=""
    local NL=$'\n'

    if [[ -d "$HOME/.pyenv" ]] || command -v pyenv &>/dev/null; then
        found+="pyenv${NL}"
    fi
    if command -v conda &>/dev/null; then
        found+="conda (active)${NL}"
    elif [[ -d "$HOME/miniconda3" || -d "$HOME/anaconda3" || -d "$HOME/miniforge3" ]]; then
        found+="conda/miniconda (installed but inactive)${NL}"
    fi
    if command -v asdf &>/dev/null; then
        found+="asdf${NL}"
    fi
    if [[ -d /Library/Frameworks/Python.framework ]]; then
        found+="python.org installer${NL}"
    fi

    # Check shell rc for existing Python-related PATH manipulation
    local rc=""
    case "${SHELL:-}" in
        */zsh)  rc="$HOME/.zshrc" ;;
        */bash) rc="$HOME/.bash_profile" ;;
    esac
    if [[ -n "$rc" && -f "$rc" ]]; then
        if grep -qE 'pyenv init|conda init|asdf\.sh|PATH=.*python' "$rc" 2>/dev/null; then
            found+="custom Python config in $(basename "$rc")${NL}"
        fi
    fi

    # Trim trailing newline, emit only if non-empty
    printf '%s' "${found%"$NL"}"
}

_persist_brew_shellenv() {
    # Homebrew's official post-install instruction: persist `brew shellenv`
    # to the user's shell rc so brew + brew-installed commands are in PATH
    # for future shell sessions. This puts /opt/homebrew/bin at the FRONT
    # of PATH, which can shadow existing pyenv/conda/asdf setups. We detect
    # those and prompt before modifying the user's shell rc.
    local rc
    case "${SHELL:-}" in
        */zsh)  rc="$HOME/.zprofile" ;;
        */bash) rc="$HOME/.bash_profile" ;;
        *)      return 0 ;;
    esac
    if [[ -f "$rc" ]] && grep -q 'brew shellenv' "$rc" 2>/dev/null; then
        skip "brew shellenv already in $rc"
        return 0
    fi

    local existing
    existing=$(_detect_existing_python_tooling)

    if [[ -n "$existing" ]]; then
        echo ""
        warn "Existing Python tooling detected on this machine:"
        while IFS= read -r tool; do
            [[ -n "$tool" ]] && echo -e "    ${YELLOW}•${RESET} $tool"
        done <<< "$existing"
        echo ""
        desc "Adding 'brew shellenv' to $rc puts /opt/homebrew/bin at the FRONT"
        desc "of your PATH for new terminal sessions. Effects:"
        desc "  • python3, pip3, etc. will resolve to Homebrew's versions in fresh shells"
        desc "  • pyenv/conda/asdf init scripts in .zshrc still run AFTER .zprofile,"
        desc "    so they will override this and keep their own Python in PATH"
        desc "  • Could surprise you if you have a manual python.org or custom setup"
        echo ""
        desc "DevBrain itself does NOT depend on this — our venvs always use"
        desc "/opt/homebrew/bin/python3 via absolute path, so they work either way."
        desc "If you skip this, brew-installed tools (gh, docker, ollama, psql)"
        desc "won't be in PATH for new shells until you manually add brew shellenv."
        echo ""
        if ! ask "Add brew shellenv to $rc?"; then
            info "Skipped. To enable later, run:"
            echo -e "    ${CYAN}echo 'eval \"\$(/opt/homebrew/bin/brew shellenv)\"' >> $rc${RESET}"
            return 0
        fi
    fi

    {
        echo ""
        echo "# Added by DevBrain installer — Homebrew shell environment"
        echo 'eval "$(/opt/homebrew/bin/brew shellenv)"'
    } >> "$rc"
    ok "Added Homebrew to $rc (effective on next shell start)"
}

_ensure_local_bin_in_path() {
    # Ensure ~/.local/bin is in PATH for new shells. This is the XDG
    # standard for user-installed binaries (used by Claude Code, pipx,
    # cargo, and many others). Adding it is non-controversial and
    # doesn't shadow anything.
    local rc
    case "${SHELL:-}" in
        */zsh)  rc="$HOME/.zprofile" ;;
        */bash) rc="$HOME/.bash_profile" ;;
        *)      return 0 ;;
    esac
    mkdir -p "$HOME/.local/bin"
    if [[ -f "$rc" ]] && grep -q '\.local/bin' "$rc" 2>/dev/null; then
        return 0  # already configured
    fi
    {
        echo ""
        echo "# Added by DevBrain installer — XDG user bin directory"
        echo 'export PATH="$HOME/.local/bin:$PATH"'
    } >> "$rc"
}

install_homebrew() {
    step "Package manager"
    desc "Homebrew is the standard macOS package manager. All other"
    desc "dependencies are installed through it."
    if command -v brew &>/dev/null; then
        skip "Homebrew $(brew --version | head -1 | awk '{print $2}')"
        # Even when brew is already installed, ensure shellenv is persisted.
        _persist_brew_shellenv
    else
        info "Installing Homebrew (will prompt for your macOS password)..."
        # Homebrew's installer refuses to run if stdin isn't a TTY — redirect
        # from /dev/tty so sudo's password prompt works even when this script
        # was invoked via curl|bash.
        if [[ -r /dev/tty ]]; then
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" </dev/tty
        else
            NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        fi
        eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)"
        _persist_brew_shellenv
        ok "Homebrew installed"
    fi
}

install_linux_essentials() {
    step "System packages"
    desc "Essential build tools and libraries needed by Python, Node,"
    desc "and PostgreSQL client tools."
    if command -v git &>/dev/null && command -v curl &>/dev/null; then
        skip "git, curl, build-essential"
    else
        info "Installing system packages..."
        sudo apt-get update -qq
        sudo apt-get install -y -qq git curl build-essential libssl-dev libffi-dev python3-dev
        ok "System packages installed"
    fi
}

_launch_docker_in_background() {
    # Kick Docker Desktop to launch in the background so the daemon is
    # warm by the time start_postgres() needs it. Safe to call multiple
    # times — `open -a Docker` is idempotent (no-op if already running).
    if [[ -d /Applications/Docker.app ]]; then
        # First-launch detection: absence of Docker's settings file means
        # this is Docker's first-ever launch, which will require the user
        # to accept a license dialog before the daemon starts.
        local is_first_launch=false
        if [[ ! -f "$HOME/Library/Application Support/Docker Desktop/settings-store.json" ]] \
           && [[ ! -f "$HOME/Library/Application Support/Docker Desktop/settings.json" ]]; then
            is_first_launch=true
        fi

        info "Launching Docker Desktop in background..."
        open -a Docker 2>/dev/null || true

        if $is_first_launch; then
            echo ""
            warn "FIRST LAUNCH: Docker Desktop's license agreement dialog"
            warn "will appear. Accept it when convenient — you can keep working"
            warn "while it shows; the installer will wait for the daemon later"
            warn "in the Postgres step (in ~10-20 min once Ollama models finish)."
            echo ""
        else
            info "Daemon will be warm by the time we need it."
        fi
    fi
}

install_docker() {
    step "Docker"
    desc "Runs PostgreSQL + pgvector in a container. DevBrain stores all"
    desc "memory, sessions, and factory state in this database."

    if command -v docker &>/dev/null; then
        skip "Docker $(docker --version 2>/dev/null | awk '{print $3}' | tr -d ',')"
        # Even if Docker is already installed, kick the daemon if it's
        # not running so it's ready by the Postgres step.
        if [[ "$OS" == "macos" ]] && ! docker info &>/dev/null 2>&1; then
            _launch_docker_in_background
        fi
    elif [[ "$OS" == "macos" ]]; then
        info "Installing Docker Desktop via Homebrew..."
        desc "(Alternatives: Colima or OrbStack — see INSTALL.md)"
        brew install --cask docker
        ok "Docker Desktop installed"
        _launch_docker_in_background
    else
        info "Installing Docker Engine..."
        curl -fsSL https://get.docker.com | sh
        sudo usermod -aG docker "$USER"
        ok "Docker installed (you may need to log out/in for group changes)"
        # Start dockerd on Linux systemd systems
        if command -v systemctl &>/dev/null; then
            sudo systemctl start docker 2>/dev/null || true
        fi
    fi
}

install_ollama() {
    step "Ollama"
    desc "Local LLM inference server. DevBrain uses it for embedding your"
    desc "sessions into vectors (semantic search) and for summarizing"
    desc "transcripts. Runs natively for GPU acceleration — not in Docker."

    if command -v ollama &>/dev/null; then
        skip "Ollama $(ollama --version 2>/dev/null | awk '{print $NF}')"
    elif [[ "$OS" == "macos" ]]; then
        info "Installing Ollama via Homebrew..."
        brew install ollama
        ok "Ollama installed"
    else
        info "Installing Ollama..."
        curl -fsSL https://ollama.com/install.sh | sh
        ok "Ollama installed"
    fi

    if ! pgrep -x ollama &>/dev/null && ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
        info "Starting Ollama service..."
        if [[ "$OS" == "macos" ]]; then
            brew services start ollama 2>/dev/null || ollama serve &>/dev/null &
        else
            sudo systemctl start ollama 2>/dev/null || ollama serve &>/dev/null &
        fi
        sleep 2
    fi
}

install_node() {
    step "Node.js"
    desc "Required to build the MCP server, which is the bridge between"
    desc "your AI agents (Claude Code, Codex, Gemini) and DevBrain's database."

    if command -v node &>/dev/null; then
        local ver
        ver="$(node --version)"
        local major
        major="${ver#v}"
        major="${major%%.*}"
        if [[ "$major" -ge 20 ]]; then
            skip "Node.js $ver"
            return
        else
            warn "Node.js $ver found but v20+ required"
        fi
    fi

    if [[ "$OS" == "macos" ]]; then
        info "Installing Node.js 22 via Homebrew..."
        brew install node@22
        ok "Node.js installed"
    else
        info "Installing Node.js 22 via nodesource..."
        curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
        sudo apt-get install -y -qq nodejs
        ok "Node.js installed"
    fi
}

# Python version we target on macOS. Change this to pin a different
# version (e.g., PY_MAJOR_MINOR="3.13" for stability).
PY_MAJOR_MINOR="3.14"
PY_BREW_FORMULA="python@${PY_MAJOR_MINOR}"
PY_BIN="/opt/homebrew/bin/python${PY_MAJOR_MINOR}"
PY_IMPORT_CHECK="import xml.parsers.expat, ssl, ensurepip"

_fix_tahoe_libexpat_mismatch() {
    # macOS Tahoe-specific Homebrew bottle bug: Python's pyexpat.so is
    # stamped to load /usr/lib/libexpat.1.dylib (system, too old) instead
    # of /opt/homebrew/opt/expat/lib/libexpat.1.dylib (Homebrew's newer
    # expat). Symbols added in expat 2.7.0+ (like _XML_SetAllocTree…)
    # aren't in the system version, so pyexpat fails to load at import.
    #
    # We surgically repoint the library reference using install_name_tool.
    # Much faster than rebuilding from source (~30 sec vs ~30 min).
    info "Attempting auto-fix: repoint pyexpat.so to Homebrew's libexpat..."

    # Ensure Homebrew's expat is available
    if [[ ! -f /opt/homebrew/opt/expat/lib/libexpat.1.dylib ]]; then
        info "Installing Homebrew expat..."
        brew install expat
    fi

    # Find the broken pyexpat.so (path includes version-specific directory)
    local pyexpat_so
    pyexpat_so=$(ls /opt/homebrew/Cellar/${PY_BREW_FORMULA}/*/Frameworks/Python.framework/Versions/${PY_MAJOR_MINOR}/lib/python${PY_MAJOR_MINOR}/lib-dynload/pyexpat.cpython-*-darwin.so 2>/dev/null | head -1)

    if [[ -z "$pyexpat_so" ]]; then
        fail "Could not locate pyexpat.so under /opt/homebrew/Cellar/${PY_BREW_FORMULA}"
        return 1
    fi

    info "Patching: $pyexpat_so"
    install_name_tool -change \
        /usr/lib/libexpat.1.dylib \
        /opt/homebrew/opt/expat/lib/libexpat.1.dylib \
        "$pyexpat_so"

    # Re-sign the binary since install_name_tool invalidates the signature
    codesign --force --sign - "$pyexpat_so" 2>/dev/null || true

    # Re-test
    if "$PY_BIN" -c "$PY_IMPORT_CHECK" 2>/dev/null; then
        ok "Fix applied successfully"
        return 0
    else
        warn "install_name_tool fix did not resolve the issue"
        return 1
    fi
}

_rebuild_python_from_source() {
    # Fallback when the bottle can't be salvaged. Compiling from source
    # lets Python link against Homebrew's own expat/ssl/etc. directly,
    # avoiding any system-library ABI mismatches. Takes ~20-30 minutes
    # on Apple Silicon.
    warn "Rebuilding Python from source — this takes 20-30 minutes."
    warn "The progress output will be verbose; that's normal."
    brew uninstall --ignore-dependencies "$PY_BREW_FORMULA" 2>/dev/null || true
    brew install --build-from-source "$PY_BREW_FORMULA"

    if "$PY_BIN" -c "$PY_IMPORT_CHECK" 2>/dev/null; then
        ok "Source build succeeded"
        return 0
    else
        return 1
    fi
}

install_python() {
    step "Python"
    desc "Runs the ingest pipeline (session capture + embedding), the"
    desc "factory orchestrator (automated code generation pipeline), and"
    desc "the DevBrain CLI."

    if [[ "$OS" == "macos" ]]; then
        # Fast path: Python is installed and imports cleanly
        if [[ -x "$PY_BIN" ]] && "$PY_BIN" -c "$PY_IMPORT_CHECK" 2>/dev/null; then
            skip "Python $("$PY_BIN" --version | awk '{print $2}') (Homebrew, verified working)"
            return 0
        fi

        # Install or reinstall if needed
        if [[ ! -x "$PY_BIN" ]]; then
            info "Installing $PY_BREW_FORMULA via Homebrew..."
            brew install "$PY_BREW_FORMULA"
        fi

        # Run the real import check and show the error if it fails
        if "$PY_BIN" -c "$PY_IMPORT_CHECK" 2>/dev/null; then
            ok "Python $("$PY_BIN" --version | awk '{print $2}') installed and verified"
            return 0
        fi

        warn "Python import check failed. Diagnosing..."
        echo ""
        local err
        err=$("$PY_BIN" -c "$PY_IMPORT_CHECK" 2>&1 || true)
        echo "$err" | head -10
        echo ""

        # Pattern match known issues and apply targeted fixes
        if echo "$err" | grep -qE 'libexpat\.1\.dylib|_XML_SetAlloc|symbol not found.*XML_'; then
            info "Detected known Homebrew bottle issue on macOS Tahoe"
            info "(pyexpat linked to wrong libexpat version)."
            if _fix_tahoe_libexpat_mismatch; then
                ok "Python $("$PY_BIN" --version | awk '{print $2}') working after patch"
                return 0
            fi
            warn "Surgical fix insufficient. Falling back to source build..."
        else
            warn "Unknown failure pattern — falling back to source build..."
        fi

        if _rebuild_python_from_source; then
            ok "Python $("$PY_BIN" --version | awk '{print $2}') built from source"
            return 0
        fi

        fail "Unable to get a working Python after install, patch, and source rebuild."
        fail "Please file an issue with the output above. To debug manually:"
        fail "  $PY_BIN -c '$PY_IMPORT_CHECK'"
        exit 1
    else
        # Linux: system Python is fine. Need 3.11+.
        if command -v python3 &>/dev/null; then
            local ver major minor
            ver="$(python3 --version | awk '{print $2}')"
            major="${ver%%.*}"; minor="${ver#*.}"; minor="${minor%%.*}"
            if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
                skip "Python $ver"
                return
            else
                warn "Python $ver found but 3.11+ required"
            fi
        fi
        info "Installing Python 3..."
        sudo apt-get install -y -qq python3 python3-venv python3-pip
        ok "Python installed"
    fi
}

# Resolve the Python interpreter to use for venv creation.
_python_for_venv() {
    if [[ "$OS" == "macos" && -x "$PY_BIN" ]]; then
        echo "$PY_BIN"
    else
        echo "python3"
    fi
}

install_gh() {
    step "GitHub CLI"
    desc "Used for repository management, authentication, and creating"
    desc "pull requests from the dev factory pipeline."

    if command -v gh &>/dev/null; then
        skip "GitHub CLI $(gh --version 2>/dev/null | head -1 | awk '{print $NF}')"
    elif [[ "$OS" == "macos" ]]; then
        info "Installing GitHub CLI via Homebrew..."
        brew install gh
        ok "GitHub CLI installed"
    else
        info "Installing GitHub CLI..."
        (type -p wget >/dev/null || sudo apt-get install -y -qq wget) \
            && sudo mkdir -p -m 755 /etc/apt/keyrings \
            && wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null \
            && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null \
            && sudo apt-get update -qq && sudo apt-get install -y -qq gh
        ok "GitHub CLI installed"
    fi
}

install_psql() {
    step "PostgreSQL client"
    desc "The psql command-line client is used by DevBrain's session-start"
    desc "hook and for database inspection. The server runs in Docker."

    if command -v psql &>/dev/null; then
        skip "psql $(psql --version 2>/dev/null | awk '{print $NF}')"
    elif [[ "$OS" == "macos" ]]; then
        info "Installing libpq (psql client) via Homebrew..."
        # libpq is keg-only by default (Homebrew prints a "caveat" warning
        # because it could conflict with a full PostgreSQL install). For
        # DevBrain we only need the client and Postgres runs in Docker, so
        # there's no real conflict — we force-link to put psql in PATH.
        # HOMEBREW_NO_ENV_HINTS suppresses the noise.
        HOMEBREW_NO_ENV_HINTS=1 brew install --quiet libpq 2>&1 | grep -vE '^(==>|Warning:|If you|For compilers|export |  echo|libpq is keg-only|because it conflicts|Hide these hints)' || true
        brew link --force --quiet libpq 2>/dev/null || true
        if command -v psql &>/dev/null; then
            ok "psql installed and linked into PATH"
        else
            warn "psql linked but not in PATH yet — will be in next shell session"
        fi
    else
        info "Installing postgresql-client..."
        sudo apt-get install -y -qq postgresql-client
        ok "psql installed"
    fi
}

# ─── DevBrain Setup ────────────────────────────────────────────────────────

setup_config() {
    step "Configuration files"
    desc "DevBrain uses two config files: .env for secrets/environment and"
    desc "config/devbrain.yaml for project mappings and preferences."

    if [[ ! -f "$DEVBRAIN_HOME/.env" ]]; then
        cp "$DEVBRAIN_HOME/.env.example" "$DEVBRAIN_HOME/.env"
        ok "Created .env from template"
        info "Edit .env later to customize (or run 'devbrain setup')"
    else
        skip ".env already exists"
    fi

    if [[ ! -f "$DEVBRAIN_HOME/config/devbrain.yaml" ]]; then
        cp "$DEVBRAIN_HOME/config/devbrain.yaml.example" "$DEVBRAIN_HOME/config/devbrain.yaml"
        ok "Created config/devbrain.yaml from template"
    else
        skip "config/devbrain.yaml already exists"
    fi
}

setup_venvs() {
    step "Python virtual environments"
    desc "DevBrain uses two venvs: a root venv for the CLI and factory,"
    desc "and an ingest venv for the session capture pipeline."

    local PY
    PY="$(_python_for_venv)"
    info "Using Python: $PY ($($PY --version 2>&1 | awk '{print $2}'))"

    # Root venv. Use --without-pip to avoid the macOS Tahoe ensurepip bug,
    # then bootstrap pip explicitly via get-pip.py for reliability.
    if [[ ! -f "$DEVBRAIN_HOME/.venv/bin/python" ]]; then
        info "Creating root venv..."
        if ! $PY -m venv "$DEVBRAIN_HOME/.venv" 2>/dev/null; then
            warn "Default venv creation failed — retrying with --without-pip + manual bootstrap"
            rm -rf "$DEVBRAIN_HOME/.venv"
            $PY -m venv --without-pip "$DEVBRAIN_HOME/.venv"
            curl -sSL https://bootstrap.pypa.io/get-pip.py | "$DEVBRAIN_HOME/.venv/bin/python"
        fi
        ok "Root venv created"
    else
        skip "Root .venv"
    fi
    info "Installing root Python dependencies..."
    "$DEVBRAIN_HOME/.venv/bin/pip" install -q --upgrade pip
    "$DEVBRAIN_HOME/.venv/bin/pip" install -q -r "$DEVBRAIN_HOME/requirements.txt"
    ok "Root deps installed (click, psycopg2, pyyaml, textual, pytest)"

    # Ingest venv (same fallback strategy)
    if [[ ! -f "$DEVBRAIN_HOME/ingest/.venv/bin/python" ]]; then
        info "Creating ingest venv..."
        if ! $PY -m venv "$DEVBRAIN_HOME/ingest/.venv" 2>/dev/null; then
            warn "Default venv creation failed — retrying with --without-pip + manual bootstrap"
            rm -rf "$DEVBRAIN_HOME/ingest/.venv"
            $PY -m venv --without-pip "$DEVBRAIN_HOME/ingest/.venv"
            curl -sSL https://bootstrap.pypa.io/get-pip.py | "$DEVBRAIN_HOME/ingest/.venv/bin/python"
        fi
        ok "Ingest venv created"
    else
        skip "Ingest .venv"
    fi
    info "Installing ingest Python dependencies..."
    "$DEVBRAIN_HOME/ingest/.venv/bin/pip" install -q --upgrade pip
    "$DEVBRAIN_HOME/ingest/.venv/bin/pip" install -q -r "$DEVBRAIN_HOME/ingest/requirements.txt"
    ok "Ingest deps installed (psycopg2, watchdog, pyyaml)"
}

_wait_for_docker_daemon() {
    # Poll `docker info` until the daemon responds, or timeout. Prints
    # a progress dot per second so the user knows we're still alive.
    local max_wait="${1:-60}"
    local waited=0
    echo -n "  Waiting for Docker daemon"
    while ! docker info &>/dev/null 2>&1; do
        sleep 1
        waited=$((waited + 1))
        echo -n "."
        if (( waited >= max_wait )); then
            echo ""
            return 1
        fi
    done
    echo ""
    return 0
}

start_postgres() {
    step "PostgreSQL + pgvector"
    desc "DevBrain's database stores all memory (sessions, chunks, decisions,"
    desc "patterns, issues), factory job state, file locks, and notifications."
    desc "pgvector adds vector similarity search for semantic retrieval."

    if docker ps 2>/dev/null | grep -q devbrain-db; then
        skip "devbrain-db container running"
        return 0
    fi

    if ! command -v docker &>/dev/null; then
        fail "Docker command not found — install it first or open Docker Desktop"
        POST_ACTIONS+=("Start Docker Desktop, then run: cd $DEVBRAIN_HOME && docker compose up -d devbrain-db")
        return
    fi

    # If the daemon isn't responding, try to start Docker Desktop automatically
    # on macOS. First launch always needs a manual license-accept click, but
    # subsequent launches are fully unattended.
    if ! docker info &>/dev/null 2>&1; then
        if [[ "$OS" == "macos" ]] && [[ -d /Applications/Docker.app ]]; then
            info "Docker daemon not running — launching Docker Desktop..."
            open -a Docker

            # First-launch detection: if Docker Desktop has never been run,
            # a license/tutorial dialog will block until user interaction.
            if [[ ! -f "$HOME/Library/Application Support/Docker Desktop/settings-store.json" ]] \
               && [[ ! -f "$HOME/Library/Application Support/Docker Desktop/settings.json" ]]; then
                echo ""
                warn "This appears to be Docker's first launch."
                warn "A license agreement dialog may appear — accept it to continue."
                echo ""
            fi

            if _wait_for_docker_daemon 90; then
                ok "Docker daemon is up"
            else
                warn "Docker daemon didn't come up within 90 seconds."
                warn "If a license dialog is waiting, accept it and re-run install-devbrain."
                POST_ACTIONS+=("Open Docker Desktop, accept any license dialog, then re-run: install-devbrain")
                return
            fi
        else
            fail "Docker daemon not running"
            POST_ACTIONS+=("Start Docker Desktop, then run: cd $DEVBRAIN_HOME && docker compose up -d devbrain-db")
            return
        fi
    fi

    # Daemon is up — start the database
    info "Starting PostgreSQL container..."
    (cd "$DEVBRAIN_HOME" && docker compose up -d devbrain-db)
    info "Waiting for Postgres to be ready..."
    local retries=0
    while ! docker exec devbrain-db pg_isready -U devbrain &>/dev/null; do
        retries=$((retries + 1))
        if [[ $retries -gt 30 ]]; then
            fail "Postgres did not become ready in 30s"
            return
        fi
        sleep 1
    done
    ok "PostgreSQL running on port ${DEVBRAIN_DB_HOST_PORT:-5433}"
}

pull_models() {
    step "Ollama models"
    desc "DevBrain needs two local models:"
    desc "  • snowflake-arctic-embed2 — converts text into vectors for"
    desc "    semantic search (1024-dimensional embeddings)"
    desc "  • qwen2.5:7b — summarizes session transcripts and powers"
    desc "    natural-language CLI queries"
    echo ""
    warn "First-time download is ~10 GB total. Subsequent runs skip"
    warn "models that are already pulled."

    if ! command -v ollama &>/dev/null || ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
        fail "Ollama not reachable — skipping model pull"
        POST_ACTIONS+=("Start Ollama (brew services start ollama), then run: ollama pull snowflake-arctic-embed2 && ollama pull qwen2.5:7b")
        return
    fi

    for model in "${OLLAMA_MODELS[@]}"; do
        local base="${model%%:*}"
        if ollama list 2>/dev/null | grep -q "$base"; then
            skip "$model"
        else
            info "Pulling $model (this may take several minutes)..."
            ollama pull "$model"
            ok "$model pulled"
        fi
    done
}

build_mcp() {
    step "MCP server"
    desc "The Model Context Protocol server exposes 14 tools that let AI"
    desc "agents search memory, store decisions, manage factory jobs, and"
    desc "send notifications — all through a standard protocol."

    info "Installing npm dependencies..."
    (cd "$DEVBRAIN_HOME/mcp-server" && npm install --silent 2>&1 | tail -1)
    info "Building TypeScript..."
    (cd "$DEVBRAIN_HOME/mcp-server" && npm run build --silent 2>&1 | tail -1)
    ok "MCP server built at mcp-server/dist/index.js"
}

install_launchd() {
    step "Background ingest service"
    desc "A persistent background process that watches for new AI session"
    desc "files (from Claude Code, OpenClaw, Codex, Gemini) and automatically"
    desc "ingests them into DevBrain's database."

    if [[ "$OS" != "macos" ]]; then
        info "Linux detected — skipping launchd. See INSTALL.md for the"
        info "systemd unit, or run 'python ingest/main.py watch' manually."
        return
    fi

    if ask "Install launchd ingest service?"; then
        "$DEVBRAIN_HOME/scripts/install-ingest-service.sh"
        ok "Ingest service installed and running"
    else
        info "Skipped — run scripts/install-ingest-service.sh later if you want it."
    fi
}

# ─── Optional: PKRelay ──────────────────────────────────────────────────────

install_pkrelay() {
    step "PKRelay (optional)"
    echo ""
    desc "PKRelay is a companion browser extension + MCP server that gives"
    desc "your AI agents the ability to see and interact with web pages."
    desc ""
    desc "What it does:"
    desc "  • Captures structured page snapshots (DOM, text, metadata)"
    desc "    at a fraction of the token cost of raw screenshots"
    desc "  • Lets agents click, fill forms, and navigate via MCP tools"
    desc "  • Works with any MCP-compatible agent (Claude Code, etc.)"
    desc ""
    desc "Why it's useful with DevBrain:"
    desc "  • Factory review agents can verify UI changes in a real browser"
    desc "  • Research agents can browse documentation and capture findings"
    desc "  • QA agents can run lightweight browser checks after deployment"
    desc ""
    desc "It's a separate project (github.com/nooma-stack/pkrelay) installed"
    desc "alongside DevBrain — not a required dependency."

    if $SKIP_PKRELAY; then
        info "Skipped (--no-pkrelay flag)"
        return
    fi

    if ! ask_no "Install PKRelay?"; then
        info "Skipped — install later from github.com/nooma-stack/pkrelay"
        return
    fi

    # PKRelay is an optional companion install. Its failures must NOT
    # fail the DevBrain install — we explicitly ignore errors from its
    # installer and surface them as warnings.
    if [[ -d "$PKRELAY_HOME" ]]; then
        skip "PKRelay already cloned at $PKRELAY_HOME"
        info "Pulling latest..."
        (cd "$PKRELAY_HOME" && git pull --ff-only 2>/dev/null) || warn "PKRelay pull failed — using existing checkout"
    else
        info "Cloning PKRelay..."
        if ! git clone "$PKRELAY_REPO" "$PKRELAY_HOME" 2>&1 | tail -5; then
            warn "PKRelay clone failed — skipping (DevBrain install continues)"
            return 0
        fi
        ok "PKRelay cloned to $PKRELAY_HOME"
    fi

    if [[ -f "$PKRELAY_HOME/install.sh" ]]; then
        info "Running PKRelay installer..."
        if (cd "$PKRELAY_HOME" && bash install.sh); then
            ok "PKRelay installed"
        else
            warn "PKRelay installer returned non-zero. DevBrain continues."
            warn "Install PKRelay manually later: cd $PKRELAY_HOME && bash install.sh"
            POST_ACTIONS+=("PKRelay install failed during setup — run manually: cd $PKRELAY_HOME && bash install.sh")
        fi
    elif [[ -f "$PKRELAY_HOME/package.json" ]]; then
        info "Building PKRelay..."
        if (cd "$PKRELAY_HOME" && npm install --silent && npm run build --silent 2>/dev/null); then
            ok "PKRelay built"
        else
            warn "PKRelay build failed. DevBrain continues."
            POST_ACTIONS+=("PKRelay build failed — debug from $PKRELAY_HOME")
        fi
    fi

    POST_ACTIONS+=("Load PKRelay in Chrome: chrome://extensions → Enable Developer Mode → Load Unpacked → select $PKRELAY_HOME")
    echo ""
    info "PKRelay is a Chrome extension — it needs to be loaded manually."
    info "See the post-install actions below."
}

# ─── Shell shims (put `devbrain` + `install-devbrain` in PATH) ─────────────

pick_bin_dir() {
    # Prefer Homebrew's bin dir (always in PATH for brew users), fall back
    # to /usr/local/bin, then ~/.local/bin (no sudo, needs PATH export).
    local candidates=(
        "/opt/homebrew/bin"
        "/usr/local/bin"
        "$HOME/.local/bin"
    )
    for dir in "${candidates[@]}"; do
        if [[ -d "$dir" && -w "$dir" ]]; then
            echo "$dir"
            return 0
        fi
    done
    mkdir -p "$HOME/.local/bin"
    echo "$HOME/.local/bin"
}

install_shims() {
    step "Command-line shortcuts"
    desc "Install 'devbrain' and 'install-devbrain' as global commands so you"
    desc "can run them from anywhere without cd-ing to the repo."

    if $SKIP_SHIMS; then
        info "Skipped (--no-shims flag)"
        return
    fi

    # Always ensure ~/.local/bin is in PATH (XDG standard, non-controversial)
    # so our shims work even when the user opted out of brew shellenv.
    _ensure_local_bin_in_path

    local bin_dir
    bin_dir="$(pick_bin_dir)"

    local devbrain_shim="$bin_dir/devbrain"
    local install_shim="$bin_dir/install-devbrain"

    ln -sf "$DEVBRAIN_HOME/bin/devbrain" "$devbrain_shim"
    ok "Linked $devbrain_shim → $DEVBRAIN_HOME/bin/devbrain"

    ln -sf "$DEVBRAIN_HOME/scripts/install.sh" "$install_shim"
    ok "Linked $install_shim → $DEVBRAIN_HOME/scripts/install.sh"

    # If bin_dir isn't in PATH, tell the user how to fix it.
    case ":$PATH:" in
        *":$bin_dir:"*)
            ok "$bin_dir is in PATH"
            ;;
        *)
            warn "$bin_dir is not yet in PATH for this shell."
            warn "Open a new terminal session, or run: source $rc"
            ;;
    esac
}

# ─── Verification ──────────────────────────────────────────────────────────

run_doctor() {
    step "Verification"
    desc "Running devbrain doctor to verify the installation..."
    echo ""

    if "$DEVBRAIN_HOME/bin/devbrain" doctor; then
        echo ""
        ok "All checks passed!"
    else
        echo ""
        fail "Some checks failed — see output above for details."
        fail "Fix the issues and re-run this script (it's idempotent)."
    fi
}

# ─── Post-Install Actions ──────────────────────────────────────────────────

POST_ACTIONS=()

print_post_actions() {
    if [[ ${#POST_ACTIONS[@]} -eq 0 ]]; then
        return
    fi

    echo ""
    echo -e "${BOLD}━━━ Required Actions ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    echo -e "  ${YELLOW}These steps need your manual attention before DevBrain${RESET}"
    echo -e "  ${YELLOW}is fully operational:${RESET}"
    echo ""

    local i=1
    for action in "${POST_ACTIONS[@]}"; do
        echo -e "  ${BOLD}$i.${RESET} $action"
        i=$((i + 1))
    done

    echo ""
    echo -e "  After completing these, re-run ${CYAN}./bin/devbrain doctor${RESET}"
    echo -e "  to verify everything is green."
    echo ""
}

print_next_steps() {
    echo ""
    echo -e "${BOLD}━━━ Next Steps ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    echo -e "  Run the interactive setup wizard to configure your projects,"
    echo -e "  notification channels, and MCP client:"
    echo ""
    echo -e "    ${CYAN}./bin/devbrain setup${RESET}"
    echo ""
    echo -e "  Or jump straight to the docs:"
    echo -e "    ${DIM}• README.md        — overview${RESET}"
    echo -e "    ${DIM}• ARCHITECTURE.md  — how it works${RESET}"
    echo -e "    ${DIM}• INSTALL.md       — detailed manual install${RESET}"
    echo ""
}

# ─── Main ──────────────────────────────────────────────────────────────────

main() {
    # If we're not in a DevBrain repo (e.g., piped from curl), clone first
    # then re-execute from the cloned location.
    if ! is_in_devbrain_repo; then
        bootstrap_clone "$@"
        exit 0  # unreachable — exec replaces the process
    fi

    echo ""
    echo -e "${BOLD}DevBrain Installer${RESET}"
    echo -e "${DIM}Local-first persistent memory and dev factory for coding agents${RESET}"
    echo ""

    detect_os
    info "Detected: $OS ($ARCH)"
    info "DevBrain home: $DEVBRAIN_HOME"
    echo ""

    # Dependencies
    if [[ "$OS" == "macos" ]]; then
        install_homebrew
        ensure_rosetta_on_apple_silicon
    else
        install_linux_essentials
    fi

    install_docker
    install_ollama
    install_node
    install_python
    install_gh
    install_psql

    # DevBrain build
    setup_config
    setup_venvs
    start_postgres
    pull_models
    build_mcp
    install_launchd

    # Optional companions
    install_pkrelay

    # Global shortcuts
    install_shims

    # Verify
    run_doctor

    # Auto-run the interactive setup wizard unless disabled
    if $SKIP_SETUP; then
        info "Skipping setup wizard (--no-setup flag)"
    else
        echo ""
        if $AUTO_YES || ask "Run interactive setup wizard now?"; then
            echo ""
            "$DEVBRAIN_HOME/bin/devbrain" setup || true
        else
            info "Skipped — run './bin/devbrain setup' when ready."
        fi
    fi

    print_post_actions
    print_next_steps
}

main "$@"
