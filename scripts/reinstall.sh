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
# With --full: also removes Docker Desktop, Ollama models, Homebrew, and CLT.
# Colima/OrbStack VM and application data are not removed wholesale.
#
# Usage (sync to target machine and run):
#   bash reinstall.sh             # quick reset (preserves Homebrew/Ollama/CLT)
#   bash reinstall.sh --full      # extended toolchain reset
#   bash reinstall.sh --yes       # skip confirmation prompt
#
# Or directly from GitHub:
#   curl -fsSL https://raw.githubusercontent.com/nooma-stack/devbrain/main/scripts/reinstall.sh | bash
# ============================================================================

set -euo pipefail

DEVBRAIN_HOME="${DEVBRAIN_HOME:-$HOME/devbrain}"
PKRELAY_HOME="${PKRELAY_HOME:-$HOME/pkrelay}"
RESILIENCE_MANIFEST="$HOME/.devbrain/resilience/install-manifest.json"
INSTALL_TARGET_PATH="$HOME/.devbrain/install-target.json"
TARGET_CONTAINER_RUNTIME=""
TARGET_DOCKER_CONTEXT=""
EXPLICIT_CONTAINER_RUNTIME=""
EXPLICIT_DOCKER_CONTEXT=""

# Anchor CWD to $HOME so that when DEVBRAIN_HOME gets deleted mid-script,
# subshells (e.g., spawned by the Homebrew uninstaller) don't spam
# "getcwd: No such file or directory" noise. The working dir going
# invalid is harmless because we only ever use absolute paths — but it's
# ugly output that can mask real errors.
cd "$HOME"

FULL_RESET=false
AUTO_YES=false

print_usage() {
    cat <<'EOF'
Usage: bash scripts/reinstall.sh [options]

  --full                           Also reset the documented toolchain items
  --yes, -y                        Skip the confirmation prompt
  --container-runtime=RUNTIME      Required with --docker-context when an
                                   older install has no target metadata
  --docker-context=CONTEXT         Exact Docker context containing DevBrain
  --help, -h                       Show this help
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --full) FULL_RESET=true ;;
            --yes|-y) AUTO_YES=true ;;
            --container-runtime=*)
                EXPLICIT_CONTAINER_RUNTIME="${1#*=}"
                ;;
            --container-runtime)
                shift
                [[ $# -gt 0 ]] || {
                    echo "Error: --container-runtime requires a value" >&2
                    exit 2
                }
                EXPLICIT_CONTAINER_RUNTIME="$1"
                ;;
            --docker-context=*)
                EXPLICIT_DOCKER_CONTEXT="${1#*=}"
                ;;
            --docker-context)
                shift
                [[ $# -gt 0 ]] || {
                    echo "Error: --docker-context requires a value" >&2
                    exit 2
                }
                EXPLICIT_DOCKER_CONTEXT="$1"
                ;;
            --help|-h)
                print_usage
                exit 0
                ;;
            *)
                echo "Error: unknown reinstall option: $1" >&2
                exit 2
                ;;
        esac
        shift
    done
}
parse_args "$@"

if [[ -n "$EXPLICIT_CONTAINER_RUNTIME" || -n "$EXPLICIT_DOCKER_CONTEXT" ]]; then
    if [[ -z "$EXPLICIT_CONTAINER_RUNTIME" || -z "$EXPLICIT_DOCKER_CONTEXT" ]]; then
        echo "Error: --container-runtime and --docker-context must be used together" >&2
        exit 2
    fi
    case "$EXPLICIT_CONTAINER_RUNTIME" in
        colima|docker-desktop|docker-engine|orbstack) ;;
        *)
            echo "Error: unsupported --container-runtime" >&2
            exit 2
            ;;
    esac
    if [[ ! "$EXPLICIT_DOCKER_CONTEXT" =~ ^[A-Za-z0-9][-A-Za-z0-9._+]*$ ]]; then
        echo "Error: --docker-context contains unsupported characters" >&2
        exit 2
    fi
fi

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

