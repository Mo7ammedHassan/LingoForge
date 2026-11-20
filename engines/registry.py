# -*- coding: utf-8 -*-
"""سجل محركات الألعاب واختيار المحوّل المناسب تلقائياً حسب مجلد اللعبة."""

from typing import Callable, List, Optional, Sequence, Tuple

from core.models import EngineInfo
from engines.base import GameEngineAdapter
from engines.godot.adapter import GodotAdapter
from engines.rpgmaker.adapter import RPGMakerAdapter
from engines.unity.adapter import UnityAdapter
from engines.unreal.adapter import UnrealAdapter


# لإضافة محرك جديد: أضف مصنع المحوّل إلى هذه القائمة.
_ADAPTER_FACTORIES: Tuple[Callable[[], GameEngineAdapter], ...] = (
    UnityAdapter,
    RPGMakerAdapter,
    GodotAdapter,
    UnrealAdapter,
)


def get_registered_adapters() -> List[GameEngineAdapter]:
    """إنشاء وإرجاع قائمة بجميع محوّلات المحركات المسجلة."""
    return [factory() for factory in _ADAPTER_FACTORIES]


def get_default_adapter() -> GameEngineAdapter:
    """إرجاع المحوّل الافتراضي الأول (Unity) لأغراض التوافق المسبق."""
    return _ADAPTER_FACTORIES[0]()


def detect_engine_adapter(
    game_dir: str,
    adapters: Optional[Sequence[GameEngineAdapter]] = None,
) -> Tuple[GameEngineAdapter, EngineInfo]:
    """فحص مجلد اللعبة عبر المحركات المسجلة وإرجاع أول محوّل يدعمها."""
    candidates = list(adapters) if adapters is not None else get_registered_adapters()
    if not candidates:
        raise RuntimeError("لا توجد محركات مسجلة في سجل المحركات.")

    last_adapter = candidates[0]
    last_info: Optional[EngineInfo] = None
    recognized_unsupported: Optional[Tuple[GameEngineAdapter, EngineInfo]] = None

    for adapter in candidates:
        info = adapter.detect(game_dir)
        last_adapter = adapter
        last_info = info
        if info.supported:
            return adapter, info
        if (
            recognized_unsupported is None
            and info.engine_name not in ("", "Unknown", "Unity", "RPG Maker", "Godot", "Unreal Engine")
        ):
            recognized_unsupported = (adapter, info)

    if recognized_unsupported is not None:
        return recognized_unsupported

    if last_info is None:
        last_info = EngineInfo(
            engine_name="Unknown",
            supported=False,
            game_dir=game_dir,
            unsupported_reason="تعذر التعرف على محرك اللعبة.",
        )
    elif len(candidates) > 1 and not last_info.unsupported_reason:
        last_info.unsupported_reason = "لم يتم التعرف على محرك مدعوم في مجلد اللعبة المحدد."

    return last_adapter, last_info
