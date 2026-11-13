import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time
import html
import base64
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

import requests
import telebot
from telebot import types

from config import BOT_TOKEN, CATEGORIES, AI_HEALTH_URL
import database as db
import collectors
import ai_engine

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "radar_bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Минское время (UTC+3)
MINSK_TZ = timezone(timedelta(hours=3))

# Состояния ожидания ввода пользователя (канал, доход в трекер, тест в трекер)
user_state: Dict[int, str] = {}
# Последние элементы дайджеста для генерации «Задания дня на 45 мин»
last_digest_cache: Dict[int, List[Dict[str, Any]]] = {}

CATEGORY_BADGES = {
    "news": "🔥 Релиз/Новость",
    "apps": "🚀 Приложение/Быт",
    "money": "💰 Заработок/Кейс",
    "github": "🧩 GitHub/Скилл",
    "prompts": "🧠 Промпт/Гайд",
}


def now_minsk() -> datetime:
    return datetime.now(MINSK_TZ)


# ---------------------------------------------------------------------------
# КЛАВИАТУРЫ
# ---------------------------------------------------------------------------
def get_main_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.row(
        types.KeyboardButton("🌅 Утренний Топ-10"),
        types.KeyboardButton("🌆 Вечерний Топ-10"),
    )
    kb.row(
        types.KeyboardButton("🕵️‍♂️ Разведчик болей ($)"),
        types.KeyboardButton("🔮 Машина времени (США→СНГ)"),
    )
    kb.row(
        types.KeyboardButton("🧬 Неочевидные идеи (Ютуб/Быт)"),
        types.KeyboardButton("🎓 ИИ-Тренажёр навыка"),
    )
    kb.row(
        types.KeyboardButton("💵 Трекер цели ($1k→$100k)"),
        types.KeyboardButton("⭐ Избранное"),
    )
    kb.row(
        types.KeyboardButton("🔥 Полный радар"),
        types.KeyboardButton("📂 Рубрики"),
        types.KeyboardButton("📡 Настройки"),
    )
    return kb


def get_categories_inline_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for cat_key, cat_title in CATEGORIES.items():
        kb.add(types.InlineKeyboardButton(f"{cat_title} (Топ-5)", callback_data=f"cat:{cat_key}"))
    kb.add(types.InlineKeyboardButton("🔄 Собрать полный Топ-10 (все 5 блоков)", callback_data="refresh:full"))
    return kb


def get_digest_chunk_kb(saved_items: List[Dict[str, Any]], edition: str, is_last_chunk: bool = False) -> types.InlineKeyboardMarkup:
    """
    Компактная панель под пачкой из 5 материалов:
    Для каждого № в один ряд:
    [📖 №N] (Разбор) | [💡 №N] (3 Идеи) | [🎬 №N] (Ролик) | [⚡ №N] (Промпт+Оффер) | [⭐ №N]
    """
    kb = types.InlineKeyboardMarkup(row_width=5)
    for it in saved_items:
        num = it["item_num"]
        item_id = it["id"]
        kb.row(
            types.InlineKeyboardButton(f"📖 №{num}", callback_data=f"deep:{item_id}"),
            types.InlineKeyboardButton(f"💡 №{num}", callback_data=f"idea:{item_id}"),
            types.InlineKeyboardButton(f"🎬 №{num}", callback_data=f"vid:{item_id}"),
            types.InlineKeyboardButton(f"⚡ №{num}", callback_data=f"proto:{item_id}"),
            types.InlineKeyboardButton(f"⭐ №{num}", callback_data=f"fav:{item_id}"),
        )
    if is_last_chunk:
        kb.row(
            types.InlineKeyboardButton("🎯 Задание дня (45 мин)", callback_data="daily_sprint"),
            types.InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh:{edition}"),
        )
    return kb


