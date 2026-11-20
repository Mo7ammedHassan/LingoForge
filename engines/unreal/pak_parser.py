# -*- coding: utf-8 -*-
"""محلل وباني حزم Unreal Engine (.pak) بلغة Python خالصة (v1–v11) بدون تعديل الأصل."""

from dataclasses import dataclass, field
import gzip
import hashlib
import os
import re
import struct
from typing import Dict, List, Optional, Tuple
import zlib

from formats.locres_handler import read_fstring, write_fstring


PAK_MAGIC = 0x5A6F12E1
PAK_MAGIC_BYTES = struct.pack("<I", PAK_MAGIC)
UTOC_MAGIC = b"-==--==--==--==-"
UASSET_MAGIC = 0x9E2A83C1
UASSET_MAGIC_BYTES = struct.pack("<I", UASSET_MAGIC)


@dataclass
class PakEntry:
    """مدخل ملف واحد داخل حزمة .pak."""

    path: str
    offset: int
    compressed_size: int
    uncompressed_size: int
    compression_method: str
    encrypted: bool = False
    compression_block_size: int = 0
    compression_blocks: List[Tuple[int, int]] = field(default_factory=list)


@dataclass
class PakInfo:
    """ترويسة وفهرس حزمة Unreal .pak."""

    container_path: str
    version: int
    mount_point: str
    index_offset: int
    index_size: int
    encrypted_index: bool = False
    has_full_dir_index: bool = True
    raw_entry_count: int = 0
    compression_methods: List[str] = field(default_factory=list)
    entries: List[PakEntry] = field(default_factory=list)


def _normalize_internal_path(mount_point: str, dir_part: str, file_part: str = "") -> str:
    """توحيد المسار الداخلي للملف داخل الحزمة بصيغة موحدة (مثل Game/Content/...)."""
    parts: List[str] = []
    for raw in (mount_point, dir_part, file_part):
        cleaned = raw.replace("\\", "/").strip("/")
        while cleaned.startswith("../"):
            cleaned = cleaned[3:]
        if cleaned and cleaned not in (".", ".."):
            parts.append(cleaned)
    joined = "/".join(parts)
    # إزالة أية أجزاء ../ متبقية بعد الدمج
    norm_segs: List[str] = []
    for seg in joined.split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if norm_segs:
                norm_segs.pop()
            continue
        norm_segs.append(seg)
    return "/".join(norm_segs)


def _resolve_compression_name(
    version: int,
    method_idx: int,
    compression_methods: List[str],
) -> str:
    if method_idx == 0:
        return "None"
    if version < 8:
        legacy_map = {
            1: "Zlib",
            2: "Gzip",
            4: "Oodle",
        }
        return legacy_map.get(method_idx, f"Method_{method_idx}")
    slot = method_idx - 1
    if 0 <= slot < len(compression_methods) and compression_methods[slot]:
        return compression_methods[slot]
    return f"Method_{method_idx}"


def _read_unencoded_pak_entry(
    data: bytes,
    pos: int,
    version: int,
    compression_methods: List[str],
    has_relative_blocks: bool = True,
) -> Tuple[dict, int]:
    """قراءة سجل FPakEntry غير مُرمّز من الفهرس أو من الترويسة المحلية قبل البيانات."""
    offset, comp_size, uncomp_size, method_idx = struct.unpack_from("<qqqI", data, pos)
    pos += 28
    if version <= 1:
        pos += 8  # Timestamp
    pos += 20  # SHA-1 Hash

    blocks: List[Tuple[int, int]] = []
    if version >= 3 and method_idx != 0:
        block_count = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        for _ in range(block_count):
            b_start, b_end = struct.unpack_from("<qq", data, pos)
            pos += 16
            blocks.append((b_start, b_end))

    flags = data[pos]
    pos += 1
    block_size = struct.unpack_from("<I", data, pos)[0]
    pos += 4

    encrypted = bool(flags & 0x01)
    comp_name = _resolve_compression_name(version, method_idx, compression_methods)
    return {
        "offset": offset,
        "compressed_size": comp_size,
        "uncompressed_size": uncomp_size,
        "compression_method": comp_name,
        "encrypted": encrypted,
        "compression_block_size": block_size,
        "compression_blocks": blocks,
        "relative_blocks": has_relative_blocks,
    }, pos


