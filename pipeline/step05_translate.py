"""Step 5: Translate — tier-based fallback with multi-language support.

Cổng chuẩn từ sever_v2/sever/services/translation_service.py:
- Index-based output [{"index","translated"}] — không lệch hàng khi model bỏ sót
- Context chain (history 20 segment) — xưng hô/tên nhân vật nhất quán xuyên chunks
- Retry 2 lần + cooldown khi rate-limit (429/402/403) — không đập API
- Partial success — segment dịch được giữ, chỉ phần fail chuyển tier sau
- Parse linh hoạt: SSE stream, JSON lồng string, code fence, raw_decode
- Prompt per-language; prompt Việt cấm phiên âm, chuẩn TTS
"""
import asyncio
import json
import logging
import re
import time
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = {
    "vi": "Tiếng Việt",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "zh": "中文",
    "th": "ไทย",
    "id": "Bahasa Indonesia",
    "ms": "Bahasa Melayu",
    "tl": "Filipino",
    "hi": "हिन्दी",
}

LANGUAGE_RULES = {
    "vi": {
        "name": "Vietnamese",
        "prompt": """Bạn là đạo diễn biên dịch & lồng tiếng phim chuyên nghiệp.
Nhiệm vụ: Dịch các câu thoại video từ tiếng Trung sang tiếng Việt để lồng tiếng (Voiceover/Dubbing) với mạch truyện liền mạch và nhất quán tuyệt đối.

QUY TẮC BẮT BUỘC:
1. MẠCH TRUYỆN & NHẤT QUÁN XƯNG HÔ (CỰC KỲ QUAN TRỌNG):
   - Đọc kỹ phần `RECENT TRANSLATED CONTEXT` để nắm mối quan hệ giữa các nhân vật và cách xưng hô của các câu trước.
   - TUYỆT ĐỐI KHÔNG thay đổi cách xưng hô giữa chừng trong cùng một đoạn hội thoại (Ví dụ: Nếu đoạn trước đang xưng "ta - ngươi" hoặc "huynh - muội" hoặc "anh - em" hoặc "tôi - cậu" thì các câu tiếp theo của nhân vật đó BẮT BUỘC giữ nguyên cặp xưng hô đó, không được nhảy lung tung sang "ông - tôi", "mày - tao").
   - Xác định đúng ngôi xưng theo bối cảnh:
     + Đô thị / Hiện đại / Hài hước: tôi - bạn, anh - em, cậu - tớ, sếp - em, lão đại, hắn, cô ta...
     + Cổ trang / Tiên hiệp / Kiếm hiệp: ta - ngươi, huynh - đệ - muội, tiền bối - vãn bối, sư phụ - đồ nhi, chưởng môn...
2. TÊN RIÊNG & ĐỊA DANH (HÁN VIỆT CHUẨN):
   - Chuyển toàn bộ tên người, môn phái, địa danh, chiêu thức sang âm Hán Việt chuẩn quen thuộc trong phim ảnh (ví dụ: Lâm Phong, Tiêu Viêm, Trương Tam, Lục Tuyết Kỳ, Thiếu Lâm, Võ Đang, Thanh Vân Môn, Thái Hư Quyền...).
   - Tuyệt đối KHÔNG giữ pinyin (Zhang San, Xiao Yan) và KHÔNG dịch nghĩa đen ngớ ngẩn (không dịch Lâm Phong thành "Gió Rừng").
3. KHẨU NGỮ ĐIỆN ẢNH & SIÊU SÚC TÍCH:
   - Dịch thoát ý, tự nhiên, nhịp nhàng theo hơi thở nhân vật. Lược bỏ từ đệm rườm rà để số từ tiếng Việt ngắn gọn tương đương câu gốc, giúp giọng đọc TTS lồng tiếng vừa vặn, không bị tràn timeline.
4. TỐI ƯU CHO GIỌNG ĐỌC AI (TTS):
   - Câu ngắn gọn, mạch lạc, dễ phát âm.
   - KHÔNG dùng dấu ngoặc kép (""), ngoặc vuông ([]), emoji hay ký hiệu lạ trong câu.
5. ĐỊNH DẠNG ĐẦU RA:
   - Chỉ xuất DUY NHẤT một mảng JSON: [{"index": 0, "translated": "..."}]. Không giải thích, không thêm bất kỳ văn bản nào ngoài JSON.""",
    },
    "en": {
        "name": "English",
        "prompt": """You translate Chinese web novel dialogue into natural, conversational English for voiceover.

RULES:
1. Sound like a real person speaking - not a textbook. Use contractions (don't, I'll, gonna) where natural.
2. DO NOT use Chinese pinyin or phonetic transliteration for names, places, or terms. Translate all character/place names and cultivation terms directly to their literal meaning or English equivalents.
3. Match tone: formal for authority, casual for friends, arrogant for villains.
4. BE CONCISE: Cut out wordy phrases, fluff, and redundant words. Short, punchy sentences. Easy to read aloud. No flowery prose.
5. Output JSON only. No notes, no markers.
""",
    },
    "th": {
        "name": "Thai",
        "prompt": """คุณเป็นนักแปลบทสนทนานิยายจีนสู่ภาษาไทยสำหรับพากย์เสียง.

กฎ:
1. แปลเป็นภาษาไทยที่ฟังเป็นธรรมชาติ พูดง่าย ไม่ยึดติดคำต่อคำ
2. ห้ามใช้การถอดเสียงภาษาจีนหรือพินอิน (pinyin) สำหรับชื่อตัวละครและสถานที่. ให้แปลความหมายโดยตรงของชื่อและคำศัพท์เฉพาะทั้งหมดเป็นภาษาไทย
3. ใช้คำสรรพนามเหมาะสมกับลำดับชั้น (ผม/ฉัน/ข้า แล้วแต่สถานะ)
4. ประโยคสั้น กระชับ ได้ใจความ เหมาะกับการพูด
5. ส่งออก JSON เท่านั้น
""",
    },
    "id": {
        "name": "Indonesian",
        "prompt": """Anda penerjemah dialog novel China ke Bahasa Indonesia untuk sulih suara.

ATURAN:
1. Terjemahkan secara alami seperti percakapan sehari-hari. Tidak kaku.
2. JANGAN menggunakan pinyin atau transliterasi fonetik untuk nama tokoh, tempat, atau istilah. Terjemahkan arti langsung dari nama dan istilah tersebut ke dalam Bahasa Indonesia.
3. Gunakan kata ganti sesuai konteks (aku/saya, kamu/Anda, dia/ia).
4. Kalimat pendek, padat, mudah diucapkan.
5. Output JSON saja.
""",
    },
    "ms": {
        "name": "Malay",
        "prompt": """Anda penterjemah dialog novel Cina ke Bahasa Melayu untuk alih suara.

PERATURAN:
1. Terjemah secara semula jadi seperti perbualan harian. Tidak kaku.
2. JANGAN gunakan pinyin atau transliterasi fonetik untuk nama watak, tempat, atau istilah. Terjemahkan maksud secara langsung ke Bahasa Melayu.
3. Gunakan kata ganti mengikut konteks (aku/saya, kamu/anda, dia).
4. Ayat pendek, padat, mudah disebut.
5. Output JSON sahaja.
""",
    },
    "ja": {
        "name": "Japanese",
        "prompt": """あなたは中国小説の会話を日本語に吹き替え用に翻訳しています。

ルール：
1. 自然な日本語に翻訳。一字一句訳さない。
2. キャラ名・地名・修行用語はピンインや中国語表記のままにせず、その意味を直接日本語に翻訳してください（意訳・和訳）。
3. 呼称は立場に合わせて（私/僕/俺、あなた/君/お前）。
4. 短く簡潔に、話しやすく。意味は十分に。
5. JSONのみ出力。
""",
    },
    "ko": {
        "name": "Korean",
        "prompt": """당신은 중국 소설 대화문을 한국어로 더빙용 번역하고 있습니다.

규칙:
1. 자연스러운 한국어로 번역하세요. 직역하지 마세요.
2. 인물명, 지명, 수련 용어는 pinyin이나 한자음 독음을 유지하지 말고, 해당 단어의 직접적인 의미를 한국어로 번역해 주세요.
3. 호칭은 관계에 맞게 (저/나, 당신/너/그대). 높임말/반말 구분.
4. 짧고 간결하게, 발화하기 쉽게. 의미는 충분히 전달.
5. JSON만 출력.
""",
    },
    "hi": {
        "name": "Hindi",
        "prompt": """आप चीनी उपन्यास संवाद को हिंदी में डबिंग के लिए अनुवाद कर रहे हैं।

नियम:
1. स्वाभाविक हिंदी में अनुवाद करें। शब्द-दर-शब्द नहीं।
2. नाम, स्थान, शब्दों को पिनयिन या चीनी ध्वन्यात्मक रूप में न रखें — सीधे अर्थ का हिंदी अनुवाद करें।
3. संबोधन संदर्भ के अनुसार (मैं/हम, तुम/आप)।
4. छोटे, स्पष्ट वाक्य — बोलने में आसान।
5. केवल JSON आउटपुट।
""",
    },
}

