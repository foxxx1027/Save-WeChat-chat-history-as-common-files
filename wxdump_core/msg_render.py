"""消息渲染：zstd 解压 + 微信消息类型解析为可读文本。"""
from __future__ import annotations

import ctypes
import re


def _stdlib_zstd():
    """Python 3.14+ 的 compression.zstd（PEP 784）。"""
    from compression import zstd
    return lambda data: zstd.ZstdDecompressor().decompress(data)


def _ctypes_zstd():
    """低版本兜底：ctypes 直调系统 libzstd。"""
    lib = None
    for name in ("libzstd.so.1", "libzstd.so"):
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    if lib is None:
        raise ImportError("libzstd 未找到")
    lib.ZSTD_decompress.restype = ctypes.c_size_t
    lib.ZSTD_isError.restype = ctypes.c_uint
    lib.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
    _UNKNOWN = 0xFFFFFFFFFFFFFFFE
    _ERROR = 0xFFFFFFFFFFFFFFFF

    def _decompress(data: bytes) -> bytes:
        size = lib.ZSTD_getFrameContentSize(data, len(data))
        if size in (_UNKNOWN, _ERROR):
            size = 1 << 20  # 内容大小未知：从 1MB 起步扩容
        while True:
            buf = ctypes.create_string_buffer(size)
            ret = lib.ZSTD_decompress(buf, size, data, len(data))
            if lib.ZSTD_isError(ret):
                if size > (1 << 31):  # 反复扩容仍失败
                    raise OSError("ZSTD_decompress 失败")
                size *= 4
                continue
            return buf.raw[:ret]

    return _decompress


try:
    _decompress_raw = _stdlib_zstd()
    ZSTD_IMPL = "stdlib (Python≥3.14)"
except ImportError:
    try:
        _decompress_raw = _ctypes_zstd()
        ZSTD_IMPL = "ctypes libzstd"
    except (ImportError, OSError):
        _decompress_raw = None
        ZSTD_IMPL = None

ZSTD_OK = _decompress_raw is not None

TYPE_NAMES = {
    1: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频", 47: "动画表情",
    48: "位置", 49: "链接/卡片", 50: "通话", 51: "系统通知", 10000: "系统消息",
    10002: "撤回消息",
}


def decompress_content(content, compressed_flag) -> bytes | str | None:
    """按 WCDB_CT 标志解压 zstd 内容。"""
    if content is None:
        return None
    if not compressed_flag:
        return content
    if not ZSTD_OK or not isinstance(content, (bytes, bytearray)):
        return "[内容已压缩，系统缺少 zstd 支持无法解压]"
    try:
        return _decompress_raw(bytes(content))
    except Exception:
        return content


def _extract(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    return m.group(1).strip() if m else ""


def render_message(msg_type: int, content: str) -> tuple[str, str]:
    """返回 (类型名, 可读文本)。"""
    if msg_type == 1:
        return "文本", content
    if msg_type == 3:
        return "图片", "[图片]"
    if msg_type == 34:
        return "语音", "[语音]"
    if msg_type == 42:
        return "名片", "[名片]"
    if msg_type == 43:
        return "视频", "[视频]"
    if msg_type == 47:
        return "动画表情", "[动画表情]"
    if msg_type == 48:
        return "位置", "[位置]"
    if msg_type == 49:
        return _render_appmsg(content)
    if msg_type == 50:
        dur = re.search(r"通话时长[^\x00]*", content or "")
        return "通话", f"[通话] {dur.group(0).strip()}" if dur else "[通话]"
    if msg_type in (10000, 10002):
        text = re.sub(r"<[^>]+>", "", content or "").strip()
        return "系统消息", text or "[系统消息]"
    name = TYPE_NAMES.get(msg_type, f"未知类型{msg_type}")
    return name, f"[{name}]" + (f" {content[:80]}" if content else "")


def _render_appmsg(content: str) -> tuple[str, str]:
    """type 49 按 appmsg 子类型细分（引用/文件/音乐/链接/聊天记录…）。"""
    title = _extract(content, "title")
    sub_m = re.search(r"<appmsg[^>]*>.*?<type>(\d+)</type>", content, re.S)
    sub_t = int(sub_m.group(1)) if sub_m else 0
    if sub_t == 57:  # 引用回复：title 即回复文本
        ref = re.search(r"<refermsg>.*?<content>(.*?)</content>", content, re.S)
        ref_txt = ref.group(1).strip()[:60] if ref else ""
        if ref_txt.startswith(("&lt;?xml", "<?xml")):
            ref_txt = "[图片/媒体]"
        base = title or "[引用回复]"
        return "引用回复", f"{base} ⤷ 引用: {ref_txt}" if ref_txt else base
    if sub_t == 6:
        return "文件", f"[文件] {title}" if title else "[文件]"
    if sub_t in (5, 3):
        return "链接", f"[音乐/链接] {title}" if title else "[音乐/链接]"
    if sub_t == 19:
        return "聊天记录", f"[聊天记录] {title}" if title else "[聊天记录]"
    if sub_t == 33:
        return "小程序", f"[小程序] {title}" if title else "[小程序]"
    return "链接/卡片", f"[卡片] {title}" if title else "[卡片消息]"
