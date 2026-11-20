//=============================================================================
// AutoTranslator_ArabicText.js
//=============================================================================
/*:
 * @target MV MZ
 * @plugindesc [AutoTranslator] Draws Arabic message text as connected words/lines (no isolated letters) with RTL lines.
 * @author AutoTranslator
 *
 * @param Enabled
 * @text Enabled
 * @type boolean
 * @default true
 *
 * @param Mode
 * @text Grouping mode
 * @desc word = typewriter effect word by word (connected letters). line = draw each segment at once.
 * @type select
 * @option word
 * @option line
 * @default word
 *
 * @param RtlMessages
 * @text RTL message lines (MV)
 * @desc Right-align and lay out Arabic dialogue lines from right to left in RPG Maker MV.
 * @type boolean
 * @default true
 *
 * @command Toggle
 * @text Enable / disable Arabic text fix
 * @arg enabled
 * @type boolean
 * @default true
 *
 * @help
 * Generated automatically by Auto Translator (Arabic localization tool).
 *
 * RPG Maker MV draws message text one character at a time
 * (Window_Base.processNormalCharacter) and RPG Maker MZ flushes the text
 * buffer after every character while the typewriter effect is running.
 * Drawing Arabic letters one by one breaks their contextual (joined)
 * forms and the bidirectional order. This plugin keeps every Arabic word
 * (or the whole segment in "line" mode) in one drawText call while
 * keeping escape codes (\C[n], \I[n], \., \|, \{ ...) working.
 *
 * MV plugin command:  ArabicText on | off | word | line
 * MZ plugin command:  Toggle
 *
 * The tool's Rollback removes this file and restores js/plugins.js.
 */