def _locate_pak_footer(file_size: int, tail: bytes) -> Optional[Tuple[int, int]]:
    """تحديد موقع Magic في ذيل ملف .pak وحساب الإزاحة داخل tail."""
    candidate_offsets: List[int] = []
    for footer_len in (204, 172, 44):
        pos = len(tail) - footer_len
        if 0 <= pos <= len(tail) - 44:
            candidate_offsets.append(pos)

    # بحث احتياطي في آخر 512 بايت
    search_idx = len(tail) - 44
    while search_idx >= 0:
        found = tail.rfind(PAK_MAGIC_BYTES, 0, search_idx + 4)
        if found == -1:
            break
        if found not in candidate_offsets:
            candidate_offsets.append(found)
        search_idx = found - 1

    for pos in candidate_offsets:
        if pos + 44 > len(tail):
            continue
        magic, version, index_offset, index_size = struct.unpack_from("<IIqq", tail, pos)
        if magic != PAK_MAGIC:
            continue
        if not (1 <= version <= 15):
            continue
        if index_offset < 0 or index_size <= 0:
            continue
        if index_offset + index_size > file_size - 44:
            continue
        return pos, version
    return None


def parse_pak_header_and_entries(pak_path: str) -> Optional[PakInfo]:
    """قراءة ترويسة وفهرس حزمة .pak (الإصدارات 1 إلى 11) بدون تعديل الملف."""
    if not os.path.isfile(pak_path):
        return None
    try:
        file_size = os.path.getsize(pak_path)
    except OSError:
        return None
    if file_size < 44:
        return None

    try:
        with open(pak_path, "rb") as f:
            tail_len = min(512, file_size)
            f.seek(file_size - tail_len)
            tail = f.read(tail_len)
            located = _locate_pak_footer(file_size, tail)
            if located is None:
                return None
            magic_pos, version = located
            _, _, index_offset, index_size = struct.unpack_from("<IIqq", tail, magic_pos)

            encrypted_index = False
            if version >= 4 and magic_pos >= 1:
                encrypted_index = tail[magic_pos - 1] != 0

            compression_methods: List[str] = []
            if version >= 8:
                methods_start = magic_pos + 44
                slot_pos = methods_start
                while slot_pos + 32 <= len(tail):
                    slot_raw = tail[slot_pos : slot_pos + 32]
                    name = slot_raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
                    compression_methods.append(name)
                    slot_pos += 32

            if encrypted_index:
                return PakInfo(
                    container_path=pak_path,
                    version=version,
                    mount_point="",
                    index_offset=index_offset,
                    index_size=index_size,
                    encrypted_index=True,
                    compression_methods=compression_methods,
                    entries=[],
                )

            f.seek(index_offset)
            idx_bytes = f.read(index_size)
            if len(idx_bytes) < index_size:
                return None

            mount_point, pos = read_fstring(idx_bytes, 0)
            entry_count = struct.unpack_from("<i", idx_bytes, pos)[0]
            pos += 4

            has_full_dir_index_flag = True
            entries: List[PakEntry] = []
            if version < 10:
                for _ in range(max(0, entry_count)):
                    rel_name, pos = read_fstring(idx_bytes, pos)
                    meta, pos = _read_unencoded_pak_entry(
                        idx_bytes, pos, version, compression_methods
                    )
                    full_internal = _normalize_internal_path(mount_point, rel_name)
                    entries.append(
                        PakEntry(
                            path=full_internal,
                            offset=meta["offset"],
                            compressed_size=meta["compressed_size"],
                            uncompressed_size=meta["uncompressed_size"],
                            compression_method=meta["compression_method"],
                            encrypted=meta["encrypted"],
                            compression_block_size=meta["compression_block_size"],
                            compression_blocks=meta["compression_blocks"],
                        )
                    )
            else:
                # الإصدارات v10/v11 (PathHashIndex + FullDirectoryIndex + EncodedPakEntries)
                pos += 8  # PathHashSeed
                has_path_hash_index = struct.unpack_from("<i", idx_bytes, pos)[0]
                pos += 4
                if has_path_hash_index:
                    pos += 8 + 8 + 20  # Offset + Size + SHA1

                has_full_dir_index = struct.unpack_from("<i", idx_bytes, pos)[0]
                has_full_dir_index_flag = bool(has_full_dir_index)
                pos += 4
                full_dir_offset = 0
                full_dir_size = 0
                if has_full_dir_index:
                    full_dir_offset, full_dir_size = struct.unpack_from("<qq", idx_bytes, pos)
                    pos += 16 + 20  # Offset + Size + SHA1

                encoded_size = struct.unpack_from("<i", idx_bytes, pos)[0]
                pos += 4
                encoded_bytes = idx_bytes[pos : pos + max(0, encoded_size)]
                pos += max(0, encoded_size)

                non_encoded_entries: List[dict] = []
                if pos + 4 <= len(idx_bytes):
                    non_enc_count = struct.unpack_from("<i", idx_bytes, pos)[0]
                    pos += 4
                    for _ in range(max(0, non_enc_count)):
                        meta, pos = _read_unencoded_pak_entry(
                            idx_bytes, pos, version, compression_methods
                        )
                        non_encoded_entries.append(meta)

                if has_full_dir_index and full_dir_offset > 0 and full_dir_size > 0:
                    f.seek(full_dir_offset)
                    dir_bytes = f.read(full_dir_size)
                    dir_count = struct.unpack_from("<I", dir_bytes, 0)[0]
                    dpos = 4
                    for _ in range(dir_count):
                        dir_name, dpos = read_fstring(dir_bytes, dpos)
                        file_count = struct.unpack_from("<I", dir_bytes, dpos)[0]
                        dpos += 4
                        for _ in range(file_count):
                            file_name, dpos = read_fstring(dir_bytes, dpos)
                            entry_loc = struct.unpack_from("<i", dir_bytes, dpos)[0]
                            dpos += 4
                            full_internal = _normalize_internal_path(
                                mount_point, dir_name, file_name
                            )
                            if entry_loc >= 0:
                                meta = _decode_pak_entry_location(
                                    encoded_bytes,
                                    entry_loc,
                                    version,
                                    compression_methods,
                                )
                            else:
                                fallback_idx = -(entry_loc + 1)
                                if 0 <= fallback_idx < len(non_encoded_entries):
                                    meta = non_encoded_entries[fallback_idx]
                                else:
                                    continue
                            entries.append(
                                PakEntry(
                                    path=full_internal,
                                    offset=meta["offset"],
                                    compressed_size=meta["compressed_size"],
                                    uncompressed_size=meta["uncompressed_size"],
                                    compression_method=meta["compression_method"],
                                    encrypted=meta["encrypted"],
                                    compression_block_size=meta["compression_block_size"],
                                    compression_blocks=meta.get("compression_blocks", []),
                                )
                            )

            return PakInfo(
                container_path=pak_path,
                version=version,
                mount_point=mount_point,
                index_offset=index_offset,
                index_size=index_size,
                encrypted_index=False,
                has_full_dir_index=has_full_dir_index_flag,
                raw_entry_count=max(0, entry_count),
                compression_methods=compression_methods,
                entries=entries,
            )
    except Exception:
        return None