_JSON_FMT = """Translate these segments and return ONLY a valid JSON array in exactly this format:
[{"index": 0, "translated": "..."}, {"index": 1, "translated": "..."}]

Do NOT include any text outside the JSON array. No explanations, no notes, no metadata."""

def _base_segment_prompt(source_lang: str, target_lang: str) -> str:
    rules = LANGUAGE_RULES.get(target_lang, {})
    custom = rules.get("prompt", "")
    if custom:
        return custom + "\n\n" + _JSON_FMT
    src = source_lang.upper()
    tgt = target_lang.upper()
    return f"""Translate the following segments from {src} to {tgt}.

GENERAL RULES:
1. Translate naturally, not word-for-word. Do NOT use pinyin or phonetic transliteration for names/places - translate their direct meaning.
2. Match speaker tone - formal, casual, arrogant as appropriate.
3. Output clean, short sentences suitable for voiceover.
4. Return ONLY valid JSON array: [{{"index": 0, "translated": "..."}}, {{"index": 1, "translated": "..."}}]
"""

def _base_title_prompt(source_lang: str, target_lang: str) -> str:
    rules = LANGUAGE_RULES.get(target_lang, {})
    name = rules.get("name", target_lang.upper())
    return f"""Translate and compress the following title from {source_lang.upper()} to {name}.

RULES:
1. SUMMARISE the core meaning in AT MOST 80 CHARACTERS. This is a video title - be punchy, not wordy. Drop fluff, keep key nouns and verbs.
2. Do NOT just truncate or cut characters - rephrase the essence concisely.
3. Proper names -> use natural {name} transliteration or direct meaning translation (NO pinyin).
4. If RECENT TITLES are provided below, use them as context: ensure this title fits the series naming pattern and doesn't repeat the same info.
5. Clean output: JSON only, no notes, no markdown.
6. Output format: [{{"index": 0, "translated": "..."}}]
"""

