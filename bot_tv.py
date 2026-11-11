import os
import sys
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

import telebot
from telebot import types

import database_tv as db
from schools_client import SchoolsClient
from services.weather import format_weather_message
from services.currency import format_currency_message
from services.movies import get_random_movie, get_rezka_url
from services.ai_helper import try_eval_math, ask_homework_tutor
import asyncio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BOT_TOKEN = "8637861449:AAHFx0rVKG7kGWj9nWTUeFF25fPXgTeMHkc"
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

BELLS = {
    "1": "08:30–09:15",
    "2": "09:25–10:10",
    "3": "10:30–11:15",
    "4": "11:35–12:20",
    "5": "12:35–13:20",
    "6": "13:35–14:20"
}

RUSSIAN_WEEKDAYS = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье"
}

WINTER_HOLIDAYS_START = datetime(2026, 12, 26)

user_clients: Dict[int, SchoolsClient] = {}

def get_client_for_user(user: Dict[str, Any]) -> SchoolsClient:
    tg_id = user["tg_id"]
    if tg_id in user_clients and user_clients[tg_id].logged_in:
        return user_clients[tg_id]
    client = SchoolsClient()
    ok = client.login(user["login"], user["password"])
    if ok:
        user_clients[tg_id] = client
    return client

def resolve_user_context(message: types.Message) -> Optional[Dict[str, Any]]:
    if message.chat.type in ("group", "supergroup"):
        owner_id = db.get_family_chat_owner(message.chat.id)
        if owner_id:
            return db.get_user(owner_id)
        return None
    return db.get_user(message.from_user.id)

def get_main_keyboard(has_multi_pupils: bool = False) -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    b1 = types.KeyboardButton("📅 Сегодня")
    b2 = types.KeyboardButton("📆 Завтра")
    b3 = types.KeyboardButton("🗓 Вся неделя")
    b4 = types.KeyboardButton("📊 Четвертные оценки")
    b5 = types.KeyboardButton("🌤 Погода")
    b6 = types.KeyboardButton("💵 Курсы валют")
    b7 = types.KeyboardButton("🎬 Кино на вечер")
    b8 = types.KeyboardButton("💡 AI-помощник")
    
    markup.add(b1, b2)
    markup.add(b3, b4)
    markup.add(b5, b6)
    markup.add(b7, b8)
    
    extra = []
    if has_multi_pupils:
        extra.append(types.KeyboardButton("🔄 Сменить дневник"))
    extra.append(types.KeyboardButton("⚙️ Настройки"))
    markup.add(*extra)
    return markup

def get_settings_keyboard(has_multi_pupils: bool, notifications_enabled: bool) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    notify_text = "🔔 Уведомления: ВКЛ" if notifications_enabled else "🔕 Уведомления: ВЫКЛ"
    markup.add(types.InlineKeyboardButton(notify_text, callback_data="toggle_notify"))
    if has_multi_pupils:
        markup.add(types.InlineKeyboardButton("🔄 Переключить ученика", callback_data="switch_pupil"))
    return markup

def format_day_message(day_data: Dict[str, Any], header_prefix: str = "📅") -> str:
    title = day_data.get("title", "День")
    lessons = day_data.get("lessons", [])
    header = f"{header_prefix} <b>{title}</b>\n"
    if not lessons:
        return header + "<i>Уроков нет или расписание ещё не заполнено</i>"

    subj_entries = []
    for les in lessons:
        subj = les.get("subject", "")
        mark = les.get("mark", "")
        if mark:
            subj_entries.append(f"{subj} ({mark})")
        else:
            subj_entries.append(subj)

    max_subj_len = max([len(s) for s in subj_entries] or [12])
    col_width = max(max_subj_len + 1, 14)

    lines = []
    for les, subj_str in zip(lessons, subj_entries):
        num = str(les.get("number", ""))
        time_str = BELLS.get(num, f"{num} урок    ")
        hw = les.get("homework", "").strip()
        if hw:
            line = f"{time_str} | {subj_str:<{col_width}} [📝 {hw}]"
        else:
            line = f"{time_str} | {subj_str}"
        lines.append(line)

    table_content = "\n".join(lines)
    return f"{header}<pre>{table_content}</pre>"

# ================= HANDLERS =================