def _decode_pak_entry_location(
    encoded: bytes,
    offset_in_encoded: int,
    version: int,
    compression_methods: List[str],
) -> dict:
    """فك ترميز سجل FPakEntry مضغوط البتات (v10/v11)."""
    flags = struct.unpack_from("<I", encoded, offset_in_encoded)[0]
    epos = offset_in_encoded + 4

    is_offset_32 = bool(flags & (1 << 31))
    is_uncomp_32 = bool(flags & (1 << 30))
    is_size_32 = bool(flags & (1 << 29))
    comp_method_idx = (flags >> 23) & 0x3F
    encrypted = bool(flags & (1 << 22))
    block_count = (flags >> 6) & 0xFFFF
    block_size = (flags & 0x3F) << 11

    if is_offset_32:
        data_offset = struct.unpack_from("<I", encoded, epos)[0]
        epos += 4
    else:
        data_offset = struct.unpack_from("<q", encoded, epos)[0]
        epos += 8

    if is_uncomp_32:
        uncomp_size = struct.unpack_from("<I", encoded, epos)[0]
        epos += 4
    else:
        uncomp_size = struct.unpack_from("<q", encoded, epos)[0]
        epos += 8

    if comp_method_idx != 0:
        if is_size_32:
            comp_size = struct.unpack_from("<I", encoded, epos)[0]
            epos += 4
        else:
            comp_size = struct.unpack_from("<q", encoded, epos)[0]
            epos += 8
    else:
        comp_size = uncomp_size

    if block_count == 1:
        block_size = uncomp_size

    return {
        "offset": data_offset,
        "compressed_size": comp_size,
        "uncompressed_size": uncomp_size,
        "compression_method": _resolve_compression_name(
            version, comp_method_idx, compression_methods
        ),
        "encrypted": encrypted,
        "compression_block_size": block_size,
        "compression_blocks": [],
    }


_OODLE_DLL_CACHE: Dict[str, Optional[object]] = {}


