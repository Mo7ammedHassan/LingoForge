# -*- coding: utf-8 -*-
"""إدارة النسخ الاحتياطي الآمن والاسترجاع مع التحقق من بصمة SHA-256 ومنع إفساد الأصل."""

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
from typing import Dict, List, Optional, Tuple


ARABIC_CHARS_RE = re.compile(r"[\u0600-\u06FF\uFE70-\uFEFF]")
TEXT_EXTENSIONS = {".json", ".csv", ".tsv", ".xml", ".ini", ".po", ".pot", ".txt", ".bytes", ".yaml", ".yml"}
COMMIT_TMP_SUFFIX = ".arabic_commit.tmp"
RESTORE_TMP_SUFFIX = ".restore.tmp"


def atomic_write_bytes(file_path: str, data: bytes, tmp_suffix: str = ".tmp") -> None:
    """كتابة بايتات ذرياً مع flush و fsync قبل الاستبدال لضمان عدم التلف عند انقطاع الكهرباء."""
    abs_dst = os.path.abspath(file_path)
    parent = os.path.dirname(abs_dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = abs_dst + tmp_suffix
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, abs_dst)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def atomic_write_text(
    file_path: str,
    text: str,
    encoding: str = "utf-8",
    newline: Optional[str] = None,
    tmp_suffix: str = ".tmp",
) -> None:
    """كتابة نص ذرياً مع flush و fsync قبل الاستبدال."""
    abs_dst = os.path.abspath(file_path)
    parent = os.path.dirname(abs_dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = abs_dst + tmp_suffix
    try:
        with open(tmp_path, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, abs_dst)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def atomic_copy_file(src_path: str, dst_path: str, tmp_suffix: str = ".tmp") -> None:
    """نسخ ملف ذرياً مع flush و fsync على الملف المؤقت قبل الاستبدال."""
    abs_src = os.path.abspath(src_path)
    abs_dst = os.path.abspath(dst_path)
    parent = os.path.dirname(abs_dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = abs_dst + tmp_suffix
    try:
        with open(abs_src, "rb") as src_f, open(tmp_path, "wb") as dst_f:
            while True:
                chunk = src_f.read(65536)
                if not chunk:
                    break
                dst_f.write(chunk)
            dst_f.flush()
            os.fsync(dst_f.fileno())
        try:
            shutil.copystat(abs_src, tmp_path)
        except OSError:
            pass
        os.replace(tmp_path, abs_dst)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def compute_sha256(file_path: str) -> str:
    """حساب بصمة SHA-256 للملف."""
    digest = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def calculate_arabic_ratio(text: str) -> float:
    """حساب نسبة الحروف العربية مقارنة بإجمالي الأحرف غير الفراغية."""
    if not text:
        return 0.0
    non_ws = re.sub(r"\s+", "", text)
    if not non_ws:
        return 0.0
    arabic_count = len(ARABIC_CHARS_RE.findall(non_ws))
    return arabic_count / len(non_ws)


def is_file_likely_arabic(file_path: str, threshold: float = 0.05) -> bool:
    """التحقق مما إذا كان الملف النصي (أو .locres / .bak) يحتوي نسبة حروف عربية تتجاوز العتبة."""
    check_name = file_path[:-4] if file_path.lower().endswith(".bak") else file_path
    ext = os.path.splitext(check_name)[1].lower()
    if ext == ".locres":
        try:
            from formats.locres_handler import parse_locres_bytes

            with open(file_path, "rb") as f:
                locres = parse_locres_bytes(f.read())
            samples = [
                entry.text
                for ns in locres.namespaces
                for entry in ns.entries
                if entry.text
            ][:120]
            return calculate_arabic_ratio("\n".join(samples)) > threshold
        except Exception:
            return False

    if ext not in TEXT_EXTENSIONS:
        return False
    try:
        from formats.base import detect_bytes_encoding

        with open(file_path, "rb") as f:
            raw = f.read(200000)
        enc = detect_bytes_encoding(raw)
        sample = raw.decode(enc, errors="replace")
        return calculate_arabic_ratio(sample) > threshold
    except OSError:
        return False


class BackupManager:
    """مدير النسخ الاحتياطي والاسترجاع لمجلد اللعبة."""

    STATE_DIR_NAME = ".arabic_translator"
    MANIFEST_NAME = "backup_manifest.json"

    @classmethod
    def get_state_dir(cls, game_dir: str) -> str:
        return os.path.join(os.path.abspath(game_dir), cls.STATE_DIR_NAME)

    @classmethod
    def get_manifest_path(cls, game_dir: str) -> str:
        return os.path.join(cls.get_state_dir(game_dir), cls.MANIFEST_NAME)

    @classmethod
    def load_manifest(cls, game_dir: str) -> Dict:
        manifest_path = cls.get_manifest_path(game_dir)
        prev_path = manifest_path + ".prev"
        for candidate in (manifest_path, prev_path):
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("files", {})
                    data.setdefault("created_files", [])
                    return data
            except Exception:
                continue
        return {
            "game_dir": os.path.abspath(game_dir),
            "files": {},
            "created_files": [],
        }

    @classmethod
    def save_manifest(cls, game_dir: str, manifest: Dict) -> None:
        state_dir = cls.get_state_dir(game_dir)
        os.makedirs(state_dir, exist_ok=True)
        manifest_path = cls.get_manifest_path(game_dir)
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    atomic_copy_file(manifest_path, manifest_path + ".prev", tmp_suffix=".prev.tmp")
            except Exception:
                pass
        atomic_write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
            tmp_suffix=".tmp",
        )

    @classmethod
    def backup_file(cls, game_dir: str, target_path: str) -> Tuple[Optional[str], List[str]]:
        """إنشاء نسخة احتياطية آمنة مع منع الكتابة فوق ملف .bak أصلي بنسخة معرّبة."""
        warnings: List[str] = []
        abs_game = os.path.abspath(game_dir)
        abs_target = os.path.abspath(target_path)
        if not os.path.exists(abs_target):
            raise FileNotFoundError(f"الملف غير موجود لأخذ نسخة احتياطية: {abs_target}")

        rel_path = os.path.relpath(abs_target, abs_game)
        bak_path = abs_target + ".bak"
        internal_backup_path = os.path.join(cls.get_state_dir(abs_game), "backups", rel_path)
        os.makedirs(os.path.dirname(internal_backup_path), exist_ok=True)

        manifest = cls.load_manifest(abs_game)
        files_map = manifest.setdefault("files", {})
        target_is_arabic = is_file_likely_arabic(abs_target)

        if target_is_arabic:
            warnings.append(
                f"تحذير: الملف '{rel_path}' يحتوي حروفاً عربية (>5%) وقد يكون معرّباً مسبقاً."
            )

        chosen_backup: Optional[str] = None
        if os.path.exists(bak_path):
            # لا نكتب أبداً فوق ملف .bak موجود مسبقاً لحماية النسخة الإنجليزية الأصلية
            chosen_backup = bak_path
            if not os.path.exists(internal_backup_path) and not is_file_likely_arabic(bak_path):
                atomic_copy_file(bak_path, internal_backup_path)
        elif target_is_arabic:
            # نحفظ الحالة الحالية في أرشيف داخلي دون تلويث مسمى .bak
            pre_arabic_path = internal_backup_path + ".pre_arabic"
            if not os.path.exists(pre_arabic_path):
                atomic_copy_file(abs_target, pre_arabic_path)
            chosen_backup = pre_arabic_path
            warnings.append(
                f"تم حفظ لقطة '{rel_path}' كنسخة مؤقتة دون إنشاء .bak لاحتوائها على عربية مسبقاً."
            )
        else:
            atomic_copy_file(abs_target, bak_path)
            atomic_copy_file(abs_target, internal_backup_path)
            chosen_backup = bak_path

        if rel_path not in files_map:
            source_for_hash = chosen_backup if os.path.exists(chosen_backup) else abs_target
            files_map[rel_path] = {
                "original_path": abs_target,
                "backup_path": chosen_backup,
                "internal_backup_path": internal_backup_path if os.path.exists(internal_backup_path) else chosen_backup,
                "sha256": compute_sha256(source_for_hash),
                "was_arabic_at_backup": target_is_arabic,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            cls.save_manifest(abs_game, manifest)

        return chosen_backup, warnings

    @classmethod
    def register_created_file(cls, game_dir: str, created_path: str) -> None:
        """تسجيل ملف مضاف حديثاً (لم يكن موجوداً أصلاً) ليُحذف عند الاسترجاع الكامل."""
        abs_game = os.path.abspath(game_dir)
        abs_created = os.path.abspath(created_path)
        rel_path = os.path.relpath(abs_created, abs_game)

        manifest = cls.load_manifest(abs_game)
        files_map = manifest.setdefault("files", {})
        if rel_path in files_map:
            return

        created_list = manifest.setdefault("created_files", [])
        if not any(p.lower() == rel_path.lower() for p in created_list):
            created_list.append(rel_path)
            cls.save_manifest(abs_game, manifest)

    @classmethod
    def get_backup_summary(cls, game_dir: str) -> Dict[str, object]:
        """فحص وجود نسخة احتياطية فعّالة محفوظة لمجلد اللعبة حتى بعد إعادة تشغيل البرنامج."""
        abs_game = os.path.abspath(game_dir)
        manifest_path = cls.get_manifest_path(abs_game)
        if not os.path.isfile(manifest_path):
            return {
                "has_backup": False,
                "replaced_count": 0,
                "created_count": 0,
                "files": [],
                "created_files": [],
            }
        try:
            manifest = cls.load_manifest(abs_game)
        except Exception:
            return {
                "has_backup": False,
                "replaced_count": 0,
                "created_count": 0,
                "files": [],
                "created_files": [],
            }

        active_files: List[str] = []
        for rel_path, meta in manifest.get("files", {}).items():
            candidates = [meta.get("backup_path"), meta.get("internal_backup_path")]
            if any(p and os.path.exists(p) for p in candidates):
                active_files.append(rel_path)

        active_created: List[str] = []
        for rel_path in manifest.get("created_files", []):
            target = os.path.join(abs_game, rel_path)
            if os.path.exists(target):
                active_created.append(rel_path)

        return {
            "has_backup": bool(active_files or active_created),
            "replaced_count": len(active_files),
            "created_count": len(active_created),
            "files": active_files,
            "created_files": active_created,
        }

    @classmethod
    def rollback(cls, game_dir: str) -> Tuple[List[str], List[str]]:
        """استرجاع جميع الملفات المعدّلة وحذف الملفات المضافة المسجلة في backup_manifest.json."""
        abs_game = os.path.abspath(game_dir)
        manifest = cls.load_manifest(abs_game)
        files_map = manifest.get("files", {})
        created_list = list(manifest.get("created_files", []))
        restored: List[str] = []
        warnings: List[str] = []

        if not files_map and not created_list:
            warnings.append("لا يوجد سجل نسخ احتياطي (backup_manifest.json) داخل مجلد اللعبة.")
            return restored, warnings

        remaining_files: Dict = {}
        for rel_path, meta in files_map.items():
            orig_path = meta.get("original_path") or os.path.join(abs_game, rel_path)
            candidates = [meta.get("backup_path"), meta.get("internal_backup_path")]
            src = next((p for p in candidates if p and os.path.exists(p)), None)
            if not src:
                warnings.append(f"تعذر العثور على النسخة الاحتياطية للملف: {rel_path}")
                remaining_files[rel_path] = meta
                continue
            os.makedirs(os.path.dirname(orig_path), exist_ok=True)
            atomic_copy_file(src, orig_path, tmp_suffix=RESTORE_TMP_SUFFIX)
            restored.append(rel_path)

        remaining_created: List[str] = []
        for rel_path in created_list:
            target = os.path.join(abs_game, rel_path)
            if os.path.exists(target):
                try:
                    os.remove(target)
                    restored.append(f"حذف (مضاف): {rel_path}")
                except OSError as exc:
                    warnings.append(f"تعذر حذف الملف المضاف '{rel_path}': {exc}")
                    remaining_created.append(rel_path)

        for log_name in ("arabic_font_log.txt", "arabic_translator_log.txt", "arabic_captured_texts.txt"):
            log_path = os.path.join(abs_game, log_name)
            if os.path.isfile(log_path):
                try:
                    os.remove(log_path)
                except OSError:
                    pass

        for root, dirs, files in os.walk(abs_game):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for fname in files:
                if fname.endswith((COMMIT_TMP_SUFFIX, RESTORE_TMP_SUFFIX)):
                    try:
                        os.remove(os.path.join(root, fname))
                    except OSError:
                        pass

        manifest["files"] = remaining_files
        manifest["created_files"] = remaining_created
        cls.save_manifest(abs_game, manifest)
        TransactionJournal.clear_session(abs_game, clear_cache=False)

        return restored, warnings


class TransactionJournal:
    """سجل الجلسات والالتزام المرحلي لحماية اللعبة من انقطاع الكهرباء وحفظ التقدم."""

    SESSION_FILE_NAME = "session_state.json"
    STAGING_DIR_NAME = "staging"

    @classmethod
    def get_session_path(cls, game_dir: str) -> str:
        return os.path.join(BackupManager.get_state_dir(game_dir), cls.SESSION_FILE_NAME)

    @classmethod
    def get_staging_dir(cls, game_dir: str) -> str:
        return os.path.join(BackupManager.get_state_dir(game_dir), cls.STAGING_DIR_NAME)

    @classmethod
    def load_session(cls, game_dir: str) -> Optional[Dict]:
        session_path = cls.get_session_path(game_dir)
        for candidate in (session_path, session_path + ".prev"):
            if not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        return None

    @classmethod
    def save_session(cls, game_dir: str, state: Dict) -> None:
        state_copy = dict(state)
        state_copy["updated_at"] = datetime.now(timezone.utc).isoformat()
        session_path = cls.get_session_path(game_dir)
        if os.path.isfile(session_path):
            try:
                with open(session_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    atomic_copy_file(session_path, session_path + ".prev", tmp_suffix=".prev.tmp")
            except Exception:
                pass
        atomic_write_text(
            session_path,
            json.dumps(state_copy, ensure_ascii=False, indent=2),
            encoding="utf-8",
            tmp_suffix=".tmp",
        )

    @classmethod
    def clear_session(cls, game_dir: str, clear_cache: bool = False) -> None:
        session_path = cls.get_session_path(game_dir)
        for path in (session_path, session_path + ".prev", session_path + ".tmp"):
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        staging_dir = cls.get_staging_dir(game_dir)
        if os.path.isdir(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)
        if clear_cache:
            cache_path = os.path.join(
                BackupManager.get_state_dir(game_dir), "translation_cache.json"
            )
            for cpath in (
                cache_path,
                cache_path + ".prev",
                cache_path + ".tmp",
                cache_path + ".wal.jsonl",
            ):
                if os.path.isfile(cpath):
                    try:
                        os.remove(cpath)
                    except OSError:
                        pass

    @classmethod
    def get_interrupted_session_summary(cls, game_dir: str) -> Dict[str, object]:
        abs_game = os.path.abspath(game_dir)
        session = cls.load_session(abs_game)
        cache_path = os.path.join(
            BackupManager.get_state_dir(abs_game), "translation_cache.json"
        )
        cached_count = 0
        if any(
            os.path.isfile(p)
            for p in (cache_path, cache_path + ".prev", cache_path + ".wal.jsonl")
        ):
            try:
                from translators.google_batch import GoogleBatchTranslator

                cached_count = len(GoogleBatchTranslator._load_cache(cache_path))
            except Exception:
                cached_count = 0

        if not session:
            return {
                "has_interrupted": False,
                "status": "",
                "game_untouched": True,
                "needs_recovery": False,
                "total_unique_lines": 0,
                "cached_lines": cached_count,
                "completed_lines": 0,
                "percent": 0,
                "targets": [],
                "updated_at": "",
            }

        status = str(session.get("status") or "")
        has_interrupted = status in ("translating", "staging", "committing", "paused")
        needs_recovery = status == "committing"
        game_untouched = status in ("translating", "staging", "paused")
        total_unique = int(session.get("total_unique_lines") or 0)
        recorded_completed = int(session.get("completed_lines") or 0)
        if total_unique > 0:
            completed = max(recorded_completed, min(total_unique, cached_count))
            completed = min(completed, total_unique)
            percent = int(round((completed / total_unique) * 100))
        else:
            completed = max(recorded_completed, cached_count)
            percent = 100 if completed > 0 else 0

        return {
            "has_interrupted": has_interrupted,
            "status": status,
            "game_untouched": game_untouched,
            "needs_recovery": needs_recovery,
            "total_unique_lines": total_unique,
            "cached_lines": cached_count,
            "completed_lines": completed,
            "percent": percent,
            "targets": list(session.get("targets") or []),
            "updated_at": str(session.get("updated_at") or ""),
        }

    @classmethod
    def recover_if_interrupted_commit(cls, game_dir: str) -> Tuple[bool, List[str], List[str]]:
        abs_game = os.path.abspath(game_dir)
        session = cls.load_session(abs_game)

        # تنظيف أي ملفات مؤقتة عالقة خارج .arabic_translator حتى لو لم يكن هناك سجل نشط
        for root, dirs, files in os.walk(abs_game):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for fname in files:
                if fname.endswith((COMMIT_TMP_SUFFIX, RESTORE_TMP_SUFFIX)):
                    try:
                        os.remove(os.path.join(root, fname))
                    except OSError:
                        pass

        if not session:
            return False, [], []

        status = str(session.get("status") or "")
        if status == "committing":
            restored, warnings = BackupManager.rollback(abs_game)
            cls.clear_session(abs_game, clear_cache=False)
            return True, restored, warnings

        if status == "staging":
            staging_dir = cls.get_staging_dir(abs_game)
            if os.path.isdir(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
            session["status"] = "paused"
            cls.save_session(abs_game, session)
            return False, [], []

        return False, [], []
