#!/usr/bin/env python3
"""wxdump 冒烟测试：核心链路 + 导出格式 + Web API + MCP 协议。

运行: python3 test_smoke.py
依赖真实环境（微信已登录、密钥已缓存），未就绪的项标记 SKIP。
"""
import json
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

PASS, FAIL, SKIP = [], [], []


def check(name: str, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  [PASS] {name}")
    except SkipTest as e:
        SKIP.append(name)
        print(f"  [SKIP] {name}: {e}")
    except Exception as e:
        FAIL.append((name, e))
        print(f"  [FAIL] {name}: {e}")


class SkipTest(Exception):
    pass


def need_keys():
    from wxdump_core.key_derive import load_cached_keys
    if not load_cached_keys():
        raise SkipTest("密钥未缓存（先跑 capture-key）")


def t_store():
    need_keys()
    from wxdump_core.chat_store import ChatStore
    store = ChatStore()
    store.ensure_ready()
    contacts = store.list_contacts()
    assert contacts, "联系人为空"
    top = contacts[0]
    assert top["msg_count"] > 0, "消息计数异常"
    msgs = store.get_messages(top["wxid"], start=None, end=None)
    # 取消息数中等的联系人验证（太大耗时长）
    small = next((c for c in contacts if 10 < c["msg_count"] < 5000), None)
    if small:
        msgs = store.get_messages(small["wxid"])
        assert len(msgs) == small["msg_count"], f"消息数不符 {len(msgs)} != {small['msg_count']}"
        assert msgs[0].timestamp <= msgs[-1].timestamp, "时间未排序"
        assert any(m.text for m in msgs), "渲染文本全空"


def t_export_formats():
    need_keys()
    from wxdump_core.chat_store import ChatStore
    from wxdump_core.exporters import export_chat
    store = ChatStore()
    contacts = store.list_contacts()
    small = next((c for c in contacts if 10 < c["msg_count"] < 3000), None)
    if not small:
        raise SkipTest("无合适规模的联系人")
    msgs = store.get_messages(small["wxid"])
    out = Path("/tmp/wxdump-smoke")
    files = export_chat(msgs, small["name"], out, ["csv", "txt", "docx", "db", "md", "jsonl"])
    for f in files:
        assert f.exists() and f.stat().st_size > 0, f"{f.name} 为空"
    # CSV 行数（csv.reader 正确处理字段内换行）
    import csv as csv_mod
    with open(files[0], encoding="utf-8-sig") as f:
        rows = list(csv_mod.reader(f))
    assert len(rows) == len(msgs) + 1, f"CSV 记录数 {len(rows)} != {len(msgs)}+1"
    # DOCX zip 结构
    with zipfile.ZipFile([f for f in files if f.suffix == ".docx"][0]) as zf:
        assert "word/document.xml" in zf.namelist()
        doc = zf.read("word/document.xml").decode()
        assert doc.count("<w:p>") >= len(msgs)
    # SQLite 可查
    conn = sqlite3.connect([f for f in files if f.suffix == ".db"][0])
    n = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    conn.close()
    assert n == len(msgs), f"导出库行数 {n} != {len(msgs)}"
    # JSONL 可解析
    lines = [json.loads(l) for l in
             [f for f in files if f.suffix == ".jsonl"][0].read_text().splitlines()]
    assert len(lines) == len(msgs)


def t_cli():
    r = subprocess.run([sys.executable, str(ROOT / "wxdump.py"), "status"],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "环境体检" in r.stdout


def t_web_api():
    import threading
    import time
    import urllib.request
    from wxdump_core.web_server import run  # noqa: F401
    need_keys()
    from wxdump_core import web_server
    server_thread = threading.Thread(
        target=lambda: web_server.run(port=18765), daemon=True)
    server_thread.start()
    time.sleep(0.8)
    base = "http://127.0.0.1:18765"
    def jget(path, timeout=120):
        return json.loads(urllib.request.urlopen(base + path, timeout=timeout).read())
    html = urllib.request.urlopen(base + "/", timeout=10).read().decode()
    assert "wxdump" in html and "选择联系人" in html
    status = jget("/api/status")
    assert status["keys_cached"] > 0
    contacts = jget("/api/contacts")
    assert isinstance(contacts, list) and contacts


def t_mcp():
    reqs = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "wx_status", "arguments": {}}}),
    ])
    r = subprocess.run([sys.executable, str(ROOT / "wxdump.py"), "mcp"],
                       input=reqs, capture_output=True, text=True, timeout=120)
    lines = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    by_id = {l["id"]: l for l in lines if "id" in l}
    assert "serverInfo" in by_id[1]["result"]
    tools = by_id[2]["result"]["tools"]
    assert {t["name"] for t in tools} == {"wx_status", "wx_list_contacts", "wx_export_chats"}
    assert "已解锁数据库" in by_id[3]["result"]["content"][0]["text"]


def main() -> int:
    print("=== wxdump 冒烟测试 ===")
    check("CLI status", t_cli)
    check("ChatStore 联系人+消息", t_store)
    check("六格式导出", t_export_formats)
    check("Web API", t_web_api)
    check("MCP 协议", t_mcp)
    print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败 / {len(SKIP)} 跳过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