def _find_oodle_dll(pak_path: str) -> Optional[str]:
    """البحث عن مكتبة oo2core_*_win64.dll أو win32.dll المرفقة في مجلد اللعبة لفك ضغط Oodle."""
    if not pak_path:
        return None
    is_64 = struct.calcsize("P") == 8
    arch_tok = "win64" if is_64 else "win32"

    cur = os.path.dirname(os.path.abspath(pak_path))
    roots: List[str] = []
    for _ in range(5):
        roots.append(cur)
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent

    for root in reversed(roots):
        for rel_dir in (
            "",
            os.path.join("Engine", "Binaries", "ThirdParty", "Oodle", "Win64" if is_64 else "Win32"),
            os.path.join("Engine", "Binaries", "Win64" if is_64 else "Win32"),
        ):
            probe = os.path.join(root, rel_dir) if rel_dir else root
            if not os.path.isdir(probe):
                continue
            try:
                for fname in sorted(os.listdir(probe)):
                    low = fname.lower()
                    if low.startswith("oo2core_") and low.endswith(".dll") and arch_tok in low:
                        return os.path.join(probe, fname)
            except OSError:
                continue
    return None


def _try_oodle_decompress(chunk: bytes, expected_size: int, pak_path: str) -> Optional[bytes]:
    """فك ضغط كتلة Oodle عبر oo2core_*.dll إن كانت مرفقة مع اللعبة."""
    if expected_size <= 0 or not pak_path:
        return None
    dll_path = _find_oodle_dll(pak_path)
    if not dll_path:
        return None
    try:
        import ctypes

        if dll_path not in _OODLE_DLL_CACHE:
            lib = ctypes.WinDLL(dll_path)
            fn = lib.OodleLZ_Decompress
            fn.argtypes = [
                ctypes.c_void_p, ctypes.c_longlong,
                ctypes.c_void_p, ctypes.c_longlong,
                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_void_p, ctypes.c_longlong,
                ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_void_p, ctypes.c_longlong,
                ctypes.c_int,
            ]
            fn.restype = ctypes.c_longlong
            _OODLE_DLL_CACHE[dll_path] = fn
        fn = _OODLE_DLL_CACHE[dll_path]
        if fn is None:
            return None

        src_buf = ctypes.create_string_buffer(chunk)
        dst_buf = ctypes.create_string_buffer(expected_size)
        decoded_len = fn(
            src_buf, len(chunk),
            dst_buf, expected_size,
            1, 0, 0,
            None, 0,
            None, None,
            None, 0,
            0,
        )
        if decoded_len == expected_size:
            return dst_buf.raw
    except Exception:
        return None
    return None


def _decompress_chunk(
    chunk: bytes,
    method: str,
    expected_size: int,
    pak_path: str = "",
) -> Optional[bytes]:
    """فك ضغط كتلة بيانات من .pak حسب الخوارزمية."""
    low = (method or "").strip().lower()
    if low in ("", "none"):
        return chunk
    if low == "zlib":
        try:
            return zlib.decompress(chunk)
        except zlib.error:
            return None
    if low == "gzip":
        try:
            return gzip.decompress(chunk)
        except OSError:
            try:
                return zlib.decompress(chunk, 16 + zlib.MAX_WBITS)
            except zlib.error:
                return None
    if low == "lz4":
        try:
            from engines.unity.assets_extractor import lz4_decompress_block

            return lz4_decompress_block(chunk, expected_size)
        except Exception:
            return None
    if low == "oodle":
        return _try_oodle_decompress(chunk, expected_size, pak_path)
    return None


