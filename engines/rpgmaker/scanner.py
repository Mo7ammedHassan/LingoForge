# -*- coding: utf-8 -*-
"""ماسح ومصفّي ملفات اللغة لمحرك RPG Maker MV / MZ."""

import os
import re
from typing import List, Tuple

from core.backup import is_file_likely_arabic
from core.models import CandidateFile, EngineInfo
from formats.json_handler import flatten_json_strings, load_json_file


ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF\uFE70-\uFEFF]")
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")

# ملفات البيانات المعروفة في RPG Maker MV/MZ مع أوزانها الأساسية
KNOWN_DATA_FILE_SCORES = {
    "system.json": 95,
    "actors.json": 85,
    "classes.json": 85,
    "skills.json": 85,
    "items.json": 85,
    "weapons.json": 85,
    "armors.json": 85,
    "enemies.json": 85,
    "states.json": 85,
    "troops.json": 80,
    "commonevents.json": 80,
    "mapinfos.json": 70,
}

IGNORED_DATA_FILES = frozenset({
    "tilesets.json",
    "animations.json",
})

MAP_FILE_RE = re.compile(r"^map\d+\.json$", re.IGNORECASE)

# مفاتيح تقنية خالصة يجب تجاهلها دائماً
IGNORED_EXACT_SUFFIXES = (
    ".note",
    ".formula",
    ".locale",
    ".charactername",
    ".facename",
    ".battlername",
    ".title1name",
    ".title2name",
    ".battleback1name",
    ".battleback2name",
    ".parallaxname",
    ".tilesetname",
    ".imagename",
    ".animationname",
    ".pluginname",
    ".commandname",
    ".code",
    ".indent",
    ".id",
    ".iconindex",
    ".tilesetid",
    ".animationid",
    ".versionid",
)

IGNORED_KEY_SEGMENTS = (
    ".bgm.",
    ".bgs.",
    ".me.",
    ".se.",
    ".sounds.",
    ".switches.",
    ".variables.",
    ".meta.",
    ".tilesetnames.",
)

IGNORED_KEY_PREFIXES = (
    "switches.",
    "variables.",
    "sounds.",
    "bgm.",
    "bgs.",
    "me.",
    "se.",
    "airship.",
    "boat.",
    "ship.",
    "titlebgm.",
    "battlebgm.",
    "victoryme.",
    "defeatme.",
    "gameoverme.",
    "tilesetnames.",
    "advanced.",
)

EVENT_EDITOR_NAME_RE = re.compile(r"(?:^|\.)events\.\d+\.name$", re.IGNORECASE)
PARAM_OBJECT_NAME_RE = re.compile(r"\.parameters\.\d+\.name$", re.IGNORECASE)

ASSET_EXT_RE = re.compile(
    r"\.(?:png|jpe?g|webp|gif|bmp|ogg|m4a|wav|mp3|webm|mp4|js|json|css|html?|ttf|otf|woff2?)$",
    re.IGNORECASE,
)
ASSET_ID_NO_SPACE_RE = re.compile(r"^(?:[A-Za-z]+_+[A-Za-z0-9_]*|[A-Za-z]+\d+[A-Za-z0-9_]*)$")
JS_CODE_RE = re.compile(
    r"(?:\$game[A-Z]|\$data[A-Z]|\bfunction\s*\(|\b(?:var|let|const|return)\s+|=>|"
    r"\b(?:AudioManager|SceneManager|BattleManager|ConfigManager|DataManager|ImageManager|SoundManager|Graphics|Input)\.)"
)
NOTETAG_ONLY_RE = re.compile(r"^\s*(?:<[^<>]+>\s*)+$")


def is_translatable_value(key: str, value: str) -> bool:
    """تحديد هل القيمة نص بشري قابل للترجمة في بيانات RPG Maker MV/MZ."""
    if not isinstance(value, str):
        return False

    stripped = value.strip()
    if not stripped:
        return False

    # تجاهل النصوص المعرّبة مسبقاً أو الخالية من الأحرف اللاتينية
    if ARABIC_CHAR_RE.search(stripped):
        return False
    if not ASCII_LETTER_RE.search(stripped):
        return False

    key_lower = key.lower()

    if key_lower in ("note", "locale") or key_lower.endswith(IGNORED_EXACT_SUFFIXES):
        return False
    if any(key_lower.startswith(prefix) for prefix in IGNORED_KEY_PREFIXES):
        return False
    if any(seg in key_lower for seg in IGNORED_KEY_SEGMENTS):
        return False
    if EVENT_EDITOR_NAME_RE.search(key_lower) or PARAM_OBJECT_NAME_RE.search(key_lower):
        return False

    # تجاهل أسماء الملفات، الملاحظات البرمجية، والسكربتات
    if ASSET_EXT_RE.search(stripped):
        return False
    if NOTETAG_ONLY_RE.match(stripped):
        return False
    if JS_CODE_RE.search(stripped):
        return False
    if " " not in stripped and ASSET_ID_NO_SPACE_RE.match(stripped):
        return False
    if stripped.startswith(("http://", "https://", "www/")):
        return False

    return True