@bot.message_handler(commands=["start"])
def handle_start(message: types.Message):
    if message.chat.type in ("group", "supergroup"):
        handle_bind(message)
        return

    user = db.get_user(message.from_user.id)
    if user:
        pupils = db.get_user_pupils(message.from_user.id)
        has_multi = len(pupils) > 1
        name = user.get("active_pupil_name", "Ученик")
        bot.send_message(
            message.chat.id,
            f"👋 С возвращением!\n"
            f"👤 Текущий дневник: <b>{name}</b>\n\n"
            f"Выберите нужный раздел в меню ниже 👇",
            reply_markup=get_main_keyboard(has_multi)
        )
    else:
        msg = bot.send_message(
            message.chat.id,
            "👋 <b>Добро пожаловать в Школьный Дневник (Schools.by)!</b>\n\n"
            "🔐 Введите ваш <b>логин</b> от schools.by (email или имя пользователя):"
        )
        bot.register_next_step_handler(msg, process_login_step)

def process_login_step(message: types.Message):
    login = message.text.strip()
    msg = bot.send_message(message.chat.id, "🔑 Теперь введите ваш <b>пароль</b> от schools.by:")
    bot.register_next_step_handler(msg, process_password_step, login)

def process_password_step(message: types.Message, login: str):
    password = message.text.strip()
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    
    status_msg = bot.send_message(message.chat.id, "🔄 Проверяю авторизацию на schools.by...")
    client = SchoolsClient()
    ok = client.login(login, password)
    if not ok or not client.pupils:
        bot.edit_message_text(
            "❌ <b>Не удалось войти на schools.by.</b>\n"
            "Пожалуйста, проверьте логин и пароль и попробуйте снова: /start",
            message.chat.id,
            status_msg.message_id
        )
        return

    active_pupil = client.pupils[0]
    db.save_user(message.from_user.id, login, password, active_pupil, client.pupils)
    user_clients[message.from_user.id] = client
    
    bot.delete_message(message.chat.id, status_msg.message_id)
    bot.send_message(
        message.chat.id,
        f"✅ <b>Вход успешно выполнен!</b>\n\n"
        f"👤 Ученик: <b>{active_pupil['name']}</b>\n"
        f"🏫 Школа: {active_pupil['subdomain']}.schools.by\n\n"
        f"В меню ниже выберите интересующий раздел 👇",
        reply_markup=get_main_keyboard(len(client.pupils) > 1)
    )

@bot.message_handler(commands=["bind"])
def handle_bind(message: types.Message):
    if message.chat.type not in ("group", "supergroup"):
        bot.send_message(message.chat.id, "Команда /bind предназначена для добавления в семейный чат!")
        return

    user = db.get_user(message.from_user.id)
    if not user:
        bot.send_message(message.chat.id, "Сначала войдите в боте в личных сообщениях через /start")
        return

    pupil_name = user.get("active_pupil_name", "Ученик")
    db.bind_family_chat(message.chat.id, message.from_user.id, message.chat.title or "Семейный чат")

    text = (
        f"👨‍👩‍👧 <b>Семейный чат успешно привязан!</b>\n\n"
        f"Дневник: <b>{pupil_name}</b> (СШ №199)\n\n"
        f"<b>Что будет приходить в этот чат автоматически:</b>\n"
        f"• 🌤 В <b>06:40</b> — утренний прогноз погоды в Минске и совет по одежде\n"
        f"• ⏰ В <b>16:00</b> — расписание и всё домашнее задание на завтра\n"
        f"• 🔔 В течение дня — мгновенные уведомления о новых отметках\n\n"
        f"<b>Команды прямо в чате:</b>\n"
        f"/today — расписание на сегодня\n"
        f"/tomorrow — расписание на завтра\n"
        f"/week — вся неделя\n"
        f"/marks — четвертные оценки\n"
        f"/weather — погода прямо сейчас\n"
        f"/rates — курсы валют НБРБ\n"
        f"/film — кино на вечер\n"
        f"/ai &lt;вопрос&gt; — спросить AI-помощника"
    )
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text in ("📅 Сегодня", "/today"))
def handle_today(message: types.Message):
    user = resolve_user_context(message)
    if not user:
        bot.send_message(message.chat.id, "Сначала выполните вход: /start")
        return

    wait_msg = bot.send_message(message.chat.id, "⏳ Загружаю дневник...")
    try:
        client = get_client_for_user(user)
        diary = client.get_week_diary(user["active_pupil_id"], user["active_subdomain"])
        now = datetime.now()
        weekday_ru = RUSSIAN_WEEKDAYS[now.weekday()]
        target_day = next((d for d in diary.get("days", []) if weekday_ru in d.get("title", "").lower()), None)
        bot.delete_message(message.chat.id, wait_msg.message_id)

        if target_day:
            bot.send_message(message.chat.id, format_day_message(target_day, header_prefix="📅 Расписание на Сегодня:"))
        else:
            if now.weekday() in (5, 6):
                bot.send_message(message.chat.id, "🎉 Сегодня выходной! Уроков нет.")
            else:
                bot.send_message(message.chat.id, f"На сегодня ({weekday_ru.capitalize()}) уроки не найдены.")
    except Exception as e:
        logger.error(f"Error fetching today: {e}")
        bot.edit_message_text("❌ Ошибка при загрузке расписания.", message.chat.id, wait_msg.message_id)

