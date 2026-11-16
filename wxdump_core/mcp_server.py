"""MCP server（stdio, newline-delimited JSON-RPC 2.0）。

把 wxdump 能力暴露为 MCP 工具，供 AI 客户端（ZCode / Claude Desktop 等）调用：
    wx_status        — 环境体检（密钥/账号/解锁状态）
    wx_list_contacts — 联系人列表（名称/类型/消息数）
    wx_export_chats  — 导出聊天记录（联系人列表或 all + 格式列表）

注册示例（客户端 mcp 配置）:
    {"command": "python3", "args": ["/path/to/wxdump/wxdump.py", "mcp"]}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROTOCOL = "2024-11-05"

TOOLS = [
    {
        "name": "wx_status",
        "description": "检查微信聊天记录导出环境：微信账号发现、密钥缓存、已解锁数据库数量、前置条件问题",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wx_list_contacts",
        "description": "列出微信联系人及每人消息数（默认仅显示有聊天记录的），可按类型过滤",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["friend", "chatroom", "official"],
                         "description": "联系人类型过滤"},
                "min_messages": {"type": "integer", "description": "最少消息数（默认 1）"},
                "limit": {"type": "integer", "description": "最多返回条数（默认 100）"},
            },
        },
    },
    {
        "name": "wx_export_chats",
        "description": "导出聊天记录为 csv/txt/docx/db/md/jsonl 格式，返回生成的文件路径",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contacts": {
                    "oneOf": [
                        {"type": "array", "items": {"type": "string"},
                         "description": "联系人名称/备注/wxid 列表"},
                        {"type": "string", "enum": ["all"], "description": "导出全部"},
                    ],
                },
                "formats": {"type": "array", "items": {"type": "string",
                            "enum": ["csv", "txt", "docx", "db", "md", "jsonl"]},
                            "description": "导出格式（默认 ['txt','md']）"},
            },
            "required": ["contacts"],
        },
    },
]


def _result_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def tool_status(args: dict) -> dict:
    from .chat_store import discover_accounts
    from .key_capture import check_prerequisites, load_cached_passphrase
    from .key_derive import load_cached_keys
    lines = [f"前置条件问题: {check_prerequisites() or '无'}",
             f"passphrase 缓存: {'有' if load_cached_passphrase() else '无（需先抓取）'}",
             f"已解锁数据库: {len(load_cached_keys())} 个",
             f"发现账号: {[a.name for a in discover_accounts()] or '无'}"]
    return _result_text("\n".join(lines))


def _store():
    from .chat_store import ChatStore
    store = ChatStore()
    store.ensure_ready()
    return store


def tool_list_contacts(args: dict) -> dict:
    store = _store()
    contacts = store.list_contacts()
    if args.get("kind"):
        contacts = [c for c in contacts if c["kind"] == args["kind"]]
    min_n = args.get("min_messages", 1)
    contacts = [c for c in contacts if c["msg_count"] >= min_n][: args.get("limit", 100)]
    lines = [f"{c['name']}（{c['kind_name']}，{c['msg_count']} 条） wxid={c['wxid']}" for c in contacts]
    return _result_text(f"共 {len(contacts)} 个联系人:\n" + "\n".join(lines))


def tool_export_chats(args: dict) -> dict:
    from .exporters import export_chat
    store = _store()
    formats = args.get("formats") or ["txt", "md"]
    out_dir = Path.home() / ".wxdump" / "exports"
    targets_spec = args["contacts"]
    contacts = store.list_contacts()
    if targets_spec == "all":
        targets = [c for c in contacts if c["msg_count"] > 0]
    else:
        by_name = {}
        for c in contacts:
            by_name[c["name"]] = c
            by_name.setdefault(c["wxid"], c)
        targets, missing = [], []
        for name in targets_spec:
            hit = by_name.get(name) or next(
                (c for c in contacts if name in c["name"] or name in c["remark"]
                 or name in c["nick_name"]), None)
            (targets.append(hit) if hit else missing.append(name))
        if missing:
            return _result_text(f"[!] 未找到联系人: {', '.join(missing)}")
    files = []
    for c in targets:
        messages = store.get_messages(c["wxid"])
        files += export_chat(messages, c["name"], out_dir / c["wxid"], formats)
    return _result_text(f"已导出 {len(targets)} 个联系人，{len(files)} 个文件:\n"
                        + "\n".join(str(f) for f in files))


HANDLERS = {"wx_status": tool_status, "wx_list_contacts": tool_list_contacts,
            "wx_export_chats": tool_export_chats}


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method", "")
        req_id = req.get("id")
        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "wxdump", "version": "1.0.0"},
            }}
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            try:
                tool = req["params"]["name"]
                result = HANDLERS[tool](req["params"].get("arguments") or {})
                resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": req_id,
                        "result": {"content": [{"type": "text", "text": f"错误: {e}"}],
                                   "isError": True}}
        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
        else:
            if req_id is not None:
                resp = {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32601, "message": f"unknown method: {method}"}}
            else:
                continue
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