class TranslationValidator:
    CHINESE_PATTERN = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]')
    ARTIFACT_PATTERNS = [
        r'^đây là bản dịch:?\s*', r'^bản dịch:?\s*', r'^translation:?\s*',
        r'^result:?\s*', r'^dịch:?\s*', r'^```', r'^json\s*',
        r'^you\s+said:?\s*', r'^current\s+request:?\s*', r'^kết\s+quả\s+dịch:?\s*',
        r'^video\s+bilibili\s*$', r'^bilibili\s+video\s*$',
    ]
    ARTIFACT_REGEX = [re.compile(p, re.IGNORECASE) for p in ARTIFACT_PATTERNS]

    def has_chinese(self, text: str) -> bool:
        return bool(self.CHINESE_PATTERN.search(text))

    def clean_text(self, text: str) -> str:
        fixed = str(text or "").strip()
        fixed = re.sub(r'^```(?:json)?\s*', '', fixed, flags=re.IGNORECASE)
        fixed = re.sub(r'\s*```$', '', fixed)
        for pattern in self.ARTIFACT_REGEX:
            fixed = pattern.sub('', fixed).strip()
        garbage_prefixes = [
            r'^you\s+said:?\s*', r'^current\s+request:?\s*', r'^translated:?\s*',
            r'^kết\s+quả\s+dịch:?\s*', r'^bản\s+dịch:?\s*'
        ]
        for gp in garbage_prefixes:
            fixed = re.sub(gp, '', fixed, flags=re.IGNORECASE | re.MULTILINE).strip()

        # Dọn dẹp dấu câu lạ, ngoặc kép, ký tự full-width để giọng TTS mượt mà
        fixed = fixed.replace('“', '').replace('”', '').replace('"', '').replace("'", "")
        fixed = fixed.replace('「', '').replace('」', '').replace('『', '').replace('』', '')
        fixed = fixed.replace('《', '').replace('》', '').replace('【', '').replace('】', '')
        fixed = fixed.replace('（', '(').replace('）', ')')
        fixed = fixed.replace('，', ', ').replace('。', '. ').replace('！', '! ').replace('？', '? ')
        fixed = fixed.replace('：', ': ').replace('；', '; ')
        fixed = re.sub(r'\s+', ' ', fixed).strip()
        return fixed

    def is_valid(self, original: str, translated: str) -> bool:
        if not translated or not translated.strip():
            return False
        if translated.strip().lower() in ("video bilibili", "bilibili video", "bilibili_video", "bilibili"):
            return False
        if self.has_chinese(translated):
            return False
        if not re.search(r'[a-zA-Z0-9\u00C0-\u1EF9\u0E00-\u0E7F\u0E80-\u0EFF\u1780-\u17FF\u0900-\u097F\u3040-\u309F\uAC00-\uD7AF]', translated):
            return False
        if len(original) > 10 and len(translated) < 2:
            return False
        return True