@bot.message_handler(func=lambda m: m.text in ("📆 Завтра", "/tomorrow"))
def handle_tomorrow(message: types.Message):
    user = resolve_user_context(message)
    if not user:
        bot.send_message(message.chat.id, "Сначала выполните вход: /start")
        return

    wait_msg = bot.send_message(message.chat.id, "⏳ Загружаю дневник...")
    try:
        client = get_client_for_user(user)
        now = datetime.now()
        tomorrow = now + timedelta(days=1)
        weekday_ru = RUSSIAN_WEEKDAYS[tomorrow.weekday()]
        is_next_week = False

        if tomorrow.weekday() == 6: # Воскресенье -> смотрим понедельник
            monday_next = now + timedelta(days=2)
            weekday_ru = "понедельник"
            diary = client.get_week_diary(user["active_pupil_id"], user["active_subdomain"], week_date=monday_next.strftime("%Y-%m-%d"))
            is_next_week = True
        else:
            diary = client.get_week_diary(user["active_pupil_id"], user["active_subdomain"])

        target_day = next((d for d in diary.get("days", []) if weekday_ru in d.get("title", "").lower()), None)
        bot.delete_message(message.chat.id, wait_msg.message_id)

        if target_day:
            prefix = "📆 Расписание на Понедельник:" if is_next_week else "📆 Расписание на Завтра:"
            bot.send_message(message.chat.id, format_day_message(target_day, header_prefix=prefix))
        else:
            if tomorrow.weekday() == 5:
                bot.send_message(message.chat.id, "🎉 Завтра суббота! Уроков нет.")
            else:
                bot.send_message(message.chat.id, f"На завтра ({weekday_ru.capitalize()}) уроки не найдены.")
    except Exception as e:
        logger.error(f"Error fetching tomorrow: {e}")
        bot.edit_message_text("❌ Ошибка при загрузке расписания.", message.chat.id, wait_msg.message_id)

@bot.message_handler(func=lambda m: m.text in ("🗓 Вся неделя", "/week"))
def handle_week(message: types.Message):
    user = resolve_user_context(message)
    if not user:
        bot.send_message(message.chat.id, "Сначала выполните вход: /start")
        return

    wait_msg = bot.send_message(message.chat.id, "⏳ Загружаю расписание на всю неделю...")
    try:
        client = get_client_for_user(user)
        diary = client.get_week_diary(user["active_pupil_id"], user["active_subdomain"])
        days = diary.get("days", [])
        bot.delete_message(message.chat.id, wait_msg.message_id)

        if not days:
            bot.send_message(message.chat.id, "Расписание на эту неделю пусто.")
            return

        period = diary.get("period", "")
        header = f"🗓 <b>Расписание на неделю ({period}):</b>\n\n"
        day_blocks = [format_day_message(d) for d in days]
        full_text = header + "\n\n━━━━━━━━━━━━━━━\n\n".join(day_blocks)

        if len(full_text) > 4000:
            curr = header
            for block in day_blocks:
                if len(curr) + len(block) > 3500:
                    bot.send_message(message.chat.id, curr)
                    curr = block + "\n\n━━━━━━━━━━━━━━━\n\n"
                else:
                    curr += block + "\n\n━━━━━━━━━━━━━━━\n\n"
            if curr:
                bot.send_message(message.chat.id, curr)
        else:
            bot.send_message(message.chat.id, full_text)
    except Exception as e:
        logger.error(f"Error fetching week: {e}")
        bot.edit_message_text("❌ Ошибка при загрузке недели.", message.chat.id, wait_msg.message_id)

