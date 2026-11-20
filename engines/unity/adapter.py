# -*- coding: utf-8 -*-
"""محوّل محرك Unity الكامل الذي يربط الاكتشاف والماسح والحاقن عبر GameEngineAdapter."""

import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from core.models import CandidateFile, EngineInfo, InjectionResult
from engines.base import GameEngineAdapter
from engines.unity.assets_extractor import extract_assets_localization_blocks
from engines.unity.detector import UnityDetector
from engines.unity.injector import UnityInjector
from engines.unity.scanner import (
    CANDIDATE_EXTENSIONS,
    ENGLISH_FILE_STEMS,
    SIBLING_LANG_KEYWORDS,
    UnityLanguageScanner,
    is_sibling_lang_asset,
    is_translatable_value,
)
from formats.registry import get_format_handler


class UnityAdapter(GameEngineAdapter):
    """تنفيذ واجهة GameEngineAdapter لمحرك Unity."""

    def __init__(self) -> None:
        self.detector = UnityDetector()
        self.scanner = UnityLanguageScanner()
        self.injector = UnityInjector()
        self.deep_scan: bool = True
        self.options: Dict[str, Any] = {}

    def configure(self, options: Optional[Dict[str, Any]] = None) -> None:
        super().configure(options)
        self.deep_scan = bool(self.options.get("deep_scan", True))

    def detect(self, game_dir: str) -> EngineInfo:
        return self.detector.detect(game_dir)

    def scan_language_files(self, engine: EngineInfo) -> Tuple[List[CandidateFile], List[str]]:
        return self.scanner.scan(engine, deep_scan=self.deep_scan)

    def is_translatable_value(self, key: str, value: str) -> bool:
        return is_translatable_value(key, value)

    @staticmethod
    def _stem_tokens(rel_path: str) -> List[str]:
        base = os.path.basename(rel_path.replace("\\", "/")).lower()
        stem = os.path.splitext(base)[0]
        return [tok for tok in re.split(r"[._\-\s]+", stem) if tok]

    @staticmethod
    def _dir_has_sibling_languages(
        dir_norm: str, sample_path: str, candidates: List[CandidateFile]
    ) -> bool:
        """التحقق مما إذا كان المجلد يحتوي على ملفات لغات شقيقة (مثل French/German بجانب English)."""
        stems: Set[str] = set()
        for c in candidates:
            if c.format_name in ("unity_assets", "unity_code"):
                continue
            c_dir = os.path.dirname(c.relative_path.replace("\\", "/")).lower()
            if c_dir == dir_norm:
                stems.add(os.path.splitext(os.path.basename(c.relative_path))[0].lower())

        sample_dir = os.path.dirname(sample_path) if sample_path else ""
        if sample_dir and os.path.isdir(sample_dir):
            try:
                for fname in os.listdir(sample_dir):
                    low = fname.lower()
                    if low.endswith((".bak", ".tmp")):
                        continue
                    ext = os.path.splitext(low)[1]
                    if ext in CANDIDATE_EXTENSIONS:
                        stems.add(os.path.splitext(low)[0])
            except OSError:
                pass

        has_english_stem = False
        sibling_lang_count = 0
        for stem in stems:
            base_stem = stem.split("(")[0].strip()
            tokens = [tok for tok in re.split(r"[._\-\s]+", stem) if tok]
            if base_stem in ENGLISH_FILE_STEMS or any(t in ENGLISH_FILE_STEMS for t in tokens):
                has_english_stem = True
            if base_stem in SIBLING_LANG_KEYWORDS or any(t in SIBLING_LANG_KEYWORDS for t in tokens):
                sibling_lang_count += 1

        return sibling_lang_count >= 2 or (sibling_lang_count >= 1 and has_english_stem)

    @staticmethod
    def _collect_external_source_texts(external_cands: List[CandidateFile]) -> Set[str]:
        ext_texts: Set[str] = set()
        for ext_cand in external_cands:
            handler = get_format_handler(ext_cand.format_name)
            if handler is None:
                continue
            read_path = ext_cand.path
            bak_path = ext_cand.path + ".bak"
            if ext_cand.contains_arabic and os.path.isfile(bak_path):
                read_path = bak_path
            try:
                doc = handler.load(read_path)
            except Exception:
                continue
            for _, _, val in doc.entries[:5000]:
                if isinstance(val, str):
                    norm = val.strip().lower()
                    if norm:
                        ext_texts.add(norm)
        return ext_texts

    def select_language_files(
        self,
        engine: EngineInfo,
        candidates: List[CandidateFile],
        min_score: int = 45,
    ) -> List[CandidateFile]:
        """اختيار ملفات اللغة المتكاملة (خارجية + أصول متعددة غير مكررة) وتجنب اللغات الشقيقة."""
        if not candidates or candidates[0].score < min_score:
            return []

        top = candidates[0]
        if top.format_name == "unity_assets":
            asset_cands = [
                c
                for c in candidates
                if c.format_name == "unity_assets"
                and c.score >= min_score
                and c.translatable_entries > 0
            ]
            non_sibling = [
                c for c in asset_cands if not is_sibling_lang_asset(c.relative_path)
            ]
            return non_sibling if non_sibling else asset_cands

        if top.format_name == "unity_code":
            return [top]

        top_norm = top.relative_path.replace("\\", "/")
        top_dir = os.path.dirname(top_norm).lower()
        dir_parts = [p for p in top_dir.split("/") if p]
        in_explicit_en_dir = any(p in ENGLISH_FILE_STEMS for p in dir_parts)

        top_tokens = self._stem_tokens(top_norm)
        top_has_en_suffix = any(t in ENGLISH_FILE_STEMS for t in top_tokens) and len(top_tokens) >= 2
        top_stem_base = (
            os.path.splitext(os.path.basename(top_norm))[0].lower().split("(")[0].strip()
        )
        top_in_sibling_dir = any(p in SIBLING_LANG_KEYWORDS for p in dir_parts)
        is_split_loc_dir = (
            not in_explicit_en_dir
            and not top_has_en_suffix
            and bool(top_dir)
            and not top_in_sibling_dir
            and top_stem_base not in ENGLISH_FILE_STEMS
            and not self._dir_has_sibling_languages(top_dir, top.path, candidates)
        )

        selected: List[CandidateFile] = [top]
        seen = {top.relative_path}

        if in_explicit_en_dir or top_has_en_suffix:
            for cand in candidates[1:]:
                if cand.relative_path in seen:
                    continue
                if cand.score < min_score or cand.translatable_entries <= 0:
                    continue
                if cand.format_name in ("unity_assets", "unity_code"):
                    continue

                cand_norm = cand.relative_path.replace("\\", "/")
                cand_dir = os.path.dirname(cand_norm).lower()
                cand_tokens = self._stem_tokens(cand_norm)
                if any(t in SIBLING_LANG_KEYWORDS for t in cand_tokens):
                    continue

                if in_explicit_en_dir:
                    same_en_tree = cand_dir == top_dir or (top_dir and cand_dir.startswith(top_dir + "/"))
                    if not same_en_tree:
                        continue
                else:
                    if cand_dir != top_dir:
                        continue
                    if not (any(t in ENGLISH_FILE_STEMS for t in cand_tokens) and len(cand_tokens) >= 2):
                        continue

                seen.add(cand.relative_path)
                selected.append(cand)
        elif is_split_loc_dir:
            dll_confirmed_dirs: Set[str] = set()
            for cand in candidates:
                if cand.score < min_score or cand.translatable_entries <= 0:
                    continue
                if cand.format_name != top.format_name or not cand.dll_matched:
                    continue
                c_dir = os.path.dirname(cand.relative_path.replace("\\", "/")).lower()
                if not c_dir or c_dir == top_dir:
                    continue
                c_dir_parts = [p for p in c_dir.split("/") if p]
                if any(p in SIBLING_LANG_KEYWORDS for p in c_dir_parts):
                    continue
                if not self._dir_has_sibling_languages(c_dir, cand.path, candidates):
                    dll_confirmed_dirs.add(c_dir)

            for cand in candidates[1:]:
                if cand.relative_path in seen:
                    continue
                if cand.score < min_score or cand.translatable_entries <= 0:
                    continue
                if cand.format_name != top.format_name:
                    continue

                cand_norm = cand.relative_path.replace("\\", "/")
                cand_dir = os.path.dirname(cand_norm).lower()
                cand_dir_parts = [p for p in cand_dir.split("/") if p]
                cand_tokens = self._stem_tokens(cand_norm)
                cand_stem_base = (
                    os.path.splitext(os.path.basename(cand_norm))[0].lower().split("(")[0].strip()
                )
                if any(p in SIBLING_LANG_KEYWORDS for p in cand_dir_parts):
                    continue
                if any(t in SIBLING_LANG_KEYWORDS for t in cand_tokens):
                    continue
                if cand_stem_base in ENGLISH_FILE_STEMS:
                    continue

                in_top_tree = cand_dir == top_dir or (top_dir and cand_dir.startswith(top_dir + "/"))
                in_dll_split_dir = cand_dir in dll_confirmed_dirs
                if in_top_tree or in_dll_split_dir:
                    seen.add(cand.relative_path)
                    selected.append(cand)

        if self.deep_scan:
            asset_cands = [
                c
                for c in candidates
                if c.format_name == "unity_assets"
                and c.score >= min_score
                and c.translatable_entries >= 1
                and c.relative_path not in seen
                and not is_sibling_lang_asset(c.relative_path)
            ]
            if asset_cands:
                ext_texts = self._collect_external_source_texts(selected)
                for cand in asset_cands:
                    blocks = extract_assets_localization_blocks(cand.path)
                    if not blocks:
                        continue
                    has_verified_ui = any(
                        blk.block_kind in ("UGUI:Text", "TMP:Text")
                        or (blk.block_kind == "SceneText:binary" and "_textmesh" in blk.block_name.lower())
                        for blk in blocks
                    )
                    has_structured = any(blk.block_kind != "SceneText:binary" for blk in blocks)
                    cand_vals = {
                        val.strip().lower()
                        for blk in blocks
                        for _, flat_key, val in blk.entries
                        if isinstance(val, str) and is_translatable_value(flat_key, val)
                    }
                    new_unique = len(cand_vals - ext_texts)
                    if (
                        (has_verified_ui and new_unique >= 1)
                        or (has_structured and new_unique >= 3)
                        or (not has_structured and cand.score >= 55 and new_unique >= 5)
                    ):
                        seen.add(cand.relative_path)
                        selected.append(cand)

        return selected

    def inject(
        self,
        engine: EngineInfo,
        language_file: Optional[CandidateFile],
        skip_runtime_font: bool = False,
    ) -> InjectionResult:
        return self.injector.inject(engine, skip_runtime_font=skip_runtime_font)

    def update_font(
        self,
        engine: EngineInfo,
        custom_font_path: Optional[str] = None,
    ) -> InjectionResult:
        return self.injector.update_arabic_font(engine, custom_font_path=custom_font_path)
