# -*- coding: utf-8 -*-
"""حاقن الخط العربي وإعدادات التعريب لمحرك Godot (3.x / 4.x) عبر override.cfg و arabic_l10n/."""

import os
import re
import shutil
from typing import Dict, List, Optional, Tuple

from core.backup import BackupManager, atomic_copy_file, atomic_write_bytes, atomic_write_text
from core.models import EngineInfo, InjectionResult
from engines.godot.detector import find_godot_containers
from engines.godot.pck_parser import (
    build_godot_pck_blob,
    parse_pck_header_and_entries,
    parse_project_binary,
    parse_project_godot_text,
    read_pck_entry_bytes,
)
from engines.unity.injector import resolve_arabic_font_path


ARABIC_DIR_NAME = "arabic_l10n"
OVERRIDE_FILENAME = "override.cfg"
GODOT3_FONT_TRES = "arabic_font.tres"
ARABIC_THEME_TRES = "arabic_theme.tres"
ARABIC_AUTOLOAD_GD = "arabic_autoload.gd"
PATCH_PCK_NAME = "arabic_patch.pck"

_SIBLING_LANG_STEMS = {
    "fr", "de", "es", "ru", "it", "pt", "pt_br", "ja", "jp", "zh", "zh_cn", "zh_tw",
    "ko", "pl", "tr", "nl", "uk", "cs", "hu", "sv", "no", "da", "fi", "el", "th",
    "vi", "id", "french", "german", "spanish", "russian", "italian", "portuguese",
    "japanese", "chinese", "korean", "polish", "turkish", "dutch",
}


def _is_godot3(engine: EngineInfo) -> bool:
    return bool(engine.requires_arabic_shaping or "godot 3" in engine.engine_name.lower())


def _format_godot_string_array(paths: List[str], is_godot3: bool) -> str:
    quoted = ", ".join(f'"{p}"' for p in paths)
    ctor = "PoolStringArray" if is_godot3 else "PackedStringArray"
    return f"{ctor}({quoted})"


def _parse_cfg_sections(text: str) -> Tuple[List[str], Dict[str, List[Tuple[str, str]]]]:
    """تحليل بسيط وآمن لملف override.cfg مع الحفاظ على ترتيب الأقسام والمفاتيح غير المتعلقة بالتعريب."""
    section_order: List[str] = []
    sections: Dict[str, List[Tuple[str, str]]] = {}
    current_section = ""

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith((";", "#")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1].strip()
            if current_section not in sections:
                sections[current_section] = []
                section_order.append(current_section)
            continue
        if "=" in stripped:
            k, v = stripped.split("=", 1)
            k = k.strip()
            v = v.strip()
            if current_section not in sections:
                sections[current_section] = []
                section_order.append(current_section)
            existing = sections[current_section]
            replaced = False
            for idx, (ek, _) in enumerate(existing):
                if ek == k:
                    existing[idx] = (k, v)
                    replaced = True
                    break
            if not replaced:
                existing.append((k, v))

    return section_order, sections


def _set_cfg_key(
    section_order: List[str],
    sections: Dict[str, List[Tuple[str, str]]],
    section: str,
    key: str,
    value: str,
) -> None:
    if section not in sections:
        sections[section] = []
        section_order.append(section)
    items = sections[section]
    for idx, (k, _) in enumerate(items):
        if k == key:
            items[idx] = (key, value)
            return
    items.append((key, value))


