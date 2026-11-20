# -*- coding: utf-8 -*-
"""الواجهة المجردة الموحدة لمحللات صيغ ملفات الترجمة."""

from abc import ABC, abstractmethod
import codecs
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union


PathTuple = Tuple[Union[str, int], ...]


def detect_bytes_encoding(raw: bytes) -> str:
    """كشف ترميز البايتات (UTF-16-LE/BE أو UTF-8-SIG) لتفادي تشويه ملفات UTF-16 في الألعاب."""
    if raw.startswith(codecs.BOM_UTF16_LE):
        return "utf-16"
    if raw.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    sample = raw[:2048]
    if b"\x00" in sample and len(sample) >= 4:
        even_nulls = sum(1 for i in range(0, len(sample), 2) if sample[i] == 0)
        odd_nulls = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
        half = max(1, len(sample) // 2)
        if odd_nulls / half > 0.35 and even_nulls / half < 0.1:
            return "utf-16-le"
        if even_nulls / half > 0.35 and odd_nulls / half < 0.1:
            return "utf-16-be"
    return "utf-8-sig"


def read_text_file_with_encoding(file_path: str) -> Tuple[str, str]:
    """قراءة ملف نصي مع الكشف التلقائي عن UTF-16 مقابل UTF-8 وإرجاع (النص, الترميز المكتشف)."""
    with open(file_path, "rb") as f:
        raw = f.read()
    enc = detect_bytes_encoding(raw)
    text = raw.decode(enc, errors="replace")
    return text, enc


def resolve_output_encoding(detected_encoding: str) -> str:
    """تحديد ترميز الحفظ المناسب: الحفاظ على UTF-16 إن كان الأصل UTF-16 وإلا UTF-8."""
    if detected_encoding.startswith("utf-16"):
        return detected_encoding
    return "utf-8"


@dataclass
class ParsedDocument:
    """يمثل ملف ترجمة مقروءاً مع بياناته الخام وقائمة نصوصه القابلة للتعديل."""

    format_name: str
    raw_data: Any
    entries: List[Tuple[PathTuple, str, str]]
    game_dir: Optional[str] = None


class FormatHandler(ABC):
    """واجهة موحدة لقراءة وحفظ أي صيغة ملف ترجمة بشكل ذري وآمن."""

    format_names: Tuple[str, ...] = ()

    @abstractmethod
    def load(self, file_path: str) -> ParsedDocument:
        """قراءة الملف وإرجاع ParsedDocument يحتوي البيانات الأصلية وقائمة النصوص."""

    @abstractmethod
    def save(
        self,
        file_path: str,
        doc: ParsedDocument,
        replacements: Dict[PathTuple, str],
    ) -> None:
        """تطبيق الترجمات وحفظ الملف ذرياً مع التحقق من سلامة الصيغة."""
