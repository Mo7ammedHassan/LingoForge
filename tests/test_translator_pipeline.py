# -*- coding: utf-8 -*-
"""اختبارات شاملة لنواة التعريب، حماية المتغيرات، التشبيك، الاكتشاف، والنسخ الاحتياطي."""

import base64
import codecs
import gzip
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import patch
import urllib.error
import xml.etree.ElementTree as ET
import zlib

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from arabic.protector import mask_line, unmask_line, verify_placeholders
from arabic.shaper import process_text
from core.backup import BackupManager, TransactionJournal, compute_sha256
from core.models import CandidateFile, EngineInfo
from core.pipeline import AutoTranslatorPipeline
from engines.unity.adapter import UnityAdapter
from engines.unity.assets_extractor import (
    _extract_tmp_text_from_monobehaviour,
    _extract_ugui_text_from_monobehaviour,
    _is_assembly_qualified_type_name,
    _pack_aligned_string,
    build_unity_i2_languages_blob,
    build_unity_localization_string_table_blob,
    build_unity_monoscript_blob,
    build_unity_serialized_file_blob,
    build_unity_text_asset_blob,
    build_unity_tmp_text_blob,
    build_unity_ugui_text_blob,
    build_unityfs_bundle_blob,
    clear_assets_extraction_cache,
    decode_compressed_or_encoded_payload,
    extract_assets_localization_blocks,
    extract_scene_ui_texts_from_bytes,
    extract_unity_localization_string_tables_from_bytes,
    extract_unity_version_from_file,
    extract_unityfs_nodes_from_bytes,
    parse_serialized_file_index_from_file,
)
from engines.unity.detector import UnityDetector
from engines.unity.il2cpp_metadata import (
    build_il2cpp_global_metadata_blob,
    build_il2cpp_modern_metadata_blob,
    extract_il2cpp_metadata_strings,
)
from engines.unity.injector import (
    UnityInjector,
    find_coreclr_runtime,
    find_csc_compiler,
    resolve_arabic_font_path,
)
from engines.unity.scanner import (
    UnityLanguageScanner,
    _is_clean_code_ui_string,
    detect_candidate_format,
    extract_code_ui_strings_from_file,
    is_sibling_lang_asset,
    is_translatable_value,
    parse_addressables_catalog,
    score_candidate_file,
)
from formats.csv_handler import CsvFormatHandler
from formats.ini_handler import IniFormatHandler
from formats.json_handler import (
    apply_json_translations,
    clean_jsonc,
    flatten_json_strings,
    write_json_atomic_and_verify,
)
from formats.po_handler import PoFormatHandler
from formats.registry import get_format_handler
from formats.xml_handler import XmlFormatHandler
from translators.google_batch import GoogleBatchTranslator, TranslationInterruptedError
from ui.gui import TranslatorGUI


class ProtectorTests(unittest.TestCase):
    def test_mask_and_self_heal_corrupted_tokens(self):
        orig = "  Press {0} to charge <color=#00FF00>{1:P0}</color>  "
        masked = mask_line(orig)
        self.assertEqual(
            masked.placeholders,
            ["{0}", "<color=#00FF00>", "{1:P0}", "</color>"],
        )

        # محاكاة تشويه المترجم للأقواس وتحويل الأرقام لعربية وفقدان الوسم الأخير
        corrupted = "اضغط [ [ ٠ ] ] لشحن 【١】 نسبة [ [ [ 2 ] ] ]"
        unmasked, repairs = unmask_line(
            corrupted, masked.placeholders, masked.lead_ws, masked.trail_ws
        )
        self.assertGreaterEqual(repairs, 3)
        self.assertEqual(verify_placeholders(masked.placeholders, unmasked), [])
        self.assertIn("{0}", unmasked)
        self.assertIn("<color=#00FF00>", unmasked)
        self.assertIn("{1:P0}", unmasked)
        self.assertIn("</color>", unmasked)
        self.assertTrue(unmasked.startswith("  ") and unmasked.endswith("  "))


class ShaperTests(unittest.TestCase):
    def test_shaping_lam_alef_paired_tags_and_ltr(self):
        sample = "<color=#FFFF00><b>لا تضغط Seatruck ({0})</b></color>"
        shaped = process_text(sample)
        self.assertTrue(shaped.startswith("<color=#FFFF00><b>"))
        self.assertTrue(shaped.endswith("</b></color>"))
        # بقاء الكلمة الإنجليزية والمتغير غير معكوسين
        self.assertIn("Seatruck", shaped)
        self.assertIn("{0}", shaped)
        # ظهور وصلة لا (U+FEFB) بعد التشبيك
        self.assertIn("\uFEFB", shaped)
        # عدم وجود حروف عربية غير مشكلة متبقية
        self.assertNotRegex(shaped, r"[\u0621-\u064A]")


class JsonHandlerTests(unittest.TestCase):
    def test_fault_tolerant_jsonc_and_nested_preserve_order(self):
        raw = """\ufeff// تعليق بداية الملف
        {
          /* تعليق متعدد الأسطر */
          "url": "https://example.com/a//b",
          "ui": {
            "play": "Play",
            "quit": "Quit", // تعليق جانبي
          },
          "list": ["New Game", "Continue",],
        }"""
        parsed = json.loads(clean_jsonc(raw))
        self.assertEqual(parsed["url"], "https://example.com/a//b")
        flat = flatten_json_strings(parsed)
        keys = [k for _, k, _ in flat]
        self.assertEqual(keys, ["url", "ui.play", "ui.quit", "list.0", "list.1"])

        repl = {flat[1][0]: "العب"}
        updated = apply_json_translations(parsed, repl)
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "out.json")
            write_json_atomic_and_verify(out_path, updated)
            with open(out_path, "r", encoding="utf-8") as f:
                reloaded = json.load(f)
            self.assertEqual(list(reloaded.keys()), ["url", "ui", "list"])
            self.assertEqual(reloaded["ui"]["play"], "العب")


class GoogleBatchTranslatorTests(unittest.TestCase):
    def test_dedup_endpoint_rotation_and_binary_split(self):
        calls = []

        def fake_caller(batch_text, ep, sl, tl):
            calls.append((ep, batch_text.splitlines()))
            if len(calls) == 1:
                raise urllib.error.HTTPError("http://x", 429, "Too Many", {}, None)
            lines = batch_text.splitlines()
            if len(lines) > 2:
                # محاكاة دمج الأسطر لإجبار الانقسام الثنائي التلقائي
                return " | ".join(f"ترجمة:{ln}" for ln in lines)
            return "\n".join(f"ترجمة:{ln}" for ln in lines)

        translator = GoogleBatchTranslator(
            max_chars=5000,
            max_lines=50,
            inter_batch_delay=0.0,
            endpoint_caller=fake_caller,
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache_file = os.path.join(tmp, "cache.json")
            src = ["Line A", "Line B", "Line A", "Line C", "Line D"]
            res, batches_sent, cached = translator.translate_lines(src, cache_path=cache_file)
            self.assertEqual(cached, 0)
            self.assertEqual(batches_sent, 1)
            self.assertEqual(len(res), 4)
            self.assertEqual(res["Line C"], "ترجمة:Line C")
            # التحقق من تبديل السيرفر بعد خطأ 429
            self.assertEqual(calls[0][0], "gtx")
            self.assertEqual(calls[1][0], "clients5")
            # التحقق من استخدام الكاش في التشغيل الثاني
            res2, batches_sent2, cached2 = translator.translate_lines(src, cache_path=cache_file)
            self.assertEqual(batches_sent2, 0)
            self.assertEqual(cached2, 4)
            self.assertEqual(res2, res)


class ScannerBackupAndPipelineTests(unittest.TestCase):
    def test_translatability_filter_and_scoring(self):
        self.assertFalse(is_translatable_value("k1", "player_walk_01"))
        self.assertFalse(is_translatable_value("k2", "Assets/Prefabs/UI.prefab"))
        self.assertFalse(is_translatable_value("k3", "mainMenuPanel"))
        self.assertFalse(is_translatable_value("k4", "{0}"))
        self.assertFalse(is_translatable_value("k5", "لعبة جديدة"))
        self.assertTrue(is_translatable_value("k6", "Play"))
        self.assertTrue(is_translatable_value("k7", "Press {0} to dive"))

        with tempfile.TemporaryDirectory() as tmp:
            lang_dir = os.path.join(tmp, "Game_Data", "StreamingAssets", "LanguageFiles")
            os.makedirs(lang_dir)
            en_path = os.path.join(lang_dir, "English.json")
            cat_path = os.path.join(tmp, "Game_Data", "StreamingAssets", "catalog.json")

            with open(en_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "Menu_Play": "New Game",
                        "Menu_Cont": "Continue",
                        "Menu_Opt": "Options",
                        "Menu_Audio": "Audio Settings",
                        "Menu_Save": "Save Game",
                        "Menu_Quit": "Quit",
                        "Hint_1": "Press {0} to interact with <color=yellow>item</color>.",
                    },
                    f,
                )
            with open(os.path.join(lang_dir, "French.json"), "w", encoding="utf-8") as f:
                json.dump({"Menu_Play": "Nouvelle partie"}, f)
            with open(os.path.join(lang_dir, "German.json"), "w", encoding="utf-8") as f:
                json.dump({"Menu_Play": "Neues Spiel"}, f)
            with open(cat_path, "w", encoding="utf-8") as f:
                json.dump({"m_LocatorId": "AddressablesMainContentCatalog", "m_InternalIds": ["a", "b", "c"]}, f)

            hints = ["LanguageFiles/{0}.json", "English"]
            siblings = {"english", "french", "german"}
            cand_en = score_candidate_file(en_path, tmp, hints, siblings)
            cand_cat = score_candidate_file(cat_path, tmp, hints, set())
            self.assertIsNotNone(cand_en)
            self.assertGreaterEqual(cand_en.score, 85)
            self.assertTrue(cand_cat is None or cand_cat.score < 20)

    def test_backup_protects_original_bak_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            lang = os.path.join(tmp, "English.json")
            bak = lang + ".bak"
            with open(bak, "w", encoding="utf-8") as f:
                f.write('{"k": "Original English"}')
            with open(lang, "w", encoding="utf-8") as f:
                f.write('{"k": "نص عربي قديم محفوظ"}')

            chosen, warnings = BackupManager.backup_file(tmp, lang)
            self.assertEqual(chosen, bak)
            self.assertTrue(any("عربية" in w for w in warnings))
            with open(bak, "r", encoding="utf-8") as f:
                self.assertIn("Original English", f.read())

            restored, _ = BackupManager.rollback(tmp)
            self.assertEqual(restored, ["English.json"])
            with open(lang, "r", encoding="utf-8") as f:
                self.assertIn("Original English", f.read())

    def test_end_to_end_pipeline_on_mock_unity_game_and_unsupported_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1. مجلد غير مدعوم يتوقف بأمان بدون تعديل
            unreal_dir = os.path.join(tmp, "UnrealGame")
            os.makedirs(os.path.join(unreal_dir, "Engine", "Binaries"))
            pipeline = AutoTranslatorPipeline()
            rep_bad = pipeline.translate(unreal_dir)
            self.assertFalse(rep_bad.success)
            self.assertEqual(rep_bad.engine.engine_name, "Unreal Engine")

            # 2. لعبة Unity Mono تجريبية كاملة
            unity_dir = os.path.join(tmp, "UnityGame")
            managed = os.path.join(unity_dir, "MyGame_Data", "Managed")
            lang_dir = os.path.join(unity_dir, "MyGame_Data", "StreamingAssets", "Localization")
            os.makedirs(managed)
            os.makedirs(lang_dir)
            with open(os.path.join(unity_dir, "MyGame.exe"), "wb") as f:
                f.write(b"MZ" + b"\x00" * 62)
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")
            with open(os.path.join(managed, "Unity.TextMeshPro.dll"), "wb") as f:
                f.write(b"MZ")

            en_file = os.path.join(lang_dir, "English.json")
            with open(en_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "Play": "New Game",
                        "Continue": "Continue",
                        "Options": "Options",
                        "Audio": "Audio Settings",
                        "Save": "Save Game",
                        "Quit": "Quit",
                        "Hint": "Press {0} to start",
                        "TechId": "ui_main_panel",
                    },
                    f,
                )

            def fake_ep(batch_text, ep, sl, tl):
                mapping = {
                    "New Game": "لعبة جديدة",
                    "Continue": "متابعة",
                    "Options": "خيارات",
                    "Audio Settings": "إعدادات الصوت",
                    "Save Game": "حفظ اللعبة",
                    "Quit": "خروج",
                    "Press [[[0]]] to start": "اضغط [[[0]]] للبدء",
                }
                return "\n".join(mapping.get(ln, ln) for ln in batch_text.splitlines())

            fake_translator = GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            pipe = AutoTranslatorPipeline(translator=fake_translator)
            rep = pipe.translate(unity_dir, skip_inject=True)
            self.assertTrue(rep.success)
            self.assertTrue(os.path.isfile(en_file + ".bak"))
            with open(en_file, "r", encoding="utf-8") as f:
                out_json = json.load(f)
            # المعرف التقني بقي بدون ترجمة
            self.assertEqual(out_json["TechId"], "ui_main_panel")
            # النص المترجم تم تشكيله مع حفظ المتغير {0}
            self.assertIn("{0}", out_json["Hint"])
            self.assertRegex(out_json["Play"], r"[\uFE70-\uFEFF]")


def _make_minimal_x64_pe() -> bytes:
    """بناء ترويسة PE صالحة لمعمارية x64 للاختبارات."""
    buf = bytearray(256)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x80)
    buf[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", buf, 0x84, 0x8664)
    return bytes(buf)


class MultiFormatHandlersTests(unittest.TestCase):
    def test_csv_i2_multilang_and_arabic_column(self):
        csv_content = (
            "Key,Type,Desc,English,French,German,Arabic\r\n"
            "MENU_PLAY,Text,Main button,New Game,Nouvelle partie,Neues Spiel,\r\n"
            "MENU_QUIT,Text,Exit button,Quit,Quitter,Beenden,\r\n"
        )
        handler = CsvFormatHandler()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Localization.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write(csv_content)

            doc = handler.load(path)
            self.assertEqual(len(doc.entries), 2)
            self.assertEqual([v for _, _, v in doc.entries], ["New Game", "Quit"])

            replacements = {
                doc.entries[0][0]: "لعبة جديدة",
                doc.entries[1][0]: "خروج",
            }
            handler.save(path, doc, replacements)

            with open(path, "r", encoding="utf-8", newline="") as f:
                saved = f.read()
            self.assertIn("Nouvelle partie", saved)
            self.assertIn("Neues Spiel", saved)
            self.assertIn("MENU_PLAY,Text,Main button,لعبة جديدة,Nouvelle partie,Neues Spiel,لعبة جديدة", saved)
            self.assertIn("MENU_QUIT,Text,Exit button,خروج,Quitter,Beenden,خروج", saved)

    def test_xml_handler_preserves_ids_and_declaration(self):
        xml_content = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<resources>\n"
            '  <string name="menu_play">New Game</string>\n'
            '  <item id="btn_quit" value="Quit Game" />\n'
            "</resources>\n"
        )
        handler = XmlFormatHandler()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "English.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml_content)

            doc = handler.load(path)
            values = [v for _, _, v in doc.entries]
            self.assertEqual(values, ["New Game", "Quit Game"])
            keys = [k for _, k, _ in doc.entries]
            self.assertEqual(keys, ["string[menu_play]", "item[btn_quit]@value"])

            repl = {
                doc.entries[0][0]: "لعبة جديدة",
                doc.entries[1][0]: "خروج من اللعبة",
            }
            handler.save(path, doc, repl)

            with open(path, "r", encoding="utf-8") as f:
                raw_out = f.read()
            self.assertTrue(raw_out.startswith("<?xml"))
            root = ET.fromstring(raw_out)
            s_node = root.find("string")
            i_node = root.find("item")
            self.assertEqual(s_node.attrib["name"], "menu_play")
            self.assertEqual(s_node.text, "لعبة جديدة")
            self.assertEqual(i_node.attrib["id"], "btn_quit")
            self.assertEqual(i_node.attrib["value"], "خروج من اللعبة")

    def test_ini_handler_preserves_comments_sections_quotes_and_newlines(self):
        ini_content = (
            "; Top comment\n"
            "GLOBAL_TITLE=Submerged World\n"
            "\n"
            "[Menu]\n"
            "# Button label\n"
            'PLAY_BTN = "New Game\\nStart Adventure"\n'
            "TECH_ID = ui_panel_01\n"
        )
        handler = IniFormatHandler()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "English.ini")
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(ini_content)

            doc = handler.load(path)
            self.assertEqual(len(doc.entries), 3)
            self.assertEqual(doc.entries[0][1:], ("GLOBAL_TITLE", "Submerged World"))
            self.assertEqual(doc.entries[1][1:], ("Menu.PLAY_BTN", "New Game\nStart Adventure"))

            repl = {
                doc.entries[0][0]: "عالم مغمور",
                doc.entries[1][0]: "لعبة جديدة\nابدأ المغامرة",
            }
            handler.save(path, doc, repl)

            with open(path, "r", encoding="utf-8", newline="") as f:
                out_lines = f.read().splitlines()
            self.assertEqual(out_lines[0], "; Top comment")
            self.assertEqual(out_lines[1], "GLOBAL_TITLE=عالم مغمور")
            self.assertEqual(out_lines[3], "[Menu]")
            self.assertEqual(out_lines[4], "# Button label")
            self.assertEqual(out_lines[5], 'PLAY_BTN = "لعبة جديدة\\nابدأ المغامرة"')
            self.assertEqual(out_lines[6], "TECH_ID = ui_panel_01")

    def test_po_handler_standard_symbolic_and_multiline(self):
        po_content = (
            'msgid ""\n'
            'msgstr ""\n'
            '"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
            "#: menu.cs:12\n"
            'msgctxt "MainMenu"\n'
            'msgid "New Game"\n'
            'msgstr ""\n\n'
            'msgid "MENU_QUIT"\n'
            'msgstr "Quit"\n\n'
            'msgid ""\n'
            '"Line one\\n"\n'
            '"Line two"\n'
            'msgstr ""\n'
        )
        handler = PoFormatHandler()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "English.po")
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(po_content)

            doc = handler.load(path)
            self.assertEqual(len(doc.entries), 3)
            self.assertEqual(doc.entries[0][1:], ("MainMenu.New Game", "New Game"))
            self.assertEqual(doc.entries[1][1:], ("MENU_QUIT", "Quit"))
            self.assertEqual(doc.entries[2][1:], ("Line one\nLine two", "Line one\nLine two"))

            repl = {
                doc.entries[0][0]: "لعبة جديدة",
                doc.entries[1][0]: "خروج",
                doc.entries[2][0]: "السطر الأول\nالسطر الثاني",
            }
            handler.save(path, doc, repl)

            with open(path, "r", encoding="utf-8", newline="") as f:
                out_po = f.read()
            self.assertIn('"Content-Type: text/plain; charset=UTF-8\\n"', out_po)
            self.assertIn('#: menu.cs:12', out_po)
            self.assertIn('msgid "New Game"\nmsgstr "لعبة جديدة"', out_po)
            self.assertIn('msgid "MENU_QUIT"\nmsgstr "خروج"', out_po)
            self.assertIn('"السطر الأول\\n"\n"السطر الثاني"', out_po)