def _serialize_cfg(
    section_order: List[str],
    sections: Dict[str, List[Tuple[str, str]]],
) -> str:
    lines: List[str] = []
    for sec in section_order:
        items = sections.get(sec, [])
        if not items:
            continue
        if sec:
            lines.append(f"[{sec}]")
        for k, v in items:
            lines.append(f"{k}={v}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _find_custom_theme_source(engine: EngineInfo) -> Tuple[bool, Optional[str], Optional[str]]:
    """البحث عن مسار الثيم المخصص وقراءة نصه (.tres) من القرص أو من .pck للحفاظ على الأنماط."""
    game_dir = os.path.abspath(engine.game_dir)
    theme_res_path: Optional[str] = None
    pck_infos = []

    proj_path = os.path.join(game_dir, "project.godot")
    if os.path.isfile(proj_path):
        try:
            with open(proj_path, "r", encoding="utf-8-sig", errors="replace") as f:
                meta = parse_project_godot_text(f.read())
            if meta.get("custom_theme"):
                theme_res_path = str(meta["custom_theme"]).strip()
        except OSError:
            pass

    pck_files, exe_files, _ = find_godot_containers(game_dir)
    for container in pck_files + exe_files:
        pck = parse_pck_header_and_entries(container)
        if pck is None or pck.dir_encrypted:
            continue
        pck_infos.append(pck)
        if theme_res_path:
            continue
        for entry in pck.entries:
            low = entry.res_path.lower()
            if low == "res://project.godot":
                raw = read_pck_entry_bytes(pck, entry)
                meta = parse_project_godot_text(raw.decode("utf-8-sig", errors="replace"))
                if meta.get("custom_theme"):
                    theme_res_path = str(meta["custom_theme"]).strip()
                    break
            elif low == "res://project.binary":
                raw = read_pck_entry_bytes(pck, entry)
                meta = parse_project_binary(raw)
                if meta.get("custom_theme"):
                    theme_res_path = str(meta["custom_theme"]).strip()
                    break

    if not theme_res_path:
        cfg_path = os.path.join(game_dir, OVERRIDE_FILENAME)
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8-sig", errors="replace") as f:
                    _, secs = _parse_cfg_sections(f.read())
                for k, v in secs.get("gui", []):
                    val = v.strip('"').strip()
                    if k == "theme/custom" and val and ARABIC_THEME_TRES not in val:
                        theme_res_path = val
                        break
            except OSError:
                pass

    if not theme_res_path:
        return False, None, None

    norm_res = theme_res_path.replace("\\", "/")
    if not norm_res.startswith("res://"):
        norm_res = "res://" + norm_res.lstrip("/")

    rel_disk = norm_res[6:].lstrip("/")
    disk_path = os.path.join(game_dir, rel_disk.replace("/", os.sep))
    orig_tres_text: Optional[str] = None

    if os.path.isfile(disk_path) and disk_path.lower().endswith(".tres"):
        try:
            with open(disk_path, "r", encoding="utf-8-sig", errors="replace") as f:
                candidate_txt = f.read()
            if "[gd_resource" in candidate_txt and 'type="Theme"' in candidate_txt:
                orig_tres_text = candidate_txt
        except OSError:
            pass

    if orig_tres_text is None and norm_res.lower().endswith(".tres"):
        for pck in pck_infos:
            for entry in pck.entries:
                if entry.res_path.lower() == norm_res.lower() and not entry.encrypted:
                    raw = read_pck_entry_bytes(pck, entry)
                    if raw:
                        candidate_txt = raw.decode("utf-8-sig", errors="replace")
                        if "[gd_resource" in candidate_txt and 'type="Theme"' in candidate_txt:
                            orig_tres_text = candidate_txt
                            break
            if orig_tres_text is not None:
                break

    return True, norm_res, orig_tres_text


def _build_merged_arabic_theme_tres(
    orig_tres_text: str,
    font_res_path: str,
    is_godot3: bool,
) -> str:
    """دمج الخط العربي داخل نسخة من الثيم الأصلي (.tres) مع حفظ كل الأنماط والألوان."""
    ext_type = "DynamicFont" if is_godot3 else "FontFile"
    if is_godot3:
        used_ids = {int(m) for m in re.findall(r"\[ext_resource[^\]]*\bid\s*=\s*(\d+)", orig_tres_text)}
        fid_num = 99
        while fid_num in used_ids:
            fid_num += 1
        ext_line = f'[ext_resource path="{font_res_path}" type="{ext_type}" id={fid_num}]'
        font_expr = f"ExtResource( {fid_num} )"
    else:
        used_str_ids = set(re.findall(r'\[ext_resource[^\]]*\bid\s*=\s*"([^"]+)"', orig_tres_text))
        fid_str = "99_ar"
        idx = 0
        while fid_str in used_str_ids:
            idx += 1
            fid_str = f"99_ar_{idx}"
        ext_line = f'[ext_resource type="{ext_type}" path="{font_res_path}" id="{fid_str}"]'
        font_expr = f'ExtResource("{fid_str}")'

    lines = orig_tres_text.splitlines()
    # تحديث load_steps في ترويسة [gd_resource ...]
    new_ext_count = len(re.findall(r"^\[ext_resource\b", orig_tres_text, re.MULTILINE)) + 1
    new_sub_count = len(re.findall(r"^\[sub_resource\b", orig_tres_text, re.MULTILINE))
    expected_steps = new_ext_count + new_sub_count + 1
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("[gd_resource"):
            if re.search(r"\bload_steps\s*=\s*\d+", ln):
                lines[i] = re.sub(
                    r"\bload_steps\s*=\s*(\d+)",
                    lambda m: f"load_steps={max(int(m.group(1)) + 1, expected_steps)}",
                    ln,
                )
            else:
                lines[i] = ln.rstrip("]") + f" load_steps={expected_steps}]"
            break

    # إدراج [ext_resource] للخط العربي بعد آخر [ext_resource] أو قبل [sub_resource]/[resource]
    insert_idx = None
    last_ext_idx = None
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        if s.startswith("[ext_resource"):
            last_ext_idx = i
        elif insert_idx is None and s.startswith(("[sub_resource", "[resource]")):
            insert_idx = i
    if last_ext_idx is not None:
        lines.insert(last_ext_idx + 1, ext_line)
    elif insert_idx is not None:
        lines.insert(insert_idx, ext_line)
        lines.insert(insert_idx + 1, "")
    else:
        lines.append(ext_line)

    # تحديث [resource]: تعيين default_font واستبدال خصائص الخطوط الفرعية مع إبقاء الأنماط والألوان
    res_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "[resource]"), None)
    if res_idx is None:
        lines.extend(["", "[resource]", f"default_font = {font_expr}"])
    else:
        found_default = False
        font_prop_re = re.compile(r"^([A-Za-z0-9_]+/fonts?/[A-Za-z0-9_]+\s*=)\s*.*$")
        for i in range(res_idx + 1, len(lines)):
            s = lines[i].strip()
            if s.startswith("["):
                break
            if re.match(r"^default_font\s*=", s):
                lines[i] = f"default_font = {font_expr}"
                found_default = True
            else:
                m_fp = font_prop_re.match(s)
                if m_fp:
                    lines[i] = f"{m_fp.group(1)} {font_expr}"
        if not found_default:
            lines.insert(res_idx + 1, f"default_font = {font_expr}")

    return "\n".join(lines).rstrip() + "\n"


