"""高层 API：账号发现、联系人列表、消息合并读取。

使用方式：
    store = ChatStore()                     # 自动发现账号与缓存密钥
    store.ensure_ready()                    # 无密钥时抛错并提示
    contacts = store.list_contacts()        # [{wxid, name, remark, kind, msg_count}...]
    msgs = store.get_messages(wxid)         -> list[ChatMessage]
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from hashlib import md5
from pathlib import Path

from .db_access import open_plain
from .key_capture import load_cached_passphrase
from .key_derive import derive_keys, load_cached_keys
from .msg_render import decompress_content

DEFAULT_ROOTS = [
    Path.home() / "文档" / "xwechat_files",
    Path.home() / ".xwechat" / "xwechat_files",
    Path.home() / ".local/share/xwechat_files",
]

KIND_NAMES = {"friend": "好友", "chatroom": "群聊", "official": "公众号", "other": "其他"}


@dataclass
class Account:
    root: Path          # xwechat_files 根目录
    user_dir: Path      # wxid_xxx_c8b3 目录
    wxid: str

    @property
    def db_dir(self) -> Path:
        return self.user_dir / "db_storage"

    @property
    def name(self) -> str:
        return self.user_dir.name


@dataclass
class ChatMessage:
    timestamp: int
    sort_seq: int
    server_id: int
    msg_type: int
    sender: str         # 原始 wxid
    content: bytes | str | None  # 解压后的原始内容
    type_name: str      # 渲染后的人类可读类型名
    text: str           # 渲染后的可读文本
    time_str: str

    def to_dict(self) -> dict:
        return {
            "time": self.time_str, "timestamp": self.timestamp,
            "sender": self.sender, "type": self.type_name,
            "type_code": self.msg_type, "content": self.text,
        }


def discover_accounts(custom_root: Path | None = None) -> list[Account]:
    roots = [custom_root] if custom_root else DEFAULT_ROOTS
    accounts = []
    for root in roots:
        if not root.is_dir():
            continue
        for sub in sorted(root.iterdir()):
            if sub.is_dir() and (sub / "db_storage").is_dir() and sub.name.startswith("wxid_"):
                accounts.append(Account(root=root, user_dir=sub, wxid=sub.name.split("_c")[0]))
    return accounts


class ChatStore:
    def __init__(self, account: Account | None = None, custom_root: Path | None = None):
        self.accounts = discover_accounts(custom_root)
        if not self.accounts:
            raise RuntimeError(
                "未找到微信数据目录（xwechat_files）。请确认微信 4.x 已在本机登录过；"
                "非默认位置请用 --root 指定"
            )
        self.account = account or self.accounts[0]
        self._contact_cache: list[dict] | None = None

    # ---------- 就绪状态 ----------

    def status(self) -> dict:
        info = {
            "account": self.account.name,
            "db_dir": str(self.account.db_dir),
            "accounts_found": len(self.accounts),
            "passphrase_cached": load_cached_passphrase() is not None,
            "keys_cached": len(load_cached_keys()),
        }
        return info

    def ensure_ready(self, progress=None) -> None:
        """确保有各库密钥；passphrase 有缓存但未派生时自动派生。"""
        if load_cached_keys():
            return
        passphrase = load_cached_passphrase()
        if not passphrase:
            raise RuntimeError(
                "尚无密钥。先运行: python3 wxdump.py capture-key（需在微信中退出账号重新登录一次）"
            )
        derive_keys(passphrase, self.account.db_dir, progress)

    # ---------- 联系人 ----------

    def list_contacts(self, refresh: bool = False) -> list[dict]:
        """联系人 + 各库消息计数，按消息数降序。"""
        if self._contact_cache is not None and not refresh:
            return self._contact_cache

        conn = open_plain(self.account.db_dir / "contact" / "contact.db")
        rows = conn.execute(
            "SELECT username, nick_name, remark, local_type FROM contact "
            "WHERE delete_flag = 0"
        ).fetchall()
        conn.close()

        msg_counts = self._message_counts()
        contacts = []
        for username, nick, remark, _local_type in rows:
            if username.startswith("wxid_") and remark is None and nick in (None, username):
                continue  # 跳过空壳
            if username == "weixin" or username.startswith(("fake_", "weibo")):
                continue
            kind = ("chatroom" if username.endswith("@chatroom")
                    else "official" if username.startswith("gh_")
                    else "friend")
            contacts.append({
                "wxid": username,
                "name": remark or nick or username,
                "nick_name": nick or "",
                "remark": remark or "",
                "kind": kind,
                "kind_name": KIND_NAMES[kind],
                "msg_count": msg_counts.get(username, 0),
            })
        contacts.sort(key=lambda c: -c["msg_count"])
        self._contact_cache = contacts
        return contacts

    def _message_dbs(self) -> list[Path]:
        db_dir = self.account.db_dir
        dbs = [db_dir / "message" / f"message_{i}.db" for i in range(4)]
        return [p for p in dbs if p.exists()]

    def _msg_table(self, wxid: str) -> str:
        return "Msg_" + md5(wxid.encode()).hexdigest()

    def _message_counts(self) -> dict[str, int]:
        """Name2Id 反查每个会话 wxid 的消息总数（三库求和）。"""
        counts: dict[str, int] = {}
        for db in self._message_dbs():
            conn = open_plain(db)
            try:
                tables = {
                    r[0] for r in
                    conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")
                }
                for rowid, user_name in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                    table = self._msg_table(user_name)
                    if table in tables:
                        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        counts[user_name] = counts.get(user_name, 0) + n
            finally:
                conn.close()
        return counts

    # ---------- 消息 ----------

    def get_messages(self, wxid: str, self_wxid: str | None = None,
                     start: int | None = None, end: int | None = None) -> list[ChatMessage]:
        """合并三个 message 库读取与某联系人的全部消息，去重、按时间升序。"""
        from .msg_render import render_message  # 延迟导入避免环

        table = self._msg_table(wxid)
        raw: list[tuple] = []
        for db in self._message_dbs():
            conn = open_plain(db)
            try:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    continue
                names = dict(conn.execute("SELECT rowid, user_name FROM Name2Id"))
                rows = conn.execute(
                    f"SELECT local_type, sort_seq, real_sender_id, create_time, server_id, "
                    f"message_content, WCDB_CT_message_content FROM {table}"
                ).fetchall()
                for lt_raw, seq, sender_id, ts, sid, content, ct in rows:
                    sender = names.get(sender_id, f"id:{sender_id}")
                    raw.append((sender, lt_raw, seq, ts, sid, content, ct))
            finally:
                conn.close()

        # 去重 + 排序（server_id 优先；无 server_id 用组合键）
        seen, merged = set(), []
        for sender, lt_raw, seq, ts, sid, content, ct in raw:
            msg_type = lt_raw & 0xFFFFFFFF
            if start and ts < start:
                continue
            if end and ts > end:
                continue
            content = decompress_content(content, ct)
            key = ("sid", sid) if sid else ("f", ts, seq, msg_type, hash(content))
            if key in seen:
                continue
            seen.add(key)
            merged.append((ts, seq or 0, sid, msg_type, sender, content))

        messages: list[ChatMessage] = []
        for ts, seq, sid, msg_type, sender, content in sorted(merged, key=lambda m: (m[0], m[1])):
            content_str = (content.decode("utf-8", errors="replace")
                           if isinstance(content, bytes) else (content or ""))
            type_name, text = render_message(msg_type, content_str)
            messages.append(ChatMessage(
                timestamp=ts, sort_seq=seq, server_id=sid, msg_type=msg_type,
                sender=sender, content=content_str, type_name=type_name, text=text,
                time_str=datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            ))
        return messages

    def self_wxid(self) -> str:
        return self.account.wxid

    def contact_display_name(self, wxid: str) -> str:
        for c in self.list_contacts():
            if c["wxid"] == wxid:
                return c["name"]
        return wxid
