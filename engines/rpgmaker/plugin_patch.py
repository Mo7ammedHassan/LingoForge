# -*- coding: utf-8 -*-
"""نشر إضافة AutoTranslator_ArabicText.js في RPG Maker MV / MZ وتسجيلها في js/plugins.js بأمان.

المشكلة: Window_Base.processNormalCharacter (MV) يرسم الحوار حرفاً حرفاً، وفي MZ يُفرَّغ
المخزن بعد كل حرف أثناء تأثير الآلة الكاتبة؛ فتنفصل الحروف العربية وينعكس ترتيبها.
الحل: إضافة JS خفيفة تجمع الكلمات/المقاطع العربية وترسمها دفعة واحدة، مع تتبع كامل في
BackupManager (نسخ plugins.js احتياطياً وتسجيل ملف الإضافة كملف مضاف) لضمان Rollback نظيف.
"""

import json
import os
import re
import shutil
from typing import Dict, List, Optional, Tuple

from core.backup import BackupManager
from core.models import EngineInfo


PLUGIN_NAME = "AutoTranslator_ArabicText"
PLUGIN_FILENAME = PLUGIN_NAME + ".js"
PLUGIN_DESCRIPTION = (
    "[AutoTranslator] Draws Arabic message text as connected words/lines "
    "(no isolated letters) with RTL lines."
)

_PLUGINS_DECL_RE = re.compile(r"\$plugins\s*=\s*", re.MULTILINE)


