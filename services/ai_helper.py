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
            err_data = resp.json()
            msg = err_data.get("error", {}).get("message", resp.text)
            return f"⚠️ <b>Ответ AI:</b> {msg}"
    except Exception as e:
        return f"❌ Ошибка связи с AI: {e}"