def read_pak_entry_bytes(pak: PakInfo, entry: PakEntry) -> Optional[bytes]:
    """استخراج بايتات ملف واحد من داخل حزمة .pak دون تعديل الحزمة."""
    if entry.encrypted or pak.encrypted_index:
        return None
    try:
        with open(pak.container_path, "rb") as f:
            f.seek(entry.offset)
            # قراءة الترويسة المحلية قبل البيانات مباشرة
            header_buf = f.read(4096)
            if len(header_buf) < 53:
                return None
            local_meta, local_header_len = _read_unencoded_pak_entry(
                header_buf, 0, pak.version, pak.compression_methods
            )
            method = local_meta["compression_method"] or entry.compression_method
            if local_meta["encrypted"]:
                return None

            low_method = method.strip().lower()
            if low_method in ("", "none"):
                f.seek(entry.offset + local_header_len)
                return f.read(entry.uncompressed_size)

            blocks = local_meta["compression_blocks"] or entry.compression_blocks
            if not blocks:
                f.seek(entry.offset + local_header_len)
                compressed_blob = f.read(entry.compressed_size)
                return _decompress_chunk(
                    compressed_blob, method, entry.uncompressed_size, pak.container_path
                )

            out_chunks: List[bytes] = []
            remaining = entry.uncompressed_size
            block_cap = local_meta["compression_block_size"] or entry.compression_block_size or remaining
            for b_start, b_end in blocks:
                abs_start = (entry.offset + b_start) if pak.version >= 5 else b_start
                abs_end = (entry.offset + b_end) if pak.version >= 5 else b_end
                f.seek(abs_start)
                raw_block = f.read(max(0, abs_end - abs_start))
                expected = min(block_cap, remaining) if block_cap > 0 else remaining
                dec = _decompress_chunk(raw_block, method, expected, pak.container_path)
                if dec is None:
                    return None
                out_chunks.append(dec)
                remaining = max(0, remaining - len(dec))
            return b"".join(out_chunks)[: entry.uncompressed_size]
    except OSError:
        return None


def extract_pak_entry_by_path(
    pak_path: str,
    internal_path: str,
) -> Optional[Tuple[PakInfo, PakEntry, bytes]]:
    """قراءة واستخراج مدخل محدد بالاسم من حزمة .pak."""
    pak = parse_pak_header_and_entries(pak_path)
    if pak is None or pak.encrypted_index:
        return None
    target_norm = internal_path.replace("\\", "/").lstrip("/").lower()
    for entry in pak.entries:
        if entry.path.replace("\\", "/").lstrip("/").lower() == target_norm:
            data = read_pak_entry_bytes(pak, entry)
            if data is None:
                return None
            return pak, entry, data
    return None


def _pack_unencoded_pak_entry(
    offset: int,
    size: int,
    sha1_digest: bytes,
    version: int,
) -> bytes:
    """بناء سجل FPakEntry غير مضغوط متوافق مع v4–v8."""
    chunks = [struct.pack("<qqqI", offset, size, size, 0)]
    if version <= 1:
        chunks.append(struct.pack("<q", 0))
    chunks.append(sha1_digest)
    chunks.append(struct.pack("<BI", 0, 0))
    return b"".join(chunks)


def build_unreal_pak_blob(
    files: Dict[str, bytes],
    mount_point: str = "../../../",
    version: int = 8,
) -> bytes:
    """بناء حزمة Unreal .pak غير مضغوطة (لـ ~mods/*_P.pak) متوافقة مع UE4 و UE5."""
    pak_ver = version if version in (3, 4, 5, 7, 8, 9) else 8
    chunks: List[bytes] = []
    cursor = 0
    index_records: List[Tuple[str, int, int, bytes]] = []

    norm_mount = mount_point or "../../../"
    if not norm_mount.endswith("/"):
        norm_mount += "/"

    for rel_path in sorted(files.keys()):
        payload = files[rel_path]
        clean_rel = rel_path.replace("\\", "/").lstrip("/")
        sha1 = hashlib.sha1(payload).digest()
        local_hdr = _pack_unencoded_pak_entry(0, len(payload), sha1, pak_ver)
        entry_offset = cursor
        chunks.append(local_hdr)
        chunks.append(payload)
        cursor += len(local_hdr) + len(payload)
        index_records.append((clean_rel, entry_offset, len(payload), sha1))

    index_offset = cursor
    idx_chunks: List[bytes] = [
        write_fstring(norm_mount),
        struct.pack("<I", len(index_records)),
    ]
    for rel_path, entry_offset, payload_len, sha1 in index_records:
        idx_chunks.append(write_fstring(rel_path))
        idx_chunks.append(_pack_unencoded_pak_entry(entry_offset, payload_len, sha1, pak_ver))

    index_bytes = b"".join(idx_chunks)
    index_hash = hashlib.sha1(index_bytes).digest()
    chunks.append(index_bytes)

    footer_chunks: List[bytes] = []
    if pak_ver >= 7:
        footer_chunks.append(b"\x00" * 16)  # EncryptionKeyGuid
    if pak_ver >= 4:
        footer_chunks.append(b"\x00")  # bEncryptedIndex = 0
    footer_chunks.append(
        struct.pack("<IIqq", PAK_MAGIC, pak_ver, index_offset, len(index_bytes))
    )
    footer_chunks.append(index_hash)
    if pak_ver >= 8:
        footer_chunks.append(b"\x00" * (5 * 32))  # 5 compression method slots

    chunks.extend(footer_chunks)
    return b"".join(chunks)


