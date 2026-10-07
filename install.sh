#!/bin/sh
set -eu

# fast-delegate installation script for Claude Code, Codex, and Cursor
# Usage: sh install.sh [claude] [codex] [cursor] [--force]
#
# With no runtime named, installs for every runtime detected on this machine.
# A runtime is detected when its CLI is on PATH or its config directory exists.
# Codex and Cursor share ~/.agents/skills, so they get a single copy.

usage() {
    echo "Usage: $0 [claude] [codex] [cursor] [--force]" >&2
    echo "With no runtime named, installs for every runtime detected on this machine." >&2
}

has_cmd() {
    command -v "$1" >/dev/null 2>&1
}

detect_claude() {
    has_cmd claude || [ -n "${CLAUDE_CONFIG_DIR:-}" ] || [ -d "$HOME/.claude" ]
}

detect_codex() {
    has_cmd codex || [ -n "${CODEX_HOME:-}" ] || [ -d "$HOME/.codex" ]
}

detect_cursor() {
    has_cmd cursor-agent || has_cmd cursor || [ -d "$HOME/.cursor" ]
}

target_for() {
    case "$1" in
        claude) echo "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/fast-delegate" ;;
        codex | cursor) echo "$HOME/.agents/skills/fast-delegate" ;;
    esac
}

FORCE=""
RUNTIMES=""
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        -h | --help) usage; exit 0 ;;
        claude | codex | cursor) RUNTIMES="$RUNTIMES $arg" ;;
        *)
            echo "Invalid argument: $arg. Use 'claude', 'codex', 'cursor', or '--force'." >&2
            usage
            exit 1
            ;;
    esac
done

if [ -z "$RUNTIMES" ]; then
    for runtime in claude codex cursor; do
        if "detect_$runtime"; then
            RUNTIMES="$RUNTIMES $runtime"
        fi
    done
    if [ -z "$RUNTIMES" ]; then
        echo "No supported runtime detected (looked for claude, codex, cursor-agent/cursor on PATH" >&2
        echo "and ~/.claude, ~/.codex, ~/.cursor). Name one explicitly:" >&2
        usage
        exit 1
    fi
    echo "Detected runtimes:$RUNTIMES"
fi

# Find the source directory (script's directory)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR/skills/fast-delegate"

if [ ! -d "$SOURCE_DIR" ]; then
    echo "Source directory $SOURCE_DIR not found." >&2
    exit 1
fi

# Resolve unique targets (newline-separated) with the runtimes each serves
TARGETS=""
for runtime in $RUNTIMES; do
    target="$(target_for "$runtime")"
    case "
$TARGETS
" in
        *"
$target
"*) ;;
        *) TARGETS="${TARGETS:+$TARGETS
}$target" ;;
    esac
done

# Check every target before writing anything, so a refusal leaves no partial install
CONFLICT=""
OLD_IFS="$IFS"
IFS='
'
for target in $TARGETS; do
    if [ -d "$target" ] && [ -n "$(find "$target" -type f 2>/dev/null)" ] && [ -z "$FORCE" ]; then
        echo "Target directory $target already exists and is not empty." >&2
        CONFLICT=1
    fi
done
if [ -n "$CONFLICT" ]; then
    echo "Use --force to overwrite." >&2
    exit 1
fi

for target in $TARGETS; do
    rm -rf "$target"
    mkdir -p "$target"
    # Copy skills/fast-delegate to target, excluding __pycache__
    find "$SOURCE_DIR" -type f ! -path "*/__pycache__/*" | while read -r file; do
        relative_path="${file#"$SOURCE_DIR"/}"
        target_file="$target/$relative_path"
        mkdir -p "$(dirname "$target_file")"
        cp "$file" "$target_file"
    done
    echo "Installed fast-delegate to $target"
done
IFS="$OLD_IFS"