class DoorstopAndMultiFormatPipelineTests(unittest.TestCase):
    def test_pipeline_csv_with_doorstop_deploy_and_clean_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            unity_dir = os.path.join(tmp, "UnityCsvGame")
            managed = os.path.join(unity_dir, "MyGame_Data", "Managed")
            lang_dir = os.path.join(unity_dir, "MyGame_Data", "StreamingAssets", "Localization")
            os.makedirs(managed)
            os.makedirs(lang_dir)

            with open(os.path.join(unity_dir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            for dll in ("Assembly-CSharp.dll", "Unity.TextMeshPro.dll"):
                with open(os.path.join(managed, dll), "wb") as f:
                    f.write(b"MZ")

            csv_path = os.path.join(lang_dir, "Localization.csv")
            orig_csv = (
                "Key,Type,English,French\r\n"
                "Menu_Play,Text,New Game,Nouvelle partie\r\n"
                "Menu_Cont,Text,Continue,Continuer\r\n"
                "Menu_Opt,Text,Options,Options\r\n"
                "Menu_Audio,Text,Audio Settings,Audio\r\n"
                "Menu_Save,Text,Save Game,Sauvegarder\r\n"
                "Menu_Quit,Text,Quit,Quitter\r\n"
                "Hint_Start,Text,Press {0} to start,Appuyez sur {0}\r\n"
            )
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                f.write(orig_csv)

            def fake_ep(batch_text, ep, sl, tl):
                mapping = {
                    "New Game": "لعبة جديدة",
                    "Continue": "متابعة",
                    "Options": "خيارات",
                    "Audio Settings": "إعدادات الصوت",
                    "Save Game": "حفظ اللعبة",
                    "Quit": "خروج",
                    "Press [[[0]]] to start": "اضغط [[[0]]] للبدء",
                }
                return "\n".join(mapping.get(ln, ln) for ln in batch_text.splitlines())

            def fake_compile(self_inj, engine, output_dll_path, include_input_utils=False):
                os.makedirs(os.path.dirname(output_dll_path), exist_ok=True)
                with open(output_dll_path, "wb") as df:
                    df.write(b"MZ_FAKE_INJECTOR")
                return True, "compiled ok"

            fake_translator = GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            pipe = AutoTranslatorPipeline(translator=fake_translator)

            with patch.object(type(pipe.adapter.injector), "compile_injector", fake_compile):
                rep = pipe.translate(unity_dir, skip_inject=False)

            self.assertTrue(rep.success)
            self.assertIsNotNone(rep.injection)
            self.assertEqual(rep.injection.method, "doorstop")

            winhttp_dst = os.path.join(unity_dir, "winhttp.dll")
            cfg_dst = os.path.join(unity_dir, "doorstop_config.ini")
            inj_dll_dst = os.path.join(managed, "ArabicRuntimeInjector.dll")
            self.assertTrue(os.path.isfile(winhttp_dst))
            self.assertTrue(os.path.isfile(cfg_dst))
            self.assertTrue(os.path.isfile(inj_dll_dst))

            with open(winhttp_dst, "rb") as f:
                pe_data = f.read(512)
            pe_off = struct.unpack_from("<I", pe_data, 0x3C)[0]
            self.assertEqual(struct.unpack_from("<H", pe_data, pe_off + 4)[0], 0x8664)

            with open(cfg_dst, "r", encoding="utf-8") as f:
                cfg_txt = f.read()
            self.assertIn("[General]", cfg_txt)
            self.assertIn("target_assembly=", cfg_txt)
            self.assertIn("[UnityDoorstop]", cfg_txt)
            self.assertIn("targetAssembly=", cfg_txt)

            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                tr_csv = f.read()
            self.assertIn("Nouvelle partie", tr_csv)
            self.assertIn("{0}", tr_csv)
            self.assertRegex(tr_csv, r"[\uFE70-\uFEFF]")

            # التحقق من أن Rollback يعيد ملف اللغة الأصلي ويحذف ملفات Doorstop المضافة
            restored, warnings = AutoTranslatorPipeline.rollback(unity_dir)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(winhttp_dst))
            self.assertFalse(os.path.exists(cfg_dst))
            self.assertFalse(os.path.exists(inj_dll_dst))
            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                self.assertEqual(f.read(), orig_csv)

    def test_preexisting_winhttp_is_restored_not_deleted_on_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            unity_dir = os.path.join(tmp, "ModdedUnity")
            managed = os.path.join(unity_dir, "Game_Data", "Managed")
            lang_dir = os.path.join(unity_dir, "Game_Data", "StreamingAssets", "Localization")
            os.makedirs(managed)
            os.makedirs(lang_dir)

            with open(os.path.join(unity_dir, "Game.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            for dll in ("Assembly-CSharp.dll", "Unity.TextMeshPro.dll"):
                with open(os.path.join(managed, dll), "wb") as f:
                    f.write(b"MZ")

            existing_winhttp = os.path.join(unity_dir, "winhttp.dll")
            with open(existing_winhttp, "wb") as f:
                f.write(b"ORIGINAL_MOD_WINHTTP")

            po_path = os.path.join(lang_dir, "English.po")
            with open(po_path, "w", encoding="utf-8") as f:
                f.write(
                    'msgid "New Game"\nmsgstr ""\n\n'
                    'msgid "Continue"\nmsgstr ""\n\n'
                    'msgid "Options"\nmsgstr ""\n\n'
                    'msgid "Audio Settings"\nmsgstr ""\n\n'
                    'msgid "Save Game"\nmsgstr ""\n\n'
                    'msgid "Press {0} to start"\nmsgstr ""\n\n'
                    'msgid "Quit"\nmsgstr ""\n'
                )

            def fake_ep(batch_text, ep, sl, tl):
                return "\n".join("نص:" + ln for ln in batch_text.splitlines())

            def fake_compile(self_inj, engine, output_dll_path, include_input_utils=False):
                with open(output_dll_path, "wb") as df:
                    df.write(b"MZ_FAKE")
                return True, "ok"

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            with patch.object(type(pipe.adapter.injector), "compile_injector", fake_compile):
                rep = pipe.translate(unity_dir, skip_inject=False)

            self.assertTrue(rep.success)
            self.assertTrue(os.path.isfile(existing_winhttp + ".bak"))

            AutoTranslatorPipeline.rollback(unity_dir)
            self.assertTrue(os.path.isfile(existing_winhttp))
            with open(existing_winhttp, "rb") as f:
                self.assertEqual(f.read(), b"ORIGINAL_MOD_WINHTTP")


class PackedAssetsAndGuiPhase3Tests(unittest.TestCase):
    def test_assets_textasset_extraction_override_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            unity_dir = os.path.join(tmp, "PackedUnityGame")
            data_dir = os.path.join(unity_dir, "MyGame_Data")
            managed = os.path.join(data_dir, "Managed")
            os.makedirs(managed)

            with open(os.path.join(unity_dir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            for dll in ("Assembly-CSharp.dll", "Unity.TextMeshPro.dll"):
                with open(os.path.join(managed, dll), "wb") as f:
                    f.write(b"MZ")

            text_payload = json.dumps(
                {
                    "Menu_Play": "New Game",
                    "Menu_Cont": "Continue",
                    "Menu_Opt": "Options",
                    "Menu_Save": "Save Game",
                    "Menu_Quit": "Quit",
                    "Hint_Dive": "Press {0} to dive",
                    "TechId": "ui_main_panel",
                }
            )
            french_payload = json.dumps(
                {
                    "Menu_Play": "Nouvelle partie",
                    "Menu_Cont": "Continuer",
                    "Menu_Quit": "Quitter",
                }
            )
            blob = (
                b"\xAA\xBB\xCC\xDD" * 8
                + build_unity_text_asset_blob("English", text_payload)
                + build_unity_text_asset_blob("French", french_payload)
                + b"\x00\x11\x22\x33" * 4
            )
            res_assets = os.path.join(data_dir, "resources.assets")
            with open(res_assets, "wb") as f:
                f.write(blob)
            orig_sha = compute_sha256(res_assets)

            def fake_ep(batch_text, ep, sl, tl):
                mapping = {
                    "New Game": "لعبة جديدة",
                    "Continue": "متابعة",
                    "Options": "خيارات",
                    "Save Game": "حفظ اللعبة",
                    "Quit": "خروج",
                    "Press [[[0]]] to dive": "اضغط [[[0]]] للغوص",
                }
                return "\n".join(mapping.get(ln, ln) for ln in batch_text.splitlines())

            def fake_compile(self_inj, engine, output_dll_path, include_input_utils=False):
                with open(output_dll_path, "wb") as df:
                    df.write(b"MZ_FAKE")
                return True, "ok"

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            with patch.object(type(pipe.adapter.injector), "compile_injector", fake_compile):
                rep = pipe.translate(unity_dir, skip_inject=False)

            self.assertTrue(rep.success)
            self.assertIsNotNone(rep.selected_file)
            self.assertEqual(rep.selected_file.format_name, "unity_assets")
            self.assertEqual(compute_sha256(res_assets), orig_sha)
            self.assertFalse(os.path.exists(res_assets + ".bak"))

            override_path = os.path.join(unity_dir, "ArabicOverride.json")
            self.assertTrue(os.path.isfile(override_path))
            with open(override_path, "r", encoding="utf-8") as f:
                override_data = json.load(f)

            self.assertIn("Menu_Play", override_data["by_key"])
            self.assertIn("English.Menu_Play", override_data["by_key"])
            self.assertNotIn("TechId", override_data["by_key"])
            self.assertNotIn("Nouvelle partie", override_data["by_source"])
            self.assertIn("Press {0} to dive", override_data["by_source"])
            self.assertIn("{0}", override_data["by_source"]["Press {0} to dive"])
            self.assertRegex(override_data["by_source"]["New Game"], r"[\uFE70-\uFEFF]")

            restored, warnings = AutoTranslatorPipeline.rollback(unity_dir)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(override_path))
            self.assertFalse(os.path.exists(os.path.join(unity_dir, "winhttp.dll")))
            self.assertFalse(os.path.exists(os.path.join(unity_dir, "doorstop_config.ini")))
            self.assertEqual(compute_sha256(res_assets), orig_sha)
            self.assertTrue(any("ArabicOverride.json" in r for r in restored))

    def test_assets_i2languages_binary_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            unity_dir = os.path.join(tmp, "I2BinaryUnityGame")
            data_dir = os.path.join(unity_dir, "I2Game_Data")
            managed = os.path.join(data_dir, "Managed")
            os.makedirs(managed)

            with open(os.path.join(unity_dir, "I2Game.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")

            i2_blob = build_unity_i2_languages_blob(
                "I2Languages",
                terms=[
                    ("MENU_PLAY", ["Nouvelle partie", "New Game"]),
                    ("MENU_OPTIONS", ["Options", "Options"]),
                    ("MENU_AUDIO", ["Audio", "Audio Settings"]),
                    ("MENU_QUIT", ["Quitter", "Quit"]),
                ],
                languages=["French", "English"],
            )
            shared_assets = os.path.join(data_dir, "sharedassets0.assets")
            with open(shared_assets, "wb") as f:
                f.write(b"\x01\x02\x03\x04" * 4 + i2_blob + b"\x00" * 16)
            orig_sha = compute_sha256(shared_assets)

            def fake_ep(batch_text, ep, sl, tl):
                mapping = {
                    "New Game": "لعبة جديدة",
                    "Options": "خيارات",
                    "Audio Settings": "إعدادات الصوت",
                    "Quit": "خروج",
                }
                return "\n".join(mapping.get(ln, ln) for ln in batch_text.splitlines())

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(unity_dir, skip_inject=True)
            self.assertTrue(rep.success)
            self.assertIsNotNone(rep.selected_file)
            self.assertEqual(rep.selected_file.format_name, "unity_assets")
            self.assertEqual(compute_sha256(shared_assets), orig_sha)

            override_path = os.path.join(unity_dir, "ArabicOverride.json")
            self.assertTrue(os.path.isfile(override_path))
            with open(override_path, "r", encoding="utf-8") as f:
                override_data = json.load(f)
            self.assertIn("MENU_PLAY", override_data["by_key"])
            self.assertIn("I2Languages.MENU_PLAY", override_data["by_key"])
            self.assertIn("Audio Settings", override_data["by_source"])
            self.assertNotIn("Nouvelle partie", override_data["by_source"])

    def test_assets_i2languages_with_description_and_non_text_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            unity_dir = os.path.join(tmp, "I2DescUnityGame")
            data_dir = os.path.join(unity_dir, "I2Desc_Data")
            managed = os.path.join(data_dir, "Managed")
            os.makedirs(managed)

            with open(os.path.join(unity_dir, "I2Desc.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")

            i2_blob = build_unity_i2_languages_blob(
                "I2Languages",
                terms=[
                    ("Apply", ["Apply", "Применить"]),
                    ("Back", ["Back", "Назад"]),
                    ("START_NEW_GAME", ["New Game", "Новая игра"]),
                    ("AUDIO_SETTINGS", ["Audio Settings", "Настройки звука"]),
                    ("Noway Regular SDF", ["Noway Regular SDF", "NotoSansCJK Regular SDF"]),
                    ("DIALOG_01", ["Line one\nLine two", "Строка 1\nСтрока 2"]),
                    ("Quit", ["Quit", "Выйти"]),
                ],
                languages=["English", "Russian"],
                descriptions=["Apply settings", "Go back", "Start game", "Audio menu", "", "", "Exit game"],
                term_types=[0, 0, 0, 0, 7, 0, 0],
                pre_terms_ints=[0, 0, 1],
            )
            res_assets = os.path.join(data_dir, "resources.assets")
            with open(res_assets, "wb") as f:
                f.write(b"\x00\x00\x00\x00" * 4 + i2_blob + b"\x00" * 16)
            orig_sha = compute_sha256(res_assets)

            def fake_ep(batch_text, ep, sl, tl):
                mapping = {
                    "Apply": "تطبيق",
                    "Back": "رجوع",
                    "New Game": "لعبة جديدة",
                    "Audio Settings": "إعدادات الصوت",
                    "Line one": "السطر الأول",
                    "Line two": "السطر الثاني",
                    "Quit": "خروج",
                }
                return "\n".join(mapping.get(ln, ln) for ln in batch_text.splitlines())

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(unity_dir, skip_inject=True)
            self.assertTrue(rep.success)
            self.assertIsNotNone(rep.selected_file)
            self.assertEqual(rep.selected_file.format_name, "unity_assets")
            self.assertEqual(compute_sha256(res_assets), orig_sha)

            override_path = os.path.join(unity_dir, "ArabicOverride.json")
            with open(override_path, "r", encoding="utf-8") as f:
                ov = json.load(f)
            self.assertIn("Apply", ov["by_key"])
            self.assertIn("DIALOG_01", ov["by_key"])
            self.assertNotIn("Noway Regular SDF", ov["by_key"])
            self.assertNotIn("Noway Regular SDF", ov["by_source"])
            self.assertIn("Line one", ov["by_source"])
            self.assertIn("Line two", ov["by_source"])

    def test_compile_real_csharp_injector_with_csc(self):
        csc = find_csc_compiler()
        if not csc:
            self.skipTest("csc.exe غير متوفر على هذا النظام")

        core_stub_cs = r"""
using System;
namespace UnityEngine
{
    public enum HideFlags { None = 0, DontSave = 52, HideAndDontSave = 61 }
    public enum RuntimeInitializeLoadType { AfterSceneLoad = 0 }
    [AttributeUsage(AttributeTargets.Method)]
    public sealed class RuntimeInitializeOnLoadMethodAttribute : Attribute
    {
        public RuntimeInitializeOnLoadMethodAttribute(RuntimeInitializeLoadType t) {}
    }
    public enum TextureFormat { Alpha8 = 1 }
    public struct Color32 { public byte r, g, b, a; }
    public enum KeyCode { None = 0 }
    public class Object
    {
        public HideFlags hideFlags { get; set; }
        public string name { get; set; }
        public static void DontDestroyOnLoad(Object target) {}
        public static void Destroy(Object obj) {}
    }
    public class Component : Object
    {
        public GameObject gameObject { get { return null; } }
    }
    public class MonoBehaviour : Component {}
    public class ScriptableObject : Object
    {
        public static T CreateInstance<T>() where T : ScriptableObject, new() { return new T(); }
    }
    public sealed class GameObject : Object
    {
        public GameObject(string name) {}
        public bool activeInHierarchy { get { return true; } }
        public T AddComponent<T>() where T : Component, new() { return new T(); }
    }
    public sealed class Camera : Component
    {
        public delegate void CameraCallback(Camera cam);
        public static event CameraCallback onPreRender;
    }
    public static class Application
    {
        public static string dataPath { get { return string.Empty; } }
        public static string streamingAssetsPath { get { return string.Empty; } }
        public static event Action onBeforeRender;
    }
    public static class Time { public static int frameCount { get { return 1; } } }
    public static class Resources
    {
        public static T[] FindObjectsOfTypeAll<T>() where T : Object { return new T[0]; }
        public static Object[] FindObjectsOfTypeAll(Type t) { return new Object[0]; }
    }
    public sealed class Font : Object
    {
        public Font(string name) {}
        public static Font CreateDynamicFontFromOSFont(string[] names, int size) { return null; }
        public bool HasCharacter(char c) { return false; }
    }
    public sealed class Texture2D : Object
    {
        public Texture2D(int w, int h, TextureFormat fmt, bool mip) {}
        public void SetPixels32(Color32[] cols) {}
        public void Apply(bool u, bool r) {}
    }
    public sealed class Shader : Object { public static Shader Find(string n) { return null; } }
    public sealed class Material : Object
    {
        public Shader shader { get { return null; } }
        public Material(Shader s) {}
        public void SetTexture(int id, Texture2D tex) {}
        public void SetFloat(int id, float v) {}
    }
    public static class Input
    {
        public static bool anyKey { get { return false; } }
        public static bool anyKeyDown { get { return false; } }
        public static bool GetKeyDown(KeyCode k) { return false; }
        public static bool GetKeyUp(KeyCode k) { return false; }
        public static bool GetKey(KeyCode k) { return false; }
        public static float GetAxis(string a) { return 0f; }
    }
}
"""
        tmp_stub_cs = r"""
using System.Collections.Generic;
using UnityEngine;
namespace UnityEngine.TextCore
{
    public struct FaceInfo {}
    public struct GlyphRect { public GlyphRect(int x, int y, int w, int h) {} }
    public class Glyph { public int atlasIndex { get; set; } }
}
namespace UnityEngine.TextCore.LowLevel
{
    public enum GlyphRenderMode { SDFAA = 4165 }
    public enum GlyphPackingMode { BestShortSideFit = 0 }
    public enum FontEngineError { Success = 0 }
    public static class FontEngine
    {
        public static FontEngineError InitializeFontEngine() { return FontEngineError.Success; }
        public static FontEngineError LoadFontFace(string p, int pt) { return FontEngineError.Success; }
        public static FontEngineError LoadFontFace(byte[] b, int pt) { return FontEngineError.Success; }
        public static bool TryAddGlyphToTexture(uint idx, int pad, GlyphPackingMode m, List<GlyphRect> f, List<GlyphRect> u, GlyphRenderMode r, Texture2D t, out Glyph g) { g = new Glyph(); return true; }
        public static void ResetAtlasTexture(Texture2D t) {}
        public static FaceInfo GetFaceInfo() { return new FaceInfo(); }
        public static bool TryGetGlyphIndex(uint cp, out uint idx) { idx = 0; return false; }
    }
}
namespace TMPro
{
    using UnityEngine.TextCore;
    using UnityEngine.TextCore.LowLevel;
    public enum AtlasPopulationMode { Static = 0, Dynamic = 1 }
    public class TMP_Character { public TMP_Character(uint u, TMP_FontAsset f, Glyph g) {} }
    public class TMP_FontAsset : ScriptableObject
    {
        public List<Glyph> glyphTable { get; set; }
        public List<TMP_Character> characterTable { get; set; }
        public Dictionary<uint, TMP_Character> characterLookupTable { get; set; }
        public Texture2D atlasTexture { get; set; }
        public Texture2D[] atlasTextures { get; set; }
        public bool isMultiAtlasTexturesEnabled { get; set; }
        public Material material { get; set; }
        public FaceInfo faceInfo { get; set; }
        public AtlasPopulationMode atlasPopulationMode { get; set; }
        public float normalStyle { get { return 0f; } }
        public float boldStyle { get { return 0f; } }
        public static TMP_FontAsset CreateFontAsset(Font f, int s, int p, GlyphRenderMode r, int w, int h, AtlasPopulationMode m, bool e) { return null; }
        public bool TryAddCharacters(uint[] c, out uint[] m, bool i) { m = null; return true; }
        public void ReadFontAssetDefinition() {}
    }
    public class TMP_Settings
    {
        public static TMP_Settings instance { get { return null; } }
        public static List<TMP_FontAsset> fallbackFontAssets { get { return null; } }
    }
    public class TMP_Text : Component
    {
        public string text { get; set; }
        public void ForceMeshUpdate(bool i, bool f) {}
        public void SetAllDirty() {}
    }
    public static class ShaderUtilities
    {
        public static Shader ShaderRef_MobileSDF { get { return null; } }
        public static int ID_MainTex = 1;
        public static int ID_TextureWidth = 2;
        public static int ID_TextureHeight = 3;
        public static int ID_GradientScale = 4;
        public static int ID_WeightNormal = 5;
        public static int ID_WeightBold = 6;
    }
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            managed = os.path.join(tmp, "Game_Data", "Managed")
            os.makedirs(managed)
            fx_dir = os.path.dirname(csc)
            for fx_dll in ("mscorlib.dll", "System.dll", "System.Core.dll"):
                shutil.copy2(os.path.join(fx_dir, fx_dll), os.path.join(managed, fx_dll))

            core_cs_path = os.path.join(tmp, "CoreStub.cs")
            tmp_cs_path = os.path.join(tmp, "TmpStub.cs")
            with open(core_cs_path, "w", encoding="utf-8") as f:
                f.write(core_stub_cs)
            with open(tmp_cs_path, "w", encoding="utf-8") as f:
                f.write(tmp_stub_cs)

            core_dll = os.path.join(managed, "UnityEngine.CoreModule.dll")
            tmp_dll = os.path.join(managed, "Unity.TextMeshPro.dll")

            p1 = subprocess.run(
                [csc, "/nologo", "/target:library", f"/out:{core_dll}", core_cs_path],
                capture_output=True,
                text=True,
            )
            self.assertEqual(p1.returncode, 0, p1.stdout + p1.stderr)

            # التحقق أولاً من مسار UGUI بدون Unity.TextMeshPro.dll
            engine = EngineInfo(
                engine_name="Unity",
                supported=True,
                game_dir=tmp,
                architecture="x64",
                scripting_backend="Mono",
                managed_dir=managed,
                has_tmp=False,
                has_ugui=True,
            )
            injector = UnityInjector()
            ugui_out = os.path.join(tmp, "OutUgui.dll")
            ok_ugui, msg_ugui = injector.compile_injector(engine, ugui_out, include_input_utils=False)
            self.assertTrue(ok_ugui, msg_ugui)
            self.assertTrue(os.path.isfile(ugui_out))

            # التحقق ثانياً من مسار USE_TMP + INCLUDE_INPUT_UTILS
            p2 = subprocess.run(
                [csc, "/nologo", "/target:library", f"/r:{core_dll}", f"/out:{tmp_dll}", tmp_cs_path],
                capture_output=True,
                text=True,
            )
            self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)

            engine.has_tmp = True
            tmp_out = os.path.join(tmp, "OutTmp.dll")
            ok_tmp, msg_tmp = injector.compile_injector(engine, tmp_out, include_input_utils=True)
            self.assertTrue(ok_tmp, msg_tmp)
            with open(tmp_out, "rb") as f:
                self.assertEqual(f.read(2), b"MZ")

    def test_gui_workflow_scan_translate_and_rollback(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"تعذر تهيئة Tkinter في هذه البيئة: {exc}")

        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                unity_dir = os.path.join(tmp, "GuiUnityGame")
                managed = os.path.join(unity_dir, "MyGame_Data", "Managed")
                lang_dir = os.path.join(unity_dir, "MyGame_Data", "StreamingAssets", "Localization")
                os.makedirs(managed)
                os.makedirs(lang_dir)

                with open(os.path.join(unity_dir, "MyGame.exe"), "wb") as f:
                    f.write(_make_minimal_x64_pe())
                for dll in ("Assembly-CSharp.dll", "Unity.TextMeshPro.dll"):
                    with open(os.path.join(managed, dll), "wb") as f:
                        f.write(b"MZ")

                en_path = os.path.join(lang_dir, "English.json")
                orig_json = {
                    "Play": "New Game",
                    "Continue": "Continue",
                    "Options": "Options",
                    "Audio": "Audio Settings",
                    "Quit": "Quit",
                }
                with open(en_path, "w", encoding="utf-8") as f:
                    json.dump(orig_json, f)

                def fake_ep(batch_text, ep, sl, tl):
                    return "\n".join("عربي:" + ln for ln in batch_text.splitlines())

                fake_pipe = AutoTranslatorPipeline(
                    translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
                )
                app = TranslatorGUI(root=root, initial_game_dir="", pipeline=fake_pipe)
                app.inject_var.set(False)

                scan_rep = app.run_scan(unity_dir)
                self.assertIsNotNone(scan_rep)
                self.assertTrue(scan_rep.success)
                self.assertIn("Unity", app.engine_label_var.get())
                self.assertIn("English.json", app.file_label_var.get())
                self.assertIn("100", app.score_label_var.get() + str(scan_rep.selected_file.score))

                tr_rep = app.run_translate_sync(unity_dir)
                self.assertIsNotNone(tr_rep)
                self.assertTrue(tr_rep.success)
                self.assertEqual(float(app.progress_bar["value"]), 100.0)
                self.assertTrue(os.path.isfile(en_path + ".bak"))
                with open(en_path, "r", encoding="utf-8") as f:
                    translated_data = json.load(f)
                self.assertRegex(translated_data["Play"], r"[\uFE70-\uFEFF]")

                restored, warnings = app.run_rollback(unity_dir)
                self.assertEqual(warnings, [])
                self.assertTrue(any("English.json" in r for r in restored))
                with open(en_path, "r", encoding="utf-8") as f:
                    self.assertEqual(json.load(f), orig_json)
        finally:
            root.destroy()


class Il2CppPhase4Tests(unittest.TestCase):
    def test_il2cpp_detection_and_global_metadata_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            unity_dir = os.path.join(tmp, "Il2CppMetaGame")
            data_dir = os.path.join(unity_dir, "MyGame_Data")
            meta_dir = os.path.join(data_dir, "il2cpp_data", "Metadata")
            lang_dir = os.path.join(data_dir, "StreamingAssets", "LanguageFiles")
            os.makedirs(meta_dir)
            os.makedirs(lang_dir)

            with open(os.path.join(unity_dir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(unity_dir, "GameAssembly.dll"), "wb") as f:
                f.write(_make_minimal_x64_pe())

            meta_path = os.path.join(meta_dir, "global-metadata.dat")
            meta_blob = build_il2cpp_global_metadata_blob(
                string_literals=["LanguageFiles/{0}.json", "English", "Press {0} to start"],
                identifiers=["Unity.TextMeshPro.dll", "TMP_FontAsset", "UnityEngine.UI.dll"],
            )
            with open(meta_path, "wb") as f:
                f.write(meta_blob)

            en_path = os.path.join(lang_dir, "English.json")
            with open(en_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "Menu_Play": "New Game",
                        "Menu_Cont": "Continue",
                        "Menu_Opt": "Options",
                        "Menu_Save": "Save Game",
                        "Menu_Quit": "Quit",
                        "Hint": "Press {0} to start",
                    },
                    f,
                )
            with open(os.path.join(lang_dir, "French.json"), "w", encoding="utf-8") as f:
                json.dump({"Menu_Play": "Nouvelle partie"}, f)
            with open(os.path.join(lang_dir, "German.json"), "w", encoding="utf-8") as f:
                json.dump({"Menu_Play": "Neues Spiel"}, f)

            engine = UnityDetector().detect(unity_dir)
            self.assertTrue(engine.supported)
            self.assertEqual(engine.scripting_backend, "IL2CPP")
            self.assertEqual(engine.architecture, "x64")
            self.assertIsNone(engine.managed_dir)
            self.assertEqual(engine.il2cpp_metadata_path, meta_path)
            self.assertEqual(engine.mod_loader, "none")
            self.assertTrue(engine.has_tmp)
            self.assertTrue(engine.has_ugui)

            candidates, hints = UnityLanguageScanner().scan(engine)
            self.assertIn("LanguageFiles/{0}.json", hints)
            self.assertTrue(candidates)
            top = candidates[0]
            self.assertEqual(top.relative_path.replace("\\", "/"), "MyGame_Data/StreamingAssets/LanguageFiles/English.json")
            self.assertTrue(top.dll_matched)
            self.assertGreaterEqual(top.score, 85)

    def test_il2cpp_vanilla_packed_assets_doorstop_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            unity_dir = os.path.join(tmp, "VanillaIl2CppPacked")
            data_dir = os.path.join(unity_dir, "MyGame_Data")
            meta_dir = os.path.join(data_dir, "il2cpp_data", "Metadata")
            dotnet_dir = os.path.join(unity_dir, "dotnet")
            os.makedirs(meta_dir)
            os.makedirs(dotnet_dir)

            with open(os.path.join(unity_dir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(unity_dir, "GameAssembly.dll"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(dotnet_dir, "coreclr.dll"), "wb") as f:
                f.write(b"MZ_CORECLR")

            meta_path = os.path.join(meta_dir, "global-metadata.dat")
            with open(meta_path, "wb") as f:
                f.write(
                    build_il2cpp_global_metadata_blob(
                        string_literals=["Localization", "English"],
                        identifiers=["Unity.TextMeshPro.dll", "TMP_Text"],
                    )
                )

            text_payload = json.dumps(
                {
                    "Menu_Play": "New Game",
                    "Menu_Cont": "Continue",
                    "Menu_Opt": "Options",
                    "Menu_Save": "Save Game",
                    "Menu_Quit": "Quit",
                    "Hint_Start": "Press {0} to start",
                }
            )
            res_assets = os.path.join(data_dir, "resources.assets")
            with open(res_assets, "wb") as f:
                f.write(b"\xAA\xBB\xCC\xDD" * 4 + build_unity_text_asset_blob("English", text_payload))
            orig_sha = compute_sha256(res_assets)

            def fake_ep(batch_text, ep, sl, tl):
                mapping = {
                    "New Game": "لعبة جديدة",
                    "Continue": "متابعة",
                    "Options": "خيارات",
                    "Save Game": "حفظ اللعبة",
                    "Quit": "خروج",
                    "Press [[[0]]] to start": "اضغط [[[0]]] للبدء",
                }
                return "\n".join(mapping.get(ln, ln) for ln in batch_text.splitlines())

            def fake_compile_il2cpp(self_inj, engine, output_dll_path):
                os.makedirs(os.path.dirname(os.path.abspath(output_dll_path)), exist_ok=True)
                with open(output_dll_path, "wb") as df:
                    df.write(b"MZ_IL2CPP_INJECTOR")
                return True, "ok"

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            with patch.object(type(pipe.adapter.injector), "compile_il2cpp_injector", fake_compile_il2cpp):
                rep = pipe.translate(unity_dir, skip_inject=False)

            self.assertTrue(rep.success)
            self.assertIsNotNone(rep.selected_file)
            self.assertEqual(rep.selected_file.format_name, "unity_assets")
            self.assertIsNotNone(rep.injection)
            self.assertEqual(rep.injection.method, "il2cpp_doorstop")
            self.assertEqual(compute_sha256(res_assets), orig_sha)

            override_path = os.path.join(unity_dir, "ArabicOverride.json")
            inj_dll = os.path.join(unity_dir, "ArabicRuntimeInjector.dll")
            winhttp_dst = os.path.join(unity_dir, "winhttp.dll")
            cfg_dst = os.path.join(unity_dir, "doorstop_config.ini")
            for p in (override_path, inj_dll, winhttp_dst, cfg_dst):
                self.assertTrue(os.path.isfile(p), f"Missing deployed file: {p}")

            with open(override_path, "r", encoding="utf-8") as f:
                ov = json.load(f)
            self.assertIn("{0}", ov["by_source"]["Press {0} to start"])
            self.assertRegex(ov["by_source"]["New Game"], r"[\uFE70-\uFEFF]")

            with open(cfg_dst, "r", encoding="utf-8") as f:
                cfg_txt = f.read()
            self.assertIn("[Il2Cpp]", cfg_txt)
            self.assertIn("coreclr_path=", cfg_txt)
            self.assertIn("corlib_dir=", cfg_txt)
            self.assertIn("target_assembly=ArabicRuntimeInjector.dll", cfg_txt)

            restored, warnings = AutoTranslatorPipeline.rollback(unity_dir)
            self.assertEqual(warnings, [])
            for p in (override_path, inj_dll, winhttp_dst, cfg_dst):
                self.assertFalse(os.path.exists(p), f"File left after rollback: {p}")
            self.assertEqual(compute_sha256(res_assets), orig_sha)
            self.assertTrue(any("ArabicRuntimeInjector.dll" in r for r in restored))

    def test_il2cpp_bepinex_and_melonloader_injection_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1. BepInEx 6 IL2CPP
            bep_dir = os.path.join(tmp, "BepInExIl2CppGame")
            os.makedirs(os.path.join(bep_dir, "BepInEx", "interop"))
            os.makedirs(os.path.join(bep_dir, "MyGame_Data", "il2cpp_data", "Metadata"))
            with open(os.path.join(bep_dir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(bep_dir, "GameAssembly.dll"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(bep_dir, "BepInEx", "interop", "Unity.TextMeshPro.dll"), "wb") as f:
                f.write(b"MZ")
            existing_cfg = os.path.join(bep_dir, "doorstop_config.ini")
            with open(existing_cfg, "w", encoding="utf-8") as f:
                f.write("# existing BepInEx doorstop config\n")

            eng_bep = UnityDetector().detect(bep_dir)
            self.assertEqual(eng_bep.scripting_backend, "IL2CPP")
            self.assertEqual(eng_bep.mod_loader, "BepInEx")
            self.assertTrue(eng_bep.has_tmp)
            self.assertIsNotNone(eng_bep.il2cpp_interop_dir)

            def fake_compile_il2cpp(self_inj, engine, output_dll_path):
                os.makedirs(os.path.dirname(os.path.abspath(output_dll_path)), exist_ok=True)
                with open(output_dll_path, "wb") as df:
                    df.write(b"MZ_IL2CPP_PLUGIN")
                return True, "ok"

            inj = UnityInjector()
            with patch.object(UnityInjector, "compile_il2cpp_injector", fake_compile_il2cpp):
                res_bep = inj.inject(eng_bep, skip_runtime_font=False)

            self.assertTrue(res_bep.success)
            self.assertEqual(res_bep.method, "il2cpp_bepinex")
            bep_plugin = os.path.join(bep_dir, "BepInEx", "plugins", "ArabicRuntimeInjector.dll")
            self.assertTrue(os.path.isfile(bep_plugin))
            with open(existing_cfg, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "# existing BepInEx doorstop config\n")

            _, warn_bep = AutoTranslatorPipeline.rollback(bep_dir)
            self.assertEqual(warn_bep, [])
            self.assertFalse(os.path.exists(bep_plugin))
            self.assertTrue(os.path.isfile(existing_cfg))

            # 2. MelonLoader IL2CPP
            melon_dir = os.path.join(tmp, "MelonIl2CppGame")
            os.makedirs(os.path.join(melon_dir, "MelonLoader", "Il2CppAssemblies"))
            os.makedirs(os.path.join(melon_dir, "MyGame_Data", "il2cpp_data", "Metadata"))
            with open(os.path.join(melon_dir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(melon_dir, "GameAssembly.dll"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(melon_dir, "MelonLoader", "Il2CppAssemblies", "UnityEngine.UI.dll"), "wb") as f:
                f.write(b"MZ")

            eng_melon = UnityDetector().detect(melon_dir)
            self.assertEqual(eng_melon.scripting_backend, "IL2CPP")
            self.assertEqual(eng_melon.mod_loader, "MelonLoader")
            self.assertTrue(eng_melon.has_ugui)
            self.assertIsNotNone(eng_melon.il2cpp_interop_dir)

            with patch.object(UnityInjector, "compile_il2cpp_injector", fake_compile_il2cpp):
                res_melon = inj.inject(eng_melon, skip_runtime_font=False)

            self.assertTrue(res_melon.success)
            self.assertEqual(res_melon.method, "il2cpp_melonloader")
            melon_mod = os.path.join(melon_dir, "Mods", "ArabicRuntimeInjector.dll")
            self.assertTrue(os.path.isfile(melon_mod))

            _, warn_melon = AutoTranslatorPipeline.rollback(melon_dir)
            self.assertEqual(warn_melon, [])
            self.assertFalse(os.path.exists(melon_mod))

    def test_compile_real_il2cpp_injector_with_csc(self):
        csc = find_csc_compiler()
        if not csc:
            self.skipTest("csc.exe غير متوفر على هذا النظام")

        with tempfile.TemporaryDirectory() as tmp:
            engine = EngineInfo(
                engine_name="Unity",
                supported=True,
                game_dir=tmp,
                architecture="x64",
                scripting_backend="IL2CPP",
                managed_dir=None,
                has_tmp=True,
                has_ugui=True,
            )
            out_dll = os.path.join(tmp, "ArabicRuntimeInjector.dll")
            ok, msg = UnityInjector().compile_il2cpp_injector(engine, out_dll)
            self.assertTrue(ok, msg)
            self.assertTrue(os.path.isfile(out_dll))
            with open(out_dll, "rb") as f:
                self.assertEqual(f.read(2), b"MZ")


class BackupUxAndGeneralizationTests(unittest.TestCase):
    def test_persistent_backup_summary_and_gui_prompt_choice(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"تعذر تهيئة Tkinter في هذه البيئة: {exc}")

        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                unity_dir = os.path.join(tmp, "PersistentBackupGame")
                managed = os.path.join(unity_dir, "MyGame_Data", "Managed")
                lang_dir = os.path.join(unity_dir, "MyGame_Data", "StreamingAssets", "Localization")
                os.makedirs(managed)
                os.makedirs(lang_dir)

                with open(os.path.join(unity_dir, "MyGame.exe"), "wb") as f:
                    f.write(_make_minimal_x64_pe())
                for dll in ("Assembly-CSharp.dll", "Unity.TextMeshPro.dll"):
                    with open(os.path.join(managed, dll), "wb") as f:
                        f.write(b"MZ")

                en_path = os.path.join(lang_dir, "English.json")
                orig_json = {
                    "Play": "New Game",
                    "Continue": "Continue",
                    "Options": "Options",
                    "Audio": "Audio Settings",
                    "Quit": "Quit",
                }
                with open(en_path, "w", encoding="utf-8") as f:
                    json.dump(orig_json, f)

                self.assertFalse(BackupManager.get_backup_summary(unity_dir)["has_backup"])

                def fake_ep(batch_text, ep, sl, tl):
                    return "\n".join("عربي:" + ln for ln in batch_text.splitlines())

                def fake_compile(self_inj, engine, output_dll_path, include_input_utils=False):
                    with open(output_dll_path, "wb") as df:
                        df.write(b"MZ_FAKE")
                    return True, "ok"

                pipe = AutoTranslatorPipeline(
                    translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
                )
                with patch.object(type(pipe.adapter.injector), "compile_injector", fake_compile):
                    rep = pipe.translate(unity_dir, skip_inject=False)
                self.assertTrue(rep.success)

                font_log = os.path.join(unity_dir, "arabic_font_log.txt")
                with open(font_log, "w", encoding="utf-8") as f:
                    f.write("dummy log\n")

                summary = BackupManager.get_backup_summary(unity_dir)
                self.assertTrue(summary["has_backup"])
                self.assertGreaterEqual(summary["replaced_count"], 1)
                self.assertGreaterEqual(summary["created_count"], 1)

                # محاكاة فتح البرنامج من جديد واختيار المستخدم "لا" للإبقاء على النسخة الاحتياطية وإعادة التجربة
                app2 = TranslatorGUI(root=root, initial_game_dir="", pipeline=pipe)
                with patch("ui.gui.messagebox.askyesno", return_value=False) as mock_ask:
                    app2.run_scan(unity_dir)
                    restored_flag = app2.prompt_backup_decision_if_present(unity_dir)
                    self.assertTrue(mock_ask.called)
                    self.assertFalse(restored_flag)
                self.assertTrue(BackupManager.get_backup_summary(unity_dir)["has_backup"])

                # اختيار المستخدم "نعم" لاسترجاع النسخة الأصلية فوراً
                with patch("ui.gui.messagebox.askyesno", return_value=True) as mock_ask:
                    restored_flag = app2.prompt_backup_decision_if_present(unity_dir)
                    self.assertTrue(mock_ask.called)
                    self.assertTrue(restored_flag)

                self.assertFalse(BackupManager.get_backup_summary(unity_dir)["has_backup"])
                self.assertFalse(os.path.exists(font_log))
                self.assertFalse(os.path.exists(os.path.join(unity_dir, "winhttp.dll")))
                with open(en_path, "r", encoding="utf-8") as f:
                    self.assertEqual(json.load(f), orig_json)
        finally:
            root.destroy()

    def test_bytes_json_autodetect_and_serialized_kv_table_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            # أ) ملف لغة بامتداد .bytes يحوي JSON مع استبعاد .bytes ثنائي
            bytes_game = os.path.join(tmp, "BytesJsonGame")
            managed_b = os.path.join(bytes_game, "Game_Data", "Managed")
            loc_b = os.path.join(bytes_game, "Game_Data", "StreamingAssets", "Localization")
            os.makedirs(managed_b)
            os.makedirs(loc_b)
            with open(os.path.join(bytes_game, "Game.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed_b, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")

            en_bytes = os.path.join(loc_b, "English.bytes")
            with open(en_bytes, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "Menu_Play": "New Game",
                        "Menu_Cont": "Continue",
                        "Menu_Opt": "Options",
                        "Menu_Audio": "Audio Settings",
                        "Menu_Quit": "Quit",
                    },
                    f,
                )
            with open(os.path.join(loc_b, "BundleData.bytes"), "wb") as f:
                f.write(b"UnityFS\x00\x00\x00\x075.x.x\x00" * 4)

            def fake_ep(batch_text, ep, sl, tl):
                return "\n".join("ع:" + ln for ln in batch_text.splitlines())

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep_b = pipe.translate(bytes_game, skip_inject=True)
            self.assertTrue(rep_b.success)
            self.assertIsNotNone(rep_b.selected_file)
            self.assertEqual(rep_b.selected_file.format_name, "json")
            self.assertTrue(rep_b.selected_file.relative_path.endswith("English.bytes"))
            with open(en_bytes, "r", encoding="utf-8") as f:
                out_b = json.load(f)
            self.assertRegex(out_b["Menu_Play"], r"[\uFE70-\uFEFF]")

            # ب) جدول نصوص متسلسل (Key/Value) داخل globalgamemanagers.assets
            kv_game = os.path.join(tmp, "KvAssetsGame")
            data_kv = os.path.join(kv_game, "Game_Data")
            managed_kv = os.path.join(data_kv, "Managed")
            os.makedirs(managed_kv)
            with open(os.path.join(kv_game, "Game.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed_kv, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")

            kv_pairs = [
                ("MENU_NEW_GAME", "New Game"),
                ("MENU_CONTINUE", "Continue"),
                ("MENU_OPTIONS", "Options"),
                ("MENU_AUDIO", "Audio Settings"),
                ("MENU_SAVE", "Save Game"),
                ("MENU_QUIT", "Quit"),
            ]
            kv_blob = bytearray(_pack_aligned_string("EnglishStringTable"))
            kv_blob.extend(struct.pack("<I", len(kv_pairs)))
            for k, v in kv_pairs:
                kv_blob.extend(_pack_aligned_string(k))
                kv_blob.extend(_pack_aligned_string(v))

            ggm_assets = os.path.join(data_kv, "globalgamemanagers.assets")
            with open(ggm_assets, "wb") as f:
                f.write(b"\x01\x02\x03\x04" * 4 + bytes(kv_blob) + b"\x00" * 16)
            orig_sha = compute_sha256(ggm_assets)

            rep_kv = pipe.translate(kv_game, skip_inject=True)
            self.assertTrue(rep_kv.success)
            self.assertIsNotNone(rep_kv.selected_file)
            self.assertEqual(rep_kv.selected_file.format_name, "unity_assets")
            self.assertEqual(compute_sha256(ggm_assets), orig_sha)
            ov_path = os.path.join(kv_game, "ArabicOverride.json")
            self.assertTrue(os.path.isfile(ov_path))
            with open(ov_path, "r", encoding="utf-8") as f:
                ov_data = json.load(f)
            self.assertIn("MENU_NEW_GAME", ov_data["by_key"])
            self.assertIn("Audio Settings", ov_data["by_source"])


class Unity6Il2CppAndUnityFsTests(unittest.TestCase):
    def test_modern_il2cpp_metadata_v108_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta_path = os.path.join(tmp, "global-metadata.dat")
            blob = build_il2cpp_modern_metadata_blob(
                string_literals=["New Game", "Bring home", "Options", "Quit"],
                identifiers=["Assembly-CSharp.dll", "CatMenuController", "Unity.TextMeshPro"],
                version=108,
            )
            with open(meta_path, "wb") as f:
                f.write(blob)

            extracted = extract_il2cpp_metadata_strings(meta_path)
            for expected in ("New Game", "Bring home", "CatMenuController", "Unity.TextMeshPro"):
                self.assertIn(expected, extracted)

    def test_unityfs_lz4_bundle_and_scene_text_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = os.path.join(tmp, "UnityFsGame")
            data_dir = os.path.join(game_dir, "UnityFsGame_Data")
            meta_dir = os.path.join(data_dir, "il2cpp_data", "Metadata")
            os.makedirs(meta_dir)

            with open(os.path.join(game_dir, "UnityFsGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(game_dir, "GameAssembly.dll"), "wb") as f:
                f.write(b"MZ")
            with open(os.path.join(data_dir, "ScriptingAssemblies.json"), "w", encoding="utf-8") as f:
                json.dump({"names": ["Unity.TextMeshPro.dll", "UnityEngine.UI.dll"]}, f)
            with open(os.path.join(meta_dir, "global-metadata.dat"), "wb") as f:
                f.write(
                    build_il2cpp_modern_metadata_blob(
                        ["New Game", "Options", "Quit"],
                        ["TMP_Text", "LocalizationManager"],
                        version=108,
                    )
                )

            level0_buf = bytearray(b"\x00" * 64)
            for label in (
                "New Game",
                "Continue",
                "Options",
                "Settings",
                "Audio",
                "Back",
                "Quit",
                "Always on top",
                "Click me to open menu!",
            ):
                level0_buf.extend(struct.pack("<7I", 0, 0, 0, 0, 0, 1, 0))
                level0_buf.extend(_pack_aligned_string(label))
                level0_buf.extend(struct.pack("<II", 0, 0))
            level0_buf.extend(_pack_aligned_string("Drag to move and right click to pet."))

            bundle_bytes = build_unityfs_bundle_blob(
                [
                    ("level0", bytes(level0_buf)),
                    ("level0.resS", b"\xFF" * 2048),
                ],
                compress_lz4=True,
            )
            nodes = extract_unityfs_nodes_from_bytes(bundle_bytes)
            self.assertEqual([n for n, _ in nodes], ["level0"])

            unity3d_path = os.path.join(data_dir, "data.unity3d")
            with open(unity3d_path, "wb") as f:
                f.write(bundle_bytes)
            orig_sha = compute_sha256(unity3d_path)

            def fake_ep(batch_text, ep, sl, tl):
                return "\n".join("ع:" + ln for ln in batch_text.splitlines())

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(game_dir, skip_inject=True)
            self.assertTrue(rep.success)
            self.assertIsNotNone(rep.selected_file)
            self.assertEqual(rep.selected_file.format_name, "unity_assets")
            self.assertTrue(rep.selected_file.relative_path.endswith("data.unity3d"))
            self.assertEqual(compute_sha256(unity3d_path), orig_sha)

            ov_path = os.path.join(game_dir, "ArabicOverride.json")
            self.assertTrue(os.path.isfile(ov_path))
            with open(ov_path, "r", encoding="utf-8") as f:
                ov = json.load(f)
            self.assertIn("New Game", ov["by_source"])
            self.assertIn("Always on top", ov["by_source"])
            self.assertIn("Drag to move and right click to pet.", ov["by_source"])

            restored, warnings = AutoTranslatorPipeline.rollback(game_dir)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(ov_path))

    def test_csc_compiled_dll_internal_assembly_name_and_coreclr_delegate(self):
        csc = find_csc_compiler()
        if not csc:
            self.skipTest("csc.exe غير متوفر على هذا النظام")

        with tempfile.TemporaryDirectory() as tmp:
            engine = EngineInfo(
                engine_name="Unity",
                supported=True,
                game_dir=tmp,
                architecture="x64",
                scripting_backend="IL2CPP",
                managed_dir=None,
                has_tmp=True,
                has_ugui=True,
            )
            dll_path = os.path.join(tmp, "ArabicRuntimeInjector.dll")
            ok, msg = UnityInjector().compile_il2cpp_injector(engine, dll_path)
            self.assertTrue(ok, msg)

            with open(dll_path, "rb") as f:
                dll_bytes = f.read()
            self.assertIn(b"\x00ArabicRuntimeInjector\x00", dll_bytes)
            self.assertNotIn(b".dll.tmp", dll_bytes)

            coreclr_info = find_coreclr_runtime(tmp, "x64")
            if not coreclr_info:
                self.skipTest("CoreCLR runtime غير مثبت على هذا الجهاز")
            coreclr_path, corlib_dir = coreclr_info
            probe_code = (
                "import ctypes, os, sys\n"
                "coreclr_path, corlib_dir, dll_path, work_dir = sys.argv[1:5]\n"
                "clr = ctypes.CDLL(coreclr_path)\n"
                "tpa = [os.path.join(corlib_dir, f) for f in os.listdir(corlib_dir) if f.endswith('.dll')]\n"
                "tpa.append(dll_path)\n"
                "keys = (ctypes.c_char_p * 1)(b'TRUSTED_PLATFORM_ASSEMBLIES')\n"
                "vals = (ctypes.c_char_p * 1)(';'.join(tpa).encode('utf-8'))\n"
                "hh = ctypes.c_void_p()\n"
                "did = ctypes.c_uint32()\n"
                "hr1 = clr.coreclr_initialize(work_dir.encode('utf-8'), b'Doorstop Domain', 1, keys, vals, ctypes.byref(hh), ctypes.byref(did))\n"
                "fn = ctypes.c_void_p()\n"
                "hr2 = clr.coreclr_create_delegate(hh, did, b'ArabicRuntimeInjector', b'Doorstop.Entrypoint', b'Start', ctypes.byref(fn))\n"
                "assert hr1 == 0 and hr2 == 0 and fn.value, f'hr1={hr1:#x} hr2={hr2:#x}'\n"
                "print('CORECLR_DELEGATE_OK')\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", probe_code, coreclr_path, corlib_dir, dll_path, tmp],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + "\n" + proc.stderr)
            self.assertIn("CORECLR_DELEGATE_OK", proc.stdout)


class CompressedAndLocalizationTableTests(unittest.TestCase):
    def test_compressed_bytes_and_base64_text_asset_extraction(self):
        sample_json = json.dumps(
            {
                "menu_play": "Play Game",
                "menu_opts": "Open Settings",
                "menu_quit": "Exit to Desktop",
                "menu_save": "Save Progress Now",
            }
        )
        gz_payload = gzip.compress(sample_json.encode("utf-8"))
        zlib_payload = zlib.compress(sample_json.encode("utf-8"))
        b64_gz_payload = base64.b64encode(gz_payload)

        self.assertIsNotNone(decode_compressed_or_encoded_payload(gz_payload))
        self.assertIsNotNone(decode_compressed_or_encoded_payload(zlib_payload))
        decoded_nested = decode_compressed_or_encoded_payload(b64_gz_payload)
        self.assertIsNotNone(decoded_nested)
        self.assertIn("Play Game", decoded_nested)

        with tempfile.TemporaryDirectory() as tmp:
            unity_dir = os.path.join(tmp, "CompressedBytesGame")
            managed = os.path.join(unity_dir, "MyGame_Data", "Managed")
            stream_dir = os.path.join(unity_dir, "MyGame_Data", "StreamingAssets")
            os.makedirs(managed)
            os.makedirs(stream_dir)
            with open(os.path.join(unity_dir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")

            bytes_path = os.path.join(stream_dir, "English.bytes")
            with open(bytes_path, "wb") as f:
                f.write(gz_payload)

            eng = UnityDetector().detect(unity_dir)
            cands, _ = UnityLanguageScanner().scan(eng)
            self.assertTrue(cands, "Expected compressed .bytes candidate")
            self.assertEqual(cands[0].format_name, "unity_assets")
            self.assertTrue(cands[0].relative_path.endswith("English.bytes"))

            blocks = extract_assets_localization_blocks(bytes_path)
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0].block_kind, "Bytes:json")
            self.assertGreaterEqual(len(blocks[0].entries), 4)

    def test_unity_localization_string_table_and_addressables_bundle(self):
        rows = [
            (10001, "Continue Adventure"),
            (10002, "Graphics Options"),
            (10003, "Return to Main Menu"),
            (10004, "Press {0} to interact"),
        ]
        table_blob = build_unity_localization_string_table_blob("UI_English", rows)
        parsed = extract_unity_localization_string_tables_from_bytes(table_blob)
        self.assertEqual(len(parsed), 1)
        self.assertIn(parsed[0][0], ("UI_English", "en"))
        self.assertEqual(len(parsed[0][1]), 4)

        bundle_bytes = build_unityfs_bundle_blob(
            [("CAB-loc123", b"\x00\x00\x00\x00" + table_blob)]
        )

        with tempfile.TemporaryDirectory() as tmp:
            unity_dir = os.path.join(tmp, "AddressablesLocGame")
            managed = os.path.join(unity_dir, "MyGame_Data", "Managed")
            aa_dir = os.path.join(unity_dir, "MyGame_Data", "StreamingAssets", "aa")
            aa_win = os.path.join(aa_dir, "StandaloneWindows64")
            os.makedirs(managed)
            os.makedirs(aa_win)
            with open(os.path.join(unity_dir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")
            with open(os.path.join(aa_dir, "catalog.json"), "w", encoding="utf-8") as f:
                f.write('{"m_LocatorId": "AddressablesMainContentCatalog", "m_InternalIds": ["a", "b", "c", "d"]}')
            bundle_path = os.path.join(aa_win, "localization-strings.bundle")
            with open(bundle_path, "wb") as f:
                f.write(bundle_bytes)

            eng = UnityDetector().detect(unity_dir)
            cands, _ = UnityLanguageScanner().scan(eng)
            self.assertTrue(cands, "Expected Addressables .bundle candidate")
            self.assertFalse(any("catalog.json" in c.relative_path for c in cands))
            self.assertEqual(cands[0].format_name, "unity_assets")
            self.assertIn("localization-strings.bundle", cands[0].relative_path)

            blocks = extract_assets_localization_blocks(bundle_path)
            self.assertTrue(any(b.block_kind == "UnityLocalization:binary" for b in blocks))


class DefaultArabicFontAndUpdateTests(unittest.TestCase):
    def test_resolve_default_ibm_plex_sans_arabic_and_invalid_custom_fallback(self):
        default_font = resolve_arabic_font_path()
        self.assertIsNotNone(default_font)
        self.assertTrue(os.path.isfile(default_font))
        self.assertIn("IBMPlexSansArabic", os.path.basename(default_font))

        with tempfile.TemporaryDirectory() as tmp:
            bad_font = os.path.join(tmp, "tiny.ttf")
            with open(bad_font, "wb") as f:
                f.write(b"BAD")
            resolved = resolve_arabic_font_path(bad_font)
            self.assertEqual(resolved, default_font)

    def test_update_font_on_already_translated_mono_game_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = os.path.join(tmp, "AlreadyTranslatedMono")
            managed = os.path.join(game_dir, "MyGame_Data", "Managed")
            lang_dir = os.path.join(game_dir, "MyGame_Data", "StreamingAssets")
            os.makedirs(managed)
            os.makedirs(lang_dir)
            with open(os.path.join(game_dir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")

            en_json = os.path.join(lang_dir, "English.json")
            with open(en_json, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "play": "Start New Adventure",
                        "settings": "Open Video and Audio Settings",
                        "menu": "Return to Main Menu",
                        "save": "Save Current Progress Now",
                        "load": "Load Previous Checkpoint",
                        "quit": "Exit to Desktop",
                    },
                    f,
                )

            def fake_ep(ep, text, src_lang, tgt_lang):
                return text

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            # محاكاة لعبة معرّبة مسبقاً مع skip_inject=True (بدون الخط المدمج)
            rep1 = pipe.translate(game_dir, skip_inject=True)
            self.assertTrue(rep1.success)
            font_dst = os.path.join(game_dir, "ArabicFont.ttf")
            self.assertFalse(os.path.exists(font_dst))
            translated_sha = compute_sha256(en_json)

            def fake_compile(self_inj, engine, output_dll_path, include_input_utils=False):
                os.makedirs(os.path.dirname(os.path.abspath(output_dll_path)), exist_ok=True)
                with open(output_dll_path, "wb") as df:
                    df.write(b"MZ_UPDATED")
                return True, "ok"

            with patch.object(UnityInjector, "compile_injector", fake_compile):
                rep2 = pipe.update_font(game_dir)

            self.assertTrue(rep2.success, rep2.errors)
            self.assertTrue(os.path.isfile(font_dst))
            self.assertGreater(os.path.getsize(font_dst), 100000)
            self.assertEqual(compute_sha256(en_json), translated_sha)

            # استدعاء update_font مرة ثانية لا يكرر created_files في المانيفست
            with patch.object(UnityInjector, "compile_injector", fake_compile):
                rep3 = pipe.update_font(game_dir)
            self.assertTrue(rep3.success)
            manifest = BackupManager.load_manifest(game_dir)
            created = manifest.get("created_files", [])
            self.assertEqual(len(created), len(set(created)))

            restored, warnings = AutoTranslatorPipeline.rollback(game_dir)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(font_dst))
            self.assertTrue(any("English.json" in r for r in restored))


class EdgeCasesAndResilienceTests(unittest.TestCase):
    def test_corrupt_compressed_and_base64_payloads_do_not_crash(self):
        bad_payloads = [
            b"\x1f\x8b\x08\x00truncated_gzip",
            b"\x78\x9c\x00\x01corrupt_zlib",
            base64.b64encode(b"\x1f\x8b\x08bad_nested_gzip"),
            b"NotBase64!@#$%^&*()_+",
            b"\x00\x01\x02\x03",
        ]
        for payload in bad_payloads:
            self.assertIsNone(decode_compressed_or_encoded_payload(payload))

        with tempfile.TemporaryDirectory() as tmp:
            bad_bytes = os.path.join(tmp, "Corrupt.bytes")
            with open(bad_bytes, "wb") as f:
                f.write(b"\x1f\x8b\x08\x00\xff\xff\x00")
            self.assertEqual(extract_assets_localization_blocks(bad_bytes), [])

    def test_nested_rich_text_tags_and_placeholders_with_shaping(self):
        src = "<color=#FF0000><b>Hello {0}, HP: {1:P0}</b></color>"
        masked = mask_line(src)
        simulated_ar = masked.masked.replace("Hello", "مرحبا").replace("HP:", "الصحة:")
        unmasked, _ = unmask_line(
            simulated_ar, masked.placeholders, masked.lead_ws, masked.trail_ws
        )
        issues = verify_placeholders(masked.placeholders, unmasked)
        self.assertEqual(issues, [])
        shaped = process_text(unmasked)
        self.assertIn("<color=#FF0000>", shaped)
        self.assertIn("</color>", shaped)
        self.assertIn("{0}", shaped)
        self.assertIn("{1:P0}", shaped)

    def test_rollback_cleans_runtime_logs_and_captured_texts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "ArabicOverride.json")
            with open(target, "w", encoding="utf-8") as f:
                f.write("{}")
            BackupManager.register_created_file(tmp, target)
            BackupManager.register_created_file(tmp, target)
            manifest = BackupManager.load_manifest(tmp)
            self.assertEqual(len(manifest.get("created_files", [])), 1)

            for fname in ("arabic_font_log.txt", "arabic_translator_log.txt", "arabic_captured_texts.txt"):
                with open(os.path.join(tmp, fname), "w", encoding="utf-8") as f:
                    f.write("sample")

            _, warnings = BackupManager.rollback(tmp)
            self.assertEqual(warnings, [])
            for fname in ("ArabicOverride.json", "arabic_font_log.txt", "arabic_translator_log.txt", "arabic_captured_texts.txt"):
                self.assertFalse(os.path.exists(os.path.join(tmp, fname)))


class EngineRegistryAndMultiEngineTests(unittest.TestCase):
    def test_registry_selects_custom_rtl_multi_file_engine_and_update_font_unsupported(self):
        from core.models import CandidateFile, EngineInfo, InjectionResult
        from engines.base import GameEngineAdapter
        from engines.unity.adapter import UnityAdapter

        class MockRtlEngineAdapter(GameEngineAdapter):
            def detect(self, game_dir: str) -> EngineInfo:
                marker = os.path.join(game_dir, "mock_engine.cfg")
                if os.path.isfile(marker):
                    return EngineInfo(
                        engine_name="MockRTL",
                        supported=True,
                        game_dir=game_dir,
                        architecture="x64",
                        scripting_backend="GDScript",
                        requires_arabic_shaping=False,
                    )
                return EngineInfo(
                    engine_name="MockRTL",
                    supported=False,
                    game_dir=game_dir,
                    unsupported_reason="Not MockRTL",
                )

            def scan_language_files(self, engine: EngineInfo):
                cands = []
                for fname in ("ui.json", "dialogue.json"):
                    fpath = os.path.join(engine.game_dir, fname)
                    cands.append(
                        CandidateFile(
                            path=fpath,
                            relative_path=fname,
                            format_name="json",
                            score=90,
                            total_entries=2,
                            translatable_entries=2,
                        )
                    )
                return cands, []

            def select_language_files(self, engine, candidates, min_score: int = 45):
                return [c for c in candidates if c.score >= min_score]

            def is_translatable_value(self, key: str, value: str) -> bool:
                return bool(value.strip())

            def inject(self, engine, language_file, skip_runtime_font: bool = False):
                return InjectionResult(success=True, method="mock_rtl_native", message="ok")

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "mock_engine.cfg"), "w", encoding="utf-8") as f:
                f.write("mock=1")
            with open(os.path.join(tmp, "ui.json"), "w", encoding="utf-8") as f:
                json.dump({"start": "Start Game"}, f)
            with open(os.path.join(tmp, "dialogue.json"), "w", encoding="utf-8") as f:
                json.dump({"hello": "Hello World"}, f)

            def fake_ep(text, ep, src_lang, tgt_lang):
                return text.replace("Start Game", "ابدأ اللعبة").replace("Hello World", "مرحبا بالعالم")

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep),
                adapters=[UnityAdapter(), MockRtlEngineAdapter()],
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            self.assertEqual(rep.engine.engine_name, "MockRTL")
            self.assertEqual(len(rep.selected_files), 2)
            self.assertEqual(rep.stats.translatable_keys, 2)

            # requires_arabic_shaping=False يجب أن يحافظ على الحروف المنطقية بدون تشبيك Presentation Forms-B
            with open(os.path.join(tmp, "ui.json"), "r", encoding="utf-8") as f:
                ui_ar = json.load(f)["start"]
            self.assertEqual(ui_ar, "ابدأ اللعبة")

            with open(os.path.join(tmp, "dialogue.json"), "r", encoding="utf-8") as f:
                dlg_ar = json.load(f)["hello"]
            self.assertEqual(dlg_ar, "مرحبا بالعالم")

            # update_font على محرك لا ينفذها يرجع رسالة واضحة، وعلى مجلد غير مدعوم لا يرمي AttributeError
            uf_rep = pipe.update_font(tmp)
            self.assertFalse(uf_rep.success)
            self.assertIn("MockRTL", uf_rep.errors[0])

            empty_dir = os.path.join(tmp, "empty_unsupported")
            os.makedirs(empty_dir)
            uf_empty = pipe.update_font(empty_dir)
            self.assertFalse(uf_empty.success)
            self.assertTrue(uf_empty.errors)


class RpgMakerMvMzTests(unittest.TestCase):
    def test_rpgmaker_escape_codes_protection(self):
        line = r"Welcome \N[1]! You have \V[10] \G and \C[2]\{Power\}\C[0]\. \| \! \< \> \^ \$ \P[1] \I[5]"
        masked = mask_line(line)
        self.assertEqual(len(masked.placeholders), 16)
        for token in (
            r"\N[1]", r"\V[10]", r"\G", r"\C[2]", r"\{", r"\}", r"\C[0]",
            r"\.", r"\|", r"\!", r"\<", r"\>", r"\^", r"\$", r"\P[1]", r"\I[5]",
        ):
            if token != r"\C[0]":  # C[2] and C[0] both captured
                self.assertIn(token, masked.placeholders)
        self.assertIn(r"\C[0]", masked.placeholders)

        translated_core = masked.masked.replace("Welcome", "أهلاً").replace("You have", "لديك").replace("Power", "قوة")
        unmasked, repairs = unmask_line(translated_core, masked.placeholders, masked.lead_ws, masked.trail_ws)
        self.assertEqual(repairs, 0)
        self.assertEqual(verify_placeholders(masked.placeholders, unmasked), [])
        self.assertIn(r"\N[1]", unmasked)
        self.assertIn(r"\V[10]", unmasked)
        self.assertIn(r"\G", unmasked)

    def test_rpgmaker_mv_end_to_end_multifile_font_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            mv_data = os.path.join(tmp, "www", "data")
            mv_fonts = os.path.join(tmp, "www", "fonts")
            os.makedirs(mv_data)
            os.makedirs(mv_fonts)

            with open(os.path.join(tmp, "Game.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(tmp, "www", "index.html"), "w", encoding="utf-8") as f:
                f.write("<html><body></body></html>")

            orig_css = "@font-face {\n    font-family: GameFont;\n    src: url('mplus-1m-regular.ttf');\n}\n"
            css_path = os.path.join(mv_fonts, "gamefont.css")
            with open(css_path, "w", encoding="utf-8") as f:
                f.write(orig_css)

            system_data = {
                "gameTitle": "Heroic Quest",
                "currencyUnit": "Gold",
                "locale": "en_US",
                "switches": ["", "QuestStarted", "BossDefeated"],
                "variables": ["", "GoldCount"],
                "titleBgm": {"name": "Theme6", "pan": 0, "pitch": 100, "volume": 90},
                "sounds": [{"name": "Cursor2", "pan": 0, "pitch": 100, "volume": 90}],
                "terms": {
                    "commands": ["Fight", "Escape"],
                    "messages": {"victory": "%1 was victorious!"},
                },
            }
            actors_data = [
                None,
                {
                    "id": 1,
                    "name": "Harold",
                    "nickname": "The Brave",
                    "profile": "A young knight from the northern village.",
                    "note": "<Passive State: 5>",
                    "characterName": "Actor1",
                    "faceName": "Actor1",
                },
            ]
            items_data = [
                None,
                {
                    "id": 1,
                    "name": "Potion",
                    "description": "Restores 50 HP to one ally.",
                    "note": "<Custom Effect>",
                    "damage": {"formula": "a.atk * 4 - b.def * 2"},
                },
            ]
            map001_data = {
                "displayName": "Starting Village",
                "bgm": {"name": "Town1", "pan": 0, "pitch": 100, "volume": 90},
                "note": "<NoSave>",
                "events": [
                    None,
                    {
                        "id": 1,
                        "name": "EV001_Guide",
                        "pages": [
                            {
                                "list": [
                                    {"code": 101, "indent": 0, "parameters": ["People", 0, 0, 2]},
                                    {"code": 401, "indent": 0, "parameters": [r"Hello \N[1]! Welcome to \C[2]\{our town\}\C[0]\."]},
                                    {"code": 102, "indent": 0, "parameters": [["Enter Inn", "Leave"], 1, 0, 2, 0]},
                                    {"code": 108, "indent": 0, "parameters": ["Light 150 white"]},
                                    {"code": 231, "indent": 0, "parameters": [1, "World Map", 0, 0, 0, 0, 100, 100, 255, 0]},
                                    {"code": 283, "indent": 0, "parameters": ["Grassland", "Cobblestones"]},
                                    {"code": 284, "indent": 0, "parameters": ["BlueSky", True, False, 0, 0]},
                                    {"code": 322, "indent": 0, "parameters": [1, "HeroChar", 0, "HeroFace", 0, "HeroBattler"]},
                                    {"code": 355, "indent": 0, "parameters": ["$gameActors.actor(1).gainExp(100);"]},
                                    {"code": 356, "indent": 0, "parameters": ["QuestJournal open"]},
                                    {"code": 241, "indent": 0, "parameters": [{"name": "Town_Night", "pan": 0, "pitch": 100, "volume": 90}]},
                                ]
                            }
                        ],
                    },
                ],
            }
            common_events_data = [
                None,
                {
                    "id": 1,
                    "name": "InitEvent",
                    "list": [
                        {"code": 401, "indent": 0, "parameters": ["Your journey begins now."]},
                    ],
                },
            ]

            tilesets_data = [
                None,
                {"id": 1, "name": "Outside", "note": "<Tileset>", "tilesetNames": ["World_A1", "World_A2"]},
            ]
            files_map = {
                "System.json": system_data,
                "Actors.json": actors_data,
                "Items.json": items_data,
                "Map001.json": map001_data,
                "CommonEvents.json": common_events_data,
                "Tilesets.json": tilesets_data,
            }
            orig_contents = {}
            for fname, payload in files_map.items():
                p = os.path.join(mv_data, fname)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                with open(p, "r", encoding="utf-8") as f:
                    orig_contents[p] = f.read()

            def fake_ep(batch_text, ep, sl, tl):
                return "\n".join("ع:" + ln for ln in batch_text.splitlines())

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            self.assertEqual(rep.engine.engine_name, "RPG Maker MV")
            self.assertEqual(rep.engine.scripting_backend, "MV")
            self.assertFalse(rep.engine.requires_arabic_shaping)
            self.assertEqual(len(rep.selected_files), 5)

            # التحقق من ترجمة النصوص البشرية وبقاء الحقول التقنية سليمة
            with open(os.path.join(mv_data, "System.json"), "r", encoding="utf-8") as f:
                sys_ar = json.load(f)
            self.assertTrue(sys_ar["gameTitle"].startswith("ع:"))
            self.assertEqual(sys_ar["locale"], "en_US")
            self.assertEqual(sys_ar["titleBgm"]["name"], "Theme6")
            self.assertEqual(sys_ar["switches"][1], "QuestStarted")
            self.assertIn("%1", sys_ar["terms"]["messages"]["victory"])
            self.assertTrue(sys_ar["terms"]["messages"]["victory"].startswith("ع:"))

            with open(os.path.join(mv_data, "Tilesets.json"), "r", encoding="utf-8") as f:
                ts_ar = json.load(f)
            self.assertEqual(ts_ar[1]["name"], "Outside")

            with open(os.path.join(mv_data, "Actors.json"), "r", encoding="utf-8") as f:
                act_ar = json.load(f)
            self.assertTrue(act_ar[1]["name"].startswith("ع:"))
            self.assertEqual(act_ar[1]["note"], "<Passive State: 5>")
            self.assertEqual(act_ar[1]["faceName"], "Actor1")

            with open(os.path.join(mv_data, "Map001.json"), "r", encoding="utf-8") as f:
                map_ar = json.load(f)
            self.assertTrue(map_ar["displayName"].startswith("ع:"))
            self.assertEqual(map_ar["events"][1]["name"], "EV001_Guide")
            ev_list = map_ar["events"][1]["pages"][0]["list"]
            # code 101 faceName ("People") بدون أرقام يجب ألا يُترجم أبداً
            self.assertEqual(ev_list[0]["parameters"][0], "People")
            dlg = ev_list[1]["parameters"][0]
            self.assertTrue(dlg.startswith("ع:"))
            self.assertIn(r"\N[1]", dlg)
            self.assertIn(r"\C[2]", dlg)
            self.assertIn(r"\{", dlg)
            self.assertTrue(ev_list[2]["parameters"][0][0].startswith("ع:"))
            # code 108 comment, 231 picture, 283 battleback, 284 parallax, 322 images, 355 script, 356 plugin cmd, 241 bgm
            self.assertEqual(ev_list[3]["parameters"][0], "Light 150 white")
            self.assertEqual(ev_list[4]["parameters"][1], "World Map")
            self.assertEqual(ev_list[5]["parameters"], ["Grassland", "Cobblestones"])
            self.assertEqual(ev_list[6]["parameters"][0], "BlueSky")
            self.assertEqual(ev_list[7]["parameters"][1], "HeroChar")
            self.assertEqual(ev_list[7]["parameters"][3], "HeroFace")
            self.assertEqual(ev_list[7]["parameters"][5], "HeroBattler")
            self.assertEqual(ev_list[8]["parameters"][0], "$gameActors.actor(1).gainExp(100);")
            self.assertEqual(ev_list[9]["parameters"][0], "QuestJournal open")
            self.assertEqual(ev_list[10]["parameters"][0]["name"], "Town_Night")

            # التحقق من حقن الخط و CSS
            font_dst = os.path.join(mv_fonts, "IBMPlexSansArabic-Regular.ttf")
            self.assertTrue(os.path.isfile(font_dst))
            with open(css_path, "r", encoding="utf-8") as f:
                new_css = f.read()
            self.assertIn("IBMPlexSansArabic-Regular.ttf", new_css)
            self.assertIn("direction: rtl;", new_css)

            # التحقق من Rollback الكامل
            restored, warnings = AutoTranslatorPipeline.rollback(tmp)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(font_dst))
            with open(css_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), orig_css)
            for p, content in orig_contents.items():
                with open(p, "r", encoding="utf-8") as f:
                    self.assertEqual(f.read(), content)

    def test_rpgmaker_mz_end_to_end_and_update_font(self):
        with tempfile.TemporaryDirectory() as tmp:
            mz_data = os.path.join(tmp, "data")
            mz_fonts = os.path.join(tmp, "fonts")
            mz_css = os.path.join(tmp, "css")
            os.makedirs(mz_data)
            os.makedirs(mz_fonts)
            os.makedirs(mz_css)

            with open(os.path.join(tmp, "Game.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(tmp, "index.html"), "w", encoding="utf-8") as f:
                f.write("<html></html>")
            orig_game_css = "body { margin: 0; }"
            game_css_path = os.path.join(mz_css, "game.css")
            with open(game_css_path, "w", encoding="utf-8") as f:
                f.write(orig_game_css)

            system_path = os.path.join(mz_data, "System.json")
            with open(system_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "gameTitle": "MZ Adventure",
                        "battleBgm": {"name": "Battle1"},
                        "advanced": {"mainFontFilename": "mplus-1m-regular.ttf", "fallbackFonts": "Verdana, sans-serif"},
                        "terms": {"commands": ["Start", "Options"]},
                    },
                    f,
                )
            map001_path = os.path.join(mz_data, "Map001.json")
            with open(map001_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "displayName": "Crystal Cavern",
                        "events": [
                            None,
                            {
                                "id": 1,
                                "name": "EV001",
                                "pages": [
                                    {
                                        "list": [
                                            {"code": 101, "indent": 0, "parameters": ["People", 0, 0, 2, "Elder Rowan"]},
                                            {"code": 401, "indent": 0, "parameters": ["The crystal awaits you."]},
                                            {"code": 405, "indent": 0, "parameters": ["Chapter One: Dawn"]},
                                            {"code": 357, "indent": 0, "parameters": ["VisuMZ_Message", "OpenLog", "Open Log", {"mode": "normal"}]},
                                        ]
                                    }
                                ],
                            },
                        ],
                    },
                    f,
                )

            def fake_ep(batch_text, ep, sl, tl):
                return "\n".join("ع:" + ln for ln in batch_text.splitlines())

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            self.assertEqual(rep.engine.engine_name, "RPG Maker MZ")
            self.assertEqual(rep.engine.scripting_backend, "MZ")

            with open(system_path, "r", encoding="utf-8") as f:
                sys_mz = json.load(f)
            self.assertEqual(sys_mz["battleBgm"]["name"], "Battle1")
            self.assertEqual(sys_mz["advanced"]["fallbackFonts"], "Verdana, sans-serif")

            with open(map001_path, "r", encoding="utf-8") as f:
                map_mz = json.load(f)
            ev_mz = map_mz["events"][1]["pages"][0]["list"]
            self.assertEqual(ev_mz[0]["parameters"][0], "People")
            self.assertTrue(ev_mz[0]["parameters"][4].startswith("ع:"))
            self.assertTrue(ev_mz[1]["parameters"][0].startswith("ع:"))
            self.assertTrue(ev_mz[2]["parameters"][0].startswith("ع:"))
            self.assertEqual(ev_mz[3]["parameters"][0], "VisuMZ_Message")
            self.assertEqual(ev_mz[3]["parameters"][2], "Open Log")

            font_dst = os.path.join(mz_fonts, "IBMPlexSansArabic-Regular.ttf")
            css_path = os.path.join(mz_fonts, "gamefont.css")
            self.assertTrue(os.path.isfile(font_dst))
            self.assertTrue(os.path.isfile(css_path))
            with open(game_css_path, "r", encoding="utf-8") as f:
                game_css_txt = f.read()
            self.assertIn("../fonts/IBMPlexSansArabic-Regular.ttf", game_css_txt)
            self.assertIn("direction: rtl;", game_css_txt)

            # اختبار update_font المستقل بدون تكرار كتلة CSS
            uf_rep = pipe.update_font(tmp)
            self.assertTrue(uf_rep.success, uf_rep.errors)
            with open(css_path, "r", encoding="utf-8") as f:
                css_text = f.read()
            self.assertEqual(css_text.count("AutoTranslator Arabic Font & RTL START"), 1)
            with open(game_css_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read().count("AutoTranslator Arabic Font & RTL START"), 1)

            restored, warnings = AutoTranslatorPipeline.rollback(tmp)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(font_dst))
            self.assertFalse(os.path.exists(css_path))
            with open(game_css_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), orig_game_css)


def _make_minimal_x86_pe() -> bytes:
    """بناء ترويسة PE صالحة لمعمارية x86 للاختبارات."""
    buf = bytearray(256)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, 0x80)
    buf[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", buf, 0x84, 0x014C)
    return bytes(buf)


class Godot3And4Tests(unittest.TestCase):
    def test_godot_bbcode_and_named_placeholders_protection_and_shaping(self):
        line = "[color=#FFD700][b]Welcome, {player_name}! Score: {score:0.0} ([url=https://example.com]Info[/url])[/b][/color]"
        masked = mask_line(line)
        for token in (
            "[color=#FFD700]",
            "[b]",
            "{player_name}",
            "{score:0.0}",
            "[url=https://example.com]",
            "[/url]",
            "[/b]",
            "[/color]",
        ):
            self.assertIn(token, masked.placeholders)

        translated = (
            masked.masked
            .replace("Welcome", "أهلاً")
            .replace("Score:", "النتيجة:")
            .replace("Info", "معلومات")
        )
        unmasked, repairs = unmask_line(
            translated, masked.placeholders, masked.lead_ws, masked.trail_ws
        )
        self.assertEqual(repairs, 0)
        self.assertEqual(verify_placeholders(masked.placeholders, unmasked), [])

        # تشبيك Godot 3 يحافظ على BBCode والمتغيرات المسماة دون عكس الأقواس
        shaped = process_text(unmasked)
        self.assertIn("[color=#FFD700]", shaped)
        self.assertIn("[/color]", shaped)
        self.assertIn("{player_name}", shaped)
        self.assertIn("{score:0.0}", shaped)
        self.assertIn("[url=https://example.com]", shaped)
        self.assertIn("[/url]", shaped)
        self.assertRegex(shaped, r"[\uFE70-\uFEFF]")

    def test_godot_translatability_filter_and_project_godot_detection(self):
        from engines.godot.detector import GodotDetector
        from engines.godot.scanner import is_translatable_value as godot_translatable

        self.assertFalse(godot_translatable("k1", "res://scenes/main.tscn"))
        self.assertFalse(godot_translatable("k2", "user://save_01.dat"))
        self.assertFalse(godot_translatable("k3", "ui_accept"))
        self.assertFalse(godot_translatable("k4", "/root/Main/HUD/Label"))
        self.assertFalse(godot_translatable("k5", "../UI/HealthBar"))
        self.assertFalse(godot_translatable("k6", "%PlayerStats"))
        self.assertFalse(godot_translatable("k7", "$AnimatedSprite2D"))
        self.assertFalse(godot_translatable("k8", "_on_start_button_pressed"))
        self.assertFalse(godot_translatable("k9", "player_walk_01"))
        self.assertFalse(godot_translatable("node.node_path", "MainMenu"))
        self.assertFalse(godot_translatable("k10", "[b]{player_name}[/b]"))

        self.assertTrue(godot_translatable("MENU_PLAY", "Play"))
        self.assertTrue(godot_translatable("GREETING", "[color=#FF0]Hello, {player_name}![/color]"))

        detector = GodotDetector()
        with tempfile.TemporaryDirectory() as tmp:
            g3_dir = os.path.join(tmp, "Godot3Proj")
            g4_dir = os.path.join(tmp, "Godot4Proj")
            os.makedirs(g3_dir)
            os.makedirs(g4_dir)
            with open(os.path.join(g3_dir, "project.godot"), "w", encoding="utf-8") as f:
                f.write('config_version=4\n[application]\nconfig/name="G3"\n')
            with open(os.path.join(g4_dir, "project.godot"), "w", encoding="utf-8") as f:
                f.write('config_version=5\n[application]\nconfig/name="G4"\n')

            info3 = detector.detect(g3_dir)
            info4 = detector.detect(g4_dir)
            self.assertTrue(info3.supported)
            self.assertEqual(info3.engine_name, "Godot 3")
            self.assertTrue(info3.requires_arabic_shaping)

            self.assertTrue(info4.supported)
            self.assertEqual(info4.engine_name, "Godot 4")
            self.assertFalse(info4.requires_arabic_shaping)

    def test_godot3_standalone_pck_v1_csv_po_shaping_override_update_font_and_rollback(self):
        from engines.godot.pck_parser import build_godot_pck_blob, parse_pck_header_and_entries

        with tempfile.TemporaryDirectory() as tmp:
            exe_path = os.path.join(tmp, "MyGodot3Game.exe")
            pck_path = os.path.join(tmp, "MyGodot3Game.pck")
            with open(exe_path, "wb") as f:
                f.write(_make_minimal_x86_pe())

            csv_bytes = (
                "keys,en,fr\n"
                'UI_WELCOME,"[b]Welcome, {player_name}![/b]","[b]Bienvenue, {player_name}![/b]"\n'
                'UI_PLAY,"Play Game","Jouer"\n'
                'TECH_RES,"res://scenes/world.tscn","res://scenes/world.tscn"\n'
                'TECH_INPUT,"ui_accept","ui_accept"\n'
                'TECH_NODE,"/root/Main/HUD","/root/Main/HUD"\n'
            ).encode("utf-8")

            po_en_bytes = (
                'msgid ""\n'
                'msgstr ""\n'
                '"Content-Type: text/plain; charset=UTF-8\\n"\n'
                '"Language: en\\n"\n\n'
                'msgid "DLG_QUEST"\n'
                'msgstr "[color=#00FF00]Find the ancient key.[/color]"\n\n'
                'msgid "DLG_LINK"\n'
                'msgstr "Visit [url=https://example.org]Journal[/url] for {quest_count} hints."\n'
            ).encode("utf-8")

            po_fr_bytes = (
                'msgid ""\n'
                'msgstr ""\n'
                '"Language: fr\\n"\n\n'
                'msgid "DLG_QUEST"\n'
                'msgstr "Trouvez la clé ancienne."\n'
            ).encode("utf-8")

            pck_blob = build_godot_pck_blob(
                {
                    "res://locale/strings.csv": csv_bytes,
                    "res://translations/dialogue.en.po": po_en_bytes,
                    "res://translations/dialogue.fr.po": po_fr_bytes,
                },
                pack_format_version=1,
                godot_version=(3, 5, 2),
            )
            with open(pck_path, "wb") as f:
                f.write(pck_blob)
            orig_pck_sha = compute_sha256(pck_path)

            orig_override = "[display]\nwindow/size/width=1280\n"
            override_path = os.path.join(tmp, "override.cfg")
            with open(override_path, "w", encoding="utf-8") as f:
                f.write(orig_override)

            def fake_ep(batch_text, ep, sl, tl):
                lines = []
                for ln in batch_text.splitlines():
                    out = (
                        ln.replace("Welcome", "أهلاً")
                        .replace("Play Game", "ابدأ اللعبة")
                        .replace("Find the ancient key.", "ابحث عن المفتاح القديم.")
                        .replace("Visit", "زر")
                        .replace("Journal", "المفكرة")
                        .replace("for", "لرؤية")
                        .replace("hints.", "تلميحات.")
                    )
                    lines.append(out)
                return "\n".join(lines)

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            self.assertEqual(rep.engine.engine_name, "Godot 3")
            self.assertEqual(rep.engine.architecture, "x86")
            self.assertTrue(rep.engine.requires_arabic_shaping)
            self.assertEqual(len(rep.selected_files), 2)
            selected_rels = {c.relative_path for c in rep.selected_files}
            self.assertTrue(any("strings.csv" in r for r in selected_rels))
            self.assertTrue(any("dialogue.en.po" in r for r in selected_rels))
            self.assertFalse(any("dialogue.fr.po" in r for r in selected_rels))

            # بصمة .pck الأصلية لم تتغير إطلاقاً
            self.assertEqual(compute_sha256(pck_path), orig_pck_sha)

            # التحقق من الملفات المستخرجة والمعربة في arabic_l10n/ وتطبيق التشبيك (Godot 3)
            out_csv = os.path.join(tmp, "arabic_l10n", "locale", "strings.csv")
            out_po = os.path.join(tmp, "arabic_l10n", "translations", "dialogue.ar.po")
            self.assertTrue(os.path.isfile(out_csv))
            self.assertTrue(os.path.isfile(out_po))

            with open(out_csv, "r", encoding="utf-8") as f:
                csv_text = f.read()
            self.assertIn("{player_name}", csv_text)
            self.assertIn("[b]", csv_text)
            self.assertIn("[/b]", csv_text)
            self.assertRegex(csv_text, r"[\uFE70-\uFEFF]")
            self.assertIn("res://scenes/world.tscn", csv_text)
            self.assertIn("ui_accept", csv_text)

            with open(out_po, "r", encoding="utf-8") as f:
                po_text = f.read()
            self.assertIn("Language: ar", po_text)
            self.assertIn("[color=#00FF00]", po_text)
            self.assertIn("[/color]", po_text)
            self.assertIn("{quest_count}", po_text)
            self.assertRegex(po_text, r"[\uFE70-\uFEFF]")

            # التحقق من الخط و .tres و override.cfg و arabic_patch.pck
            font_dst = os.path.join(tmp, "arabic_l10n", "IBMPlexSansArabic-Regular.ttf")
            tres_dst = os.path.join(tmp, "arabic_l10n", "arabic_font.tres")
            patch_pck = os.path.join(tmp, "arabic_l10n", "arabic_patch.pck")
            self.assertTrue(os.path.isfile(font_dst))
            self.assertTrue(os.path.isfile(tres_dst))
            self.assertTrue(os.path.isfile(patch_pck))

            patch_info = parse_pck_header_and_entries(patch_pck)
            self.assertIsNotNone(patch_info)
            self.assertEqual(patch_info.pack_format_version, 1)

            with open(override_path, "r", encoding="utf-8") as f:
                cfg_txt = f.read()
            self.assertIn("window/size/width=1280", cfg_txt)
            self.assertIn('test="ar"', cfg_txt)
            self.assertIn("PoolStringArray(", cfg_txt)
            self.assertIn('theme/custom_font="res://arabic_l10n/arabic_font.tres"', cfg_txt)

            # update_font المستقل لا يكرر الأقسام ويحافظ على إعدادات العرض والترجمة
            uf_rep = pipe.update_font(tmp)
            self.assertTrue(uf_rep.success, uf_rep.errors)
            with open(override_path, "r", encoding="utf-8") as f:
                cfg_after_uf = f.read()
            self.assertEqual(cfg_after_uf.count("[gui]"), 1)
            self.assertEqual(cfg_after_uf.count("[locale]"), 1)
            self.assertIn("window/size/width=1280", cfg_after_uf)

            # Rollback الكامل
            _, warnings = AutoTranslatorPipeline.rollback(tmp)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(font_dst))
            self.assertFalse(os.path.exists(tres_dst))
            self.assertFalse(os.path.exists(out_csv))
            self.assertFalse(os.path.exists(out_po))
            self.assertFalse(os.path.exists(patch_pck))
            with open(override_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), orig_override)
            self.assertEqual(compute_sha256(pck_path), orig_pck_sha)

    def test_godot4_embedded_exe_pck_v2_translation_rsrc_and_po_no_shaping_rollback(self):
        from engines.godot.pck_parser import (
            build_godot_pck_blob,
            build_godot_translation_rsrc_blob,
            parse_pck_header_and_entries,
        )

        with tempfile.TemporaryDirectory() as tmp:
            exe_path = os.path.join(tmp, "Godot4Adventure.exe")
            proj_txt = (
                'config_version=5\n'
                '[application]\n'
                'config/name="Godot 4 Adventure"\n'
                '[internationalization]\n'
                'locale/translations=PackedStringArray("res://translations/ui.en.translation")\n'
            ).encode("utf-8")

            rsrc_trans = build_godot_translation_rsrc_blob(
                locale="en",
                messages={
                    "MENU_START": "Start New Journey",
                    "MENU_QUIT": "Exit to Desktop",
                    "PATH_REF": "res://scenes/menu.tscn",
                },
                optimized=False,
                godot_version=(4, 2, 1),
            )
            opt_trans = build_godot_translation_rsrc_blob(
                locale="en",
                messages={
                    "OPT_1": "Press [b]Space[/b] to jump, {hero_name}.",
                },
                optimized=True,
                godot_version=(4, 2, 1),
            )
            po_story = (
                'msgid ""\n'
                'msgstr ""\n'
                '"Content-Type: text/plain; charset=UTF-8\\n"\n'
                '"Language: en\\n"\n\n'
                'msgid "STORY_1"\n'
                'msgstr "[wave amp=50 freq=2]The stars align for {hero_name}.[/wave]"\n'
            ).encode("utf-8")

            embedded_exe = build_godot_pck_blob(
                {
                    "res://project.godot": proj_txt,
                    "res://translations/ui.en.translation": rsrc_trans,
                    "res://translations/hints.en.translation": opt_trans,
                    "res://translations/story.en.po": po_story,
                },
                pack_format_version=2,
                godot_version=(4, 2, 1),
                embedded_exe_prefix=_make_minimal_x64_pe() + (b"\x90" * 512),
            )
            with open(exe_path, "wb") as f:
                f.write(embedded_exe)
            orig_exe_sha = compute_sha256(exe_path)

            def fake_ep(batch_text, ep, sl, tl):
                lines = []
                for ln in batch_text.splitlines():
                    out = (
                        ln.replace("Start New Journey", "ابدأ رحلة جديدة")
                        .replace("Exit to Desktop", "الخروج إلى سطح المكتب")
                        .replace("Press", "اضغط")
                        .replace("Space", "مسافة")
                        .replace("to jump,", "للقفز،")
                        .replace("The stars align for", "تتراصف النجوم لأجل")
                    )
                    lines.append(out)
                return "\n".join(lines)

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            self.assertEqual(rep.engine.engine_name, "Godot 4")
            self.assertEqual(rep.engine.architecture, "x64")
            self.assertFalse(rep.engine.requires_arabic_shaping)
            self.assertEqual(len(rep.selected_files), 3)

            # بصمة .exe المدمج لم تتغير إطلاقاً
            self.assertEqual(compute_sha256(exe_path), orig_exe_sha)

            ui_ar_po = os.path.join(tmp, "arabic_l10n", "translations", "ui.ar.po")
            hints_ar_po = os.path.join(tmp, "arabic_l10n", "translations", "hints.ar.po")
            story_ar_po = os.path.join(tmp, "arabic_l10n", "translations", "story.ar.po")
            self.assertTrue(os.path.isfile(ui_ar_po))
            self.assertTrue(os.path.isfile(hints_ar_po))
            self.assertTrue(os.path.isfile(story_ar_po))

            # Godot 4 يحافظ على الحروف العربية المنطقية بدون Presentation Forms-B
            with open(ui_ar_po, "r", encoding="utf-8") as f:
                ui_txt = f.read()
            self.assertIn("ابدأ رحلة جديدة", ui_txt)
            self.assertNotRegex(ui_txt, r"[\uFE70-\uFEFF]")
            self.assertNotIn("res://scenes/menu.tscn", ui_txt)

            with open(hints_ar_po, "r", encoding="utf-8") as f:
                hints_txt = f.read()
            self.assertIn("[b]", hints_txt)
            self.assertIn("[/b]", hints_txt)
            self.assertIn("{hero_name}", hints_txt)

            with open(story_ar_po, "r", encoding="utf-8") as f:
                story_txt = f.read()
            self.assertIn("[wave amp=50 freq=2]", story_txt)
            self.assertIn("[/wave]", story_txt)
            self.assertIn("{hero_name}", story_txt)
            self.assertIn("تتراصف النجوم لأجل", story_txt)

            override_path = os.path.join(tmp, "override.cfg")
            self.assertTrue(os.path.isfile(override_path))
            with open(override_path, "r", encoding="utf-8") as f:
                cfg_txt = f.read()
            self.assertIn("[internationalization]", cfg_txt)
            self.assertIn('locale/test="ar"', cfg_txt)
            self.assertIn('locale/fallback="en"', cfg_txt)
            self.assertIn("locale/translations=PackedStringArray(", cfg_txt)
            self.assertIn("res://arabic_l10n/translations/ui.ar.po", cfg_txt)
            self.assertIn(
                'theme/custom_font="res://arabic_l10n/IBMPlexSansArabic-Regular.ttf"',
                cfg_txt,
            )
            self.assertNotIn("[autoload]", cfg_txt)

            patch_pck = os.path.join(tmp, "arabic_l10n", "arabic_patch.pck")
            self.assertTrue(os.path.isfile(patch_pck))
            patch_info = parse_pck_header_and_entries(patch_pck)
            self.assertIsNotNone(patch_info)
            self.assertEqual(patch_info.pack_format_version, 2)

            # Rollback الكامل يحذف override.cfg و arabic_l10n/*
            _, warnings = AutoTranslatorPipeline.rollback(tmp)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(override_path))
            self.assertFalse(os.path.exists(ui_ar_po))
            self.assertFalse(os.path.exists(hints_ar_po))
            self.assertFalse(os.path.exists(story_ar_po))
            self.assertFalse(os.path.exists(patch_pck))
            self.assertEqual(compute_sha256(exe_path), orig_exe_sha)

    def test_godot_printf_ellipsis_custom_theme_autoload_and_encrypted_pck_fixes(self):
        from engines.godot.pck_parser import PCK_MAGIC, parse_pck_header_and_entries
        from engines.godot.scanner import is_translatable_value as godot_translatable

        # 1. التحقق من عدم رفض الكلمات المنتهية بثلاث نقاط ("Loading...") مع رفض المسارات المنقوطة التقنية
        self.assertTrue(godot_translatable("MSG_LOAD", "Loading..."))
        self.assertTrue(godot_translatable("MSG_WAIT", "Wait..."))
        self.assertFalse(godot_translatable("MSG_KEY", "ui.menu.start"))

        # 2. التحقق من حماية صيغ % في GDScript دون كسر النسب المئوية العادية ("50% discount")
        line_fmt = "HP: %03d / %.2f%% (%-10s) - 50% discount"
        masked = mask_line(line_fmt)
        for tok in ("%03d", "%.2f", "%%", "%-10s"):
            self.assertIn(tok, masked.placeholders)
        self.assertIn("discount", masked.masked)
        unmasked, repairs = unmask_line(
            masked.masked.replace("discount", "خصم"),
            masked.placeholders,
            masked.lead_ws,
            masked.trail_ws,
        )
        self.assertEqual(repairs, 0)
        self.assertEqual(verify_placeholders(masked.placeholders, unmasked), [])

        # 3. مشروع Godot 4 مع custom_theme + ملف خارجي + التحقق من توليد arabic_theme.tres و arabic_autoload.gd
        with tempfile.TemporaryDirectory() as tmp:
            tr_dir = os.path.join(tmp, "translations")
            os.makedirs(tr_dir)
            with open(os.path.join(tmp, "project.godot"), "w", encoding="utf-8") as f:
                f.write(
                    'config_version=5\n'
                    '[application]\n'
                    'config/name="CustomThemeGame"\n'
                    '[internationalization]\n'
                    'locale/translations=PackedStringArray("res://translations/en.po")\n'
                    '[gui]\n'
                    'theme/custom="res://themes/dark.tres"\n'
                )
            po_path = os.path.join(tr_dir, "en.po")
            with open(po_path, "w", encoding="utf-8") as f:
                f.write(
                    'msgid ""\n'
                    'msgstr ""\n'
                    '"Language: en\\n"\n\n'
                    'msgid "LOADING"\n'
                    'msgstr "Loading..."\n'
                )

            def fake_ep(batch_text, ep, sl, tl):
                return batch_text.replace("Loading...", "جاري التحميل...")

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            self.assertTrue(any("Custom Theme" in h for h in rep.dll_hints))

            with open(po_path, "r", encoding="utf-8") as f:
                self.assertIn("جاري التحميل...", f.read())

            theme_tres = os.path.join(tmp, "arabic_l10n", "arabic_theme.tres")
            autoload_gd = os.path.join(tmp, "arabic_l10n", "arabic_autoload.gd")
            override_path = os.path.join(tmp, "override.cfg")
            self.assertTrue(os.path.isfile(theme_tres))
            self.assertTrue(os.path.isfile(autoload_gd))
            with open(override_path, "r", encoding="utf-8") as f:
                cfg_txt = f.read()
            self.assertIn('theme/custom="res://arabic_l10n/arabic_theme.tres"', cfg_txt)
            self.assertIn('ArabicL10n="*res://arabic_l10n/arabic_autoload.gd"', cfg_txt)

            _, warnings = AutoTranslatorPipeline.rollback(tmp)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(theme_tres))
            self.assertFalse(os.path.exists(autoload_gd))
            self.assertFalse(os.path.exists(override_path))

        # 4. حزمة .pck مشفّرة الفهرس لا تنهار وتُبلغ بتحذير واضح
        with tempfile.TemporaryDirectory() as tmp:
            enc_pck = os.path.join(tmp, "Encrypted.pck")
            hdr = bytearray(96)
            struct.pack_into("<4sIIII", hdr, 0, PCK_MAGIC, 2, 4, 2, 0)
            struct.pack_into("<IQ", hdr, 20, 1, 96)  # flags=1 (PACK_DIR_ENCRYPTED)
            with open(enc_pck, "wb") as f:
                f.write(hdr)

            info = parse_pck_header_and_entries(enc_pck)
            self.assertIsNotNone(info)
            self.assertTrue(info.dir_encrypted)
            rep = AutoTranslatorPipeline().scan(tmp)
            self.assertTrue(rep.engine.supported)
            self.assertFalse(rep.success)
            self.assertTrue(any("PACK_DIR_ENCRYPTED" in h for h in rep.dll_hints))


class UnrealEngineTests(unittest.TestCase):
    @staticmethod
    def _build_pe_exe(path: str, x64: bool = True) -> None:
        buf = bytearray(256)
        buf[0:2] = b"MZ"
        struct.pack_into("<I", buf, 0x3C, 0x80)
        buf[0x80:0x84] = b"PE\x00\x00"
        struct.pack_into("<H", buf, 0x84, 0x8664 if x64 else 0x014C)
        with open(path, "wb") as f:
            f.write(buf)

    @staticmethod
    def _build_pak_v11_single_entry(internal_rel: str, payload: bytes, mount_point: str = "../../../") -> bytes:
        import hashlib
        from formats.locres_handler import write_fstring

        sha1 = hashlib.sha1(payload).digest()
        local_hdr = struct.pack("<qqqI", 0, len(payload), len(payload), 0) + sha1 + struct.pack("<BI", 0, 0)
        data_section = local_hdr + payload

        # EncodedPakEntry: offset=0 (32-bit), uncomp=len (32-bit), method=0, block_count=0
        flags = (1 << 31) | (1 << 30)
        encoded_entry = struct.pack("<III", flags, 0, len(payload))

        dir_part = os.path.dirname(internal_rel).replace("\\", "/")
        if dir_part:
            dir_part = "/" + dir_part.strip("/") + "/"
        else:
            dir_part = "/"
        file_part = os.path.basename(internal_rel)

        full_dir_bytes = (
            struct.pack("<I", 1)
            + write_fstring(dir_part)
            + struct.pack("<I", 1)
            + write_fstring(file_part)
            + struct.pack("<i", 0)
        )
        full_dir_offset = len(data_section)
        full_dir_hash = hashlib.sha1(full_dir_bytes).digest()

        index_offset = full_dir_offset + len(full_dir_bytes)
        index_bytes = (
            write_fstring(mount_point)
            + struct.pack("<iQ", 1, 0x123456789ABCDEF0)
            + struct.pack("<i", 0)  # bHasPathHashIndex = 0
            + struct.pack("<i", 1)  # bHasFullDirectoryIndex = 1
            + struct.pack("<qq", full_dir_offset, len(full_dir_bytes))
            + full_dir_hash
            + struct.pack("<i", len(encoded_entry))
            + encoded_entry
            + struct.pack("<i", 0)  # NonEncodedEntriesCount = 0
        )
        index_hash = hashlib.sha1(index_bytes).digest()

        footer = (
            b"\x00" * 16  # EncryptionKeyGuid
            + b"\x00"     # bEncryptedIndex = 0
            + struct.pack("<IIqq", 0x5A6F12E1, 11, index_offset, len(index_bytes))
            + index_hash
            + b"\x00" * (5 * 32)
        )
        return data_section + full_dir_bytes + index_bytes + footer

    def test_locres_all_versions_read_write_preserves_keys_hashes_and_shared_refs(self):
        from formats.locres_handler import (
            VERSION_COMPACT,
            VERSION_LEGACY,
            VERSION_OPTIMIZED,
            VERSION_OPTIMIZED_CITYHASH64,
            build_locres_bytes,
            parse_locres_bytes,
            serialize_locres_bytes,
        )

        items = [
            ("UI", "BtnPlay", "Play"),
            ("UI", "BtnStart", "Play"),  # نص مشترك لاختبار RefCount
            ("Dialog", "Greeting", "Hello {PlayerName}!"),
        ]
        hashes = [0x11111111, 0x22222222, 0x33333333]

        for ver in (VERSION_LEGACY, VERSION_COMPACT, VERSION_OPTIMIZED, VERSION_OPTIMIZED_CITYHASH64):
            blob = build_locres_bytes(items, version=ver, source_hashes=hashes)
            parsed = parse_locres_bytes(blob)
            self.assertEqual(parsed.version, ver)
            self.assertEqual(len(parsed.namespaces), 2)
            self.assertEqual(parsed.namespaces[0].entries[0].source_hash, 0x11111111)
            self.assertEqual(parsed.namespaces[0].entries[1].text, "Play")

            # ترجمة BtnPlay فقط مع إبقاء BtnStart بالإنجليزية للتأكد من عدم تداخل الجدول المشترك
            updated_blob = serialize_locres_bytes(
                parsed,
                replacements={
                    (0, 0): "العب",
                    (1, 0): "مرحباً {PlayerName}!",
                },
            )
            reparsed = parse_locres_bytes(updated_blob)
            self.assertEqual(reparsed.version, ver)
            self.assertEqual(reparsed.namespaces[0].entries[0].key, "BtnPlay")
            self.assertEqual(reparsed.namespaces[0].entries[0].text, "العب")
            self.assertEqual(reparsed.namespaces[0].entries[0].source_hash, 0x11111111)
            self.assertEqual(reparsed.namespaces[0].entries[1].text, "Play")
            self.assertEqual(reparsed.namespaces[1].entries[0].text, "مرحباً {PlayerName}!")
            self.assertEqual(reparsed.namespaces[1].entries[0].source_hash, 0x33333333)
            if ver >= VERSION_OPTIMIZED:
                self.assertEqual(reparsed.namespaces[0].name_hash, parsed.namespaces[0].name_hash)
                self.assertEqual(
                    reparsed.namespaces[1].entries[0].key_hash,
                    parsed.namespaces[1].entries[0].key_hash,
                )

    def test_unreal_detection_ue4_vs_ue5_pak_extraction_and_encrypted_iostore_warnings(self):
        from engines.unreal.detector import UnrealDetector
        from engines.unreal.pak_parser import (
            extract_pak_entry_by_path,
            parse_pak_header_and_entries,
        )
        from engines.unreal.scanner import is_translatable_value as ue_translatable
        from formats.locres_handler import build_locres_bytes, parse_locres_bytes

        # 1. فلتر قابلية الترجمة في Unreal
        self.assertTrue(ue_translatable("UI.Load", "Loading..."))
        self.assertTrue(ue_translatable("HUD.Prompt", "Press {Key} to interact"))
        self.assertTrue(ue_translatable("HUD.Warn", "<Bold>Warning:</> {PlayerName}"))
        self.assertFalse(ue_translatable("Asset.Path", "/Game/UI/Widgets/WBP_Main"))
        self.assertFalse(ue_translatable("BP.Class", "BP_PlayerCharacter_C"))
        self.assertFalse(ue_translatable("Tag.Id", "UI_Main_Menu"))
        self.assertFalse(ue_translatable("Tex.File", "T_Icon_Sword.uasset"))

        # 2. كشف UE4 (.uproject 4.27 + Win64 Shipping + pak v11) واستخراج .locres من v11 بدون تعديل الأصل
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "QuestUE4.uproject"), "w", encoding="utf-8") as f:
                json.dump({"FileVersion": 3, "EngineAssociation": "4.27"}, f)
            exe_path = os.path.join(tmp, "QuestUE4-Win64-Shipping.exe")
            self._build_pe_exe(exe_path, x64=True)
            paks_dir = os.path.join(tmp, "QuestUE4", "Content", "Paks")
            os.makedirs(paks_dir)

            locres_blob = build_locres_bytes([("Game", "Title", "Shadow Realm")], version=2)
            pak_v11 = os.path.join(paks_dir, "pakchunk0-WindowsNoEditor.pak")
            with open(pak_v11, "wb") as f:
                f.write(
                    self._build_pak_v11_single_entry(
                        "QuestUE4/Content/Localization/Game/en/Game.locres",
                        locres_blob,
                    )
                )
            pak_sha_before = compute_sha256(pak_v11)

            info = UnrealDetector().detect(tmp)
            self.assertTrue(info.supported)
            self.assertEqual(info.engine_name, "Unreal Engine")
            self.assertEqual(info.architecture, "x64")
            self.assertIn("UE4", info.scripting_backend)
            self.assertFalse(info.requires_arabic_shaping)

            parsed_pak = parse_pak_header_and_entries(pak_v11)
            self.assertIsNotNone(parsed_pak)
            self.assertEqual(parsed_pak.version, 11)
            extracted = extract_pak_entry_by_path(
                pak_v11, "QuestUE4/Content/Localization/Game/en/Game.locres"
            )
            self.assertIsNotNone(extracted)
            loc = parse_locres_bytes(extracted[2])
            self.assertEqual(loc.namespaces[0].entries[0].text, "Shadow Realm")
            self.assertEqual(compute_sha256(pak_v11), pak_sha_before)

            # التحقق أيضاً من استخراج .locres مضغوط بـ Zlib داخل حزمة .pak v8
            import hashlib
            import zlib
            from formats.locres_handler import write_fstring

            zlib_blob = zlib.compress(locres_blob)
            raw_sha1 = hashlib.sha1(zlib_blob).digest()
            local_hdr = (
                struct.pack("<qqqI", 0, len(zlib_blob), len(locres_blob), 1)
                + raw_sha1
                + struct.pack("<I", 1)
                + struct.pack("<qq", 53 + 4 + 16, 53 + 4 + 16 + len(zlib_blob))
                + struct.pack("<BI", 0, len(locres_blob))
            )
            payload_start = len(local_hdr) - len(zlib_blob)
            # إعادة حساب إزاحة البلوك النسبية لبداية السجل المحلي
            hdr_len = 28 + 20 + 4 + 16 + 5
            local_hdr = (
                struct.pack("<qqqI", 0, len(zlib_blob), len(locres_blob), 1)
                + raw_sha1
                + struct.pack("<I", 1)
                + struct.pack("<qq", hdr_len, hdr_len + len(zlib_blob))
                + struct.pack("<BI", 0, len(locres_blob))
            )
            idx_offset = len(local_hdr) + len(zlib_blob)
            idx_bytes = (
                write_fstring("../../../")
                + struct.pack("<I", 1)
                + write_fstring("QuestUE4/Content/Localization/Game/en/Game.locres")
                + struct.pack("<qqqI", 0, len(zlib_blob), len(locres_blob), 1)
                + raw_sha1
                + struct.pack("<I", 1)
                + struct.pack("<qq", hdr_len, hdr_len + len(zlib_blob))
                + struct.pack("<BI", 0, len(locres_blob))
            )
            zlib_pak = os.path.join(paks_dir, "pakchunk1-Zlib.pak")
            methods_slots = b"Zlib".ljust(32, b"\x00") + b"\x00" * (4 * 32)
            with open(zlib_pak, "wb") as f:
                f.write(
                    local_hdr
                    + zlib_blob
                    + idx_bytes
                    + b"\x00" * 16
                    + b"\x00"
                    + struct.pack("<IIqq", 0x5A6F12E1, 8, idx_offset, len(idx_bytes))
                    + hashlib.sha1(idx_bytes).digest()
                    + methods_slots
                )
            extracted_zlib = extract_pak_entry_by_path(
                zlib_pak, "QuestUE4/Content/Localization/Game/en/Game.locres"
            )
            self.assertIsNotNone(extracted_zlib)
            self.assertEqual(
                parse_locres_bytes(extracted_zlib[2]).namespaces[0].entries[0].text,
                "Shadow Realm",
            )

        # 3. كشف UE5 عبر IoStore (.utoc/.ucas) + حزمة .pak مشفّرة بـ AES دون انهيار
        with tempfile.TemporaryDirectory() as tmp:
            paks_dir = os.path.join(tmp, "NovaGame", "Content", "Paks")
            os.makedirs(paks_dir)
            with open(os.path.join(paks_dir, "pakchunk0-Windows.utoc"), "wb") as f:
                f.write(b"-==--==--==--==-")
            with open(os.path.join(paks_dir, "pakchunk0-Windows.ucas"), "wb") as f:
                f.write(b"\x00" * 32)
            # .pak ذو فهرس مشفر bEncryptedIndex=1
            enc_pak = os.path.join(paks_dir, "pakchunk0-Windows.pak")
            footer = (
                b"\x00" * 16
                + b"\x01"  # bEncryptedIndex = 1
                + struct.pack("<IIqq", 0x5A6F12E1, 8, 0, 64)
                + b"\x00" * 20
                + b"\x00" * (5 * 32)
            )
            with open(enc_pak, "wb") as f:
                f.write(b"\x00" * 64 + footer)

            rep = AutoTranslatorPipeline().scan(tmp)
            self.assertTrue(rep.engine.supported)
            self.assertIn("UE5", rep.engine.scripting_backend)
            self.assertFalse(rep.success)
            self.assertTrue(any("AES" in h for h in rep.dll_hints))
            self.assertTrue(any("IoStore" in h for h in rep.dll_hints))

    def test_unreal_richtext_and_named_placeholders_protection_and_shaping(self):
        sample = '<Bold>Welcome</> {PlayerName}, click <span style="Highlight">Start</> <img id="Icon"/> ({0})'
        masked = mask_line(sample)
        for token in ("<Bold>", "</>", "{PlayerName}", '<span style="Highlight">', '<img id="Icon"/>', "{0}"):
            self.assertIn(token, masked.placeholders)

        translated = masked.masked.replace("Welcome", "أهلاً").replace("click", "اضغط").replace("Start", "ابدأ")
        unmasked, repairs = unmask_line(
            translated,
            masked.placeholders,
            masked.lead_ws,
            masked.trail_ws,
        )
        self.assertEqual(repairs, 0)
        self.assertEqual(verify_placeholders(masked.placeholders, unmasked), [])

        # التحقق من أن process_text يحافظ على أزواج Unreal RichText (<Bold>...</>) دون قلب الوسوم
        shaped = process_text("<Bold>مرحبا بكم</> {PlayerName}")
        self.assertTrue(shaped.startswith("<Bold>") or "{PlayerName}" in shaped)
        self.assertIn("<Bold>", shaped)
        self.assertIn("</>", shaped)
        self.assertLess(shaped.index("<Bold>"), shaped.index("</>"))

    def test_unreal_pak_end_to_end_mods_pak_font_update_and_rollback(self):
        from engines.unreal.pak_parser import (
            build_unreal_pak_blob,
            extract_pak_entry_by_path,
            parse_pak_header_and_entries,
        )
        from formats.locres_handler import build_locres_bytes, parse_locres_bytes

        with tempfile.TemporaryDirectory() as tmp:
            exe_path = os.path.join(tmp, "StarOdyssey-Win64-Shipping.exe")
            self._build_pe_exe(exe_path, x64=True)
            paks_dir = os.path.join(tmp, "StarOdyssey", "Content", "Paks")
            os.makedirs(paks_dir)

            locres_blob = build_locres_bytes(
                [
                    ("MainMenu", "Play", "Start Adventure"),
                    ("MainMenu", "Quit", "Exit Game"),
                    ("HUD", "Greeting", "<Bold>Hello</> {PlayerName}!"),
                    ("Sys", "Ref", "/Game/UI/MainMenu"),
                ],
                version=3,
            )
            base_pak = os.path.join(paks_dir, "pakchunk0-WindowsNoEditor.pak")
            with open(base_pak, "wb") as f:
                f.write(
                    build_unreal_pak_blob(
                        {"StarOdyssey/Content/Localization/Game/en/Game.locres": locres_blob},
                        mount_point="../../../",
                        version=8,
                    )
                )
            base_sha_before = compute_sha256(base_pak)

            def fake_ep(batch_text, ep, sl, tl):
                return (
                    batch_text.replace("Start Adventure", "ابدأ المغامرة")
                    .replace("Exit Game", "خروج من اللعبة")
                    .replace("Hello", "مرحباً")
                )

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            self.assertEqual(rep.engine.engine_name, "Unreal Engine")
            self.assertEqual(rep.engine.architecture, "x64")
            self.assertFalse(rep.engine.requires_arabic_shaping)
            self.assertIsNotNone(rep.injection)
            self.assertTrue(rep.injection.success)
            self.assertEqual(rep.injection.method, "unreal_mods_pak")

            # بصمة .pak الأصلية لم تتغير بايتاً واحداً
            self.assertEqual(compute_sha256(base_pak), base_sha_before)

            mod_pak = os.path.join(paks_dir, "~mods", "z_ArabicL10n_P.pak")
            font_disk = os.path.join(
                tmp, "StarOdyssey", "Content", "Localization", "ArabicFonts", "IBMPlexSansArabic-Regular.ttf"
            )
            ini_disk = os.path.join(tmp, "StarOdyssey", "Config", "DefaultEngine.ini")
            self.assertTrue(os.path.isfile(mod_pak))
            self.assertTrue(os.path.isfile(font_disk))
            self.assertTrue(os.path.isfile(ini_disk))

            mod_info = parse_pak_header_and_entries(mod_pak)
            self.assertIsNotNone(mod_info)
            extracted = extract_pak_entry_by_path(
                mod_pak, "StarOdyssey/Content/Localization/Game/en/Game.locres"
            )
            self.assertIsNotNone(extracted)
            ar_locres = parse_locres_bytes(extracted[2])
            self.assertEqual(ar_locres.version, 3)
            self.assertEqual(ar_locres.namespaces[0].entries[0].text, "ابدأ المغامرة")
            self.assertEqual(ar_locres.namespaces[0].entries[1].text, "خروج من اللعبة")
            # عدم التشبيك (harfBuzz أصلي في UE) + حماية الوسوم والمتغيرات
            self.assertEqual(ar_locres.namespaces[1].entries[0].text, "<Bold>مرحباً</> {PlayerName}!")
            self.assertEqual(ar_locres.namespaces[2].entries[0].text, "/Game/UI/MainMenu")

            # اختبار update_font يحافظ على النصوص المترجمة داخل ~mods
            uf_rep = pipe.update_font(tmp)
            self.assertTrue(uf_rep.success, uf_rep.errors)
            extracted_after = extract_pak_entry_by_path(
                mod_pak, "StarOdyssey/Content/Localization/Game/en/Game.locres"
            )
            self.assertIsNotNone(extracted_after)
            self.assertEqual(
                parse_locres_bytes(extracted_after[2]).namespaces[0].entries[0].text,
                "ابدأ المغامرة",
            )

            # الاسترجاع الكامل
            _, warnings = AutoTranslatorPipeline.rollback(tmp)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(mod_pak))
            self.assertFalse(os.path.exists(font_disk))
            self.assertFalse(os.path.exists(ini_disk))
            self.assertEqual(compute_sha256(base_pak), base_sha_before)

    def test_unreal_external_locres_end_to_end_sibling_skip_and_rollback(self):
        from formats.locres_handler import build_locres_bytes, parse_locres_bytes

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "Engine", "Binaries"))
            en_dir = os.path.join(tmp, "MyGame", "Content", "Localization", "Game", "en")
            fr_dir = os.path.join(tmp, "MyGame", "Content", "Localization", "Game", "fr")
            cfg_dir = os.path.join(tmp, "MyGame", "Config")
            os.makedirs(en_dir)
            os.makedirs(fr_dir)
            os.makedirs(cfg_dir)

            en_locres = os.path.join(en_dir, "Game.locres")
            fr_locres = os.path.join(fr_dir, "Game.locres")
            with open(en_locres, "wb") as f:
                f.write(build_locres_bytes([("Menu", "NewGame", "New Game")], version=2))
            with open(fr_locres, "wb") as f:
                f.write(build_locres_bytes([("Menu", "NewGame", "Nouvelle partie")], version=2))

            ini_path = os.path.join(cfg_dir, "DefaultEngine.ini")
            with open(ini_path, "w", encoding="utf-8") as f:
                f.write("[/Script/EngineSettings.GeneralProjectSettings]\nProjectName=MyGame\n")

            en_sha_orig = compute_sha256(en_locres)
            fr_sha_orig = compute_sha256(fr_locres)
            ini_sha_orig = compute_sha256(ini_path)

            def fake_ep(batch_text, ep, sl, tl):
                return batch_text.replace("New Game", "لعبة جديدة")

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            self.assertEqual(len(rep.selected_files), 1)
            self.assertTrue(rep.selected_files[0].relative_path.endswith("en/Game.locres"))

            with open(en_locres, "rb") as f:
                self.assertEqual(
                    parse_locres_bytes(f.read()).namespaces[0].entries[0].text,
                    "لعبة جديدة",
                )
            self.assertEqual(compute_sha256(fr_locres), fr_sha_orig)

            with open(ini_path, "r", encoding="utf-8") as f:
                ini_txt = f.read()
            self.assertIn("ProjectName=MyGame", ini_txt)
            self.assertIn("[Internationalization]", ini_txt)

            _, warnings = AutoTranslatorPipeline.rollback(tmp)
            self.assertEqual(warnings, [])
            self.assertEqual(compute_sha256(en_locres), en_sha_orig)
            self.assertEqual(compute_sha256(ini_path), ini_sha_orig)

    def test_unreal_mount_point_sig_locmeta_and_packed_json_datatables(self):
        from engines.unreal.pak_parser import (
            build_unreal_pak_blob,
            extract_pak_entry_by_path,
            parse_pak_header_and_entries,
        )
        from formats.locres_handler import LOCMETA_MAGIC, build_locres_bytes, write_fstring

        with tempfile.TemporaryDirectory() as tmp:
            exe_path = os.path.join(tmp, "Echoes-Win64-Shipping.exe")
            self._build_pe_exe(exe_path, x64=True)
            paks_dir = os.path.join(tmp, "Echoes", "Content", "Paks")
            os.makedirs(paks_dir)

            # ملف .locmeta يحدد en-US كثقافة أم
            locmeta_blob = (
                LOCMETA_MAGIC
                + struct.pack("<B", 1)
                + write_fstring("en-US")
                + write_fstring("en-US/Game.locres")
                + struct.pack("<I", 1)
                + write_fstring("en-US")
            )
            locres_blob = build_locres_bytes([("HUD", "Hint", "Press Jump")], version=2)
            dialogue_json = json.dumps({"intro_1": "Welcome traveler"}, ensure_ascii=False).encode("utf-8")

            # حزمة أصلية تستخدم MountPoint نسبي بالمشروع ../../../Echoes/
            base_pak = os.path.join(paks_dir, "pakchunk0-WindowsNoEditor.pak")
            with open(base_pak, "wb") as f:
                f.write(
                    build_unreal_pak_blob(
                        {
                            "Content/Localization/Game/Game.locmeta": locmeta_blob,
                            "Content/Localization/Game/en-US/Game.locres": locres_blob,
                            "Content/Dialogue/en/Story.json": dialogue_json,
                        },
                        mount_point="../../../Echoes/",
                        version=8,
                    )
                )
            # وجود ملف .sig بجانب الحزمة
            with open(os.path.join(paks_dir, "pakchunk0-WindowsNoEditor.sig"), "wb") as f:
                f.write(b"\x00" * 16)

            def fake_ep(batch_text, ep, sl, tl):
                return (
                    batch_text.replace("Press Jump", "اضغط للقفز")
                    .replace("Welcome traveler", "أهلاً أيها المسافر")
                )

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            self.assertTrue(any(".sig" in h for h in rep.dll_hints))
            self.assertTrue(any("en-US" in h for h in rep.dll_hints))
            self.assertIn(".sig", rep.injection.message)

            mod_pak = os.path.join(paks_dir, "~mods", "z_ArabicL10n_P.pak")
            mod_info = parse_pak_header_and_entries(mod_pak)
            self.assertIsNotNone(mod_info)
            # التحقق من عدم مضاعفة Echoes/Echoes داخل MountPoint
            self.assertEqual(mod_info.mount_point, "../../../")
            entry_paths = [e.path for e in mod_info.entries]
            self.assertIn("Echoes/Content/Localization/Game/en-US/Game.locres", entry_paths)
            self.assertIn("Echoes/Content/Dialogue/en/Story.json", entry_paths)
            self.assertFalse(any("Echoes/Echoes" in p for p in entry_paths))

            extracted_json = extract_pak_entry_by_path(mod_pak, "Echoes/Content/Dialogue/en/Story.json")
            self.assertIsNotNone(extracted_json)
            self.assertEqual(json.loads(extracted_json[2].decode("utf-8"))["intro_1"], "أهلاً أيها المسافر")

            ini_path = os.path.join(tmp, "Echoes", "Config", "DefaultEngine.ini")
            with open(ini_path, "r", encoding="utf-8") as f:
                self.assertIn("+LocalizationPaths=", f.read())

            AutoTranslatorPipeline.rollback(tmp)

    def test_unreal_utf16_datatable_csv_and_ftext_plural_protection(self):
        # 1. حماية صيغة FText plural/gender ووسوم RPG Maker NameBox
        ue_line = "Found {Count}|plural(one=1 apple,other={Count} apples)!"
        masked_ue = mask_line(ue_line)
        self.assertEqual(masked_ue.placeholders, ["{Count}|plural(one=1 apple,other={Count} apples)"])
        unmasked_ue, rep_ue = unmask_line(
            masked_ue.masked.replace("Found", "تم العثور على"),
            masked_ue.placeholders,
            "",
            "",
        )
        self.assertEqual(rep_ue, 0)
        self.assertEqual(unmasked_ue, "تم العثور على {Count}|plural(one=1 apple,other={Count} apples)!")

        # 2. جدول Unreal DataTable CSV بترميز UTF-16-LE BOM وصف رأس ---
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "Engine", "Binaries"))
            dt_dir = os.path.join(tmp, "MyGame", "Content", "Data")
            os.makedirs(dt_dir)
            csv_path = os.path.join(dt_dir, "QuestTable.csv")
            csv_body = "---,Title,Description\r\nQ_01,First Steps,Talk to the elder\r\n"
            with open(csv_path, "wb") as f:
                f.write(csv_body.encode("utf-16"))

            def fake_ep(batch_text, ep, sl, tl):
                return (
                    batch_text.replace("First Steps", "الخطوات الأولى")
                    .replace("Talk to the elder", "تحدث إلى الحكيم")
                )

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)

            with open(csv_path, "rb") as f:
                raw_out = f.read()
            # الحفاظ على UTF-16 وحفظ رأس الأعمدة ---,Title,Description بدون ترجمة
            self.assertTrue(raw_out.startswith((b"\xff\xfe", b"\xfe\xff")))
            decoded = raw_out.decode("utf-16")
            self.assertTrue(decoded.startswith("---,Title,Description"))
            self.assertIn("Q_01,الخطوات الأولى,تحدث إلى الحكيم", decoded)


class CrossEngineHardeningTests(unittest.TestCase):
    def test_rpgmaker_locale_protection_custom_css_font_and_namebox_escape(self):
        from engines.rpgmaker.scanner import is_translatable_value as rm_translatable

        # 1. مفتاح locale الجذري في System.json لا يُترجم، ووسم \n<Hero> و \c[#FF0000] يُحمى كوحدة واحدة
        self.assertFalse(rm_translatable("locale", "en"))
        self.assertFalse(rm_translatable("locale", "English"))

        rm_line = r"\n<Hero> \c[#FF0000]Watch out!"
        masked_rm = mask_line(rm_line)
        self.assertIn(r"\n<Hero>", masked_rm.placeholders)
        self.assertIn(r"\c[#FF0000]", masked_rm.placeholders)
        unmasked_rm, repairs = unmask_line(
            masked_rm.masked.replace("Watch out!", "احذر!"),
            masked_rm.placeholders,
            "",
            "",
        )
        self.assertEqual(repairs, 0)
        self.assertEqual(verify_placeholders(masked_rm.placeholders, unmasked_rm), [])

        # 2. تغطية خط مخصص في gamefont.css عبر unicode-range
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "www", "data")
            fonts_dir = os.path.join(tmp, "www", "fonts")
            os.makedirs(data_dir)
            os.makedirs(fonts_dir)
            with open(os.path.join(data_dir, "System.json"), "w", encoding="utf-8") as f:
                json.dump({"locale": "en", "gameTitle": "Fantasy Quest"}, f)
            css_path = os.path.join(fonts_dir, "gamefont.css")
            with open(css_path, "w", encoding="utf-8") as f:
                f.write("@font-face {\n  font-family: 'PixelFantasy';\n  src: url('pixel.ttf');\n}\n")

            def fake_ep(batch_text, ep, sl, tl):
                return batch_text.replace("Fantasy Quest", "مهمة الخيال")

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)

            with open(os.path.join(data_dir, "System.json"), "r", encoding="utf-8") as f:
                sys_out = json.load(f)
            self.assertEqual(sys_out["locale"], "en")
            self.assertEqual(sys_out["gameTitle"], "مهمة الخيال")

            with open(css_path, "r", encoding="utf-8") as f:
                css_out = f.read()
            self.assertIn("font-family: 'PixelFantasy';", css_out)
            self.assertIn("unicode-range:", css_out)

    def test_backup_detects_arabic_inside_bak_and_locres_bak(self):
        from core.backup import is_file_likely_arabic
        from formats.locres_handler import build_locres_bytes

        with tempfile.TemporaryDirectory() as tmp:
            json_bak = os.path.join(tmp, "English.json.bak")
            with open(json_bak, "w", encoding="utf-8") as f:
                json.dump({"title": "مرحبا بكم في اللعبة التجريبية"}, f, ensure_ascii=False)
            self.assertTrue(is_file_likely_arabic(json_bak))

            locres_bak = os.path.join(tmp, "Game.locres.bak")
            with open(locres_bak, "wb") as f:
                f.write(build_locres_bytes([("UI", "Title", "مرحبا بكم في اللعبة")], version=2))
            self.assertTrue(is_file_likely_arabic(locres_bak))


_RPGM_JS_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const pluginPath = process.argv[2];
const variant = process.argv[3];
const draws = [];
const ctx = { console };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(`
function Window_Base() {}
Window_Base._iconWidth = 32;
Window_Base.prototype.textWidth = function(t) { return t.length * 10; };
Window_Base.prototype.contentsWidth = function() { return 400; };
Window_Base.prototype.drawIcon = function() {};
Window_Base.prototype.processNewLine = function(ts) { ts.x = ts.left || 0; ts.y += ts.height; ts.index++; };
Window_Base.prototype.processNormalCharacter = function(ts) {
  var c = ts.text[ts.index++]; var w = this.textWidth(c);
  this.contents.drawText(c, ts.x, ts.y, w * 2, ts.height); ts.x += w;
};
Window_Base.prototype.processDrawIcon = function(i, ts) { ts.x += 36; };
function Window_Message() {}
Window_Message.prototype = Object.create(Window_Base.prototype);
Window_Message.prototype.constructor = Window_Message;
Window_Message.prototype.newPage = function(ts) { ts.x = 0; ts.y = 0; ts.left = 0; ts.height = 36; };
function Game_Interpreter() {}
Game_Interpreter.prototype.pluginCommand = function() {};
var PluginManager = { parameters: function() { return {}; } };
`, ctx);
if (variant === 'mz') {
  vm.runInContext(`
  Window_Base.prototype.flushTextState = function() {};
  Window_Message.prototype.canBreakHere = function(ts) { return true; };
  PluginManager.registerCommand = function() {};
  `, ctx);
}
vm.runInContext(fs.readFileSync(pluginPath, 'utf8'), ctx);
const out = { draws };
if (variant === 'mv') {
  const win = new ctx.Window_Message();
  win.contents = { drawText: (t, x) => draws.push([t, x]) };
  const run = (text) => {
    const ts = { text: text, index: 0 };
    win.newPage(ts);
    while (ts.index < ts.text.length) {
      const c = ts.text[ts.index];
      if (c === '\n') win.processNewLine(ts);
      else if (c === '\x1b') { ts.index += 1; while (/[A-Z]/.test(ts.text[ts.index])) ts.index++;
        if (ts.text[ts.index] === '[') ts.index = ts.text.indexOf(']', ts.index) + 1; }
      else win.processNormalCharacter(ts);
    }
  };
  run('\u0645\u0631\u062d\u0628\u0627 \u0628\u0643 \x1bC[2]Bob\x1bC[0] \u0647\u0646\u0627\nHello world');
  ctx.Game_Interpreter.prototype.pluginCommand.call({}, 'ArabicText', ['off']);
  out.disabled = ctx.AutoTranslatorArabicText.enabled === false;
} else {
  const win = new ctx.Window_Message();
  const text = '\u0645\u0631\u062d\u0628\u0627 \u0628\u0643 Hi';
  out.breaks = [];
  for (let i = 1; i < text.length; i++) out.breaks.push(win.canBreakHere({ text: text, index: i }) ? 1 : 0);
  const latin = 'Hello';
  out.latinBreaks = [];
  for (let i = 1; i < latin.length; i++) out.latinBreaks.push(win.canBreakHere({ text: latin, index: i }) ? 1 : 0);
}
process.stdout.write(JSON.stringify(out));
"""


class RpgMakerArabicDialogueFixTests(unittest.TestCase):
    def _make_mv_game(self, tmp, plugins_js_text):
        data = os.path.join(tmp, "www", "data")
        js_dir = os.path.join(tmp, "www", "js")
        os.makedirs(data)
        os.makedirs(os.path.join(js_dir, "plugins"))
        os.makedirs(os.path.join(tmp, "www", "fonts"))
        with open(os.path.join(tmp, "Game.exe"), "wb") as f:
            f.write(_make_minimal_x64_pe())
        with open(os.path.join(data, "System.json"), "w", encoding="utf-8") as f:
            json.dump({"gameTitle": "Hero Saga", "terms": {"commands": ["Fight", "Escape"]}}, f)
        plugins_js = os.path.join(js_dir, "plugins.js")
        if plugins_js_text is not None:
            with open(plugins_js, "w", encoding="utf-8") as f:
                f.write(plugins_js_text)
        return plugins_js

    @staticmethod
    def _fake_pipe(**kw):
        def fake_ep(batch_text, ep, sl, tl):
            return "\n".join("ع:" + ln for ln in batch_text.splitlines())

        return AutoTranslatorPipeline(
            translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep), **kw
        )

    def test_plugins_js_upsert_preserves_other_plugins_and_is_idempotent(self):
        from engines.rpgmaker.plugin_patch import (
            PLUGIN_NAME, build_plugin_entry, parse_plugins_js, upsert_plugin_entry,
        )

        original = (
            "// Generated by RPG Maker.\n// Do not edit this file directly.\nvar $plugins =\n[\n"
            '{"name":"Community_Basic","status":true,"description":"Basic plugin","parameters":{"cacheLimit":"20"}},\n'
            '{"name":"YEP_MessageCore","status":false,"description":"Msg \\"core\\"","parameters":{"Font Name":"GameFont"}}\n'
            "];\n"
        )
        new_text, changed = upsert_plugin_entry(original, build_plugin_entry())
        self.assertTrue(changed)
        self.assertTrue(new_text.startswith("// Generated by RPG Maker."))
        self.assertTrue(new_text.rstrip().endswith("];"))
        # الإضافات الأصلية محفوظة حرفياً
        self.assertIn('{"name":"YEP_MessageCore","status":false,"description":"Msg \\"core\\"","parameters":{"Font Name":"GameFont"}}', new_text)
        _, plugins, _ = parse_plugins_js(new_text)
        self.assertEqual([p["name"] for p in plugins], ["Community_Basic", "YEP_MessageCore", PLUGIN_NAME])

        again, changed2 = upsert_plugin_entry(new_text, build_plugin_entry())
        self.assertFalse(changed2)
        self.assertEqual(again, new_text)

        switched, changed3 = upsert_plugin_entry(new_text, build_plugin_entry(mode="line"))
        self.assertTrue(changed3)
        _, plugins3, _ = parse_plugins_js(switched)
        self.assertEqual(len(plugins3), 3)
        self.assertEqual(plugins3[2]["parameters"]["Mode"], "line")

        empty_text, _ = upsert_plugin_entry("var $plugins =\n[\n];\n", build_plugin_entry())
        self.assertEqual(len(parse_plugins_js(empty_text)[1]), 1)

        with self.assertRaises(ValueError):
            parse_plugins_js("var $plugins = [ {broken ];")
        with self.assertRaises(ValueError):
            parse_plugins_js("console.log('no plugins');")

    def test_mv_pipeline_deploys_plugin_and_rollback_restores_plugins_js(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig_plugins = (
                "var $plugins =\n[\n"
                '{"name":"Community_Basic","status":true,"description":"","parameters":{}}\n'
                "];\n"
            )
            plugins_js = self._make_mv_game(tmp, orig_plugins)
            rep = self._fake_pipe().translate(tmp)
            self.assertTrue(rep.success, rep.errors)

            from engines.rpgmaker.plugin_patch import PLUGIN_FILENAME, is_arabic_text_plugin_registered

            plugin_file = os.path.join(tmp, "www", "js", "plugins", PLUGIN_FILENAME)
            self.assertTrue(os.path.isfile(plugin_file))
            self.assertTrue(is_arabic_text_plugin_registered(plugins_js))
            self.assertIn(plugin_file, rep.injection.output_files)

            # update_font لا يكرر تسجيل الإضافة
            uf = self._fake_pipe().update_font(tmp)
            self.assertTrue(uf.success, uf.errors)
            with open(plugins_js, "r", encoding="utf-8") as f:
                self.assertEqual(f.read().count("AutoTranslator_ArabicText"), 1)

            restored, warnings = AutoTranslatorPipeline.rollback(tmp)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(plugin_file))
            with open(plugins_js, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), orig_plugins)

    def test_plugin_disabled_option_missing_and_corrupt_plugins_js(self):
        from engines.rpgmaker.plugin_patch import PLUGIN_FILENAME

        # 1. تعطيل الخيار: لا يُمس plugins.js
        with tempfile.TemporaryDirectory() as tmp:
            orig = 'var $plugins =\n[\n];\n'
            plugins_js = self._make_mv_game(tmp, orig)
            rep = self._fake_pipe(engine_options={"rpgmaker_text_fix": False}).translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            with open(plugins_js, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), orig)
            self.assertFalse(os.path.exists(os.path.join(tmp, "www", "js", "plugins", PLUGIN_FILENAME)))

        # 2. غياب plugins.js: تنبيه دون فشل
        with tempfile.TemporaryDirectory() as tmp:
            self._make_mv_game(tmp, None)
            rep = self._fake_pipe().translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            self.assertTrue(rep.injection.success)
            self.assertIn("plugins.js", rep.injection.message)

        # 3. plugins.js تالف: لا تعديل ولا ملف إضافة يتيم
        with tempfile.TemporaryDirectory() as tmp:
            corrupt = "var $plugins = [ {oops ];\n"
            plugins_js = self._make_mv_game(tmp, corrupt)
            rep = self._fake_pipe().translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            with open(plugins_js, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), corrupt)
            self.assertFalse(os.path.exists(os.path.join(tmp, "www", "js", "plugins", PLUGIN_FILENAME)))
            AutoTranslatorPipeline.rollback(tmp)
            with open(plugins_js, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), corrupt)

    @unittest.skipUnless(shutil.which("node"), "Node.js غير متوفر لمحاكاة رسم الحوار")
    def test_js_plugin_draws_arabic_words_connected_in_mv_and_mz(self):
        from engines.rpgmaker.plugin_patch import get_plugin_source_path

        with tempfile.TemporaryDirectory() as tmp:
            harness = os.path.join(tmp, "harness.js")
            with open(harness, "w", encoding="utf-8") as f:
                f.write(_RPGM_JS_HARNESS)

            def run(variant):
                proc = subprocess.run(
                    ["node", harness, get_plugin_source_path(), variant],
                    capture_output=True, text=True, encoding="utf-8", timeout=60,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return json.loads(proc.stdout)

            mv = run("mv")
            texts = [t.replace("\u202b", "").replace("\u202c", "") for t, _ in mv["draws"]]
            # الكلمات العربية تُرسم كاملة (لا حروف منفردة) والسطر الإنجليزي يبقى حرفاً حرفاً كالأصل
            self.assertIn("مرحبا ", texts)
            self.assertIn("بك ", texts)
            self.assertIn("Bob", texts)
            self.assertIn("هنا", [t.strip() for t in texts])
            self.assertNotIn("م", texts)
            self.assertIn("H", texts)
            self.assertIn("w", texts)
            # الاتجاه RTL: أول كلمة عربية تبدأ من يمين الصندوق (x يتناقص)
            xs = [x for t, x in mv["draws"][:3]]
            self.assertEqual(xs, sorted(xs, reverse=True))
            self.assertTrue(mv["draws"][0][0].startswith("\u202b"))
            self.assertTrue(mv["disabled"])

            mz = run("mz")
            text = "مرحبا بك Hi"
            allowed = [i + 1 for i, b in enumerate(mz["breaks"]) if b]
            # الكسر مسموح فقط عند بداية كل كلمة
            self.assertEqual(allowed, [text.index("بك"), text.index("Hi")])
            self.assertEqual(mz["latinBreaks"], [1, 1, 1, 1])


class GodotSmazAndThemeMergeTests(unittest.TestCase):
    def test_smaz_compress_decompress_and_optimized_translation_pck_pipeline(self):
        from engines.godot.pck_parser import (
            build_godot_pck_blob,
            build_godot_translation_rsrc_blob,
            extract_translation_resource_entries,
            smaz_compress,
            smaz_decompress,
        )

        # 1. ضغط وفك ضغط SMAZ قياسي + التعامل الآمن مع البيانات التالفة
        samples = [
            "Welcome to the ancient kingdom!",
            "Press [b]Space[/b] to jump, {player_name}.",
            "Loading... 100% complete! http://example.com",
            "Quick UTF-8 check: café & résumé",
        ]
        for s in samples:
            raw = s.encode("utf-8")
            comp = smaz_compress(raw)
            dec = smaz_decompress(comp, expected_size=len(raw))
            self.assertEqual(dec, raw)
        self.assertIsNone(smaz_decompress(b"\xfe"))  # 254 ناقص البايت التالي
        self.assertIsNone(smaz_decompress(b"\xff\x05abc"))  # 255 بطول أكبر من المتبقي

        # 2. مورد OptimizedTranslation مضغوط بـ SMAZ داخل .pck وترجمته بالكامل
        with tempfile.TemporaryDirectory() as tmp:
            pck_path = os.path.join(tmp, "SmazGame.pck")
            proj_txt = (
                'config_version=5\n'
                '[application]\n'
                'config/name="SmazQuest"\n'
                '[internationalization]\n'
                'locale/translations=PackedStringArray("res://translations/dialog.en.translation")\n'
            ).encode("utf-8")

            opt_smaz = build_godot_translation_rsrc_blob(
                locale="en",
                messages={
                    "K1": "The sun rises over the valley.",
                    "K2": "Talk to the elder for your quest.",
                },
                optimized=True,
                godot_version=(4, 2, 1),
                compress_smaz=True,
            )
            parsed = extract_translation_resource_entries(opt_smaz)
            self.assertIsNotNone(parsed)
            self.assertEqual(len(parsed[1]), 2)
            vals = [v for _, v in parsed[1]]
            self.assertIn("The sun rises over the valley.", vals)
            self.assertIn("Talk to the elder for your quest.", vals)

            with open(pck_path, "wb") as f:
                f.write(
                    build_godot_pck_blob(
                        {
                            "res://project.godot": proj_txt,
                            "res://translations/dialog.en.translation": opt_smaz,
                        },
                        pack_format_version=2,
                        godot_version=(4, 2, 1),
                    )
                )

            def fake_ep(batch_text, ep, sl, tl):
                return (
                    batch_text.replace("The sun rises over the valley.", "تشرق الشمس فوق الوادي.")
                    .replace("Talk to the elder for your quest.", "تحدث إلى الحكيم من أجل مهمتك.")
                )

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)

            ar_po = os.path.join(tmp, "arabic_l10n", "translations", "dialog.ar.po")
            self.assertTrue(os.path.isfile(ar_po))
            with open(ar_po, "r", encoding="utf-8") as f:
                po_txt = f.read()
            self.assertIn("تشرق الشمس فوق الوادي.", po_txt)
            self.assertIn("تحدث إلى الحكيم من أجل مهمتك.", po_txt)

    def test_godot_custom_theme_tres_merge_preserves_styles_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            themes_dir = os.path.join(tmp, "themes")
            tr_dir = os.path.join(tmp, "translations")
            os.makedirs(themes_dir)
            os.makedirs(tr_dir)

            with open(os.path.join(tmp, "project.godot"), "w", encoding="utf-8") as f:
                f.write(
                    'config_version=5\n'
                    '[application]\n'
                    'config/name="StyledThemeGame"\n'
                    '[internationalization]\n'
                    'locale/translations=PackedStringArray("res://translations/en.po")\n'
                    '[gui]\n'
                    'theme/custom="res://themes/dark.tres"\n'
                )

            orig_theme = (
                '[gd_resource type="Theme" load_steps=3 format=3]\n\n'
                '[ext_resource type="FontFile" path="res://fonts/old.ttf" id="1_old"]\n\n'
                '[sub_resource type="StyleBoxFlat" id="StyleBoxFlat_1"]\n'
                'bg_color = Color(0.1, 0.1, 0.2, 1)\n\n'
                '[resource]\n'
                'default_font = ExtResource("1_old")\n'
                'Button/colors/font_color = Color(1, 0.9, 0.2, 1)\n'
                'Button/fonts/font = ExtResource("1_old")\n'
                'Button/styles/normal = SubResource("StyleBoxFlat_1")\n'
            )
            with open(os.path.join(themes_dir, "dark.tres"), "w", encoding="utf-8") as f:
                f.write(orig_theme)

            with open(os.path.join(tr_dir, "en.po"), "w", encoding="utf-8") as f:
                f.write('msgid ""\nmsgstr ""\n"Language: en\\n"\n\nmsgid "PLAY"\nmsgstr "Play Now"\n')

            def fake_ep(batch_text, ep, sl, tl):
                return batch_text.replace("Play Now", "العب الآن")

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)

            merged_theme_path = os.path.join(tmp, "arabic_l10n", "arabic_theme.tres")
            self.assertTrue(os.path.isfile(merged_theme_path))
            with open(merged_theme_path, "r", encoding="utf-8") as f:
                merged_txt = f.read()

            # الحفاظ على الأنماط والألوان الأصلية مع استبدال الخط الافتراضي بالخط العربي
            self.assertIn('[sub_resource type="StyleBoxFlat" id="StyleBoxFlat_1"]', merged_txt)
            self.assertIn("bg_color = Color(0.1, 0.1, 0.2, 1)", merged_txt)
            self.assertIn("Button/colors/font_color = Color(1, 0.9, 0.2, 1)", merged_txt)
            self.assertIn('Button/styles/normal = SubResource("StyleBoxFlat_1")', merged_txt)
            self.assertIn('path="res://arabic_l10n/IBMPlexSansArabic-Regular.ttf"', merged_txt)
            self.assertIn('default_font = ExtResource("99_ar")', merged_txt)
            self.assertIn('Button/fonts/font = ExtResource("99_ar")', merged_txt)

            _, warnings = AutoTranslatorPipeline.rollback(tmp)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(merged_theme_path))