_capture_container_target() {
    if [[ -L "$INSTALL_TARGET_PATH" ]] || {
        [[ -e "$INSTALL_TARGET_PATH" ]] && [[ ! -f "$INSTALL_TARGET_PATH" ]]
    }; then
        warn "Install target metadata is not a regular file:"
        warn "  $INSTALL_TARGET_PATH"
        return 1
    fi
    if [[ ! -f "$RESILIENCE_MANIFEST" && ! -f "$INSTALL_TARGET_PATH" ]]; then
        if [[ -n "$EXPLICIT_CONTAINER_RUNTIME" ]]; then
            TARGET_CONTAINER_RUNTIME="$EXPLICIT_CONTAINER_RUNTIME"
            TARGET_DOCKER_CONTEXT="$EXPLICIT_DOCKER_CONTEXT"
            return 0
        fi
        warn "No recorded DevBrain Docker target was found."
        warn "For an older install, retry with both:"
        warn "  --container-runtime=RUNTIME --docker-context=CONTEXT"
        return 1
    fi

    local manifest_python="$DEVBRAIN_HOME/.venv/bin/python"
    if [[ ! -x "$manifest_python" ]]; then
        manifest_python="$(command -v python3 2>/dev/null || true)"
    fi
    if [[ -z "$manifest_python" ]]; then
        warn "Python is required to validate DevBrain container metadata."
        return 1
    fi

    local manifest_target
    if ! manifest_target="$(
        PYTHONPATH="$DEVBRAIN_HOME${PYTHONPATH:+:$PYTHONPATH}" \
            "$manifest_python" - "$RESILIENCE_MANIFEST" \
            "$INSTALL_TARGET_PATH" <<'PY'
import json
import re
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1]).expanduser().resolve()
target_path = Path(sys.argv[2]).expanduser().resolve()

resilience_metadata = None
if manifest_path.is_file():
    from ops.resilience.install import _load_manifest

    resilience_metadata = _load_manifest(manifest_path)
    if resilience_metadata is None:
        raise SystemExit(
            "resilience manifest disappeared while it was being read"
        )

install_metadata = None
if target_path.is_file():
    try:
        install_metadata = json.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"install target metadata is unreadable: {exc}") from exc
    expected = {
        "schema_version",
        "generated_by",
        "profile",
        "container_runtime",
        "docker_context",
    }
    if not isinstance(install_metadata, dict) or set(install_metadata) != expected:
        raise SystemExit("install target metadata fields are invalid")
    if (
        install_metadata.get("schema_version") != 2
        or install_metadata.get("generated_by") != "devbrain-installer"
    ):
        raise SystemExit("install target metadata identity is invalid")
    if install_metadata.get("profile") not in {"workstation", "studio"}:
        raise SystemExit("install target metadata profile is invalid")
if resilience_metadata is None and install_metadata is None:
    raise SystemExit(
        "container target metadata disappeared while it was being read"
    )

if resilience_metadata is not None and install_metadata is not None:
    resilience_target = (
        resilience_metadata.get("container_runtime"),
        resilience_metadata.get("docker_context"),
    )
    install_target = (
        install_metadata.get("container_runtime"),
        install_metadata.get("docker_context"),
    )
    if resilience_target != install_target:
        raise SystemExit(
            "resilience and install target metadata disagree; "
            "refusing destructive cleanup"
        )

metadata = install_metadata or resilience_metadata
runtime = metadata.get("container_runtime")
context = metadata.get("docker_context")
if runtime not in {"colima", "docker-desktop", "docker-engine", "orbstack"}:
    raise SystemExit("container metadata has an invalid runtime")
if not isinstance(context, str) or not re.fullmatch(
    r"[A-Za-z0-9][-A-Za-z0-9._+]*", context
):
    raise SystemExit("container metadata has an invalid Docker context")

