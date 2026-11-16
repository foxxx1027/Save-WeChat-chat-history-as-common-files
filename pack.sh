#!/usr/bin/env bash
# 打包自包含分发包 wxdump-linux-x64.zip（解压后 ./start.sh 即用）
set -euo pipefail
cd "$(dirname "$0")"

OUT="dist/wxdump-linux-x64"
rm -rf "$OUT" dist/wxdump-linux-x64.zip
mkdir -p "$OUT"

# 只拷运行所需文件（排除开发产物与本机导出数据）
cp -r wxdump.py wxdump_core webapp start.sh README.md "$OUT/"
rm -rf "$OUT/wxdump_core/__pycache__"
find "$OUT" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
chmod +x "$OUT/start.sh"

cd dist && zip -qr wxdump-linux-x64.zip wxdump-linux-x64 && cd ..
echo "[+] 打包完成: dist/wxdump-linux-x64.zip ($(du -h dist/wxdump-linux-x64.zip | cut -f1))"
echo "[+] 内容清单:"
unzip -l dist/wxdump-linux-x64.zip | tail -n +4 | head -20
