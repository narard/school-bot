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

def translate_ru_to_en(text: str) -> str:
    """Быстрый перевод на английский для нейросети FLUX (нейросети понимают только English)"""
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=en&dt=t&q={urllib.parse.quote(text)}"
        r = requests.get(url, timeout=4)
        if r.status_code == 200:
            return "".join([part[0] for part in r.json()[0]])
    except Exception as e:
        logger.warning(f"Translation error: {e}")
    return text

def generate_image_pollinations(prompt: str) -> Tuple[bool, Optional[bytes], str]:
    """
    Генерирует высококачественное изображение через FLUX.1.
    Переводит идею на английский и добавляет профессиональные параметры качества.
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
        clean = "милый пушистый котёнок в цветах"

    # 1. Переводим на английский (FLUX генерирует реалистично только по английскому промпту)
    en_desc = translate_ru_to_en(clean)

    # 2. Формируем профессиональный промпт для фотореализма и чётких линий (без кривизны)
    flux_prompt = (
        f"masterpiece, award-winning professional photography, {en_desc}, "
        f"highly detailed, intricate details, realistic textures, volumetric natural lighting, "
        f"sharp focus, cinematic composition, 8k resolution, photorealistic"
    )

    seed = random.randint(1, 9999999)
    encoded = urllib.parse.quote(flux_prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?model=flux&width=1024&height=1024&nologo=true&seed={seed}"

    try:
        resp = requests.get(url, timeout=35)
        if resp.status_code == 200 and len(resp.content) > 2000:
            return True, resp.content, clean
        elif resp.status_code == 402:
            return False, None, "⏳ Художник как раз смешивает краски (лимит 1 картинка в 45 сек). Отправьте запрос ещё раз через 15–20 секунд!"
        else:
            return False, None, f"Ошибка генерации ({resp.status_code})"
    except Exception as e:
        logger.error(f"Image generation error: {e}")
        return False, None, f"Ошибка соединения с генератором: {e}"