def _extract_json_payload(content: str) -> str:
    clean = content.strip()
    clean = re.sub(r'^```(?:json)?\s*', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'\s*```$', '', clean)
    try:
        decoder = json.JSONDecoder()
        idx_brace = clean.find('{')
        idx_bracket = clean.find('[')
        start_chars = []
        if idx_brace != -1 and idx_bracket != -1:
            start_chars = ['{', '['] if idx_brace < idx_bracket else ['[', '{']
        elif idx_brace != -1:
            start_chars = ['{']
        elif idx_bracket != -1:
            start_chars = ['[']
        for start_char in start_chars:
            start = clean.find(start_char)
            if start != -1:
                try:
                    _obj, end = decoder.raw_decode(clean[start:])
                    return clean[start:start + end]
                except Exception:
                    continue
    except Exception:
        pass
    return clean

def _unpack_sse_response(raw: str) -> str:
    lines = raw.strip().split("\n")
    sse_lines = [ln for ln in lines if ln.strip().startswith("data: ")]
    if not sse_lines:
        try:
            decoder = json.JSONDecoder()
            start = raw.find("{")
            if start != -1:
                obj, _ = decoder.raw_decode(raw[start:])
                c = obj.get("choices", [{}])[0].get("message", {}).get("content", "")
                if c:
                    return c
        except Exception:
            pass
        return raw
    non_sse = [ln for ln in lines if ln.strip() and not ln.strip().startswith("data: ")]
    if non_sse:
        try:
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(non_sse[0].strip())
            content = obj.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                return content
        except Exception:
            pass
    contents = []
    for sse_line in sse_lines:
        json_str = sse_line[6:].strip()
        if json_str == "[DONE]" or not json_str:
            continue
        try:
            chunk = json.loads(json_str)
        except json.JSONDecodeError:
            continue
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {}) or {}
            frag = delta.get("content", "") or choice.get("message", {}).get("content", "")
            if frag:
                contents.append(frag)
    return "".join(contents) if contents else raw

def _parse_indexed_items(items: list, original: list[str], key_names: tuple[str, ...]) -> list[str] | None:
    """Map model output (index-aligned) -> fixed-length list theo thứ tự original.

    Strict như sever_v2 (translation_service._parse_json): sai số lượng hoặc thiếu
    index bất kỳ -> trả None để cả sub-batch chuyển tier sau. TUYỆT ĐỐI không nén
    list (lỗi cũ gây lệch hàng: segment bị mất text, segment khác dính dịch nhầm).
    """
    if not items or not isinstance(items[0], dict):
        return None
    if len(items) != len(original):
        return None
    item_map: dict[int, str] = {}
    for item in items:
        try:
            idx = int(item.get("index", -1))
        except (TypeError, ValueError):
            return None
        if idx < 0:
            return None
        val = ""
        for k in key_names:
            v = item.get(k)
            if v is not None:
                val = str(v)
                break
        item_map[idx] = val
    result = []
    for i in range(len(original)):
        if i not in item_map:
            return None
        result.append(item_map[i])
    return result