def is_translatable_entry_in_data(
    raw_data: object,
    path_tuple: tuple,
    key: str,
    value: str,
) -> bool:
    """التحقق من قابلية الترجمة مع فحص رمز أمر الحدث (code) في RPG Maker MV/MZ."""
    if not is_translatable_value(key, value):
        return False

    if "parameters" not in path_tuple or raw_data is None:
        return True

    param_pos = path_tuple.index("parameters")
    cursor = raw_data
    try:
        for seg in path_tuple[:param_pos]:
            cursor = cursor[seg]
    except (KeyError, IndexError, TypeError):
        return True

    if not isinstance(cursor, dict) or "code" not in cursor:
        return True

    code = cursor.get("code")
    sub_path = path_tuple[param_pos + 1 :]
    if not sub_path:
        return False
    param_idx = sub_path[0]

    # 401: Show Text, 405: Show Scrolling Text
    if code in (401, 405):
        return param_idx == 0 and len(sub_path) == 1
    # 101: Show Text header -> parameters[0]=faceName (صورة), parameters[4]=speakerName (MZ)
    if code == 101:
        return param_idx == 4 and len(sub_path) == 1
    # 102: Show Choices -> parameters[0] قائمة خيارات نصية
    if code == 102:
        return param_idx == 0 and len(sub_path) == 2
    # 402: When [Choice], 320: Change Name, 324: Change Nickname, 325: Change Profile
    if code in (402, 320, 324, 325):
        return param_idx == 1 and len(sub_path) == 1

    # باقي الأوامر (108 تعليقات، 355 سكربت، 356/357 أوامر بلجن، 231/283/284/322 صور، 241 صوت) تقنية
    return False


def _score_data_file(filename: str, translatable_count: int) -> int:
    """حساب درجة الثقة لملف بيانات RPG Maker."""
    lower = filename.lower()
    if lower in IGNORED_DATA_FILES or translatable_count <= 0:
        return 10

    if lower in KNOWN_DATA_FILE_SCORES:
        return KNOWN_DATA_FILE_SCORES[lower]
    if MAP_FILE_RE.match(lower):
        return 90
    return 60


class RPGMakerLanguageScanner:
    """يمسح مجلد data/ (أو www/data/) في RPG Maker MV/MZ ويجمع ملفات النصوص."""

    def scan(self, engine: EngineInfo) -> Tuple[List[CandidateFile], List[str]]:
        candidates: List[CandidateFile] = []
        hints: List[str] = [f"Engine: {engine.engine_name} ({engine.scripting_backend})"]

        data_dir = engine.data_dir
        if not data_dir or not os.path.isdir(data_dir):
            return candidates, hints

        hints.append(f"Data directory: {os.path.relpath(data_dir, engine.game_dir).replace(os.sep, '/')}")

        try:
            entries = sorted(os.listdir(data_dir))
        except OSError:
            return candidates, hints

        for fname in entries:
            if not fname.lower().endswith(".json"):
                continue
            full_path = os.path.join(data_dir, fname)
            if not os.path.isfile(full_path):
                continue

            rel_path = os.path.relpath(full_path, engine.game_dir).replace(os.sep, "/")
            try:
                raw_data = load_json_file(full_path)
                flat_items = flatten_json_strings(raw_data)
            except Exception:
                continue

            total_entries = len(flat_items)
            if fname.lower() in IGNORED_DATA_FILES:
                translatable_entries = 0
            else:
                translatable_entries = sum(
                    1
                    for path_tuple, flat_key, val in flat_items
                    if is_translatable_entry_in_data(raw_data, path_tuple, flat_key, val)
                )
            score = _score_data_file(fname, translatable_entries)
            contains_arabic = is_file_likely_arabic(full_path)

            reasons = [f"RPG Maker data ({translatable_entries}/{total_entries} نص قابل للترجمة)"]
            if contains_arabic:
                reasons.append("يحتوي على نصوص عربية مسبقاً")

            candidates.append(
                CandidateFile(
                    path=full_path,
                    relative_path=rel_path,
                    format_name="json",
                    score=score,
                    reasons=reasons,
                    dll_matched=False,
                    contains_arabic=contains_arabic,
                    total_entries=total_entries,
                    translatable_entries=translatable_entries,
                )
            )

        candidates.sort(key=lambda c: (-c.score, -c.translatable_entries, c.relative_path))
        return candidates, hints