sys.stdout.write(f"{runtime}\t{context}")
PY
    )"; then
        warn "Could not validate the recorded container target."
        return 1
    fi

    IFS=$'\t' read -r TARGET_CONTAINER_RUNTIME \
        TARGET_DOCKER_CONTEXT <<< "$manifest_target"
    if [[ -z "$TARGET_CONTAINER_RUNTIME" || -z "$TARGET_DOCKER_CONTEXT" ]]; then
        warn "Validated metadata did not identify its container target."
        return 1
    fi
    if [[ -n "$EXPLICIT_CONTAINER_RUNTIME" ]] && {
        [[ "$EXPLICIT_CONTAINER_RUNTIME" != "$TARGET_CONTAINER_RUNTIME" ]] ||
        [[ "$EXPLICIT_DOCKER_CONTEXT" != "$TARGET_DOCKER_CONTEXT" ]]
    }; then
        warn "Explicit container target disagrees with recorded metadata."
        warn "Refusing destructive cleanup; repair the metadata first."
        return 1
    fi
}

docker_for_devbrain() {
    if [[ -n "$TARGET_DOCKER_CONTEXT" ]]; then
        command docker --context "$TARGET_DOCKER_CONTEXT" "$@"
    else
        command docker "$@"
    fi
}

_prepare_selected_container_runtime() {
    [[ -n "$TARGET_DOCKER_CONTEXT" ]] || return 0
    if ! command -v docker >/dev/null 2>&1; then
        warn "Docker CLI is unavailable; cannot clean context '$TARGET_DOCKER_CONTEXT'."
        return 1
    fi
    if docker_for_devbrain info >/dev/null 2>&1; then
        return 0
    fi

    case "$TARGET_CONTAINER_RUNTIME" in
        colima)
            if ! command -v colima >/dev/null 2>&1; then
                warn "Colima is unavailable; cannot start the selected Docker context."
                return 1
            fi
            info "Starting Colima so its DevBrain data can be removed..."
            colima start
            ;;
        docker-desktop)
            info "Launching Docker Desktop so its DevBrain data can be removed..."
            open -a Docker >/dev/null 2>&1 || true
            ;;
        orbstack)
            info "Launching OrbStack so its DevBrain data can be removed..."
            open -a OrbStack >/dev/null 2>&1 || true
            ;;
        docker-engine)
            info "Starting Docker Engine so its DevBrain data can be removed..."
            if command -v systemctl >/dev/null 2>&1; then
                sudo systemctl start docker
            elif command -v service >/dev/null 2>&1; then
                sudo service docker start
            fi
            ;;
    esac

    local attempt=0
    while (( attempt < 30 )); do
        attempt=$((attempt + 1))
        if docker_for_devbrain info >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    warn "Docker context '$TARGET_DOCKER_CONTEXT' did not become ready."
    return 1
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
echo -e "  ${RED}✗${RESET} $INSTALL_TARGET_PATH (recorded Docker target)"
if [[ -f "$RESILIENCE_MANIFEST" ]]; then
    echo -e "  ${RED}✗${RESET} Resilience service + files recorded in $RESILIENCE_MANIFEST"
fi

if $FULL_RESET; then
    echo ""
    echo -e "${YELLOW}--full flag set — also removing:${RESET}"
    echo -e "  ${RED}✗${RESET} Docker Desktop application and data (if installed)"
    echo -e "  ${RED}✗${RESET} Ollama models (snowflake-arctic-embed2, qwen2.5:7b — ~10GB to redownload)"
    echo -e "  ${RED}✗${RESET} Homebrew itself (will be reinstalled fresh)"
    echo -e "  ${RED}✗${RESET} Xcode Command Line Tools (will be reinstalled fresh, slow)"
    echo -e "  ${GREEN}✓${RESET} Colima/OrbStack VM disks are not wiped"
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

if [[ -d "$DEVBRAIN_HOME" || -f "$RESILIENCE_MANIFEST" \
      || -f "$INSTALL_TARGET_PATH" || -n "$EXPLICIT_CONTAINER_RUNTIME" ]]; then
    if ! _capture_container_target; then
        warn "Refusing to delete DevBrain without its exact Docker target."
        exit 1
    fi
    info "Using Docker context '$TARGET_DOCKER_CONTEXT' ($TARGET_CONTAINER_RUNTIME)."
    if ! _prepare_selected_container_runtime; then
        warn "Refusing to continue while DevBrain database cleanup is unavailable."
        exit 1
    fi
