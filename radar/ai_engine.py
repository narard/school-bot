import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import re
import json
import html
import logging
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional

import requests

try:
    from radar.config import AI_PROXY_URL, AI_HEALTH_URL
except ImportError:
    from config import AI_PROXY_URL, AI_HEALTH_URL

logger = logging.getLogger(__name__)


def wake_ai_proxy() -> bool:
    """Пробуждает Render-прокси перед серией запросов (если нет локального ключа)."""
    if os.environ.get("GEMINI_API_KEY"):
        return True
    try:
        r = requests.get(AI_HEALTH_URL, timeout=45)
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"AI proxy wake warning: {e}")
        return False


def _call_gemini_direct(prompt: str, timeout: int = 25, image_b64: Optional[str] = None) -> Optional[str]:
    """Прямой вызов Google Gemini API при работе на облачном сервере Render."""
    keys = [
        k.strip()
        for k in [os.environ.get("GEMINI_API_KEY", ""), os.environ.get("GEMINI_API_KEY_BACKUP", "")]
        if k.strip()
    ]
    if not keys:
        return None

    parts: List[Dict[str, Any]] = [{"text": prompt}]
    if image_b64:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": image_b64}})

    models = [
        "gemini-3.5-flash",
        "gemini-flash-latest",
        "gemini-3.1-flash-lite",
        "gemini-flash-lite-latest",
    ]

    for key in keys:
        for m in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}"
            try:
                resp = requests.post(url, json={"contents": [{"parts": parts}]}, timeout=timeout)
                if resp.status_code == 200:
                    candidates = resp.json().get("candidates", [])
                    if candidates:
                        txt = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        if txt:
                            return txt.strip()
            except Exception as e:
                logger.warning(f"Direct Gemini ({m}) error: {e}")
    return None


