# -*- coding: utf-8 -*-
"""محلل وكاتب ملفات CSV و TSV (بما فيها الجداول ثنائية العمود وجداول اللغات المتعددة مثل I2 Localization)."""

import copy
import csv
import io
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


# أسماء أعمدة اللغة الإنجليزية حسب أولوية الاختيار
EXPLICIT_ENGLISH_COLS = (
    "english",
    "english [en]",
    "en-us",
    "en_us",
    "en-gb",
    "en_gb",
    "en",
)
GENERIC_VALUE_COLS = (
    "source",
    "default",
    "translation",
    "text",
    "value",
)
ARABIC_COL_NAMES = {"arabic", "arabic [ar]", "ar", "ar-sa", "ar_sa"}
TECH_COL_NAMES = {
    "---",
    "rowname",
    "row_name",
    "key",
    "keys",
    "id",
    "term",
    "msgid",
    "type",
    "desc",
    "context",
    "category",
    "comment",
    "notes",
    "tag",
}
HEADER_HINT_TEXT_COLS = {
    "text",
    "dialogue",
    "dialog",
    "subtitle",
    "subtitles",
    "displayname",
    "display_name",
    "description",
    "title",
    "speaker",
    "prompt",
    "message",
    "caption",
    "body",
    "value",
    "english",
    "en",
    "source",
    "translation",
}
SIBLING_LANG_COLS = {
    "french",
    "german",
    "spanish",
    "russian",
    "italian",
    "portuguese",
    "japanese",
    "chinese",
    "korean",
    "polish",
    "turkish",
    "dutch",
    "arabic",
    "ukrainian",
    "czech",
    "hungarian",
    "swedish",
    "norwegian",
    "danish",
    "finnish",
    "greek",
    "thai",
    "vietnamese",
    "indonesian",
    "brazilian",
    "fr",
    "de",
    "es",
    "ru",
    "it",
    "pt",
    "ja",
    "zh",
    "ko",
    "pl",
    "tr",
    "nl",
    "ar",
}

HEADER_BRACKET_RE = re.compile(r"\s*[\[(][^)\]]*[\])]")


def _norm_col(name: str) -> str:
    return name.strip().lower()


def _strip_col_tag(name: str) -> str:
    return HEADER_BRACKET_RE.sub("", name).strip().lower()


def _detect_delimiter(raw_text: str, ext: str) -> str:
    """تحديد الفاصل المستخدم في CSV/TSV بدقة دون الارتباك من الفواصل داخل علامات التنصيص."""
    if ext == ".tsv":
        return "\t"

    sample_lines = [ln for ln in raw_text.splitlines()[:12] if ln.strip()]
    if not sample_lines:
        return ","

    candidates = (",", ";", "\t")
    best_delim = ","
    best_score = -1

    for delim in candidates:
        counts: List[int] = []
        for ln in sample_lines:
            in_quotes = False
            cnt = 0
            for ch in ln:
                if ch == '"':
                    in_quotes = not in_quotes
                elif ch == delim and not in_quotes:
                    cnt += 1
            counts.append(cnt)
        if not counts or max(counts) == 0:
            continue
        positive = [c for c in counts if c > 0]
        if len(positive) == len(counts) and len(set(counts)) == 1:
            score = 1000 + counts[0]
        else:
            score = sum(counts)
        if score > best_score:
            best_score = score
            best_delim = delim

    return best_delim


def _analyze_header(rows: List[List[str]]) -> Tuple[int, int, List[int], Optional[int]]:
    """تحليل صف الرأس وتحديد صف البداية وعمود المفتاح والأعمدة المستهدفة وعمود العربية."""
    if not rows:
        return 0, 0, [], None

    header = rows[0]
    normed = [_norm_col(c) for c in header]
    stripped = [_strip_col_tag(c) for c in header]

    arabic_col_idx: Optional[int] = None
    for idx, (n, s) in enumerate(zip(normed, stripped)):
        if n in ARABIC_COL_NAMES or s in ("arabic", "ar"):
            arabic_col_idx = idx
            break

    has_tech_col = any(n in TECH_COL_NAMES for n in normed) or (
        len(normed) >= 2
        and normed[0] in ("name", "label")
        and any(n in HEADER_HINT_TEXT_COLS or s in HEADER_HINT_TEXT_COLS for n, s in zip(normed[1:], stripped[1:]))
    )
    has_sibling_col = any(
        n in SIBLING_LANG_COLS or s in SIBLING_LANG_COLS
        for n, s in zip(normed, stripped)
    )

    eng_col_idx: Optional[int] = None
    for target in EXPLICIT_ENGLISH_COLS:
        for idx, (n, s) in enumerate(zip(normed, stripped)):
            if n == target or s == target:
                eng_col_idx = idx
                break
        if eng_col_idx is not None:
            break

    if eng_col_idx is None:
        for target in GENERIC_VALUE_COLS:
            for idx, n in enumerate(normed):
                if n == target:
                    eng_col_idx = idx
                    break
            if eng_col_idx is not None:
                break

    key_col_idx = 0
    for idx, n in enumerate(normed):
        if n in ("---", "rowname", "row_name", "key", "keys", "id", "term", "msgid", "name"):
            key_col_idx = idx
            break

    # حالة 1: عمود لغة إنجليزية/قيمة صريح مع أعمدة لغات شقيقة أو أعمدة تقنية
    if eng_col_idx is not None and (has_tech_col or has_sibling_col):
        return 1, key_col_idx, [eng_col_idx], arabic_col_idx

    # حالة 2: صف رأس تقني واضح بدون عمود إنجليزية صريح (مثل Key, Col1, Col2)
    if has_tech_col and len(header) >= 2:
        target_cols = [
            idx
            for idx, (n, s) in enumerate(zip(normed, stripped))
            if idx != key_col_idx
            and n not in TECH_COL_NAMES
            and n not in SIBLING_LANG_COLS
            and s not in SIBLING_LANG_COLS
        ]
        if not target_cols:
            target_cols = [idx for idx in range(len(header)) if idx != key_col_idx]
        return 1, key_col_idx, target_cols, arabic_col_idx

    # حالة 3: ملف بدون رأس لغات متعدد، العمود 0 هو المفتاح والبقية نصوص
    if len(header) == 1:
        return 0, -1, [0], None

    max_cols = max((len(r) for r in rows), default=len(header))
    return 0, 0, list(range(1, max_cols)), None


