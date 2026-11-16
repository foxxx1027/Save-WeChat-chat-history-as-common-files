#!/usr/bin/env python3
"""wxdump — 微信本地聊天记录解密导出工具（Linux 完整支持 / Android 引擎复用型支持）。

仅限本人设备、本人微信账号的数据导出。

用法:
    python3 wxdump.py status                 # 环境体检
    python3 wxdump.py capture-key [--force]  # 抓取数据库密钥（需微信退出账号重新登录）
    python3 wxdump.py contacts [--kind friend|chatroom|official] [--all]
    python3 wxdump.py export --contacts "张三,李四" --formats csv,txt,docx,db,md,jsonl --out ./exports
    python3 wxdump.py export --all --formats txt
    python3 wxdump.py ui [--port 8765]       # Web 界面
    python3 wxdump.py mcp                    # MCP server（stdio）
    python3 wxdump.py android scan|pull|key  # Android 支持
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from wxdump_core.chat_store import ChatStore, discover_accounts  # noqa: E402
from wxdump_core.exporters import FORMATS, export_chat, pack_zip  # noqa: E402


def _progress(done, total, ok):
    print(f"    密钥派生: {done}/{total}（{ok} 个验证通过）", flush=True)


def get_store(args) -> ChatStore:
    root = Path(args.root).expanduser() if getattr(args, "root", None) else None
    return ChatStore(custom_root=root)


def cmd_status(args) -> int:
    from wxdump_core.key_capture import check_prerequisites, load_cached_passphrase
    print("=== wxdump 环境体检 ===")
    issues = check_prerequisites()
    for issue in issues:
        print(f"  [!] {issue}")
    if not issues:
        print("  [ok] gdb / ptrace 权限就绪")
    print(f"  [{'ok' if load_cached_passphrase() else '!'}] passphrase 缓存")
    accounts = discover_accounts(Path(args.root).expanduser() if args.root else None)
    for acc in accounts:
        print(f"  [ok] 账号: {acc.name}  数据目录: {acc.db_dir}")
    if not accounts:
        print("  [!] 未发现微信账号目录")
        return 1
    try:
        store = get_store(args)
        store.ensure_ready(_progress)
        info = store.status()
        print(f"  [ok] 已解锁 {info['keys_cached']} 个数据库")
    except RuntimeError as e:
        print(f"  [!] {e}")
    return 0


def cmd_capture_key(args) -> int:
    from wxdump_core.key_capture import capture_passphrase, load_cached_passphrase
    if load_cached_passphrase() and not args.force:
        print("[+] 已有缓存的 passphrase（用 --force 重新抓取）")
        return 0
    print("[*] 前提：微信正在运行且已登录")
    print("[*] 启动后请在微信中：退出账号 → 重新扫码登录（不是退出微信程序）")
    print("[*] 开始等待登录事件（最长 300 秒）...", flush=True)
    try:
        passphrase = capture_passphrase(timeout=args.timeout)
    except Exception as e:
        print(f"[!] 失败: {e}")
        return 1
    print(f"[+] passphrase 已抓取并缓存: {passphrase[:8]}...")
    store = get_store(args)
    print("[*] 开始派生各数据库密钥（约 30 秒）...")
    store.ensure_ready(_progress)
    print("[+] 密钥全部就绪，可以导出了")
    return 0


def cmd_contacts(args) -> int:
    store = get_store(args)
    store.ensure_ready(_progress)
    contacts = store.list_contacts()
    if args.kind:
        contacts = [c for c in contacts if c["kind"] == args.kind]
    if not args.all:
        contacts = [c for c in contacts if c["msg_count"] > 0] or contacts[:20]
    print(f"{'联系人':<24}{'类型':<8}{'消息数':>8}  wxid")
    for c in contacts:
        print(f"{c['name']:<24}{c['kind_name']:<8}{c['msg_count']:>8}  {c['wxid']}")
    print(f"\n共 {len(contacts)} 个联系人")
    return 0


def _match_contacts(store: ChatStore, patterns: list[str]) -> list[dict]:
    """按 名称/备注/昵称/wxid 模糊匹配联系人。"""
    all_contacts = store.list_contacts()
    matched = []
    for pattern in patterns:
        p = pattern.strip()
        hit = next(
            (c for c in all_contacts
             if c["wxid"] == p or c["name"] == p or c["nick_name"] == p or c["remark"] == p),
            None,
        )
        if hit is None:
            hits = [c for c in all_contacts
                    if p in c["name"] or p in c["nick_name"] or p in c["remark"] or p in c["wxid"]]
            if len(hits) == 1:
                hit = hits[0]
            elif len(hits) > 1:
                print(f"[!] 「{p}」匹配到多个联系人，请精确指定:")
                for c in hits:
                    print(f"    {c['name']}（{c['kind_name']}，{c['msg_count']} 条）  {c['wxid']}")
                continue
        if hit is None:
            print(f"[!] 未找到联系人「{p}」")
        elif hit not in matched:
            matched.append(hit)
    return matched


def cmd_export(args) -> int:
    store = get_store(args)
    store.ensure_ready(_progress)

    if args.all:
        targets = [c for c in store.list_contacts() if c["msg_count"] > 0]
        if args.kind:
            targets = [c for c in targets if c["kind"] == args.kind]
    else:
        if not args.contacts:
            print("[!] 请用 --contacts \"名字1,名字2\" 或 --all")
            return 1
        targets = _match_contacts(store, args.contacts.split(","))

    if not targets:
        print("[!] 没有可导出的联系人")
        return 1

    formats = [f.strip() for f in args.formats.split(",")]
    out_dir = Path(args.out).expanduser()
    print(f"[*] 导出 {len(targets)} 个联系人 → {out_dir}（格式: {', '.join(formats)}）")

    files = []
    for i, c in enumerate(targets, 1):
        print(f"  [{i}/{len(targets)}] {c['name']}（{c['msg_count']} 条）...", flush=True)
        messages = store.get_messages(c["wxid"], self_wxid=store.self_wxid())
        files += export_chat(messages, c["name"], out_dir / c["wxid"], formats)

    if args.zip and len(files) > 1:
        zip_path = pack_zip(files, out_dir / f"wxdump-export-{len(files)}files.zip")
        print(f"[+] 打包: {zip_path}")
    print(f"[+] 完成，共生成 {len(files)} 个文件")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="wxdump", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", help="微信数据目录（xwechat_files 父目录，默认自动发现）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="环境体检")

    p_cap = sub.add_parser("capture-key", help="抓取数据库密钥")
    p_cap.add_argument("--force", action="store_true", help="忽略缓存重新抓取")
    p_cap.add_argument("--timeout", type=int, default=300)

    p_con = sub.add_parser("contacts", help="列出联系人及消息数")
    p_con.add_argument("--kind", choices=["friend", "chatroom", "official"])
    p_con.add_argument("--all", action="store_true", help="含零消息联系人")

    p_exp = sub.add_parser("export", help="导出聊天记录")
    p_exp.add_argument("--contacts", help='联系人（逗号分隔，支持名称/备注/wxid 模糊匹配）')
    p_exp.add_argument("--all", action="store_true", help="导出全部有消息的联系人")
    p_exp.add_argument("--kind", choices=["friend", "chatroom", "official"], help="配合 --all")
    p_exp.add_argument("--formats", default="txt,md", help=f"格式（默认 txt,md；可选 {','.join(FORMATS)}）")
    p_exp.add_argument("--out", default="./wxdump-exports", help="输出目录")
    p_exp.add_argument("--zip", action="store_true", help="多文件时打包 zip")

    p_ui = sub.add_parser("ui", help="启动 Web 界面")
    p_ui.add_argument("--port", type=int, default=8765)

    sub.add_parser("mcp", help="MCP server（stdio，供 AI 客户端调用）")

    p_and = sub.add_parser("android", help="Android 支持（scan/pull/key）")
    p_and.add_argument("action", choices=["scan", "pull", "key"])
    p_and.add_argument("--imei")
    p_and.add_argument("--uin")
    p_and.add_argument("--db-key")

    args = parser.parse_args()
    if args.command == "status":
        return cmd_status(args)
    if args.command == "capture-key":
        return cmd_capture_key(args)
    if args.command == "contacts":
        return cmd_contacts(args)
    if args.command == "export":
        return cmd_export(args)
    if args.command == "ui":
        from wxdump_core.web_server import run
        run(port=args.port, root=args.root)
        return 0
    if args.command == "mcp":
        from wxdump_core.mcp_server import serve
        serve()
        return 0
    if args.command == "android":
        from wxdump_core import android
        return android.dispatch(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
