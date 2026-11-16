"""密钥派生与验证：passphrase → 各数据库 enc_key（SQLCipher4 参数）。

- enc_key = PBKDF2-SHA512(passphrase, db_salt, 256000, 32)
- 验证: mac_salt = salt ⊕ 0x3A; mac_key = PBKDF2-SHA512(enc_key, mac_salt, 2, 32)
  HMAC-SHA512 覆盖 page1[16:4032] + pgno(**小端** 4 字节)，与 page1[4032:4096] 比对
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
from pathlib import Path

PAGE_SZ = 4096
SALT_SZ = 16
RESERVE_SZ = 80  # IV(16) + HMAC(64)
KEY_SZ = 32
KDF_ITER = 256_000

CACHE_DIR = Path.home() / ".wxdump"
KEYS_FILE = CACHE_DIR / "db_keys.json"


def verify_enc_key(enc_key: bytes, page1: bytes) -> bool:
    salt = page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    hm = hmac.new(mac_key, page1[SALT_SZ:PAGE_SZ - RESERVE_SZ + 16], hashlib.sha512)
    hm.update(struct.pack("<I", 1))  # pgno=1 小端（经 sqlcipher 实测确认）
    return hm.digest() == page1[PAGE_SZ - 64:PAGE_SZ]


def collect_db_files(db_dir: Path) -> list[tuple[str, Path, bytes]]:
    """收集 db_dir 下全部加密 .db（排除 -wal/-shm），返回 (相对路径, 绝对路径, page1)。"""
    out = []
    for root, _dirs, files in os.walk(db_dir):
        for name in files:
            if not name.endswith(".db"):
                continue
            path = Path(root) / name
            if path.stat().st_size < PAGE_SZ:
                continue
            with open(path, "rb") as f:
                page1 = f.read(PAGE_SZ)
            out.append((str(path.relative_to(db_dir)), path, page1))
    return out


def derive_keys(passphrase_hex: str, db_dir: Path, progress=None) -> dict[str, dict]:
    """派生并验证所有库的密钥，缓存到 ~/.wxdump/db_keys.json。

    返回 {相对路径: {"enc_key": hex, "salt": hex}}（仅验证通过的）。
    """
    passphrase = bytes.fromhex(passphrase_hex)
    db_files = collect_db_files(db_dir)

    keys: dict[str, dict] = {}
    salt_cache: dict[str, str | None] = {}
    total = len({p1[:SALT_SZ].hex() for _, _, p1 in db_files})
    done = 0
    for rel, _path, page1 in db_files:
        salt_hex = page1[:SALT_SZ].hex()
        if salt_hex not in salt_cache:
            enc_key = hashlib.pbkdf2_hmac("sha512", passphrase, bytes.fromhex(salt_hex), KDF_ITER, dklen=KEY_SZ)
            salt_cache[salt_hex] = enc_key.hex() if verify_enc_key(enc_key, page1) else None
            done += 1
            if progress and (done % 5 == 0 or done == total):
                progress(done, total, len(keys))
        if salt_cache[salt_hex]:
            keys[rel] = {"enc_key": salt_cache[salt_hex], "salt": salt_hex}

    CACHE_DIR.mkdir(exist_ok=True)
    KEYS_FILE.write_text(json.dumps({"_db_dir": str(db_dir), **keys}, ensure_ascii=False, indent=1))
    KEYS_FILE.chmod(0o600)
    return keys


def load_cached_keys() -> dict[str, dict]:
    try:
        data = json.loads(KEYS_FILE.read_text())
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except (OSError, json.JSONDecodeError):
        return {}
