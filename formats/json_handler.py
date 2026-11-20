# -*- coding: utf-8 -*-
"""قارئ وكاتب متسامح لملفات JSON/JSONC يحفظ ترتيب المفاتيح ويعالج التعليقات والفواصل الزائدة."""

import copy
import json
import os
from typing import Any, Dict, List, Tuple

from formats.base import (
    FormatHandler,
    ParsedDocument,
    PathTuple,
    read_text_file_with_encoding,
)


def clean_jsonc(raw_text: str) -> str:
    """تنظيف BOM والتعليقات (// و /* */) والفواصل الزائدة خارج السلاسل النصية."""
    if raw_text.startswith("\ufeff"):
        raw_text = raw_text.lstrip("\ufeff")

    out: List[str] = []
    i = 0
    n = len(raw_text)
    in_string = False

    # المرحلة 1: إزالة التعليقات مع احترام النصوص المحاطة بعلامات تنصيص
    while i < n:
        ch = raw_text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(raw_text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif ch == "/" and i + 1 < n and raw_text[i + 1] == "/":
            i += 2
            while i < n and raw_text[i] not in ("\r", "\n"):
                i += 1
        elif ch == "/" and i + 1 < n and raw_text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (raw_text[i] == "*" and raw_text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(ch)
            i += 1

    no_comments = "".join(out)

    # المرحلة 2: إزالة الفواصل الزائدة قبل } أو ] خارج السلاسل النصية
    cleaned: List[str] = []
    j = 0
    m = len(no_comments)
    in_string = False
    while j < m:
        ch = no_comments[j]
        if in_string:
            cleaned.append(ch)
            if ch == "\\" and j + 1 < m:
                cleaned.append(no_comments[j + 1])
                j += 2
                continue
            if ch == '"':
                in_string = False
            j += 1
            continue

        if ch == '"':
            in_string = True
            cleaned.append(ch)
            j += 1
        elif ch == ",":
            k = j + 1
            while k < m and no_comments[k] in (" ", "\t", "\r", "\n"):
                k += 1
            if k < m and no_comments[k] in ("}", "]"):
                j += 1
                continue
            cleaned.append(ch)
            j += 1
        else:
            cleaned.append(ch)
            j += 1

    return "".join(cleaned)


def load_json_file(file_path: str) -> Any:
    """قراءة ملف JSON/JSONC مع التنظيف التلقائي ودعم UTF-16/UTF-8."""
    raw, _ = read_text_file_with_encoding(file_path)
    return json.loads(clean_jsonc(raw))


def flatten_json_strings(data: Any) -> List[Tuple[PathTuple, str, str]]:
    """استخراج جميع القيم النصية من بنية JSON مسطحة أو متداخلة مع مسارها."""
    items: List[Tuple[PathTuple, str, str]] = []

    def _walk(node: Any, path: PathTuple) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, path + (str(k),))
        elif isinstance(node, list):
            for idx, v in enumerate(node):
                _walk(v, path + (idx,))
        elif isinstance(node, str):
            flat_key = ".".join(str(p) for p in path)
            items.append((path, flat_key, node))

    _walk(data, ())
    return items


def apply_json_translations(original_data: Any, replacements: Dict[PathTuple, str]) -> Any:
    """تطبيق النصوص المترجمة على نسخة من البنية الأصلية مع الحفاظ الكامل على الترتيب والأنواع."""
    updated = copy.deepcopy(original_data)
    for path, new_val in replacements.items():
        if not path:
            continue
        cursor = updated
        for seg in path[:-1]:
            cursor = cursor[seg]
        cursor[path[-1]] = new_val
    return updated


def write_json_atomic_and_verify(file_path: str, data: Any) -> None:
    """كتابة JSON ذرياً ثم إعادة قراءته والتحقق من سلامته."""
    parent = os.path.dirname(os.path.abspath(file_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = file_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    # التحقق الصارم قبل استبدال الملف النهائي
    with open(tmp_path, "r", encoding="utf-8") as f:
        json.load(f)

    os.replace(tmp_path, file_path)


class JsonFormatHandler(FormatHandler):
    """محلل وكاتب ملفات JSON و JSONC عبر الواجهة الموحدة FormatHandler."""

    format_names = ("json",)

    def load(self, file_path: str) -> ParsedDocument:
        raw_data = load_json_file(file_path)
        entries = flatten_json_strings(raw_data)
        return ParsedDocument(
            format_name="json",
            raw_data=raw_data,
            entries=entries,
        )

    def save(
        self,
        file_path: str,
        doc: ParsedDocument,
        replacements: Dict[PathTuple, str],
    ) -> None:
        updated = apply_json_translations(doc.raw_data, replacements)
        write_json_atomic_and_verify(file_path, updated)