def _inspect_project_settings(engine: EngineInfo) -> Tuple[List[str], bool]:
    """فحص إعدادات المشروع (project.godot / project.binary / override.cfg) لمعرفة مسارات الترجمة ووجود ثيم مخصص."""
    game_dir = os.path.abspath(engine.game_dir)
    translations: List[str] = []
    has_custom_theme = False

    proj_path = os.path.join(game_dir, "project.godot")
    if os.path.isfile(proj_path):
        try:
            with open(proj_path, "r", encoding="utf-8-sig", errors="replace") as f:
                meta = parse_project_godot_text(f.read())
            translations.extend(meta.get("translations", []))
            if meta.get("custom_theme"):
                has_custom_theme = True
        except OSError:
            pass

    pck_files, exe_files, _ = find_godot_containers(game_dir)
    for container in pck_files + exe_files:
        pck = parse_pck_header_and_entries(container)
        if pck is None or pck.dir_encrypted:
            continue
        for entry in pck.entries:
            low = entry.res_path.lower()
            if low == "res://project.godot":
                raw = read_pck_entry_bytes(pck, entry)
                meta = parse_project_godot_text(raw.decode("utf-8-sig", errors="replace"))
                translations.extend(meta.get("translations", []))
                if meta.get("custom_theme"):
                    has_custom_theme = True
            elif low == "res://project.binary":
                raw = read_pck_entry_bytes(pck, entry)
                meta = parse_project_binary(raw)
                translations.extend(meta.get("translations", []))
                if meta.get("custom_theme"):
                    has_custom_theme = True

    cfg_path = os.path.join(game_dir, OVERRIDE_FILENAME)
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8-sig", errors="replace") as f:
                _, secs = _parse_cfg_sections(f.read())
            for k, v in secs.get("gui", []):
                if k == "theme/custom" and v.strip('"') and ARABIC_THEME_TRES not in v:
                    has_custom_theme = True
        except OSError:
            pass

    return translations, has_custom_theme


