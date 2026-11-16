"""gdb 断点抓取微信 4.1+ 数据库 passphrase。

前提：微信运行中、gdb 已装、ptrace_scope=0（或 root）。
触发方式：微信「退出账号 → 重新登录」（4.1+ 仅登录瞬间调用 Cipher 配置函数）。
抓到的 32 字节 passphrase 经 PBKDF2 派生才是各库实际密钥（见 key_derive）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .elf_analyzer import find_runtime_base, select_hook_candidate

CACHE_DIR = Path.home() / ".wxdump"
PASSPHRASE_FILE = CACHE_DIR / "passphrase.json"

GDB_SCRIPT_TEMPLATE = """\
set pagination off
attach {pid}
python
import gdb

class CaptureBreakpoint(gdb.Breakpoint):
    def stop(self):
        try:
            rsi = int(gdb.parse_and_eval("$rsi"))
            rdx = int(gdb.parse_and_eval("$rdx"))
            if rsi and rdx == 32:
                raw = gdb.selected_inferior().read_memory(rsi, 32).tobytes()
                print("WXDUMP_PASSPHRASE=" + raw.hex())
                gdb.execute("detach")
                gdb.execute("quit")
                return True
            if rsi:
                try:
                    size_val = int(gdb.parse_and_eval("*(unsigned long long*)($rsi+16)"))
                    if size_val == 32:
                        key_ptr = int(gdb.parse_and_eval("*(unsigned long long*)($rsi+8)"))
                        if key_ptr:
                            raw = gdb.selected_inferior().read_memory(key_ptr, 32).tobytes()
                            print("WXDUMP_PASSPHRASE=" + raw.hex())
                            gdb.execute("detach")
                            gdb.execute("quit")
                            return True
                except Exception:
                    pass
        except Exception as e:
            print("CAPTURE_ERROR=" + str(e))
        return False

CaptureBreakpoint("*{addr:#x}")
end
continue
quit
"""


class CaptureError(RuntimeError):
    pass


def find_wechat_pid() -> int:
    """查找微信主进程 PID（/proc 扫描，不依赖 pgrep）。"""
    for pid_str in os.listdir("/proc"):
        if not pid_str.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{pid_str}/exe")
            if exe.endswith("/wechat") or exe.endswith("/weixin"):
                return int(pid_str)
        except OSError:
            continue
    raise CaptureError("微信未运行，请先启动微信并登录")


def resolve_wechat_binary(pid: int) -> Path:
    try:
        return Path(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        for candidate in ("/opt/wechat/wechat", "/usr/bin/wechat"):
            if Path(candidate).exists():
                return Path(candidate)
    raise CaptureError("未找到微信二进制")


def check_prerequisites() -> list[str]:
    """环境预检，返回问题列表（空=就绪）。"""
    issues = []
    if not shutil.which("gdb"):
        issues.append("未安装 gdb: sudo apt install gdb")
    try:
        scope = int(Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip())
        if scope > 0 and os.geteuid() != 0:
            issues.append(
                f"ptrace_scope={scope}，需要: sudo sysctl kernel.yama.ptrace_scope=0（导出完可改回 1）"
            )
    except (OSError, ValueError):
        pass
    return issues


def load_cached_passphrase() -> str | None:
    try:
        return json.loads(PASSPHRASE_FILE.read_text()).get("passphrase")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def save_passphrase(passphrase_hex: str) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    PASSPHRASE_FILE.write_text(json.dumps({"passphrase": passphrase_hex}))
    PASSPHRASE_FILE.chmod(0o600)


def capture_passphrase(pid: int | None = None, timeout: int = 300) -> str:
    """gdb 附加 + 断点等待登录事件，返回 64 位 hex passphrase。"""
    issues = check_prerequisites()
    if issues:
        raise CaptureError("；".join(issues))
    if pid is None:
        pid = find_wechat_pid()
    binary = resolve_wechat_binary(pid)

    candidate = select_hook_candidate(binary)
    base = find_runtime_base(pid, binary)
    addr = base + candidate.target_va

    with tempfile.TemporaryDirectory(prefix="wxdump-gdb-") as tmp:
        script = Path(tmp) / "capture.gdb"
        script.write_text(GDB_SCRIPT_TEMPLATE.format(pid=pid, addr=addr))
        try:
            proc = subprocess.run(
                [shutil.which("gdb"), "-q", "--nx", "-batch", "-x", str(script)],
                capture_output=True, text=True, timeout=timeout,
            )
            combined = proc.stdout + "\n" + proc.stderr
        except subprocess.TimeoutExpired:
            subprocess.run(["killall", "-9", "gdb"], capture_output=True)
            raise CaptureError(
                f"等待 {timeout} 秒超时：断点未触发。请确保执行了「微信退出账号 → 重新登录」"
                "（是退出账号，不是退出微信程序）"
            ) from None

    if "ptrace" in combined.lower() or "operation not permitted" in combined.lower():
        raise CaptureError(
            "gdb 无法附加微信进程。执行: sudo sysctl kernel.yama.ptrace_scope=0 后重试"
        )
    match = re.search(r"WXDUMP_PASSPHRASE=([0-9a-fA-F]{64})", combined)
    if match:
        passphrase = match.group(1).lower()
        save_passphrase(passphrase)
        return passphrase
    err = re.search(r"CAPTURE_ERROR=(.*)", combined)
    raise CaptureError(
        f"捕获失败: {err.group(1) if err else '断点未产出密钥'}。gdb 输出尾部:\n"
        + "\n".join(combined.strip().splitlines()[-10:])
    )
