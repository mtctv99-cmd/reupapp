"""Gemini Stealth — gọi trực tiếp Gemini web StreamGenerate, không cần key.

Dịch từ gemini-stealth-api (G:\\My Drive\\Data_sv4\\gemini-stealth-api).
Gọi thẳng gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate
Anonymous access — không cần API key, không cần cookie.
"""
import json
import logging
import re
import ssl
import time
import urllib.parse
import urllib.request
import uuid

logger = logging.getLogger(__name__)

# Gemini BL version — cần update định kỳ nếu Google thay đổi
_GEMINI_BL = "boq_assistant-bard-web-server_20260615.05_p0"

# Model mapping từ MODE_CATEGORY enum trong Gemini JS
# 1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FLASH_THINKING_LITE, 6=FLASH_LITE
_MODEL_MAP = {
    "gemini-flash-lite": (6, 4),
    "gemini-3.5-flash": (1, 4),
    "gemini-3.5-flash-thinking": (2, 0),
}


async def gemini_translate(
    texts: list[str],
    model: str = "gemini-flash-lite",
    source_lang: str = "zh",
    target_lang: str = "vi",
    system_prompt: str | None = None,
    history: list[dict] | None = None,
    context: str | None = None,
) -> list[str] | None:
    """Dịch texts qua Gemini stealth với đầy đủ prompt lồng tiếng và context."""
    mode, think = _MODEL_MAP.get(model, (6, 4))

    lang_names = {"zh": "Chinese", "vi": "Vietnamese", "en": "English",
                  "ja": "Japanese", "ko": "Korean", "th": "Thai"}
    src_name = lang_names.get(source_lang, source_lang)
    tgt_name = lang_names.get(target_lang, target_lang)

    segments_json = json.dumps([{"index": i, "text": t} for i, t in enumerate(texts)], ensure_ascii=False)

    if system_prompt:
        base_inst = system_prompt
    else:
        base_inst = (
            f"Translate these segments from {src_name} to {tgt_name} for natural video voiceover.\n"
            f"Output ONLY a valid JSON array: [{{\"index\": 0, \"translated\": \"...\"}}, ...]\n"
            f"No explanations, no notes, no markdown."
        )

    prompt_parts = [base_inst]
    if context:
        prompt_parts.append(f"\nCONTEXT (video title):\n{context}")
    if history:
        recent = history[-15:]
        prompt_parts.append(f"\nRECENT TRANSLATED (for context):\n{json.dumps(recent, ensure_ascii=False)}")
    prompt_parts.append(f"\nSEGMENTS TO TRANSLATE:\n{segments_json}")

    full_prompt = "\n\n".join(prompt_parts)

    try:
        raw = _gemini_request_sync(full_prompt, mode, think)
        if not raw:
            return None
        text = extract_response_text(raw)
        if not text:
            return None
        return _parse_json_array(text, len(texts))
    except Exception as exc:
        logger.warning("gemini_stealth fail (%s): %s", model, exc)
        return None


def _gemini_request_sync(prompt: str, mode: int, think: int, timeout: int = 60) -> str:
    """Gửi request đến Gemini StreamGenerate — đồng bộ (dùng trong thread)."""
    inner = [None] * 80
    inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = mode

    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    body = urllib.parse.urlencode(params).encode()

    reqid = int(time.time()) % 1000000
    url = (
        "https://gemini.google.com/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={_GEMINI_BL}&hl=en&_reqid={reqid}&rt=c"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    last_err = None

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                if raw:
                    return raw
            last_err = RuntimeError("empty response")
        except Exception as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(2 ** attempt)

    raise last_err or RuntimeError("gemini stealth failed")


def extract_response_text(raw: str) -> str:
    """Parse StreamGenerate response — trích xuất text từ Gemini nested JSON.

    Ported directly from gemini-stealth-api/main.py:extract_response_text
    """
    bard_err = re.search(r'BardErrorInfo\s*\[(\d+)\]', raw)
    if bard_err:
        raise RuntimeError(f"Gemini upstream error: BardErrorInfo [{bard_err.group(1)}]")

    texts = []
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line or len(line) < 200:
            continue
        try:
            arr = json.loads(line)
            inner_str = arr[0][2]
            if not inner_str or len(inner_str) < 50:
                continue
            inner = json.loads(inner_str)
            if isinstance(inner, list) and len(inner) > 4 and inner[4]:
                for part in inner[4]:
                    if isinstance(part, list) and len(part) > 1 and part[1]:
                        if isinstance(part[1], list):
                            for t in part[1]:
                                if isinstance(t, str) and len(t) > 0:
                                    texts.append(t)
        except (json.JSONDecodeError, IndexError, TypeError):
            pass

    text = ""
    for t in reversed(texts):
        if t.strip():
            text = t
            break
    return _clean_gemini_text(text)


def _clean_gemini_text(text: str) -> str:
    """Remove internal code execution artifacts."""
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL,
    )
    return text.strip()


def _parse_json_array(text: str, expected_count: int) -> list[str] | None:
    """Parse JSON array từ Gemini response, align theo index."""
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text)

    try:
        data = json.loads(text)
    except Exception:
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except Exception:
                return None
        else:
            return None

    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            result = [""] * expected_count
            for item in data:
                idx = item.get("index", -1)
                val = item.get("translated", "") or ""
                if 0 <= idx < expected_count:
                    result[idx] = str(val)
            return result if any(result) else None
        return [str(t) for t in data[:expected_count]]

    return None