def _parse_translate_response(content: str, original: list[str]) -> list[str] | None:
    """Parse model output -> list[str] align theo index (hoac vi tri neu model tra list thuan)."""
    try:
        json_text = _extract_json_payload(content)
        data = json.loads(json_text)
    except Exception as exc:
        logger.warning("translate parse: json load fail: %s", exc)
        return None

    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            return _parse_indexed_items(data, original, ("translated",))
        if len(data) == len(original):
            return [str(t) for t in data]
        return None

    if isinstance(data, dict):
        items = data.get("segments") or data.get("translations") or data.get("data")
        if isinstance(items, list):
            if items and isinstance(items[0], dict):
                return _parse_indexed_items(items, original, ("translated", "text"))
            if len(items) == len(original):
                return [str(t) for t in items]
    return None

_RATE_LIMIT_STATUSES = frozenset({429, 402, 403})
_TIER_COOLDOWNS: dict[str, float] = {}

def _is_tier_cooling(tier_name: str) -> bool:
    return time.time() < _TIER_COOLDOWNS.get(tier_name, 0)

def _start_tier_cooldown(tier_name: str, duration: int = 60) -> None:
    _TIER_COOLDOWNS[tier_name] = time.time() + duration
    logger.warning("translate: tier [%s] cooldown %ds", tier_name, duration)

async def _translate_openai(
    texts: list[str], url: str, api_key: str, model: str,
    source_lang: str, target_lang: str,
    history: list[dict] | None = None, context: str | None = None,
) -> list[str] | None:
    """OpenAI-compatible API (vd https://gemini.maika.sbs/v1) — retry 2 + cooldown.

    ponytail: Batch splitting — send max 30 segments per request to avoid
    output token truncation on lightweight models (gemini-flash-lite).
    """
    import httpx
    if not url:
        url = "https://gemini.maika.sbs/v1"
    url = url.rstrip("/")
    endpoint = f"{url}/chat/completions" if not url.endswith("/chat/completions") else url

    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # ponytail: split into batches of 30 to avoid truncation
    BATCH_SIZE = 30
    all_results: list[str] = []

    for batch_start in range(0, len(texts), BATCH_SIZE):
        batch_texts = texts[batch_start:batch_start + BATCH_SIZE]

        system_prompt = _base_segment_prompt(source_lang, target_lang)
        user_content = ""
        if context:
            user_content += f"\nCONTEXT (video title):\n{context}\n\n"
        if history:
            recent = history[-20:]
            user_content += "RECENT TRANSLATED (for context, do NOT re-translate):\n"
            user_content += json.dumps(recent, ensure_ascii=False) + "\n\n"
        user_content += "SEGMENTS TO TRANSLATE:\n"
        user_content += json.dumps([{"index": i, "text": t} for i, t in enumerate(batch_texts)], ensure_ascii=False)

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
            "max_tokens": 10000,
            "stream": False,
        }

        last_err = None
        for attempt in range(2):
            if _is_tier_cooling(f"openai:{model}"):
                return None
            try:
                async with httpx.AsyncClient(timeout=25, verify=False) as client:
                    resp = await client.post(endpoint, json=payload, headers=headers)
                    if resp.status_code in _RATE_LIMIT_STATUSES:
                        _start_tier_cooldown(f"openai:{model}", duration=120)
                        return None
                    if resp.status_code >= 400:
                        last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                        logger.warning("translate openai/gemini attempt %d/2 HTTP %d (%s)", attempt + 1, resp.status_code, model)
                        continue
                    content = resp.text
                    content = _unpack_sse_response(content)
                    translated = _parse_translate_response(content, batch_texts)
                    if translated is not None:
                        all_results.extend(translated)
                        break
                    last_err = "unparseable response"
            except Exception as exc:
                last_err = str(exc)
                logger.warning("translate openai/gemini attempt %d/2 fail (%s): %s", attempt + 1, model, exc)
            if attempt == 0:
                await asyncio.sleep(2 ** attempt)
        else:
            # Batch fail (parse/HTTP) -> giữ chỗ bằng "" cho đúng vị trí để không lệch
            # hàng, các segment này chuyển tier sau (giống sever_v2 sub-batch failure).
            # KHÔNG return None vì sẽ vứt luôn kết quả các batch đã dịch thành công.
            logger.warning("translate openai/gemini batch [%d:%d] fail (%s): %s",
                           batch_start, batch_start + len(batch_texts), model, last_err)
            all_results.extend([""] * len(batch_texts))

    return all_results if any(r for r in all_results) else None

