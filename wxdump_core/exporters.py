"""多格式导出：CSV / TXT / DOCX / SQLite .db / Markdown / JSONL（零第三方依赖）。

DOCX 通过 zipfile + 手写最小 OOXML 结构生成，无需 python-docx。
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import zipfile
from pathlib import Path

from .chat_store import ChatMessage

FORMATS = ("csv", "txt", "docx", "db", "md", "jsonl")


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip())[:60] or "contact"


def export_chat(messages: list[ChatMessage], contact_name: str, out_dir: Path,
                formats: list[str]) -> list[Path]:
    """把一个联系人的消息导出为指定格式，返回生成的文件列表。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{safe_filename(contact_name)}-聊天记录"
    files = []
    for fmt in formats:
        fmt = fmt.lower().strip(".")
        if fmt == "csv":
            files.append(_export_csv(messages, out_dir / f"{base}.csv", contact_name))
        elif fmt == "txt":
            files.append(_export_txt(messages, out_dir / f"{base}.txt", contact_name))
        elif fmt == "docx" or fmt == "word":
            files.append(_export_docx(messages, out_dir / f"{base}.docx", contact_name))
        elif fmt == "db":
            files.append(_export_db(messages, out_dir / f"{base}.db", contact_name))
        elif fmt == "md":
            files.append(_export_md(messages, out_dir / f"{base}.md", contact_name))
        elif fmt in ("jsonl", "json"):
            files.append(_export_jsonl(messages, out_dir / f"{base}.jsonl", contact_name))
        else:
            raise ValueError(f"不支持的格式: {fmt}（可选 {', '.join(FORMATS)}）")
    return files


def pack_zip(files: list[Path], zip_path: Path) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    return zip_path


# ---------- 各格式实现 ----------

def _export_csv(messages: list[ChatMessage], path: Path, name: str) -> Path:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["时间", "发送者", "类型", "内容"])
        for m in messages:
            writer.writerow([m.time_str, m.sender, m.type_name, m.text])
    return path


def _export_txt(messages: list[ChatMessage], path: Path, name: str) -> Path:
    lines = [f"与 {name} 的聊天记录（共 {len(messages)} 条）", "=" * 40, ""]
    lines += [f"[{m.time_str}] {m.sender}: {m.text}" for m in messages]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _export_md(messages: list[ChatMessage], path: Path, name: str) -> Path:
    if messages:
        t0, t1 = messages[0].time_str, messages[-1].time_str
        header = f"# 与 {name} 的聊天记录\n\n共 {len(messages)} 条消息 | 时间范围 {t0} ~ {t1}\n\n---\n\n"
    else:
        header = f"# 与 {name} 的聊天记录\n\n（无消息）\n\n"
    body = "".join(f"[{m.time_str}] {m.sender}: {m.text}\n" for m in messages)
    path.write_text(header + body, encoding="utf-8")
    return path


def _export_jsonl(messages: list[ChatMessage], path: Path, name: str) -> Path:
    with open(path, "w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m.to_dict(), ensure_ascii=False) + "\n")
    return path


def _export_db(messages: list[ChatMessage], path: Path, name: str) -> Path:
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages ("
                 "id INTEGER PRIMARY KEY, time TEXT, timestamp INTEGER, sender TEXT, "
                 "type TEXT, type_code INTEGER, content TEXT)")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('contact', ?)", (name,))
    conn.execute("INSERT INTO meta VALUES ('count', ?)", (str(len(messages)),))
    conn.executemany(
        "INSERT INTO messages (time, timestamp, sender, type, type_code, content) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(m.time_str, m.timestamp, m.sender, m.type_name, m.msg_type, m.text) for m in messages],
    )
    conn.commit()
    conn.close()
    return path


# ---------- 零依赖 DOCX ----------

_DOC_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml"
 ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _para(text: str, bold: bool = False, size: int = 21) -> str:
    rpr = f'<w:rPr><w:b/><w:sz w:val="{size}"/></w:rPr>' if bold else f'<w:rPr><w:sz w:val="{size}"/></w:rPr>'
    return (f'<w:p><w:pPr><w:spacing w:after="60"/></w:pPr>'
            f'<w:r>{rpr}<w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r></w:p>')


def _export_docx(messages: list[ChatMessage], path: Path, name: str) -> Path:
    body = [_para(f"与 {name} 的聊天记录", bold=True, size=36),
            _para(f"共 {len(messages)} 条消息", size=20)]
    if messages:
        body.append(_para(f"{messages[0].time_str} ~ {messages[-1].time_str}", size=20))
    for m in messages:
        body.append(_para(f"[{m.time_str}] {m.sender}: {m.text}"))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{"".join(body)}<w:sectPr/></w:body></w:document>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _DOC_CT)
        zf.writestr("_rels/.rels", _DOC_RELS)
        zf.writestr("word/document.xml", document)
    return path