@bot.message_handler(func=lambda m: m.text in ("📊 Четвертные оценки", "/marks"))
def handle_quarter_marks(message: types.Message):
    user = resolve_user_context(message)
    if not user:
        bot.send_message(message.chat.id, "Сначала выполните вход: /start")
        return

    wait_msg = bot.send_message(message.chat.id, "⏳ Загружаю четвертные оценки...")
    try:
        client = get_client_for_user(user)
        q_data = client.get_quarter_marks(user["active_pupil_id"], user["active_subdomain"])
        subjects = q_data.get("subjects", [])
        avg = q_data.get("average", "")
        pupil_name = user.get("active_pupil_name", "Ученик")
        bot.delete_message(message.chat.id, wait_msg.message_id)

        if not subjects:
            bot.send_message(message.chat.id, "Четвертные оценки пока не выставлены.")
            return

        lines = [f"📊 <b>Четвертные оценки ({pupil_name}):</b>\n"]
        for s in subjects:
            parts = []
            if s.get("q1"): parts.append(f"I: <b>{s['q1']}</b>")
            if s.get("q2"): parts.append(f"II: <b>{s['q2']}</b>")
            if s.get("q3"): parts.append(f"III: <b>{s['q3']}</b>")
            if s.get("q4"): parts.append(f"IV: <b>{s['q4']}</b>")
            if s.get("year"): parts.append(f"Год: <b>{s['year']}</b>")
            scores_str = " | ".join(parts) if parts else "<i>нет отметок</i>"
            lines.append(f"• <b>{s['subject']}</b>: {scores_str}")

        if avg:
            lines.append(f"\n⭐️ <b>Средний балл: {avg}</b>")

        bot.send_message(message.chat.id, "\n".join(lines))
    except Exception as e:
        logger.error(f"Error fetching quarters: {e}")
        bot.edit_message_text("❌ Ошибка при загрузке четвертных оценок.", message.chat.id, wait_msg.message_id)

@bot.message_handler(func=lambda m: m.text in ("🌤 Погода", "/weather"))
def handle_weather(message: types.Message):
    text = format_weather_message(morning_alert=False)
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text in ("💵 Курсы валют", "/rates"))
def handle_rates(message: types.Message):
    text = format_currency_message()
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text in ("🎬 Кино на вечер", "/film"))
def handle_film(message: types.Message):
    markup = types.InlineKeyboardMarkup(row_width=3)
    b1 = types.InlineKeyboardButton("👻 Ужасы", callback_data="film:horror")
    b2 = types.InlineKeyboardButton("✨ Новинки HD", callback_data="film:new")
    b3 = types.InlineKeyboardButton("👨‍👩‍👧 Семейное", callback_data="film:family")
    markup.add(b1, b2, b3)
    bot.send_message(message.chat.id, "🎬 <b>Кино на вечер</b>\n\nВыберите жанр под настроение:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("film:"))
def callback_film(call: types.CallbackQuery):
    cat = call.data.split(":")[1]
    cat_names = {"horror": "👻 Ужасы", "new": "✨ Новинки HD", "family": "👨‍👩‍👧 Семейное / Мультфильмы"}
    movie = get_random_movie(cat)
    rezka_url = get_rezka_url(movie["title"])

    text = (
        f"🎬 <b>{cat_names.get(cat, 'Фильм')}</b>\n\n"
        f"🍿 <b>{movie['title']}</b> ({movie['year']})\n"
        f"⭐️ Рейтинг: <b>{movie['rating']}</b>\n"
        f"📝 <i>{movie['desc']}</i>\n\n"
        f"🔗 <a href='{rezka_url}'>Смотреть онлайн на HDRezka</a>"
    )
    markup = types.InlineKeyboardMarkup()
    b1 = types.InlineKeyboardButton("🎲 Другой фильм", callback_data=f"film:{cat}")
    markup.add(b1)
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.text in ("💡 AI-помощник",))
def handle_ai_prompt(message: types.Message):
    text = (
        "💡 <b>AI-помощник по домашним заданиям</b>\n\n"
        "Отправьте пример или вопрос в чат:\n"
        "• Например: <code>9600 : 24</code> или <code>/ai реши уравнение 2x + 15 = 45</code>\n"
        "• Или отправьте фото страницы тетради с заданием!"
    )
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=["ai"])
def handle_ai_cmd(message: types.Message):
    query = message.text.replace("/ai", "").strip()
    if not query:
        bot.send_message(message.chat.id, "Напишите вопрос после команды: например <code>/ai как пишется слово парашют?</code>")
        return
    process_ai_query(message, query)