async def _translate_google(texts: list[str], source_lang: str, target_lang: str) -> list[str] | None:
    """Free Google Translate API (async, concurrent)."""
    import httpx

    async def _one(client, text: str) -> str:
        if not text.strip():
            return ""
        try:
            resp = await client.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": source_lang, "tl": target_lang,
                        "dt": "t", "q": text},
            )
            resp.raise_for_status()
            translated = "".join(part[0] for part in resp.json()[0])
            return translated.strip() or text
        except Exception:
            return text

    try:
        sem = asyncio.Semaphore(5)

        async def _limited(client, text: str) -> str:
            async with sem:
                return await _one(client, text)

        async with httpx.AsyncClient(timeout=15) as client:
            return await asyncio.gather(*[_limited(client, t) for t in texts])
    except Exception as exc:
        logger.warning("translate google fail: %s", exc)
        return None

async def _translate_claude(texts: list[str], api_key: str,
                             source_lang: str, target_lang: str) -> list[str] | None:
    """Claude API translate."""
    if not api_key:
        logger.warning("translate claude: no API key")
        return None
    import httpx
    try:
        batch_size = 50
        result = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            prompt = _base_segment_prompt(source_lang, target_lang) + json.dumps(
                [{"index": j, "text": t} for j, t in enumerate(batch)], ensure_ascii=False)
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            resp.raise_for_status()
            content = resp.json()["content"][0]["text"]
            translated = _parse_translate_response(content, batch)
            if translated is None:
                return None
            result.extend(translated)
        return result
    except Exception as exc:
        logger.warning("translate claude fail: %s", exc)
        return None

