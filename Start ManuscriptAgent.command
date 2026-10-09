#!/bin/sh
cd "$(dirname "$0")" || exit 1
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH
for manuscript_python in "$HOME/.local/bin/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 python3.14 python3.13 python3.12 python3.11; do
    if "$manuscript_python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        exec "$manuscript_python" gui.py --language en "$@"
    fi
done
echo 'ManuscriptAgent requires Python 3.11 or newer. Press Return to close.'
read manuscript_answer
