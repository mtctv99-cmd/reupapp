#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

docker build -f Dockerfile.linux -t sever-dub-linux-build .
docker run --rm \
  -e SEVER_BUILD_OS=linux \
  -e BUILD_LITE=1 \
  -e NO_BIN=1 \
  -e SEVER_DESKTOP_DIR=/tmp/sever-desktop-build \
  -e HOME=/tmp/sever-linux-home \
  -v "$PWD:/src" \
  sever-dub-linux-build \
  bash -lc '
    mkdir -p "$SEVER_DESKTOP_DIR/models_cache/models"
    cp -a "$HOME/.config/sever-desktop/models_cache/models/." "$SEVER_DESKTOP_DIR/models_cache/models/" 2>/dev/null || true
    for model in \
      iic--speech_fsmn_vad_zh-cn-16k-common-pytorch \
      iic--punc_ct-transformer_cn-en-common-vocab471067-large \
      iic--speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch; do
      if [[ ! -d "$SEVER_DESKTOP_DIR/models_cache/models/$model/snapshots/master" ]]; then
        echo "Missing STT model: $model; Linux model cache must be provisioned before build."
        exit 1
      fi
    done
    python -m PyInstaller SeverDubStudioPro.spec --clean --noconfirm
    chmod +x dist/SeverDubStudioPro/SeverDubStudioPro
    cp -a core pipeline providers hooks src dist/SeverDubStudioPro/
    mkdir -p dist/SeverDubStudioPro/models/piper
    cp -a models/piper/ngoc_huyen dist/SeverDubStudioPro/models/piper/
    cp app.py run.py requirements.txt settings.example.json setup-linux.sh run-linux.sh dist/SeverDubStudioPro/
    cp -a "$SEVER_DESKTOP_DIR/models_cache" dist/SeverDubStudioPro/
    chmod +x dist/SeverDubStudioPro/setup-linux.sh dist/SeverDubStudioPro/run-linux.sh
  '

echo "Linux bundle: dist/SeverDubStudioPro/"
