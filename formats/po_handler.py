# -*- coding: utf-8 -*-
"""محلل وكاتب ملفات GNU Gettext (.po) مع دعم الأسطر المتعددة وحفظ الترويسة والتعليقات."""

import os
import re
from typing import Dict, List, Optional, Tuple

from formats.base import (
    FormatHandler,
    ParsedDocument,
    PathTuple,
    read_text_file_with_encoding,
    resolve_output_encoding,
)


ARABIC_ANY_RE = re.compile(r"[\u0600-\u06FF\uFE70-\uFEFF]")
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")


def _unescape_po_literal(quoted: str) -> str:
    """فك تهريب سلسلة محاطة بعلامتي تنصيص في صيغة PO دون تشويه UTF-8."""
    s = quoted.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    out: List[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "r":
                out.append("\r")
            elif nxt == '"':
                out.append('"')
            elif nxt == "\\":
                out.append("\\")
            else:
                out.append("\\" + nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _escape_po_segment(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "")
    )


def _format_po_msgstr(text: str, eol: str, keyword: str = "msgstr") -> List[str]:
    """تنسيق قيمة msgstr بصيغة PO القياسية (سطر واحد أو عدة أسطر)."""
    if "\n" not in text:
        return [f'{keyword} "{_escape_po_segment(text)}"{eol}']

    parts = text.split("\n")
    out: List[str] = [f'{keyword} ""{eol}']
    for idx, part in enumerate(parts):
        is_last = idx == len(parts) - 1
        if is_last and part == "":
            continue
        escaped = _escape_po_segment(part)
        suffix = "\\n" if not is_last else ""
        out.append(f'"{escaped}{suffix}"{eol}')
    return out


def _parse_po_lines(raw_lines: List[str]) -> List[Dict]:
    """تحليل أسطر ملف PO إلى كتل تحتوي القيم ومواقع msgstr الدقيقة في الملف."""
    blocks: List[Dict] = []
    cur: Optional[Dict] = None
    active_field: Optional[str] = None

    def _new_block() -> Dict:
        return {
            "msgctxt": None,
            "msgid": None,
            "msgstr": None,
            "msgstr_keyword": "msgstr",
            "msgstr_start": None,
            "msgstr_end": None,
        }

    def _flush() -> None:
        nonlocal cur, active_field
        if cur is not None and cur["msgid"] is not None:
            blocks.append(cur)
        cur = None
        active_field = None

    for idx, full_line in enumerate(raw_lines):
        stripped = full_line.strip()
        if not stripped:
            _flush()
            continue
        if stripped.startswith("#"):
            if cur is not None and cur["msgid"] is not None:
                _flush()
            continue

        if stripped.startswith("msgctxt "):
            if cur is not None and cur["msgid"] is not None:
                _flush()
            if cur is None:
                cur = _new_block()
            cur["msgctxt"] = _unescape_po_literal(stripped[len("msgctxt ") :])
            active_field = "msgctxt"
        elif stripped.startswith("msgid "):
            if cur is not None and cur["msgid"] is not None:
                _flush()
            if cur is None:
                cur = _new_block()
            cur["msgid"] = _unescape_po_literal(stripped[len("msgid ") :])
            active_field = "msgid"
        elif stripped.startswith("msgid_plural "):
            if cur is None:
                cur = _new_block()
            active_field = "msgid_plural"
        elif stripped.startswith("msgstr ") or stripped.startswith("msgstr[0] "):
            if cur is None:
                cur = _new_block()
            if stripped.startswith("msgstr[0] "):
                kw = "msgstr[0]"
                rest = stripped[len("msgstr[0] ") :]
            else:
                kw = "msgstr"
                rest = stripped[len("msgstr ") :]
            cur["msgstr_keyword"] = kw
            cur["msgstr"] = _unescape_po_literal(rest)
            cur["msgstr_start"] = idx
            cur["msgstr_end"] = idx + 1
            active_field = "msgstr"
        elif stripped.startswith("msgstr["):
            active_field = "msgstr_other"
        elif stripped.startswith('"') and cur is not None and active_field:
            piece = _unescape_po_literal(stripped)
            if active_field == "msgctxt":
                cur["msgctxt"] = (cur["msgctxt"] or "") + piece
            elif active_field == "msgid":
                cur["msgid"] = (cur["msgid"] or "") + piece
            elif active_field == "msgstr":
                cur["msgstr"] = (cur["msgstr"] or "") + piece
                cur["msgstr_end"] = idx + 1

    _flush()
    return blocks


class PoFormatHandler(FormatHandler):
    """محلل وكاتب ملفات الترجمة بصيغة GNU Gettext (.po / .pot)."""

    format_names = ("po", "pot")

    def load(self, file_path: str) -> ParsedDocument:
        raw_text, detected_enc = read_text_file_with_encoding(file_path)

        eol = "\r\n" if "\r\n" in raw_text else "\n"
        raw_lines = raw_text.splitlines(keepends=True)
        blocks = _parse_po_lines(raw_lines)

        entries: List[Tuple[PathTuple, str, str]] = []
        for block_idx, blk in enumerate(blocks):
            msgid = blk.get("msgid")
            msgstr = blk.get("msgstr")
            msgctxt = blk.get("msgctxt")
            # تخطي كتلة الترويسة الأولى (msgid "") أو الكتل التي لا تحتوي msgstr
            if msgid is None or msgid == "" or blk.get("msgstr_start") is None:
                continue

            # اختيار المصدر الأنسب: msgstr إن كانت غير فارغة وإنجليزية غير معربة، وإلا msgid
            if (
                msgstr
                and msgstr.strip()
                and not ARABIC_ANY_RE.search(msgstr)
                and ASCII_LETTER_RE.search(msgstr)
            ):
                source_val = msgstr
            else:
                source_val = msgid

            flat_key = f"{msgctxt}.{msgid}" if msgctxt else msgid
            entries.append(((block_idx,), flat_key, source_val))

        return ParsedDocument(
            format_name="po",
            raw_data={"lines": raw_lines, "blocks": blocks, "eol": eol, "encoding": detected_enc},
            entries=entries,
        )

    def save(
        self,
        file_path: str,
        doc: ParsedDocument,
        replacements: Dict[PathTuple, str],
    ) -> None:
        orig_lines: List[str] = doc.raw_data["lines"]
        blocks: List[Dict] = doc.raw_data["blocks"]
        eol: str = doc.raw_data["eol"]
        out_enc = resolve_output_encoding(str(doc.raw_data.get("encoding") or "utf-8"))

        # بناء خريطة تعديلات نطاقات الأسطر لكل كتلة مع مراعاة ترتيب الإزاحات من الأسفل للأعلى
        edits: List[Tuple[int, int, List[str]]] = []
        for path_tuple, new_val in replacements.items():
            if not path_tuple:
                continue
            block_idx = int(path_tuple[0])
            if not (0 <= block_idx < len(blocks)):
                continue
            blk = blocks[block_idx]
            start = blk.get("msgstr_start")
            end = blk.get("msgstr_end")
            if start is None or end is None:
                continue
            kw = blk.get("msgstr_keyword") or "msgstr"
            formatted_lines = _format_po_msgstr(new_val, eol, keyword=kw)
            edits.append((start, end, formatted_lines))

        new_lines = list(orig_lines)
        for start, end, formatted_lines in sorted(edits, key=lambda x: x[0], reverse=True):
            new_lines[start:end] = formatted_lines

        parent = os.path.dirname(os.path.abspath(file_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = file_path + ".tmp"

        with open(tmp_path, "w", encoding=out_enc, newline="") as f:
            f.write("".join(new_lines))
            f.flush()
            os.fsync(f.fileno())

        verify_enc = out_enc if out_enc.startswith("utf-16") else "utf-8-sig"
        with open(tmp_path, "r", encoding=verify_enc, newline="") as f:
            reloaded_blocks = _parse_po_lines(f.read().splitlines(keepends=True))
        if len(reloaded_blocks) != len(blocks):
            raise ValueError(
                f"فشل التحقق من سلامة PO بعد الكتابة: عدد الكتل ({len(reloaded_blocks)} != {len(blocks)})."
            )

        os.replace(tmp_path, file_path)
