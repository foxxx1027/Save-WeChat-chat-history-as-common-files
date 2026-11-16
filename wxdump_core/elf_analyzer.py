"""ELF 静态分析：在微信二进制中定位 WCDB Cipher 配置函数的虚拟地址。

原理：.rodata 中的锚字符串 "com.Tencent.WCDB.Config.Cipher" 被 LEA rsi 引用，
其前方 7 字节处是 LEA rdi 指向的中间数据；再由第二次 LEA rsi 交叉引用回溯，
找到以 `55 41 57`（push rbp; push r15）开头的函数头。该函数在微信登录
打开数据库时以寄存器传递 32 字节 passphrase。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

ANCHOR_STRING = b"com.Tencent.WCDB.Config.Cipher"
LEA_RSI = b"\x48\x8D\x35"
LEA_RDI = b"\x48\x8D\x3D"
FUNC_HEAD = b"\x55\x41\x57"
ELF_MAGIC = b"\x7fELF"
EM_X86_64 = 62


@dataclass(frozen=True)
class HookCandidate:
    target_va: int
    anchor_string_va: int
    intermediate_va: int
    second_reference_va: int


def load_elf_sections(binary_path: Path) -> dict[str, tuple[int, bytes]]:
    """解析 ELF64 节表，返回 {节名: (虚拟地址, 节数据)}。"""
    data = binary_path.read_bytes()
    if data[:4] != ELF_MAGIC:
        raise RuntimeError(f"不是 ELF 文件: {binary_path}")
    if data[4] != 2 or data[5] != 1:
        raise RuntimeError(f"仅支持 ELF64 小端: {binary_path}")
    (_etype, machine, _ver, _entry, _phoff, shoff, _flags, _ehsz, _phesz, _phnum,
     shentsize, shnum, shstrndx) = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    if machine != EM_X86_64:
        raise RuntimeError(f"仅支持 x86_64 ELF: {binary_path}")

    sections_raw = []
    for index in range(shnum):
        offset = shoff + index * shentsize
        sh_name, _t, _f, sh_addr, sh_offset, sh_size = struct.unpack_from("<IIQQQQ", data, offset)
        sections_raw.append((sh_name, sh_addr, sh_offset, sh_size))
    if shstrndx >= len(sections_raw):
        raise RuntimeError(f"节字符串表索引无效: {binary_path}")
    _n, _a, shstr_off, shstr_size = sections_raw[shstrndx]
    shstr = data[shstr_off:shstr_off + shstr_size]

    sections: dict[str, tuple[int, bytes]] = {}
    for sh_name, sh_addr, sh_offset, sh_size in sections_raw:
        end = shstr.find(b"\0", sh_name)
        name = shstr[sh_name:end if end != -1 else None].decode("utf-8", errors="replace")
        if name:
            sections[name] = (sh_addr, data[sh_offset:sh_offset + sh_size])
    return sections


def _rip_relative_hits(text_addr: int, text: bytes, opcode: bytes, target_va: int) -> list[int]:
    hits = []
    limit = max(0, len(text) - 7)
    for off in range(limit + 1):
        if text[off:off + 3] != opcode:
            continue
        disp = struct.unpack_from("<i", text, off + 3)[0]
        if text_addr + off + 7 + disp == target_va:
            hits.append(off)
    return hits


def find_hook_candidates(binary_path: Path) -> list[HookCandidate]:
    sections = load_elf_sections(binary_path)
    try:
        rodata_addr, rodata = sections[".rodata"]
        text_addr, text = sections[".text"]
    except KeyError as exc:
        raise RuntimeError(f"ELF 缺少 .rodata/.text 节: {exc}") from exc

    candidates: list[HookCandidate] = []
    search_from = 0
    while True:
        anchor_off = rodata.find(ANCHOR_STRING, search_from)
        if anchor_off == -1:
            break
        search_from = anchor_off + 1
        anchor_va = rodata_addr + anchor_off

        for first_ref in _rip_relative_hits(text_addr, text, LEA_RSI, anchor_va):
            if first_ref < 7 or text[first_ref - 7:first_ref - 4] != LEA_RDI:
                continue
            unk_disp = struct.unpack_from("<i", text, first_ref - 4)[0]
            unk_va = text_addr + first_ref + unk_disp
            for second_ref in _rip_relative_hits(text_addr, text, LEA_RSI, unk_va):
                head = next(
                    (c for c in range(second_ref, max(0, second_ref - 0x500) - 1, -1)
                     if text[c:c + len(FUNC_HEAD)] == FUNC_HEAD),
                    None,
                )
                if head is None:
                    continue
                candidates.append(HookCandidate(
                    target_va=text_addr + head,
                    anchor_string_va=anchor_va,
                    intermediate_va=unk_va,
                    second_reference_va=text_addr + second_ref,
                ))
    return sorted({c.target_va: c for c in candidates}.values(), key=lambda c: c.target_va)


def select_hook_candidate(binary_path: Path) -> HookCandidate:
    candidates = find_hook_candidates(binary_path)
    if not candidates:
        raise RuntimeError("未找到 WCDB Cipher 挂点（微信版本可能不兼容）")
    return candidates[0]


def find_runtime_base(pid: int, binary_path: Path) -> int:
    """从 /proc/{pid}/maps 找二进制可执行段的运行时基址。"""
    binary_name = binary_path.name
    maps = Path(f"/proc/{pid}/maps").read_text(encoding="utf-8")
    lines = [ln.split() for ln in maps.splitlines() if len(ln.split()) >= 6]
    for parts in lines:
        if parts[5].endswith("/" + binary_name) and "x" in parts[1]:
            return int(parts[0].split("-")[0], 16)
    for parts in lines:  # 兜底：任意映射段
        if parts[5].endswith("/" + binary_name):
            return int(parts[0].split("-")[0], 16)
    raise RuntimeError(f"未找到 {binary_name} 的内存映射 (PID={pid})")
