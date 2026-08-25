#!/usr/bin/env bash
# A股公告看板 - 服务器一键部署脚本（Linux x86_64）
# 用法：bash deploy.sh
set -euo pipefail

cd "$(dirname "$0")"

echo "=== 1/4 检测 Python3 ==="
if ! command -v python3 >/dev/null 2>&1; then
    echo "未找到 python3，请先安装（Ubuntu/Debian: sudo apt install -y python3 python3-venv python3-pip）"
    exit 1
fi
python3 --version

echo "=== 2/4 创建虚拟环境 ==="
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "=== 3/4 安装依赖（仅 pypdf）==="
pip install --upgrade pip >/dev/null
pip install pypdf

echo "=== 4/4 启动服务 ==="
echo ""
echo "启动方式二选一："
echo "  A) 前台运行（调试用）:  bash run_server.sh"
echo "  B) 后台常驻（systemd）: sudo bash install_service.sh"
echo ""
echo "依赖与部署准备完成。默认端口 8765，监听 0.0.0.0。"