def _collect_translation_res_paths(engine: EngineInfo) -> List[str]:
    """تجميع مسارات ملفات الترجمة المنشأة أو الموجودة كصيغة res://... لإدراجها في override.cfg."""
    game_dir = os.path.abspath(engine.game_dir)
    collected: List[str] = []
    seen = set()

    def _add(res_path: str) -> None:
        norm = res_path.replace("\\", "/")
        if not norm.startswith("res://"):
            norm = "res://" + norm.lstrip("/")
        if norm.lower() not in seen:
            seen.add(norm.lower())
            collected.append(norm)

    l10n_dir = os.path.join(game_dir, ARABIC_DIR_NAME)
    if os.path.isdir(l10n_dir):
        for root, _, files in os.walk(l10n_dir):
            for fname in sorted(files):
                ext = os.path.splitext(fname.lower())[1]
                if ext in (".po", ".csv", ".tsv", ".translation"):
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, game_dir).replace(os.sep, "/")
                    _add(f"res://{rel}")

    # إذا لم توجد ملفات معربة داخل arabic_l10n/، نجمع الملفات الخارجية المترجمة على القرص في مجلدات اللغة
    if not collected:
        for folder in ("translations", "locale", "localization", "lang", "i18n"):
            sub = os.path.join(game_dir, folder)
            if not os.path.isdir(sub):
                continue
            for root, _, files in os.walk(sub):
                for fname in sorted(files):
                    low = fname.lower()
                    if low.endswith(".bak"):
                        continue
                    ext = os.path.splitext(low)[1]
                    if ext not in (".po", ".csv", ".tsv", ".translation"):
                        continue
                    tokens = {p for p in re.split(r"[._\-]+", os.path.splitext(low)[0]) if p}
                    if tokens & _SIBLING_LANG_STEMS and "en" not in tokens and "ar" not in tokens:
                        continue
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, game_dir).replace(os.sep, "/")
                    _add(f"res://{rel}")

    # دمج مسارات project.godot / project.binary الأصلية حتى لا تُفقد ملفات اللغة الأخرى
    proj_translations, _ = _inspect_project_settings(engine)
    for rp in proj_translations:
        _add(rp)

    return collected


def _write_text_atomic_tracked(game_dir: str, path: str, content: str) -> None:
    existed = os.path.exists(path)
    if existed:
        BackupManager.backup_file(game_dir, path)
    else:
        BackupManager.register_created_file(game_dir, path)
    atomic_write_text(path, content, encoding="utf-8", newline="\n")


