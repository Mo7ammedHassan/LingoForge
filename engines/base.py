# -*- coding: utf-8 -*-
"""الواجهة المجردة لمحركات الألعاب (Adapter Pattern) لتسهيل دعم محركات جديدة مستقبلاً."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from core.models import CandidateFile, EngineInfo, InjectionResult


class GameEngineAdapter(ABC):
    """واجهة موحدة يجب أن يطبقها أي محرك ألعاب (Unity, Unreal, Godot, ...)."""

    @abstractmethod
    def detect(self, game_dir: str) -> Optional[EngineInfo]:
        """فحص مجلد اللعبة وتحديد معلومات المحرك."""

    @abstractmethod
    def scan_language_files(self, engine: EngineInfo) -> Tuple[List[CandidateFile], List[str]]:
        """البحث عن ملفات اللغة وتقييمها وإرجاع الأدلة المكتشفة."""

    @abstractmethod
    def is_translatable_value(self, key: str, value: str) -> bool:
        """تحديد هل القيمة نص بشري قابل للترجمة أم رمز تقني برمجي."""

    def is_translatable_entry(
        self,
        doc: object,
        path_tuple: tuple,
        key: str,
        value: str,
    ) -> bool:
        """تحديد قابلية الترجمة مع إمكانية فحص بنية المستند الكاملة (اختياري للمحركات)."""
        return self.is_translatable_value(key, value)

    @abstractmethod
    def inject(
        self,
        engine: EngineInfo,
        language_file: Optional[CandidateFile],
        skip_runtime_font: bool = False,
    ) -> InjectionResult:
        """تنفيذ الحقن وقت التشغيل (مثل بناء وتثبيت حاقن الخط العربي)."""

    def select_language_files(
        self,
        engine: EngineInfo,
        candidates: List[CandidateFile],
        min_score: int = 45,
    ) -> List[CandidateFile]:
        """اختيار ملف (أو ملفات) اللغة المستهدفة للترجمة من قائمة المرشحين."""
        if candidates and candidates[0].score >= min_score:
            return [candidates[0]]
        return []

    def update_font(
        self,
        engine: EngineInfo,
        custom_font_path: Optional[str] = None,
    ) -> InjectionResult:
        """إضافة أو تحديث الخط العربي الافتراضي على لعبة معرّبة مسبقاً."""
        return InjectionResult(
            success=False,
            method="none",
            message=f"تحديث الخط المستقل غير مدعوم لمحرك {engine.engine_name}.",
        )

    def requires_arabic_shaping(self, engine: EngineInfo) -> bool:
        """تحديد هل يحتاج المحرك إلى تشبيك الحروف وعكس الاتجاه (LTR) أم يدعم RTL أصلاً."""
        return engine.requires_arabic_shaping

    def configure(self, options: Optional[Dict[str, Any]] = None) -> None:
        """تمرير خيارات اختيارية خاصة بالمحرك (مثل rpgmaker_text_fix) من الواجهة/CLI.

        التنفيذ الافتراضي يحفظ الخيارات فقط؛ المحولات تتجاوزه لتطبيق ما يخصها.
        """
        self.options = dict(options or {})
