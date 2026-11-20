# -*- coding: utf-8 -*-
"""محلل ثنائي لملف global-metadata.dat في ألعاب Unity IL2CPP لاستخراج أدلة اللغة ومكتبات النصوص بدون موديل."""

import os
import re
import struct
from typing import List, Optional, Set, Tuple


IL2CPP_METADATA_MAGIC = 0xFAB11BAF

_ASCII_RUN_RE = re.compile(rb"[ -~]{4,120}")

_TMP_MARKERS = (
    b"Unity.TextMeshPro",
    b"TMP_FontAsset",
    b"TMP_Text",
    b"TextMeshProUGUI",
)
_UGUI_MARKERS = (
    b"UnityEngine.UI",
    b"GraphicRaycaster",
    b"CanvasScaler",
)


def build_il2cpp_global_metadata_blob(
    string_literals: List[str],
    identifiers: Optional[List[str]] = None,
    version: int = 29,
) -> bytes:
    """بناء صورة ثنائية صالحة لملف global-metadata.dat (مفيدة للاختبارات والتحقق)."""
    lit_table = bytearray()
    lit_data = bytearray()
    for text in string_literals:
        encoded = text.encode("utf-8")
        data_idx = len(lit_data)
        lit_data.extend(encoded)
        lit_table.extend(struct.pack("<II", len(encoded), data_idx))

    str_table = bytearray()
    for ident in identifiers or []:
        str_table.extend(ident.encode("utf-8"))
        str_table.append(0)

    header_size = 32
    lit_off = header_size
    lit_size = len(lit_table)
    lit_data_off = lit_off + lit_size
    lit_data_size = len(lit_data)
    str_off = lit_data_off + lit_data_size
    str_size = len(str_table)

    header = struct.pack(
        "<IIIIIIII",
        IL2CPP_METADATA_MAGIC,
        version,
        lit_off,
        lit_size,
        lit_data_off,
        lit_data_size,
        str_off,
        str_size,
    )
    return bytes(header) + bytes(lit_table) + bytes(lit_data) + bytes(str_table)


def build_il2cpp_modern_metadata_blob(
    string_literals: List[str],
    identifiers: Optional[List[str]] = None,
    version: int = 108,
) -> bytes:
    """بناء صورة ثنائية حديثة (تخطيط ثلاثيات v31+/v108) لملف global-metadata.dat للاختبارات."""
    lit_offsets = bytearray()
    lit_data = bytearray()
    for text in string_literals:
        lit_offsets.extend(struct.pack("<I", len(lit_data)))
        lit_data.extend(text.encode("utf-8"))
        lit_data.append(0)

    str_table = bytearray()
    idents = identifiers or []
    for ident in idents:
        str_table.extend(ident.encode("utf-8"))
        str_table.append(0)

    header_size = 48
    lit_off = header_size
    lit_size = len(lit_offsets)
    lit_cnt = len(string_literals)
    lit_data_off = lit_off + lit_size
    lit_data_size = len(lit_data)
    lit_data_cnt = lit_cnt
    str_off = lit_data_off + lit_data_size
    str_size = len(str_table)
    str_cnt = len(idents)

    header = struct.pack(
        "<12I",
        IL2CPP_METADATA_MAGIC,
        version,
        lit_off,
        lit_size,
        lit_cnt,
        lit_data_off,
        lit_data_size,
        lit_data_cnt,
        str_off,
        str_size,
        str_cnt,
        0,
    )
    return bytes(header) + bytes(lit_offsets) + bytes(lit_data) + bytes(str_table)


def _append_unique(target: List[str], seen: Set[str], value: str, max_strings: int) -> bool:
    if not value or value in seen:
        return len(target) >= max_strings
    seen.add(value)
    target.append(value)
    return len(target) >= max_strings