def call_gemini(prompt: str, timeout: int = 40, image_b64: Optional[str] = None) -> Optional[str]:
    """Вызывает Gemini напрямую (в облаке Render) или через облачный прокси."""
    if os.environ.get("GEMINI_API_KEY"):
        direct_ans = _call_gemini_direct(prompt, timeout=min(timeout, 28), image_b64=image_b64)
        if direct_ans:
            return direct_ans

    payload: Dict[str, Any] = {"question": prompt}
    if image_b64:
        payload["image_b64"] = image_b64

    for attempt in range(2):
        try:
            resp = requests.post(
                AI_PROXY_URL,
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok") and data.get("answer"):
                    return data["answer"].strip()
        except Exception as e:
            logger.warning(f"Gemini call attempt {attempt + 1} error: {e}")
            if attempt == 0:
                wake_ai_proxy()
    return None


def translate_to_ru_fast(text: str) -> str:
    """Резервный быстрый перевод на русский через Google GTX, если текст на английском."""
    if not text:
        return ""
    # Если уже много кириллицы, не переводим
    cyr_count = len(re.findall(r"[а-яёА-ЯЁ]", text))
    if cyr_count > len(text) * 0.25:
        return text
    try:
        q = urllib.parse.quote(text[:900])
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=ru&dt=t&q={q}"
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            return "".join(part[0] for part in r.json()[0] if part[0])
    except Exception:
        pass
    return text


def _FallbackItem(raw_item: Dict[str, Any]) -> Dict[str, Any]:
    """Резервное форматирование на случай недоступности LLM."""
    title_ru = translate_to_ru_fast(raw_item.get("title", ""))
    raw_body = raw_item.get("raw_text", "")
    if raw_body.startswith(raw_item.get("title", "")):
        raw_body = raw_body[len(raw_item.get("title", "")) :].lstrip(". ")
    summary_ru = translate_to_ru_fast(raw_body[:380])
    if not summary_ru or len(summary_ru) < 25:
        summary_ru = title_ru

    cat = raw_item.get("category", "news")
    default_ideas = {
        "news": "Использовать новые возможности модели в своих ботах и автоматизации раньше конкурентов.",
        "apps": "Адаптировать концепцию под русскоязычный рынок или сделать аналогичного Telegram-бота.",
        "money": "Применить описанную механику монетизации/автоматизации в своём микро-SaaS или пет-проекте.",
        "github": "Подключить этот опенсорс-инструмент или скилл к своему ИИ-агенту для ускорения разработки.",
        "prompts": "Сохранить промпт/пайплайн в избранное и внедрить в рабочие задачи.",
    }
    return {
        "title_ru": title_ru[:160],
        "summary_ru": summary_ru[:420],
        "idea_ru": default_ideas.get(cat, default_ideas["news"]),
        "url": raw_item.get("url", ""),
        "source_name": raw_item.get("source_name", ""),
        "category": cat,
        "raw_text": raw_item.get("raw_text", ""),
    }


def _summarize_batch_5(batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Обрабатывает пачку до 5 материалов за один быстрый вызов Gemini (~3–4 сек).
    Возвращает список обогащённых материалов на русском языке.
    """
    if not batch:
        return []

    items_Wait_prompt = []
    for idx, it in enumerate(batch):
        items_Wait_prompt.append(
            f"[{idx}] ИСТОЧНИК: {it.get('source_name')} | РУБРИКА: {it.get('category')}\n"
            f"ЗАГОЛОВОК: {it.get('title')}\n"
            f"ТЕКСТ: {it.get('raw_text', '')[:500]}"
        )

    prompt = (
        "Ты — главный редактор закрытого дайджеста «ИИ РАДАР» для практика и охотника за новыми идеями.\n"
        "Переведи и упакуй каждый из материалов ниже на русский язык.\n"
        "Требования:\n"
        "- Без воды, без вводных слов, только конкретика (цифры, технологии, что именно делает инструмент/кейс).\n"
        "- В поле `idea_ru` предлагай НЕ толко SaaS, а широкие, нестандартные идеи: как применить это для YouTube/TikTok/Shorts (создание контента), в обычном быту и жизни (до чего ещё никто не додумался), в офлайн-услугах, фрилансе или автоматизации.\n"
        "- Верни СТРОГО валидный JSON-массив объектов (без маркдаун-обёрток или только JSON), где для каждого индекса idx:\n"
        '[{"idx": 0, "title_ru": "Чёткий заголовок на русском", '
        '"summary_ru": "2-3 плотных предложения сути без воды.", '
        '"idea_ru": "1 яркая практическая идея: как применить в YouTube/TikTok, в быту, услугах или для заработка."}]\n\n'
        "МАТЕРИАЛЫ:\n" + "\n---\n".join(items_Wait_prompt)
    )

    ans = call_gemini(prompt, timeout=35)
    if ans:
        try:
            match = re.search(r"\[.*\]", ans, re.DOTALL)
            json_str = match.group(0) if match else ans
            parsed = json.loads(json_str)
            if isinstance(parsed, list):
                by_idx = {int(obj.get("idx", i)): obj for i, obj in enumerate(parsed) if isinstance(obj, dict)}
                results = []
                for idx, raw_it in enumerate(batch):
                    ai_obj = by_idx.get(idx)
                    if ai_obj and ai_obj.get("title_ru") and ai_obj.get("summary_ru"):
                        results.append(
                            {
                                "title_ru": str(ai_obj["title_ru"]).strip(),
                                "summary_ru": str(ai_obj["summary_ru"]).strip(),
                                "idea_ru": str(ai_obj.get("idea_ru") or "Протестировать решение в своих проектах.").strip(),
                                "url": raw_it.get("url", ""),
                                "source_name": raw_it.get("source_name", ""),
                                "category": raw_it.get("category", "news"),
                                "raw_text": raw_it.get("raw_text", ""),
                            }
                        )
                    else:
                        results.append(_FallbackItem(raw_it))
                return results
        except Exception as e:
            logger.warning(f"Batch JSON parse warning: {e}")

    return [_FallbackItem(it) for it in batch]


def prepare_curated_items(candidates: List[Dict[str, Any]], count: int = 10) -> List[Dict[str, Any]]:
    """
    Принимает список лучших кандидатов, разбивает на пачки по 5 штук,
    обогащает через ИИ параллельно и возвращает ровно `count` готовых карточек.
    """
    selected = candidates[:count]
    if not selected:
        return []

    wake_ai_proxy()

    chunks = [selected[i : i + 5] for i in range(0, len(selected), 5)]
    enriched: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_summarize_batch_5, ch) for ch in chunks]
        for fut in futures:
            enriched.extend(fut.result())

    return enriched[:count]


def generate_deep_dive(item: Dict[str, Any], full_source_text: str) -> str:
    """
    Генерирует подробный технический и практический разбор материала по кнопке «📖 Подробнее».
    """
    prompt = (
        "Сделай глубокий, максимально полезный и практичный разбор этого материала на русском языке (без воды и приветствий).\n\n"
        f"Заголовок: {item.get('title_ru')}\n"
        f"Источник: {item.get('source_name')} ({item.get('url')})\n"
        f"Текст первоисточника / README:\n{full_source_text[:3500]}\n\n"
        "Структура ответа (используй эмодзи и чистые абзацы, НЕ используй сложную разметку таблиц):\n"
        "1. 📌 Полная суть и ключевые фишки (в деталях: что внутри, какие цифры, чем отличается от аналогов).\n"
        "2. ⚙️ Как устроено / Стек / Промпт (технические подробности, команды или ключевой промпт).\n"
        "3. 🚀 Пошаговый план: как запустить, протестировать или внедрить у себя (шаг 1, шаг 2, шаг 3)."
    )
    ans = call_gemini(prompt, timeout=35)
    if ans:
        return clean_markdown_to_html(ans)

    fallback = translate_to_ru_fast(full_source_text[:1500])
    return (
        f"📌 <b>Подробности из первоисточника:</b>\n\n"
        f"{html.escape(fallback)}\n\n"
        f"🔗 <a href=\"{html.escape(item.get('url', ''))}\">Открыть оригинал</a>"
    )


def generate_monetization_plan(item: Dict[str, Any]) -> str:
    """
    Генерирует 3 разноплановых сценария применения и заработка по кнопке «💡 Идея для меня»:
    1) YouTube / TikTok / Контент
    2) Быт / Повседневная жизнь / Офлайн (до чего ещё не додумались)
    3) Быстрые деньги (Услуга / Бот / Сервис)
    """
    prompt = (
        "Ты — изобретательный практик по заработку на ИИ в самых разных сферах (от YouTube/TikTok и бытовых ноу-хау до услуг и автоматизации).\n"
        "На основе этого материала придумай 3 конкретных, нестандартных сценария применения (без воды, шаблонов и лести):\n\n"
        f"Материал: {item.get('title_ru')}\n"
        f"Суть: {item.get('summary_ru')}\n"
        f"Детали: {item.get('raw_text', '')[:1500]}\n\n"
        "Распиши ровно по 3 блокам:\n"
        "1. 🎬 YouTube / TikTok / Reels (Контент-завод): какой формат канала или видео-конвейера можно запустить на этой базе (о чём ролики, как автоматизировать создание и на чём монетизировать).\n"
        "2. 🏠 Быт и реальная жизнь (До чего ещё никто не додумался): как нестандартно применить эту технологию в повседневной жизни, ремонте, покупках, семье, авто или обычной офлайн-сфере.\n"
        "3. 💰 Быстрые деньги за 1–2 вечера (Услуга / Бот / Автоматизация): кому продать готовое решение прямо сейчас, за какой чек и первые 3 шага запуска."
    )
    ans = call_gemini(prompt, timeout=35)
    if ans:
        return clean_markdown_to_html(ans)

    return (
        f"💡 <b>Идея внедрения:</b>\n\n"
        f"{html.escape(item.get('idea_ru', ''))}\n\n"
        f"Рекомендуемый шаг: протестировать механику из <a href=\"{html.escape(item.get('url', ''))}\">первоисточника</a> "
        f"для генерации контента (Shorts/Reels) или автоматизации бытовых/рабочих задач."
    )


def generate_unconventional_ideas(recent_titles: List[str]) -> str:
    """
    Генерирует 5 свежих неочевидных идей заработка и бытового применения ИИ
    на основе сегодняшних трендов (YouTube/TikTok, быт, офлайн, услуги).
    """
    trends_block = "\n".join(f"- {t}" for t in recent_titles[:15])
    prompt = (
        "Ты — генератор прорывных прикладных идей на стыке ИИ и реальной жизни.\n"
        "Вот свежие инструменты и новости ИИ за сегодня:\n"
        f"{trends_block}\n\n"
        "Придумай 5 НЕОЧЕВИДНЫХ, свежих идей (НЕ банальный SaaS!), до которых большинство ещё не додумалось.\n"
        "Обязательно распредели их по сферам:\n"
        "1. 🎬 YouTube / TikTok / Shorts (новая незанятая ниша контента с ИИ).\n"
        "2. 📱 Вирусный микро-формат в соцсетях / Telegram с монетизацией.\n"
        "3. 🏠 Быт, дом, семья, покупки или экономия денег/времени в реальной жизни.\n"
        "4. 🛠 Земная офлайн-сфера (авто, ремонт, недвижимость, локальные услуги, торговля).\n"
        "5. ⚡️ Быстрая услуга «под ключ» для людей далёких от IT.\n\n"
        "Для каждой из 5 идей напиши кратко и по делу:\n"
        "- Суть идеи (в чём фишка)\n"
        "- Чем собирается (какие нейросети/инструменты)\n"
        "- Где тут деньги или прямая выгода."
    )
    ans = call_gemini(prompt, timeout=35)
    if ans:
        return clean_markdown_to_html(ans)
    return "⚠️ Не удалось связаться с ИИ-генератором. Попробуй нажать кнопку ещё раз через 15 секунд."


# ---------------------------------------------------------------------------
# МОДУЛЬ 2: КНОПКА «🎬 В СЦЕНАРИЙ YOUTUBE / TIKTOK / REELS»
# ---------------------------------------------------------------------------
def generate_video_script(item: Dict[str, Any]) -> str:
    prompt = (
        "Ты — топовый продюсер вирусных YouTube Shorts, TikTok и Instagram Reels с миллионами просмотров.\n"
        "Преврати этот материал в готовый к съёмке/генерации сценарий короткого ролика на 35–45 секунд (можно без лица — faceless).\n\n"
        f"Тема: {item.get('title_ru')}\n"
        f"Суть: {item.get('summary_ru')}\n"
        f"Идея: {item.get('idea_ru')}\n\n"
        "Выдай строго без воды по 4 блокам:\n"
        "1. 🪝 ХУК (0–3 сек): крупный текст на экране + первая убойная фраза озвучки, чтобы зритель не смахнул.\n"
        "2. 🎙 ТЕКСТ ОЗВУЧКИ (3–40 сек): готовый живой текст слово-в-слово (для диктора или ИИ-озвучки ElevenLabs/Edge-TTS).\n"
        "3. 🖼 ВИЗУАЛ И ПРОМПТЫ: что именно показывать на экране каждые 5 секунд + готовый промпт на английском для генерации фонового видео/картинки.\n"
        "4. 💰 МОНЕТИЗАЦИЯ РОЛИКА: финальный призыв (байт на коммент/переход в ТГ-канал) и как выжать из этого ролика деньги."
    )
    ans = call_gemini(prompt, timeout=35)
    if ans:
        return clean_markdown_to_html(ans)
    return "⚠️ Не удалось сгенерировать сценарий. Нажми кнопку ещё раз через 10 секунд."


# ---------------------------------------------------------------------------
# МОДУЛЬ 3: КНОПКА «⚡ СОБРАТЬ ПРОТОТИП + ОФФЕР»
# ---------------------------------------------------------------------------
def generate_prototype_and_offer(item: Dict[str, Any]) -> str:
    prompt = (
        "Ты — практик быстрого запуска ИИ-услуг и продуктов за 1 вечер с целью заработать первые $1,000.\n"
        "На основе этого материала собери готовый комплект для немедленного старта (без воды):\n\n"
        f"Материал: {item.get('title_ru')}\n"
        f"Суть: {item.get('summary_ru')}\n"
        f"Применение: {item.get('idea_ru')}\n\n"
        "Выдай ровно 3 готовых блока, которые можно сразу скопировать в работу:\n"
        "1. 🤖 ГОТОВЫЙ СИСТЕМНЫЙ ПРОМПТ (System Prompt): напиши полный рабочий промпт, который пользователь может прямо сейчас вставить в ChatGPT / Claude / Telegram-бота, чтобы оказывать эту услугу или решать задачу.\n"
        "2. 📢 ГОТОВОЕ ОБЪЯВЛЕНИЕ / ОФФЕР: готовый продающий текст для Куфара, Авито, Threads, Instagram или чатов предпринимателей (на простом языке выгоды клиента).\n"
        "3. 💵 ЧЕК И СДАЧА РАБОТЫ ЗА 1 ВЕЧЕР: сколько брать денег ($30–$200) и как за 3 шага отдать результат клиенту без сложного программирования."
    )
    ans = call_gemini(prompt, timeout=35)
    if ans:
        return clean_markdown_to_html(ans)
    return "⚠️ Не удалось собрать прототип. Попробуй ещё раз через 10 секунд."


# ---------------------------------------------------------------------------
# МОДУЛЬ 4: «🎯 ЗАДАНИЕ ДНЯ НА 45 МИНУТ» (К ПЕРВОЙ $1,000)
# ---------------------------------------------------------------------------
def generate_daily_sprint(items: List[Dict[str, Any]], stage_name: str = "Этап 1: Первая $1,000") -> str:
    summary_list = "\n".join(
        f"- {it.get('title_ru')}: {it.get('idea_ru')}"
        for it in items[:8]
    )
    prompt = (
        f"Цель пользователя: освоить ИИ на практике и заработать деньги ({stage_name}).\n"
        "Вот сегодняшняя выжимка свежих инструментов и идей:\n"
        f"{summary_list}\n\n"
        "Выбери ОДНУ самую простую, быструю и денежную идею из списка выше и составь «ЗАДАНИЕ ДНЯ НА 45 МИНУТ».\n"
        "Никакой теории — только конкретные действия руками:\n"
        "1. 🎯 ЧТО ДЕЛАЕМ СЕГОДНЯ: конкретный мини-результат за 45 минут.\n"
        "2. ⏱ ПОШАГОВЫЙ ТАЙМИНГ:\n"
        "   • Минуты 0–15: что открыть и какой промпт/инструмент запустить.\n"
        "   • Минуты 15–30: какой тестовый образец (ролик / бот / услугу / разбор) собрать.\n"
        "   • Минуты 30–45: куда выложить или кому показать, чтобы получить первые отклики/деньги.\n"
        "3. 🧠 КАКОЙ СКИЛЛ ТЫ ПРОКАЧАЕШЬ ЭТИМ ДЕЙСТВИЕМ."
    )
    ans = call_gemini(prompt, timeout=35)
    if ans:
        return clean_markdown_to_html(ans)
    return "⚠️ Не удалось сформировать задание дня. Попробуй через 15 секунд."


# ---------------------------------------------------------------------------
# МОДУЛЬ 1: «🕵️‍♂️ РАЗВЕДЧИК ЧУЖИХ БОЛЕЙ И ЗАКАЗОВ»
# ---------------------------------------------------------------------------
def analyze_pain_points(raw_pains: List[Dict[str, Any]]) -> str:
    lines = []
    for idx, p in enumerate(raw_pains[:12], 1):
        lines.append(f"[{idx}] {p.get('source_name')}: {p.get('title')} | {p.get('raw_text', '')[:300]} | URL: {p.get('url')}")

    prompt = (
        "Ты — охотник за платёжеспособным спросом и болями бизнеса.\n"
        "Ниже реальные свежие посты предпринимателей и пользователей с форумов:\n\n"
        + "\n".join(lines)
        + "\n\nОтбери 5 САМЫХ ДЕНЕЖНЫХ и понятных болей/запросов, которые один человек может закрыть с помощью ИИ.\n"
        "Для каждой из 5 болей напиши на русском:\n"
        "1. 🔥 БОЛЬ КЛИЕНТА (и источник): на что жалуются и за что готовы платить.\n"
        "2. 🛠 РЕШЕНИЕ ЧЕРЕЗ ИИ ЗА ВЕЧЕР: какой связкой нейросетей/ботов закрыть эту задачу.\n"
        "3. 💵 ЧЕК И ГОТОВЫЙ ОФФЕР: за сколько продавать в СНГ/мире ($50–$500) и 1 фраза, чтобы предложить это бизнесу."
    )
    ans = call_gemini(prompt, timeout=35)
    if ans:
        return clean_markdown_to_html(ans)
    return "⚠️ Не удалось проанализировать боли. Попробуй ещё раз через 15 секунд."


# ---------------------------------------------------------------------------
# МОДУЛЬ 5: «🎓 ИИ-ТРЕНАЖЁР НАВЫКА»
# ---------------------------------------------------------------------------
def generate_trainer_lesson(topic_code: str) -> str:
    topics = {
        "prompt": "Секретные приёмы промпт-инжиниринга (мета-промпты, ролевые каскады, заставляем ИИ выдавать готовый продукт без воды)",
        "content": "Создание вирусного контента для YouTube Shorts, TikTok и Reels с помощью ИИ (сценарии, озвучка, видео, удержание)",
        "money": "Упаковка и быстрая продажа ИИ-услуг на первые $1,000 без кода (для бизнеса, селлеров, экспертов и в быту)",
        "agents": "ИИ-агенты, автоматизация рутины и связка бесплатных нейросетей в один конвейер",
        "life": "Нестандартное применение ИИ в быту, покупках, авто, ремонте и экономии денег, о котором не знают 99% людей",
    }
    chosen_topic = topics.get(topic_code, topics["prompt"])

    prompt = (
        f"Ты — жёсткий практик-наставник по ИИ. Тема мини-тренировки: «{chosen_topic}».\n"
        "Выдай концентрированный практический урок на 3 минуты чтения (без воды и приветствий):\n\n"
        "1. ⚡ СУТЬ ПРИЁМА: в чём главная фишка и почему 95% людей делают это неправильно.\n"
        "2. 📋 ГОТОВЫЙ ШАБЛОН / ПРОМПТ / СВЯЗКА: конкретный пример, который можно скопировать и применить прямо сейчас.\n"
        "3. 💰 КАК ПРИМЕНИТЬ ДЛЯ ЗАРАБОТКА ИЛИ ВЫГОДЫ.\n"
        "4. 🎯 МИНИ-ЗАДАНИЕ НА 5 МИНУТ: дай пользователю короткую практическую задачу, которую он может выполнить прямо в ответном сообщении этому боту для проверки!"
    )
    ans = call_gemini(prompt, timeout=35)
    if ans:
        return clean_markdown_to_html(ans)
    return "⚠️ Тренажёр перезагружается, нажми на кнопку ещё раз через 10 секунд."


# ---------------------------------------------------------------------------
# МОДУЛЬ 6: «📸 РЕВЕРС-ИНЖИНИРИНГ ПО ФОТО / ССЫЛКЕ / ТЕКСТУ»
# ---------------------------------------------------------------------------
def reverse_engineer_project(context_text: str, image_b64: Optional[str] = None) -> str:
    prompt = (
        "Ты — главный инженер по реверс-инжинирингу ИИ-продуктов, вирусных каналов и бизнес-схем.\n"
        "Пользователь прислал скриншот, ссылку или описание чужого проекта/ролика/сервиса.\n"
        f"Вводные данные:\n{context_text[:3500]}\n\n"
        "Сделай полный рентген по 4 пунктам (без воды):\n"
        "1. 🔍 КАК ЭТО УСТРОЕНО ПОД КАПОТОМ: какие нейросети, промпты, голоса или скрипты тут используются.\n"
        "2. 💵 ГДЕ ТУТ ДЕНЬГИ И СЕБЕСТОИМОСТЬ: на чём автор зарабатывает и сколько реально стоит генерация.\n"
        "3. 🛠 КАК ПОВТОРИТЬ САМОМУ ЗА 1–2 ВЕЧЕРА БЕСПЛАТНО (пошагово: шаг 1, шаг 2, шаг 3).\n"
        "4. 🚀 КАК СДЕЛАТЬ В 2 РАЗА ЛУЧШЕ ИЛИ ПЕРЕНЕСТИ В ДРУГУЮ НИШУ (чтобы не толкаться с автором, а снять сливки)."
    )
    ans = call_gemini(prompt, timeout=40, image_b64=image_b64)
    if ans:
        return clean_markdown_to_html(ans)
    return "⚠️ Не удалось разобрать объект. Попробуй отправить ещё раз через 15 секунд."


# ---------------------------------------------------------------------------
# МОДУЛЬ 7: «🔮 МАШИНА ВРЕМЕНИ (США → СНГ)»
# ---------------------------------------------------------------------------
def generate_time_machine_report(west_items: List[Dict[str, Any]]) -> str:
    lines = []
    for idx, it in enumerate(west_items[:15], 1):
        lines.append(f"[{idx}] {it.get('source_name')}: {it.get('title')} — {it.get('raw_text', '')[:250]}")

    prompt = (
        "Ты — аналитик арбитража идей между рынком США и рынком СНГ (Беларусь/РФ/Казахстан).\n"
        "То, что выстреливает в США сегодня, доходит до русскоязычного рынка через 2–4 месяца.\n"
        "Вот свежие западные запуски, тренды и продукты:\n\n"
        + "\n".join(lines)
        + "\n\nВыбери ТОП-3 самых перспективных западных тренда/продукта (включая контент YouTube/TikTok, бытовые сервисы и микро-бизнесы), которых ещё почти нет в СНГ.\n"
        "По каждому из 3 трендов распиши:\n"
        "1. 🇺🇸 ЧТО ВЗЛЕТЕЛО НА ЗАПАДЕ: суть продукта или контент-формата и почему за это платят.\n"
        "2. 🇧🇾🇷🇺 КАК АДАПТИРОВАТЬ ПОД СНГ: под какие наши реалии (Telegram, Куфар, Авито, WB/Ozon, русскоязычный YouTube/Shorts, локальные услуги) это переупаковать.\n"
        "3. ⚡ ПЛАН ЗАХВАТА НИШИ ЗА 48 ЧАСОВ: как запуститься первым без вложений."
    )
    ans = call_gemini(prompt, timeout=35)
    if ans:
        return clean_markdown_to_html(ans)
    return "⚠️ Не удалось построить отчёт «Машина времени». Попробуй через 15 секунд."


def clean_markdown_to_html(text: str) -> str:
    """Аккуратно переводит Markdown от Gemini в безопасный Telegram HTML."""
    escaped = html.escape(text)
    # **bold** -> <b>bold</b>
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    # ## Header -> <b>Header</b>
    escaped = re.sub(r"(?m)^#{1,4}\s*(.+)$", r"<b>\1</b>", escaped)
    # `code` -> <code>code</code>
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    return escaped.strip()
