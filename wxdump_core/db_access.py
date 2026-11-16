"""sqlcipher 访问封装：快照隔离 + 整库解密为明文 SQLite（供标准库 sqlite3 读取）。

设计：sqlcipher CLI 只用来做一件事——把加密库导出成明文库（sqlcipher_export）。
后续所有查询走 Python sqlite3，避开 CLI 文本输出解析的转义问题。
所有操作都先复制快照（含 -wal/-shm），绝不写原库，避免干扰运行中的微信。
明文库缓存在 ~/.wxdump/plain/，源库更新（mtime 变化）时自动重新解密。
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

from .key_derive import load_cached_keys

WORK_DIR = Path.home() / ".wxdump"
SNAPSHOT_ROOT = WORK_DIR / "snapshot"
PLAIN_ROOT = WORK_DIR / "plain"


def require_sqlcipher() -> str:
    bin_path = shutil.which("sqlcipher")
    if not bin_path:
        raise RuntimeError("未安装 sqlcipher: sudo apt install sqlcipher")
    return bin_path


def snapshot_db(db_path: Path) -> Path:
    """复制 db（含 -wal/-shm）到快照目录，返回快照路径。"""
    sub = SNAPSHOT_ROOT / db_path.parent.name
    sub.mkdir(parents=True, exist_ok=True)
    dst = sub / db_path.name
    wal = db_path.with_name(db_path.name + "-wal")
    newest = max(db_path.stat().st_mtime, wal.stat().st_mtime if wal.exists() else 0)
    if not dst.exists() or dst.stat().st_mtime < newest:
        shutil.copy2(db_path, dst)
        for ext in ("-wal", "-shm"):
            src = db_path.with_name(db_path.name + ext)
            target = dst.with_name(dst.name + ext)
            if src.exists():
                shutil.copy2(src, target)
            else:
                target.unlink(missing_ok=True)
        # 快照 mtime 提到源库最新时刻，作为缓存键
        import os
        os.utime(dst, (newest, newest))
    return dst


def cached_rel_path(db_path: Path) -> str:
    """由库绝对路径反推密钥缓存键（相对 db_storage 的路径，或按文件名匹配）。"""
    keys = load_cached_keys()
    parts = db_path.parts
    for i, part in enumerate(parts):
        if part == "db_storage" and i + 1 < len(parts):
            rel = "/".join(parts[i + 1:])
            if rel in keys:
                return rel
    for rel in keys:
        if rel.endswith("/" + db_path.name) or rel == db_path.name:
            return rel
    raise RuntimeError(f"无缓存密钥: {db_path}（先运行 capture-key 派生密钥）")


def get_enc_key(db_path: Path) -> str:
    entry = load_cached_keys().get(cached_rel_path(db_path))
    if not entry:
        raise RuntimeError(f"无缓存密钥: {db_path}")
    return entry["enc_key"]


def ensure_plain(db_path: Path) -> Path:
    """确保该库有最新的明文解密副本，返回明文库路径（~/.wxdump/plain/<组>/<名>.db）。"""
    out = PLAIN_ROOT / db_path.parent.name / db_path.name
    if out.exists() and out.stat().st_mtime >= db_path.stat().st_mtime:
        return out
    snap = snapshot_db(db_path)
    key = get_enc_key(db_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    proc = subprocess.run(
        [require_sqlcipher(), str(snap),
         f'PRAGMA key="x\'{key}\'"; '
         f"ATTACH DATABASE '{out}' AS plain KEY ''; "
         "SELECT sqlcipher_export('plain'); DETACH plain;"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not out.exists():
        out.unlink(missing_ok=True)
        raise RuntimeError(f"解密失败 {db_path.name}: {proc.stderr.strip()[:300]}")
    return out


def open_plain(db_path: Path) -> sqlite3.Connection:
    """解密（或取缓存）并以只读方式打开，返回 sqlite3.Connection。"""
    plain = ensure_plain(db_path)
    conn = sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
    return conn