def extract_il2cpp_metadata_strings(
    metadata_path: str, max_strings: int = 25000
) -> List[str]:
    """استخراج النصوص والمعرّفات مباشرة من global-metadata.dat دون فك تشفير خارجي."""
    if not metadata_path or not os.path.isfile(metadata_path):
        return []

    try:
        with open(metadata_path, "rb") as f:
            data = f.read()
    except OSError:
        return []

    if len(data) < 32:
        return []

    strings: List[str] = []
    seen: Set[str] = set()

    magic = struct.unpack_from("<I", data, 0)[0]
    if magic == IL2CPP_METADATA_MAGIC:
        is_modern_triplet = False
        if len(data) >= 48:
            hdr12 = struct.unpack_from("<12I", data, 0)
            m_lit_off, m_lit_size, m_lit_cnt = hdr12[2], hdr12[3], hdr12[4]
            m_lit_data_off, m_lit_data_size = hdr12[5], hdr12[6]
            m_str_off, m_str_size = hdr12[8], hdr12[9]
            if (
                m_lit_off >= 44
                and m_lit_cnt > 0
                and m_lit_size == m_lit_cnt * 4
                and m_lit_off + m_lit_size <= m_lit_data_off <= len(data)
                and (m_lit_data_off - (m_lit_off + m_lit_size)) <= 16
                and m_lit_data_size > 0
                and m_lit_data_off + m_lit_data_size <= len(data)
            ):
                is_modern_triplet = True
                lit_data_end = m_lit_data_off + m_lit_data_size
                offsets = struct.unpack_from(f"<{m_lit_cnt}I", data, m_lit_off)
                for idx in range(m_lit_cnt):
                    start = m_lit_data_off + offsets[idx]
                    next_off = offsets[idx + 1] if idx + 1 < m_lit_cnt else m_lit_data_size
                    end = m_lit_data_off + next_off
                    if start < m_lit_data_off or end > lit_data_end or end <= start or (end - start) > 4096:
                        continue
                    text = data[start:end].rstrip(b"\x00").decode("utf-8", errors="ignore")
                    if _append_unique(strings, seen, text, max_strings):
                        return strings

                if m_str_off >= 44 and m_str_size > 0 and m_str_off + m_str_size <= len(data):
                    blob = data[m_str_off : m_str_off + m_str_size]
                    for raw in blob.split(b"\x00"):
                        if len(raw) < 2 or len(raw) > 256:
                            continue
                        text = raw.decode("utf-8", errors="ignore")
                        if _append_unique(strings, seen, text, max_strings):
                            return strings

        if not is_modern_triplet:
            _, _, lit_off, lit_size, lit_data_off, lit_data_size, str_off, str_size = (
                struct.unpack_from("<IIIIIIII", data, 0)
            )

            # استخراج String Literals (تخطيط v24–v29 الكلاسيكي)
            if (
                lit_off >= 32
                and lit_size > 0
                and lit_off + lit_size <= len(data)
                and lit_data_off >= 32
                and lit_data_size > 0
                and lit_data_off + lit_data_size <= len(data)
            ):
                lit_data_end = lit_data_off + lit_data_size
                table_end = lit_off + (lit_size - (lit_size % 8))
                for pos in range(lit_off, table_end, 8):
                    str_len, str_idx = struct.unpack_from("<II", data, pos)
                    if str_len == 0 or str_len > 4096:
                        continue
                    start = lit_data_off + str_idx
                    end = start + str_len
                    if start < lit_data_off or end > lit_data_end:
                        continue
                    text = data[start:end].decode("utf-8", errors="ignore")
                    if _append_unique(strings, seen, text, max_strings):
                        return strings

            # استخراج جدول المعرفات النصية المنتهية بصفر
            if str_off >= 32 and str_size > 0 and str_off + str_size <= len(data):
                blob = data[str_off : str_off + str_size]
                for raw in blob.split(b"\x00"):
                    if len(raw) < 2 or len(raw) > 256:
                        continue
                    text = raw.decode("utf-8", errors="ignore")
                    if _append_unique(strings, seen, text, max_strings):
                        return strings

    # مسح احتياطي إذا كانت الترويسة غير قياسية أو مخصصة
    if not strings:
        sample = data[: 8 * 1024 * 1024]
        for match in _ASCII_RUN_RE.finditer(sample):
            text = match.group(0).decode("ascii", errors="ignore")
            if _append_unique(strings, seen, text, max_strings):
                break

    return strings


def _read_head_bytes(file_path: str, limit: int) -> bytes:
    try:
        with open(file_path, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def detect_il2cpp_ui_frameworks_from_files(
    metadata_path: Optional[str],
    data_dir: Optional[str],
    interop_dir: Optional[str],
) -> Tuple[bool, bool]:
    """اكتشاف استخدام TextMeshPro و UGUI في ألعاب IL2CPP عبر Interop أو البصمات الثنائية."""
    has_tmp = False
    has_ugui = False

    if interop_dir and os.path.isdir(interop_dir):
        try:
            dll_names = {f.lower() for f in os.listdir(interop_dir) if f.lower().endswith(".dll")}
        except OSError:
            dll_names = set()
        if dll_names & {"unity.textmeshpro.dll", "il2cppunity.textmeshpro.dll"}:
            has_tmp = True
        if dll_names & {"unityengine.ui.dll", "il2cppunityengine.ui.dll"}:
            has_ugui = True

    if has_tmp and has_ugui:
        return has_tmp, has_ugui

    scan_targets: List[Tuple[str, int]] = []
    if metadata_path and os.path.isfile(metadata_path):
        scan_targets.append((metadata_path, 16 * 1024 * 1024))
    if data_dir and os.path.isdir(data_dir):
        for fname in ("ScriptingAssemblies.json", "globalgamemanagers", "resources.assets", "data.unity3d"):
            candidate = os.path.join(data_dir, fname)
            if os.path.isfile(candidate):
                scan_targets.append((candidate, 8 * 1024 * 1024))

    for file_path, max_bytes in scan_targets:
        chunk = _read_head_bytes(file_path, max_bytes)
        if not chunk:
            continue
        if not has_tmp and any(marker in chunk for marker in _TMP_MARKERS):
            has_tmp = True
        if not has_ugui and any(marker in chunk for marker in _UGUI_MARKERS):
            has_ugui = True
        if has_tmp and has_ugui:
            break

    return has_tmp, has_ugui
