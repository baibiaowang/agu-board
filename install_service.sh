#!/usr/bin/env bash
# A股公告看板 - 安装为 systemd 常驻服务（开机自启、崩溃自动重启）
# 用法：sudo bash install_service.sh
set -euo pipefail
cd "$(dirname "$0")"

APP_DIR="$(pwd)"
PY="$APP_DIR/.venv/bin/python3"
SERVICE_NAME="agu-board"

if [ ! -x "$PY" ]; then
    echo "未找到虚拟环境，请先运行: bash deploy.sh"
    exit 1
fi

# 运行用户：默认当前 sudo 的实际用户（避免 root 权限跑网络抓取）
RUN_USER="${SUDO_USER:-$(whoami)}"

# 访问密码（必改！改成你自己的密码，至少 8 位）
AUTH_PASSWORD="${1:-}"
if [ -z "$AUTH_PASSWORD" ]; then
    echo "用法: sudo bash install_service.sh <访问密码>"
    echo "示例: sudo bash install_service.sh mypassword123"
    exit 1
fi

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=A股公告看板 Web 服务
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}/mainboard_tool
ExecStart=${PY} ${APP_DIR}/mainboard_tool/server.py
Environment=PYTHONIOENCODING=utf-8
Environment=HOST=0.0.0.0
Environment=PORT=8765
Environment=AUTH_PASSWORD=${AUTH_PASSWORD}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo ""
echo "服务已安装并启动。管理命令："
echo "  查看状态: systemctl status ${SERVICE_NAME}"
echo "  查看日志: journalctl -u ${SERVICE_NAME} -f"
echo "  停止服务: systemctl stop ${SERVICE_NAME}"
echo "  卸载服务: systemctl disable --now ${SERVICE_NAME}"
echo ""
echo "访问地址: http://<服务器IP>:8765/"
echo "访问密码: 浏览器会弹登录框，用户名随便填，密码填你刚设置的"
