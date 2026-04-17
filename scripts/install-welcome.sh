#!/usr/bin/env bash
# ============================================================================
# DevBrain installer — welcome screen
# ============================================================================
#
# Sourced by scripts/install.sh. Exposes three functions:
#   welcome_can_animate   → 0 (yes) or 1 (no — CI, non-TTY, NO_COLOR, etc.)
#   welcome_show          → banner + optional spinning-brain / matrix-rain
#                           animation. Skippable with any keypress.
#   welcome_prompt_style  → asks verbose vs quiet, echoes "verbose" or
#                           "quiet" on stdout. Falls back to verbose on
#                           non-interactive invocations (CI).
#
# The animation runs when possible and degrades gracefully when it can't.
# Respects $NO_COLOR (https://no-color.org), $CI, and $DEVBRAIN_NO_ANIMATION.
# ============================================================================

# ─── Capability detection ──────────────────────────────────────────────────

welcome_can_animate() {
    [[ -t 1 ]] || return 1
    [[ -z "${CI:-}" ]] || return 1
    [[ -z "${NO_COLOR:-}" ]] || return 1
    [[ -z "${DEVBRAIN_NO_ANIMATION:-}" ]] || return 1

    local colors
    colors=$(tput colors 2>/dev/null || echo 0)
    [[ "$colors" -ge 8 ]] || return 1

    local cols lines
    cols=$(tput cols 2>/dev/null || echo 0)
    lines=$(tput lines 2>/dev/null || echo 0)
    # Need room for the 54-wide banner + some margin + brain + rain.
    [[ "$cols" -ge 62 ]] && [[ "$lines" -ge 20 ]]
}

# ─── ANSI helpers ──────────────────────────────────────────────────────────

_ANSI_RESET=$'\033[0m'
_ANSI_BOLD=$'\033[1m'
_ANSI_DIM=$'\033[2m'
_ANSI_CYAN=$'\033[96m'
_ANSI_MAGENTA=$'\033[95m'
_ANSI_GREEN=$'\033[32m'
_ANSI_BRIGHT_GREEN=$'\033[92m'
_ANSI_WHITE=$'\033[97m'
_ANSI_HIDE_CURSOR=$'\033[?25l'
_ANSI_SHOW_CURSOR=$'\033[?25h'
_ANSI_CLEAR=$'\033[2J'
_ANSI_HOME=$'\033[H'

_move() {
    # $1 = row (1-based), $2 = col (1-based)
    printf '\033[%d;%dH' "$1" "$2"
}

# ─── Static assets ─────────────────────────────────────────────────────────
#
# ASCII banner (standard figlet "DevBrain"). 54 cols × 5 rows.
# We cache into an array so each line is indexable.

_welcome_banner_lines() {
    # shellcheck disable=SC2028
    cat <<'EOF'
  ____              ____             _
 |  _ \   _____   _| __ )  _ __ __ _(_)_ __
 | | | | / _ \ \ / /  _ \ | '__/ _` | | '_ \
 | |_| ||  __/\ V /| |_) || | | (_| | | | | |
 |____/  \___| \_/ |____/ |_|  \__,_|_|_| |_|
EOF
}

# Spinning-brain frames. 11 cols × 6 rows each, space-padded to constant
# width. The "rotation" is conveyed by varying width + feature offsets so
# the brain appears to turn around its vertical axis. Forward rotation:
# front → 3/4-right → edge → 3/4-left → front, then repeats.

