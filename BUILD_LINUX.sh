#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[[ -x .venv-linux/bin/python ]] || { echo "Run ./setup-linux.sh first"; exit 1; }
[[ -f models/piper/ngoc_huyen/ngoc_huyen.onnx ]] || { echo "Missing Piper voice model"; exit 1; }
[[ -f models/piper/ngoc_huyen/ngoc_huyen.onnx.json ]] || { echo "Missing Piper voice config"; exit 1; }

export SEVER_BUILD_OS=linux
export BUILD_LITE=1
export NO_BIN=1
export SEVER_DESKTOP_DIR="$PWD/.build-linux-config"
mkdir -p "$SEVER_DESKTOP_DIR/models_cache/models"
if [[ -d "$HOME/.config/sever-desktop/models_cache/models" ]]; then
  cp -a "$HOME/.config/sever-desktop/models_cache/models/." "$SEVER_DESKTOP_DIR/models_cache/models/"
fi
if [[ ! -d "$SEVER_DESKTOP_DIR/models_cache/models" || -z "$(ls -A "$SEVER_DESKTOP_DIR/models_cache/models")" ]]; then
  echo "STT models missing; run ./run-linux.sh once and complete model download before full build."
  exit 1
fi
.venv-linux/bin/python -m PyInstaller SeverDubStudioPro.spec --clean --noconfirm
chmod +x dist/SeverDubStudioPro/SeverDubStudioPro
cp setup-linux.sh run-linux.sh requirements.txt dist/SeverDubStudioPro/
cp -a core pipeline providers hooks src dist/SeverDubStudioPro/
mkdir -p dist/SeverDubStudioPro/models/piper
cp -a models/piper/ngoc_huyen dist/SeverDubStudioPro/models/piper/
cp app.py run.py settings.example.json dist/SeverDubStudioPro/
cp -a .build-linux-config/models_cache dist/SeverDubStudioPro/
chmod +x dist/SeverDubStudioPro/setup-linux.sh dist/SeverDubStudioPro/run-linux.sh

echo "Linux bundle built at dist/SeverDubStudioPro/"