fi

if [[ -f "$RESILIENCE_MANIFEST" ]]; then
    if [[ ! -f "$DEVBRAIN_HOME/scripts/install-resilience.sh" ]]; then
        warn "Resilience is installed, but its manifest-aware uninstaller is missing:"
        warn "  $DEVBRAIN_HOME/scripts/install-resilience.sh"
        warn "Refusing to delete the repo and leave an unmanaged background service."
        exit 1
    fi
    info "Uninstalling manifest-owned resilience service and files..."
    if bash "$DEVBRAIN_HOME/scripts/install-resilience.sh" --uninstall --yes; then
        ok "Resilience service uninstalled"
    else
        warn "Resilience uninstall failed. Resolve it before deleting DevBrain."
        exit 1
    fi
else
    skip "No resilience install manifest found"
fi

if launchctl list 2>/dev/null | grep -q com.devbrain.ingest; then
    info "Unloading launchd ingest service..."
    launchctl unload ~/Library/LaunchAgents/com.devbrain.ingest.plist 2>/dev/null || true
    ok "Unloaded"
else
    skip "No launchd ingest service running"
fi

if [[ -n "$TARGET_DOCKER_CONTEXT" ]]; then
    if ! command -v docker >/dev/null 2>&1; then
        warn "Docker CLI disappeared before DevBrain data cleanup."
        exit 1
    fi
    if ! docker_for_devbrain info >/dev/null 2>&1; then
        warn "Selected Docker context '$TARGET_DOCKER_CONTEXT' became unavailable."
        exit 1
    fi
    devbrain_volumes=(
        devbrain_devbrain-pgdata
        devbrain_devbrain-wal-archive
        devbrain-pgdata
        devbrain-wal-archive
    )

    _inventory_contains() {
        local needle="$1"
        local inventory="$2"
        local item
        while IFS= read -r item; do
            [[ "$item" == "$needle" ]] && return 0
        done <<< "$inventory"
        return 1
    }

    _append_devbrain_volume() {
        local candidate="$1"
        local existing
        for existing in "${devbrain_volumes[@]}"; do
            [[ "$existing" == "$candidate" ]] && return 0
        done
        devbrain_volumes+=("$candidate")
    }

    container_present=false
    volume_present=false
    container_inventory=""
    volume_inventory=""
    if ! container_inventory="$(
        docker_for_devbrain container ls -a --format '{{.Names}}'
    )"; then
        warn "Could not inventory containers in the selected Docker context."
        exit 1
    fi
    if ! volume_inventory="$(docker_for_devbrain volume ls -q)"; then
        warn "Could not inventory volumes in the selected Docker context."
        exit 1
    fi

    mounted_volumes=""
    if _inventory_contains "devbrain-db" "$container_inventory"; then
        container_present=true
        if ! mounted_volumes="$(
            docker_for_devbrain container inspect \
            --format '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}' \
            devbrain-db 2>/dev/null
        )"; then
            warn "Could not inspect the confirmed devbrain-db container."
            exit 1
        fi
        while IFS= read -r volume; do
            [[ -n "$volume" ]] || continue
            if [[ ! "$volume" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
                warn "Container reported an unsafe volume name; refusing cleanup."
                exit 1
            fi
            _append_devbrain_volume "$volume"
        done <<< "$mounted_volumes"
    fi
    for volume in "${devbrain_volumes[@]}"; do
        if _inventory_contains "$volume" "$volume_inventory"; then
            volume_present=true
        fi
    done
    labeled_candidates=""
    for logical_volume in devbrain-pgdata devbrain-wal-archive; do
        labeled=""
        if ! labeled="$(
            docker_for_devbrain volume ls -q \
                --filter "label=com.docker.compose.volume=$logical_volume"
        )"; then
            warn "Could not inspect labeled Compose volumes."
            exit 1
        fi
        if [[ -n "$labeled" ]]; then
            labeled_candidates+="${labeled_candidates:+$'\n'}$labeled"
            volume_present=true
        fi
    done
    labeled_services=""
    if ! labeled_services="$(
        docker_for_devbrain container ls -a -q \
            --filter label=com.docker.compose.service=devbrain-db
    )"; then
        warn "Could not inspect labeled Compose services."
        exit 1
    fi

    if $container_present || $volume_present || \
       [[ -n "$labeled_candidates" || -n "$labeled_services" ]]; then
        info "Removing devbrain-db container and volume..."
        if $container_present; then
            if ! docker_for_devbrain container rm -f -v \
                devbrain-db >/dev/null 2>&1; then
                warn "Could not remove the confirmed devbrain-db container."
                exit 1
            fi
        fi

        if ! volume_inventory="$(docker_for_devbrain volume ls -q)"; then
            warn "Could not refresh the Docker volume inventory."
            exit 1
        fi
        for volume in "${devbrain_volumes[@]}"; do
            if _inventory_contains "$volume" "$volume_inventory"; then
                if ! docker_for_devbrain volume rm "$volume" >/dev/null 2>&1; then
                    warn "Could not remove confirmed DevBrain volume '$volume'."
                    exit 1
                fi
            fi
        done

        if ! container_inventory="$(
            docker_for_devbrain container ls -a --format '{{.Names}}'
        )"; then
            warn "Could not verify the post-cleanup container inventory."
            exit 1
        fi
        if ! volume_inventory="$(docker_for_devbrain volume ls -q)"; then
            warn "Could not verify the post-cleanup volume inventory."
            exit 1
        fi

        cleanup_incomplete=false
        if _inventory_contains "devbrain-db" "$container_inventory"; then
            cleanup_incomplete=true
        fi
        for volume in "${devbrain_volumes[@]}"; do
            if _inventory_contains "$volume" "$volume_inventory"; then
                cleanup_incomplete=true
            fi
        done
        leftover_services=""
        if ! leftover_services="$(
            docker_for_devbrain container ls -a -q \
                --filter label=com.docker.compose.service=devbrain-db
        )"; then
            warn "Could not verify Compose service cleanup."
            exit 1
        fi
        leftover_volumes=""
        for logical_volume in devbrain-pgdata devbrain-wal-archive; do
            labeled=""
            if ! labeled="$(
                docker_for_devbrain volume ls -q \
                    --filter "label=com.docker.compose.volume=$logical_volume"
            )"; then
                warn "Could not verify Compose volume cleanup."
                exit 1
            fi
            if [[ -n "$labeled" ]]; then
                leftover_volumes+="${leftover_volumes:+$'\n'}$labeled"
            fi
        done
        if [[ -n "$leftover_services" || -n "$leftover_volumes" ]]; then
            cleanup_incomplete=true
        fi
        if $cleanup_incomplete; then
            warn "DevBrain container data remains in Docker context '$TARGET_DOCKER_CONTEXT'."
            if [[ -n "$leftover_volumes" ]]; then
                warn "Labeled volumes still present:"
                while IFS= read -r volume; do
                    [[ -n "$volume" ]] && warn "  $volume"
                done <<< "$leftover_volumes"
            fi
            exit 1
        fi
        ok "Container + volume removed"
    else
        skip "No devbrain-db container or volume found"
    fi
