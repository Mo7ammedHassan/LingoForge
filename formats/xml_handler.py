# -*- coding: utf-8 -*-
"""محلل وكاتب ملفات XML مع حفظ السمات التعريفية دون تعديل والتحقق الذري بعد الكتابة."""

import copy
import os
import re
from typing import Dict, List, Tuple
import xml.etree.ElementTree as ET

from formats.base import (
    FormatHandler,
    ParsedDocument,
    PathTuple,
    read_text_file_with_encoding,
)


ID_ATTR_NAMES = ("key", "name", "id", "term", "code")
TRANSLATABLE_ATTR_NAMES = {
    "value",
    "text",
    "translation",
    "en",
    "english",
    "label",
    "caption",
    "title",
    "message",
    "desc",
    "description",
    "tooltip",
}
XMLNS_RE = re.compile(r'xmlns(?::([A-Za-z0-9_-]+))?=["\']([^"\']+)["\']')


def _register_namespaces(raw_text: str) -> None:
    """تسجيل مساحات الأسماء الموجودة لتفادي إضافة بادئات ns0 التلقائية عند الحفظ."""
    for prefix, uri in XMLNS_RE.findall(raw_text):
        try:
            ET.register_namespace(prefix or "", uri)
        except ValueError:
            pass


def _element_label(elem: ET.Element) -> str:
    """استخراج اسم تعريفي مقروء للعنصر من سماته أو وسمه."""
    tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
    lower_map = {k.lower(): v for k, v in elem.attrib.items()}
    for id_attr in ID_ATTR_NAMES:
        val = lower_map.get(id_attr)
        if val and val.strip():
            return f"{tag}[{val.strip()}]"
    return tag


class XmlFormatHandler(FormatHandler):
    """محلل وكاتب ملفات الترجمة بصيغة XML."""

    format_names = ("xml",)

    def load(self, file_path: str) -> ParsedDocument:
        raw_text, _ = read_text_file_with_encoding(file_path)

        cleaned = raw_text.lstrip()
        has_decl = cleaned.startswith("<?xml")
        _register_namespaces(cleaned)
        root = ET.fromstring(cleaned)

        entries: List[Tuple[PathTuple, str, str]] = []
        for elem_idx, elem in enumerate(root.iter()):
            label = _element_label(elem)
            if elem.text and elem.text.strip():
                entries.append(((elem_idx, "text"), label, elem.text))
            for attr_k, attr_v in elem.attrib.items():
                if attr_k.lower() in TRANSLATABLE_ATTR_NAMES and attr_v and attr_v.strip():
                    entries.append(
                        ((elem_idx, "attr", attr_k), f"{label}@{attr_k}", attr_v)
                    )

        return ParsedDocument(
            format_name="xml",
            raw_data={"root": root, "has_decl": has_decl},
            entries=entries,
        )

    def save(
        self,
        file_path: str,
        doc: ParsedDocument,
        replacements: Dict[PathTuple, str],
    ) -> None:
        root_copy: ET.Element = copy.deepcopy(doc.raw_data["root"])
        has_decl: bool = bool(doc.raw_data.get("has_decl"))
        elements = list(root_copy.iter())

        for path_tuple, new_val in replacements.items():
            if not path_tuple:
                continue
            elem_idx = int(path_tuple[0])
            if not (0 <= elem_idx < len(elements)):
                continue
            elem = elements[elem_idx]
            kind = path_tuple[1] if len(path_tuple) > 1 else ""
            if kind == "text":
                elem.text = new_val
            elif kind == "attr" and len(path_tuple) >= 3:
                attr_name = str(path_tuple[2])
                elem.attrib[attr_name] = new_val

        parent = os.path.dirname(os.path.abspath(file_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = file_path + ".tmp"

        tree = ET.ElementTree(root_copy)
        with open(tmp_path, "wb") as f:
            tree.write(f, encoding="utf-8", xml_declaration=has_decl)
            f.flush()
            os.fsync(f.fileno())

        # التحقق الصارم بإعادة تحليل XML قبل استبدال الملف الأصلي
        ET.parse(tmp_path)
        os.replace(tmp_path, file_path)
