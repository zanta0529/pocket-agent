#!/bin/sh
# Executing PocketAgent-cli.exe (Linux/macOS)
DIR="$(cd "$(dirname "$0")" && pwd)"
"$DIR/PocketAgent-cli.exe" "$@"