else
    skip "Docker daemon unavailable; no recorded DevBrain context to clean"
fi

if [[ -f "$INSTALL_TARGET_PATH" ]]; then
    rm -f "$INSTALL_TARGET_PATH"
    ok "Removed recorded container target"
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
        # osascript only quits the main GUI process. Docker Desktop also spawns
        # a menu-bar helper, backend daemons (com.docker.*), and a VM. Without
        # terminating these explicitly, the helper keeps holding the menu-bar
        # icon after /Applications/Docker.app is removed, leaving an orphan
        # icon that only disappears when the user clicks it.
        killall Docker 2>/dev/null || true
        pkill -f "Docker Desktop" 2>/dev/null || true
        pkill -f "com.docker" 2>/dev/null || true
        sleep 1
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

        # Docker Desktop installs CLI plugins to /usr/local/cli-plugins/
        # that persist across app uninstalls. Next `brew install --cask
        # docker-desktop` refuses to overwrite these and errors with:
        # "there is already a Binary at '/usr/local/cli-plugins/docker-compose'"
        # Remove the whole plugin dir if it's Docker's (empty otherwise OK).
        if [[ -d /usr/local/cli-plugins ]]; then
            info "Removing Docker CLI plugins at /usr/local/cli-plugins..."
            sudo rm -rf /usr/local/cli-plugins 2>/dev/null || true
        fi

        # Docker Desktop also installs many CLI binaries in /usr/local/bin
        # (docker, docker-compose, hub-tool, compose-switch, kubectl.docker,
        # com.docker.*, docker-credential-*). These are symlinks into
        # /Applications/Docker.app and survive cask uninstalls, blocking
        # future `brew install` with errors like:
        # "there is already a Binary at '/usr/local/bin/hub-tool'"
        # "there is already a Binary at '/usr/local/bin/docker-credential-desktop'"
        # Strategy: use shell globs for the pattern-matching names AND a
        # find-based catch-all for any remaining Docker symlinks.
        info "Cleaning up Docker CLI binaries in /usr/local/bin..."
        for bin_dir in /usr/local/bin /usr/local/sbin; do
            [[ -d "$bin_dir" ]] || continue

            # Glob-matched family names (docker-credential-*, com.docker.*)
            # Use shell expansion via sudo sh -c since sudo doesn't expand globs directly.
            sudo sh -c "rm -f $bin_dir/docker-credential-* $bin_dir/com.docker.* 2>/dev/null" || true

            # Known fixed-name binaries
            for bin in docker docker-compose hub-tool compose-switch kubectl.docker; do
                if [[ -e "$bin_dir/$bin" || -L "$bin_dir/$bin" ]]; then
                    sudo rm -f "$bin_dir/$bin" 2>/dev/null || true
                fi
            done

            # Catch-all for anything else pointing at Docker.app (covers future
            # Docker binaries we haven't hardcoded)
            while IFS= read -r -d '' link; do
                sudo rm -f "$link" 2>/dev/null || true
            done < <(find "$bin_dir" -maxdepth 1 -type l -lname "*Docker.app*" -print0 2>/dev/null)
        done

        # Some Docker installs also leave helper files here
        for path in \
            "$HOME/Library/Application Support/com.docker.helper" \
            "$HOME/Library/Application Support/Docker"; do
            [[ -e "$path" ]] && rm -rf "$path" 2>/dev/null
        done

        ok "Docker Desktop + data directories + CLI plugins removed"
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
        # Force-remove any leftover /opt/homebrew directory (Homebrew's
        # own uninstaller in NONINTERACTIVE mode leaves etc/, share/,
        # var/ behind, plus any stray files from earlier failed installs).
        if [[ -d /opt/homebrew ]]; then
            info "Cleaning up /opt/homebrew..."
            sudo rm -rf /opt/homebrew
        fi

        # /etc/paths.d/homebrew is root-owned, so Homebrew's uninstaller
        # can't remove it in NONINTERACTIVE mode. Clean it up ourselves.
        if [[ -f /etc/paths.d/homebrew ]]; then
            info "Cleaning up /etc/paths.d/homebrew..."
            sudo rm -f /etc/paths.d/homebrew
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

