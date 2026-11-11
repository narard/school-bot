import re
import random
import urllib.parse
import logging
import requests
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

def is_draw_request(text: str) -> bool:
    """Проверяет, просит ли пользователь нарисовать картинку"""
    clean = text.lower().strip()
    if clean.startswith(("/draw", "/art", "/pic", "/img", "нарисуй", "сгенерируй", "нарисуйте")):
        return True
    if any(phrase in clean for phrase in ["сделай картинку", "сделай фото", "нарисуй мне", "нарисуй картинку"]):
        return True
    return False

def generate_image_pollinations(prompt: str) -> Tuple[bool, Optional[bytes], str]:
    """
    Генерирует изображение по тексту через Pollinations FLUX.
    Возвращает (success, image_bytes, clean_prompt_or_error)
    """
    clean = prompt.strip()
    prefixes = [
        r"^/draw\s*", r"^/art\s*", r"^/pic\s*", r"^/img\s*",
        r"^нарисуйте\s+(мне\s+)?", r"^нарисуй\s+(мне\s+)?(картинку\s+|фото\s+|арт\s+)?",
        r"^сгенерируй\s+(мне\s+)?(картинку\s+|фото\s+|изображение\s+)?",
        r"^сделай\s+(мне\s+)?(картинку\s+|фото\s+|арт\s+)?"
    ]
    for p in prefixes:
        clean = re.sub(p, "", clean, flags=re.IGNORECASE).strip()

    if not clean:
        clean = "милый пушистый котёнок в цветах, 3D мультфильм"

    seed = random.randint(1, 9999999)
    encoded = urllib.parse.quote(clean)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&seed={seed}&model=flux"

    try:
        resp = requests.get(url, timeout=35)
        if resp.status_code == 200 and len(resp.content) > 1000:
            return True, resp.content, clean
        elif resp.status_code == 402:
            # Фолбэк на стандартную модель без seed
            fallback_url = f"https://image.pollinations.ai/prompt/{encoded}?width=800&height=800&nologo=true"
            fb = requests.get(fallback_url, timeout=30)
            if fb.status_code == 200 and len(fb.content) > 1000:
                return True, fb.content, clean
            return False, None, "Художник немного занят (лимит 1 картинка в минуту). Попробуйте ещё раз через 30 секунд!"
        else:
            return False, None, f"Ошибка генератора ({resp.status_code})"
    except Exception as e:
        logger.error(f"Image generation error: {e}")
        return False, None, f"Ошибка соединения: {e}"