@bot.message_handler(content_types=["photo"])
def handle_photo(message: types.Message):
    caption = message.caption or "Помоги решить задание на фото"
    status = bot.send_message(message.chat.id, "🤖 Анализирую задание с фото...")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        ans = asyncio.run(ask_homework_tutor(caption, image_bytes=downloaded))
        bot.edit_message_text(f"💡 <b>Ответ AI-репетитора:</b>\n\n{ans}", message.chat.id, status.message_id)
    except Exception as e:
        bot.edit_message_text(f"❌ Не удалось обработать фото: {e}", message.chat.id, status.message_id)

def process_ai_query(message: types.Message, query: str):
    # Прямой калькулятор
    direct_math = try_eval_math(query)
    if direct_math:
        bot.send_message(message.chat.id, f"💡 <b>Ответ:</b>\n\n{direct_math}")
        return

    status = bot.send_message(message.chat.id, "🤖 AI думает над ответом...")
    try:
        ans = asyncio.run(ask_homework_tutor(query))
        bot.edit_message_text(f"💡 <b>Ответ AI-репетитора:</b>\n\n{ans}", message.chat.id, status.message_id)
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка AI: {e}", message.chat.id, status.message_id)

@bot.message_handler(func=lambda m: m.text in ("⚙️ Настройки",))
def handle_settings(message: types.Message):
    user = db.get_user(message.from_user.id)
    if not user:
        bot.send_message(message.chat.id, "Сначала выполните вход: /start")
        return
    pupils = db.get_user_pupils(message.from_user.id)
    has_multi = len(pupils) > 1
    is_on = user.get("notify_new_grades", 1) == 1
    bot.send_message(message.chat.id, "⚙️ <b>Настройки уведомлений:</b>", reply_markup=get_settings_keyboard(has_multi, is_on))

@bot.callback_query_handler(func=lambda c: c.data == "toggle_notify")
def callback_toggle_notify(call: types.CallbackQuery):
    is_on = db.toggle_notifications(call.from_user.id)
    pupils = db.get_user_pupils(call.from_user.id)
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=get_settings_keyboard(len(pupils) > 1, is_on))
    status_str = "включены" if is_on else "отключены"
    bot.answer_callback_query(call.id, f"Уведомления {status_str}!")

# ================= NOTIFIER BACKGROUND WORKER =================