# ─── Post-wipe verification ────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Verifying uninstall${RESET}"

_check_removed() {
    local label="$1"
    local path="$2"
    if [[ -e "$path" ]]; then
        warn "$label: still present at $path"
        return 1
    else
        ok "$label: removed"
        return 0
    fi
}

_check_cmd_removed() {
    local label="$1"
    local cmd="$2"
    local resolved
    resolved=$(command -v "$cmd" 2>/dev/null || true)
    if [[ -z "$resolved" ]]; then
        ok "$label: removed"
        return 0
    fi
    # bash's hash table can return a path for a binary that was just rm'd.
    # Treat "resolved but file missing" as removed (with a stale-hash note).
    if [[ ! -e "$resolved" ]]; then
        ok "$label: removed (shell hash is stale: $resolved — open a new terminal)"
        return 0
    fi
    warn "$label: still in PATH ($resolved)"
    return 1
}

# Flush bash's command hash table so the checks below see reality, not
# cached lookups from earlier in this script (e.g., `brew uninstall ...`
# hashes /opt/homebrew/bin/brew before we rm -rf /opt/homebrew).
hash -r 2>/dev/null || true

verify_failures=0

_check_removed "DevBrain repo" "$DEVBRAIN_HOME" || ((verify_failures++))
_check_removed "launchd plist" "$HOME/Library/LaunchAgents/com.devbrain.ingest.plist" || ((verify_failures++))
_check_removed "container target metadata" "$INSTALL_TARGET_PATH" || ((verify_failures++))