class UnrealIoStoreStringTableAndFontDiagTests(unittest.TestCase):
    def test_utoc_header_parsing_and_ustringtable_uasset_inspection(self):
        from engines.unreal.pak_parser import (
            build_mock_ustringtable_uasset_uexp,
            build_utoc_blob,
            inspect_uasset_bytes,
            parse_utoc_header,
        )

        with tempfile.TemporaryDirectory() as tmp:
            utoc_path = os.path.join(tmp, "pakchunk0-Windows.utoc")
            blob = build_utoc_blob(
                toc_entry_count=12,
                compression_methods=["Zlib", "Oodle"],
                directory_paths=[
                    "MyGame/Content/Localization/Game/en/Game.locres",
                    "MyGame/Content/UI/ST_MainMenu.uasset",
                    "MyGame/Content/Fonts/Roboto_FontFace.uasset",
                ],
                version=5,
                compressed=True,
                encrypted=False,
                signed=True,
                indexed=True,
            )
            with open(utoc_path, "wb") as f:
                f.write(blob)

            uinfo = parse_utoc_header(utoc_path)
            self.assertIsNotNone(uinfo)
            self.assertTrue(uinfo.valid_magic)
            self.assertTrue(uinfo.header_complete)
            self.assertEqual(uinfo.version, 5)
            self.assertEqual(uinfo.toc_entry_count, 12)
            self.assertTrue(uinfo.compressed)
            self.assertFalse(uinfo.encrypted)
            self.assertTrue(uinfo.signed)
            self.assertTrue(uinfo.indexed)
            self.assertIn("Zlib", uinfo.compression_methods)
            self.assertIn("Oodle", uinfo.compression_methods)
            self.assertTrue(any(p.endswith(".locres") for p in uinfo.detected_locres_paths))
            self.assertTrue(any("ST_MainMenu" in p for p in uinfo.detected_string_tables))
            self.assertTrue(any("Roboto" in p for p in uinfo.detected_font_assets))

        uasset_bytes, uexp_bytes = build_mock_ustringtable_uasset_uexp(
            "UI_Menu",
            [("PlayBtn", "Start Game"), ("QuitBtn", "Exit to Desktop")],
        )
        insp = inspect_uasset_bytes(uasset_bytes, uexp_bytes)
        self.assertTrue(insp.is_uasset)
        self.assertEqual(insp.asset_kind, "StringTable")
        self.assertEqual(insp.string_table_namespace, "UI_Menu")
        self.assertEqual(
            insp.string_table_entries,
            [("PlayBtn", "Start Game"), ("QuitBtn", "Exit to Desktop")],
        )

    def test_unreal_font_readiness_report_arabic_vs_latin_and_cooked_tables(self):
        from engines.unreal.pak_parser import (
            build_mock_ustringtable_uasset_uexp,
            build_unreal_pak_blob,
            build_utoc_blob,
        )

        # 1. حالة وجود خطوط لاتينية فقط + StringTable مطبوخ بدون .locres -> تحذير مربعات واضح
        with tempfile.TemporaryDirectory() as tmp:
            paks_dir = os.path.join(tmp, "SkyGame", "Content", "Paks")
            slate_fonts = os.path.join(tmp, "Engine", "Content", "Slate", "Fonts")
            os.makedirs(paks_dir)
            os.makedirs(slate_fonts)
            with open(os.path.join(slate_fonts, "Roboto-Regular.ttf"), "wb") as f:
                f.write(b"\x00\x01\x00\x00")
            with open(os.path.join(paks_dir, "pakchunk0-Windows.utoc"), "wb") as f:
                f.write(
                    build_utoc_blob(
                        toc_entry_count=4,
                        compression_methods=["Oodle"],
                        directory_paths=["SkyGame/Content/Localization/ST_Dialog.uasset"],
                    )
                )

            uasset_b, uexp_b = build_mock_ustringtable_uasset_uexp(
                "ST_Dialog", [("Hello", "Greetings traveler")]
            )
            with open(os.path.join(paks_dir, "pakchunk0-Windows.pak"), "wb") as f:
                f.write(
                    build_unreal_pak_blob(
                        {
                            "SkyGame/Content/Localization/ST_Dialog.uasset": uasset_b,
                            "SkyGame/Content/Localization/ST_Dialog.uexp": uexp_b,
                        },
                        version=8,
                    )
                )

            rep = AutoTranslatorPipeline().scan(tmp)
            self.assertTrue(rep.engine.supported)
            self.assertTrue(any("UStringTable" in h for h in rep.dll_hints))
            self.assertTrue(any("تحذير مربعات محتمل" in h for h in rep.dll_hints))
            self.assertTrue(any("IoStore" in h and "4 أصل" in h for h in rep.dll_hints))

            # 2. إضافة خط عربي مدعوم إلى Slate/Fonts -> يصبح تقرير الخط العربي (جاهز)
            with open(os.path.join(slate_fonts, "NotoSansArabic-Regular.ttf"), "wb") as f:
                f.write(b"\x00\x01\x00\x00")
            rep2 = AutoTranslatorPipeline().scan(tmp)
            self.assertTrue(any("تقرير الخط العربي (جاهز)" in h for h in rep2.dll_hints))


