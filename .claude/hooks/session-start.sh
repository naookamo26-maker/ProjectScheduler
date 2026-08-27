#!/bin/bash
set -euo pipefail

# Claude Code on the web専用（ローカル開発では実行しない）
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# tests/ の "gui" マーカー（PySide6）を動かすにはQtのシステムライブラリが要る。
# 無いと `libEGL.so.1: cannot open shared object file` でimportが落ちる。
# 参照: CLAUDE.md「テストの実行範囲」
if ! ldconfig -p 2>/dev/null | grep -q libEGL.so.1; then
  apt-get update -qq
  apt-get install -y -qq libegl1 libgl1 libxkbcommon0 libdbus-1-3
fi

pip install -q -r requirements.txt pytest