if $FULL_RESET; then
    _check_removed "Docker.app" "/Applications/Docker.app" || ((verify_failures++))
    _check_removed "Docker CLI plugins" "/usr/local/cli-plugins" || ((verify_failures++))

    # Check for Docker binaries in /usr/local/bin that would block re-install.
    # Use find instead of hardcoded enumeration so we catch any new binary
    # Docker might install in the future (e.g., new credential helpers).
    leftover_docker=$(find /usr/local/bin -maxdepth 1 \
        \( -name "docker*" -o -name "com.docker.*" -o -name "hub-tool" \
           -o -name "compose-switch" -o -name "kubectl.docker" \) 2>/dev/null)
    if [[ -n "$leftover_docker" ]]; then
        warn "Docker-related binaries still in /usr/local/bin:"
        while IFS= read -r f; do
            [[ -n "$f" ]] && echo -e "    ${YELLOW}•${RESET} $f"
        done <<< "$leftover_docker"
        info "Remove with: sudo rm -f /usr/local/bin/docker-credential-* /usr/local/bin/com.docker.* /usr/local/bin/hub-tool /usr/local/bin/compose-switch"
        ((verify_failures++))
    else
        ok "Docker CLI binaries in /usr/local/bin: none"
    fi
    _check_removed "Homebrew prefix" "/opt/homebrew" || ((verify_failures++))
    _check_removed "Homebrew /etc/paths.d entry" "/etc/paths.d/homebrew" || ((verify_failures++))
    _check_removed "CLT" "/Library/Developer/CommandLineTools" || ((verify_failures++))
    _check_cmd_removed "docker binary" "docker" || ((verify_failures++))
    _check_cmd_removed "brew binary" "brew" || ((verify_failures++))
    _check_cmd_removed "ollama binary" "ollama" || ((verify_failures++))

    # Check for leftover Docker LaunchAgents that could auto-start Docker
    if ls "$HOME/Library/LaunchAgents/"com.docker.* &>/dev/null || \
       ls /Library/LaunchAgents/com.docker.* &>/dev/null 2>&1 || \
       ls /Library/LaunchDaemons/com.docker.* &>/dev/null 2>&1; then
        warn "Docker LaunchAgents/Daemons still present (can auto-restart Docker)"
        info "Clean up with:"
        info "  ls ~/Library/LaunchAgents/com.docker.* /Library/LaunchAgents/com.docker.* /Library/LaunchDaemons/com.docker.* 2>/dev/null"
        info "  sudo rm -f ~/Library/LaunchAgents/com.docker.* /Library/LaunchAgents/com.docker.* /Library/LaunchDaemons/com.docker.*"
        ((verify_failures++))
    else
        ok "No Docker LaunchAgents/Daemons remaining"
    fi
fi

if (( verify_failures > 0 )); then
    echo ""
    warn "$verify_failures verification check(s) failed — some artifacts remain."
    warn "Review the warnings above. The installer is idempotent so re-running"
    warn "it will work, but the clean-slate test is compromised."
else
    echo ""
    ok "All items verified removed"
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