def notifier_loop():
    logger.info("Background Notifier thread started on Mi Box!")
    last_grades_check = 0

    while True:
        try:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            users = db.get_all_users()

            # 1. Погода в 06:40 (Пн-Пт)
            if now.weekday() < 5 and now.hour == 6 and 40 <= now.minute < 55:
                for u in users:
                    if u.get("notify_new_grades", 1):
                        if not db.has_weather_reminder_been_sent(u["tg_id"], today_str):
                            text = format_weather_message(morning_alert=True)
                            for cid in db.get_all_target_chat_ids_for_user(u["tg_id"]):
                                try:
                                    bot.send_message(cid, text)
                                except Exception:
                                    pass
                            db.mark_weather_reminder_sent(u["tg_id"], today_str)

            # 2. Напоминание расписания в 16:00 (Вс-Чт)
            if now.weekday() in (6, 0, 1, 2, 3) and now.hour == 16 and now.minute < 30:
                for u in users:
                    if u.get("notify_new_grades", 1):
                        if not db.has_daily_reminder_been_sent(u["tg_id"], today_str):
                            client = get_client_for_user(u)
                            tomorrow = now + timedelta(days=1)
                            weekday_ru = RUSSIAN_WEEKDAYS[tomorrow.weekday()]
                            is_next_week = False
                            if tomorrow.weekday() == 6:
                                monday_next = now + timedelta(days=2)
                                weekday_ru = "понедельник"
                                diary = client.get_week_diary(u["active_pupil_id"], u["active_subdomain"], week_date=monday_next.strftime("%Y-%m-%d"))
                                is_next_week = True
                            else:
                                diary = client.get_week_diary(u["active_pupil_id"], u["active_subdomain"])

                            target_day = next((d for d in diary.get("days", []) if weekday_ru in d.get("title", "").lower()), None)
                            if target_day:
                                prefix = "⏰ Расписание и ДЗ на Понедельник (16:00):" if is_next_week else "⏰ Расписание и ДЗ на завтра (16:00):"
                                text = format_day_message(target_day, header_prefix=prefix)
                                for cid in db.get_all_target_chat_ids_for_user(u["tg_id"]):
                                    try:
                                        bot.send_message(cid, text)
                                    except Exception:
                                        pass
                            db.mark_daily_reminder_sent(u["tg_id"], today_str)

            # 3. Проверка отметок каждые 15 мин (08:00 - 20:00)
            now_ts = time.time()
            if 8 <= now.hour <= 20 and (now_ts - last_grades_check >= 15 * 60):
                last_grades_check = now_ts
                for u in users:
                    if u.get("notify_new_grades", 1):
                        client = get_client_for_user(u)
                        pupil_id = u["active_pupil_id"]
                        subdomain = u["active_subdomain"]
                        name = u.get("active_pupil_name", "Ученик")
                        diary = client.get_week_diary(pupil_id, subdomain)
                        q_id = str(diary.get("quarter_id") or "curr")
                        is_init = db.is_initial_marks_sync(pupil_id, q_id)
                        target_chats = db.get_all_target_chat_ids_for_user(u["tg_id"])

                        for day in diary.get("days", []):
                            day_title = day.get("title", "")
                            for les in day.get("lessons", []):
                                mark = les.get("mark", "").strip()
                                subj = les.get("subject", "").strip()
                                if not mark or not subj:
                                    continue
                                if is_init:
                                    db.save_known_mark(pupil_id, q_id, day_title, subj, mark)
                                else:
                                    if not db.is_mark_known(pupil_id, q_id, day_title, subj, mark):
                                        db.save_known_mark(pupil_id, q_id, day_title, subj, mark)
                                        alert = (
                                            f"🔔 <b>Новая оценка!</b>\n\n"
                                            f"👤 <b>{name}</b>\n"
                                            f"📚 Предмет: <b>{subj}</b>\n"
                                            f"🎯 Отметка: <b>[{mark}]</b>\n"
                                            f"📅 День: <i>{day_title}</i>"
                                        )
                                        for cid in target_chats:
                                            try:
                                                bot.send_message(cid, alert)
                                            except Exception:
                                                pass

            # 4. Пятничный дайджест в 17:00
            if now.weekday() == 4 and now.hour == 17 and now.minute < 30:
                for u in users:
                    digest_key = f"digest_{today_str}"
                    if not db.has_daily_reminder_been_sent(u["tg_id"], digest_key):
                        client = get_client_for_user(u)
                        q_data = client.get_quarter_marks(u["active_pupil_id"], u["active_subdomain"])
                        avg = q_data.get("average", "—")
                        days_to_holidays = max((WINTER_HOLIDAYS_START - now).days, 0)
                        text = (
                            f"🎉 <b>Пятничный дайджест недели!</b>\n\n"
                            f"👤 <b>{u.get('active_pupil_name', 'Ученик')}</b> отлично потрудилась на этой неделе!\n"
                            f"📊 Текущий средний балл: <b>{avg}</b>\n"
                            f"🏖 До зимних каникул осталось: <b>{days_to_holidays} дней</b>!\n\n"
                            f"Желаем всей семье прекрасных и уютных выходных! 🌟"
                        )
                        for cid in db.get_all_target_chat_ids_for_user(u["tg_id"]):
                            try:
                                bot.send_message(cid, text)
                            except Exception:
                                pass
                        db.mark_daily_reminder_sent(u["tg_id"], digest_key)

        except Exception as e:
            logger.error(f"Error in background notifier loop: {e}")

        time.sleep(30)

def main():
    db.init_db()
    logger.info("Database initialized.")
    
    # Запуск фонового планировщика
    t = threading.Thread(target=notifier_loop, daemon=True)
    t.start()
    
    logger.info("Bot starting polling on Xiaomi Mi Box...")
    while True:
        try:
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=30)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