_welcome_brain_frames() {
    # Each frame begins with "---" so we can split on it.
    cat <<'EOF'
---
  .-"""-.
 /   _   \
| (o) (o) |
| \__ / /\|
 \  \_/  /
  '-___-'
---
   .--.-.
  / _  \ \
 |(o)(o)  |
 | \_/  /\|
  \  _  /
   '-.-'
---
    .-.
   /   \
  |     |
  |  |  |
   \   /
    '-'
---
   .-.--.
  / /  _ \
 |  (o)(o)|
 |/\  / _/
  \  _  /
   '-.-'
EOF
}

_welcome_tagline="Universal persistent memory + dev factory for coding agents"

# ─── Cleanup on Ctrl-C or normal exit ──────────────────────────────────────

_welcome_cleanup() {
    printf '%s%s' "$_ANSI_SHOW_CURSOR" "$_ANSI_RESET"
    # Clear to end of screen from cursor (avoids leaving artefacts)
    printf '\033[J'
}

# ─── Matrix rain + spinning brain animation ────────────────────────────────

_welcome_animate() {
    local duration_ms="${1:-3000}"

    local cols lines
    cols=$(tput cols)
    lines=$(tput lines)

    # Load banner lines
    local banner_lines=()
    local line
    while IFS='' read -r line; do
        banner_lines+=("$line")
    done < <(_welcome_banner_lines)
    local banner_h=${#banner_lines[@]}
    local banner_w=54

    # Load brain frames (6 rows × 11 cols per frame, constant-padded)
    local frames_raw
    frames_raw=$(_welcome_brain_frames)
    local -a frames=()
    local buf=""
    local in_frame=0
    while IFS='' read -r line; do
        if [[ "$line" == "---" ]]; then
            if [[ $in_frame -eq 1 ]]; then
                frames+=("$buf")
            fi
            buf=""
            in_frame=1
            continue
        fi
        # Pad / truncate to 11 cols to guarantee constant width
        local padded
        padded=$(printf '%-11s' "$line")
        padded="${padded:0:11}"
        buf+="${padded}"$'\n'
    done <<< "$frames_raw"
    [[ -n "$buf" ]] && frames+=("$buf")
    local frame_count=${#frames[@]}
    local brain_w=11
    local brain_h=6

    # Layout centers
    local banner_row=3
    local banner_col=$(( (cols - banner_w) / 2 + 1 ))
    [[ $banner_col -lt 1 ]] && banner_col=1
    local brain_row=$(( banner_row + banner_h + 2 ))
    local brain_col=$(( (cols - brain_w) / 2 + 1 ))
    local tagline_row=$(( brain_row + brain_h + 1 ))
    local tagline_col=$(( (cols - ${#_welcome_tagline}) / 2 + 1 ))

    # Set of "brain-owned" cells per frame so rain skips them. Reuse the
    # same bounding box across frames — simpler and looks fine.
    # (row,col) is brain-owned if
    #   brain_row <= row < brain_row + brain_h
    #   brain_col <= col < brain_col + brain_w

    # Rain state: per-column head row (1-based) and speed modulo.
    # Array indices are column numbers.
    local -a head=()
    local -a speed=()
    local c
    for (( c = 1; c <= cols; c++ )); do
        head[c]=$(( RANDOM % lines - (RANDOM % 15) ))
        speed[c]=$(( (RANDOM % 3) + 1 ))
    done

    # Random-char palette
    local -a chars=(
        '@' '#' '$' '%' '&' '*' '+' '!' '?' '/' '\' '~' '='
        '0' '1' '2' '3' '4' '5' '6' '7' '8' '9'
        'A' 'B' 'C' 'D' 'E' 'F' 'H' 'K' 'M' 'N' 'P' 'R' 'T' 'X' 'Y' 'Z'
    )
    local char_count=${#chars[@]}

    trap _welcome_cleanup EXIT INT TERM
    printf '%s%s%s' "$_ANSI_HIDE_CURSOR" "$_ANSI_CLEAR" "$_ANSI_HOME"

    # Also a key-skippable loop: set stdin non-blocking via read -t 0.
    # Bash read -t 0 returns 0 if data is available, else non-zero.

    local start_sec start_ns end_ns
    start_sec=$(date +%s)
    local trail_len=10
    local frame_no=0

    while true; do
        local now_sec
        now_sec=$(date +%s)
        local elapsed_ms=$(( (now_sec - start_sec) * 1000 ))
        (( elapsed_ms >= duration_ms )) && break

        # Skippable: poll for a single keypress (short timeout so the
        # frame cadence stays ~12 fps). Consumes the char so it doesn't
        # leak into subsequent prompts.
        if read -r -t 0.01 -n 1 -s _unused 2>/dev/null; then
            break
        fi

        local current_frame="${frames[$(( frame_no % frame_count ))]}"

        # Build this frame's output into one buffer then print atomically.
        # Much faster than per-cell printfs.
        local out="$_ANSI_HOME"

        local row col
        for (( row = 1; row <= lines; row++ )); do
            out+=$(printf '\033[%d;1H\033[K' "$row")  # clear line
        done

        # Draw rain. For each column, render a trail of chars from
        # head[col] upward (trail_len cells).
        for (( c = 1; c <= cols; c++ )); do
            if (( frame_no % speed[c] == 0 )); then
                head[c]=$(( head[c] + 1 ))
            fi
            if (( head[c] > lines + trail_len )); then
                head[c]=$(( -(RANDOM % 15) ))
                speed[c]=$(( (RANDOM % 3) + 1 ))
            fi

            local t
            for (( t = 0; t < trail_len; t++ )); do
                local r=$(( head[c] - t ))
                (( r < 1 || r > lines )) && continue

                # Skip brain bbox
                if (( r >= brain_row && r < brain_row + brain_h \
                      && c >= brain_col && c < brain_col + brain_w )); then
                    continue
                fi
                # Skip banner bbox (text stands out over rain too)
                if (( r >= banner_row && r < banner_row + banner_h \
                      && c >= banner_col && c < banner_col + banner_w )); then
                    continue
                fi
                # Skip tagline row
                if (( r == tagline_row \
                      && c >= tagline_col \
                      && c < tagline_col + ${#_welcome_tagline} )); then
                    continue
                fi

                local ch="${chars[$(( RANDOM % char_count ))]}"
                local color
                if (( t == 0 )); then
                    color="$_ANSI_WHITE"
                elif (( t < 3 )); then
                    color="$_ANSI_BRIGHT_GREEN"
                else
                    color="${_ANSI_DIM}${_ANSI_GREEN}"
                fi
                out+=$(printf '\033[%d;%dH%s%s' "$r" "$c" "$color" "$ch")
            done
        done

        # Draw banner (in cyan) on top of cleared region
        local br
        for (( br = 0; br < banner_h; br++ )); do
            out+=$(printf '\033[%d;%dH%s%s%s' \
                "$(( banner_row + br ))" "$banner_col" \
                "$_ANSI_BOLD$_ANSI_CYAN" \
                "${banner_lines[br]}" \
                "$_ANSI_RESET")
        done

        # Draw brain (in magenta) — split current_frame by newlines
        local IFS_bak="$IFS"
        IFS=$'\n'
        local -a brain_lines=($current_frame)
        IFS="$IFS_bak"
        local i
        for (( i = 0; i < brain_h && i < ${#brain_lines[@]}; i++ )); do
            out+=$(printf '\033[%d;%dH%s%s%s' \
                "$(( brain_row + i ))" "$brain_col" \
                "$_ANSI_BOLD$_ANSI_MAGENTA" \
                "${brain_lines[i]}" \
                "$_ANSI_RESET")
        done

        # Draw tagline (dim white)
        out+=$(printf '\033[%d;%dH%s%s%s' \
            "$tagline_row" "$tagline_col" \
            "$_ANSI_DIM" \
            "$_welcome_tagline" \
            "$_ANSI_RESET")

        # Emit the whole frame at once
        printf '%s' "$out"

        frame_no=$(( frame_no + 1 ))
        sleep 0.08
    done

    _welcome_cleanup
    trap - EXIT INT TERM
}

# ─── Static banner (shown always, animation or not) ────────────────────────

_welcome_static_banner() {
    printf '\n'
    if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
        printf '%s' "$_ANSI_BOLD$_ANSI_CYAN"
        _welcome_banner_lines
        printf '%s\n' "$_ANSI_RESET"
        printf '  %s%s%s\n\n' "$_ANSI_DIM" "$_welcome_tagline" "$_ANSI_RESET"
    else
        _welcome_banner_lines
        printf '\n  %s\n\n' "$_welcome_tagline"
    fi
}

# ─── Public: show the welcome screen ───────────────────────────────────────

welcome_show() {
    if welcome_can_animate; then
        _welcome_animate 3000
        # After the animation, wipe the screen so the static banner
        # starts clean without any lingering rain / brain fragments.
        printf '%s%s' "$_ANSI_CLEAR" "$_ANSI_HOME"
    fi
    _welcome_static_banner
}

# ─── Public: prompt verbose vs quiet ───────────────────────────────────────

_welcome_can_prompt_user() {
    # True if we can actually read a line of input from either stdin or
    # /dev/tty. `-r /dev/tty` is not sufficient — the device node exists
    # and looks readable even when no controlling terminal is attached,
    # and the subsequent `read` fails with "Device not configured".
    [[ -t 0 ]] && return 0
    # shellcheck disable=SC2188
    ( exec < /dev/tty ) 2>/dev/null && return 0
    return 1
}

welcome_prompt_style() {
    # Env override takes precedence, including in non-interactive runs —
    # lets CI/tests force one mode without touching the installer flags.
    case "${DEVBRAIN_INSTALL_STYLE:-}" in
        verbose) echo "verbose"; return 0 ;;
        quiet)   echo "quiet"; return 0 ;;
    esac

    # Non-interactive or can't reach a real TTY → default verbose so
    # CI / scripted installs keep their existing behavior.
    if ! _welcome_can_prompt_user; then
        echo "verbose"
        return 0
    fi
    if [[ -n "${CI:-}" ]]; then
        echo "verbose"
        return 0
    fi

    local default="1"

    # Print explainer to stderr so stdout stays clean for the return value.
    {
        printf '\n'
        printf '  %sInstall output style%s\n' "$_ANSI_BOLD" "$_ANSI_RESET"
        printf '\n'
        printf '    1. %sVerbose%s — stream every command as it runs\n' "$_ANSI_BOLD" "$_ANSI_RESET"
        printf '                (helpful for first installs and debugging)\n'
        printf '    2. %sQuiet%s   — progress spinners only; tool output saved to a log\n' "$_ANSI_BOLD" "$_ANSI_RESET"
        printf '                (cleaner for repeat / automated installs)\n'
        printf '\n'
    } >&2

    local answer
    if [[ -r /dev/tty ]]; then
        read -rp "  Choose (1-2) [${default}]: " answer </dev/tty
    else
        read -rp "  Choose (1-2) [${default}]: " answer
    fi
    [[ -z "$answer" ]] && answer="$default"
    case "$answer" in
        2|q|Q|quiet)   echo "quiet" ;;
        *)             echo "verbose" ;;
    esac
}