@dataclass
class UtocInfo:
    """معلومات ترويسة وفهرس حاوية IoStore (.utoc) في Unreal Engine 5 / UE4.26+."""

    container_path: str
    valid_magic: bool
    header_complete: bool
    version: int = 0
    toc_entry_count: int = 0
    compressed_block_count: int = 0
    directory_index_size: int = 0
    container_flags: int = 0
    encrypted: bool = False
    compressed: bool = False
    signed: bool = False
    indexed: bool = False
    compression_methods: List[str] = field(default_factory=list)
    detected_locres_paths: List[str] = field(default_factory=list)
    detected_string_tables: List[str] = field(default_factory=list)
    detected_font_assets: List[str] = field(default_factory=list)


_UTOC_TOKEN_RE = re.compile(rb"[A-Za-z0-9_./\\-]{4,240}")
_KNOWN_COMP_METHODS = ("Zlib", "Oodle", "LZ4", "Gzip", "Zstd")


def _scan_utoc_plaintext_hints(blob: bytes, info: UtocInfo) -> None:
    """مسح المقاطع النصية غير المشفرة في .utoc لاكتشاف طرق الضغط و.locres وStringTable والخطوط."""
    seen_locres = set()
    seen_st = set()
    seen_fonts = set()
    seen_comp = set(m.lower() for m in info.compression_methods)

    for m in _UTOC_TOKEN_RE.finditer(blob):
        tok = m.group(0).decode("ascii", errors="ignore").replace("\\", "/")
        low = tok.lower()
        for cname in _KNOWN_COMP_METHODS:
            if low == cname.lower() and low not in seen_comp:
                seen_comp.add(low)
                info.compression_methods.append(cname)
        if low.endswith(".locres") or "/localization/" in low:
            if low not in seen_locres:
                seen_locres.add(low)
                info.detected_locres_paths.append(tok)
        if "stringtable" in low or os.path.basename(low).startswith("st_"):
            if low not in seen_st:
                seen_st.add(low)
                info.detected_string_tables.append(tok)
        if any(fk in low for fk in ("ufont", "fontface", "noto", "arabic", "roboto", "droidsans", "ibmplex")) or low.endswith((".ttf", ".otf")):
            if low not in seen_fonts:
                seen_fonts.add(low)
                info.detected_font_assets.append(tok)


def parse_utoc_header(utoc_path: str) -> Optional[UtocInfo]:
    """تحليل ترويسة FIoStoreTocHeader لحاوية .utoc دون الحاجة لفك تشفير الحزم."""
    if not os.path.isfile(utoc_path):
        return None
    try:
        file_size = os.path.getsize(utoc_path)
        if file_size < 16:
            return None
        with open(utoc_path, "rb") as f:
            head = f.read(min(file_size, 4 * 1024 * 1024))
    except OSError:
        return None

    if head[:16] != UTOC_MAGIC:
        return None

    if len(head) < 144:
        return UtocInfo(
            container_path=utoc_path,
            valid_magic=True,
            header_complete=False,
        )

    version = head[16]
    (
        toc_header_size,
        toc_entry_count,
        compressed_block_count,
        compressed_block_entry_size,
        comp_name_count,
        comp_name_len,
        _comp_block_size,
        dir_index_size,
        _partition_count,
    ) = struct.unpack_from("<9I", head, 20)
    container_flags = head[80]
    compressed = bool(container_flags & 0x01)
    encrypted = bool(container_flags & 0x02)
    signed = bool(container_flags & 0x04)
    indexed = bool(container_flags & 0x08)

    perfect_hash_seeds_count = struct.unpack_from("<I", head, 84)[0]
    chunks_without_hash_count = struct.unpack_from("<I", head, 96)[0]

    info = UtocInfo(
        container_path=utoc_path,
        valid_magic=True,
        header_complete=True,
        version=version,
        toc_entry_count=toc_entry_count,
        compressed_block_count=compressed_block_count,
        directory_index_size=dir_index_size,
        container_flags=container_flags,
        encrypted=encrypted,
        compressed=compressed,
        signed=signed,
        indexed=indexed,
    )

    hdr_size = toc_header_size if 96 <= toc_header_size <= 256 else 144
    block_entry_sz = compressed_block_entry_size if 8 <= compressed_block_entry_size <= 32 else 12
    comp_offset = hdr_size + (toc_entry_count * 12) + (toc_entry_count * 10)
    if version >= 5:
        comp_offset += (perfect_hash_seeds_count + chunks_without_hash_count) * 4
    comp_offset += compressed_block_count * block_entry_sz

    if 0 < comp_name_count <= 32 and 8 <= comp_name_len <= 64:
        end_comp = comp_offset + comp_name_count * comp_name_len
        if end_comp <= len(head):
            for idx in range(comp_name_count):
                raw_name = head[comp_offset + idx * comp_name_len : comp_offset + (idx + 1) * comp_name_len]
                cname = raw_name.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
                if cname and cname.isprintable():
                    info.compression_methods.append(cname)

    if not encrypted:
        scan_start = min(len(head), comp_offset) if comp_offset < len(head) else 144
        _scan_utoc_plaintext_hints(head[scan_start:], info)

    return info