class YamlUnityMultiFileAndCapturedTextsTests(unittest.TestCase):
    def test_yaml_handler_nested_lists_comments_quotes_utf16_and_rollback(self):
        from formats.yaml_handler import YamlFormatHandler

        yaml_text = (
            "# Main localization file\n"
            "ui:\n"
            '  play: "Start Game" # button\n'
            "  quit: 'Exit Game'\n"
            "  count: 42\n"
            "  enabled: true\n"
            "  notes: |\n"
            "    First line\n"
            "    Second line\n"
            "items:\n"
            '  - "Health Potion"\n'
            "  - id: 1\n"
            "    name: Iron Sword\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            yml_path = os.path.join(tmp, "strings.yml")
            with open(yml_path, "wb") as f:
                f.write(yaml_text.encode("utf-16"))

            handler = YamlFormatHandler()
            doc = handler.load(yml_path)
            keys = [k for _, k, _ in doc.entries]
            self.assertIn("ui.play", keys)
            self.assertIn("ui.quit", keys)
            self.assertIn("ui.notes", keys)
            self.assertIn("items.0", keys)
            self.assertIn("items.1.name", keys)
            self.assertNotIn("ui.count", keys)
            self.assertNotIn("ui.enabled", keys)

            repl = {}
            for pt, k, _ in doc.entries:
                if k == "ui.play":
                    repl[pt] = "ابدأ: اللعبة"
                elif k == "ui.quit":
                    repl[pt] = "خروج\u200e من اللعبة"
                elif k == "ui.notes":
                    repl[pt] = "السطر الأول\nالسطر الثاني"
                elif k == "items.0":
                    repl[pt] = "جرعة صحة"
                elif k == "items.1.name":
                    repl[pt] = "سيف: حديدي"

            handler.save(yml_path, doc, repl)
            with open(yml_path, "rb") as f:
                raw_out = f.read()
            self.assertTrue(raw_out.startswith((b"\xff\xfe", b"\xfe\xff")))
            decoded = raw_out.decode("utf-16")
            self.assertIn("# Main localization file", decoded)
            self.assertIn("# button", decoded)
            self.assertIn("count: 42", decoded)
            self.assertNotIn("\u200e", decoded)

            reloaded = handler.load(yml_path)
            by_k = {k: v for _, k, v in reloaded.entries}
            self.assertEqual(by_k["ui.play"], "ابدأ: اللعبة")
            self.assertEqual(by_k["ui.quit"], "خروج من اللعبة")
            self.assertEqual(by_k["ui.notes"], "السطر الأول\nالسطر الثاني")
            self.assertEqual(by_k["items.0"], "جرعة صحة")
            self.assertEqual(by_k["items.1.name"], "سيف: حديدي")

    def test_protector_preserves_literal_single_brackets_and_strips_directional_marks(self):
        # 1. [1] العادية لا تتحول إلى متغير إذا لم يكن مفقوداً
        plain = "اختر الخيار [1] أو [2]"
        unmasked_plain, rep_plain = unmask_line(plain, [], "", "")
        self.assertEqual(rep_plain, 0)
        self.assertEqual(unmasked_plain, plain)

        masked = mask_line("Player {0} chose option [1]")
        self.assertEqual(masked.placeholders, ["{0}"])
        # علامات اتجاه خفية داخل [[[0]]] تُنظف، و [1] العادية تبقى كما هي
        trans = "اللاعب [[[\u200e0\u200f]]] اختار [1] من [0]"
        # ملاحظة: [[[0]]] موجودة لذا [0] تبقى حرفية أيضاً ولا تُفسر خطأً
        out, repairs = unmask_line(trans, masked.placeholders, "", "")
        self.assertEqual(out, "اللاعب {0} اختار [1] من [0]")
        self.assertNotIn("\u200e", out)
        self.assertNotIn("\u200f", out)

    def test_unity_multifile_english_dir_selection_and_sibling_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            managed = os.path.join(tmp, "Demo_Data", "Managed")
            en_dir = os.path.join(tmp, "Demo_Data", "StreamingAssets", "Localization", "en")
            fr_dir = os.path.join(tmp, "Demo_Data", "StreamingAssets", "Localization", "fr")
            os.makedirs(managed)
            os.makedirs(en_dir)
            os.makedirs(fr_dir)
            with open(os.path.join(managed, "UnityEngine.CoreModule.dll"), "wb") as f:
                f.write(b"MZ")

            ui_en = os.path.join(en_dir, "ui.json")
            items_en = os.path.join(en_dir, "items.json")
            quests_en = os.path.join(en_dir, "quests.yaml")
            ui_fr = os.path.join(fr_dir, "ui.json")

            with open(ui_en, "w", encoding="utf-8") as f:
                json.dump({"play": "Play Game", "opt": "Options", "quit": "Exit Game"}, f)
            with open(items_en, "w", encoding="utf-8") as f:
                json.dump({"potion": "Health Potion", "sword": "Iron Sword", "shield": "Oak Shield"}, f)
            with open(quests_en, "w", encoding="utf-8") as f:
                f.write('q1: "Save the village"\nq2: "Find the lost key"\nq3: "Defeat the dragon"\n')
            with open(ui_fr, "w", encoding="utf-8") as f:
                json.dump({"play": "Jouer", "opt": "Options", "quit": "Quitter"}, f)
            fr_sha = compute_sha256(ui_fr)

            def fake_ep(batch_text, ep, sl, tl):
                return "\n".join("ع:" + ln for ln in batch_text.splitlines())

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp, skip_inject=True)
            self.assertTrue(rep.success, rep.errors)
            sel_rels = {c.relative_path.replace("\\", "/") for c in rep.selected_files}
            self.assertEqual(len(sel_rels), 3)
            self.assertTrue(any(r.endswith("en/ui.json") for r in sel_rels))
            self.assertTrue(any(r.endswith("en/items.json") for r in sel_rels))
            self.assertTrue(any(r.endswith("en/quests.yaml") for r in sel_rels))
            self.assertFalse(any("/fr/" in r for r in sel_rels))
            self.assertEqual(compute_sha256(ui_fr), fr_sha)

            with open(quests_en, "r", encoding="utf-8") as f:
                q_txt = f.read()
            self.assertRegex(q_txt, r"[\uFE70-\uFEFF]")

            _, warnings = AutoTranslatorPipeline.rollback(tmp)
            self.assertEqual(warnings, [])
            with open(quests_en, "r", encoding="utf-8") as f:
                self.assertIn("Save the village", f.read())

    def test_translate_captured_texts_merges_override_and_rollback(self):
        from main import main as cli_main

        with tempfile.TemporaryDirectory() as tmp:
            managed = os.path.join(tmp, "Cap_Data", "Managed")
            os.makedirs(managed)
            with open(os.path.join(managed, "UnityEngine.CoreModule.dll"), "wb") as f:
                f.write(b"MZ")

            # حالة الخطأ عند عدم وجود arabic_captured_texts.txt
            def fake_ep(batch_text, ep, sl, tl):
                return (
                    batch_text.replace("Press Any Key", "اضغط أي مفتاح")
                    .replace("Score", "النقاط")
                )

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            missing_rep = pipe.translate_captured_texts(tmp, skip_inject=True)
            self.assertFalse(missing_rep.success)
            self.assertTrue(missing_rep.errors)

            # وجود ArabicOverride.json سابق + arabic_captured_texts.txt جديد
            override_path = os.path.join(tmp, "ArabicOverride.json")
            with open(override_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "version": 1,
                        "by_key": {"MENU_PLAY": "ابدأ"},
                        "by_source": {"Play": "ابدأ"},
                    },
                    f,
                    ensure_ascii=False,
                )
            with open(os.path.join(tmp, "arabic_captured_texts.txt"), "w", encoding="utf-8") as f:
                f.write("Press Any Key\nScore: {0}\\nLevel Up\n12345\n")

            rep = pipe.translate_captured_texts(tmp, skip_inject=True)
            self.assertTrue(rep.success, rep.errors)
            self.assertEqual(rep.stats.translatable_keys, 2)

            with open(override_path, "r", encoding="utf-8") as f:
                ov = json.load(f)
            # الحفاظ على المدخلات السابقة ودمج الملتقطة المشكّلة مع حفظ المتغير {0}
            self.assertEqual(ov["by_key"]["MENU_PLAY"], "ابدأ")
            self.assertEqual(ov["by_source"]["Play"], "ابدأ")
            self.assertIn("Press Any Key", ov["by_source"])
            self.assertRegex(ov["by_source"]["Press Any Key"], r"[\uFE70-\uFEFF]")
            self.assertIn("Score: {0}\nLevel Up", ov["by_source"])
            self.assertIn("{0}", ov["by_source"]["Score: {0}\nLevel Up"])

            # التحقق من CLI translate-captured على مجلد مفقود يُرجع 1 بأمان
            self.assertEqual(cli_main(["translate-captured", os.path.join(tmp, "missing_sub"), "--skip-inject"]), 1)

            # Rollback يسترجع ArabicOverride.json الأصلي وينظف arabic_captured_texts.txt
            _, warnings = AutoTranslatorPipeline.rollback(tmp)
            self.assertEqual(warnings, [])
            with open(override_path, "r", encoding="utf-8") as f:
                restored_ov = json.load(f)
            self.assertNotIn("Press Any Key", restored_ov["by_source"])
            self.assertEqual(restored_ov["by_key"]["MENU_PLAY"], "ابدأ")


