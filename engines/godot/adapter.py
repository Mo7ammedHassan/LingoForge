# -*- coding: utf-8 -*-
"""محوّل محرك Godot (3.x / 4.x) الكامل عبر GameEngineAdapter."""

import os
from typing import List, Optional, Tuple

from core.models import CandidateFile, EngineInfo, InjectionResult
from engines.base import GameEngineAdapter
from engines.godot.detector import GodotDetector
from engines.godot.injector import GodotInjector
from engines.godot.scanner import GodotLanguageScanner, is_translatable_value


class GodotAdapter(GameEngineAdapter):
    """تنفيذ واجهة GameEngineAdapter لمحرك Godot 3.x و Godot 4.x."""

    def __init__(self) -> None:
        self.detector = GodotDetector()
        self.scanner = GodotLanguageScanner()
        self.injector = GodotInjector()

    def detect(self, game_dir: str) -> EngineInfo:
        return self.detector.detect(game_dir)

    def scan_language_files(self, engine: EngineInfo) -> Tuple[List[CandidateFile], List[str]]:
        return self.scanner.scan(engine)

    def select_language_files(
        self,
        engine: EngineInfo,
        candidates: List[CandidateFile],
        min_score: int = 45,
    ) -> List[CandidateFile]:
        """تحديد جميع ملفات اللغة المصدر القابلة للترجمة في Godot مع استبعاد النسخ المكررة."""
        selected: List[CandidateFile] = []
        seen_stems = set()
        for c in candidates:
            if c.score < min_score or c.translatable_entries <= 0:
                continue
            stem_key = c.relative_path.split("::")[-1].lower()
            if stem_key in seen_stems:
                continue
            seen_stems.add(stem_key)
            selected.append(c)
        return selected

    def is_translatable_value(self, key: str, value: str) -> bool:
        return is_translatable_value(key, value)

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
