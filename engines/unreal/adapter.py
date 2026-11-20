# -*- coding: utf-8 -*-
"""محوّل محرك Unreal Engine (UE4 / UE5) الكامل عبر GameEngineAdapter."""

from typing import List, Optional, Tuple

from core.models import CandidateFile, EngineInfo, InjectionResult
from engines.base import GameEngineAdapter
from engines.unreal.detector import UnrealDetector
from engines.unreal.injector import UnrealInjector
from engines.unreal.scanner import UnrealLanguageScanner, is_translatable_value


class UnrealAdapter(GameEngineAdapter):
    """تنفيذ واجهة GameEngineAdapter لمحرك Unreal Engine 4 و Unreal Engine 5."""

    def __init__(self, requires_arabic_shaping: Optional[bool] = None) -> None:
        self._shaping_override = requires_arabic_shaping
        self.detector = UnrealDetector()
        self.scanner = UnrealLanguageScanner()
        self.injector = UnrealInjector()

    def detect(self, game_dir: str) -> EngineInfo:
        info = self.detector.detect(game_dir)
        if self._shaping_override is not None:
            info.requires_arabic_shaping = self._shaping_override
        return info

    def scan_language_files(self, engine: EngineInfo) -> Tuple[List[CandidateFile], List[str]]:
        return self.scanner.scan(engine)

    def select_language_files(
        self,
        engine: EngineInfo,
        candidates: List[CandidateFile],
        min_score: int = 45,
    ) -> List[CandidateFile]:
        """تحديد ملفات .locres المصدر القابلة للترجمة مع استبعاد التكرار."""
        selected: List[CandidateFile] = []
        seen_paths = set()
        for c in candidates:
            if c.score < min_score or c.translatable_entries <= 0:
                continue
            key = c.relative_path.split("::")[-1].lower()
            if key in seen_paths:
                continue
            seen_paths.add(key)
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

    def requires_arabic_shaping(self, engine: EngineInfo) -> bool:
        if self._shaping_override is not None:
            return self._shaping_override
        return engine.requires_arabic_shaping
