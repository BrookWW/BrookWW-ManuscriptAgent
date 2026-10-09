#!/bin/sh
cd "$(dirname "$0")" || exit 1
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH
for manuscript_python in "$HOME/.local/bin/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 python3.14 python3.13 python3.12 python3.11; do
    if "$manuscript_python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        exec "$manuscript_python" gui.py --language zh-CN "$@"
    fi
done
echo 'ManuscriptAgent 需要 Python 3.11 或更新版本。按回车关闭。'
read manuscript_answer