(function() {
    'use strict';

    var PLUGIN_NAME = 'AutoTranslator_ArabicText';
    var params = {};
    try {
        if (typeof PluginManager !== 'undefined' && PluginManager.parameters) {
            params = PluginManager.parameters(PLUGIN_NAME) || {};
        }
    } catch (e) {
        params = {};
    }

    var ArabicText = {
        version: '1.0.0',
        enabled: String(params['Enabled'] || 'true') !== 'false',
        mode: String(params['Mode'] || 'word').toLowerCase() === 'line' ? 'line' : 'word',
        rtlMessages: String(params['RtlMessages'] || 'true') !== 'false'
    };
    window.AutoTranslatorArabicText = ArabicText;

    var ARABIC_RE = /[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]/;
    var LATIN_RE = /[A-Za-z\u00C0-\u024F]/;
    var RLE = '\u202B';
    var PDF = '\u202C';

    function isArabic(ch) { return !!ch && ARABIC_RE.test(ch); }
    function isControl(ch) { return !ch || ch.charCodeAt(0) < 0x20; }
    function isSpace(ch) { return ch === ' ' || ch === '\u00A0' || ch === '\u3000'; }

    // تخطي رمز هروب محوَّل (\x1b + حرف/رمز + [param] اختياري)
    function skipEscape(text, i) {
        var j = i + 1;
        var ch = text[j];
        if (!ch) { return j; }
        if (/[A-Za-z]/.test(ch)) {
            while (j < text.length && /[A-Za-z]/.test(text[j])) { j++; }
            var close = -1;
            if (text[j] === '[') { close = text.indexOf(']', j); }
            else if (text[j] === '<') { close = text.indexOf('>', j); }
            if (close >= 0) { j = close + 1; }
            return j;
        }
        return j + 1;
    }

    // الاتجاه الأساسي للسطر حسب أول حرف قوي (قاعدة P2 في Unicode BiDi)
    ArabicText.lineIsRtl = function(text, index) {
        var i = index || 0;
        while (i < text.length) {
            var ch = text[i];
            if (ch === '\n' || ch === '\f') { break; }
            if (ch === '\x1b') { i = skipEscape(text, i); continue; }
            if (isArabic(ch)) { return true; }
            if (LATIN_RE.test(ch)) { return false; }
            i++;
        }
        return false;
    };

    // نهاية المقطع الحالي حتى أقرب رمز تحكم (\n, \f, \x1b)
    ArabicText.segmentEnd = function(text, index) {
        var i = index;
        while (i < text.length && !isControl(text[i])) { i++; }
        return i;
    };

    ArabicText.segmentHasArabic = function(text, index) {
        var end = ArabicText.segmentEnd(text, index);
        for (var i = index; i < end; i++) {
            if (isArabic(text[i])) { return true; }
        }
        return false;
    };

    // هل يحتوي السطر الحالي كاملاً (بين \n السابق والتالي) على حروف عربية؟
    ArabicText.lineHasArabic = function(text, index) {
        var i = index - 1;
        while (i >= 0 && text[i] !== '\n' && text[i] !== '\f') {
            if (isArabic(text[i])) { return true; }
            i--;
        }
        i = index;
        while (i < text.length && text[i] !== '\n' && text[i] !== '\f') {
            if (isArabic(text[i])) { return true; }
            i++;
        }
        return false;
    };

    // القطعة التي تُرسم دفعة واحدة: كلمة + الفراغات التالية لها، أو تتابع كلمات لاتينية كاملة
    ArabicText.pieceEnd = function(text, index, mode) {
        var end = ArabicText.segmentEnd(text, index);
        if (mode === 'line') { return end; }
        var i = index;
        // الفراغات البادئة (أول السطر) تُضم إلى الكلمة التالية
        while (i < end && isSpace(text[i])) { i++; }
        var wordStart = i;
        while (i < end && !isSpace(text[i])) { i++; }
        var word = text.substring(wordStart, i);
        var latinRun = !ARABIC_RE.test(word) && LATIN_RE.test(word);
        while (i < end && isSpace(text[i])) { i++; }
        if (latinRun) {
            // ضم الكلمات اللاتينية المتتالية في قطعة واحدة لإبقاء ترتيبها LTR داخل السطر العربي
            while (i < end) {
                var j = i;
                while (j < end && !isSpace(text[j])) { j++; }
                var next = text.substring(i, j);
                if (ARABIC_RE.test(next) || !LATIN_RE.test(next)) { break; }
                i = j;
                while (i < end && isSpace(text[i])) { i++; }
            }
        }
        return Math.max(i, index + 1);
    };

    ArabicText.wrapDirection = function(piece) {
        return ArabicText.lineIsRtl(piece, 0) ? RLE + piece + PDF : piece;
    };

    function drawPiece(win, textState, piece, rtl) {
        var width = win.textWidth(piece);
        var drawn = ArabicText.wrapDirection(piece);
        if (rtl) {
            textState.x -= width;
            win.contents.drawText(drawn, textState.x, textState.y, width * 2, textState.height);
        } else {
            win.contents.drawText(drawn, textState.x, textState.y, width * 2, textState.height);
            textState.x += width;
        }
    }

    var isMZ = typeof Window_Base !== 'undefined' && typeof Window_Base.prototype.flushTextState === 'function';
    ArabicText.isMZ = isMZ;

    //-------------------------------------------------------------------------
    // RPG Maker MV: processNormalCharacter يرسم حرفاً واحداً في كل مرة
    //-------------------------------------------------------------------------
    if (!isMZ && typeof Window_Base !== 'undefined') {
        var _MV_processNormalCharacter = Window_Base.prototype.processNormalCharacter;
        Window_Base.prototype.processNormalCharacter = function(textState) {
            if (!ArabicText.enabled || !textState || typeof textState.text !== 'string') {
                return _MV_processNormalCharacter.apply(this, arguments);
            }
            var text = textState.text;
            var index = textState.index;
            var end;
            if (textState.arabicRtl) {
                end = ArabicText.pieceEnd(text, index, ArabicText.mode);
                drawPiece(this, textState, text.substring(index, end), true);
                textState.index = end;
                return;
            }
            if (ArabicText.segmentHasArabic(text, index)) {
                end = ArabicText.segmentEnd(text, index);
                drawPiece(this, textState, text.substring(index, end), false);
                textState.index = end;
                return;
            }
            return _MV_processNormalCharacter.apply(this, arguments);
        };

        ArabicText.setupMessageLine = function(win, textState) {
            textState.arabicRtl = !!(ArabicText.enabled && ArabicText.rtlMessages &&
                ArabicText.lineIsRtl(textState.text || '', textState.index || 0));
            if (textState.arabicRtl) {
                textState.x = win.contentsWidth();
            }
        };

        if (typeof Window_Message !== 'undefined') {
            var _MV_newPage = Window_Message.prototype.newPage;
            Window_Message.prototype.newPage = function(textState) {
                _MV_newPage.apply(this, arguments);
                ArabicText.setupMessageLine(this, textState);
            };
        }

        var _MV_processNewLine = Window_Base.prototype.processNewLine;
        Window_Base.prototype.processNewLine = function(textState) {
            _MV_processNewLine.apply(this, arguments);
            if (typeof Window_Message !== 'undefined' && this instanceof Window_Message) {
                ArabicText.setupMessageLine(this, textState);
            } else if (textState) {
                textState.arabicRtl = false;
            }
        };

        var _MV_processDrawIcon = Window_Base.prototype.processDrawIcon;
        Window_Base.prototype.processDrawIcon = function(iconIndex, textState) {
            if (ArabicText.enabled && textState && textState.arabicRtl) {
                textState.x -= Window_Base._iconWidth + 4;
                this.drawIcon(iconIndex, textState.x + 2, textState.y + 2);
                return;
            }
            return _MV_processDrawIcon.apply(this, arguments);
        };
    }

    //-------------------------------------------------------------------------
    // RPG Maker MZ: canBreakHere يفرّغ المخزن بعد كل حرف أثناء تأثير الآلة الكاتبة
    //-------------------------------------------------------------------------
    if (isMZ && typeof Window_Message !== 'undefined' && Window_Message.prototype.canBreakHere) {
        var _MZ_canBreakHere = Window_Message.prototype.canBreakHere;
        Window_Message.prototype.canBreakHere = function(textState) {
            if (!_MZ_canBreakHere.apply(this, arguments)) { return false; }
            if (!ArabicText.enabled || !textState || typeof textState.text !== 'string') { return true; }
            var text = textState.text;
            var i = textState.index;
            var prev = text[i - 1];
            var next = text[i];
            if (isControl(prev) || isControl(next)) { return true; }
            var arabicContext = textState.rtl || isArabic(prev) || isArabic(next) ||
                ArabicText.lineHasArabic(text, i);
            if (!arabicContext) { return true; }
            if (ArabicText.mode === 'line') { return false; }
            // لا نكسر داخل الكلمة؛ الكسر مسموح فقط بعد الفراغات وقبل بداية كلمة جديدة
            return isSpace(prev) && !isSpace(next);
        };
    }

    //-------------------------------------------------------------------------
    // أوامر الإضافة
    //-------------------------------------------------------------------------
    if (typeof Game_Interpreter !== 'undefined' && Game_Interpreter.prototype.pluginCommand) {
        var _pluginCommand = Game_Interpreter.prototype.pluginCommand;
        Game_Interpreter.prototype.pluginCommand = function(command, args) {
            _pluginCommand.apply(this, arguments);
            if (String(command).toLowerCase() === 'arabictext') {
                var arg = String((args && args[0]) || '').toLowerCase();
                if (arg === 'on') { ArabicText.enabled = true; }
                else if (arg === 'off') { ArabicText.enabled = false; }
                else if (arg === 'word' || arg === 'line') { ArabicText.mode = arg; }
            }
        };
    }
    if (isMZ && typeof PluginManager !== 'undefined' && PluginManager.registerCommand) {
        PluginManager.registerCommand(PLUGIN_NAME, 'Toggle', function(args) {
            ArabicText.enabled = String(args && args.enabled) !== 'false';
        });
    }
})();
