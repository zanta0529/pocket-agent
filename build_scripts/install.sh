#!/bin/sh
# PocketAgent POSIX Installer for Linux & macOS
# Usage: curl -fsSL https://raw.githubusercontent.com/zanta0529/pocket-agent/main/build_scripts/install.sh | sh
set -eu

REPO="${POCKET_AGENT_REPO:-zanta0529/pocket-agent}"
VERSION="${POCKET_AGENT_VERSION:-latest}"
INSTALL_DIR="${POCKET_AGENT_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/pocket-agent}"
BIN_DIR="${POCKET_AGENT_BIN_DIR:-$HOME/.local/bin}"
UNINSTALL="${POCKET_AGENT_UNINSTALL:-0}"

show_help() {
    cat <<'EOF'
PocketAgent Installer (Linux / macOS)

Options:
  --version <tag>   Install a specific release tag or branch (default: latest)
  --dir <path>      Install directory (default: ~/.local/share/pocket-agent)
  --bin-dir <path>  Launcher bin directory (default: ~/.local/bin)
  --uninstall       Remove installed PocketAgent files (preserves ~/.pocket-agent)
  -h, --help        Show this help message
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        --version)
            [ "$#" -ge 2 ] || { echo "[ERROR] --version requires a value." >&2; exit 1; }
            VERSION="$2"
            shift 2
            ;;
        --dir)
            [ "$#" -ge 2 ] || { echo "[ERROR] --dir requires a value." >&2; exit 1; }
            INSTALL_DIR="$2"
            shift 2
            ;;
        --bin-dir)
            [ "$#" -ge 2 ] || { echo "[ERROR] --bin-dir requires a value." >&2; exit 1; }
            BIN_DIR="$2"
            shift 2
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

case "$REPO" in
    *[!A-Za-z0-9_.-/]*)
        echo "[ERROR] Invalid POCKET_AGENT_REPO: $REPO" >&2
        exit 1
        ;;
    */*) ;;
    *)
        echo "[ERROR] POCKET_AGENT_REPO must be in owner/repo format: $REPO" >&2
        exit 1
        ;;
esac

SHORT_LAUNCHER_PATH="$BIN_DIR/pocket"
LAUNCHER_PATH="$BIN_DIR/pocket-agent"

if [ "$UNINSTALL" = "1" ]; then
    for target_launcher in "$SHORT_LAUNCHER_PATH" "$LAUNCHER_PATH"; do
        if [ -f "$target_launcher" ] && grep -q "POCKET_AGENT_INSTALLER_MANAGED" "$target_launcher" 2>/dev/null; then
            rm -f "$target_launcher"
            echo "[INFO] Removed launcher $target_launcher"
        fi
    done
    if [ -d "$INSTALL_DIR" ]; then
        if [ -f "$INSTALL_DIR/app/PORTABLE" ] || [ -d "$INSTALL_DIR/app/src/pocket_agent" ]; then
            rm -rf "$INSTALL_DIR"
            echo "[OK] Uninstalled PocketAgent from $INSTALL_DIR"
        else
            echo "[ERROR] Refusing to remove $INSTALL_DIR: not a PocketAgent installation." >&2
            exit 1
        fi
    else
        echo "[INFO] PocketAgent is not installed at $INSTALL_DIR"
    fi
    echo "[INFO] User data in $HOME/.pocket-agent was preserved."
    exit 0
fi

for cmd in curl tar; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "[ERROR] Required command '$cmd' was not found in PATH." >&2
        exit 1
    fi
done

find_python314() {
    for candidate in python3.14 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c "import sys, venv; raise SystemExit(0 if sys.version_info >= (3, 14) else 1)" >/dev/null 2>&1; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

USE_UV=0
SYS_PY=""
if command -v uv >/dev/null 2>&1; then
    USE_UV=1
elif SYS_PY="$(find_python314)"; then
    USE_UV=0
else
    echo "[ERROR] Python 3.14+ (with venv) or 'uv' is required." >&2
    echo "Install uv:    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "Or Python 3.14 via Homebrew (brew install python@3.14) / OS package manager." >&2
    exit 1
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pocket-agent-install.XXXXXX")"
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

if [ "$VERSION" = "latest" ]; then
    EFFECTIVE_URL="$(curl -fsSL -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest" || true)"
    case "$EFFECTIVE_URL" in
        */releases/tag/*)
            TAG="${EFFECTIVE_URL##*/releases/tag/}"
            ARCHIVE_URL="https://github.com/$REPO/archive/refs/tags/$TAG.tar.gz"
            DISPLAY_VER="$TAG"
            ;;
        *)
            echo "[WARN] Could not resolve latest release tag; falling back to main branch archive."
            ARCHIVE_URL="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
            DISPLAY_VER="main"
            ;;
    esac
else
    ARCHIVE_URL="https://github.com/$REPO/archive/refs/tags/$VERSION.tar.gz"
    DISPLAY_VER="$VERSION"
fi

ARCHIVE_FILE="$TMP_DIR/pocket-agent.tar.gz"
EXTRACT_DIR="$TMP_DIR/extract"
mkdir -p "$EXTRACT_DIR"

