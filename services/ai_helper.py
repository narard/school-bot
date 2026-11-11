import os
import re
import base64
import asyncio
import requests
from typing import Optional

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

SYSTEM_PROMPT = (
    "Ты дружелюбный, умный и всесторонний AI-помощник для всей семьи (живёшь в домашнем Telegram-боте семьи в Минске).\n\n"
    "Твои правила общения:\n"
    "1. ЕСЛИ ВОПРОС ПО ШКОЛЕ (для дочки 4-го класса, 9-10 лет): будь добрым, терпеливым школьным репетитором. Объясняй пошагово, просто, наглядными примерами (конфеты, яблоки), без сложных научных терминов. Если это задача или пример — покажи промежуточные шаги.\n"
    "2. ЕСЛИ ВОПРОС ВЗРОСЛЫЙ ИЛИ ОБЩИЙ (кулинария, быт, авто, история, наука, переводы, IT, ремонт, советы, стихи, сочинения): отвечай как первоклассный эрудированный эксперт — развёрнуто, точно, практично и понятно.\n"
    "3. Всегда отвечай с добротой и уважением, красиво структурируй текст списками и абзацами для удобного чтения в Telegram."
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

    # 2. Обращаемся к облачному AI-прокси
    url = "https://school-bot-zbh4.onrender.com/api/gemini"
    payload = {"question": question}
    if image_bytes:
        payload["image_b64"] = base64.b64encode(image_bytes).decode("utf-8")

    def call_proxy():
        return requests.post(url, json=payload, timeout=35)

    try:
        resp = await asyncio.to_thread(call_proxy)
        if resp.status_code == 200:
            rj = resp.json()
            if rj.get("ok"):
                return rj.get("answer")
            return f"💡 Ответ AI: {rj.get('error', 'не удалось получить ответ')}"
        return f"💡 AI-сервер временно недоступен ({resp.status_code}). Попробуйте позже."
    except Exception as e:
        return f"💡 Ошибка связи с AI: {e}"
