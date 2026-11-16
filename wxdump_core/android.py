"""Android 支持（引擎复用型）：设备扫描、root 拉库、密钥计算/导入、解密。

现实约束（详见 README）：
- 微信 ≤7.x 数据库密钥 = MD5(IMEI + uin) 前 7 位（本模块可自动计算）
- 微信 8.0+ 密钥需 root + Frida hook sqlite3_key 或内存搜索提取后手动导入
- 9.x 加固更强，同 8.0+ 处理方式
- 所有操作均需设备已 root（adb pull /data 需要它）
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from .key_derive import CACHE_DIR

ANDROID_IMPORT_DIR = CACHE_DIR / "android"
WECHAT_PKG = "com.tencent.mm"


def _adb(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    if not shutil.which("adb"):
        raise RuntimeError("未安装 adb（sudo apt install adb 或安装 platform-tools）")
    return subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)


def scan() -> int:
    print("=== Android 设备扫描 ===")
    proc = _adb("devices")
    print(proc.stdout.strip())
    devices = [ln.split("\t")[0] for ln in proc.stdout.splitlines()
               if "\tdevice" in ln]
    if not devices:
        print("[!] 无已连接设备（需开启 USB 调试并授权）")
        return 1
    serial = devices[0]
    ver = _adb("-s", serial, "shell", f"dumpsys package {WECHAT_PKG} | grep versionName").stdout
    print(f"[+] 设备: {serial}")
    print(f"[+] 微信版本: {ver.strip() or '未安装微信'}")
    root = _adb("-s", serial, "shell", "su -c id").stdout
    print(f"[+] root: {'可用' if 'uid=0' in root else '不可用（拉库需要 root）'}")
    return 0


def _su_cat(serial: str, remote: str, local: Path) -> bool:
    """su + base64 流式拉取（adb pull 无法直接访问 /data/data）。"""
    proc = subprocess.run(
        ["adb", "-s", serial, "shell", f"su -c 'base64 {remote}'"],
        capture_output=True, timeout=300,
    )
    if proc.returncode != 0 or not proc.stdout:
        return False
    import base64
    local.write_bytes(base64.b64decode(b"".join(proc.stdout.split())))
    return local.stat().st_size > 0


def pull() -> int:
    print("=== 拉取微信数据库（需 root） ===")
    proc = _adb("devices")
    devices = [ln.split("\t")[0] for ln in proc.stdout.splitlines() if "\tdevice" in ln]
    if not devices:
        print("[!] 无设备连接")
        return 1
    serial = devices[0]
    listing = _adb("-s", serial, "shell",
                   f"su -c 'find /data/data/{WECHAT_PKG}/MicroMsg -name \"*.db\" 2>/dev/null'").stdout
    dbs = [ln.strip() for ln in listing.splitlines() if ln.strip().endswith(".db")]
    if not dbs:
        print("[!] 未找到数据库（root 不可用或微信数据结构变化）")
        return 1
    ANDROID_IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    for remote in dbs:
        name = remote.strip("/").replace("/", "_")
        local = ANDROID_IMPORT_DIR / name
        print(f"  拉取 {remote} ...", end=" ", flush=True)
        if _su_cat(serial, remote, local):
            print(f"OK ({local.stat().st_size // 1024}KB)")
            ok += 1
        else:
            print("失败")
    print(f"[+] 完成 {ok}/{len(dbs)} 个库 → {ANDROID_IMPORT_DIR}")
    print("[*] 下一步: python3 wxdump.py android key --imei ... --uin ... 计算密钥")
    return 0 if ok else 1


def legacy_key(imei: str, uin: str) -> str:
    """微信 ≤7.x: 密钥 = MD5(IMEI + uin) 前 7 位小写。"""
    return hashlib.md5((imei + uin).encode()).hexdigest()[:7]


def key(args) -> int:
    print("=== 密钥计算 / 导入 ===")
    if args.imei and args.uin:
        k = legacy_key(args.imei, args.uin)
        print(f"[+] 老版密钥（≤7.x）: {k}")
        print("[*] 解密: sqlcipher EnMicroMsg.db \"PRAGMA key='" + k +
              "'; PRAGMA cipher_compatibility=1; ATTACH ... \"（见 README Android 章节）")
        return 0
    if args.db_key:
        (ANDROID_IMPORT_DIR).mkdir(parents=True, exist_ok=True)
        (ANDROID_IMPORT_DIR / "imported-key.txt").write_text(args.db_key)
        print("[+] 密钥已导入（8.0+ 的 32 字节 hex 或老版 7 位密钥均可）")
        return 0
    print("[!] 需要 --imei/--uin（老版）或 --db-key <hex>（8.0+ 用 Frida 等工具提取后导入）")
    return 1


def dispatch(args) -> int:
    if args.action == "scan":
        return scan()
    if args.action == "pull":
        return pull()
    if args.action == "key":
        return key(args)
    return 1
