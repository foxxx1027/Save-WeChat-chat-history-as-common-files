#!/usr/bin/env bash
# wxdump 一键启动 —— 在本人电脑上导出本人微信聊天记录
# 用法: ./start.sh [端口]     （默认 8765，浏览器会自动打开）
set -u
cd "$(dirname "$0")"

say()  { printf '\033[36m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m[OK]\033[0m %s\n' "$*"; }
bad()  { printf '\033[31m[!!]\033[0m %s\n' "$*"; }

echo "──────────────────────────────────────────────"
echo "  wxdump 一键启动（仅限本人设备 / 本人微信账号）"
echo "──────────────────────────────────────────────"

# 1) Python ≥ 3.10
if ! command -v python3 >/dev/null 2>&1; then
    bad "未安装 python3，请先: sudo apt install python3"
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    bad "需要 Python 3.10+（当前 $(python3 -V 2>&1)）"
    exit 1
fi
ok "Python $(python3 -V 2>&1 | cut -d' ' -f2)"

# 2) 系统依赖 sqlcipher / gdb（缺则自动尝试安装）
MISSING=()
command -v sqlcipher >/dev/null 2>&1 || MISSING+=(sqlcipher)
command -v gdb       >/dev/null 2>&1 || MISSING+=(gdb)
if [ "${#MISSING[@]}" -gt 0 ]; then
    say "缺少依赖: ${MISSING[*]}，尝试自动安装（可能要求输入你的开机密码）"
    if sudo apt install -y "${MISSING[@]}"; then
        ok "依赖安装完成"
    else
        bad "自动安装失败，请手动执行: sudo apt install ${MISSING[*]}"
        exit 1
    fi
else
    ok "sqlcipher / gdb 已就绪"
fi

# 3) ptrace_scope（抓密钥需要，仅在当前为 1 时处理）
SCOPE=$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo 0)
if [ "$SCOPE" != "0" ]; then
    say "需要临时放开调试附加权限（抓密钥用；结束后会提醒你恢复）"
    if sudo sysctl kernel.yama.ptrace_scope=0; then
        OPENED_PTRACE=1
        ok "ptrace_scope 已放开"
    else
        bad "失败，请手动执行: sudo sysctl kernel.yama.ptrace_scope=0 后重跑本脚本"
        exit 1
    fi
else
    ok "调试附加权限就绪"
fi

# 4) 微信需在运行
if ! pgrep -x wechat >/dev/null 2>&1 && ! pgrep -x weixin >/dev/null 2>&1 \
   && ! pgrep -f '/opt/wechat/wechat|/usr/bin/wechat' >/dev/null 2>&1; then
    bad "微信未运行。请先启动微信并登录你的账号，再重新运行本脚本"
    exit 1
fi
ok "微信运行中"

# 5) 启动
PORT="${1:-8765}"
URL="http://127.0.0.1:${PORT}"
cat <<TIP

接下来（整个流程约 2 分钟）:
  1. 浏览器打开后，若显示「开始抓取密钥」按钮，点击它
  2. 到微信里: 左下角菜单 → 退出登录（回到扫码界面，不是退出微信程序）
  3. 重新扫码登录你的账号
  4. 回到浏览器: 搜索并勾选要导出的联系人 → 勾选格式 → 点「导出所选」
  5. 完成后点击页面上的下载链接保存文件

TIP
say "启动界面: ${URL}（浏览器将自动打开；此窗口不要关，Ctrl+C 退出）"
( sleep 1.5; xdg-open "${URL}" >/dev/null 2>&1 || true ) &
python3 wxdump.py ui --port "${PORT}"
RC=$?

if [ "${OPENED_PTRACE:-0}" = "1" ]; then
    say "导出完成后建议恢复安全设置: sudo sysctl kernel.yama.ptrace_scope=1"
fi
exit $RC
