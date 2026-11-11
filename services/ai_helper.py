import os
import re
import base64
import requests
from typing import Optional

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

SYSTEM_PROMPT = (
    "Ты добрый, терпеливый и понятный школьный репетитор для ученицы 4-го класса белорусской школы (9-10 лет). "
    "Твоя цель: помочь понять домашнее задание (математика, русский язык, белорусский язык, Человек и Мир и др.). "
    "Объясняй пошагово, с добротой, доступным для 4-классника языком. Не используй заумных академических терминов. "
    "Если это задача или пример — покажи логику решения и промежуточные шаги."
)

def try_eval_math(text: str) -> Optional[str]:
    """
    Быстрый локальный калькулятор для простых арифметических примеров
    """
    clean = text.lower().replace("помоги", "").replace("решить", "").replace("пример", "").replace("сколько будет", "").strip()
    clean = clean.replace("=", "").replace(":", "/").replace("х", "*").replace("x", "*")
    clean = re.sub(r"\s+", "", clean)

    if re.fullmatch(r"[\d\+\-\*/\(\)\.]+", clean):
        try:
            val = eval(clean, {"__builtins__": {}}, {})
            if isinstance(val, float) and val.is_integer():
                val = int(val)
            original_display = clean.replace("/", " : ").replace("*", " · ")
            return f"🔢 <b>Решение примера:</b>\n\n<code>{original_display} = {val}</code>"
        except Exception:
            pass
    return None

async def ask_homework_tutor(question: str, image_bytes: Optional[bytes] = None) -> str:
    # 1. Сначала проверяем, можно ли решить пример локально
    if not image_bytes:
        math_res = try_eval_math(question)
        if math_res:
            return math_res

    # 2. Если есть GEMINI_API_KEY — обращаемся к модели Gemini 3.8 Flash
    key = os.environ.get("GEMINI_API_KEY") or GEMINI_API_KEY
    if not key:
        return (
            "💡 <b>AI-репетитор пока не подключен</b>\n"
            "Ключ API не обнаружен в переменных окружения."
        )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={key}"

    parts = [{"text": f"{SYSTEM_PROMPT}\n\nВопрос: {question}"}]
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": b64
            }
        })

    payload = {"contents": [{"parts": parts}]}

    try:
        resp = requests.post(url, json=payload, timeout=25)
        if resp.status_code == 200:
            data = resp.json()
            candidates = data.get("candidates", [])
            if candidates:
                content = candidates[0].get("content", {})
                res_parts = content.get("parts", [])
                if res_parts:
                    return res_parts[0].get("text", "")
            return "Не удалось сформулировать ответ. Попробуйте перефразировать вопрос."
        else:
            err_data = resp.json()
            msg = err_data.get("error", {}).get("message", resp.text)
            return f"⚠️ <b>Ответ AI:</b> {msg}"
    except Exception as e:
        return f"❌ Ошибка связи с AI: {e}"
