#!/bin/bash
# garmin-connector v1 -- build the Lambda package (Python 3.13, arm64) into ./package.
# Works on any Linux or macOS machine with Python 3 and pip, including AWS CloudShell;
# pip downloads prebuilt Linux arm64 wheels, so no Docker is needed.
set -euo pipefail

cd "$(dirname "$0")"
rm -rf package
mkdir package

python3 -m pip install --quiet \
  --target package \
  --platform manylinux2014_aarch64 \
  --platform manylinux_2_28_aarch64 \
  --implementation cp \
  --python-version 3.13 \
  --only-binary=:all: \
  -r src/requirements.txt

cp src/app.py src/run.sh package/
chmod +x package/run.sh
find package -name "__pycache__" -prune -exec rm -rf {} +

echo "Built package/ ($(du -sh package | cut -f1))"