async def _translate_sever(texts: list[str], url: str, api_key: str,
                            source_lang: str, target_lang: str) -> list[str] | None:
    """Call sever translate endpoint."""
    import httpx
    if not url:
        return None
    url = url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    payload = {"texts": texts, "source_language": source_lang, "target_language": target_lang}
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(f"{url}/api/v2/translate", json=payload, headers=headers)
            if resp.status_code == 404:
                resp = await client.post(f"{url}/translate", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return data if isinstance(data, list) else (data.get("translations") or data.get("texts") or texts)
    except Exception as exc:
        logger.warning("translate sever fail: %s", exc)
        return None

def _validate_translation(texts: list[str], translated: list[str], target_lang: str) -> bool:
    """Validate translation quality and consistency.

    ponytail: Relaxed thresholds for small batches (< 10 segments).
    When only a few segments remain, even partial success is better than
    discarding everything and falling back to Google (which often fails too).
    """
    if not translated or not texts:
        return False
    n = len(translated)
    # ponytail: For small batches, accept as long as >30% are non-empty
    empty_threshold = 0.3 if n <= 10 else 0.5
    empty_count = sum(1 for t in translated if not t.strip())
    if empty_count > n * empty_threshold:
        logger.warning("translate validation: too many empty translations (%d/%d)", empty_count, n)
        return False
    # ponytail: For small batches, accept as long as >50% differ from original
    same_threshold = 0.5 if n <= 10 else 0.7
    same_count = sum(1 for s, t in zip(texts, translated[:len(texts)]) if s.strip() == t.strip())
    if same_count > len(texts) * same_threshold:
        logger.warning("translate validation: too many untranslated texts (%d/%d)", same_count, len(texts))
        return False
    return True

def _dedupe_tiers(tiers: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    uniq = []
    for t in tiers:
        key = (t.get("provider"), t.get("url", ""), t.get("model", ""))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(t)
    return uniq

async def _call_tier_provider(
    texts: list[str], tier: dict, source_lang: str, target_lang: str,
    history: list[dict] | None = None, context: str | None = None,
) -> list[str] | None:
    tp = settings.get("translate_provider", {})
    provider = tier.get("provider") or tp.get("provider") or "openai"
    url = tier.get("url") or tp.get("url", "")
    api_key = tier.get("api_key") or tp.get("api_key", "")
    model = tier.get("model") or "gemini-3.5-flash-lite"

    if provider == "gemini_stealth":
        from providers.gemini_stealth import gemini_translate
        res = await gemini_translate(
            texts, model=model, source_lang=source_lang, target_lang=target_lang,
            system_prompt=_base_segment_prompt(source_lang, target_lang),
            history=history, context=context,
        )
        if res is not None and len(res) != len(texts):
            res = _align_translated_length(res, len(texts))
        return res if res and _validate_translation(texts, res, target_lang) else None

    if provider == "google":
        res = await _translate_google(texts, source_lang, target_lang)
        if res is not None and len(res) != len(texts):
            res = _align_translated_length(res, len(texts))
        return res if res and _validate_translation(texts, res, target_lang) else None

    if provider == "claude":
        res = await _translate_claude(texts, api_key, source_lang, target_lang)
        if res is not None and len(res) != len(texts):
            res = _align_translated_length(res, len(texts))
        return res if res and _validate_translation(texts, res, target_lang) else None

    if provider == "sever":
        res = await _translate_sever(texts, url, api_key, source_lang, target_lang)
        if res is not None and len(res) != len(texts):
            res = _align_translated_length(res, len(texts))
        return res if res and _validate_translation(texts, res, target_lang) else None

    res = await _translate_openai(texts, url, api_key, model, source_lang, target_lang,
                                  history=history, context=context)
    if res is not None and len(res) != len(texts):
        res = _align_translated_length(res, len(texts))
    if res and _validate_translation(texts, res, target_lang):
        return res
    return None

async def translate_tier(texts: list[str], tier: dict[str, Any],
                          source_lang: str, target_lang: str) -> list[str] | None:
    """Translate qua 1 tier (dung cho title). Return list[str] hoac None neu fail."""
    if not texts:
        return []
    res = await _call_tier_provider(texts, tier, source_lang, target_lang)
    if res is not None and len(res) != len(texts):
        res = _align_translated_length(res, len(texts))
    return res

async def run_translate(
    segments: list[dict], source_lang: str, target_lang: str,
    history: list[dict] | None = None, context: str | None = None,
) -> list[dict]:
    """Translate segments voi tier fallback + partial success.

    - Moi tier dich phan pending con lai; segment thanh cong duoc giu lai
      (index-based), chi phan fail chuyen tier sau.
    - Retry 2 lan/tier; rate-limit (429/402/403) -> cooldown 120s.
    - Fallback cuoi: Google Translate tung cau.
    - Neu sau toan bo funnel + retry van con segment khong dich duoc -> fallback
      giu nguyen text goc (giong sever_v2 handle_controlled_error).
    """
    if target_lang not in SUPPORTED_LANGUAGES:
        logger.warning("translate: unsupported target language '%s', falling back to 'vi'", target_lang)
        target_lang = "vi"

    texts = [s["text"] for s in segments]
    tiers = _dedupe_tiers(settings.get_translate_tiers())
    if not tiers:
        logger.warning("translate: no tiers configured, skip translate")
        return [{**seg, "translated_text": seg.get("text", "")} for seg in segments]

    translated_map: dict[int, str] = {}
    pending_idx = list(range(len(texts)))

    validator = TranslationValidator()

    # Funnel day du; retry 1 lan cho cac segment con pending neu can
    for attempt in range(2):
        if not pending_idx:
            break
        logger.info("translate: funnel attempt %d/2 (%d pending)", attempt + 1, len(pending_idx))
        await _translate_funnel(texts, pending_idx, translated_map, tiers,
                                source_lang, target_lang, history, context, validator)

    # Fallback hết van con pending -> chia doi batch, retry tung nua de cach ly
    # segment "doc" (1 segment hỏng format khong duoc keo chet ca lo)
    if pending_idx:
        logger.info("translate: split %d pending into halves and retry", len(pending_idx))
        await _retry_split_recursive(texts, pending_idx, translated_map, tiers,
                                     source_lang, target_lang, history, context, validator)

    result = []
    for i, seg in enumerate(segments):
        val = translated_map.get(i, "").strip()
        # Fallback: van khong dich duoc -> giu text goc (khong bo trong, khong mat text)
        if not val or validator.has_chinese(val) or val.lower() in ("video bilibili", "bilibili_video", "bilibili video", "bilibili"):
            val = seg.get("text", "").strip()
        result.append({**seg, "translated_text": val})
    return result


async def _retry_split_recursive(texts: list[str], pending_idx: list[int],
                                 translated_map: dict[int, str], tiers: list[dict],
                                 source_lang: str, target_lang: str,
                                 history: list[dict] | None, context: str | None,
                                 validator: TranslationValidator, depth: int = 0) -> None:
    """Chia doi batch con pending, retry tung nua qua funnel.

    Khi fallback da chay het ma van con segment chua dich duoc, tach batch lam doi
    va retry moi nua: segment "doc" (text qua dai, ky tu la) lam model tra sai format
    cho ca lo thi sau khi tach nho, phan tot van dich duoc, phan hong con lai moi
    di fallback cuoi cung. Depth cap 6 de tranh tham chung khi ca tiers deu chet.
    """
    if not pending_idx or len(pending_idx) <= 1 or depth >= 6:
        return
    mid = len(pending_idx) // 2
    for part in (pending_idx[:mid], pending_idx[mid:]):
        part_pending = list(part)
        await _translate_funnel(texts, part_pending, translated_map, tiers,
                                source_lang, target_lang, history, context, validator)
        await _retry_split_recursive(texts, part_pending, translated_map, tiers,
                                     source_lang, target_lang, history, context,
                                     validator, depth + 1)


async def _translate_funnel(texts: list[str], pending_idx: list[int],
                            translated_map: dict[int, str], tiers: list[dict],
                            source_lang: str, target_lang: str,
                            history: list[dict] | None, context: str | None,
                            validator: TranslationValidator) -> None:
    """Mot luot funnel: chay tung tier cho cac segment pending, cuoi cung google."""
    for idx, tier in enumerate(tiers):
        if not pending_idx:
            break
        tier_name = str(tier.get("provider") or "?")
        if _is_tier_cooling(f"tier:{tier_name}:{tier.get('model', '')}"):
            continue
        logger.info("translate tier %d/%d: %s -> %s (pending %d)",
                    idx + 1, len(tiers), tier.get("label", tier_name),
                    SUPPORTED_LANGUAGES.get(target_lang, target_lang), len(pending_idx))

        pending_texts = [texts[i] for i in pending_idx]
        try:
            translated = await _call_tier_provider(
                pending_texts, tier, source_lang, target_lang,
                history=history, context=context,
            )
        except Exception as exc:
            logger.warning("translate tier %d fail: %s", idx + 1, exc)
            _start_tier_cooldown(f"tier:{tier_name}:{tier.get('model', '')}", duration=60)
            continue

        if translated is None:
            continue

        still_pending: list[int] = []
        for pos, orig_idx in enumerate(pending_idx):
            if pos >= len(translated):
                still_pending.append(orig_idx)
                continue
            val = str(translated[pos]).strip()
            orig = texts[orig_idx]
            if val and val != orig and validator.is_valid(orig, val):
                translated_map[orig_idx] = val
            else:
                still_pending.append(orig_idx)
        pending_idx[:] = still_pending

    if pending_idx:
        logger.info("translate: google fallback for %d segments", len(pending_idx))
        pending_texts = [texts[i] for i in pending_idx]
        g_res = await _translate_google(pending_texts, source_lang, target_lang)
        if g_res:
            still_pending: list[int] = []
            for pos, orig_idx in enumerate(pending_idx):
                if pos < len(g_res):
                    g_val = validator.clean_text(str(g_res[pos]).strip())
                    orig = texts[orig_idx]
                    if g_val and g_val != orig and validator.is_valid(orig, g_val):
                        translated_map[orig_idx] = g_val
                        continue
                still_pending.append(orig_idx)
            pending_idx[:] = still_pending

def _align_translated_length(translated: list[str], target_len: int) -> list[str]:
    if len(translated) == target_len:
        return translated
    if len(translated) > target_len:
        return translated[:target_len]
    padded = list(translated)
    while len(padded) < target_len:
        padded.append("")
    return padded

def get_supported_languages() -> dict[str, str]:
    """Return dict of supported language codes and names."""
    return dict(SUPPORTED_LANGUAGES)

def is_supported_language(lang_code: str) -> bool:
    """Check if a language code is supported."""
    return lang_code in SUPPORTED_LANGUAGES

