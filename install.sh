#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo 'Run this installer as root (sudo -i).'; exit 1
fi
command -v python3 >/dev/null || { echo 'Python 3 is required.'; exit 1; }
command -v curl >/dev/null || { echo 'Install curl first: apt-get install curl'; exit 1; }
umask 077
work=$(mktemp -d /tmp/farstar-installer.XXXXXXXX)
trap 'rm -f -- "$work/install.py"; rmdir -- "$work" 2>/dev/null || true' EXIT
curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/farstar-team/FARSTAR-ROBOT/main/scripts/install.py -o "$work/install.py"
python3 "$work/install.py" </dev/tty