class UnrealPakYamlAndGodotRootResolutionTests(unittest.TestCase):
    def test_unreal_pak_handler_yaml_yml_existing_and_unsupported_extensions(self):
        from engines.unreal.pak_parser import build_unreal_pak_blob
        from formats.unreal_pak_handler import UnrealPakFormatHandler, _get_inner_text_handler
        from formats.yaml_handler import YamlFormatHandler

        self.assertEqual(_get_inner_text_handler(".yaml")[0], "yaml")
        self.assertEqual(_get_inner_text_handler(".yml")[0], "yaml")
        self.assertIsInstance(_get_inner_text_handler(".yaml")[1], YamlFormatHandler)
        self.assertIsInstance(_get_inner_text_handler(".yml")[1], YamlFormatHandler)
        self.assertEqual(_get_inner_text_handler(".csv")[0], "csv")
        self.assertEqual(_get_inner_text_handler(".tsv")[0], "csv")
        self.assertEqual(_get_inner_text_handler(".json")[0], "json")
        self.assertEqual(_get_inner_text_handler(".po")[0], "po")
        self.assertEqual(_get_inner_text_handler(".ini")[0], "ini")
        with self.assertRaises(ValueError):
            _get_inner_text_handler(".xml")

        yaml_bytes = (
            "# UI localization\n"
            "menu:\n"
            '  play: "Start Game" # main button\n'
            "  count: 3\n"
        ).encode("utf-8")
        yml_bytes = (
            "dialogue:\n"
            '  greeting: "Welcome, {Hero}!"\n'
        ).encode("utf-8")
        json_bytes = json.dumps({"title": "Echoes"}, ensure_ascii=False).encode("utf-8")
        xml_bytes = b"<strings><item id='1'>Hello</item></strings>"

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "Engine", "Binaries"))
            paks_dir = os.path.join(tmp, "MyGame", "Content", "Paks")
            os.makedirs(paks_dir)
            pak_path = os.path.join(paks_dir, "pakchunk0-WindowsNoEditor.pak")
            with open(pak_path, "wb") as f:
                f.write(
                    build_unreal_pak_blob(
                        {
                            "MyGame/Content/Localization/Game/en/Menu.yaml": yaml_bytes,
                            "MyGame/Content/Dialogue/en/Intro.yml": yml_bytes,
                            "MyGame/Content/Localization/Game/en/Meta.json": json_bytes,
                            "MyGame/Content/Data/Unsupported.xml": xml_bytes,
                        },
                        mount_point="../../../",
                        version=8,
                    )
                )

            handler = UnrealPakFormatHandler()

            doc_yaml = handler.load(f"{pak_path}::MyGame/Content/Localization/Game/en/Menu.yaml")
            self.assertEqual(doc_yaml.raw_data["inner_mode"], "yaml")
            self.assertEqual([k for _, k, _ in doc_yaml.entries], ["menu.play"])
            handler.save(
                f"{pak_path}::MyGame/Content/Localization/Game/en/Menu.yaml",
                doc_yaml,
                {doc_yaml.entries[0][0]: "ابدأ اللعبة"},
            )

            doc_yml = handler.load(f"{pak_path}::MyGame/Content/Dialogue/en/Intro.yml")
            self.assertEqual(doc_yml.raw_data["inner_mode"], "yaml")
            self.assertEqual([k for _, k, _ in doc_yml.entries], ["dialogue.greeting"])
            handler.save(
                f"{pak_path}::MyGame/Content/Dialogue/en/Intro.yml",
                doc_yml,
                {doc_yml.entries[0][0]: "مرحباً {Hero}!"},
            )

            doc_json = handler.load(f"{pak_path}::MyGame/Content/Localization/Game/en/Meta.json")
            self.assertEqual(doc_json.raw_data["inner_mode"], "json")
            self.assertEqual(doc_json.entries[0][2], "Echoes")

            with self.assertRaises(ValueError):
                handler.load(f"{pak_path}::MyGame/Content/Data/Unsupported.xml")

            staged_yaml = os.path.join(
                tmp, ".arabic_translator", "unreal_staging", "files",
                "MyGame", "Content", "Localization", "Game", "en", "Menu.yaml",
            )
            staged_yml = os.path.join(
                tmp, ".arabic_translator", "unreal_staging", "files",
                "MyGame", "Content", "Dialogue", "en", "Intro.yml",
            )
            self.assertTrue(os.path.isfile(staged_yaml))
            self.assertTrue(os.path.isfile(staged_yml))
            with open(staged_yaml, "r", encoding="utf-8") as f:
                staged_yaml_txt = f.read()
            self.assertIn("# UI localization", staged_yaml_txt)
            self.assertIn("# main button", staged_yaml_txt)
            self.assertIn("count: 3", staged_yaml_txt)
            reloaded_yaml = YamlFormatHandler().load(staged_yaml)
            self.assertEqual(reloaded_yaml.entries[0][2], "ابدأ اللعبة")

            reloaded_yml = YamlFormatHandler().load(staged_yml)
            self.assertEqual(reloaded_yml.entries[0][2], "مرحباً {Hero}!")

    def test_unreal_pak_yaml_yml_end_to_end_pipeline_and_rollback(self):
        from engines.unreal.pak_parser import build_unreal_pak_blob, extract_pak_entry_by_path
        from formats.yaml_handler import YamlFormatHandler

        with tempfile.TemporaryDirectory() as tmp:
            exe_path = os.path.join(tmp, "YamlQuest-Win64-Shipping.exe")
            UnrealEngineTests._build_pe_exe(exe_path, x64=True)
            paks_dir = os.path.join(tmp, "YamlQuest", "Content", "Paks")
            os.makedirs(paks_dir)

            base_pak = os.path.join(paks_dir, "pakchunk0-WindowsNoEditor.pak")
            with open(base_pak, "wb") as f:
                f.write(
                    build_unreal_pak_blob(
                        {
                            "YamlQuest/Content/Localization/Game/en/UI.yaml": (
                                "# Menu strings\n"
                                "ui:\n"
                                '  start: "Start Journey"\n'
                                '  quit: "Exit Game"\n'
                            ).encode("utf-8"),
                            "YamlQuest/Content/Dialogue/en/Quest.yml": (
                                "quest:\n"
                                '  intro: "Speak to the guardian, {PlayerName}."\n'
                            ).encode("utf-8"),
                            "YamlQuest/Content/Dialogue/fr/Quest.yml": (
                                "quest:\n"
                                '  intro: "Parlez au gardien."\n'
                            ).encode("utf-8"),
                        },
                        mount_point="../../../",
                        version=8,
                    )
                )
            base_sha = compute_sha256(base_pak)

            def fake_ep(batch_text, ep, sl, tl):
                return (
                    batch_text.replace("Start Journey", "ابدأ الرحلة")
                    .replace("Exit Game", "إنهاء اللعبة")
                    .replace("Speak to the guardian,", "تحدث إلى الحارس،")
                )

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            self.assertEqual(compute_sha256(base_pak), base_sha)

            sel_rels = {c.relative_path.replace("\\", "/") for c in rep.selected_files}
            self.assertTrue(any(r.endswith("en/UI.yaml") for r in sel_rels))
            self.assertTrue(any(r.endswith("en/Quest.yml") for r in sel_rels))
            self.assertFalse(any("fr/Quest.yml" in r for r in sel_rels))

            mod_pak = os.path.join(paks_dir, "~mods", "z_ArabicL10n_P.pak")
            self.assertTrue(os.path.isfile(mod_pak))

            ext_yaml = extract_pak_entry_by_path(mod_pak, "YamlQuest/Content/Localization/Game/en/UI.yaml")
            ext_yml = extract_pak_entry_by_path(mod_pak, "YamlQuest/Content/Dialogue/en/Quest.yml")
            self.assertIsNotNone(ext_yaml)
            self.assertIsNotNone(ext_yml)

            yaml_tmp = os.path.join(tmp, "_check_ui.yaml")
            yml_tmp = os.path.join(tmp, "_check_quest.yml")
            with open(yaml_tmp, "wb") as f:
                f.write(ext_yaml[2])
            with open(yml_tmp, "wb") as f:
                f.write(ext_yml[2])

            parsed_yaml = {k: v for _, k, v in YamlFormatHandler().load(yaml_tmp).entries}
            parsed_yml = {k: v for _, k, v in YamlFormatHandler().load(yml_tmp).entries}
            self.assertEqual(parsed_yaml["ui.start"], "ابدأ الرحلة")
            self.assertEqual(parsed_yaml["ui.quit"], "إنهاء اللعبة")
            self.assertEqual(parsed_yml["quest.intro"], "تحدث إلى الحارس، {PlayerName}.")

            os.remove(yaml_tmp)
            os.remove(yml_tmp)

            _, warnings = AutoTranslatorPipeline.rollback(tmp)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(mod_pak))
            self.assertEqual(compute_sha256(base_pak), base_sha)

    def test_godot_pck_root_resolution_layouts_without_mocks(self):
        from engines.godot.pck_parser import build_godot_pck_blob
        from formats.godot_pck_handler import GodotPckFormatHandler

        po_bytes = (
            'msgid ""\n'
            'msgstr ""\n'
            '"Language: en\\n"\n\n'
            'msgid "PLAY"\n'
            'msgstr "Play Game"\n'
        ).encode("utf-8")
        pck_blob = build_godot_pck_blob(
            {"res://locale/strings.en.po": po_bytes},
            pack_format_version=2,
            godot_version=(4, 2, 1),
        )
        handler = GodotPckFormatHandler()

        with tempfile.TemporaryDirectory() as tmp:
            # 1. مشروع Godot يحتوي project.godot في الجذر وحزمة .pck داخل مجلد فرعي (كان يفشل سابقاً)
            proj_root = os.path.join(tmp, "ProjectLayout")
            exports_sub = os.path.join(proj_root, "exports")
            os.makedirs(exports_sub)
            with open(os.path.join(proj_root, "project.godot"), "w", encoding="utf-8") as f:
                f.write('config_version=5\n[application]\nconfig/name="ProjLayout"\n')
            proj_pck = os.path.join(exports_sub, "ExtraPack.pck")
            with open(proj_pck, "wb") as f:
                f.write(pck_blob)

            doc = handler.load(f"{proj_pck}::res://locale/strings.en.po")
            handler.save(f"{proj_pck}::res://locale/strings.en.po", doc, {doc.entries[0][0]: "ابدأ اللعبة"})
            self.assertTrue(os.path.isfile(os.path.join(proj_root, "arabic_l10n", "locale", "strings.ar.po")))
            self.assertFalse(os.path.exists(os.path.join(exports_sub, "arabic_l10n")))

            # 2. لعبة مصدّرة: التنفيذي في الجذر وحزمة .pck مطابقة الاسم في مجلد فرعي (كان يفشل سابقاً)
            exe_root = os.path.join(tmp, "ExeAndSubPck")
            data_sub = os.path.join(exe_root, "data")
            os.makedirs(data_sub)
            with open(os.path.join(exe_root, "Adventure.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            sub_pck = os.path.join(data_sub, "Adventure.pck")
            with open(sub_pck, "wb") as f:
                f.write(pck_blob)

            doc2 = handler.load(f"{sub_pck}::res://locale/strings.en.po")
            handler.save(f"{sub_pck}::res://locale/strings.en.po", doc2, {doc2.entries[0][0]: "ابدأ اللعبة"})
            self.assertTrue(os.path.isfile(os.path.join(exe_root, "arabic_l10n", "locale", "strings.ar.po")))
            self.assertFalse(os.path.exists(os.path.join(data_sub, "arabic_l10n")))

            # 3. مجلد تصدير كامل (exe + pck معاً) داخل مجلد مشروع أب يحتوي project.godot -> يجب أن يبقى الجذر في مجلد التصدير
            parent_proj = os.path.join(tmp, "ParentWithBuild")
            build_dir = os.path.join(parent_proj, "build")
            os.makedirs(build_dir)
            with open(os.path.join(parent_proj, "project.godot"), "w", encoding="utf-8") as f:
                f.write('config_version=5\n')
            with open(os.path.join(build_dir, "Exported.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            build_pck = os.path.join(build_dir, "Exported.pck")
            with open(build_pck, "wb") as f:
                f.write(pck_blob)

            doc3 = handler.load(f"{build_pck}::res://locale/strings.en.po")
            handler.save(f"{build_pck}::res://locale/strings.en.po", doc3, {doc3.entries[0][0]: "ابدأ اللعبة"})
            self.assertTrue(os.path.isfile(os.path.join(build_dir, "arabic_l10n", "locale", "strings.ar.po")))
            self.assertFalse(os.path.exists(os.path.join(parent_proj, "arabic_l10n")))

            # 4. تنفيذي غير ذي صلة في مجلد أب لا يجذب حزمة مستقلة باسم مختلف
            shared_parent = os.path.join(tmp, "SharedGamesDir")
            mod_dir = os.path.join(shared_parent, "StandaloneMod")
            os.makedirs(mod_dir)
            with open(os.path.join(shared_parent, "OtherGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            solo_pck = os.path.join(mod_dir, "CustomPack.pck")
            with open(solo_pck, "wb") as f:
                f.write(pck_blob)

            doc4 = handler.load(f"{solo_pck}::res://locale/strings.en.po")
            handler.save(f"{solo_pck}::res://locale/strings.en.po", doc4, {doc4.entries[0][0]: "ابدأ اللعبة"})
            self.assertTrue(os.path.isfile(os.path.join(mod_dir, "arabic_l10n", "locale", "strings.ar.po")))
            self.assertFalse(os.path.exists(os.path.join(shared_parent, "arabic_l10n")))

    def test_godot_pipeline_end_to_end_split_exe_and_nested_pck_layout(self):
        from engines.godot.pck_parser import build_godot_pck_blob, parse_pck_header_and_entries

        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            packs_dir = os.path.join(tmp, "packs")
            os.makedirs(bin_dir)
            os.makedirs(packs_dir)

            with open(os.path.join(bin_dir, "Launcher.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())

            pck_path = os.path.join(packs_dir, "data0.pck")
            proj_txt = (
                'config_version=5\n'
                '[application]\n'
                'config/name="SplitLayoutGame"\n'
                '[internationalization]\n'
                'locale/translations=PackedStringArray("res://translations/ui.en.po")\n'
            ).encode("utf-8")
            po_bytes = (
                'msgid ""\n'
                'msgstr ""\n'
                '"Content-Type: text/plain; charset=UTF-8\\n"\n'
                '"Language: en\\n"\n\n'
                'msgid "MENU_START"\n'
                'msgstr "Start Adventure"\n'
            ).encode("utf-8")
            with open(pck_path, "wb") as f:
                f.write(
                    build_godot_pck_blob(
                        {
                            "res://project.godot": proj_txt,
                            "res://translations/ui.en.po": po_bytes,
                        },
                        pack_format_version=2,
                        godot_version=(4, 2, 1),
                    )
                )
            orig_pck_sha = compute_sha256(pck_path)

            def fake_ep(batch_text, ep, sl, tl):
                return batch_text.replace("Start Adventure", "ابدأ المغامرة")

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(tmp)
            self.assertTrue(rep.success, rep.errors)
            self.assertEqual(rep.engine.engine_name, "Godot 4")
            self.assertEqual(rep.engine.architecture, "x64")
            self.assertEqual(rep.engine.game_dir, os.path.abspath(tmp))

            # المخرجات في جذر اللعبة (tmp) وليس داخل packs/
            out_po = os.path.join(tmp, "arabic_l10n", "translations", "ui.ar.po")
            override_cfg = os.path.join(tmp, "override.cfg")
            patch_pck = os.path.join(tmp, "arabic_l10n", "arabic_patch.pck")
            self.assertTrue(os.path.isfile(out_po))
            self.assertTrue(os.path.isfile(override_cfg))
            self.assertTrue(os.path.isfile(patch_pck))
            self.assertFalse(os.path.exists(os.path.join(packs_dir, "arabic_l10n")))
            self.assertEqual(compute_sha256(pck_path), orig_pck_sha)

            with open(out_po, "r", encoding="utf-8") as f:
                self.assertIn("ابدأ المغامرة", f.read())
            with open(override_cfg, "r", encoding="utf-8") as f:
                cfg_txt = f.read()
            self.assertIn("res://arabic_l10n/translations/ui.ar.po", cfg_txt)
            patch_info = parse_pck_header_and_entries(patch_pck)
            self.assertIsNotNone(patch_info)
            self.assertTrue(any(e.res_path == "res://arabic_l10n/translations/ui.ar.po" for e in patch_info.entries))

            _, warnings = AutoTranslatorPipeline.rollback(tmp)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(out_po))
            self.assertFalse(os.path.exists(override_cfg))
            self.assertFalse(os.path.exists(patch_pck))
            self.assertEqual(compute_sha256(pck_path), orig_pck_sha)

    def test_godot_root_precedence_and_nested_container_discovery(self):
        from engines.godot.detector import GodotDetector, find_godot_containers
        from engines.godot.pck_parser import build_godot_pck_blob, build_godot_translation_rsrc_blob
        from formats.godot_pck_handler import GodotPckFormatHandler

        po_bytes = (
            'msgid ""\n'
            'msgstr ""\n'
            '"Language: en\\n"\n\n'
            'msgid "HELLO"\n'
            'msgstr "Hello World"\n'
        ).encode("utf-8")
        pck_v2 = build_godot_pck_blob(
            {"res://locale/strings.en.po": po_bytes},
            pack_format_version=2,
            godot_version=(4, 2, 1),
        )
        handler = GodotPckFormatHandler()

        with tempfile.TemporaryDirectory() as tmp:
            # 1. .arabic_translator في المجلد الأب لا يتجاوز بناءً مصدّراً مكتفياً بذاته في build/
            parent_proj = os.path.join(tmp, "ParentWithState")
            build_dir = os.path.join(parent_proj, "build")
            os.makedirs(os.path.join(parent_proj, ".arabic_translator"))
            os.makedirs(build_dir)
            with open(os.path.join(build_dir, "Exported.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            build_pck = os.path.join(build_dir, "Exported.pck")
            with open(build_pck, "wb") as f:
                f.write(pck_v2)

            doc1 = handler.load(f"{build_pck}::res://locale/strings.en.po")
            handler.save(f"{build_pck}::res://locale/strings.en.po", doc1, {doc1.entries[0][0]: "مرحباً"})
            self.assertTrue(os.path.isfile(os.path.join(build_dir, "arabic_l10n", "locale", "strings.ar.po")))
            self.assertFalse(os.path.exists(os.path.join(parent_proj, "arabic_l10n")))

            # 2. .arabic_translator في مجلد أب بعيد لا يتجاوز مشروع project.godot أقرب
            outer_state = os.path.join(tmp, "OuterWithState")
            nested_proj = os.path.join(outer_state, "MyProj")
            nested_packs = os.path.join(nested_proj, "packs")
            os.makedirs(os.path.join(outer_state, ".arabic_translator"))
            os.makedirs(nested_packs)
            with open(os.path.join(nested_proj, "project.godot"), "w", encoding="utf-8") as f:
                f.write('config_version=5\n[application]\nconfig/name="NestedProj"\n')
            nested_proj_pck = os.path.join(nested_packs, "Extra.pck")
            with open(nested_proj_pck, "wb") as f:
                f.write(pck_v2)

            doc2 = handler.load(f"{nested_proj_pck}::res://locale/strings.en.po")
            handler.save(f"{nested_proj_pck}::res://locale/strings.en.po", doc2, {doc2.entries[0][0]: "مرحباً"})
            self.assertTrue(os.path.isfile(os.path.join(nested_proj, "arabic_l10n", "locale", "strings.ar.po")))
            self.assertFalse(os.path.exists(os.path.join(outer_state, "arabic_l10n")))

            # 3. override.cfg في الأب مقابل مجلد التنفيذي: وجود التنفيذي في bin/ يثبت الجذر في bin/
            parent_cfg = os.path.join(tmp, "ParentOverrideVsExe")
            exe_sub = os.path.join(parent_cfg, "bin")
            os.makedirs(exe_sub)
            with open(os.path.join(parent_cfg, "override.cfg"), "w", encoding="utf-8") as f:
                f.write("[display]\n")
            with open(os.path.join(exe_sub, "Game.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            exe_pck = os.path.join(exe_sub, "Game.pck")
            with open(exe_pck, "wb") as f:
                f.write(pck_v2)

            doc3 = handler.load(f"{exe_pck}::res://locale/strings.en.po")
            handler.save(f"{exe_pck}::res://locale/strings.en.po", doc3, {doc3.entries[0][0]: "مرحباً"})
            self.assertTrue(os.path.isfile(os.path.join(exe_sub, "arabic_l10n", "locale", "strings.ar.po")))
            self.assertFalse(os.path.exists(os.path.join(parent_cfg, "arabic_l10n")))

            # 4. تصدير Godot 4 متداخل داخل مشروع مصدر Godot 3 لا يتجاوز إصدار المشروع ولا يكرر الترجمات ولا يحقن arabic_patch.pck
            src_proj = os.path.join(tmp, "Godot3SourceWithExport")
            src_tr = os.path.join(src_proj, "translations")
            src_exp = os.path.join(src_proj, "exports")
            os.makedirs(src_tr)
            os.makedirs(src_exp)
            with open(os.path.join(src_proj, "project.godot"), "w", encoding="utf-8") as f:
                f.write('config_version=4\n[application]\nconfig/name="SrcG3"\n')
            with open(os.path.join(src_tr, "en.po"), "wb") as f:
                f.write(po_bytes)
            compiled_trans = build_godot_translation_rsrc_blob(
                locale="en",
                messages={"HELLO": "Hello World"},
                optimized=False,
                godot_version=(4, 2, 1),
            )
            with open(os.path.join(src_exp, "WinExport.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(src_exp, "WinExport.pck"), "wb") as f:
                f.write(
                    build_godot_pck_blob(
                        {"res://.godot/imported/en.translation": compiled_trans},
                        pack_format_version=2,
                        godot_version=(4, 2, 1),
                    )
                )

            pcks_found, exes_found, proj_found = find_godot_containers(src_proj)
            self.assertEqual(pcks_found, [])
            self.assertEqual(exes_found, [])
            self.assertEqual(proj_found, os.path.join(src_proj, "project.godot"))

            def fake_ep(batch_text, ep, sl, tl):
                return batch_text.replace("Hello World", "مرحباً بالعالم")

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(src_proj)
            self.assertTrue(rep.success, rep.errors)
            self.assertEqual(rep.engine.engine_name, "Godot 3")
            self.assertEqual(len(rep.selected_files), 1)
            self.assertEqual(rep.selected_files[0].format_name, "po")
            self.assertFalse(os.path.exists(os.path.join(src_proj, "arabic_l10n", "arabic_patch.pck")))
            AutoTranslatorPipeline.rollback(src_proj)

            # 5. مجلد أب يغلف مجلد لعبة مستقلة (exe+pck أو exe مدمج أو project.godot أو pck منفرد) لا يُكتشف كلعبة Godot
            def _write_bytes(path: str, data: bytes) -> None:
                with open(path, "wb") as f:
                    f.write(data)

            def _write_text(path: str, text: str) -> None:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)

            detector = GodotDetector()
            for case_name, files_spec in (
                ("WrapperExePck", [("G.exe", _make_minimal_x64_pe()), ("G.pck", pck_v2)]),
                (
                    "WrapperEmbeddedExe",
                    [
                        (
                            "Embedded.exe",
                            build_godot_pck_blob(
                                {"res://locale/strings.en.po": po_bytes},
                                pack_format_version=2,
                                godot_version=(4, 2, 1),
                                embedded_exe_prefix=_make_minimal_x64_pe(),
                            ),
                        )
                    ],
                ),
                ("WrapperNestedProj", [("project.godot", "config_version=5\n"), ("data.pck", pck_v2)]),
                ("WrapperSoloPck", [("Solo.pck", pck_v2)]),
            ):
                wrapper = os.path.join(tmp, case_name)
                sub_game = os.path.join(wrapper, "NestedGame")
                os.makedirs(sub_game)
                for fname, payload in files_spec:
                    fpath = os.path.join(sub_game, fname)
                    if isinstance(payload, bytes):
                        _write_bytes(fpath, payload)
                    else:
                        _write_text(fpath, payload)
                info = detector.detect(wrapper)
                self.assertFalse(info.supported, f"{case_name} should not detect wrapper dir as Godot game")


def _build_minimal_dotnet_pe_with_us(us_strings: list) -> bytes:
    """بناء ملف .NET PE DLL صالح يحتوي مجرى #US للاختبارات الآلية."""
    us_buf = bytearray(b"\x00")
    for s in us_strings:
        raw = s.encode("utf-16-le")
        blen = len(raw) + 1
        if blen < 0x80:
            us_buf.append(blen)
        else:
            us_buf.extend(struct.pack(">H", 0x8000 | blen))
        us_buf.extend(raw)
        us_buf.append(0x00)
    while len(us_buf) % 4 != 0:
        us_buf.append(0)

    ver_bytes = b"v4.0"
    stream_hdr = struct.pack("<II", 20 + 4 + 12, len(us_buf)) + b"#US\x00"
    # streams_hdr at meta_off + 20: flags(2) + num_streams(2) + stream_hdr(12) = 16
    s_off = 20 + 4 + 12
    stream_hdr = struct.pack("<II", s_off, len(us_buf)) + b"#US\x00"
    meta_hdr = (
        b"BSJB"
        + b"\x00" * 8
        + struct.pack("<I", len(ver_bytes))
        + ver_bytes
        + struct.pack("<HH", 0, 1)
        + stream_hdr
        + bytes(us_buf)
    )

    total_size = 0x250 + len(meta_hdr) + 64
    buf = bytearray(total_size)
    buf[0:2] = b"MZ"
    pe_off = 0x80
    struct.pack_into("<I", buf, 0x3C, pe_off)
    buf[pe_off : pe_off + 4] = b"PE\x00\x00"
    struct.pack_into("<H", buf, pe_off + 6, 1)  # num_sections
    opt_hdr_size = 0xE0
    struct.pack_into("<H", buf, pe_off + 20, opt_hdr_size)
    opt_off = pe_off + 24
    struct.pack_into("<H", buf, opt_off, 0x10B)  # PE32
    clr_dir_off = opt_off + 96 + 14 * 8
    struct.pack_into("<II", buf, clr_dir_off, 0x2000, 72)

    sec_table_off = opt_off + opt_hdr_size
    struct.pack_into("<IIII", buf, sec_table_off + 8, 0x2000, 0x2000, 0x2000, 0x200)

    # CLR header at 0x200 (RVA 0x2000) -> meta_rva at +8 = 0x2050 (file 0x250)
    struct.pack_into("<II", buf, 0x200 + 8, 0x2050, len(meta_hdr))
    buf[0x250 : 0x250 + len(meta_hdr)] = meta_hdr
    return bytes(buf)


def _build_addressables_catalog_json(
    internal_ids: list,
    resource_types: list,
    entries_spec: list,
    buckets_spec: list,
) -> dict:
    """بناء بنية catalog.json بصيغة Addressables القياسية مع m_EntryDataString وm_BucketDataString."""
    entry_buf = bytearray(struct.pack("<I", len(entries_spec)))
    for row in entries_spec:
        entry_buf.extend(struct.pack("<7i", *row))

    bucket_buf = bytearray(struct.pack("<I", len(buckets_spec)))
    for data_off, e_indices in buckets_spec:
        bucket_buf.extend(struct.pack("<ii", data_off, len(e_indices)))
        for e_idx in e_indices:
            bucket_buf.extend(struct.pack("<i", e_idx))

    return {
        "m_LocatorId": "AddressablesMainContentCatalog",
        "m_InternalIds": internal_ids,
        "m_resourceTypes": [
            {"m_AssemblyName": "UnityEngine.CoreModule", "m_ClassName": cls_name}
            for cls_name in resource_types
        ],
        "m_EntryDataString": base64.b64encode(bytes(entry_buf)).decode("ascii"),
        "m_BucketDataString": base64.b64encode(bytes(bucket_buf)).decode("ascii"),
    }


class UnitySmartDeepScanAndIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_assets_extraction_cache()

    def test_serialized_file_index_v22_v17_monobehaviour_textmesh_and_media_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1. SerializedFile v22 يحتوي TextAsset (49) + ScriptableObject dialogue (114) + TextMesh (102)
            ta_payload = _pack_aligned_string("MenuStrings") + _pack_aligned_string(
                json.dumps({"play": "Start Game", "opt": "Graphics Options", "quit": "Exit to Desktop"})
            )
            mb_so_payload = (
                struct.pack("<iq", 0, 0)  # m_GameObject (ScriptableObject: pathID=0)
                + struct.pack("<i", 1)
                + struct.pack("<iq", 0, 99)  # m_Script
                + _pack_aligned_string("ElderDialogue")
                + _pack_aligned_string("Welcome to the ancient citadel.")
                + _pack_aligned_string("Please bring me the silver key.")
                + _pack_aligned_string("The northern gate opens at dawn.")
            )
            tm_payload = struct.pack("<iq", 0, 42) + _pack_aligned_string("Press Space to Begin")
            blob_v22 = build_unity_serialized_file_blob(
                [
                    (101, 49, ta_payload),
                    (102, 114, mb_so_payload),
                    (103, 102, tm_payload),
                    (104, 28, b"\xFF\x00" * 64),  # Texture2D
                ],
                unity_version="2022.3.15f1",
                version=22,
            )
            path_v22 = os.path.join(tmp, "resources_v22.assets")
            with open(path_v22, "wb") as f:
                f.write(blob_v22)

            self.assertEqual(extract_unity_version_from_file(path_v22), "2022.3.15f1")
            sf_idx = parse_serialized_file_index_from_file(path_v22)
            self.assertIsNotNone(sf_idx)
            self.assertEqual(sf_idx.version, 22)
            self.assertEqual(len(sf_idx.objects), 4)

            blocks_v22 = extract_assets_localization_blocks(path_v22)
            kinds_v22 = {b.block_kind for b in blocks_v22}
            self.assertIn("TextAsset:json", kinds_v22)
            self.assertIn("MonoBehaviour:dialogue", kinds_v22)
            self.assertIn("SceneText:binary", kinds_v22)
            all_vals = {v for b in blocks_v22 for _, _, v in b.entries}
            self.assertIn("Welcome to the ancient citadel.", all_vals)
            self.assertIn("Press Space to Begin", all_vals)
            self.assertIn("Start Game", all_vals)

            # 2. SerializedFile v17 (ترويسة 20 بايت)
            blob_v17 = build_unity_serialized_file_blob(
                [(201, 114, mb_so_payload), (202, 28, b"\x00" * 32)],
                unity_version="2019.4.40f1",
                version=17,
            )
            path_v17 = os.path.join(tmp, "shared_v17.assets")
            with open(path_v17, "wb") as f:
                f.write(blob_v17)

            self.assertEqual(extract_unity_version_from_file(path_v17), "2019.4.40f1")
            blocks_v17 = extract_assets_localization_blocks(path_v17)
            self.assertTrue(any(b.block_kind == "MonoBehaviour:dialogue" for b in blocks_v17))

            # 3. ملف وسائط فقط (Texture2D / AudioClip / Mesh) يحتوي بايتات مضللة: يتم تخطيه من الفهرس فوراً
            decoy_media_payload = (
                b"\x00" * 16
                + _pack_aligned_string("This looks like a real English sentence inside texture metadata!")
                + _pack_aligned_string("Another decoy sentence inside audio metadata!")
                + _pack_aligned_string("Third decoy sentence inside mesh metadata!")
            )
            media_blob = build_unity_serialized_file_blob(
                [
                    (301, 28, decoy_media_payload),
                    (302, 83, decoy_media_payload),
                    (303, 43, decoy_media_payload),
                ],
                unity_version="2022.3.15f1",
                version=22,
            )
            media_path = os.path.join(tmp, "media_only.assets")
            with open(media_path, "wb") as f:
                f.write(media_blob)
            self.assertEqual(extract_assets_localization_blocks(media_path), [])

    def test_addressables_catalog_decoding_priority_and_pure_media_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            unity_dir = os.path.join(tmp, "CatalogGame")
            data_dir = os.path.join(unity_dir, "MyGame_Data")
            managed = os.path.join(data_dir, "Managed")
            aa_dir = os.path.join(data_dir, "StreamingAssets", "aa")
            aa_win = os.path.join(aa_dir, "StandaloneWindows64")
            os.makedirs(managed)
            os.makedirs(aa_win)
            with open(os.path.join(unity_dir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")

            cat_dict = _build_addressables_catalog_json(
                internal_ids=[
                    "{Runtime}/StandaloneWindows64/loc_en.bundle",
                    "{Runtime}/StandaloneWindows64/textures_only.bundle",
                    "Assets/Loc/UI_English.asset",
                    "Assets/Art/Hero.png",
                    "Assets/Audio/Bgm.wav",
                ],
                resource_types=[
                    "UnityEngine.ResourceManagement.ResourceProviders.IAssetBundleResource",
                    "UnityEngine.Localization.Tables.StringTable",
                    "UnityEngine.Texture2D",
                    "UnityEngine.AudioClip",
                ],
                entries_spec=[
                    (0, 0, -1, 0, 0, 0, 0),
                    (1, 0, -1, 0, 0, 1, 0),
                    (2, 1, 0, 1, 0, 2, 1),
                    (3, 1, 1, 2, 0, 3, 2),
                    (4, 1, 1, 3, 0, 4, 3),
                ],
                buckets_spec=[
                    (0, [0]),
                    (4, [1]),
                ],
            )
            with open(os.path.join(aa_dir, "catalog.json"), "w", encoding="utf-8") as f:
                json.dump(cat_dict, f)

            loc_bundle = os.path.join(aa_win, "loc_en.bundle")
            tex_bundle = os.path.join(aa_win, "textures_only.bundle")
            unmapped_bundle = os.path.join(aa_win, "unmapped_extra.bundle")

            loc_blob = build_unity_localization_string_table_blob(
                "UI_English",
                [(1, "New Game"), (2, "Options"), (3, "Return to Main Menu")],
            )
            extra_blob = build_unity_text_asset_blob(
                "ExtraText",
                json.dumps({"tip1": "Explore the dungeon", "tip2": "Find the hidden chest", "tip3": "Defeat the boss"}),
            )
            with open(loc_bundle, "wb") as f:
                f.write(build_unityfs_bundle_blob([("CAB-loc", b"\x00\x00\x00\x00" + loc_blob)]))
            # يحتوي textures_only.bundle على نصوص مضللة، لكن catalog يثبت أنه وسائط فقط فيُتخطى تماماً
            with open(tex_bundle, "wb") as f:
                f.write(build_unityfs_bundle_blob([("CAB-tex", b"\x00\x00\x00\x00" + extra_blob)]))
            with open(unmapped_bundle, "wb") as f:
                f.write(build_unityfs_bundle_blob([("CAB-ext", b"\x00\x00\x00\x00" + extra_blob)]))

            eng = UnityDetector().detect(unity_dir)
            cat_info = parse_addressables_catalog(eng)
            self.assertIn("loc_en.bundle", cat_info.priority_bundles)
            self.assertIn("textures_only.bundle", cat_info.skip_bundles)
            self.assertNotIn("unmapped_extra.bundle", cat_info.skip_bundles)

            cands, _ = UnityLanguageScanner().scan(eng, deep_scan=True)
            cand_names = {os.path.basename(c.relative_path) for c in cands}
            self.assertIn("loc_en.bundle", cand_names)
            self.assertIn("unmapped_extra.bundle", cand_names)
            self.assertNotIn("textures_only.bundle", cand_names)
            self.assertEqual(os.path.basename(cands[0].relative_path), "loc_en.bundle")

    def test_multi_asset_level_merging_and_sibling_bundle_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            unity_dir = os.path.join(tmp, "MultiAssetGame")
            data_dir = os.path.join(unity_dir, "MyGame_Data")
            managed = os.path.join(data_dir, "Managed")
            aa_win = os.path.join(data_dir, "StreamingAssets", "aa", "StandaloneWindows64")
            os.makedirs(managed)
            os.makedirs(aa_win)
            with open(os.path.join(unity_dir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")

            res_path = os.path.join(data_dir, "resources.assets")
            lvl1_path = os.path.join(data_dir, "level1")
            lvl2_path = os.path.join(data_dir, "level2")
            loc_en_path = os.path.join(aa_win, "loc_en.bundle")
            loc_fr_path = os.path.join(aa_win, "loc_fr.bundle")

            with open(res_path, "wb") as f:
                f.write(
                    build_unity_serialized_file_blob(
                        [
                            (
                                1,
                                49,
                                _pack_aligned_string("English")
                                + _pack_aligned_string(
                                    json.dumps(
                                        {
                                            "play": "New Game",
                                            "opt": "Options",
                                            "quit": "Quit",
                                        }
                                    )
                                ),
                            )
                        ]
                    )
                )
            with open(lvl1_path, "wb") as f:
                f.write(
                    build_unity_serialized_file_blob(
                        [
                            (
                                2,
                                114,
                                struct.pack("<iq", 0, 0)
                                + struct.pack("<i", 1)
                                + struct.pack("<iq", 0, 10)
                                + _pack_aligned_string("Level1Dlg")
                                + _pack_aligned_string("Enter the forest of shadows.")
                                + _pack_aligned_string("Watch out for hidden traps.")
                                + _pack_aligned_string("Find the ancient lantern."),
                            )
                        ]
                    )
                )
            with open(lvl2_path, "wb") as f:
                f.write(
                    build_unity_serialized_file_blob(
                        [
                            (
                                3,
                                114,
                                struct.pack("<iq", 0, 0)
                                + struct.pack("<i", 1)
                                + struct.pack("<iq", 0, 11)
                                + _pack_aligned_string("Level2Dlg")
                                + _pack_aligned_string("Cross the frozen bridge now.")
                                + _pack_aligned_string("Speak with the mountain guardian.")
                                + _pack_aligned_string("Claim the crown of ice."),
                            )
                        ]
                    )
                )
            en_table = build_unity_localization_string_table_blob(
                "EN_Bundle", [(10, "Continue"), (11, "Audio Settings"), (12, "Back")]
            )
            fr_table = build_unity_localization_string_table_blob(
                "FR_Bundle", [(10, "Continuer"), (11, "Options Audio"), (12, "Retour")]
            )
            with open(loc_en_path, "wb") as f:
                f.write(build_unityfs_bundle_blob([("CAB-en", b"\x00\x00\x00\x00" + en_table)]))
            with open(loc_fr_path, "wb") as f:
                f.write(build_unityfs_bundle_blob([("CAB-fr", b"\x00\x00\x00\x00" + fr_table)]))

            self.assertTrue(is_sibling_lang_asset("MyGame_Data/StreamingAssets/aa/StandaloneWindows64/loc_fr.bundle"))
            self.assertFalse(is_sibling_lang_asset("MyGame_Data/StreamingAssets/aa/StandaloneWindows64/loc_en.bundle"))

            orig_hashes = {p: compute_sha256(p) for p in (res_path, lvl1_path, lvl2_path, loc_en_path, loc_fr_path)}

            def fake_ep(batch_text, ep, sl, tl):
                return "\n".join("عربي:" + ln for ln in batch_text.splitlines())

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(unity_dir, skip_inject=True)
            self.assertTrue(rep.success, rep.errors)

            selected_names = {os.path.basename(c.relative_path) for c in rep.selected_files}
            self.assertEqual(selected_names, {"resources.assets", "level1", "level2", "loc_en.bundle"})
            self.assertNotIn("loc_fr.bundle", selected_names)

            override_path = os.path.join(unity_dir, "ArabicOverride.json")
            self.assertTrue(os.path.isfile(override_path))
            with open(override_path, "r", encoding="utf-8") as f:
                ov = json.load(f)
            self.assertIn("New Game", ov["by_source"])
            self.assertIn("Enter the forest of shadows.", ov["by_source"])
            self.assertIn("Cross the frozen bridge now.", ov["by_source"])
            self.assertIn("Audio Settings", ov["by_source"])
            self.assertNotIn("Continuer", ov["by_source"])
            self.assertGreaterEqual(len(ov.get("source_assets", [])), 4)

            for p, h in orig_hashes.items():
                self.assertEqual(compute_sha256(p), h)

            _, warnings = AutoTranslatorPipeline.rollback(unity_dir)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(override_path))

    def test_deep_scan_default_vs_fast_scan_and_duplicate_suppression(self):
        with tempfile.TemporaryDirectory() as tmp:
            def _make_mixed_game(name: str, same_content: bool) -> tuple:
                gdir = os.path.join(tmp, name)
                data_dir = os.path.join(gdir, "MyGame_Data")
                managed = os.path.join(data_dir, "Managed")
                loc_dir = os.path.join(data_dir, "StreamingAssets", "Localization")
                os.makedirs(managed)
                os.makedirs(loc_dir)
                with open(os.path.join(gdir, "MyGame.exe"), "wb") as f:
                    f.write(_make_minimal_x64_pe())
                with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                    f.write(b"MZ")

                ext_payload = {
                    "play": "New Game",
                    "cont": "Continue",
                    "opt": "Options",
                    "audio": "Audio Settings",
                    "quit": "Quit",
                }
                en_json = os.path.join(loc_dir, "English.json")
                with open(en_json, "w", encoding="utf-8") as f:
                    json.dump(ext_payload, f)

                if same_content:
                    asset_payload = ext_payload
                    asset_name = "resources.assets"
                else:
                    asset_payload = {
                        "lvl_title": "Chapter One: Awakening",
                        "lvl_hint": "Press Tab to open map",
                        "lvl_npc": "Welcome to the harbor",
                        "lvl_exit": "Return to Main Menu",
                    }
                    asset_name = "level1"

                asset_path = os.path.join(data_dir, asset_name)
                with open(asset_path, "wb") as f:
                    f.write(
                        build_unity_serialized_file_blob(
                            [
                                (
                                    10,
                                    49,
                                    _pack_aligned_string("SceneExtra")
                                    + _pack_aligned_string(json.dumps(asset_payload)),
                                )
                            ]
                        )
                    )
                return gdir, en_json, asset_path

            def fake_ep(batch_text, ep, sl, tl):
                return "\n".join("ع:" + ln for ln in batch_text.splitlines())

            # 1. لعبة مختلطة (ملف خارجي + level1 مختلف): الافتراضي (deep_scan=True) يعرّب الاثنين
            mixed_dir, mixed_json, _ = _make_mixed_game("MixedDeep", same_content=False)
            pipe_deep = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep_deep = pipe_deep.translate(mixed_dir, skip_inject=True)
            self.assertTrue(rep_deep.success, rep_deep.errors)
            sel_deep = {os.path.basename(c.relative_path) for c in rep_deep.selected_files}
            self.assertEqual(sel_deep, {"English.json", "level1"})
            self.assertTrue(os.path.isfile(os.path.join(mixed_dir, "ArabicOverride.json")))
            self.assertTrue(os.path.isfile(mixed_json + ".bak"))
            AutoTranslatorPipeline.rollback(mixed_dir)
            self.assertFalse(os.path.exists(os.path.join(mixed_dir, "ArabicOverride.json")))

            # 2. نفس اللعبة المختلطة مع تعطيل الفحص العميق (deep_scan=False): يتوقف عند English.json
            pipe_fast = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep),
                engine_options={"deep_scan": False},
            )
            rep_fast = pipe_fast.translate(mixed_dir, skip_inject=True)
            self.assertTrue(rep_fast.success, rep_fast.errors)
            sel_fast = {os.path.basename(c.relative_path) for c in rep_fast.selected_files}
            self.assertEqual(sel_fast, {"English.json"})
            self.assertFalse(os.path.exists(os.path.join(mixed_dir, "ArabicOverride.json")))

            # 3. لعبة محتوى مكرر (English.json + resources.assets بنفس النصوص): deep_scan يتجاهل النسخة المكررة
            dup_dir, _, _ = _make_mixed_game("DupGame", same_content=True)
            rep_dup = pipe_deep.translate(dup_dir, skip_inject=True)
            self.assertTrue(rep_dup.success, rep_dup.errors)
            self.assertEqual([os.path.basename(c.relative_path) for c in rep_dup.selected_files], ["English.json"])
            self.assertFalse(os.path.exists(os.path.join(dup_dir, "ArabicOverride.json")))

    def test_code_strings_fallback_deep_scan_and_noise_filtering(self):
        with tempfile.TemporaryDirectory() as tmp:
            gdir = os.path.join(tmp, "CodeOnlyUnity")
            managed = os.path.join(gdir, "MyGame_Data", "Managed")
            os.makedirs(managed)
            with open(os.path.join(gdir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())

            dll_bytes = _build_minimal_dotnet_pe_with_us(
                [
                    "New Game",
                    "Continue",
                    "Options",
                    "Audio Settings",
                    "Press Space to jump",
                    "Return to Main Menu",
                    "NullReferenceException",
                    "Failed to load texture",
                    "UnityEngine.UI.Button",
                    "ui_panel_01",
                    "Error: missing audio clip",
                ]
            )
            asm_path = os.path.join(managed, "Assembly-CSharp.dll")
            with open(asm_path, "wb") as f:
                f.write(dll_bytes)
            asm_sha = compute_sha256(asm_path)

            def fake_ep(batch_text, ep, sl, tl):
                return "\n".join("ع:" + ln for ln in batch_text.splitlines())

            pipe_fast = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep),
                engine_options={"deep_scan": False},
            )
            rep_fast = pipe_fast.scan(gdir)
            self.assertFalse(rep_fast.success)

            pipe_deep = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep),
            )
            rep = pipe_deep.translate(gdir, skip_inject=True)
            self.assertTrue(rep.success, rep.errors)
            self.assertEqual(rep.selected_file.format_name, "unity_code")
            self.assertEqual(compute_sha256(asm_path), asm_sha)

            override_path = os.path.join(gdir, "ArabicOverride.json")
            self.assertTrue(os.path.isfile(override_path))
            with open(override_path, "r", encoding="utf-8") as f:
                ov = json.load(f)
            self.assertIn("Press Space to jump", ov["by_source"])
            self.assertIn("Audio Settings", ov["by_source"])
            self.assertNotIn("NullReferenceException", ov["by_source"])
            self.assertNotIn("Failed to load texture", ov["by_source"])
            self.assertNotIn("UnityEngine.UI.Button", ov["by_source"])

            _, warnings = AutoTranslatorPipeline.rollback(gdir)
            self.assertEqual(warnings, [])
            self.assertFalse(os.path.exists(override_path))
            self.assertEqual(compute_sha256(asm_path), asm_sha)

    def test_inventory_gui_deep_scan_toggle_and_cli_fast_scan(self):
        from main import main as cli_main
        from ui.gui import TranslatorGUI

        with tempfile.TemporaryDirectory() as tmp:
            gdir = os.path.join(tmp, "InventoryAndControlsGame")
            data_dir = os.path.join(gdir, "MyGame_Data")
            managed = os.path.join(data_dir, "Managed")
            loc_dir = os.path.join(data_dir, "StreamingAssets", "Localization")
            aa_dir = os.path.join(data_dir, "StreamingAssets", "aa")
            os.makedirs(managed)
            os.makedirs(loc_dir)
            os.makedirs(aa_dir)

            with open(os.path.join(gdir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(gdir, "EasyAntiCheat_EOS.dll"), "wb") as f:
                f.write(b"MZ")
            for dll in ("Assembly-CSharp.dll", "Unity.Localization.dll", "I2.Localization.dll", "YarnSpinner.dll"):
                with open(os.path.join(managed, dll), "wb") as f:
                    f.write(b"MZ")
            with open(os.path.join(aa_dir, "catalog.json"), "w", encoding="utf-8") as f:
                json.dump({"m_LocatorId": "Catalog", "m_InternalIds": []}, f)
            with open(os.path.join(data_dir, "globalgamemanagers"), "wb") as f:
                f.write(build_unity_serialized_file_blob([(1, 1, b"\x00" * 16)], unity_version="6000.0.12f1", version=22))
            with open(os.path.join(loc_dir, "English.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "play": "New Game",
                        "cont": "Continue",
                        "opt": "Options",
                        "audio": "Audio Settings",
                        "quit": "Quit",
                    },
                    f,
                )
            with open(os.path.join(data_dir, "level1"), "wb") as f:
                f.write(
                    build_unity_serialized_file_blob(
                        [
                            (
                                2,
                                49,
                                _pack_aligned_string("SceneStrings")
                                + _pack_aligned_string(
                                    json.dumps(
                                        {
                                            "chapter": "Chapter One",
                                            "hint": "Press Tab for inventory",
                                            "gate": "Unlock the stone door",
                                        }
                                    )
                                ),
                            )
                        ],
                        unity_version="6000.0.12f1",
                        version=22,
                    )
                )

            pipe = AutoTranslatorPipeline()
            rep = pipe.scan(gdir)
            self.assertTrue(rep.success)
            self.assertEqual(rep.engine.unity_version, "6000.0.12f1")
            self.assertEqual(
                set(rep.engine.localization_systems),
                {"Unity.Localization", "I2.Localization", "Addressables", "YarnSpinner"},
            )
            self.assertTrue(any("EasyAntiCheat" in w for w in rep.warnings))
            self.assertEqual({os.path.basename(c.relative_path) for c in rep.selected_files}, {"English.json", "level1"})

            # التحقق من CLI --fast-scan
            rc = cli_main(["scan", gdir, "--fast-scan"])
            self.assertEqual(rc, 0)

            # التحقق من الواجهة الرسومية: الافتراضي True ويمكن إلغاء تحديده
            try:
                root = tk.Tk()
            except tk.TclError as exc:
                self.skipTest(f"تعذر تهيئة Tkinter في هذه البيئة: {exc}")
            root.withdraw()
            try:
                app = TranslatorGUI(root=root, initial_game_dir="", pipeline=pipe)
                self.assertTrue(app.deep_scan_var.get())
                scan_deep = app.run_scan(gdir)
                self.assertEqual(
                    {os.path.basename(c.relative_path) for c in scan_deep.selected_files},
                    {"English.json", "level1"},
                )

                app.deep_scan_var.set(False)
                scan_fast = app.run_scan(gdir)
                self.assertEqual(
                    [os.path.basename(c.relative_path) for c in scan_fast.selected_files],
                    ["English.json"],
                )
            finally:
                root.destroy()


class PowerLossAndTransactionalSafetyTests(unittest.TestCase):
    def test_wal_per_batch_durability_and_truncated_tail_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_file = os.path.join(tmp, "translation_cache.json")
            # كتابة نسخة أولية صالحة ثم نسخة ثانية لتوليد .prev سليم مع بقاء .wal.jsonl مبتور الطرف
            GoogleBatchTranslator._save_cache_atomic(cache_file, {"Line 1": "ترجمة:Line 1"})
            GoogleBatchTranslator._save_cache_atomic(cache_file, {"Line 1": "ترجمة:Line 1"})

            batch_calls = []

            def failing_caller(batch_text, ep, sl, tl):
                lines = batch_text.splitlines()
                batch_calls.append(lines)
                if len(batch_calls) >= 3:
                    raise RuntimeError("Simulated sudden outage on batch 3")
                return "\n".join(f"ترجمة:{ln}" for ln in lines)

            tr = GoogleBatchTranslator(
                max_chars=5000,
                max_lines=2,
                inter_batch_delay=0.0,
                endpoint_caller=failing_caller,
            )
            all_lines = [f"Line {i}" for i in range(1, 9)]
            with patch("translators.google_batch.time.sleep"):
                with self.assertRaises(RuntimeError):
                    tr.translate_lines(all_lines, cache_path=cache_file)

            # محاكاة تلف الملف الرئيسي أثناء الدمج ووجود سطر مبتور في ذيل WAL
            with open(cache_file, "w", encoding="utf-8") as f:
                f.write("{CORRUPT_POWER_CUT")
            wal_path = cache_file + ".wal.jsonl"
            self.assertTrue(os.path.isfile(wal_path))
            with open(wal_path, "a", encoding="utf-8") as f:
                f.write('[["Line 6", "تر')  # سطر مبتور بدون نهاية JSON

            recovered = GoogleBatchTranslator._load_cache(cache_file)
            for i in range(1, 6):
                self.assertEqual(recovered[f"Line {i}"], f"ترجمة:Line {i}")
            self.assertNotIn("Line 6", recovered)

            # استكمال التشغيل: يترجم الأسطر المتبقية فقط (6، 7، 8)
            resumed_calls = []

            def clean_caller(batch_text, ep, sl, tl):
                lines = batch_text.splitlines()
                resumed_calls.append(lines)
                return "\n".join(f"ترجمة:{ln}" for ln in lines)

            tr2 = GoogleBatchTranslator(
                max_chars=5000,
                max_lines=2,
                inter_batch_delay=0.0,
                endpoint_caller=clean_caller,
            )
            final_map, sent_batches, cached_count = tr2.translate_lines(
                all_lines, cache_path=cache_file
            )
            self.assertEqual(cached_count, 5)
            self.assertEqual(sent_batches, 2)
            self.assertEqual(resumed_calls, [["Line 6", "Line 7"], ["Line 8"]])
            self.assertEqual(len(final_map), 8)
            self.assertFalse(os.path.exists(wal_path))

    def test_zero_touch_phase1_crash_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            gdir = os.path.join(tmp, "ZeroTouchGame")
            managed = os.path.join(gdir, "MyGame_Data", "Managed")
            en_dir = os.path.join(gdir, "MyGame_Data", "StreamingAssets", "Localization", "en")
            os.makedirs(managed)
            os.makedirs(en_dir)
            exe_path = os.path.join(gdir, "MyGame.exe")
            dll_path = os.path.join(managed, "Assembly-CSharp.dll")
            ui_json = os.path.join(en_dir, "ui.json")
            items_json = os.path.join(en_dir, "items.json")

            with open(exe_path, "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(dll_path, "wb") as f:
                f.write(b"MZ")
            with open(ui_json, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "play": "New Game",
                        "cont": "Continue",
                        "opt": "Options",
                        "quit": "Quit",
                    },
                    f,
                )
            with open(items_json, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "sword": "Iron Sword",
                        "shield": "Oak Shield",
                        "potion": "Health Potion",
                        "key": "Silver Key",
                    },
                    f,
                )

            orig_hashes = {p: compute_sha256(p) for p in (exe_path, dll_path, ui_json, items_json)}

            calls = []

            def crash_on_second_batch(batch_text, ep, sl, tl):
                lines = batch_text.splitlines()
                calls.append(lines)
                if len(calls) >= 2:
                    raise RuntimeError("Power cut during translation")
                return "\n".join("ع:" + ln for ln in lines)

            pipe1 = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(
                    max_chars=5000,
                    max_lines=3,
                    inter_batch_delay=0.0,
                    endpoint_caller=crash_on_second_batch,
                )
            )
            with patch("translators.google_batch.time.sleep"):
                rep1 = pipe1.translate(gdir, skip_inject=False)
            self.assertFalse(rep1.success)

            # التحقق الصارم: ولا ملف لعبة تأثر أو أُنشئ له .bak أو حُقن DLL
            for p, h in orig_hashes.items():
                self.assertEqual(compute_sha256(p), h)
            self.assertFalse(os.path.exists(ui_json + ".bak"))
            self.assertFalse(os.path.exists(items_json + ".bak"))
            self.assertFalse(os.path.exists(os.path.join(gdir, "winhttp.dll")))
            self.assertFalse(BackupManager.get_backup_summary(gdir)["has_backup"])

            summary = TransactionJournal.get_interrupted_session_summary(gdir)
            self.assertTrue(summary["has_interrupted"])
            self.assertTrue(summary["game_untouched"])
            self.assertFalse(summary["needs_recovery"])
            self.assertEqual(summary["completed_lines"], 3)
            self.assertEqual(summary["total_unique_lines"], 8)

            # الاستئناف بعد عودة الكهرباء: يكمل من الكاش ويطبق بنجاح
            pipe2 = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(
                    max_chars=5000,
                    max_lines=3,
                    inter_batch_delay=0.0,
                    endpoint_caller=lambda bt, ep, s, t: "\n".join(
                        "ع:" + ln for ln in bt.splitlines()
                    ),
                )
            )
            rep2 = pipe2.translate(gdir, skip_inject=True)
            self.assertTrue(rep2.success, rep2.errors)
            self.assertEqual(rep2.stats.cached_lines, 3)
            self.assertFalse(TransactionJournal.get_interrupted_session_summary(gdir)["has_interrupted"])
            self.assertFalse(os.path.isdir(TransactionJournal.get_staging_dir(gdir)))

            with open(ui_json, "r", encoding="utf-8") as f:
                self.assertRegex(json.load(f)["play"], r"[\uFE70-\uFEFF]")
            with open(items_json, "r", encoding="utf-8") as f:
                self.assertRegex(json.load(f)["sword"], r"[\uFE70-\uFEFF]")

            _, warnings = AutoTranslatorPipeline.rollback(gdir)
            self.assertEqual(warnings, [])
            for p, h in orig_hashes.items():
                self.assertEqual(compute_sha256(p), h)

    def test_zero_touch_phase2_staging_failure_leaves_game_untouched(self):
        from formats.json_handler import JsonFormatHandler

        with tempfile.TemporaryDirectory() as tmp:
            gdir = os.path.join(tmp, "StagingFailGame")
            managed = os.path.join(gdir, "MyGame_Data", "Managed")
            en_dir = os.path.join(gdir, "MyGame_Data", "StreamingAssets", "Localization", "en")
            os.makedirs(managed)
            os.makedirs(en_dir)
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")

            f1 = os.path.join(en_dir, "a_ui.json")
            f2 = os.path.join(en_dir, "b_items.json")
            with open(f1, "w", encoding="utf-8") as f:
                json.dump({"play": "New Game", "opt": "Options", "quit": "Quit"}, f)
            with open(f2, "w", encoding="utf-8") as f:
                json.dump({"sword": "Iron Sword", "shield": "Oak Shield", "potion": "Health Potion"}, f)

            h1 = compute_sha256(f1)
            h2 = compute_sha256(f2)

            orig_save = JsonFormatHandler.save
            save_calls = []

            def fail_second_stage(self_h, path, doc, replacements):
                save_calls.append(path)
                if len(save_calls) == 2:
                    raise OSError("Simulated disk failure during staging")
                return orig_save(self_h, path, doc, replacements)

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(
                    inter_batch_delay=0.0,
                    endpoint_caller=lambda bt, ep, s, t: "\n".join(
                        "ع:" + ln for ln in bt.splitlines()
                    ),
                )
            )
            with patch.object(JsonFormatHandler, "save", fail_second_stage):
                rep1 = pipe.translate(gdir, skip_inject=True)

            self.assertFalse(rep1.success)
            # كلا الملفين الأصليين سليمان 100% ولم يُنشأ .bak واحد
            self.assertEqual(compute_sha256(f1), h1)
            self.assertEqual(compute_sha256(f2), h2)
            self.assertFalse(os.path.exists(f1 + ".bak"))
            self.assertFalse(os.path.exists(f2 + ".bak"))
            self.assertFalse(os.path.isdir(TransactionJournal.get_staging_dir(gdir)))

            # إعادة التشغيل بدون خلل تستفيد من الكاش (0 دفعات مرسلة)
            rep2 = pipe.translate(gdir, skip_inject=True)
            self.assertTrue(rep2.success, rep2.errors)
            self.assertEqual(rep2.stats.batches_sent, 0)
            self.assertEqual(rep2.stats.cached_lines, 6)

    def test_auto_recovery_from_interrupted_phase3_commit_and_cli_recover(self):
        from main import main as cli_main

        def _setup_interrupted_commit_game(base_dir: str):
            managed = os.path.join(base_dir, "MyGame_Data", "Managed")
            en_dir = os.path.join(base_dir, "MyGame_Data", "StreamingAssets", "Localization", "en")
            os.makedirs(managed)
            os.makedirs(en_dir)
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")

            f1 = os.path.join(en_dir, "a_ui.json")
            f2 = os.path.join(en_dir, "b_items.json")
            with open(f1, "w", encoding="utf-8") as f:
                json.dump({"play": "New Game", "opt": "Options", "quit": "Quit"}, f)
            with open(f2, "w", encoding="utf-8") as f:
                json.dump({"sword": "Iron Sword", "shield": "Oak Shield", "potion": "Health Potion"}, f)

            h1 = compute_sha256(f1)
            h2 = compute_sha256(f2)

            # محاكاة انقطاع الكهرباء في منتصف المرحلة 3: نُسخ f1 احتياطياً ثم كُتب عليه جزئياً، ووُجد ملف .tmp و winhttp.dll
            BackupManager.backup_file(base_dir, f1)
            with open(f1, "w", encoding="utf-8") as f:
                json.dump({"play": "ترجمة جزئية", "opt": "خيارات", "quit": "خروج"}, f)
            stray_tmp = f2 + ".arabic_commit.tmp"
            with open(stray_tmp, "w", encoding="utf-8") as f:
                f.write("half written")
            winhttp = os.path.join(base_dir, "winhttp.dll")
            BackupManager.register_created_file(base_dir, winhttp)
            with open(winhttp, "wb") as f:
                f.write(b"MZ_DOORSTOP")

            cache_path = os.path.join(BackupManager.get_state_dir(base_dir), "translation_cache.json")
            GoogleBatchTranslator._save_cache_atomic(
                cache_path,
                {
                    "New Game": "لعبة جديدة",
                    "Options": "خيارات",
                    "Quit": "خروج",
                    "Iron Sword": "سيف حديدي",
                    "Oak Shield": "درع بلوط",
                    "Health Potion": "جرعة صحة",
                },
            )
            TransactionJournal.save_session(
                base_dir,
                {
                    "status": "committing",
                    "total_unique_lines": 6,
                    "completed_lines": 6,
                },
            )
            return f1, f2, h1, h2, stray_tmp, winhttp, cache_path

        with tempfile.TemporaryDirectory() as tmp:
            # 1. التحقق من CLI recover
            g1 = os.path.join(tmp, "RecoverCliGame")
            f1, f2, h1, h2, stray1, win1, cache1 = _setup_interrupted_commit_game(g1)
            self.assertTrue(TransactionJournal.get_interrupted_session_summary(g1)["needs_recovery"])

            rc = cli_main(["recover", g1])
            self.assertEqual(rc, 0)
            self.assertEqual(compute_sha256(f1), h1)
            self.assertEqual(compute_sha256(f2), h2)
            self.assertFalse(os.path.exists(stray1))
            self.assertFalse(os.path.exists(win1))
            self.assertFalse(BackupManager.get_backup_summary(g1)["has_backup"])
            self.assertTrue(os.path.isfile(cache1))  # الكاش محفوظ للاستكمال الفوري
            self.assertFalse(TransactionJournal.get_interrupted_session_summary(g1)["has_interrupted"])

            # 2. التحقق من الاستعادة التلقائية قبل البدء في pipe.translate() مع استهلاك الكاش المحفوظ
            g2 = os.path.join(tmp, "AutoPreflightGame")
            gf1, gf2, gh1, gh2, stray2, win2, _ = _setup_interrupted_commit_game(g2)

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(
                    inter_batch_delay=0.0,
                    endpoint_caller=lambda bt, ep, s, t: self.fail("Should not call remote translator when cache is intact"),
                )
            )
            rep = pipe.translate(g2, skip_inject=True)
            self.assertTrue(rep.success, rep.errors)
            self.assertFalse(os.path.exists(stray2))
            self.assertFalse(os.path.exists(win2))
            self.assertEqual(rep.stats.batches_sent, 0)
            self.assertEqual(rep.stats.cached_lines, 6)
            self.assertTrue(any("انقطاع" in w for w in rep.warnings))
            with open(gf1, "r", encoding="utf-8") as f:
                self.assertRegex(json.load(f)["play"], r"[\uFE70-\uFEFF]")
                # فحص بعد rollback للتأكد من بقاء النسخة الأصلية النظيفة
            AutoTranslatorPipeline.rollback(g2)
            self.assertEqual(compute_sha256(gf1), gh1)
            self.assertEqual(compute_sha256(gf2), gh2)

    def test_gui_safe_pause_button_and_interrupted_resume_prompt(self):
        from ui.gui import TranslatorGUI

        with tempfile.TemporaryDirectory() as tmp:
            gdir = os.path.join(tmp, "GuiPauseGame")
            managed = os.path.join(gdir, "MyGame_Data", "Managed")
            loc_dir = os.path.join(gdir, "MyGame_Data", "StreamingAssets", "Localization")
            os.makedirs(managed)
            os.makedirs(loc_dir)
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")
            en_json = os.path.join(loc_dir, "English.json")
            with open(en_json, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "k1": "New Game",
                        "k2": "Continue",
                        "k3": "Options",
                        "k4": "Audio Settings",
                        "k5": "Save Game",
                        "k6": "Quit",
                    },
                    f,
                )
            orig_sha = compute_sha256(en_json)

            cancel_ev = threading.Event()

            def pause_after_first_batch(bt, ep, s, t):
                cancel_ev.set()
                return "\n".join("ع:" + ln for ln in bt.splitlines())

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(
                    max_chars=5000,
                    max_lines=2,
                    inter_batch_delay=0.0,
                    endpoint_caller=pause_after_first_batch,
                )
            )
            rep_paused = pipe.translate(gdir, skip_inject=True, cancel_event=cancel_ev)
            self.assertFalse(rep_paused.success)
            self.assertTrue(rep_paused.interrupted)
            self.assertEqual(compute_sha256(en_json), orig_sha)
            self.assertFalse(os.path.exists(en_json + ".bak"))

            try:
                root = tk.Tk()
            except tk.TclError as exc:
                self.skipTest(f"تعذر تهيئة Tkinter في هذه البيئة: {exc}")
            root.withdraw()
            try:
                app = TranslatorGUI(root=root, initial_game_dir="", pipeline=pipe)
                app.run_scan(gdir)
                self.assertIn("لم تُمس", app.backup_label_var.get())
                self.assertIn("2/6", app.backup_label_var.get())

                app._is_busy = True
                app.on_pause_click()
                self.assertTrue(app._cancel_event.is_set())
                app._is_busy = False

                with patch("ui.gui.messagebox.askyesno", return_value=False) as mock_ask:
                    prompted = app.prompt_interrupted_session_if_present(gdir)
                    self.assertTrue(prompted)
                    self.assertTrue(mock_ask.called)
            finally:
                root.destroy()


class UguiAndScannerHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_assets_extraction_cache()

    def test_ugui_text_extraction_single_word_multiline_and_single_entry_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1. SerializedFile v17 (Unity 2018) مع كلمات مفردة ونص متعدد الأسطر وكائن Image مضلل
            cull_sig = b"UnityEngine.UI.MaskableGraphic+CullStateChangedEvent, UnityEngine.UI, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null"
            decoy_image_payload = (
                struct.pack("<iq", 0, 10)
                + struct.pack("<I", 1)
                + struct.pack("<iq", 1, 200)
                + _pack_aligned_string("")
                + struct.pack("<iq4fII", 0, 0, 1.0, 1.0, 1.0, 1.0, 1, 0)
                + _pack_aligned_string(cull_sig.decode("ascii"))
                + b"\x00" * 40
            )
            multiline_ctrl = "CONTROLLER\nLeft Joystick to move\nRight Joystick to look\nA to barrel roll"
            v17_objs = [
                (78, 114, build_unity_ugui_text_blob("Play", sf_version=17)),
                (79, 114, build_unity_ugui_text_blob("Quit", sf_version=17)),
                (80, 114, build_unity_ugui_text_blob("Controls", sf_version=17)),
                (81, 114, build_unity_ugui_text_blob("Back", sf_version=17)),
                (82, 114, build_unity_ugui_text_blob("EverLight", sf_version=17)),
                (85, 114, build_unity_ugui_text_blob(multiline_ctrl, sf_version=17)),
                (88, 114, decoy_image_payload),
            ]
            self.assertIsNone(_extract_ugui_text_from_monobehaviour(decoy_image_payload, sf_version=17))
            self.assertEqual(
                _extract_ugui_text_from_monobehaviour(v17_objs[5][2], sf_version=17),
                multiline_ctrl,
            )

            lvl0_path = os.path.join(tmp, "level0")
            with open(lvl0_path, "wb") as f:
                f.write(build_unity_serialized_file_blob(v17_objs, unity_version="2018.1.0f2", version=17))
            blocks_v17 = extract_assets_localization_blocks(lvl0_path)
            self.assertEqual(len(blocks_v17), 1)
            self.assertEqual(blocks_v17[0].block_kind, "UGUI:Text")
            vals_v17 = [v for _, _, v in blocks_v17[0].entries]
            self.assertEqual(vals_v17, ["Play", "Quit", "Controls", "Back", "EverLight", multiline_ctrl])

            # 2. مشهد بنص واحد فقط (level1)
            lvl1_path = os.path.join(tmp, "level1")
            with open(lvl1_path, "wb") as f:
                f.write(
                    build_unity_serialized_file_blob(
                        [(979, 114, build_unity_ugui_text_blob("Space or A to Skip", sf_version=17))],
                        unity_version="2018.1.0f2",
                        version=17,
                    )
                )
            blocks_lvl1 = extract_assets_localization_blocks(lvl1_path)
            self.assertEqual(len(blocks_lvl1), 1)
            self.assertEqual(blocks_lvl1[0].block_kind, "UGUI:Text")
            self.assertEqual([v for _, _, v in blocks_lvl1[0].entries], ["Space or A to Skip"])

            # 3. Unity 2020+ (بدون m_TypeName) على SerializedFile v22
            v22_path = os.path.join(tmp, "level_v22")
            v22_objs = [
                (10, 114, build_unity_ugui_text_blob("New Game", sf_version=22, include_cull_type_name=False)),
                (
                    11,
                    114,
                    build_unity_ugui_text_blob(
                        "Press Space\nto Continue", sf_version=22, include_cull_type_name=False
                    ),
                ),
            ]
            with open(v22_path, "wb") as f:
                f.write(build_unity_serialized_file_blob(v22_objs, unity_version="2021.3.20f1", version=22))
            blocks_v22 = extract_assets_localization_blocks(v22_path)
            self.assertEqual(len(blocks_v22), 1)
            self.assertEqual(blocks_v22[0].block_kind, "UGUI:Text")
            self.assertEqual([v for _, _, v in blocks_v22[0].entries], ["New Game", "Press Space\nto Continue"])

            # 4. extract_scene_ui_texts_from_bytes تقبل الأسطر المتعددة للترويسات الموثّقة وترفض غير الموثّقة
            tmp_blob = (
                struct.pack("<7I", 0, 0, 0, 0, 0, 1, 0)
                + _pack_aligned_string("Line One\nLine Two")
                + struct.pack("<II", 0, 1)
            )
            ugui_blob = build_unity_ugui_text_blob("KEYBOARD\nWASD to move", sf_version=17)
            unverified_blob = (
                b"\x00" * 32
                + _pack_aligned_string("Unverified Line One\nUnverified Line Two")
                + b"\x00" * 16
            )
            extracted_raw = [
                v for _, v in extract_scene_ui_texts_from_bytes(tmp_blob + ugui_blob + unverified_blob)
            ]
            self.assertIn("Line One\nLine Two", extracted_raw)
            self.assertIn("KEYBOARD\nWASD to move", extracted_raw)
            self.assertNotIn("Unverified Line One\nUnverified Line Two", extracted_raw)

    def test_ugui_level_selection_primary_and_complementary_with_external(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1. المسار الأساسي: لعبة UGUI خالصة بمشاهد متعددة (منها مشاهد بنص واحد وكلمة مفردة) + Assembly-CSharp.dll
            gdir = os.path.join(tmp, "UguiOnlyGame")
            data_dir = os.path.join(gdir, "MyGame_Data")
            managed = os.path.join(data_dir, "Managed")
            os.makedirs(managed)
            with open(os.path.join(gdir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(
                    _build_minimal_dotnet_pe_with_us(
                        [
                            "Setting camera movement to freefrom movement",
                            "New Game",
                            "Continue",
                            "Options",
                            "Audio Settings",
                            "Press Space to jump",
                        ]
                    )
                )

            kb_multi = "KEYBOARD\nWASD to move\nMouse to look\nSpace to barrel roll"
            level_specs = {
                "level0": ["Play", "Quit", "Controls", "Back", "EverLight", kb_multi],
                "level1": ["Space or A to Skip"],
                "level2": ["Press B to Continue"],
                "level3": ["Paused", "Options", "Main Menu", "Set Sensitivity", "Invert Y Axis"],
                "level4": ["Skip"],
            }
            orig_hashes = {}
            for lvl_name, texts in level_specs.items():
                lvl_path = os.path.join(data_dir, lvl_name)
                objs = [
                    (100 + idx, 114, build_unity_ugui_text_blob(txt, sf_version=17))
                    for idx, txt in enumerate(texts)
                ]
                with open(lvl_path, "wb") as f:
                    f.write(build_unity_serialized_file_blob(objs, unity_version="2018.1.0f2", version=17))
                orig_hashes[lvl_path] = compute_sha256(lvl_path)

            def fake_ep(batch_text, ep, sl, tl):
                return "\n".join("ع:" + ln for ln in batch_text.splitlines())

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(gdir, skip_inject=True)
            self.assertTrue(rep.success, rep.errors)
            selected_names = {os.path.basename(c.relative_path) for c in rep.selected_files}
            self.assertEqual(selected_names, {"level0", "level1", "level2", "level3", "level4"})
            self.assertTrue(all(c.format_name == "unity_assets" for c in rep.selected_files))
            self.assertFalse(any(c.format_name == "unity_code" for c in rep.selected_files))

            override_path = os.path.join(gdir, "ArabicOverride.json")
            self.assertTrue(os.path.isfile(override_path))
            with open(override_path, "r", encoding="utf-8") as f:
                ov = json.load(f)
            self.assertIn("Play", ov["by_source"])
            self.assertIn("Space or A to Skip", ov["by_source"])
            self.assertIn("Skip", ov["by_source"])
            self.assertIn(kb_multi, ov["by_source"])
            self.assertIn("Paused", ov["by_source"])
            for p, h in orig_hashes.items():
                self.assertEqual(compute_sha256(p), h)

            _, rb_warns = AutoTranslatorPipeline.rollback(gdir)
            self.assertEqual(rb_warns, [])
            self.assertFalse(os.path.exists(override_path))

            # 2. المسار التكميلي مع ملف خارجي + مشهد TextMesh بنص واحد
            gdir2 = os.path.join(tmp, "MixedUguiExternalGame")
            data2 = os.path.join(gdir2, "MyGame_Data")
            managed2 = os.path.join(data2, "Managed")
            loc2 = os.path.join(data2, "StreamingAssets", "Localization")
            os.makedirs(managed2)
            os.makedirs(loc2)
            with open(os.path.join(gdir2, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed2, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")
            with open(os.path.join(loc2, "English.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "play": "New Game",
                        "cont": "Continue",
                        "opt": "Options",
                        "audio": "Audio Settings",
                        "quit": "Quit",
                    },
                    f,
                )
            with open(os.path.join(data2, "level1"), "wb") as f:
                f.write(
                    build_unity_serialized_file_blob(
                        [(201, 114, build_unity_ugui_text_blob("Space or A to Skip", sf_version=17))],
                        unity_version="2018.1.0f2",
                        version=17,
                    )
                )
            with open(os.path.join(data2, "level2"), "wb") as f:
                f.write(
                    build_unity_serialized_file_blob(
                        [(202, 114, build_unity_ugui_text_blob("New Game", sf_version=17))],
                        unity_version="2018.1.0f2",
                        version=17,
                    )
                )
            tm_payload = struct.pack("<iq", 0, 42) + _pack_aligned_string("Press Space")
            with open(os.path.join(data2, "level5"), "wb") as f:
                f.write(
                    build_unity_serialized_file_blob(
                        [(205, 102, tm_payload)],
                        unity_version="2018.1.0f2",
                        version=22,
                    )
                )

            rep2 = pipe.scan(gdir2)
            self.assertTrue(rep2.success)
            sel2 = {os.path.basename(c.relative_path) for c in rep2.selected_files}
            self.assertEqual(sel2, {"English.json", "level1", "level5"})
            self.assertNotIn("level2", sel2)

    def test_unity_code_noise_filtering_blocks_shaders_bindings_and_sdk_warnings(self):
        noisy_strings = [
            "Hidden/Post FX/Bloom",
            "Hidden/Post FX/Uber Shader",
            "Sprites/Default",
            "joystick button 0",
            "joystick button 7",
            "Controller_Mouse X",
            "FOVKick camera is null, please supply the camera to the constructor",
            "This script need an Image with a readbale Texture2D to work.",
            " not found in the FMOD project.",
            "There is already a virtual axis named ",
            "Shader not found ({0})",
            "Attempting to remove a RenderTexture that was not allocated: {0}",
        ]
        clean_strings = [
            "New Game",
            "Options",
            "Audio Settings",
            "Press Space to jump",
        ]
        for s in noisy_strings:
            self.assertFalse(_is_clean_code_ui_string(s), f"Should reject noisy string: {s!r}")
        for s in clean_strings:
            self.assertTrue(_is_clean_code_ui_string(s), f"Should accept UI string: {s!r}")

        with tempfile.TemporaryDirectory() as tmp:
            dll_path = os.path.join(tmp, "Assembly-CSharp.dll")
            with open(dll_path, "wb") as f:
                f.write(_build_minimal_dotnet_pe_with_us(noisy_strings + clean_strings))
            extracted = extract_code_ui_strings_from_file(dll_path)
            self.assertEqual(extracted, clean_strings)

    def test_arabic_captured_texts_and_logs_excluded_from_rescan(self):
        with tempfile.TemporaryDirectory() as tmp:
            gdir = os.path.join(tmp, "RescanGame")
            data_dir = os.path.join(gdir, "MyGame_Data")
            managed = os.path.join(data_dir, "Managed")
            os.makedirs(managed)
            with open(os.path.join(gdir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")
            with open(os.path.join(data_dir, "level0"), "wb") as f:
                f.write(
                    build_unity_serialized_file_blob(
                        [
                            (1, 114, build_unity_ugui_text_blob("Play", sf_version=17)),
                            (2, 114, build_unity_ugui_text_blob("Quit", sf_version=17)),
                            (3, 114, build_unity_ugui_text_blob("Controls", sf_version=17)),
                        ],
                        unity_version="2018.1.0f2",
                        version=17,
                    )
                )

            cap_path = os.path.join(gdir, "arabic_captured_texts.txt")
            font_log = os.path.join(gdir, "arabic_font_log.txt")
            tr_log = os.path.join(gdir, "arabic_translator_log.txt")
            sample_body = "\n".join(
                ["New Game", "Continue", "Options", "Settings", "Controls", "Play", "Quit"]
            )
            for p in (cap_path, font_log, tr_log):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(sample_body)

            eng = UnityDetector().detect(gdir)
            cands, _ = UnityLanguageScanner().scan(eng, deep_scan=True)
            cand_names = {os.path.basename(c.relative_path).lower() for c in cands}
            self.assertNotIn("arabic_captured_texts.txt", cand_names)
            self.assertNotIn("arabic_font_log.txt", cand_names)
            self.assertNotIn("arabic_translator_log.txt", cand_names)
            self.assertIn("level0", cand_names)

            direct_cap = score_candidate_file(cap_path, gdir, [], set())
            self.assertTrue(direct_cap is None or direct_cap.score < 20)


class MonoScriptTmpAndDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_assets_extraction_cache()

    def test_monoscript_externals_resolution_and_engine_blocklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1. بناء globalgamemanagers.assets يحتوي MonoScripts (ClassID 115)
            ggm_path = os.path.join(tmp, "globalgamemanagers.assets")
            ggm_objs = [
                (434, 115, build_unity_monoscript_blob("Text", "UnityEngine.UI", "UnityEngine.UI.dll", sf_version=22)),
                (1186, 115, build_unity_monoscript_blob("TextMeshProUGUI", "TMPro", "Unity.TextMeshPro.dll", sf_version=22)),
                (2001, 115, build_unity_monoscript_blob("TMP_SpriteAsset", "TMPro", "Unity.TextMeshPro.dll", sf_version=22)),
                (2002, 115, build_unity_monoscript_blob("Image", "UnityEngine.UI", "UnityEngine.UI.dll", sf_version=22)),
            ]
            with open(ggm_path, "wb") as f:
                f.write(build_unity_serialized_file_blob(ggm_objs, unity_version="2020.3.30f1", version=22))

            # UGUI Text برأس مزاح (مؤكد عبر MonoScript file_id=1, path_id=434)
            shifted_ugui = (
                struct.pack("<iq", 0, 10)
                + struct.pack("<I", 1)
                + struct.pack("<iq", 1, 434)
                + _pack_aligned_string("")
                + struct.pack("<iq4fI", 0, 0, 1.0, 1.0, 1.0, 1.0, 1)
                + b"\xAA\xBB\xCC\xDD" * 4  # حقل إضافي غير مألوف قبل m_FontData
                + struct.pack("<iq", 0, 10102)
                + struct.pack("<iiiiiiiiiif", 48, 0, 0, 10, 65, 4, 0, 1, 1, 1, 1.0)
                + _pack_aligned_string("To be continued...")
            )
            # TMP_SpriteAsset مضلل يحتوي كلمات إنجليزية (يجب تجاهله بالكامل بفضل MonoScript)
            sprite_decoy = (
                struct.pack("<iq", 0, 0)
                + struct.pack("<I", 1)
                + struct.pack("<iq", 1, 2001)
                + _pack_aligned_string("Default Sprite Asset")
                + _pack_aligned_string("Smiling face with eyes")
                + _pack_aligned_string("Wink at player")
                + _pack_aligned_string("Grinning face icon")
            )
            # UI.Image مضلل يحتوي نصاً عشوائياً (يجب تجاهله بالكامل بفضل MonoScript)
            image_decoy = (
                struct.pack("<iq", 0, 11)
                + struct.pack("<I", 1)
                + struct.pack("<iq", 1, 2002)
                + _pack_aligned_string("")
                + _pack_aligned_string("This is an image decoy sentence.")
                + _pack_aligned_string("Another decoy sentence inside image.")
                + _pack_aligned_string("Third decoy sentence inside image.")
            )
            tmp_obj = build_unity_tmp_text_blob(
                "Continue",
                sf_version=22,
                include_raycast_padding=True,
                script_file_id=1,
                script_path_id=1186,
            )

            lvl_path = os.path.join(tmp, "level0")
            with open(lvl_path, "wb") as f:
                f.write(
                    build_unity_serialized_file_blob(
                        [(1, 114, shifted_ugui), (2, 114, tmp_obj), (3, 114, sprite_decoy), (4, 114, image_decoy)],
                        unity_version="2020.3.30f1",
                        version=22,
                        externals=["globalgamemanagers.assets"],
                    )
                )

            sf_idx = parse_serialized_file_index_from_file(lvl_path)
            self.assertIsNotNone(sf_idx)
            self.assertEqual(sf_idx.externals, ["globalgamemanagers.assets"])

            blocks = extract_assets_localization_blocks(lvl_path)
            kinds = {b.block_kind for b in blocks}
            self.assertEqual(kinds, {"UGUI:Text", "TMP:Text"})
            all_vals = {v for b in blocks for _, _, v in b.entries}
            self.assertEqual(all_vals, {"To be continued...", "Continue"})

            # 2. التحقق داخل حزمة UnityFS مضغوطة (globalgamemanagers + level0 كعقدتين مترابطتين)
            ggm_bytes = build_unity_serialized_file_blob(ggm_objs, unity_version="2020.3.30f1", version=22)
            lvl_bytes = build_unity_serialized_file_blob(
                [(2, 114, tmp_obj), (3, 114, sprite_decoy)],
                unity_version="2020.3.30f1",
                version=22,
                externals=["globalgamemanagers.assets"],
            )
            bundle_path = os.path.join(tmp, "data.unity3d")
            with open(bundle_path, "wb") as f:
                f.write(build_unityfs_bundle_blob([("globalgamemanagers.assets", ggm_bytes), ("level0", lvl_bytes)]))
            b_blocks = extract_assets_localization_blocks(bundle_path)
            self.assertEqual([b.block_kind for b in b_blocks], ["TMP:Text"])
            self.assertEqual([v for _, _, v in b_blocks[0].entries], ["Continue"])

    def test_tmp_text_extraction_across_versions_coexistence_and_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1. اختبار استخراج TMP_Text عبر Unity 2018 و 2019 و 2020+
            blob_2018 = build_unity_tmp_text_blob("Start", sf_version=17, include_cull_type_name=True)
            blob_2019 = build_unity_tmp_text_blob("Settings", sf_version=21, include_raycast_padding=False)
            blob_2020 = build_unity_tmp_text_blob("Little\ntown", sf_version=22, include_raycast_padding=True)
            self.assertEqual(_extract_tmp_text_from_monobehaviour(blob_2018, sf_version=17), "Start")
            self.assertEqual(_extract_tmp_text_from_monobehaviour(blob_2019, sf_version=21), "Settings")
            self.assertEqual(_extract_tmp_text_from_monobehaviour(blob_2020, sf_version=22), "Little\ntown")

            # 2. تعايش MonoBehaviour:dialogue + TMP:Text في المشهد نفسه + مشهد بنص TMP واحد فقط
            gdir = os.path.join(tmp, "TmpAndDialogueGame")
            data_dir = os.path.join(gdir, "MyGame_Data")
            managed = os.path.join(data_dir, "Managed")
            os.makedirs(managed)
            with open(os.path.join(gdir, "MyGame.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(b"MZ")

            dlg_payload = (
                struct.pack("<iq", 0, 0)
                + struct.pack("<i", 1)
                + struct.pack("<iq", 0, 99)
                + _pack_aligned_string("CombatLog")
                + _pack_aligned_string("An enemy appeared...")
                + _pack_aligned_string("Player's turn...")
                + _pack_aligned_string("Enemy defeated!")
            )
            lvl2_objs = [
                (10, 114, dlg_payload),
                (11, 114, build_unity_tmp_text_blob("Continue", sf_version=22)),
                (12, 114, build_unity_tmp_text_blob("Main Menu", sf_version=22)),
                (13, 114, build_unity_tmp_text_blob("Pause\n", sf_version=22)),
            ]
            with open(os.path.join(data_dir, "level2"), "wb") as f:
                f.write(build_unity_serialized_file_blob(lvl2_objs, unity_version="2020.3.30f1", version=22))
            with open(os.path.join(data_dir, "level8"), "wb") as f:
                f.write(
                    build_unity_serialized_file_blob(
                        [
                            (20, 114, build_unity_tmp_text_blob("Quit", sf_version=22)),
                            (21, 114, build_unity_ugui_text_blob("To be continued...", sf_version=22, include_cull_type_name=False)),
                        ],
                        unity_version="2020.3.30f1",
                        version=22,
                    )
                )
            with open(os.path.join(data_dir, "level9"), "wb") as f:
                f.write(
                    build_unity_serialized_file_blob(
                        [(30, 114, build_unity_tmp_text_blob("Skip", sf_version=22))],
                        unity_version="2020.3.30f1",
                        version=22,
                    )
                )

            lvl2_blocks = extract_assets_localization_blocks(os.path.join(data_dir, "level2"))
            self.assertEqual({b.block_kind for b in lvl2_blocks}, {"MonoBehaviour:dialogue", "TMP:Text"})

            rep = AutoTranslatorPipeline().scan(gdir)
            self.assertTrue(rep.success)
            self.assertEqual({os.path.basename(c.relative_path) for c in rep.selected_files}, {"level2", "level8", "level9"})

            # 3. الدمج التكميلي مع ملف خارجي English.json يلتقط مشهد TMP ذي النص الواحد غير المكرر
            loc_dir = os.path.join(data_dir, "StreamingAssets", "Localization")
            os.makedirs(loc_dir)
            with open(os.path.join(loc_dir, "English.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {"play": "New Game", "cont": "Continue", "opt": "Options", "audio": "Audio Settings", "quit": "Quit"},
                    f,
                )
            rep_mixed = AutoTranslatorPipeline().scan(gdir)
            sel_mixed = {os.path.basename(c.relative_path) for c in rep_mixed.selected_files}
            self.assertIn("English.json", sel_mixed)
            self.assertIn("level9", sel_mixed)

    def test_assembly_qualified_type_names_rejected_while_natural_dialogue_kept(self):
        rejected = [
            "GameStateChanger, Assembly-CSharp",
            "PlayerSpawner, Assembly-CSharp",
            "UnityEngine.UI.Button, UnityEngine.UI, Version=1.0.0.0",
            "MyNs.StateCtrl, MyGame.Runtime",
        ]
        kept = [
            "Hi, Sensei.",
            "Hello, World",
            "Yes, please",
            "Wait, stop",
            "Dedicated to A.T, T.U and J.R",
        ]
        for s in rejected:
            self.assertTrue(_is_assembly_qualified_type_name(s), f"Should detect type name: {s!r}")
            self.assertFalse(_is_clean_code_ui_string(s), f"Should reject in unity_code: {s!r}")
        for s in kept:
            self.assertFalse(_is_assembly_qualified_type_name(s), f"Should keep natural dialogue: {s!r}")

        with tempfile.TemporaryDirectory() as tmp:
            # كائن Event يحتوي فقط أسماء أنواع UnityEvent يجب ألا يستخرج كحوار
            event_payload = (
                struct.pack("<iq", 0, 10)
                + struct.pack("<i", 1)
                + struct.pack("<iq", 0, 50)
                + _pack_aligned_string("")
                + _pack_aligned_string("GameStateChanger, Assembly-CSharp")
                + _pack_aligned_string("GameStateSO, Assembly-CSharp")
                + _pack_aligned_string("PreferenceSystem, Assembly-CSharp")
            )
            # كائن حوار حقيقي يحتوي جمل بفواصل طبيعية
            dlg_payload = (
                struct.pack("<iq", 0, 0)
                + struct.pack("<i", 1)
                + struct.pack("<iq", 0, 51)
                + _pack_aligned_string("Gabe&SenseiMagic")
                + _pack_aligned_string("Hi, Sensei.")
                + _pack_aligned_string("Hello, World")
                + _pack_aligned_string("Yes, please")
            )
            lvl_path = os.path.join(tmp, "sharedassets2.assets")
            with open(lvl_path, "wb") as f:
                f.write(build_unity_serialized_file_blob([(1, 114, event_payload), (2, 114, dlg_payload)], version=22))
            blocks = extract_assets_localization_blocks(lvl_path)
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0].block_name, "Gabe&SenseiMagic")
            self.assertEqual([v for _, _, v in blocks[0].entries], ["Hi, Sensei.", "Hello, World", "Yes, please"])

    def test_standalone_import_order_no_circular_import(self):
        translator_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        code = (
            "import sys; "
            f"sys.path.insert(0, {translator_root!r}); "
            "from engines.unity.assets_extractor import extract_assets_localization_blocks; "
            "from formats.registry import get_format_handler; "
            "assert get_format_handler('json') is not None; "
            "assert get_format_handler('unity_assets') is not None"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class MisnamedPlainTextSplitDirAndUguiFontTests(unittest.TestCase):
    def test_utf16_misnamed_json_sniffed_as_txt_and_preserves_lines_encoding_and_bak(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1. ملف نصي سطري بترميز UTF-16-LE وبامتداد .json يحتوي فواصل حوارية وعلامات اقتباس وأسطر فارغة
            plain_json = os.path.join(tmp, "1.json")
            orig_lines = [
                "•  Pandemonica, the Tired Demon  •\r\n",
                "\r\n",
                '"Name\'s Pandemonica, Hell\'s Customer Service."\r\n',
                "How may I serve you?\r\n",
                "https://vanripper.itch.io/artbook2\r\n",
            ]
            with open(plain_json, "wb") as f:
                f.write(codecs.BOM_UTF16_LE + "".join(orig_lines).encode("utf-16-le"))

            self.assertEqual(detect_candidate_format(plain_json, ".json"), "txt")

            # 2. ملف JSON حقيقي بترميز UTF-16 يجب أن يظل json
            real_json = os.path.join(tmp, "real.json")
            with open(real_json, "wb") as f:
                f.write(codecs.BOM_UTF16_LE + '{"a": "Hello world"}\r\n'.encode("utf-16-le"))
            self.assertEqual(detect_candidate_format(real_json, ".json"), "json")

            # 3. جدول CSV حقيقي داخل .json أو .txt يجب أن يظل csv
            csv_in_json = os.path.join(tmp, "table.json")
            with open(csv_in_json, "w", encoding="utf-8") as f:
                f.write("Key,English,French\nmenu_play,Play,Jouer\nmenu_quit,Quit,Quitter\n")
            self.assertEqual(detect_candidate_format(csv_in_json, ".json"), "csv")

            # 4. ملف ثنائي غير نصي بامتداد .json يجب أن يُرفض (None)
            bin_json = os.path.join(tmp, "bin.json")
            with open(bin_json, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x01")
            self.assertIsNone(detect_candidate_format(bin_json, ".json"))

            # 5. تحميل وحفظ عبر get_format_handler("txt") ثم عبر امتداد .bak يحافظان على UTF-16 وعدد الأسطر والفراغات
            handler = get_format_handler("txt")
            self.assertIsNotNone(handler)
            doc = handler.load(plain_json)
            self.assertEqual(doc.format_name, "txt")
            self.assertEqual([k for _, k, _ in doc.entries], ["line_0", "line_2", "line_3", "line_4"])

            replacements = {
                (0,): "•  بانديمونيكا، شيطانة متعبة  •",
                (2,): '"اسمها بانديمونيكا، خدمة عملاء الجحيم."',
                (3,): "كيف يمكنني خدمتك؟",
                (4,): "https://vanripper.itch.io/artbook2",
            }
            out_path = os.path.join(tmp, "out.json")
            handler.save(out_path, doc, replacements)
            with open(out_path, "rb") as f:
                raw_out = f.read()
            self.assertTrue(raw_out.startswith(codecs.BOM_UTF16_LE))
            reloaded = raw_out.decode("utf-16").splitlines(keepends=True)
            self.assertEqual(len(reloaded), len(orig_lines))
            self.assertEqual(reloaded[1], "\r\n")
            self.assertIn('"اسمها بانديمونيكا، خدمة عملاء الجحيم."', reloaded[2])

            bak_path = plain_json + ".bak"
            shutil.copy2(plain_json, bak_path)
            bak_doc = handler.load(bak_path)
            self.assertEqual(bak_doc.format_name, "txt")
            self.assertEqual(len(bak_doc.entries), len(doc.entries))

    def test_split_localization_directories_selected_without_breaking_monolithic_dirs(self):
        adapter = UnityAdapter()
        engine = EngineInfo(engine_name="Unity", supported=True, game_dir="/tmp/game")

        def mk_cand(rel: str, fmt: str = "txt", score: int = 80, dll_matched: bool = True) -> CandidateFile:
            return CandidateFile(
                path=f"/tmp/game/{rel}",
                relative_path=rel.replace("/", os.sep),
                format_name=fmt,
                score=score,
                dll_matched=dll_matched,
                total_entries=10,
                translatable_entries=8,
            )

        # 1. مجلدات تقسيم نصوص متعددة + مجلد ثانٍ مطابق لـ DLL
        split_cands = [
            mk_cand("local/m.json"),
            mk_cand("local/1.json"),
            mk_cand("local/2.json"),
            mk_cand("localHM/hm_m.json"),
            mk_cand("localHM/hm_0.json"),
        ]
        selected_split = adapter.select_language_files(engine, split_cands)
        self.assertEqual(
            {c.relative_path.replace("\\", "/") for c in selected_split},
            {"local/m.json", "local/1.json", "local/2.json", "localHM/hm_m.json", "localHM/hm_0.json"},
        )

        # 2. مجلد أحادي اللغة مقابل لغات شقيقة (Subnautica-like): يُختار English.json فقط
        mono_cands = [
            mk_cand("Subnautica_Data/StreamingAssets/SNUnmanagedData/LanguageFiles/English.json", fmt="json", score=95),
            mk_cand("Subnautica_Data/StreamingAssets/SNUnmanagedData/LanguageFiles/French.json", fmt="json", score=75),
            mk_cand("Subnautica_Data/StreamingAssets/SNUnmanagedData/LanguageFiles/German.json", fmt="json", score=75),
            mk_cand("Subnautica_Data/StreamingAssets/SNUnmanagedData/LanguageFiles/Spanish.json", fmt="json", score=75),
        ]
        selected_mono = adapter.select_language_files(engine, mono_cands)
        self.assertEqual(
            [c.relative_path.replace("\\", "/") for c in selected_mono],
            ["Subnautica_Data/StreamingAssets/SNUnmanagedData/LanguageFiles/English.json"],
        )

        # 3. ملف في الجذر لا يسحب ملفات عشوائية أخرى في الجذر
        root_cands = [
            mk_cand("strings.json", fmt="json", score=80, dll_matched=False),
            mk_cand("other.json", fmt="json", score=65, dll_matched=False),
        ]
        selected_root = adapter.select_language_files(engine, root_cands)
        self.assertEqual([c.relative_path for c in selected_root], ["strings.json"])

    def test_end_to_end_pipeline_helltaker_style_utf16_split_dirs_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            gdir = os.path.join(tmp, "HelltakerLike")
            managed = os.path.join(gdir, "Helltaker_Data", "Managed")
            local_dir = os.path.join(gdir, "local")
            local_hm_dir = os.path.join(gdir, "localHM")
            os.makedirs(managed)
            os.makedirs(local_dir)
            os.makedirs(local_hm_dir)

            with open(os.path.join(gdir, "Helltaker.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(
                    _build_minimal_dotnet_pe_with_us(
                        ["/local/m.json", "/localHM/hm_m.json", "\\local\\1.json"]
                    )
                )
            with open(os.path.join(managed, "UnityEngine.UI.dll"), "wb") as f:
                f.write(b"MZ" + b"\x00" * 64)

            files_spec = {
                os.path.join(local_dir, "m.json"): [
                    "•  PAUSE MENU  •\r\n",
                    "\r\n",
                    "MAIN MENU\r\n",
                    "RESUME\r\n",
                    "-\r\n",
                    "https://example.com/artbook\r\n",
                ],
                os.path.join(local_dir, "1.json"): [
                    "•  Pandemonica, the Tired Demon  •\r\n",
                    "Name's Pandemonica, Hell's Customer Service.\r\n",
                    "How may I serve you?\r\n",
                ],
                os.path.join(local_hm_dir, "hm_m.json"): [
                    "Bonus Chapter - Examtaker\r\n",
                    "GOD MODE\r\n",
                    "Thanks for playing!\r\n",
                ],
            }
            orig_bytes = {}
            for path, lines in files_spec.items():
                raw = codecs.BOM_UTF16_LE + "".join(lines).encode("utf-16-le")
                with open(path, "wb") as f:
                    f.write(raw)
                orig_bytes[path] = raw

            translations_ar = {
                "•  PAUSE MENU  •": "•  قائمة الإيقاف  •",
                "MAIN MENU": "القائمة الرئيسية",
                "RESUME": "استئناف",
                "•  Pandemonica, the Tired Demon  •": "•  بانديمونيكا، شيطانة متعبة  •",
                "Name's Pandemonica, Hell's Customer Service.": "اسمي بانديمونيكا، خدمة عملاء الجحيم.",
                "How may I serve you?": "كيف يمكنني خدمتك؟",
                "Bonus Chapter - Examtaker": "فصل إضافي - الممتحِنة",
                "GOD MODE": "الوضع الخارق",
                "Thanks for playing!": "شكراً للعب!",
            }

            def fake_ep(batch_text, ep, src, tgt):
                lines = batch_text.split("\n")
                out = []
                for ln in lines:
                    if not ln.strip():
                        out.append("")
                    else:
                        out.append(translations_ar.get(ln, "ترجمة: " + ln))
                return "\n".join(out)

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(gdir, skip_inject=True)
            self.assertTrue(rep.success, rep.errors)
            selected_rels = {c.relative_path.replace("\\", "/") for c in rep.selected_files}
            self.assertEqual(selected_rels, {"local/m.json", "local/1.json", "localHM/hm_m.json"})

            for path, orig_lines in files_spec.items():
                with open(path, "rb") as f:
                    raw = f.read()
                self.assertTrue(raw.startswith(codecs.BOM_UTF16_LE), path)
                dec_lines = raw.decode("utf-16").splitlines(keepends=True)
                self.assertEqual(len(dec_lines), len(orig_lines), path)
                self.assertTrue(any("\uFE70" <= ch <= "\uFEFF" or "\u0600" <= ch <= "\u06FF" for ch in "".join(dec_lines)))

            with open(os.path.join(local_dir, "m.json"), "r", encoding="utf-16") as f:
                m_lines = f.read().splitlines()
            self.assertEqual(m_lines[1], "")
            self.assertEqual(m_lines[4], "-")
            self.assertEqual(m_lines[5], "https://example.com/artbook")

            ok_rb, rb_warns = AutoTranslatorPipeline.rollback(gdir)
            self.assertTrue(ok_rb, rb_warns)
            for path, expected_raw in orig_bytes.items():
                with open(path, "rb") as f:
                    self.assertEqual(f.read(), expected_raw, path)

    def test_arabic_runtime_injector_pretranslated_ugui_font_switch_and_csc_compile(self):
        cs_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "injectors",
                "unity_runtime",
                "ArabicRuntimeInjector.cs",
            )
        )
        with open(cs_path, "r", encoding="utf-8") as f:
            cs_src = f.read()

        # يجب ألا تحجب HasOverrides استدعاء ApplyActiveUiTextOverrides أو تبديل خط UGUI للنصوص المعرّبة مسبقاً
        idx_apply = cs_src.index("private static void ApplyActiveUiTextOverrides()")
        body_apply = cs_src[idx_apply : idx_apply + 220]
        self.assertNotIn("if (!_store.HasOverrides)", body_apply)

        idx_os = cs_src.index("private static void ApplyOsFontTextOverrides(")
        body_os = cs_src[idx_os : idx_os + 3200]
        self.assertIn("bool hasArabic = false;", body_os)
        self.assertIn("ContainsArabicPresentationForms(curText)", body_os)
        self.assertIn("if (hasArabic)", body_os)

        idx_interop = cs_src.index("private static void UpdateUiComponentTexts(")
        body_interop = cs_src[idx_interop : idx_interop + 3500]
        self.assertIn("ContainsArabicPresentationForms(curText)", body_interop)
        self.assertIn("if (hasArabic && !isTmp && fontProp != null && fontType != null)", body_interop)

        csc = find_csc_compiler()
        if not csc:
            self.skipTest("csc.exe غير متوفر على هذا النظام")

        with tempfile.TemporaryDirectory() as tmp:
            # التحقق من مسار IL2CPP مباشرة عبر UnityInjector
            il2cpp_engine = EngineInfo(
                engine_name="Unity",
                supported=True,
                game_dir=tmp,
                architecture="x64",
                scripting_backend="IL2CPP",
                managed_dir=None,
                has_tmp=True,
                has_ugui=True,
            )
            il2cpp_dll = os.path.join(tmp, "ArabicIl2Cpp.dll")
            ok_il, msg_il = UnityInjector().compile_il2cpp_injector(il2cpp_engine, il2cpp_dll)
            self.assertTrue(ok_il, msg_il)

            # التحقق من مسار UGUI Mono بدون الحاجة لملفات ألعاب خارجية
            managed = os.path.join(tmp, "Stub_Data", "Managed")
            os.makedirs(managed)
            fx_dir = os.path.dirname(csc)
            for fx_dll in ("mscorlib.dll", "System.dll", "System.Core.dll"):
                shutil.copy2(os.path.join(fx_dir, fx_dll), os.path.join(managed, fx_dll))
            core_stub = r"""
using System;
namespace UnityEngine
{
    public enum HideFlags { None = 0, DontSave = 52, HideAndDontSave = 61 }
    public enum RuntimeInitializeLoadType { AfterSceneLoad = 0 }
    [AttributeUsage(AttributeTargets.Method)]
    public sealed class RuntimeInitializeOnLoadMethodAttribute : Attribute
    {
        public RuntimeInitializeOnLoadMethodAttribute(RuntimeInitializeLoadType t) {}
    }
    public class Object
    {
        public HideFlags hideFlags { get; set; }
        public string name { get; set; }
        public static void DontDestroyOnLoad(Object target) {}
        public static void Destroy(Object obj) {}
    }
    public class Component : Object { public GameObject gameObject { get { return null; } } }
    public class MonoBehaviour : Component {}
    public sealed class GameObject : Object
    {
        public GameObject(string name) {}
        public bool activeInHierarchy { get { return true; } }
        public T AddComponent<T>() where T : Component, new() { return new T(); }
    }
    public sealed class Camera : Component
    {
        public delegate void CameraCallback(Camera cam);
        public static event CameraCallback onPreRender;
    }
    public static class Application
    {
        public static string dataPath { get { return string.Empty; } }
        public static string streamingAssetsPath { get { return string.Empty; } }
        public static event Action onBeforeRender;
    }
    public static class Time { public static int frameCount { get { return 1; } } }
    public static class Resources { public static Object[] FindObjectsOfTypeAll(Type t) { return new Object[0]; } }
    public sealed class Font : Object
    {
        public Font(string name) {}
        public static Font CreateDynamicFontFromOSFont(string[] names, int size) { return null; }
        public bool HasCharacter(char c) { return false; }
    }
}
"""
            stub_cs = os.path.join(tmp, "UnityStub.cs")
            with open(stub_cs, "w", encoding="utf-8") as f:
                f.write(core_stub)
            core_dll = os.path.join(managed, "UnityEngine.CoreModule.dll")
            p_stub = subprocess.run(
                [csc, "/nologo", "/target:library", f"/out:{core_dll}", stub_cs],
                capture_output=True,
                text=True,
            )
            self.assertEqual(p_stub.returncode, 0, p_stub.stdout + p_stub.stderr)

            ugui_engine = EngineInfo(
                engine_name="Unity",
                supported=True,
                game_dir=tmp,
                architecture="x64",
                scripting_backend="Mono",
                managed_dir=managed,
                has_tmp=False,
                has_ugui=True,
            )
            ugui_dll = os.path.join(tmp, "ArabicUgui.dll")
            ok_ug, msg_ug = UnityInjector().compile_injector(ugui_engine, ugui_dll, include_input_utils=False)
            self.assertTrue(ok_ug, msg_ug)

    def test_detect_candidate_format_bracketed_plaintext_json_and_malformed_json_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            def write_utf16(name: str, text: str) -> str:
                p = os.path.join(tmp, name)
                with open(p, "wb") as f:
                    f.write(codecs.BOM_UTF16_LE + text.encode("utf-16-le"))
                return p

            def write_utf8(name: str, text: str) -> str:
                p = os.path.join(tmp, name)
                with open(p, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                return p

            p1 = write_utf16(
                "1.json",
                "[ Zmora ]\r\nUghhhh, my head...\r\nYou fixed the shields, didn't you?\r\n",
            )
            self.assertEqual(detect_candidate_format(p1, ".json"), "txt")

            p_ar = write_utf16(
                "1_ar.json",
                "[ ﺍﺭﻮﻣﺯ ]\r\nﺃﻭﻭﻩ، ﺭﺃﺳﻲ...\r\nﻟﻘﺪ ﺃﺻﻠﺤﺖ ﺍﻟﺪﺭﻭﻉ، أﻟﻴﺲ ﻛﺬﻟﻚ؟\r\nﺍﻧﺘﻈﺮ، ﻣﺎﺫﺍ؟\r\n",
            )
            self.assertEqual(detect_candidate_format(p_ar, ".json"), "txt")

            p_comment = write_utf8(
                "comment_plain.json",
                "// Translator note\nStart\nQuit\nOptions\n",
            )
            self.assertEqual(detect_candidate_format(p_comment, ".json"), "txt")

            p_brace = write_utf8(
                "brace_plain.json",
                "{Player} fixed the shields!\nNext line here.\n",
            )
            self.assertEqual(detect_candidate_format(p_brace, ".json"), "txt")

            p_num_bracket = write_utf8(
                "num_bracket.json",
                "[1] Intro\nWelcome to the tunnels.\nFix the shields.\n",
            )
            self.assertEqual(detect_candidate_format(p_num_bracket, ".json"), "txt")

            p_ini = write_utf8(
                "ini_in_json.json",
                "[Menu]\nPLAY=New Game\nQUIT=Exit\n",
            )
            self.assertEqual(detect_candidate_format(p_ini, ".json"), "ini")

            p_arr = write_utf8("arr.json", '["Hello", "World"]')
            self.assertEqual(detect_candidate_format(p_arr, ".json"), "json")

            p_arr_obj = write_utf8("arr_obj.json", '[{"a": "Hello"}]')
            self.assertEqual(detect_candidate_format(p_arr_obj, ".json"), "json")

            p_jsonc = write_utf8("jsonc.json", '// note\n{"a": "Hello",}')
            self.assertEqual(detect_candidate_format(p_jsonc, ".json"), "json")

            p_bad_obj = write_utf8("bad_obj.json", '{\n  "title": "Hello",\n  "broken": ')
            self.assertEqual(detect_candidate_format(p_bad_obj, ".json"), "json")

            p_bad_arr = write_utf8("bad_arr.json", '[\n  "Hello",\n  broken')
            self.assertEqual(detect_candidate_format(p_bad_arr, ".json"), "json")

            big_plain = "[ Zmora ]\r\n" + ("You fixed the shields, didn't you?\r\n" * 600)
            p_big_plain = write_utf16("big_plain.json", big_plain)
            self.assertGreater(os.path.getsize(p_big_plain), 16384)
            self.assertEqual(detect_candidate_format(p_big_plain, ".json"), "txt")

            big_json = '{"items": [' + ", ".join(f'"{i}: Hello world"' for i in range(1200)) + "]}"
            p_big_json = write_utf8("big_real.json", big_json)
            self.assertGreater(os.path.getsize(p_big_json), 16384)
            self.assertEqual(detect_candidate_format(p_big_json, ".json"), "json")

    def test_txt_handler_preserves_bracketed_lines_in_plaintext_but_keeps_ini_sections(self):
        handler_txt = get_format_handler("txt")
        self.assertIsNotNone(handler_txt)
        handler_ini = IniFormatHandler()

        with tempfile.TemporaryDirectory() as tmp:
            # 1. Awaria-like UTF-16-LE dialogue file
            chap_path = os.path.join(tmp, "4.json")
            chap_lines = [
                "[ Cutwire 2 ]\r\n",
                "[ Cutwire 1 ]\r\n",
                "HOOOOOOOOOOOOOOOOOOWL!\r\n",
                "UMMMM...\r\n",
                "See? I told you she would give you a kiss.\r\n",
            ]
            with open(chap_path, "wb") as f:
                f.write(codecs.BOM_UTF16_LE + "".join(chap_lines).encode("utf-16-le"))

            doc = handler_txt.load(chap_path)
            self.assertEqual(doc.format_name, "txt")
            self.assertEqual(
                [val for _, _, val in doc.entries],
                [
                    "[ Cutwire 2 ]",
                    "[ Cutwire 1 ]",
                    "HOOOOOOOOOOOOOOOOOOWL!",
                    "UMMMM...",
                    "See? I told you she would give you a kiss.",
                ],
            )
            replacements = {
                doc.entries[0][0]: "[ 2 ﺮﻳﺍﻮﺗﺎﻛ ]",
                doc.entries[1][0]: "[ 1 ﺮﻳﺍﻮﺗﺎﻛ ]",
                doc.entries[2][0]: "!ﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻭﻫ",
                doc.entries[3][0]: "...ﻡﻡﻡﻡﻡ",
                doc.entries[4][0]: ".ﺔﻠﺒﻗ ﻚﻴﻄﻌﺘﺳ ﺎﻬﻧﺃ ﻚﺗﺮﺒﺧﺃ ﺪﻘﻟ ؟ﻯﺮﺗﺃ",
            }
            handler_txt.save(chap_path, doc, replacements)
            with open(chap_path, "rb") as f:
                raw_saved = f.read()
            self.assertTrue(raw_saved.startswith(codecs.BOM_UTF16_LE))
            saved_lines = raw_saved.decode("utf-16").splitlines(keepends=True)
            self.assertEqual(len(saved_lines), 5)
            self.assertEqual(saved_lines[0].strip(), "[ 2 ﺮﻳﺍﻮﺗﺎﻛ ]")

            # 2. Menu-like file containing [default button: SPACE] and //TRANSLATOR NOTE
            m_path = os.path.join(tmp, "m.json")
            m_lines = [
                "DASH\r\n",
                "[default button: SPACE]\r\n",
                "//TRANSLATOR NOTE: keep\r\n",
            ]
            with open(m_path, "wb") as f:
                f.write(codecs.BOM_UTF16_LE + "".join(m_lines).encode("utf-16-le"))

            m_doc = handler_txt.load(m_path)
            self.assertEqual(
                [val for _, _, val in m_doc.entries],
                ["DASH", "[default button: SPACE]"],
            )

            # 3. KV-structured .txt and .ini still treat [Menu] as a section header
            kv_txt_path = os.path.join(tmp, "kv.txt")
            with open(kv_txt_path, "w", encoding="utf-8", newline="") as f:
                f.write("[Menu]\nPLAY=New Game\nQUIT=Exit\n")
            kv_doc = handler_txt.load(kv_txt_path)
            self.assertEqual(
                [(k, v) for _, k, v in kv_doc.entries],
                [("Menu.PLAY", "New Game"), ("Menu.QUIT", "Exit")],
            )

            ini_path = os.path.join(tmp, "real.ini")
            with open(ini_path, "w", encoding="utf-8", newline="") as f:
                f.write("[Menu]\nPLAY=New Game\nQUIT=Exit\n")
            ini_doc = handler_ini.load(ini_path)
            self.assertEqual(
                [(k, v) for _, k, v in ini_doc.entries],
                [("Menu.PLAY", "New Game"), ("Menu.QUIT", "Exit")],
            )

    def test_end_to_end_pipeline_awaria_style_bracketed_utf16_json_and_rescan(self):
        with tempfile.TemporaryDirectory() as tmp:
            gdir = os.path.join(tmp, "AwariaLike")
            managed = os.path.join(gdir, "Awaria_Data", "Managed")
            local_dir = os.path.join(gdir, "local")
            os.makedirs(managed)
            os.makedirs(local_dir)

            with open(os.path.join(gdir, "Awaria.exe"), "wb") as f:
                f.write(_make_minimal_x64_pe())
            with open(os.path.join(managed, "Assembly-CSharp.dll"), "wb") as f:
                f.write(_build_minimal_dotnet_pe_with_us(["/local/m.json"]))
            with open(os.path.join(managed, "UnityEngine.UI.dll"), "wb") as f:
                f.write(b"MZ" + b"\x00" * 64)

            files_spec = {
                os.path.join(local_dir, "m.json"): [
                    "Start\r\n",
                    "Settings\r\n",
                    "Quit\r\n",
                    "DASH\r\n",
                    "[default button: SPACE]\r\n",
                    "//TRANSLATOR NOTE: keep untranslated\r\n",
                ],
                os.path.join(local_dir, "1.json"): [
                    "[ Zmora ]\r\n",
                    "Ughhhh, my head... You did something...\r\n",
                    "You fixed the shields, didn't you?\r\n",
                ],
                os.path.join(local_dir, "4.json"): [
                    "[ Cutwire 2 ]\r\n",
                    "[ Cutwire 1 ]\r\n",
                    "HOOOOOOOOOOOOOOOOOOWL!\r\n",
                    "See? I told you she would give you a kiss.\r\n",
                ],
            }
            orig_bytes = {}
            for path, lines in files_spec.items():
                raw = codecs.BOM_UTF16_LE + "".join(lines).encode("utf-16-le")
                with open(path, "wb") as f:
                    f.write(raw)
                orig_bytes[path] = raw

            translations_ar = {
                "Start": "ابدأ",
                "Settings": "الإعدادات",
                "Quit": "خروج",
                "DASH": "اندفاع",
                "[default button: SPACE]": "[الزر الافتراضي: SPACE]",
                "[ Zmora ]": "[ زمورا ]",
                "Ughhhh, my head... You did something...": "أوووه، رأسي... لقد فعلت شيئاً...",
                "You fixed the shields, didn't you?": "لقد أصلحت الدروع، أليس كذلك؟",
                "[ Cutwire 2 ]": "[ كاتواير 2 ]",
                "[ Cutwire 1 ]": "[ كاتواير 1 ]",
                "HOOOOOOOOOOOOOOOOOOWL!": "هووووووووووووووووول!",
                "See? I told you she would give you a kiss.": "أترى؟ أخبرتك أنها ستعطيك قبلة.",
            }

            def fake_ep(batch_text, ep, src, tgt):
                return "\n".join(
                    "" if not ln.strip() else translations_ar.get(ln, "ترجمة: " + ln)
                    for ln in batch_text.split("\n")
                )

            pipe = AutoTranslatorPipeline(
                translator=GoogleBatchTranslator(inter_batch_delay=0.0, endpoint_caller=fake_ep)
            )
            rep = pipe.translate(gdir, skip_inject=True)
            self.assertTrue(rep.success, rep.errors)
            selected_rels = {c.relative_path.replace("\\", "/") for c in rep.selected_files}
            self.assertEqual(selected_rels, {"local/m.json", "local/1.json", "local/4.json"})

            for path, orig_lines in files_spec.items():
                with open(path, "rb") as f:
                    raw = f.read()
                self.assertTrue(raw.startswith(codecs.BOM_UTF16_LE), path)
                dec_lines = raw.decode("utf-16").splitlines(keepends=True)
                self.assertEqual(len(dec_lines), len(orig_lines), path)

            with open(os.path.join(local_dir, "m.json"), "r", encoding="utf-16") as f:
                m_out = f.read().splitlines()
            self.assertEqual(m_out[4], process_text("[الزر الافتراضي: SPACE]"))
            self.assertEqual(m_out[5], "//TRANSLATOR NOTE: keep untranslated")

            with open(os.path.join(local_dir, "1.json"), "r", encoding="utf-16") as f:
                c1_out = f.read().splitlines()
            self.assertEqual(c1_out[0], process_text("[ زمورا ]"))
            self.assertEqual(c1_out[1], process_text("أوووه، رأسي... لقد فعلت شيئاً..."))

            with open(os.path.join(local_dir, "4.json"), "r", encoding="utf-16") as f:
                c4_out = f.read().splitlines()
            self.assertEqual(c4_out[0], process_text("[ كاتواير 2 ]"))
            self.assertEqual(c4_out[1], process_text("[ كاتواير 1 ]"))

            # Re-scan after translation when 1.json and 4.json have 0 ASCII letters
            rescan = pipe.scan(gdir)
            self.assertTrue(rescan.success, rescan.errors)
            rescan_map = {
                c.relative_path.replace("\\", "/"): c for c in rescan.selected_files
            }
            self.assertEqual(set(rescan_map), {"local/m.json", "local/1.json", "local/4.json"})
            for rel, cand in rescan_map.items():
                self.assertEqual(cand.format_name, "txt", rel)
                self.assertTrue(cand.contains_arabic, rel)

            ok_rb, rb_warns = AutoTranslatorPipeline.rollback(gdir)
            self.assertTrue(ok_rb, rb_warns)
            for path, expected_raw in orig_bytes.items():
                with open(path, "rb") as f:
                    self.assertEqual(f.read(), expected_raw, path)


class Unity4LegacyImGuiAndCodeStringsTests(unittest.TestCase):
    def test_unity4_injector_compiles_without_unity5_apis_and_enables_imgui(self):
        csc = find_csc_compiler()
        if not csc:
            self.skipTest("csc.exe غير متوفر على هذا النظام")

        cs_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "injectors",
                "unity_runtime",
                "ArabicRuntimeInjector.cs",
            )
        )
        with open(cs_path, "r", encoding="utf-8") as f:
            cs_src = f.read()

        with tempfile.TemporaryDirectory() as tmp:
            managed = os.path.join(tmp, "U4Game_Data", "Managed")
            os.makedirs(managed)
            fx_dir = os.path.dirname(csc)
            for fx_dll in ("mscorlib.dll", "System.dll", "System.Core.dll"):
                shutil.copy2(os.path.join(fx_dir, fx_dll), os.path.join(managed, fx_dll))

            u4_stub = r"""
using System;
namespace UnityEngine
{
    public enum HideFlags { None = 0, DontSave = 52, HideAndDontSave = 61 }
    public struct Rect { public float x, y, width, height; }
    public class Object
    {
        public HideFlags hideFlags { get; set; }
        public string name { get; set; }
        public int GetInstanceID() { return 1; }
        public static void DontDestroyOnLoad(Object target) {}
        public static void Destroy(Object obj) {}
    }
    public class Texture : Object {}
    public class Component : Object { public GameObject gameObject { get { return null; } } }
    public class Behaviour : Component {}
    public class MonoBehaviour : Behaviour {}
    public sealed class GameObject : Object
    {
        public GameObject(string name) {}
        public bool activeInHierarchy { get { return true; } }
        public T AddComponent<T>() where T : Component, new() { return new T(); }
    }
    public sealed class Camera : Behaviour {}
    public static class Application
    {
        public static string dataPath { get { return string.Empty; } }
        public static string streamingAssetsPath { get { return string.Empty; } }
    }
    public static class Time { public static int frameCount { get { return 1; } } }
    public static class Resources
    {
        public static Object[] FindObjectsOfTypeAll(Type t) { return new Object[0]; }
        public static Object GetBuiltinResource(Type t, string path) { return null; }
    }
    public sealed class Font : Object
    {
        public Font() {}
        public Font(string name) {}
        public string[] fontNames { get; set; }
        public bool dynamic { get { return true; } }
        public void RequestCharactersInTexture(string characters) {}
        public bool HasCharacter(char c) { return false; }
    }
    public sealed class GUIText : Component
    {
        public string text { get; set; }
        public Font font { get; set; }
    }
    public sealed class TextMesh : Component
    {
        public string text { get; set; }
        public Font font { get; set; }
    }
    public sealed class GUIContent
    {
        private static GUIContent s_Text = new GUIContent();
        public GUIContent() {}
        public GUIContent(string text) { this.text = text; }
        public string text { get; set; }
        public string tooltip { get; set; }
        public static GUIContent Temp(string t)
        {
            s_Text.text = t;
            return s_Text;
        }
    }
    public sealed class GUIStyle
    {
        public Font font { get; set; }
    }
    public sealed class GUISkin : Object
    {
        public Font font { get; set; }
        public GUIStyle box { get; set; }
        public GUIStyle label { get; set; }
        public GUIStyle button { get; set; }
        public GUIStyle toggle { get; set; }
        public GUIStyle textField { get; set; }
        public GUIStyle textArea { get; set; }
        public GUIStyle window { get; set; }
        public GUIStyle[] customStyles { get; set; }
    }
    public sealed class GUILayoutOption {}
    public static class GUI
    {
        public static GUISkin skin { get; set; }
        public static void Label(Rect pos, GUIContent content, GUIStyle style) {}
        public static bool Button(Rect pos, GUIContent content, GUIStyle style) { return false; }
        public static void Box(Rect pos, GUIContent content, GUIStyle style) {}
        internal static void DoLabel(Rect pos, GUIContent content, IntPtr style) {}
        internal static void INTERNAL_CALL_DoLabel(ref Rect pos, GUIContent content, IntPtr style) {}
        internal static bool DoButton(Rect pos, GUIContent content, IntPtr style) { return false; }
        internal static bool INTERNAL_CALL_DoButton(ref Rect pos, GUIContent content, IntPtr style) { return false; }
    }
    public static class GUILayout
    {
        internal static bool DoButton(GUIContent content, GUIStyle style, params GUILayoutOption[] options) { return false; }
        internal static void DoLabel(GUIContent content, GUIStyle style, params GUILayoutOption[] options) {}
        internal static void DoBox(GUIContent content, GUIStyle style, params GUILayoutOption[] options) {}
    }
    public static class GUILayoutUtility
    {
        public static Rect GetRect(GUIContent content, GUIStyle style, params GUILayoutOption[] options) { return new Rect(); }
    }
}
"""
            stub_cs = os.path.join(tmp, "Unity4Stub.cs")
            with open(stub_cs, "w", encoding="utf-8") as f:
                f.write(u4_stub)
            ue_dll = os.path.join(managed, "UnityEngine.dll")
            p_stub = subprocess.run(
                [csc, "/nologo", "/target:library", f"/out:{ue_dll}", stub_cs],
                capture_output=True,
                text=True,
            )
            self.assertEqual(p_stub.returncode, 0, p_stub.stdout + p_stub.stderr)

            u4_engine = EngineInfo(
                engine_name="Unity",
                supported=True,
                game_dir=tmp,
                architecture="x86",
                scripting_backend="Mono",
                managed_dir=managed,
                has_tmp=False,
                has_ugui=False,
                unity_version="4.5.5p5",
            )
            out_dll = os.path.join(managed, "ArabicRuntimeInjector.dll")
            ok, msg = UnityInjector().compile_injector(u4_engine, out_dll, include_input_utils=False)
            self.assertTrue(ok, msg)
            self.assertTrue(os.path.isfile(out_dll))

            self.assertIn("#if HAS_RUNTIME_INIT_LOAD", cs_src)
            self.assertIn("#if HAS_IMGUI", cs_src)
            self.assertIn("MonoWndProcBootstrap", cs_src)
            self.assertIn("MonoImGuiInterceptor", cs_src)
            self.assertIn("GetGuiTextType", cs_src)

            runner_cs = os.path.join(tmp, "Runner.cs")
            with open(runner_cs, "w", encoding="utf-8") as f:
                f.write(
                    "using System;\n"
                    "using System.Collections;\n"
                    "using System.Reflection;\n"
                    "class Runner {\n"
                    "  static void Main() {\n"
                    "    Console.OutputEncoding = System.Text.Encoding.UTF8;\n"
                    "    Assembly asm = typeof(Doorstop.Entrypoint).Assembly;\n"
                    "    Type t = asm.GetType(\"ArabicTranslatorRuntime.OverrideTranslationStore\", true);\n"
                    "    object store = Activator.CreateInstance(t);\n"
                    "    t.GetField(\"HasOverrides\").SetValue(store, true);\n"
                    "    IDictionary dict = (IDictionary)t.GetField(\"BySource\").GetValue(store);\n"
                    "    dict[\"START\"] = \"\\uFE83\\uFEAA\\uFE91\\uFE8D\";\n"
                    "    object[] args = new object[] { \"\\u25B8 START \\u25C2\", null };\n"
                    "    bool ok = (bool)t.GetMethod(\"TryTranslateRuntimeText\").Invoke(store, args);\n"
                    "    Console.WriteLine(ok + \"|\" + args[1]);\n"
                    "  }\n"
                    "}\n"
                )
            runner_exe = os.path.join(managed, "Runner.exe")
            p_run_build = subprocess.run(
                [csc, "/nologo", "/target:exe", f"/r:{out_dll}", f"/out:{runner_exe}", runner_cs],
                capture_output=True,
                text=True,
            )
            self.assertEqual(p_run_build.returncode, 0, p_run_build.stdout + p_run_build.stderr)
            run_res = subprocess.run(
                [runner_exe],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(run_res.returncode, 0, run_res.stdout + run_res.stderr)
            self.assertEqual(run_res.stdout.strip(), "True|▸ \uFE83\uFEAA\uFE91\uFE8D ◂")

    def test_unity_code_extracts_menu_words_wrapped_labels_and_multiline_credits(self):
        accepted = [
            "START",
            "CREDITS",
            "Return",
            "▸ Return ◂",
            "CONTINUE",
            "QUIT",
            'Created by Krillbite Studio \n\n"The Death of Aase" \nWritten by Edvard Grieg \nPerformed by Oslo Camerata \nUnder license from Naxos International \n\nOriginal fly by sebastian_k',
        ]
        rejected = [
            "Could not find the desired source. List: {0} - Type: {1}\nCheck that it's set in the AudioClipManager.",
            "void main() {\n  return 0;\n}",
            "ACH_EASTEREGG",
            "Controller_Mouse X",
            "Hidden/Post FX/Bloom",
        ]
        for s in accepted:
            self.assertTrue(_is_clean_code_ui_string(s), f"Should accept UI string: {s!r}")
        for s in rejected:
            self.assertFalse(_is_clean_code_ui_string(s), f"Should reject noisy string: {s!r}")


if __name__ == "__main__":
    unittest.main()