echo "[INFO] Downloading PocketAgent ($DISPLAY_VER) from $REPO..."
curl -fsSL "$ARCHIVE_URL" -o "$ARCHIVE_FILE"

echo "[INFO] Extracting source archive..."
tar -xzf "$ARCHIVE_FILE" -C "$EXTRACT_DIR"

SRC_ROOT=""
for d in "$EXTRACT_DIR"/*; do
    if [ -d "$d/src/pocket_agent" ] && [ -f "$d/requirements.txt" ]; then
        SRC_ROOT="$d"
        break
    fi
done
if [ -z "$SRC_ROOT" ]; then
    echo "[ERROR] Downloaded archive does not contain a valid PocketAgent source tree." >&2
    exit 1
fi

STAGE_DIR="$TMP_DIR/stage"
STAGE_APP="$STAGE_DIR/app"
STAGE_VENV="$STAGE_DIR/venv"
mkdir -p "$STAGE_APP"

for item in src mcp_servers subagents mcp_config.example.yaml .env.example requirements.txt; do
    if [ -e "$SRC_ROOT/$item" ]; then
        cp -R "$SRC_ROOT/$item" "$STAGE_APP/$item"
    fi
done

cat > "$STAGE_APP/PORTABLE" <<'EOF'
Packaged installer marker so paths.py stores config and logs in ~/.pocket-agent.
EOF

echo "[INFO] Creating isolated Python 3.14+ environment and installing dependencies..."
if [ "$USE_UV" = "1" ]; then
    uv venv --python ">=3.14" "$STAGE_VENV"
    uv pip install --python "$STAGE_VENV/bin/python" -r "$STAGE_APP/requirements.txt"
else
    "$SYS_PY" -m venv "$STAGE_VENV"
    "$STAGE_VENV/bin/python" -m pip install --upgrade pip
    "$STAGE_VENV/bin/python" -m pip install -r "$STAGE_APP/requirements.txt"
fi

PARENT_DIR="$(dirname "$INSTALL_DIR")"
mkdir -p "$PARENT_DIR" "$BIN_DIR"

BACKUP_DIR="${INSTALL_DIR}.bak.$$"
MOVED_BACKUP=0
if [ -e "$INSTALL_DIR" ]; then
    if [ ! -f "$INSTALL_DIR/app/PORTABLE" ] && [ ! -d "$INSTALL_DIR/app/src/pocket_agent" ]; then
        echo "[ERROR] Target $INSTALL_DIR exists and is not a PocketAgent directory." >&2
        exit 1
    fi
    mv "$INSTALL_DIR" "$BACKUP_DIR"
    MOVED_BACKUP=1
fi

if ! mv "$STAGE_DIR" "$INSTALL_DIR"; then
    if [ "$MOVED_BACKUP" = "1" ] && [ -d "$BACKUP_DIR" ] && [ ! -e "$INSTALL_DIR" ]; then
        mv "$BACKUP_DIR" "$INSTALL_DIR" || true
    fi
    echo "[ERROR] Failed to finalize installation in $INSTALL_DIR" >&2
    exit 1
fi

if [ "$MOVED_BACKUP" = "1" ] && [ -d "$BACKUP_DIR" ]; then
    rm -rf "$BACKUP_DIR"
fi

write_launcher() {
    cat > "$1" <<EOF
#!/bin/sh
# POCKET_AGENT_INSTALLER_MANAGED
set -eu
export PYTHONNOUSERSITE=1
export PYTHONPATH="$INSTALL_DIR/app/src"
exec "$INSTALL_DIR/venv/bin/python" -m pocket_agent "\$@"
EOF
    chmod +x "$1"
}

write_launcher "$LAUNCHER_PATH"

PRIMARY_CMD="pocket"
if [ -e "$SHORT_LAUNCHER_PATH" ] && { [ ! -f "$SHORT_LAUNCHER_PATH" ] || ! grep -q "POCKET_AGENT_INSTALLER_MANAGED" "$SHORT_LAUNCHER_PATH" 2>/dev/null; }; then
    echo "[WARN] $SHORT_LAUNCHER_PATH already exists and is not managed by PocketAgent; skipping short alias."
    PRIMARY_CMD="pocket-agent"
else
    write_launcher "$SHORT_LAUNCHER_PATH"
fi

echo ""
echo "[OK] PocketAgent ($DISPLAY_VER) installed to $INSTALL_DIR"
if [ "$PRIMARY_CMD" = "pocket" ]; then
    echo "Launchers: $SHORT_LAUNCHER_PATH (and $LAUNCHER_PATH)"
else
    echo "Launcher: $LAUNCHER_PATH"
fi

case ":${PATH:-}:" in
    *":$BIN_DIR:"*)
        echo "Next steps:"
        echo "  1. $PRIMARY_CMD setup"
        echo "  2. $PRIMARY_CMD"
        if [ "$PRIMARY_CMD" = "pocket" ]; then
            echo "  (Alias 'pocket-agent' is also available.)"
        fi
        ;;
    *)
        echo ""
        echo "[WARN] $BIN_DIR is not currently in your PATH."
        echo "Add it to your shell profile (~/.zshrc or ~/.bashrc):"
        echo "  export PATH=\"$BIN_DIR:\$PATH\""
        echo "Then run: $PRIMARY_CMD setup"
        ;;
esac