class CsvFormatHandler(FormatHandler):
    """محلل وكاتب صيغتي CSV و TSV مع دعم الجداول متعددة اللغات."""

    format_names = ("csv", "tsv")

    def load(self, file_path: str) -> ParsedDocument:
        ext = os.path.splitext(file_path)[1].lower()
        fmt = "tsv" if ext == ".tsv" else "csv"

        raw_text, detected_enc = read_text_file_with_encoding(file_path)

        lineterminator = "\r\n" if "\r\n" in raw_text else "\n"
        delimiter = _detect_delimiter(raw_text, ext)
        reader = csv.reader(io.StringIO(raw_text, newline=""), delimiter=delimiter)
        rows: List[List[str]] = [row for row in reader]

        entries: List[Tuple[PathTuple, str, str]] = []
        if not rows:
            return ParsedDocument(
                format_name=fmt,
                raw_data={
                    "rows": rows,
                    "delimiter": delimiter,
                    "lineterminator": lineterminator,
                    "arabic_col_idx": None,
                    "encoding": detected_enc,
                },
                entries=entries,
            )

        start_row, key_col_idx, target_cols, arabic_col_idx = _analyze_header(rows)
        header = rows[0] if start_row == 1 else []

        for row_idx in range(start_row, len(rows)):
            row = rows[row_idx]
            if not row:
                continue
            row_key = (
                row[key_col_idx].strip()
                if 0 <= key_col_idx < len(row) and row[key_col_idx].strip()
                else f"row_{row_idx}"
            )
            for col_idx in target_cols:
                if col_idx >= len(row):
                    continue
                val = row[col_idx]
                if not val:
                    continue
                col_label = (
                    header[col_idx].strip()
                    if col_idx < len(header) and header[col_idx].strip()
                    else f"col_{col_idx}"
                )
                flat_key = f"{row_key}.{col_label}"
                entries.append(((row_idx, col_idx), flat_key, val))

        return ParsedDocument(
            format_name=fmt,
            raw_data={
                "rows": rows,
                "delimiter": delimiter,
                "lineterminator": lineterminator,
                "arabic_col_idx": arabic_col_idx,
                "encoding": detected_enc,
            },
            entries=entries,
        )

    def save(
        self,
        file_path: str,
        doc: ParsedDocument,
        replacements: Dict[PathTuple, str],
    ) -> None:
        raw = doc.raw_data
        rows_copy: List[List[str]] = copy.deepcopy(raw["rows"])
        delimiter: str = raw["delimiter"]
        lineterminator: str = raw["lineterminator"]
        arabic_col_idx: Optional[int] = raw.get("arabic_col_idx")
        out_enc = resolve_output_encoding(str(raw.get("encoding") or "utf-8"))

        for path_tuple, new_val in replacements.items():
            if len(path_tuple) != 2:
                continue
            row_idx, col_idx = int(path_tuple[0]), int(path_tuple[1])
            if not (0 <= row_idx < len(rows_copy)):
                continue
            row = rows_copy[row_idx]
            while len(row) <= col_idx:
                row.append("")
            row[col_idx] = new_val
            if arabic_col_idx is not None and arabic_col_idx != col_idx:
                while len(row) <= arabic_col_idx:
                    row.append("")
                row[arabic_col_idx] = new_val

        parent = os.path.dirname(os.path.abspath(file_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = file_path + ".tmp"

        with open(tmp_path, "w", encoding=out_enc, newline="") as f:
            writer = csv.writer(
                f, delimiter=delimiter, lineterminator=lineterminator
            )
            writer.writerows(rows_copy)
            f.flush()
            os.fsync(f.fileno())

        verify_enc = out_enc if out_enc.startswith("utf-16") else "utf-8-sig"
        with open(tmp_path, "r", encoding=verify_enc, newline="") as f:
            reloaded = list(csv.reader(f, delimiter=delimiter))
        if len(reloaded) != len(rows_copy):
            raise ValueError(
                f"فشل التحقق من سلامة CSV بعد الكتابة: عدد الصفوف ({len(reloaded)} != {len(rows_copy)})."
            )

        os.replace(tmp_path, file_path)
