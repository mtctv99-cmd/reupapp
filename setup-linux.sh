#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

if [[ "$(uname -s)" != Linux ]]; then
  echo "Run this script on Ubuntu/Linux, not Windows."
  exit 1
fi

sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv python3-dev python3-pip python3-gi python3-gi-cairo \
  gir1.2-gtk-3.0 gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0 xvfb xauth \
  gobject-introspection libgirepository1.0-dev \
  ffmpeg libsndfile1 libgomp1 libgl1 libglib2.0-0 libsm6 libxrender1 libxext6 \
  libcairo2-dev pkg-config

if [[ ! -x .venv-linux/bin/python ]]; then
  python3 -m venv --system-site-packages .venv-linux
fi

.venv-linux/bin/python -m pip install --upgrade pip setuptools wheel
.venv-linux/bin/python -m pip install PyGObject pyinstaller
.venv-linux/bin/python -m pip install -r requirements.txt
.venv-linux/bin/python -m playwright install chromium
.venv-linux/bin/python -m playwright install-deps chromium

.venv-linux/bin/python - <<'PY'
import fastapi, funasr, gi, kaldi_native_fbank, onnxruntime, piper, playwright, uvicorn, webview
from gi.repository import Gtk, WebKit2
print("Linux environment OK", Gtk.get_major_version(), WebKit2.get_major_version())
PY

# Copy the local voice into app data so source and frozen layouts both resolve it.
mkdir -p "$HOME/.config/sever-desktop/models/piper"
cp -a models/piper/ngoc_huyen "$HOME/.config/sever-desktop/models/piper/"

echo "Setup complete. Run ./run-linux.sh"