def get_item_actions_kb(item_id: int, url: str) -> types.InlineKeyboardMarkup:
    """Полная панель действий внутри открытой карточки материала."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("📖 Полный разбор", callback_data=f"deep:{item_id}"),
        types.InlineKeyboardButton("💡 3 Идеи (Ютуб/Быт/$)", callback_data=f"idea:{item_id}"),
    )
    kb.row(
        types.InlineKeyboardButton("🎬 В сценарий ролика", callback_data=f"vid:{item_id}"),
        types.InlineKeyboardButton("⚡ Прототип + Оффер", callback_data=f"proto:{item_id}"),
    )
    kb.row(
        types.InlineKeyboardButton("⭐ В избранное", callback_data=f"fav:{item_id}"),
        types.InlineKeyboardButton("🔗 Первоисточник", url=url),
    )
    return kb


# ---------------------------------------------------------------------------
# ФОРМИРОВАНИЕ ВЫПУСКОВ ДАЙДЖЕСТА
# ---------------------------------------------------------------------------
def select_candidates_for_edition(by_cat: Dict[str, List[Dict[str, Any]]], edition: str) -> List[Dict[str, Any]]:
    if edition.startswith("cat_"):
        cat_key = edition.replace("cat_", "")
        return by_cat.get(cat_key, [])[:5]

    plan = {
        "morning": [("news", 5), ("apps", 3), ("github", 2)],
        "evening": [("money", 3), ("github", 4), ("prompts", 3)],
        "full": [("news", 2), ("apps", 2), ("money", 2), ("github", 2), ("prompts", 2)],
    }.get(edition, [("news", 2), ("apps", 2), ("money", 2), ("github", 2), ("prompts", 2)])

    chosen: List[Dict[str, Any]] = []
    used_urls = set()

    for cat_key, quota in plan:
        pool = by_cat.get(cat_key, [])
        added = 0
        for item in pool:
            if item["url"] not in used_urls:
                chosen.append(item)
                used_urls.add(item["url"])
                added += 1
                if added >= quota:
                    break

    target_total = 10
    if len(chosen) < target_total:
        for cat_key in ("news", "apps", "money", "github", "prompts"):
            for item in by_cat.get(cat_key, []):
                if item["url"] not in used_urls:
                    chosen.append(item)
                    used_urls.add(item["url"])
                    if len(chosen) >= target_total:
                        break
            if len(chosen) >= target_total:
                break

    return chosen[:target_total]


def build_and_send_digest(chat_id: int, edition: str = "full", status_msg_id: int = None) -> None:
    titles = {
        "morning": "🌅 <b>УТРЕННИЙ ИИ-РАДАР: Топ-10 новостей, релизов и приложений</b>",
        "evening": "🌆 <b>ВЕЧЕРНИЙ ИИ-РАДАР: Топ-10 заработка (YouTube/Быт/Услуги), скиллов и промптов</b>",
        "full": "🔥 <b>ПОЛНЫЙ ИИ-РАДАР: Топ-10 по всем 5 блокам</b>",
    }
    if edition.startswith("cat_"):
        ck = edition.replace("cat_", "")
        header_title = f"📂 <b>Рубрика: {CATEGORIES.get(ck, ck)} (Топ-5)</b>"
        target_count = 5
    else:
        header_title = titles.get(edition, titles["full"])
        target_count = 10

    if not status_msg_id:
        wait_msg = bot.send_message(
            chat_id,
            "🛰 <b>Сканирую источники...</b>\n"
            "<i>GitHub • Product Hunt • Hacker News • Reddit • X/Smol • Habr • VC • 22+ ТГ-каналов</i>",
        )
        status_msg_id = wait_msg.message_id

    try:
        by_cat = collectors.collect_all_fresh_items()
        candidates = select_candidates_for_edition(by_cat, edition)

        if not candidates:
            bot.edit_message_text(
                "⚠️ Не удалось получить свежие материалы. Попробуйте через минуту.",
                chat_id=chat_id,
                message_id=status_msg_id,
            )
            return

        try:
            bot.edit_message_text(
                f"🧠 <b>Найдено кандидатов: {sum(len(v) for v in by_cat.values())}</b>.\n"
                f"<i>ИИ-редактор отбирает Топ-{len(candidates)}, переводит и готовит схемы применения...</i>",
                chat_id=chat_id,
                message_id=status_msg_id,
            )
        except Exception:
            pass

        curated = ai_engine.prepare_curated_items(candidates, count=target_count)

        saved_items: List[Dict[str, Any]] = []
        for idx, it in enumerate(curated, start=1):
            item_id = db.save_digest_item(
                digest_type=edition,
                item_num=idx,
                category=it["category"],
                title_ru=it["title_ru"],
                summary_ru=it["summary_ru"],
                idea_ru=it["idea_ru"],
                url=it["url"],
                source_name=it["source_name"],
                raw_text=it.get("raw_text", ""),
            )
            db.mark_item_seen(it["url"], it["title_ru"], it["category"])
            it["id"] = item_id
            it["item_num"] = idx
            saved_items.append(it)

        last_digest_cache[chat_id] = saved_items

        try:
            bot.delete_message(chat_id, status_msg_id)
        except Exception:
            pass

        date_str = now_minsk().strftime("%d.%m.%Y %H:%M")
        chunks = [saved_items[i : i + 5] for i in range(0, len(saved_items), 5)]

        for ch_idx, chunk in enumerate(chunks):
            lines: List[str] = []
            if ch_idx == 0:
                lines.append(f"{header_title}\n📅 <i>Сводка на {date_str}</i>\n")
            else:
                lines.append(f"⚡️ <b>Продолжение подборки (№{chunk[0]['item_num']}–{chunk[-1]['item_num']}):</b>\n")

            for it in chunk:
                num = it["item_num"]
                badge = CATEGORY_BADGES.get(it["category"], "📌 ИИ")
                safe_title = html.escape(it["title_ru"])
                safe_summary = html.escape(it["summary_ru"])
                safe_idea = html.escape(it["idea_ru"])
                safe_src = html.escape(it["source_name"])
                safe_url = html.escape(it["url"])

                block = (
                    f"<b>{num}. [{badge}] <a href=\"{safe_url}\">{safe_title}</a></b> <i>({safe_src})</i>\n"
                    f"📝 {safe_summary}\n"
                    f"💡 <b>Идея/Применение:</b> {safe_idea}\n"
                )
                lines.append(block)

            lines.append("👇 <code>📖 Разбор</code> | <code>💡 3 Идеи</code> | <code>🎬 В Ролик</code> | <code>⚡ Промпт+Оффер</code> | <code>⭐ В избранное</code>")
            msg_text = "\n".join(lines)

            is_last = ch_idx == len(chunks) - 1
            kb = get_digest_chunk_kb(chunk, edition=edition, is_last_chunk=is_last)
            bot.send_message(
                chat_id,
                msg_text,
                reply_markup=kb,
                disable_web_page_preview=True,
            )

    except Exception as e:
        logger.error(f"Error building digest: {e}", exc_info=True)
        bot.send_message(chat_id, f"⚠️ Ошибка при сборке дайджеста: {e}")


# ---------------------------------------------------------------------------
# ОБРАБОТЧИКИ ГЛАВНОГО МЕНЮ
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message):
    user = db.ensure_user(message.chat.id)
    stats = db.get_stats()
    tr = db.get_tracker_data(message.chat.id)
    text = (
        "🛰 <b>ИИ РАДАР 2.0 (Комбайн обучения и заработка)</b>\n\n"
        f"🎯 <b>Твоя текущая цель:</b> {tr['stage_name']} (заработано: <b>${tr['total_usd']:.0f}</b> / запущено тестов: <b>{tr['total_actions']}</b>)\n\n"
        "<b>Что умеет бот:</b>\n"
        "• 🌅/🌆 <b>Утренний и Вечерний Топ-10</b> (свежие релизы, YouTube/TikTok, быт, GitHub-скиллы, промпты)\n"
        "• 🕵️‍♂️ <b>Разведчик болей ($)</b> — парсит живые жалобы бизнеса и даёт решение + оффер\n"
        "• 🔮 <b>Машина времени (США→СНГ)</b> — западные тренды, которые ещё свободны у нас\n"
        "• 🎬 <b>Кнопка «В Ролик»</b> под каждой новостью — готовый сценарий Shorts/TikTok\n"
        "• ⚡ <b>Кнопка «Промпт+Оффер»</b> — системный промпт + текст объявления на Куфар/Авито/ТГ\n"
        "• 🎓 <b>ИИ-Тренажёр</b> — ежедневная прокачка навыка за 3 минуты\n"
        "• 📸 <b>Реверс-инжиниринг</b> — просто пришли боту <b>скриншот или ссылку</b> на чужой проект/ролик, и он разложит, как повторить его самому!\n\n"
        f"⏰ Авторассылка: <b>{user['morning_time']}</b> и <b>{user['evening_time']}</b> ({stats['channels']} ТГ-каналов + мировые базы)."
    )
    bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda m: m.text == "🌅 Утренний Топ-10")
def handle_morning_btn(message: types.Message):
    db.ensure_user(message.chat.id)
    threading.Thread(target=build_and_send_digest, args=(message.chat.id, "morning"), daemon=True).start()


@bot.message_handler(func=lambda m: m.text == "🌆 Вечерний Топ-10")
def handle_evening_btn(message: types.Message):
    db.ensure_user(message.chat.id)
    threading.Thread(target=build_and_send_digest, args=(message.chat.id, "evening"), daemon=True).start()


@bot.message_handler(func=lambda m: m.text in ("🔥 Полный радар (5 блоков)", "🔥 Полный радар"))
def handle_full_btn(message: types.Message):
    db.ensure_user(message.chat.id)
    threading.Thread(target=build_and_send_digest, args=(message.chat.id, "full"), daemon=True).start()


# ---------------------------------------------------------------------------
# МОДУЛЬ 1: 🕵️‍♂️ РАЗВЕДЧИК ЧУЖИХ БОЛЕЙ И ЗАКАЗОВ
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == "🕵️‍♂️ Разведчик болей ($)")
def handle_pain_scout_btn(message: types.Message):
    chat_id = message.chat.id
    wait = bot.send_message(
        chat_id,
        "🕵️‍♂️ <b>Сканирую форумы предпринимателей и фриланса...</b>\n"
        "<i>Reddit r/smallbusiness • r/Entrepreneur • r/Automate • r/freelance • Ask HN</i>",
    )

    def run_scout():
        pains = collectors.collect_pain_points()
        report = ai_engine.analyze_pain_points(pains)
        try:
            bot.delete_message(chat_id, wait.message_id)
        except Exception:
            pass
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔄 Найти ещё 5 живых болей", callback_data="run_pains"))
        bot.send_message(
            chat_id,
            f"🕵️‍♂️ <b>РАЗВЕДЧИК БОЛЕЙ: За что люди и бизнес готовы платить сейчас</b>\n\n{report[:3800]}",
            reply_markup=kb,
            disable_web_page_preview=True,
        )

    threading.Thread(target=run_scout, daemon=True).start()


# ---------------------------------------------------------------------------
# МОДУЛЬ 7: 🔮 МАШИНА ВРЕМЕНИ (США -> СНГ)
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == "🔮 Машина времени (США→СНГ)")
def handle_time_machine_btn(message: types.Message):
    chat_id = message.chat.id
    wait = bot.send_message(
        chat_id,
        "🔮 <b>Анализирую свежие запуски рынка США (Product Hunt, Show HN, MicroSaaS, AI Video)...</b>\n"
        "<i>Ищу то, что уже приносит деньги на Западе, но ещё свободно в Беларуси и СНГ...</i>",
    )

    def run_tm():
        west = collectors.collect_western_trends()
        report = ai_engine.generate_time_machine_report(west)
        try:
            bot.delete_message(chat_id, wait.message_id)
        except Exception:
            pass
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔄 Найти ещё 3 западных тренда для клонирования", callback_data="run_tm"))
        bot.send_message(
            chat_id,
            f"🔮 <b>МАШИНА ВРЕМЕНИ (США → СНГ): Топ-3 свободные ниши</b>\n\n{report[:3800]}",
            reply_markup=kb,
            disable_web_page_preview=True,
        )

    threading.Thread(target=run_tm, daemon=True).start()


# ---------------------------------------------------------------------------
# ГЕНЕРАТОР НЕОЧЕВИДНЫХ ИДЕЙ (ЮТУБ / ТИКТОК / БЫТ)
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == "🧬 Неочевидные идеи (Ютуб/Быт)")
def handle_unconventional_btn(message: types.Message):
    chat_id = message.chat.id
    db.ensure_user(chat_id)
    wait = bot.send_message(
        chat_id,
        "🧬 <b>Собираю сегодняшние тренды и генерирую 5 неочевидных идей...</b>\n"
        "<i>YouTube / TikTok / Shorts • Быт и повседневная жизнь • Офлайн-ниши • Услуги без кода</i>",
    )

    def run_gen():
        by_cat = collectors.collect_all_fresh_items(ignore_seen=True)
        titles = []
        for cat in ("apps", "money", "news", "github", "prompts"):
            for it in by_cat.get(cat, [])[:3]:
                titles.append(f"{it['title']} ({it['source_name']})")
        ideas_html = ai_engine.generate_unconventional_ideas(titles)
        try:
            bot.delete_message(chat_id, wait.message_id)
        except Exception:
            pass
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔄 Сгенерировать ещё 5 свежих идей", callback_data="gen_unconv"))
        bot.send_message(
            chat_id,
            f"🧬 <b>5 НЕОЧЕВИДНЫХ ИДЕЙ (На стыке сегодняшних релизов ИИ, Контента и Быта):</b>\n\n{ideas_html[:3800]}",
            reply_markup=kb,
        )

    threading.Thread(target=run_gen, daemon=True).start()


# ---------------------------------------------------------------------------
# МОДУЛЬ 5: 🎓 ИИ-ТРЕНАЖЁР НАВЫКА
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == "🎓 ИИ-Тренажёр навыка")
def handle_trainer_btn(message: types.Message):
    send_trainer_menu(message.chat.id)


def send_trainer_menu(chat_id: int):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🧠 1. Секретный промпт-инжиниринг (уровень Pro)", callback_data="train:prompt"),
        types.InlineKeyboardButton("🎬 2. Контент-завод YouTube / TikTok / Reels", callback_data="train:content"),
        types.InlineKeyboardButton("💰 3. Продажа и упаковка ИИ-услуг на $1,000", callback_data="train:money"),
        types.InlineKeyboardButton("🤖 4. Бесплатные связки нейросетей и ИИ-агенты", callback_data="train:agents"),
        types.InlineKeyboardButton("🏠 5. ИИ в быту, покупках, авто и экономии", callback_data="train:life"),
    )
    bot.send_message(
        chat_id,
        "🎓 <b>ИИ-ТРЕНАЖЁР (Практика на 3 минуты + шаблон + мини-задание)</b>\n\n"
        "Выбери навык, который хочешь прокачать прямо сейчас 👇",
        reply_markup=kb,
    )


# ---------------------------------------------------------------------------
# МОДУЛЬ 4: 💵 ТРЕКЕР ЦЕЛИ ($1,000 -> $10,000 -> $100,000) + ЗАДАНИЕ ДНЯ
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: m.text == "💵 Трекер цели ($1k→$100k)")
def handle_tracker_btn(message: types.Message):
    send_tracker_panel(message.chat.id)


def send_tracker_panel(chat_id: int):
    tr = db.get_tracker_data(chat_id)
    filled = int(tr["pct"] // 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)

    roadmap = (
        f"{'🔥' if tr['stage_num'] == 1 else '✅'} <b>Этап 1: $1,000</b> (быстрые деньги: услуги, боты, YouTube/TikTok-конвейер)\n"
        f"{'🔥' if tr['stage_num'] == 2 else ('✅' if tr['stage_num'] > 2 else '⏳')} <b>Этап 2: $10,000</b> (система: регулярный трафик, база клиентов, микро-продукт)\n"
        f"{'🔥' if tr['stage_num'] == 3 else '⏳'} <b>Этап 3: $100,000</b> (масштабирование: сеть продуктов/каналов на мир)\n"
    )

    history_lines = []
    for e in tr["entries"][:8]:
        if e["amount_usd"] > 0:
            history_lines.append(f"• 💰 <b>+${e['amount_usd']:.0f}</b> — {html.escape(e['title'])} <i>({e['created_at']})</i>")
        else:
            history_lines.append(f"• 🚀 <b>Тест/Запуск:</b> {html.escape(e['title'])} <i>({e['created_at']})</i>")

    hist_block = "\n".join(history_lines) if history_lines else "<i>Пока нет записей. Запусти первый 45-минутный спринт сегодня!</i>"

    text = (
        f"💵 <b>ТРЕКЕР ЦЕЛИ: ПУТЬ К $100,000 НА ИИ</b>\n\n"
        f"{roadmap}\n"
        f"📊 <b>Текущий фокус:</b> {tr['stage_name']}\n"
        f"Прогресс этапа: {bar} <b>{tr['pct']}%</b> (<b>${tr['total_usd']:.0f}</b> из <b>${tr['target_usd']:.0f}</b>)\n"
        f"Выполнено практических запусков/тестов: <b>{tr['total_actions']}</b>\n\n"
        f"📝 <b>Последние действия и доходы:</b>\n{hist_block}"
    )

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🎯 Получить «Задание дня на 45 минут»", callback_data="daily_sprint"),
        types.InlineKeyboardButton("💰 Записать заработок (+$...)", callback_data="trk_add_money"),
        types.InlineKeyboardButton("🚀 Записать выполненный тест / ролик / прототип", callback_data="trk_add_test"),
    )
    if tr["entries"]:
        kb.add(types.InlineKeyboardButton("↩️ Удалить последнюю запись", callback_data="trk_undo"))

    bot.send_message(chat_id, text, reply_markup=kb)


@bot.message_handler(func=lambda m: m.text in ("📂 По рубрикам", "📂 Рубрики"))
def handle_categories_btn(message: types.Message):
    bot.send_message(
        message.chat.id,
        "📂 <b>Выбери конкретную рубрику для мгновенной выжимки:</b>",
        reply_markup=get_categories_inline_kb(),
    )


@bot.message_handler(func=lambda m: m.text == "⭐ Избранное")
def handle_favorites_btn(message: types.Message):
    send_favorites_list(message.chat.id)


def send_favorites_list(chat_id: int):
    favs = db.get_favorites(chat_id, limit=15)
    if not favs:
        bot.send_message(
            chat_id,
            "⭐ <b>В избранном пока пусто.</b>\n"
            "Нажимай кнопку <code>⭐ №...</code> под любым материалом в дайджесте, чтобы сохранять лучшие промпты, репозитории и идеи!",
        )
        return

    lines = [f"⭐ <b>Твоя база избранных идей, скиллов и промптов ({len(favs)}):</b>\n"]
    kb = types.InlineKeyboardMarkup(row_width=4)

    for idx, f in enumerate(favs, start=1):
        badge = CATEGORY_BADGES.get(f["category"], "📌")
        title = html.escape(f["title_ru"])
        url = html.escape(f["url"])
        idea = html.escape(f["idea_ru"])
        lines.append(
            f"<b>{idx}. [{badge}] <a href=\"{url}\">{title}</a></b>\n"
            f"💡 {idea}\n"
        )
        if f.get("item_id"):
            kb.row(
                types.InlineKeyboardButton(f"📖 #{idx}", callback_data=f"deep:{f['item_id']}"),
                types.InlineKeyboardButton(f"🎬 #{idx}", callback_data=f"vid:{f['item_id']}"),
                types.InlineKeyboardButton(f"⚡ #{idx}", callback_data=f"proto:{f['item_id']}"),
                types.InlineKeyboardButton(f"🗑 #{idx}", callback_data=f"delfav:{f['id']}"),
            )

    bot.send_message(chat_id, "\n".join(lines), reply_markup=kb, disable_web_page_preview=True)


@bot.message_handler(func=lambda m: m.text in ("📡 Источники и расписание", "📡 Настройки"))
def handle_sources_and_settings(message: types.Message):
    send_settings_panel(message.chat.id)


def send_settings_panel(chat_id: int):
    user = db.ensure_user(chat_id)
    channels = db.get_active_channels()
    stats = db.get_stats()

    ch_list = ", ".join(f"<code>@{c['username']}</code>" for c in channels[:25])
    if len(channels) > 25:
        ch_list += f" и ещё {len(channels) - 25}"

    status_str = "✅ Включена" if user["is_active"] else "⏸ На паузе"
    text = (
        "📡 <b>Управление источниками и расписанием ИИ-Радара</b>\n\n"
        f"• Авторассылка: <b>{status_str}</b>\n"
        f"• 🌅 Утренний дайджест: <b>{user['morning_time']}</b>\n"
        f"• 🌆 Вечерний дайджест: <b>{user['evening_time']}</b>\n"
        f"• Проанализировано уникальных материалов: <b>{stats['seen']}</b>\n"
        f"• В избранном: <b>{stats['favorites']}</b>\n\n"
        f"📢 <b>Отслеживаемые Telegram-каналы ({len(channels)} шт.):</b>\n{ch_list}\n\n"
        "➕ <i>Чтобы добавить любой публичный ТГ-канал в радар, нажми кнопку ниже или пришли его @username!</i>\n"
        "📸 <i>Чтобы разобрать чужой сервис/канал — просто пришли сюда его скриншот или ссылку!</i>"
    )

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("➕ Добавить ТГ-канал", callback_data="ch_add"),
        types.InlineKeyboardButton("➖ Удалить ТГ-канал", callback_data="ch_del_menu"),
    )
    kb.add(
        types.InlineKeyboardButton(f"🌅 Утро: {user['morning_time']} (сменить)", callback_data="time_m"),
        types.InlineKeyboardButton(f"🌆 Вечер: {user['evening_time']} (сменить)", callback_data="time_e"),
    )
    toggle_label = "⏸ Поставить авто-рассылку на паузу" if user["is_active"] else "▶️ Включить авто-рассылку"
    kb.add(types.InlineKeyboardButton(toggle_label, callback_data="toggle_auto"))

    bot.send_message(chat_id, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# ИНТЕРАКТИВНЫЕ INLINE-КНОПКИ
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: True)
def handle_callbacks(call: types.CallbackQuery):
    chat_id = call.message.chat.id
    data = call.data or ""

    # 1. Глубокий разбор статьи или репозитория («📖 №N»)
    if data.startswith("deep:"):
        item_id = int(data.split(":")[1])
        item = db.get_digest_item(item_id)
        if not item:
            bot.answer_callback_query(call.id, "Материал не найден в базе")
            return

        bot.answer_callback_query(call.id, "📖 Загружаю первоисточник и готовлю разбор...")

        def run_deep():
            if item.get("deep_dive_cache"):
                analysis = item["deep_dive_cache"]
            else:
                wait = bot.send_message(
                    chat_id,
                    f"⏳ <b>Читаю первоисточник:</b> <i>{html.escape(item['title_ru'])}</i>...\n"
                    "<i>Скачиваю полный текст / README и разбираю детали...</i>",
                )
                full_txt = collectors.fetch_full_article_or_repo(item["url"], item.get("raw_text", ""))
                analysis = ai_engine.generate_deep_dive(item, full_txt)
                db.save_item_cache(item_id, "deep_dive_cache", analysis)
                try:
                    bot.delete_message(chat_id, wait.message_id)
                except Exception:
                    pass

            msg = (
                f"📖 <b>ПОЛНЫЙ РАЗБОР: {html.escape(item['title_ru'])}</b>\n"
                f"<i>Источник: {html.escape(item['source_name'])}</i>\n\n"
                f"{analysis[:3600]}"
            )
            bot.send_message(
                chat_id,
                msg,
                reply_markup=get_item_actions_kb(item_id, item["url"]),
                disable_web_page_preview=True,
            )

        threading.Thread(target=run_deep, daemon=True).start()
        return

    # 2. 3 Идеи применения («💡 №N»: YouTube/TikTok + Быт + Деньги)
    if data.startswith("idea:"):
        item_id = int(data.split(":")[1])
        item = db.get_digest_item(item_id)
        if not item:
            bot.answer_callback_query(call.id, "Материал не найден")
            return

        bot.answer_callback_query(call.id, "💡 Прорабатываю 3 сценария (Ютуб/Быт/Деньги)...")

        def run_idea():
            if item.get("monetize_cache"):
                plan_html = item["monetize_cache"]
            else:
                wait = bot.send_message(
                    chat_id,
                    f"🧠 <b>Генерирую 3 сценария применения (YouTube/TikTok, Быт, Деньги):</b>\n<i>{html.escape(item['title_ru'])}</i>...",
                )
                plan_html = ai_engine.generate_monetization_plan(item)
                db.save_item_cache(item_id, "monetize_cache", plan_html)
                try:
                    bot.delete_message(chat_id, wait.message_id)
                except Exception:
                    pass

            msg = (
                f"💡 <b>3 СЦЕНАРИЯ ПРИМЕНЕНИЯ И МОНЕТИЗАЦИИ</b>\n"
                f"<b>База:</b> <a href=\"{html.escape(item['url'])}\">{html.escape(item['title_ru'])}</a>\n\n"
                f"{plan_html[:3600]}"
            )
            bot.send_message(
                chat_id,
                msg,
                reply_markup=get_item_actions_kb(item_id, item["url"]),
                disable_web_page_preview=True,
            )

        threading.Thread(target=run_idea, daemon=True).start()
        return

    # 3. Сценарий для YouTube Shorts / TikTok / Reels («🎬 №N»)
    if data.startswith("vid:"):
        item_id = int(data.split(":")[1])
        item = db.get_digest_item(item_id)
        if not item:
            bot.answer_callback_query(call.id, "Материал не найден")
            return

        bot.answer_callback_query(call.id, "🎬 Пишу сценарий ролика (Хук + Озвучка + Визуал)...")

        def run_vid():
            if item.get("video_script_cache"):
                script_html = item["video_script_cache"]
            else:
                wait = bot.send_message(
                    chat_id,
                    f"🎬 <b>Создаю сценарий YouTube Shorts / TikTok / Reels по теме:</b>\n<i>{html.escape(item['title_ru'])}</i>...",
                )
                script_html = ai_engine.generate_video_script(item)
                db.save_item_cache(item_id, "video_script_cache", script_html)
                try:
                    bot.delete_message(chat_id, wait.message_id)
                except Exception:
                    pass

            msg = (
                f"🎬 <b>ГОТОВЫЙ СЦЕНАРИЙ РОЛИКА (SHORTS / TIKTOK / REELS)</b>\n"
                f"<b>Тема:</b> <a href=\"{html.escape(item['url'])}\">{html.escape(item['title_ru'])}</a>\n\n"
                f"{script_html[:3600]}"
            )
            bot.send_message(
                chat_id,
                msg,
                reply_markup=get_item_actions_kb(item_id, item["url"]),
                disable_web_page_preview=True,
            )

        threading.Thread(target=run_vid, daemon=True).start()
        return

    # 4. Собрать прототип + Системный промпт + Оффер («⚡ №N»)
    if data.startswith("proto:"):
        item_id = int(data.split(":")[1])
        item = db.get_digest_item(item_id)
        if not item:
            bot.answer_callback_query(call.id, "Материал не найден")
            return

        bot.answer_callback_query(call.id, "⚡ Собираю системный промпт и продающий оффер...")

        def run_proto():
            if item.get("prototype_cache"):
                proto_html = item["prototype_cache"]
            else:
                wait = bot.send_message(
                    chat_id,
                    f"⚡ <b>Упаковываю прототип (Системный промпт + объявление на Куфар/Авито/ТГ):</b>\n<i>{html.escape(item['title_ru'])}</i>...",
                )
                proto_html = ai_engine.generate_prototype_and_offer(item)
                db.save_item_cache(item_id, "prototype_cache", proto_html)
                try:
                    bot.delete_message(chat_id, wait.message_id)
                except Exception:
                    pass

            msg = (
                f"⚡ <b>ГОТОВЫЙ ПРОТОТИП: ПРОМПТ + ПРОДАЮЩИЙ ОФФЕР</b>\n"
                f"<b>База:</b> <a href=\"{html.escape(item['url'])}\">{html.escape(item['title_ru'])}</a>\n\n"
                f"{proto_html[:3600]}"
            )
            bot.send_message(
                chat_id,
                msg,
                reply_markup=get_item_actions_kb(item_id, item["url"]),
                disable_web_page_preview=True,
            )

        threading.Thread(target=run_proto, daemon=True).start()
        return

    # 5. Задание дня на 45 минут («🎯 Задание дня»)
    if data == "daily_sprint":
        bot.answer_callback_query(call.id, "🎯 Формирую практическое задание на 45 минут...")

        def run_sprint():
            wait = bot.send_message(chat_id, "🎯 <b>Выбираю самый быстрый шаг к деньгам на сегодня (на 45 минут)...</b>")
            tr = db.get_tracker_data(chat_id)
            items = last_digest_cache.get(chat_id)
            if not items:
                by_cat = collectors.collect_all_fresh_items(ignore_seen=True)
                raw_cands = select_candidates_for_edition(by_cat, "evening")
                items = [
                    {"title_ru": r["title"], "idea_ru": r["raw_text"][:200]}
                    for r in raw_cands[:6]
                ]
            sprint_html = ai_engine.generate_daily_sprint(items, stage_name=tr["stage_name"])
            try:
                bot.delete_message(chat_id, wait.message_id)
            except Exception:
                pass
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                types.InlineKeyboardButton("✅ Выполнил! Записать тест в трекер", callback_data="trk_add_test"),
                types.InlineKeyboardButton("💰 Записать заработанные деньги", callback_data="trk_add_money"),
            )
            bot.send_message(
                chat_id,
                f"🎯 <b>ЗАДАНИЕ ДНЯ НА 45 МИНУТ ({tr['stage_name']})</b>\n\n{sprint_html[:3700]}",
                reply_markup=kb,
            )

        threading.Thread(target=run_sprint, daemon=True).start()
        return

    # 6. ИИ-Тренажёр (уроки по кнопкам)
    if data.startswith("train:"):
        topic_code = data.split(":")[1]
        bot.answer_callback_query(call.id, "🎓 Готовлю практический урок и шаблон...")

        def run_lesson():
            wait = bot.send_message(chat_id, "🎓 <b>Готовлю концентрированный разбор, шаблон и мини-задание...</b>")
            lesson_html = ai_engine.generate_trainer_lesson(topic_code)
            try:
                bot.delete_message(chat_id, wait.message_id)
            except Exception:
                pass
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(
                types.InlineKeyboardButton("🔄 Другой приём в этой теме", callback_data=f"train:{topic_code}"),
                types.InlineKeyboardButton("📚 Все темы тренажёра", callback_data="train_menu"),
            )
            bot.send_message(
                chat_id,
                f"🎓 <b>ИИ-ТРЕНАЖЁР НАВЫКА</b>\n\n{lesson_html[:3700]}",
                reply_markup=kb,
            )

        threading.Thread(target=run_lesson, daemon=True).start()
        return

    if data == "train_menu":
        bot.answer_callback_query(call.id)
        send_trainer_menu(chat_id)
        return

    # 7. Трекер цели ($1k -> $10k -> $100k)
    if data == "trk_add_money":
        user_state[chat_id] = "wait_money"
        bot.answer_callback_query(call.id)
        bot.send_message(
            chat_id,
            "💰 <b>Напиши сумму в $ и за что получено</b>\n"
            "Например: <code>50 Настроил ИИ-бота для автосервиса</code> или <code>25 Партнёрка с Shorts</code>:",
        )
        return

    if data == "trk_add_test":
        user_state[chat_id] = "wait_test"
        bot.answer_callback_query(call.id)
        bot.send_message(
            chat_id,
            "🚀 <b>Напиши одной строкой, что ты сегодня протестировал или запустил</b>\n"
            "Например: <code>Залил 2 ролика в TikTok по сценарию бота</code> или <code>Выложил оффер на Куфар</code>:",
        )
        return

    if data == "trk_undo":
        ok = db.clear_last_tracker_entry(chat_id)
        bot.answer_callback_query(call.id, "Запись удалена" if ok else "Нет записей")
        send_tracker_panel(chat_id)
        return

    # 8. Повторный запуск Разведчика болей и Машины времени
    if data == "run_pains":
        bot.answer_callback_query(call.id, "🕵️‍♂️ Ищу новые боли...")
        handle_pain_scout_btn(call.message)
        return

    if data == "run_tm":
        bot.answer_callback_query(call.id, "🔮 Сканирую западные запуски...")
        handle_time_machine_btn(call.message)
        return

    # 9. Избранное
    if data.startswith("fav:"):
        item_id = int(data.split(":")[1])
        ok = db.add_favorite(chat_id, item_id)
        bot.answer_callback_query(call.id, "⭐ Сохранено в Избранное!" if ok else "Не удалось сохранить")
        return

    if data == "open_favs":
        bot.answer_callback_query(call.id)
        send_favorites_list(chat_id)
        return

    if data.startswith("delfav:"):
        fav_id = int(data.split(":")[1])
        db.remove_favorite(chat_id, fav_id)
        bot.answer_callback_query(call.id, "🗑 Удалено из избранного")
        send_favorites_list(chat_id)
        return

    # 10. Обновление и рубрики
    if data.startswith("refresh:"):
        edition = data.split(":")[1]
        bot.answer_callback_query(call.id, "🔄 Запускаю свежий сбор...")
        threading.Thread(target=build_and_send_digest, args=(chat_id, edition), daemon=True).start()
        return

    if data == "gen_unconv":
        bot.answer_callback_query(call.id, "🧬 Генерирую новые 5 идей...")
        handle_unconventional_btn(call.message)
        return

    if data.startswith("cat:"):
        cat_key = data.split(":")[1]
        bot.answer_callback_query(call.id, "Загружаю Топ-5 рубрики...")
        threading.Thread(target=build_and_send_digest, args=(chat_id, f"cat_{cat_key}"), daemon=True).start()
        return

    # 11. Управление ТГ-каналами и расписанием
    if data == "ch_add":
        user_state[chat_id] = "wait_channel"
        bot.answer_callback_query(call.id)
        bot.send_message(
            chat_id,
            "➕ Пришли юзернейм или ссылку на публичный Telegram-канал (например, <code>@ai_newz</code>):",
        )
        return

    if data == "ch_del_menu":
        bot.answer_callback_query(call.id)
        channels = db.get_active_channels()
        kb = types.InlineKeyboardMarkup(row_width=2)
        buttons = [
            types.InlineKeyboardButton(f"❌ @{c['username']}", callback_data=f"ch_rm:{c['username']}")
            for c in channels[:24]
        ]
        kb.add(*buttons)
        bot.send_message(chat_id, "Выбери канал, который нужно убрать из мониторинга:", reply_markup=kb)
        return

    if data.startswith("ch_rm:"):
        uname = data.split(":", 1)[1]
        db.remove_tg_channel(uname)
        bot.answer_callback_query(call.id, f"Канал @{uname} удалён")
        send_settings_panel(chat_id)
        return

    if data == "time_m":
        user = db.ensure_user(chat_id)
        options = ["07:30", "08:00", "08:30", "09:00", "10:00"]
        cur_idx = options.index(user["morning_time"]) if user["morning_time"] in options else 2
        next_t = options[(cur_idx + 1) % len(options)]
        db.update_user_schedule(chat_id, morning_time=next_t)
        bot.answer_callback_query(call.id, f"🌅 Утренний выпуск перенесён на {next_t}")
        send_settings_panel(chat_id)
        return

    if data == "time_e":
        user = db.ensure_user(chat_id)
        options = ["18:00", "19:00", "19:30", "20:00", "21:00"]
        cur_idx = options.index(user["evening_time"]) if user["evening_time"] in options else 2
        next_t = options[(cur_idx + 1) % len(options)]
        db.update_user_schedule(chat_id, evening_time=next_t)
        bot.answer_callback_query(call.id, f"🌆 Вечерний выпуск перенесён на {next_t}")
        send_settings_panel(chat_id)
        return

    if data == "toggle_auto":
        user = db.ensure_user(chat_id)
        new_state = 0 if user["is_active"] else 1
        db.update_user_schedule(chat_id, is_active=new_state)
        bot.answer_callback_query(call.id, "Статус рассылки обновлён!")
        send_settings_panel(chat_id)
        return


# ---------------------------------------------------------------------------
# МОДУЛЬ 6: 📸 РЕВЕРС-ИНЖИНИРИНГ ПО СКРИНШОТУ / ФОТО
# ---------------------------------------------------------------------------
@bot.message_handler(content_types=["photo"])
def handle_photo_reverse_engineer(message: types.Message):
    chat_id = message.chat.id
    caption = (message.caption or "Разбери, как устроен этот проект/контент на скриншоте, и как мне повторить и монетизировать это с помощью ИИ.").strip()
    wait = bot.send_message(chat_id, "📸 <b>Анализирую скриншот (Реверс-инжиниринг)...</b>")

    def run_photo_rev():
        try:
            file_info = bot.get_file(message.photo[-1].file_id)
            img_bytes = bot.download_file(file_info.file_path)
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            res_html = ai_engine.reverse_engineer_project(caption, image_b64=img_b64)
            try:
                bot.delete_message(chat_id, wait.message_id)
            except Exception:
                pass
            bot.send_message(
                chat_id,
                f"📸 <b>РЕВЕРС-ИНЖИНИРИНГ ПО СКРИНШОТУ:</b>\n\n{res_html[:3800]}",
                reply_markup=get_main_keyboard(),
            )
        except Exception as e:
            logger.error(f"Photo reverse engineer error: {e}")
            bot.send_message(chat_id, f"⚠️ Не удалось обработать фото: {e}")

    threading.Thread(target=run_photo_rev, daemon=True).start()


# ---------------------------------------------------------------------------
# ОБРАБОТКА ТЕКСТА: ТРЕКЕР, КАНАЛЫ, ССЫЛКИ НА РЕВЕРС-ИНЖИНИРИНГ И ДИАЛОГ С ИИ
# ---------------------------------------------------------------------------
@bot.message_handler(content_types=["text"])
def handle_free_text(message: types.Message):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    state = user_state.get(chat_id, "")

    # 1. Ввод заработка в Трекер цели
    if state == "wait_money":
        user_state.pop(chat_id, None)
        m = re.match(r"^\+?(\d+(?:[.,]\d+)?)\s*(.*)$", text)
        if m:
            amount = float(m.group(1).replace(",", "."))
            desc = m.group(2).strip() or "Доход с ИИ-проекта"
        else:
            amount = 0.0
            desc = text
        db.add_tracker_entry(chat_id, amount, desc, status="earned")
        bot.send_message(chat_id, f"🔥 Записано: <b>+${amount:.0f}</b> ({html.escape(desc)})!", reply_markup=get_main_keyboard())
        send_tracker_panel(chat_id)
        return

    # 2. Ввод выполненного теста в Трекер цели
    if state == "wait_test":
        user_state.pop(chat_id, None)
        db.add_tracker_entry(chat_id, 0.0, text, status="launched")
        bot.send_message(chat_id, "🚀 Практический шаг записан в трекер! Так держать.", reply_markup=get_main_keyboard())
        send_tracker_panel(chat_id)
        return

    # 3. Добавление ТГ-канала
    if state == "wait_channel" or re.match(r"^@[a-zA-Z0-9_]{4,}$", text):
        user_state.pop(chat_id, None)
        added = db.add_tg_channel(text)
        if added:
            bot.send_message(
                chat_id,
                f"✅ Канал <code>@{added}</code> добавлен в радар! Его посты будут учитываться при следующей сборке.",
                reply_markup=get_main_keyboard(),
            )
        else:
            bot.send_message(chat_id, "⚠️ Не удалось распознать юзернейм канала.")
        return

    # 4. Если прислана ссылка (URL) — запускаем Реверс-инжиниринг чужого проекта/поста/репозитория
    url_match = re.search(r"(https?://[^\s]+)", text)
    if url_match:
        target_url = url_match.group(1)
        wait = bot.send_message(
            chat_id,
            f"🔍 <b>Запускаю реверс-инжиниринг по ссылке:</b>\n<code>{html.escape(target_url)}</code>\n"
            "<i>Скачиваю содержимое и разбираю, как это устроено, сколько приносит и как повторить...</i>",
            disable_web_page_preview=True,
        )

        def run_url_rev():
            full_content = collectors.fetch_full_article_or_repo(target_url, text)
            res_html = ai_engine.reverse_engineer_project(f"Ссылка: {target_url}\nКонтекст: {full_content}")
            try:
                bot.delete_message(chat_id, wait.message_id)
            except Exception:
                pass
            bot.send_message(
                chat_id,
                f"🔍 <b>РЕВЕРС-ИНЖИНИРИНГ ПРОЕКТА:</b>\n🔗 {html.escape(target_url)}\n\n{res_html[:3700]}",
                disable_web_page_preview=True,
            )

        threading.Thread(target=run_url_rev, daemon=True).start()
        return

    # 5. Свободный диалог с ИИ-Архитектором и Наставником (проверка заданий тренажёра, генерация промптов, бизнес-схем)
    wait = bot.send_message(chat_id, "🧠 <i>Прорабатываю ответ...</i>")

    def ask_architect():
        tr = db.get_tracker_data(chat_id)
        prompt = (
            f"Ты — персональный ИИ-архитектор, промпт-инженер и наставник по заработку на ИИ (текущая цель пользователя: {tr['stage_name']}).\n"
            "Отвечай строго по делу, без воды и лести. Давай готовые промпты, сценарии для YouTube/TikTok, бытовые ноу-хау, код или пошаговые схемы заработка.\n\n"
            f"Запрос пользователя: {text}"
        )
        ans = ai_engine.call_gemini(prompt, timeout=35)
        try:
            bot.delete_message(chat_id, wait.message_id)
        except Exception:
            pass
        if ans:
            bot.send_message(chat_id, ai_engine.clean_markdown_to_html(ans)[:4000])
        else:
            bot.send_message(chat_id, "⚠️ AI-прокси перезапускается, повтори запрос через 15 секунд.")

    threading.Thread(target=ask_architect, daemon=True).start()


# ---------------------------------------------------------------------------
# ФОНОВЫЕ ПОТОКИ: РАСПИСАНИЕ (УТРО/ВЕЧЕР) И ПОДДЕРЖАНИЕ ПРОКСИ В ГОРЯЧЕМ РЕЖИМЕ
# ---------------------------------------------------------------------------
def scheduler_worker():
    logger.info("Scheduler worker started (Minsk UTC+3).")
    while True:
        try:
            now = now_minsk()
            hm = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")

            for u in db.get_active_users():
                tg_id = u["tg_id"]
                if hm == u["morning_time"] and u.get("last_morning_date") != today:
                    logger.info(f"Sending scheduled morning digest to {tg_id}")
                    db.mark_digest_sent(tg_id, "morning", today)
                    build_and_send_digest(tg_id, "morning")

                if hm == u["evening_time"] and u.get("last_evening_date") != today:
                    logger.info(f"Sending scheduled evening digest to {tg_id}")
                    db.mark_digest_sent(tg_id, "evening", today)
                    build_and_send_digest(tg_id, "evening")
        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        time.sleep(25)


def keepalive_worker(webhook_url: str = ""):
    """Пингует Render каждые 8 минут и следит, чтобы Telegram Webhook всегда оставался активным."""
    while True:
        try:
            requests.get(AI_HEALTH_URL, timeout=20)
            if webhook_url:
                info = bot.get_webhook_info()
                if info.url != webhook_url:
                    logger.info(f"Re-binding webhook to {webhook_url}")
                    bot.set_webhook(url=webhook_url)
        except Exception as e:
            logger.debug(f"Keepalive check warning: {e}")
        time.sleep(480)


def setup_bot_commands():
    try:
        bot.set_my_commands([
            types.BotCommand("start", "🚀 Главное меню ИИ-Радара 2.0"),
            types.BotCommand("help", "ℹ️ Справка по всем 7 модулям"),
        ])
    except Exception as e:
        logger.warning(f"Could not set bot commands: {e}")


def process_webhook_update(raw_json: str) -> None:
    """Асинхронно в фоновом потоке обрабатывает входящий вебхук от Telegram."""
    def _run():
        try:
            update = types.Update.de_json(raw_json)
            bot.process_new_updates([update])
        except Exception as e:
            logger.error(f"Webhook update error: {e}", exc_info=True)

    threading.Thread(target=_run, daemon=True).start()


def start_cloud_mode(base_url: str = "https://school-bot-zbh4.onrender.com") -> str:
    """
    Запускает ИИ-Радар 2.0 в облачном режиме на Render (через Telegram Webhook + фоновый планировщик).
    """
    db.init_db()
    setup_bot_commands()
    webhook_url = f"{base_url.rstrip('/')}/radar_webhook"
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        logger.info(f"AIRADAR 2.0 Webhook registered at: {webhook_url}")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")

    threading.Thread(target=scheduler_worker, daemon=True).start()
    threading.Thread(target=keepalive_worker, args=(webhook_url,), daemon=True).start()
    return webhook_url


if __name__ == "__main__":
    db.init_db()
    setup_bot_commands()
    threading.Thread(target=scheduler_worker, daemon=True).start()
    threading.Thread(target=keepalive_worker, daemon=True).start()

    logger.info("AIRADAR Bot 2.0 (@airadar_RPbot) starting polling...")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=25)
        except Exception as e:
            logger.error(f"Polling exception: {e}")
            time.sleep(5)