class GodotInjector:
    """ينشر الخط العربي وملف override.cfg (وحزمة تكميلية عند الحاجة) مع تتبع كامل في BackupManager."""

    def deploy_arabic_font(
        self,
        engine: EngineInfo,
        custom_font_path: Optional[str] = None,
        create_theme_override: bool = False,
        orig_theme_text: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], List[str]]:
        """نسخ الخط العربي إلى arabic_l10n/ وتجهيز .tres للخط والثيم عند اللزوم."""
        src_font = resolve_arabic_font_path(custom_font_path)
        if not src_font or not os.path.isdir(engine.game_dir):
            return None, None, None, []

        game_dir = os.path.abspath(engine.game_dir)
        l10n_dir = os.path.join(game_dir, ARABIC_DIR_NAME)
        os.makedirs(l10n_dir, exist_ok=True)

        font_filename = os.path.basename(src_font)
        dst_font = os.path.join(l10n_dir, font_filename)
        existed = os.path.exists(dst_font)
        if existed:
            BackupManager.backup_file(game_dir, dst_font)
        else:
            BackupManager.register_created_file(game_dir, dst_font)
        atomic_copy_file(src_font, dst_font)

        created_files: List[str] = [dst_font]
        font_res_path = f"res://{ARABIC_DIR_NAME}/{font_filename}"
        theme_res_path: Optional[str] = None
        is_g3 = _is_godot3(engine)

        if is_g3:
            tres_path = os.path.join(l10n_dir, GODOT3_FONT_TRES)
            tres_content = (
                '[gd_resource type="DynamicFont" load_steps=2 format=2]\n\n'
                f'[ext_resource path="{font_res_path}" type="DynamicFontData" id=1]\n\n'
                "[resource]\n"
                "size = 18\n"
                "use_mipmaps = true\n"
                "use_filter = true\n"
                "font_data = ExtResource( 1 )\n"
            )
            _write_text_atomic_tracked(game_dir, tres_path, tres_content)
            created_files.append(tres_path)
            font_res_path = f"res://{ARABIC_DIR_NAME}/{GODOT3_FONT_TRES}"

            if create_theme_override:
                theme_path = os.path.join(l10n_dir, ARABIC_THEME_TRES)
                if orig_theme_text:
                    theme_content = _build_merged_arabic_theme_tres(orig_theme_text, font_res_path, True)
                else:
                    theme_content = (
                        '[gd_resource type="Theme" load_steps=2 format=2]\n\n'
                        f'[ext_resource path="{font_res_path}" type="DynamicFont" id=1]\n\n'
                        "[resource]\n"
                        "default_font = ExtResource( 1 )\n"
                    )
                _write_text_atomic_tracked(game_dir, theme_path, theme_content)
                created_files.append(theme_path)
                theme_res_path = f"res://{ARABIC_DIR_NAME}/{ARABIC_THEME_TRES}"
        elif create_theme_override:
            theme_path = os.path.join(l10n_dir, ARABIC_THEME_TRES)
            if orig_theme_text:
                theme_content = _build_merged_arabic_theme_tres(orig_theme_text, font_res_path, False)
            else:
                theme_content = (
                    '[gd_resource type="Theme" load_steps=2 format=3]\n\n'
                    f'[ext_resource type="FontFile" path="{font_res_path}" id="1_ar"]\n\n'
                    "[resource]\n"
                    'default_font = ExtResource("1_ar")\n'
                )
            _write_text_atomic_tracked(game_dir, theme_path, theme_content)
            created_files.append(theme_path)
            theme_res_path = f"res://{ARABIC_DIR_NAME}/{ARABIC_THEME_TRES}"

        return dst_font, font_res_path, theme_res_path, created_files

    def _deploy_project_autoload_if_unpacked(
        self,
        engine: EngineInfo,
        font_res_path: Optional[str],
        custom_theme_res_path: Optional[str] = None,
    ) -> Optional[str]:
        """نشر سكربت Autoload لمشاريع project.godot غير المصدّرة لفرض ar والخط على العقد المتجاوزة محلياً."""
        game_dir = os.path.abspath(engine.game_dir)
        if not os.path.isfile(os.path.join(game_dir, "project.godot")):
            return None

        l10n_dir = os.path.join(game_dir, ARABIC_DIR_NAME)
        os.makedirs(l10n_dir, exist_ok=True)
        gd_path = os.path.join(l10n_dir, ARABIC_AUTOLOAD_GD)

        theme_patch_g3 = ""
        theme_patch_g4 = ""
        if custom_theme_res_path:
            theme_patch_g3 = (
                f'\tif _ar_font != null and ResourceLoader.exists("{custom_theme_res_path}"):\n'
                f'\t\tvar _orig_theme = load("{custom_theme_res_path}")\n'
                "\t\tif _orig_theme is Theme:\n"
                "\t\t\t_orig_theme.default_font = _ar_font\n"
            )
            theme_patch_g4 = (
                f'\tif _ar_font != null and ResourceLoader.exists("{custom_theme_res_path}"):\n'
                f'\t\tvar _orig_theme = load("{custom_theme_res_path}")\n'
                "\t\tif _orig_theme is Theme:\n"
                "\t\t\t_orig_theme.default_font = _ar_font\n"
            )

        if _is_godot3(engine):
            gd_code = (
                "extends Node\n\n"
                "var _ar_font = null\n\n"
                "func _ready():\n"
                '\tTranslationServer.set_locale("ar")\n'
                f'\tif ResourceLoader.exists("{font_res_path or ""}"):\n'
                f'\t\t_ar_font = load("{font_res_path or ""}")\n'
                f"{theme_patch_g3}"
                "\tget_tree().connect(\"node_added\", self, \"_on_node_added\")\n"
                "\t_apply_tree(get_tree().get_root())\n\n"
                "func _on_node_added(node):\n"
                "\t_apply_node(node)\n\n"
                "func _apply_tree(root):\n"
                "\t_apply_node(root)\n"
                "\tfor ch in root.get_children():\n"
                "\t\t_apply_tree(ch)\n\n"
                "func _apply_node(node):\n"
                "\tif _ar_font != null and node is Control:\n"
                '\t\tif node.has_font_override("font"):\n'
                '\t\t\tnode.add_font_override("font", _ar_font)\n'
            )
        else:
            gd_code = (
                "extends Node\n\n"
                "var _ar_font: Font = null\n\n"
                "func _ready() -> void:\n"
                '\tTranslationServer.set_locale("ar")\n'
                f'\tif ResourceLoader.exists("{font_res_path or ""}"):\n'
                f'\t\t_ar_font = load("{font_res_path or ""}")\n'
                f"{theme_patch_g4}"
                "\tget_tree().node_added.connect(_on_node_added)\n"
                "\t_apply_tree(get_tree().root)\n\n"
                "func _on_node_added(node: Node) -> void:\n"
                "\t_apply_node(node)\n\n"
                "func _apply_tree(root: Node) -> void:\n"
                "\t_apply_node(root)\n"
                "\tfor ch in root.get_children():\n"
                "\t\t_apply_tree(ch)\n\n"
                "func _apply_node(node: Node) -> void:\n"
                "\tif _ar_font != null and node is Control:\n"
                '\t\tif node.has_theme_font_override("font"):\n'
                '\t\t\tnode.add_theme_font_override("font", _ar_font)\n'
            )

        _write_text_atomic_tracked(game_dir, gd_path, gd_code)
        return f"res://{ARABIC_DIR_NAME}/{ARABIC_AUTOLOAD_GD}"

    def _upsert_override_cfg(
        self,
        engine: EngineInfo,
        font_res_path: Optional[str],
        translation_res_paths: List[str],
        update_locale: bool = True,
        theme_res_path: Optional[str] = None,
        autoload_res_path: Optional[str] = None,
    ) -> str:
        """إنشاء أو تحديث override.cfg بالصيغة الصحيحة لـ Godot 3 ([locale]) و Godot 4 ([internationalization])."""
        game_dir = os.path.abspath(engine.game_dir)
        cfg_path = os.path.join(game_dir, OVERRIDE_FILENAME)
        existed = os.path.exists(cfg_path)
        existing_text = ""
        if existed:
            BackupManager.backup_file(game_dir, cfg_path)
            with open(cfg_path, "r", encoding="utf-8-sig", errors="replace") as f:
                existing_text = f.read()

        section_order, sections = _parse_cfg_sections(existing_text)
        is_g3 = _is_godot3(engine)

        if update_locale:
            if is_g3:
                _set_cfg_key(section_order, sections, "locale", "test", '"ar"')
                _set_cfg_key(section_order, sections, "locale", "fallback", '"en"')
                if translation_res_paths:
                    _set_cfg_key(
                        section_order,
                        sections,
                        "locale",
                        "translations",
                        _format_godot_string_array(translation_res_paths, True),
                    )
            else:
                _set_cfg_key(section_order, sections, "internationalization", "locale/test", '"ar"')
                _set_cfg_key(section_order, sections, "internationalization", "locale/fallback", '"en"')
                if translation_res_paths:
                    _set_cfg_key(
                        section_order,
                        sections,
                        "internationalization",
                        "locale/translations",
                        _format_godot_string_array(translation_res_paths, False),
                    )

        if font_res_path:
            _set_cfg_key(
                section_order,
                sections,
                "gui",
                "theme/custom_font",
                f'"{font_res_path}"',
            )
        if theme_res_path:
            _set_cfg_key(
                section_order,
                sections,
                "gui",
                "theme/custom",
                f'"{theme_res_path}"',
            )
        if autoload_res_path:
            _set_cfg_key(
                section_order,
                sections,
                "autoload",
                "ArabicL10n",
                f'"*{autoload_res_path}"',
            )

        serialized = _serialize_cfg(section_order, sections)
        if not existed:
            BackupManager.register_created_file(game_dir, cfg_path)
        atomic_write_text(cfg_path, serialized, encoding="utf-8", newline="\n")
        return cfg_path

    def _build_supplementary_pck(self, engine: EngineInfo) -> Optional[str]:
        """بناء حزمة .pck تكميلية (arabic_patch.pck) إذا كانت اللعبة تعتمد على .pck بدون المساس بالأصل."""
        game_dir = os.path.abspath(engine.game_dir)
        pck_files, exe_files, _ = find_godot_containers(game_dir)
        base_pck = None
        for p in pck_files + exe_files:
            base_pck = parse_pck_header_and_entries(p)
            if base_pck is not None:
                break
        if base_pck is None:
            return None

        l10n_dir = os.path.join(game_dir, ARABIC_DIR_NAME)
        if not os.path.isdir(l10n_dir):
            return None

        packed_files: Dict[str, bytes] = {}
        for root, _, files in os.walk(l10n_dir):
            for fname in sorted(files):
                if fname.lower() == PATCH_PCK_NAME:
                    continue
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, game_dir).replace(os.sep, "/")
                with open(full, "rb") as f:
                    packed_files[f"res://{rel}"] = f.read()

        if not packed_files:
            return None

        patch_pck_path = os.path.join(l10n_dir, PATCH_PCK_NAME)
        existed = os.path.exists(patch_pck_path)
        if existed:
            BackupManager.backup_file(game_dir, patch_pck_path)

        blob = build_godot_pck_blob(
            packed_files,
            pack_format_version=base_pck.pack_format_version,
            godot_version=(base_pck.godot_major, base_pck.godot_minor, base_pck.godot_patch),
        )
        if not existed:
            BackupManager.register_created_file(game_dir, patch_pck_path)
        atomic_write_bytes(patch_pck_path, blob)
        return patch_pck_path

    def inject(
        self,
        engine: EngineInfo,
        skip_runtime_font: bool = False,
        custom_font_path: Optional[str] = None,
    ) -> InjectionResult:
        """حقن الخط العربي وإعدادات override.cfg لمحرك Godot 3 / Godot 4."""
        translation_res_paths = _collect_translation_res_paths(engine)
        has_custom_theme, custom_theme_res_path, orig_theme_text = _find_custom_theme_source(engine)

        if skip_runtime_font:
            if not translation_res_paths:
                return InjectionResult(
                    success=True,
                    method="skipped",
                    message="تم تخطي حقن الخط العربي بناءً على طلب المستخدم.",
                )
            cfg_path = self._upsert_override_cfg(
                engine,
                font_res_path=None,
                translation_res_paths=translation_res_paths,
                update_locale=True,
            )
            return InjectionResult(
                success=True,
                method="godot_override_locale_only",
                output_files=[cfg_path],
                message="تم تحديث override.cfg بمسارات الترجمة مع تخطي حقن الخط.",
            )

        dst_font, font_res_path, theme_res_path, font_files = self.deploy_arabic_font(
            engine,
            custom_font_path=custom_font_path,
            create_theme_override=has_custom_theme,
            orig_theme_text=orig_theme_text,
        )
        if not dst_font or not font_res_path:
            return InjectionResult(
                success=False,
                method="font_not_found",
                message="تعذر العثور على ملف الخط العربي (IBMPlexSansArabic-Regular.ttf).",
            )

        autoload_res_path = self._deploy_project_autoload_if_unpacked(
            engine, font_res_path, custom_theme_res_path=custom_theme_res_path
        )
        cfg_path = self._upsert_override_cfg(
            engine,
            font_res_path=font_res_path,
            translation_res_paths=translation_res_paths,
            update_locale=True,
            theme_res_path=theme_res_path,
            autoload_res_path=autoload_res_path,
        )
        output_files = list(font_files) + [cfg_path]
        if autoload_res_path:
            output_files.append(
                os.path.join(os.path.abspath(engine.game_dir), ARABIC_DIR_NAME, ARABIC_AUTOLOAD_GD)
            )
        patch_pck = self._build_supplementary_pck(engine)
        if patch_pck:
            output_files.append(patch_pck)

        return InjectionResult(
            success=True,
            method="godot_override_cfg",
            output_files=output_files,
            message=(
                f"تم نشر الخط العربي ({os.path.basename(dst_font)}) وتحديث override.cfg "
                f"({engine.engine_name}) بنجاح."
            ),
        )

    def update_arabic_font(
        self,
        engine: EngineInfo,
        custom_font_path: Optional[str] = None,
    ) -> InjectionResult:
        """إضافة أو تحديث الخط العربي الافتراضي في override.cfg دون مساس بملفات الترجمة."""
        has_custom_theme, _, orig_theme_text = _find_custom_theme_source(engine)
        dst_font, font_res_path, theme_res_path, font_files = self.deploy_arabic_font(
            engine,
            custom_font_path=custom_font_path,
            create_theme_override=has_custom_theme,
            orig_theme_text=orig_theme_text,
        )
        if not dst_font or not font_res_path:
            return InjectionResult(
                success=False,
                method="font_not_found",
                message="تعذر العثور على ملف الخط العربي (IBMPlexSansArabic-Regular.ttf).",
            )

        cfg_path = os.path.join(os.path.abspath(engine.game_dir), OVERRIDE_FILENAME)
        cfg_exists = os.path.isfile(cfg_path)
        translation_res_paths = _collect_translation_res_paths(engine)
        cfg_path = self._upsert_override_cfg(
            engine,
            font_res_path=font_res_path,
            translation_res_paths=translation_res_paths,
            update_locale=not cfg_exists,
            theme_res_path=theme_res_path,
        )
        output_files = list(font_files) + [cfg_path]
        patch_pck = self._build_supplementary_pck(engine)
        if patch_pck:
            output_files.append(patch_pck)

        return InjectionResult(
            success=True,
            method="godot_override_cfg",
            output_files=output_files,
            message=(
                f"تم تحديث الخط العربي ({os.path.basename(dst_font)}) في override.cfg بنجاح."
            ),
        )
