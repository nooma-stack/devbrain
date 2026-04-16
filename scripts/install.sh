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

DEVBRAIN_HOME="${DEVBRAIN_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || pwd)}"
PKRELAY_HOME="${PKRELAY_HOME:-$HOME/pkrelay}"
PKRELAY_REPO="https://github.com/nooma-stack/pkrelay.git"
OLLAMA_MODELS=("snowflake-arctic-embed2" "qwen2.5:7b")
AUTO_YES=false
SKIP_PKRELAY=false

for arg in "$@"; do
    case "$arg" in
        --yes|-y) AUTO_YES=true ;;
        --no-pkrelay) SKIP_PKRELAY=true ;;
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
    read -rp "  $prompt" answer
    [[ -z "$answer" || "$answer" =~ ^[Yy] ]]
}

ask_no() {
    if $AUTO_YES; then return 1; fi
    local prompt="$1 [y/N]: "
    read -rp "  $prompt" answer
    [[ "$answer" =~ ^[Yy] ]]
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
        *)
            echo "Unsupported OS: $OSTYPE"
            echo "DevBrain supports macOS and Linux. See INSTALL.md for details."
            exit 1
            ;;
    esac
}

# ─── Dependency Installers ─────────────────────────────────────────────────

install_homebrew() {
    step "Package manager"
    desc "Homebrew is the standard macOS package manager. All other"
    desc "dependencies are installed through it."
    if command -v brew &>/dev/null; then
        skip "Homebrew $(brew --version | head -1 | awk '{print $2}')"
    else
        info "Installing Homebrew..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)"
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

install_docker() {
    step "Docker"
    desc "Runs PostgreSQL + pgvector in a container. DevBrain stores all"
    desc "memory, sessions, and factory state in this database."

    if command -v docker &>/dev/null; then
        skip "Docker $(docker --version 2>/dev/null | awk '{print $3}' | tr -d ',')"
    elif [[ "$OS" == "macos" ]]; then
        info "Installing Docker Desktop via Homebrew..."
        desc "(Alternatives: Colima or OrbStack — see INSTALL.md)"
        brew install --cask docker
        ok "Docker Desktop installed"
        warn "ACTION REQUIRED: Open Docker Desktop from Applications to"
        warn "accept the license agreement before containers can start."
        POST_ACTIONS+=("Open Docker Desktop (Applications → Docker) and accept the license agreement")
    else
        info "Installing Docker Engine..."
        curl -fsSL https://get.docker.com | sh
        sudo usermod -aG docker "$USER"
        ok "Docker installed (you may need to log out/in for group changes)"
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

install_python() {
    step "Python"
    desc "Runs the ingest pipeline (session capture + embedding), the"
    desc "factory orchestrator (automated code generation pipeline), and"
    desc "the DevBrain CLI."

    if command -v python3 &>/dev/null; then
        local ver
        ver="$(python3 --version | awk '{print $2}')"
        local major minor
        major="${ver%%.*}"
        minor="${ver#*.}"
        minor="${minor%%.*}"
        if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
            skip "Python $ver"
            return
        else
            warn "Python $ver found but 3.11+ required"
        fi
    fi

    if [[ "$OS" == "macos" ]]; then
        info "Installing Python 3 via Homebrew..."
        brew install python@3
        ok "Python installed"
    else
        info "Installing Python 3..."
        sudo apt-get install -y -qq python3 python3-venv python3-pip
        ok "Python installed"
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
        brew install libpq
        brew link --force libpq 2>/dev/null || true
        ok "psql installed"
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

    # Root venv
    if [[ ! -f "$DEVBRAIN_HOME/.venv/bin/python" ]]; then
        info "Creating root venv..."
        python3 -m venv "$DEVBRAIN_HOME/.venv"
        ok "Root venv created"
    else
        skip "Root .venv"
    fi
    info "Installing root Python dependencies..."
    "$DEVBRAIN_HOME/.venv/bin/pip" install -q --upgrade pip
    "$DEVBRAIN_HOME/.venv/bin/pip" install -q -r "$DEVBRAIN_HOME/requirements.txt"
    ok "Root deps installed (click, psycopg2, pyyaml, textual, pytest)"

    # Ingest venv
    if [[ ! -f "$DEVBRAIN_HOME/ingest/.venv/bin/python" ]]; then
        info "Creating ingest venv..."
        python3 -m venv "$DEVBRAIN_HOME/ingest/.venv"
        ok "Ingest venv created"
    else
        skip "Ingest .venv"
    fi
    info "Installing ingest Python dependencies..."
    "$DEVBRAIN_HOME/ingest/.venv/bin/pip" install -q --upgrade pip
    "$DEVBRAIN_HOME/ingest/.venv/bin/pip" install -q -r "$DEVBRAIN_HOME/ingest/requirements.txt"
    ok "Ingest deps installed (psycopg2, watchdog, pyyaml)"
}

start_postgres() {
    step "PostgreSQL + pgvector"
    desc "DevBrain's database stores all memory (sessions, chunks, decisions,"
    desc "patterns, issues), factory job state, file locks, and notifications."
    desc "pgvector adds vector similarity search for semantic retrieval."

    if docker ps 2>/dev/null | grep -q devbrain-db; then
        skip "devbrain-db container running"
    elif ! command -v docker &>/dev/null; then
        fail "Docker not available — install it first or open Docker Desktop"
        POST_ACTIONS+=("Start Docker Desktop, then run: cd $DEVBRAIN_HOME && docker compose up -d devbrain-db")
        return
    elif ! docker info &>/dev/null 2>&1; then
        fail "Docker daemon not running"
        POST_ACTIONS+=("Start Docker Desktop, then run: cd $DEVBRAIN_HOME && docker compose up -d devbrain-db")
        return
    else
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
    fi
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

    if [[ -d "$PKRELAY_HOME" ]]; then
        skip "PKRelay already cloned at $PKRELAY_HOME"
        info "Pulling latest..."
        (cd "$PKRELAY_HOME" && git pull --ff-only 2>/dev/null || true)
    else
        info "Cloning PKRelay..."
        git clone "$PKRELAY_REPO" "$PKRELAY_HOME"
        ok "PKRelay cloned to $PKRELAY_HOME"
    fi

    if [[ -f "$PKRELAY_HOME/install.sh" ]]; then
        info "Running PKRelay installer..."
        (cd "$PKRELAY_HOME" && bash install.sh)
        ok "PKRelay installed"
    elif [[ -f "$PKRELAY_HOME/package.json" ]]; then
        info "Building PKRelay..."
        (cd "$PKRELAY_HOME" && npm install --silent && npm run build --silent 2>/dev/null || true)
        ok "PKRelay built"
    fi

    POST_ACTIONS+=("Load PKRelay in Chrome: chrome://extensions → Enable Developer Mode → Load Unpacked → select $PKRELAY_HOME")
    echo ""
    info "PKRelay is a Chrome extension — it needs to be loaded manually."
    info "See the post-install actions below."
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

    # Verify
    run_doctor
    print_post_actions
    print_next_steps
}

main "$@"