def get_plugin_source_path() -> str:
    """مسار ملف الإضافة المرجعي داخل مجلد الأداة."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    return os.path.join(root, "injectors", "rpgmaker_runtime", PLUGIN_FILENAME)


def resolve_js_dir(engine: EngineInfo) -> str:
    """تحديد مجلد js الخاص باللعبة (www/js في MV المصدّر، أو js في MZ)."""
    if engine.data_dir:
        base_root = os.path.dirname(os.path.abspath(engine.data_dir))
        return os.path.join(base_root, "js")
    www_js = os.path.join(engine.game_dir, "www", "js")
    if os.path.isdir(www_js):
        return www_js
    return os.path.join(engine.game_dir, "js")


def parse_plugins_js(text: str) -> Tuple[str, List[Dict], str]:
    """تحليل js/plugins.js إلى (البادئة، قائمة الإضافات، اللاحقة) دون إتلاف بقية الملف.

    يرفع ValueError عند تعذر العثور على مصفوفة $plugins صالحة بصيغة JSON.
    """
    match = _PLUGINS_DECL_RE.search(text or "")
    if not match:
        raise ValueError("لم يُعثر على تعريف $plugins داخل plugins.js")
    start = text.find("[", match.end())
    if start < 0:
        raise ValueError("لم يُعثر على مصفوفة $plugins داخل plugins.js")
    decoder = json.JSONDecoder()
    try:
        plugins, end = decoder.raw_decode(text, start)
    except json.JSONDecodeError as exc:
        raise ValueError(f"مصفوفة $plugins ليست JSON صالحاً: {exc}") from exc
    if not isinstance(plugins, list):
        raise ValueError("$plugins ليست مصفوفة")
    return text[:start], plugins, text[end:]


def build_plugin_entry(enabled: bool = True, mode: str = "word", rtl_messages: bool = True) -> Dict:
    return {
        "name": PLUGIN_NAME,
        "status": True,
        "description": PLUGIN_DESCRIPTION,
        "parameters": {
            "Enabled": "true" if enabled else "false",
            "Mode": "line" if str(mode).lower() == "line" else "word",
            "RtlMessages": "true" if rtl_messages else "false",
        },
    }


def _serialize_plugins_array(plugins: List[Dict]) -> str:
    """تسلسل المصفوفة بنفس أسلوب محرر RPG Maker (إضافة واحدة في كل سطر)."""
    if not plugins:
        return "[\n]"
    lines = [json.dumps(p, ensure_ascii=False, separators=(",", ":")) for p in plugins]
    return "[\n" + ",\n".join(lines) + "\n]"


def upsert_plugin_entry(text: str, entry: Dict) -> Tuple[str, bool]:
    """إضافة/تحديث سجل الإضافة داخل نص plugins.js وإرجاع (النص الجديد، هل تغيّر).

    عند الإضافة الأولى يُدرج السطر نصياً قبل قوس الإغلاق للحفاظ على تنسيق الإضافات الأخرى
    حرفياً؛ وعند وجود السجل مسبقاً يُستبدل السجل فقط.
    """
    prefix, plugins, suffix = parse_plugins_js(text)
    existing_idx = next(
        (i for i, p in enumerate(plugins) if isinstance(p, dict) and p.get("name") == PLUGIN_NAME),
        None,
    )
    if existing_idx is not None:
        if plugins[existing_idx] == entry:
            return text, False
        plugins[existing_idx] = entry
        new_text = prefix + _serialize_plugins_array(plugins) + suffix
    else:
        array_text = text[len(prefix): len(text) - len(suffix)]
        close_idx = array_text.rfind("]")
        body = array_text[:close_idx].rstrip()
        entry_line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        if plugins:
            new_array = body + ",\n" + entry_line + "\n]"
        else:
            new_array = "[\n" + entry_line + "\n]"
        new_text = prefix + new_array + suffix

    # تحقق ذاتي: يجب أن يبقى الملف قابلاً للتحليل وأن يحتوي السجل مرة واحدة فقط
    _, check_plugins, _ = parse_plugins_js(new_text)
    count = sum(1 for p in check_plugins if isinstance(p, dict) and p.get("name") == PLUGIN_NAME)
    if count != 1 or len(check_plugins) != len(plugins) + (0 if existing_idx is not None else 1):
        raise ValueError("فشل التحقق من سلامة plugins.js بعد التعديل")
    return new_text, True


def _atomic_write_text(path: str, content: str) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    os.replace(tmp_path, path)


def deploy_arabic_text_plugin(
    engine: EngineInfo,
    mode: str = "word",
    rtl_messages: bool = True,
) -> Tuple[List[str], List[str]]:
    """نشر ملف الإضافة وتسجيلها في js/plugins.js مع تتبع كامل للاسترجاع.

    يعيد (الملفات المكتوبة، التحذيرات). لا يرفع استثناءات عند فشل التحليل بل يتخطى بأمان.
    """
    warnings: List[str] = []
    written: List[str] = []
    src = get_plugin_source_path()
    if not os.path.isfile(src):
        warnings.append(f"ملف إضافة الحوار العربي غير موجود: {src}")
        return written, warnings

    js_dir = resolve_js_dir(engine)
    plugins_js = os.path.join(js_dir, "plugins.js")
    if not os.path.isfile(plugins_js):
        warnings.append(
            "لم يُعثر على js/plugins.js؛ تم تخطي إضافة إصلاح رسم الحوار العربي (قد تنفصل الحروف في صندوق الحوار)."
        )
        return written, warnings

    try:
        with open(plugins_js, "r", encoding="utf-8-sig") as f:
            original_text = f.read()
        new_text, changed = upsert_plugin_entry(
            original_text, build_plugin_entry(True, mode=mode, rtl_messages=rtl_messages)
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        warnings.append(f"تعذر تحليل js/plugins.js بأمان ({exc})؛ تم تخطي إضافة إصلاح الحوار العربي دون تعديل.")
        return written, warnings

    plugins_dir = os.path.join(js_dir, "plugins")
    os.makedirs(plugins_dir, exist_ok=True)
    dst = os.path.join(plugins_dir, PLUGIN_FILENAME)
    dst_existed = os.path.exists(dst)
    if dst_existed:
        manifest = BackupManager.load_manifest(engine.game_dir)
        rel = os.path.relpath(os.path.abspath(dst), os.path.abspath(engine.game_dir))
        already_created = any(p.lower() == rel.lower() for p in manifest.get("created_files", []))
        if not already_created:
            BackupManager.backup_file(engine.game_dir, dst)
    shutil.copy2(src, dst)
    if not dst_existed:
        BackupManager.register_created_file(engine.game_dir, dst)
    written.append(dst)

    if changed:
        BackupManager.backup_file(engine.game_dir, plugins_js)
        _atomic_write_text(plugins_js, new_text)
    written.append(plugins_js)
    return written, warnings


def is_arabic_text_plugin_registered(plugins_js_path: str) -> bool:
    try:
        with open(plugins_js_path, "r", encoding="utf-8-sig") as f:
            _, plugins, _ = parse_plugins_js(f.read())
    except (OSError, ValueError):
        return False
    return any(isinstance(p, dict) and p.get("name") == PLUGIN_NAME and p.get("status") for p in plugins)