def build_utoc_blob(
    toc_entry_count: int = 2,
    compression_methods: Optional[List[str]] = None,
    directory_paths: Optional[List[str]] = None,
    version: int = 5,
    compressed: bool = True,
    encrypted: bool = False,
    signed: bool = False,
    indexed: bool = True,
) -> bytes:
    """بناء حاوية .utoc قياسية بترويسة FIoStoreTocHeader (144 بايت) لأغراض الاختبار."""
    methods = list(compression_methods or [])
    comp_name_len = 32
    compressed_block_count = max(1, toc_entry_count) if compressed else 0
    flags = (
        (0x01 if compressed else 0)
        | (0x02 if encrypted else 0)
        | (0x04 if signed else 0)
        | (0x08 if indexed or directory_paths else 0)
    )

    dir_bytes = b""
    if directory_paths:
        dir_chunks = [write_fstring("../../../"), struct.pack("<I", len(directory_paths))]
        for p in directory_paths:
            dir_chunks.append(write_fstring(p))
        dir_bytes = b"".join(dir_chunks)

    hdr = bytearray(144)
    hdr[0:16] = UTOC_MAGIC
    hdr[16] = version & 0xFF
    struct.pack_into(
        "<9I",
        hdr,
        20,
        144,  # TocHeaderSize
        toc_entry_count,
        compressed_block_count,
        12,   # TocCompressedBlockEntrySize
        len(methods),
        comp_name_len,
        65536,
        len(dir_bytes),
        1,    # PartitionCount
    )
    struct.pack_into("<Q", hdr, 56, 0x123456789ABCDEF0)  # ContainerId
    hdr[80] = flags

    body = bytearray()
    body.extend(b"\x00" * (toc_entry_count * 12))  # FIoChunkId
    body.extend(b"\x00" * (toc_entry_count * 10))  # FIoOffsetAndLength
    body.extend(b"\x00" * (compressed_block_count * 12))  # FIoStoreTocCompressedBlockEntry
    for m in methods:
        body.extend(m.encode("ascii", errors="ignore")[:comp_name_len].ljust(comp_name_len, b"\x00"))
    body.extend(dir_bytes)
    return bytes(hdr) + bytes(body)


@dataclass
class UAssetInspection:
    """نتيجة الفحص الثنائي لأصل Unreal (.uasset / .uexp)."""

    is_uasset: bool
    asset_kind: str = "Other"
    names: List[str] = field(default_factory=list)
    string_table_namespace: str = ""
    string_table_entries: List[Tuple[str, str]] = field(default_factory=list)
    font_references: List[str] = field(default_factory=list)


def _extract_length_prefixed_names(blob: bytes, limit: int = 512 * 1024) -> List[str]:
    """استخراج السلاسل النصية المسبوقة بطول int32 والمنتهية بـ NULL من أصل .uasset ثنائي."""
    data = blob[:limit]
    n = len(data)
    names: List[str] = []
    seen = set()
    i = 4
    while i + 5 <= n:
        slen = struct.unpack_from("<i", data, i)[0]
        if 2 <= slen <= 220 and i + 4 + slen <= n and data[i + 4 + slen - 1] == 0:
            raw = data[i + 4 : i + 4 + slen - 1]
            # التحقق من أن كل البايتات مقروءة ASCII دون رموز تحكم غريبة
            if all(0x20 <= b <= 0x7E for b in raw):
                s = raw.decode("ascii")
                if s not in seen:
                    seen.add(s)
                    names.append(s)
                i += 4 + slen
                continue
        i += 1
    return names


