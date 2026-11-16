"""零依赖 Web UI：http.server + 内置 REST API（默认 127.0.0.1，仅本机访问）。

API:
    GET  /api/status            环境体检
    POST /api/key/capture       启动密钥抓取（后台线程，等微信重新登录）
    GET  /api/key/status        抓取进度
    GET  /api/contacts          联系人列表（含消息数）
    POST /api/export            {contacts: [wxid...]|"all", formats: [...], zip: bool}
    GET  /api/export/result     导出进度与产物
    GET  /download?path=...     下载导出文件（仅限导出目录内）
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .chat_store import ChatStore
from .exporters import FORMATS, export_chat, pack_zip

WEBAPP_DIR = Path(__file__).parent.parent / "webapp"
EXPORT_ROOT = Path.home() / ".wxdump" / "web-exports"

_state = {
    "capture": {"running": False, "error": None, "done": False},
    "export": {"running": False, "error": None, "done": False,
               "progress": "", "files": [], "current": 0, "total": 0},
}
_store_lock = threading.Lock()
_store: ChatStore | None = None
_custom_root: str | None = None


def _get_store() -> ChatStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = ChatStore(custom_root=Path(_custom_root).expanduser() if _custom_root else None)
        return _store


def _start_capture() -> None:
    from .key_capture import capture_passphrase, check_prerequisites
    from .key_derive import derive_keys

    state = _state["capture"]
    state.update(running=True, error=None, done=False)
    try:
        issues = check_prerequisites()
        if issues:
            raise RuntimeError("；".join(issues))
        passphrase = capture_passphrase(timeout=300)
        store = _get_store()
        derive_keys(passphrase, store.account.db_dir)
        state.update(running=False, done=True)
    except Exception as e:  # 捕获线程内必须吞掉异常写入状态
        state.update(running=False, error=str(e))


def _run_export(wxids: list[str] | str, formats: list[str], make_zip: bool) -> None:
    state = _state["export"]
    state.update(running=True, error=None, done=False, files=[], current=0, progress="")
    try:
        store = _get_store()
        store.ensure_ready()
        contacts = store.list_contacts()
        if wxids == "all":
            targets = [c for c in contacts if c["msg_count"] > 0]
        else:
            by_wxid = {c["wxid"]: c for c in contacts}
            targets = [by_wxid[w] for w in wxids if w in by_wxid]
        state["total"] = len(targets)
        files = []
        for i, c in enumerate(targets, 1):
            state["current"] = i
            state["progress"] = f"导出 {c['name']}（{c['msg_count']} 条）"
            messages = store.get_messages(c["wxid"])
            files += export_chat(messages, c["name"], EXPORT_ROOT / c["wxid"], formats)
        if make_zip and len(files) > 1:
            state["progress"] = "打包 zip..."
            files = [pack_zip(files, EXPORT_ROOT / f"wxdump-{len(files)}files.zip")]
        state.update(files=[str(f) for f in files], running=False, done=True,
                     progress=f"完成：{len(files)} 个文件")
    except Exception as e:
        state.update(running=False, error=str(e))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    # ---- helpers ----
    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else {}

    # ---- routes ----
    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/" or path == "/index.html":
                content = (WEBAPP_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            elif path == "/api/status":
                self._json(self._status())
            elif path == "/api/key/status":
                s = _state["capture"]
                self._json({"running": s["running"], "done": s["done"], "error": s["error"]})
            elif path == "/api/contacts":
                store = _get_store()
                store.ensure_ready()
                self._json(store.list_contacts())
            elif path == "/api/export/result":
                self._json(_state["export"])
            elif path == "/download":
                self._download()
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/key/capture":
                if _state["capture"]["running"]:
                    return self._json({"ok": False, "error": "抓取已在进行中"})
                threading.Thread(target=_start_capture, daemon=True).start()
                return self._json({"ok": True})
            if path == "/api/export":
                if _state["export"]["running"]:
                    return self._json({"ok": False, "error": "已有导出任务进行中"})
                wxids = body.get("contacts", "all")
                formats = [f for f in body.get("formats", ["txt"]) if f in FORMATS]
                threading.Thread(
                    target=_run_export,
                    args=(wxids, formats or ["txt"], bool(body.get("zip"))),
                    daemon=True,
                ).start()
                return self._json({"ok": True})
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _status(self) -> dict:
        from wxdump_core.key_capture import check_prerequisites, load_cached_passphrase
        from wxdump_core.key_derive import load_cached_keys
        info = {"prerequisites": check_prerequisites(),
                "passphrase_cached": load_cached_passphrase() is not None,
                "keys_cached": len(load_cached_keys()),
                "formats": list(FORMATS)}
        try:
            store = _get_store()
            info.update(store.status(), ready=True)
        except Exception as e:
            info.update(ready=False, error=str(e))
        return info

    def _download(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        target = Path(query["path"][0]).resolve()
        if not str(target).startswith(str(EXPORT_ROOT.resolve())):
            return self._json({"error": "forbidden"}, 403)
        if not target.is_file():
            return self._json({"error": "not found"}, 404)
        content = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/zip" if target.suffix == ".zip" else "application/octet-stream")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{urllib.parse.quote(target.name)}")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def run(port: int = 8765, root: str | None = None) -> None:
    global _custom_root
    _custom_root = root
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[*] wxdump Web UI: http://127.0.0.1:{port}（Ctrl+C 停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 已停止")
