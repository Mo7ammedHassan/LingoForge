# -*- coding: utf-8 -*-
"""محرك الترجمة السحابية المجمعة عبر POST مع إزالة التكرار، تبديل الخوادم، الانقسام الثنائي، والحفظ الذري."""

from abc import ABC, abstractmethod
import json
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

from core.backup import atomic_copy_file, atomic_write_text


class TranslationInterruptedError(Exception):
    """استثناء آمن عند إيقاف الترجمة أو انقطاعها بعد حفظ الدفعات المكتملة على القرص."""

    def __init__(self, completed_lines: int, total_lines: int, message: str = "") -> None:
        self.completed_lines = completed_lines
        self.total_lines = total_lines
        super().__init__(
            message or f"تم حفظ التقدم وإيقاف الترجمة عند ({completed_lines}/{total_lines})."
        )


class TranslatorProvider(ABC):
    """الواجهة الموحدة لأي مزود ترجمة (سحابي أو محلي Offline مستقبلاً)."""

    @abstractmethod
    def translate_lines(
        self,
        lines: List[str],
        source_lang: str = "en",
        target_lang: str = "ar",
        cache_path: Optional[str] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[Dict[str, str], int, int]:
        """ترجمة قائمة أسطر وإرجاع (خريطة الترجمة، عدد الدفعات المرسلة، عدد الأسطر من الكاش)."""


class GoogleBatchTranslator(TranslatorProvider):
    """ترجمة مجمعة عبر Google Translate POST بدفعات كبيرة ومعالجة ذاتية للأخطاء."""

    ENDPOINTS = ["gtx", "clients5", "webapp"]

    def __init__(
        self,
        max_chars: int = 3800,
        max_lines: int = 55,
        inter_batch_delay: float = 0.35,
        timeout: int = 25,
        endpoint_caller: Optional[Callable[[str, str, str, str], str]] = None,
    ) -> None:
        self.max_chars = max_chars
        self.max_lines = max_lines
        self.inter_batch_delay = inter_batch_delay
        self.timeout = timeout
        self._endpoint_caller = endpoint_caller or self._call_google_endpoint

    def _call_google_endpoint(
        self, batch_text: str, endpoint_name: str, source_lang: str, target_lang: str
    ) -> str:
        body = urllib.parse.urlencode({"q": batch_text}).encode("utf-8")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }

        if endpoint_name in ("gtx", "webapp"):
            url = (
                f"https://translate.googleapis.com/translate_a/single"
                f"?client={endpoint_name}&sl={source_lang}&tl={target_lang}&dt=t"
            )
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return "".join(seg[0] for seg in payload[0] if seg and seg[0])

        if endpoint_name == "clients5":
            url = (
                f"https://clients5.google.com/translate_a/t"
                f"?client=dict-chrome-ex&sl={source_lang}&tl={target_lang}"
            )
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if isinstance(payload, list) and payload:
                first = payload[0]
                if isinstance(first, str):
                    return first
                if isinstance(first, list) and first and isinstance(first[0], str):
                    return first[0]
            if isinstance(payload, str):
                return payload
            raise ValueError(f"تنسيق استجابة غير متوقع من clients5: {type(payload)}")

        raise ValueError(f"نقطة نهاية غير معروفة: {endpoint_name}")

    def build_batches(self, unique_lines: List[str]) -> List[List[str]]:
        """تجميع الأسطر الفريدة في دفعات ضمن حد الأحرف والأسطر لتفادي حظر 429."""
        batches: List[List[str]] = []
        cur: List[str] = []
        cur_len = 0

        for text in unique_lines:
            add_len = len(text) + 1
            if cur and (cur_len + add_len > self.max_chars or len(cur) >= self.max_lines):
                batches.append(cur)
                cur = [text]
                cur_len = add_len
            else:
                cur.append(text)
                cur_len += add_len

        if cur:
            batches.append(cur)
        return batches

    def _translate_batch_with_retry(
        self,
        lines: List[str],
        source_lang: str,
        target_lang: str,
        endpoint_idx_ref: List[int],
    ) -> List[str]:
        if not lines:
            return []

        batch_text = "\n".join(lines)
        max_attempts = 9
        last_error: Optional[Exception] = None

        for attempt in range(max_attempts):
            ep = self.ENDPOINTS[endpoint_idx_ref[0] % len(self.ENDPOINTS)]
            try:
                translated_raw = self._endpoint_caller(batch_text, ep, source_lang, target_lang)
                out_lines = [ln.rstrip("\r") for ln in translated_raw.split("\n")]

                if len(out_lines) == len(lines):
                    return out_lines

                if len(lines) == 1:
                    joined = " ".join(ln.strip() for ln in out_lines if ln.strip())
                    return [joined or translated_raw.strip()]

                # الانقسام الثنائي التلقائي عند اختلاف عدد الأسطر من المترجم
                mid = len(lines) // 2
                left = self._translate_batch_with_retry(lines[:mid], source_lang, target_lang, endpoint_idx_ref)
                right = self._translate_batch_with_retry(lines[mid:], source_lang, target_lang, endpoint_idx_ref)
                return left + right

            except urllib.error.HTTPError as exc:
                last_error = exc
                endpoint_idx_ref[0] += 1
                wait = min(30.0, 1.5 * (2 ** min(attempt, 4)))
                time.sleep(wait)
            except Exception as exc:
                last_error = exc
                endpoint_idx_ref[0] += 1
                wait = min(20.0, 1.0 * (2 ** min(attempt, 4)))
                time.sleep(wait)

        raise RuntimeError(f"فشلت الترجمة بعد {max_attempts} محاولات: {last_error}")

    @staticmethod
    def _get_wal_path(cache_path: Optional[str]) -> Optional[str]:
        return (cache_path + ".wal.jsonl") if cache_path else None

    @staticmethod
    def _get_prev_cache_path(cache_path: Optional[str]) -> Optional[str]:
        return (cache_path + ".prev") if cache_path else None

    @classmethod
    def _load_cache(cls, cache_path: Optional[str]) -> Dict[str, str]:
        if not cache_path:
            return {}
        cache_map: Dict[str, str] = {}
        prev_path = cls._get_prev_cache_path(cache_path)
        for candidate in (cache_path, prev_path):
            if not candidate or not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    cache_map = {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
                    break
            except Exception:
                continue

        wal_path = cls._get_wal_path(cache_path)
        if wal_path and os.path.isfile(wal_path):
            try:
                with open(wal_path, "r", encoding="utf-8", errors="replace") as f:
                    for raw_line in f:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            pairs = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(pairs, list):
                            for item in pairs:
                                if isinstance(item, (list, tuple)) and len(item) == 2:
                                    k, v = item
                                    if isinstance(k, str) and isinstance(v, str):
                                        cache_map[k] = v
            except OSError:
                pass
        return cache_map

    @classmethod
    def _append_wal_batch(
        cls, cache_path: Optional[str], pairs: List[Tuple[str, str]]
    ) -> None:
        wal_path = cls._get_wal_path(cache_path)
        if not wal_path or not pairs:
            return
        os.makedirs(os.path.dirname(os.path.abspath(wal_path)), exist_ok=True)
        with open(wal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(pairs, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    @classmethod
    def _save_cache_atomic(
        cls, cache_path: Optional[str], cache_map: Dict[str, str]
    ) -> None:
        if not cache_path:
            return
        prev_path = cls._get_prev_cache_path(cache_path)
        if os.path.isfile(cache_path) and prev_path:
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    atomic_copy_file(cache_path, prev_path, tmp_suffix=".prev.tmp")
            except Exception:
                pass
        atomic_write_text(
            cache_path,
            json.dumps(cache_map, ensure_ascii=False, indent=2),
            encoding="utf-8",
            tmp_suffix=".tmp",
        )
        wal_path = cls._get_wal_path(cache_path)
        if wal_path and os.path.isfile(wal_path):
            try:
                os.remove(wal_path)
            except OSError:
                pass

    def translate_lines(
        self,
        lines: List[str],
        source_lang: str = "en",
        target_lang: str = "ar",
        cache_path: Optional[str] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        batch_saved_cb: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[Dict[str, str], int, int]:
        """إزالة التكرار وترجمة الأسطر المتبقية مع الحفظ الفوري بالـ WAL وfsync بعد كل دفعة."""
        cache_map = self._load_cache(cache_path)

        deduped: List[str] = []
        seen = set()
        for ln in lines:
            if ln not in seen:
                seen.add(ln)
                deduped.append(ln)

        pending = [ln for ln in deduped if ln not in cache_map]
        cached_count = len(deduped) - len(pending)
        completed_in_job = cached_count
        total_in_job = len(deduped)

        batches_sent = 0
        if pending:
            if cancel_event is not None and cancel_event.is_set():
                raise TranslationInterruptedError(completed_in_job, total_in_job)

            batches = self.build_batches(pending)
            endpoint_idx_ref = [0]
            total_batches = len(batches)
            try:
                for idx, batch in enumerate(batches, start=1):
                    if cancel_event is not None and cancel_event.is_set():
                        raise TranslationInterruptedError(completed_in_job, total_in_job)

                    out_lines = self._translate_batch_with_retry(
                        batch, source_lang, target_lang, endpoint_idx_ref
                    )
                    batch_pairs = list(zip(batch, out_lines))
                    for src, dst in batch_pairs:
                        cache_map[src] = dst

                    # حفظ كل دفعة فوراً على القرص بـ WAL + fsync لمنع فقدان التقدم عند انقطاع الكهرباء
                    self._append_wal_batch(cache_path, batch_pairs)
                    batches_sent += 1
                    completed_in_job += len(batch_pairs)

                    if batch_saved_cb:
                        batch_saved_cb(completed_in_job, total_in_job)

                    if progress_cb:
                        progress_cb(idx, total_batches)

                    if idx % 25 == 0:
                        self._save_cache_atomic(cache_path, cache_map)

                    if cancel_event is not None and cancel_event.is_set():
                        raise TranslationInterruptedError(completed_in_job, total_in_job)

                    if idx < total_batches and self.inter_batch_delay > 0:
                        if cancel_event is not None:
                            if cancel_event.wait(self.inter_batch_delay):
                                raise TranslationInterruptedError(completed_in_job, total_in_job)
                        else:
                            time.sleep(self.inter_batch_delay)
            except KeyboardInterrupt as exc:
                raise TranslationInterruptedError(completed_in_job, total_in_job) from exc

            self._save_cache_atomic(cache_path, cache_map)

        result = {ln: cache_map[ln] for ln in deduped if ln in cache_map}
        return result, batches_sent, cached_count