def _try_extract_fstring_table(blob: bytes) -> Tuple[str, List[Tuple[str, str]]]:
    """استخراج جدول FStringTable الثنائي (Namespace + Count + أزواج Key/Value) من .uexp أو .uasset."""
    n = len(blob)
    best_ns = ""
    best_entries: List[Tuple[str, str]] = []
    scan_limit = min(n, 256 * 1024)

    for off in range(0, max(0, scan_limit - 12)):
        try:
            ns, p1 = read_fstring(blob, off)
        except Exception:
            continue
        if not (1 <= len(ns) <= 128) or any(ord(ch) < 0x20 for ch in ns):
            continue
        if p1 + 4 > n:
            continue
        count = struct.unpack_from("<i", blob, p1)[0]
        if not (1 <= count <= 10000):
            continue
        pos = p1 + 4
        parsed: List[Tuple[str, str]] = []
        valid = True
        for _ in range(count):
            if pos + 8 > n:
                valid = False
                break
            try:
                key, pos = read_fstring(blob, pos)
                val, pos = read_fstring(blob, pos)
            except Exception:
                valid = False
                break
            if not key or len(key) > 256 or any(ord(ch) < 0x20 for ch in key):
                valid = False
                break
            if not val:
                valid = False
                break
            parsed.append((key, val))
        if valid and len(parsed) > len(best_entries):
            best_ns = ns
            best_entries = parsed
            # إذا استهلك الجدول معظم .uexp (أو وصل لذيل Magic)، نتوقف فوراً
            if pos >= n - 16:
                break

    return best_ns, best_entries


def inspect_uasset_bytes(
    uasset_bytes: bytes,
    uexp_bytes: Optional[bytes] = None,
) -> UAssetInspection:
    """فحص أصل .uasset (مع .uexp إن وُجد) لكشف UStringTable وUDataTable وUFontFace."""
    if len(uasset_bytes) < 24 or struct.unpack_from("<I", uasset_bytes, 0)[0] != UASSET_MAGIC:
        return UAssetInspection(is_uasset=False)

    names = _extract_length_prefixed_names(uasset_bytes)
    low_names = [nm.lower() for nm in names]
    combined_low = " ".join(low_names)

    asset_kind = "Other"
    if "stringtable" in low_names or "ustringtable" in low_names or "fstringtable" in combined_low:
        asset_kind = "StringTable"
    elif "datatable" in low_names or "udatatable" in low_names:
        asset_kind = "DataTable"
    elif any(k in low_names for k in ("font", "ufont", "fontface", "ufontface")) or any(
        nm.lower().endswith((".ttf", ".otf")) for nm in names
    ):
        asset_kind = "Font"

    st_ns = ""
    st_entries: List[Tuple[str, str]] = []
    if asset_kind == "StringTable":
        if uexp_bytes:
            st_ns, st_entries = _try_extract_fstring_table(uexp_bytes)
        if not st_entries:
            st_ns, st_entries = _try_extract_fstring_table(uasset_bytes)

    font_refs: List[str] = []
    if asset_kind == "Font":
        for nm in names:
            low_nm = nm.lower()
            if (
                low_nm.endswith((".ttf", ".otf"))
                or any(tok in low_nm for tok in ("noto", "arabic", "roboto", "droid", "tahoma", "arial", "ibmplex"))
            ):
                font_refs.append(nm)

    return UAssetInspection(
        is_uasset=True,
        asset_kind=asset_kind,
        names=names,
        string_table_namespace=st_ns,
        string_table_entries=st_entries,
        font_references=font_refs,
    )


def build_mock_ustringtable_uasset_uexp(
    namespace: str,
    entries: List[Tuple[str, str]],
) -> Tuple[bytes, bytes]:
    """بناء زوج .uasset/.uexp اختباري لأصل UStringTable بعلامة 0x9E2A83C1."""
    name_list = ["/Script/Engine", "StringTable", namespace, "None"]
    uasset_parts = [
        UASSET_MAGIC_BYTES,
        struct.pack("<iIII", -7, 0, 522, 0),
        struct.pack("<I", len(name_list)),
    ]
    for nm in name_list:
        uasset_parts.append(write_fstring(nm))
        uasset_parts.append(struct.pack("<HH", 0, 0))

    uexp_parts = [
        b"\x03\x00\x00\x00\x00\x00\x00\x00",  # None FName
        write_fstring(namespace),
        struct.pack("<i", len(entries)),
    ]
    for k, v in entries:
        uexp_parts.append(write_fstring(k))
        uexp_parts.append(write_fstring(v))
    uexp_parts.append(UASSET_MAGIC_BYTES)

    return b"".join(uasset_parts), b"".join(uexp_parts)
