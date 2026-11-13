import os
import re
import time
import base64
import asyncio
import requests
from typing import Optional, List, Dict, Any

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Память контекста диалога: chat_id -> list of turns
CONVERSATION_HISTORY: Dict[int, List[Dict[str, Any]]] = {}

def get_chat_history(chat_id: int, max_turns: int = 6) -> List[Dict[str, str]]:
    """
    Возвращает последние сообщения для контекста.
    Если прошло больше 30 минут — начинает диалог с чистого листа.
    """
    if chat_id not in CONVERSATION_HISTORY:
        return []
    history = CONVERSATION_HISTORY[chat_id]
    now = time.time()
    if history and (now - history[-1].get("time", 0) > 1800):
        CONVERSATION_HISTORY[chat_id] = []
        return []
    return [{"role": h["role"], "text": h["text"]} for h in history[-max_turns:]]

def record_chat_turn(chat_id: int, user_text: str, model_text: str):
    """
    Запоминает пару вопрос-ответ в истории диалога
    """
    now = time.time()
    if chat_id not in CONVERSATION_HISTORY:
        CONVERSATION_HISTORY[chat_id] = []
    CONVERSATION_HISTORY[chat_id].append({"role": "user", "text": user_text, "time": now})
    CONVERSATION_HISTORY[chat_id].append({"role": "model", "text": model_text, "time": now})
    # Храним не более 10 последних сообщений (5 раундов)
    if len(CONVERSATION_HISTORY[chat_id]) > 10:
        CONVERSATION_HISTORY[chat_id] = CONVERSATION_HISTORY[chat_id][-10:]

def clear_chat_history(chat_id: int):
    """Сброс контекста диалога (например, для новой темы или задачи)"""
    CONVERSATION_HISTORY[chat_id] = []

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

async def ask_homework_tutor(question: str, image_bytes: Optional[bytes] = None, chat_id: Optional[int] = None) -> str:
    """
    Умный школьный репетитор с памятью контекста, лаконичностью и визуализацией
    """
    # 1. Быстрый калькулятор (только если нет фото и нет активного контекста)
    history = get_chat_history(chat_id) if chat_id else []
    if not image_bytes and not history:
        math_res = try_eval_math(question)
        if math_res:
            if chat_id:
                record_chat_turn(chat_id, question, math_res)
            return math_res

    # 2. Обращаемся к облачному AI-прокси
    url = "https://school-bot-zbh4.onrender.com/api/gemini"
    payload: Dict[str, Any] = {
        "question": question,
        "history": history
    }
    if image_bytes:
        payload["image_b64"] = base64.b64encode(image_bytes).decode("utf-8")

    def call_proxy():
        return requests.post(url, json=payload, timeout=40)

    try:
        resp = await asyncio.to_thread(call_proxy)
        if resp.status_code == 200:
            rj = resp.json()
            if rj.get("ok"):
                ans = rj.get("answer", "")
                if chat_id and ans:
                    record_chat_turn(chat_id, question, ans)
                return ans
            return f"💡 Ответ AI: {rj.get('error', 'не удалось получить ответ')}"
        return f"💡 AI-сервер временно недоступен ({resp.status_code}). Попробуйте позже."
    except Exception as e:
        return f"💡 Ошибка связи с AI: {e}"
