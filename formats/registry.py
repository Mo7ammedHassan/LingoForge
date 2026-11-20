# -*- coding: utf-8 -*-
"""سجل محللات صيغ ملفات الترجمة واختيار المحلل المناسب حسب اسم الصيغة."""

import threading
from typing import Dict, Optional

from formats.base import FormatHandler

_REGISTRY_LOCK = threading.Lock()
_FORMAT_REGISTRY: Optional[Dict[str, FormatHandler]] = None


def _ensure_registry() -> Dict[str, FormatHandler]:
    global _FORMAT_REGISTRY
    if _FORMAT_REGISTRY is not None:
        return _FORMAT_REGISTRY
    with _REGISTRY_LOCK:
        if _FORMAT_REGISTRY is None:
            from formats.assets_override_handler import (
                UnityAssetsFormatHandler,
                UnityCodeStringsFormatHandler,
            )
            from formats.csv_handler import CsvFormatHandler
            from formats.godot_pck_handler import GodotPckFormatHandler
            from formats.ini_handler import IniFormatHandler, TxtFormatHandler
            from formats.json_handler import JsonFormatHandler
            from formats.locres_handler import LocresFormatHandler
            from formats.po_handler import PoFormatHandler
            from formats.unreal_pak_handler import UnrealPakFormatHandler
            from formats.xml_handler import XmlFormatHandler
            from formats.yaml_handler import YamlFormatHandler

            handlers = (
                JsonFormatHandler(),
                CsvFormatHandler(),
                XmlFormatHandler(),
                IniFormatHandler(),
                TxtFormatHandler(),
                PoFormatHandler(),
                YamlFormatHandler(),
                UnityAssetsFormatHandler(),
                UnityCodeStringsFormatHandler(),
                GodotPckFormatHandler(),
                LocresFormatHandler(),
                UnrealPakFormatHandler(),
            )
            _FORMAT_REGISTRY = {
                fmt: handler
                for handler in handlers
                for fmt in handler.format_names
            }
        return _FORMAT_REGISTRY


def get_format_handler(format_name: str) -> Optional[FormatHandler]:
    """إرجاع محلل الصيغة المطابق لاسم الصيغة أو الامتداد (مثل json, csv, xml, ini, po, txt)."""
    key = (format_name or "").lstrip(".").strip().lower()
    return _ensure_registry().get(key)
