import html
import os
import re
import sys
import time
import logging
import threading
from datetime import datetime, timedelta, date
from typing import Dict, Any, Optional, List, Tuple

import telebot
from telebot import types

import database_tv as db
from schools_client import SchoolsClient
from services.weather import format_weather_message
from services.currency import format_currency_message
from services.movies import get_random_movie, get_rezka_url
from services.meal_erip import (
    DISTRICT_SHORT_KEYS,
    get_pupil_meal_status,
    format_meal_message,
    query_erip_service,
)
import calendar
import webapp_server
import tunnel_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BOT_TOKEN = "8637861449:AAHFx0rVKG7kGWj9nWTUeFF25fPXgTeMHkc"
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")


def update_bot_webapp_menu_button(url: str):
    """Set the Telegram chat menu button to launch Dzetva Mini App via HTTPS."""
    if not url or not url.startswith("https://"):
        return
    try:
        bot.set_chat_menu_button(
            menu_button=types.MenuButtonWebApp(
                text="📱 Dzetva",
                web_app=types.WebAppInfo(url=url),
            )
        )
        logger.info(f"Bot WebApp menu button updated: {url}")
    except Exception as e:
        logger.warning(f"Failed to update WebApp menu button: {e}")

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

def get_client_for_user(user_data: Optional[Dict[str, Any]]) -> Optional[SchoolsClient]:
    if not user_data or not user_data.get("login") or not user_data.get("password"):
        return None
    tg_id = user_data["tg_id"]
    if tg_id in user_clients and user_clients[tg_id].logged_in:
        return user_clients[tg_id]
    client = SchoolsClient()
    ok = client.login(user_data["login"], user_data["password"])
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

def resolve_callback_user(call: types.CallbackQuery) -> Optional[Dict[str, Any]]:
    if call.message and call.message.chat.type in ("group", "supergroup"):
        owner_id = db.get_family_chat_owner(call.message.chat.id)
        if owner_id:
            return db.get_user(owner_id)
    return db.get_user(call.from_user.id)

def get_main_keyboard(has_multi_pupils: bool = False) -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    b1 = types.KeyboardButton("📅 Сегодня")
    b2 = types.KeyboardButton("📆 Завтра")
    b3 = types.KeyboardButton("🗓 Вся неделя")
    b4 = types.KeyboardButton("📊 Четвертные оценки")
    b5 = types.KeyboardButton("🍽 Питание и кружки")
    b6 = types.KeyboardButton("💸 Расходы")
    b7 = types.KeyboardButton("📱 Dzetva")
    b8 = types.KeyboardButton("🌤 Погода")

    markup.add(b1, b2)
    markup.add(b3, b4)
    markup.add(b5, b6)
    markup.add(b7, b8)

    extra = []
    if has_multi_pupils:
        extra.append(types.KeyboardButton("🔄 Сменить дневник"))
    extra.append(types.KeyboardButton("⚙️ Настройки"))
    extra.append(types.KeyboardButton("ℹ️ Справка"))
    markup.add(*extra)
    return markup

def get_settings_keyboard(
    has_multi_pupils: bool,
    notifications_enabled: bool,
    has_login: bool = True,
) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    notify_text = "🔔 Уведомления: ВКЛ" if notifications_enabled else "🔕 Уведомления: ВЫКЛ"
    markup.add(types.InlineKeyboardButton(notify_text, callback_data="toggle_notify"))
    markup.add(types.InlineKeyboardButton("🍽 Счета питания (ЕРИП)", callback_data="meal_manage"))
    if has_multi_pupils:
        markup.add(types.InlineKeyboardButton("🔄 Переключить ученика", callback_data="switch_pupil"))
    if not has_login:
        markup.add(types.InlineKeyboardButton("🔐 Подключить дневник Schools.by", callback_data="start_schools_login"))
    return markup


def get_meal_districts_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for idx, dname in enumerate(DISTRICT_SHORT_KEYS):
        short = dname.replace(" район", "")
        prefix = "⭐ " if idx == 0 else ""
        buttons.append(types.InlineKeyboardButton(f"{prefix}{short}", callback_data=f"meal_dist:{idx}"))
    markup.add(*buttons)
    return markup


def get_meal_actions_keyboard(account_number: str, threshold: float) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    thr_label = f"{int(threshold)} BYN" if threshold > 0 else "Выкл"
    markup.add(
        types.InlineKeyboardButton("🔄 Обновить", callback_data=f"meal_refresh:{account_number}"),
        types.InlineKeyboardButton(f"🔔 Порог: {thr_label}", callback_data=f"meal_thr_menu:{account_number}"),
    )
    markup.add(
        types.InlineKeyboardButton("➕ Добавить счёт", callback_data="meal_add_start"),
        types.InlineKeyboardButton("🗑 Удалить", callback_data=f"meal_del:{account_number}"),
    )
    return markup


def get_meal_threshold_keyboard(account_number: str) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔔 Ниже 3 BYN", callback_data=f"meal_set_thr:{account_number}:3"),
        types.InlineKeyboardButton("🔔 Ниже 5 BYN", callback_data=f"meal_set_thr:{account_number}:5"),
        types.InlineKeyboardButton("🔔 Ниже 10 BYN", callback_data=f"meal_set_thr:{account_number}:10"),
        types.InlineKeyboardButton("🔕 Выкл напоминания", callback_data=f"meal_set_thr:{account_number}:0"),
    )
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"meal_refresh:{account_number}"))
    return markup

def get_pupils_selection_keyboard(pupils: List[Dict[str, Any]]) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=1)
    for p in pupils:
        name = p.get('name', 'Ученик')
        pid = str(p.get('pupil_id', ''))
        markup.add(types.InlineKeyboardButton(f"👤 {name}", callback_data=f"sel_pupil:{pid}"))
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


def _send_diary_connect_prompt(chat_id: int):
    """Send friendly prompt to link Schools.by while noting ERIP works without passwords."""
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "🔐 Подключить дневник Schools.by",
            callback_data="start_schools_login",
        )
    )
    bot.send_message(
        chat_id,
        "📘 <b>Дневник Schools.by пока не подключён</b>\n\n"
        "Для просмотра расписания, домашних заданий и отметок подключите аккаунт Schools.by "
        "(кнопка ниже или команда /login).\n\n"
        "💡 <i>Если вы в 1–2 классе или вам нужен только ЕРИП (питание и кружки), "
        "пароли не нужны — всё работает сразу через <b>📱 Dzetva</b> (/app) и <b>🍽 Питание и кружки</b> (/meal).</i>",
        reply_markup=markup,
    )


def _require_schools_diary(message: types.Message) -> Optional[Dict[str, Any]]:
    """Return user if Schools.by is linked; otherwise send connect prompt and return None."""
    user = resolve_user_context(message)
    if not user:
        bot.send_message(message.chat.id, "Сначала запустите бота: /start")
        return None
    if not user.get("login") or not user.get("active_pupil_id"):
        _send_diary_connect_prompt(message.chat.id)
        return None
    return user


def _prompt_schools_login(chat_id: int):
    """Start step-by-step Schools.by login prompt."""
    msg = bot.send_message(
        chat_id,
        "🔐 <b>Подключение дневника Schools.by</b>\n\n"
        "Введите ваш <b>логин</b> от schools.by (email или имя пользователя):\n"
        "<i>Для отмены отправьте /cancel</i>",
    )
    bot.register_next_step_handler(msg, process_login_step)


@bot.message_handler(commands=["start"])
def handle_start(message: types.Message):
    if message.chat.type in ("group", "supergroup"):
        handle_bind(message)
        return

    user_id = message.from_user.id
    existing_user = db.get_user(user_id)
    url = tunnel_manager.get_public_webapp_url()
    personal_url = f"{url.rstrip('/')}/?uid={user_id}"

    if not existing_user or not existing_user.get("login"):
        db.ensure_basic_user(user_id, message.from_user.first_name or "")

        bot.send_message(
            message.chat.id,
            "👋 <b>Добро пожаловать в Dzetva!</b>\n"
            "Школьный помощник для родителей: питание ЕРИП, кружки, учёт расходов и дневник Schools.by.",
            reply_markup=get_main_keyboard(False),
        )

        inline_kb = types.InlineKeyboardMarkup(row_width=1)
        if url.startswith("https://"):
            inline_kb.add(
                types.InlineKeyboardButton(
                    "📱 Открыть Dzetva (ЕРИП и кружки)",
                    web_app=types.WebAppInfo(url=personal_url),
                )
            )
        inline_kb.add(
            types.InlineKeyboardButton(
                "🍽 Подключить питание по 10 цифрам",
                callback_data="meal_add_start",
            )
        )
        inline_kb.add(
            types.InlineKeyboardButton(
                "🔐 Подключить дневник Schools.by",
                callback_data="start_schools_login",
            )
        )

        bot.send_message(
            message.chat.id,
            "🚀 <b>Начните без лишних паролей:</b>\n\n"
            "• 🎒 <b>1–2 класс или нужен только ЕРИП (питание / кружки):</b> "
            "пароли не нужны — можно начинать сразу!\n"
            "• 📘 <b>Есть дневник Schools.by:</b> нажмите «Подключить дневник Schools.by» или введите /login.\n\n"
            "Выберите действие 👇",
            reply_markup=inline_kb,
        )
        return

    pupils = db.get_user_pupils(user_id)
    has_multi = len(pupils) > 1
    name = existing_user.get("active_pupil_name") or "Ученик"
    bot.send_message(
        message.chat.id,
        f"👋 С возвращением!\n"
        f"👤 Текущий дневник: <b>{html.escape(str(name))}</b>\n\n"
        f"Выберите нужный раздел в меню ниже 👇\n"
        f"📱 <i>Для питания ЕРИП, кружков и расходов также используйте кнопку <b>📱 Dzetva</b> или /app.</i>",
        reply_markup=get_main_keyboard(has_multi),
    )
    if url.startswith("https://"):
        app_kb = types.InlineKeyboardMarkup()
        app_kb.add(
            types.InlineKeyboardButton(
                "📱 Открыть Dzetva",
                web_app=types.WebAppInfo(url=personal_url),
            )
        )
        bot.send_message(
            message.chat.id,
            "🚀 Быстрый запуск веб-приложения <b>Dzetva</b>:",
            reply_markup=app_kb,
        )


@bot.message_handler(commands=["login"])
def handle_login(message: types.Message):
    if message.chat.type in ("group", "supergroup"):
        bot.send_message(
            message.chat.id,
            "🔐 Для безопасности ввод логина и пароля Schools.by доступен только в личных сообщениях с ботом.",
        )
        return
    db.ensure_basic_user(message.from_user.id, message.from_user.first_name or "")
    _prompt_schools_login(message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data == "start_schools_login")
def callback_start_schools_login(call: types.CallbackQuery):
    bot.answer_callback_query(call.id)
    if call.message and call.message.chat.type in ("group", "supergroup"):
        bot.send_message(
            call.message.chat.id,
            "🔐 Откройте личный чат с ботом и отправьте /login, чтобы подключить дневник Schools.by.",
        )
        return
    db.ensure_basic_user(call.from_user.id, call.from_user.first_name or "")
    _prompt_schools_login(call.message.chat.id)


def process_login_step(message: types.Message):
    if not message.text:
        bot.send_message(message.chat.id, "❌ Отправьте логин текстом или повторите через /login.")
        return
    login = message.text.strip()
    if login.lower() == "/cancel":
        bot.send_message(message.chat.id, "❌ Подключение Schools.by отменено.")
        return
    if login.startswith("/"):
        bot.send_message(
            message.chat.id,
            "ℹ️ Ввод логина отменён. Когда будете готовы, используйте /login или повторите команду.",
        )
        return
    msg = bot.send_message(
        message.chat.id,
        "🔑 Теперь введите ваш <b>пароль</b> от schools.by:\n<i>Для отмены отправьте /cancel</i>",
    )
    bot.register_next_step_handler(msg, process_password_step, login)


def process_password_step(message: types.Message, login: str):
    if not message.text:
        bot.send_message(message.chat.id, "❌ Отправьте пароль текстом или повторите через /login.")
        return
    password = message.text.strip()
    if password.lower() == "/cancel":
        bot.send_message(message.chat.id, "❌ Подключение Schools.by отменено.")
        return
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
            "Пожалуйста, проверьте логин и пароль и попробуйте снова: /login",
            message.chat.id,
            status_msg.message_id
        )
        return

    active_pupil = client.pupils[0]
    db.save_user(message.from_user.id, login, password, active_pupil, client.pupils)
    user_clients[message.from_user.id] = client

    try:
        bot.delete_message(message.chat.id, status_msg.message_id)
    except Exception:
        pass
    bot.send_message(
        message.chat.id,
        f"✅ <b>Вход успешно выполнен!</b>\n\n"
        f"👤 Ученик: <b>{html.escape(str(active_pupil['name']))}</b>\n"
        f"🏫 Школа: {html.escape(str(active_pupil['subdomain']))}.schools.by\n\n"
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
        bot.send_message(message.chat.id, "Сначала запустите бота в личных сообщениях через /start")
        return

    pupil_name = user.get("active_pupil_name") or "Семейный профиль (ЕРИП)"
    db.bind_family_chat(message.chat.id, message.from_user.id, message.chat.title or "Семейный чат")

    text = (
        f"👨‍👩‍👧 <b>Семейный чат успешно привязан!</b>\n\n"
        f"Дневник: <b>{pupil_name}</b> (СШ №199)\n\n"
        f"<b>Что будет приходить в этот чат автоматически:</b>\n"
        f"• 🌤 В <b>06:40</b> — утренний прогноз погоды в Минске и совет по одежде\n"
        f"• ⏰ В <b>16:00</b> — расписание и всё домашнее задание на завтра\n"
        f"• 🔔 В течение дня — мгновенные уведомления о новых отметках\n\n"
        f"<b>Быстрые команды прямо в чате:</b>\n"
        f"/0 — расписание на сегодня\n"
        f"/1 — расписание на завтра\n"
        f"/7 — расписание на всю неделю\n"
        f"/m — четвертные оценки\n"
        f"/. &lt;вопрос&gt; — спросить AI-помощника (или /ai)\n"
        f"/draw &lt;описание&gt; — нарисовать картинку (FLUX)\n"
        f"/weather — погода прямо сейчас\n"
        f"/rates — курсы валют НБРБ\n"
        f"/film — кино на вечер"
    )
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=["0", "today"])
@bot.message_handler(func=lambda m: m.text and (m.text.strip().lower() in ("0", "/0", "сегодня", "📅 сегодня") or m.text.startswith("/0") or m.text.startswith("/today")))
def handle_today(message: types.Message):
    user = _require_schools_diary(message)
    if not user:
        return

    wait_msg = bot.send_message(message.chat.id, "⏳ Загружаю дневник...")
    try:
        client = get_client_for_user(user)
        if not client:
            try:
                bot.delete_message(message.chat.id, wait_msg.message_id)
            except Exception:
                pass
            _send_diary_connect_prompt(message.chat.id)
            return
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

@bot.message_handler(commands=["1", "tomorrow"])
@bot.message_handler(func=lambda m: m.text and (m.text.strip().lower() in ("1", "/1", "завтра", "📆 завтра") or m.text.startswith("/1") or m.text.startswith("/tomorrow")))
def handle_tomorrow(message: types.Message):
    user = _require_schools_diary(message)
    if not user:
        return

    wait_msg = bot.send_message(message.chat.id, "⏳ Загружаю дневник...")
    try:
        client = get_client_for_user(user)
        if not client:
            try:
                bot.delete_message(message.chat.id, wait_msg.message_id)
            except Exception:
                pass
            _send_diary_connect_prompt(message.chat.id)
            return
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

@bot.message_handler(commands=["7", "week"])
@bot.message_handler(func=lambda m: m.text and (m.text.strip().lower() in ("7", "/7", "неделя", "🗓 вся неделя") or m.text.startswith("/7") or m.text.startswith("/week")))
def handle_week(message: types.Message):
    user = _require_schools_diary(message)
    if not user:
        return

    wait_msg = bot.send_message(message.chat.id, "⏳ Загружаю расписание на всю неделю...")
    try:
        client = get_client_for_user(user)
        if not client:
            try:
                bot.delete_message(message.chat.id, wait_msg.message_id)
            except Exception:
                pass
            _send_diary_connect_prompt(message.chat.id)
            return
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

@bot.message_handler(commands=["m", "marks"])
@bot.message_handler(func=lambda m: m.text and (m.text.strip().lower() in ("m", "м", "/m", "/м", "оценки", "отметки", "📊 четвертные оценки") or m.text.startswith("/m") or m.text.startswith("/marks") or m.text.startswith("/м")))
def handle_quarter_marks(message: types.Message):
    user = _require_schools_diary(message)
    if not user:
        return

    wait_msg = bot.send_message(message.chat.id, "⏳ Загружаю четвертные оценки...")
    try:
        client = get_client_for_user(user)
        if not client:
            try:
                bot.delete_message(message.chat.id, wait_msg.message_id)
            except Exception:
                pass
            _send_diary_connect_prompt(message.chat.id)
            return
        q_data = client.get_quarter_marks(user["active_pupil_id"], user["active_subdomain"])
        subjects = q_data.get("subjects", [])
        avg = q_data.get("average", "")
        pupil_name = user.get("active_pupil_name") or "Ученик"
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

@bot.message_handler(commands=["weather", "w"])
@bot.message_handler(func=lambda m: m.text in ("🌤 Погода", "/weather", "/w"))
def handle_weather(message: types.Message):
    text = format_weather_message(morning_alert=False)
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=["rates", "r"])
@bot.message_handler(func=lambda m: m.text in ("💵 Курсы валют", "/rates", "/r"))
def handle_rates(message: types.Message):
    text = format_currency_message()
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=["film", "f"])
@bot.message_handler(func=lambda m: m.text in ("🎬 Кино на вечер", "/film", "/f"))
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


HELP_TEXT = (
    "🌟 <b>Dzetva — Школьный Дневник и Баланс</b>\n\n"
    "📚 <b>ШКОЛЬНЫЙ ДНЕВНИК</b>\n"
    "• <b>📅 Сегодня / 📆 Завтра / 🗓 Вся неделя:</b> расписание с временем звонков, оценки и ДЗ.\n"
    "• <b>📊 Четвертные оценки:</b> четвертные баллы и средний балл.\n"
    "• <b>🔔 Автоуведомления:</b> погода утром (06:40), ДЗ на завтра в 16:00 и мгновенные оповещения о новых оценках.\n\n"
    "🍽 <b>ПИТАНИЕ, КРУЖКИ И РАСХОДЫ</b>\n"
    "• <b>🍽 Питание и кружки</b> (<code>/meal</code>): баланс столовой, буфета и привязанных секций из ЕРИП.\n"
    "• <b>💸 Расходы</b> (<code>/expenses</code>): статистика трат на ребёнка (неделя / месяц / год).\n"
    "• <b>📱 Dzetva</b> (<code>/app</code>): отдельное мобильное веб-приложение с учёбой, балансами ЕРИП, кружками и аналитикой.\n\n"
    "⚡️ <b>БЫСТРЫЕ КОМАНДЫ:</b>\n"
    "• <code>/0</code> — Сегодня • <code>/1</code> — Завтра • <code>/7</code> — Неделя • <code>/m</code> — Оценки\n"
    "• <code>/meal</code> — Балансы ЕРИП • <code>/check_alerts</code> — Проверить алерты\n"
    "• <code>/expenses</code> — Расходы • <code>/app</code> — Открыть Dzetva • <code>/login</code> — Дневник Schools.by • <code>/bind</code> — Семейный чат"
)


@bot.message_handler(commands=["help"])
@bot.message_handler(func=lambda m: m.text in ("ℹ️ Справка", "/help", "Справка", "Помощь"))
def handle_help(message: types.Message):
    bot.send_message(message.chat.id, HELP_TEXT)


@bot.message_handler(commands=["app"])
@bot.message_handler(func=lambda m: m.text and ("dzetva" in m.text.lower() or "школьный пульт" in m.text.lower() or "📱" in m.text))
def handle_open_app(message: types.Message):
    url = tunnel_manager.get_public_webapp_url()
    uid = message.from_user.id
    personal_url = f"{url.rstrip('/')}/?uid={uid}"
    local_url = f"http://192.168.1.35:8080/?uid={uid}"

    markup = None
    if url.startswith("https://"):
        markup = types.InlineKeyboardMarkup(row_width=1)
        if message.chat.type == "private":
            markup.add(
                types.InlineKeyboardButton(
                    "📱 Открыть Dzetva в Telegram",
                    web_app=types.WebAppInfo(url=personal_url),
                )
            )
        markup.add(types.InlineKeyboardButton("🌐 Открыть в браузере", url=personal_url))

        text = (
            f"📱 <b>Dzetva — веб-приложение</b>\n\n"
            f"Нажмите «<b>📱 Открыть Dzetva в Telegram</b>», чтобы запустить <b>Telegram Mini App</b> прямо внутри Telegram "
            f"(работает с любого интернета и мобильной сети).\n\n"
            f"🌐 Ссылка для браузера:\n<code>{html.escape(personal_url)}</code>\n\n"
            f"🏠 Локальный Wi-Fi (в одной сети с приставкой): <code>http://192.168.1.35:8080</code> "
            f"(<code>{html.escape(local_url)}</code>)\n\n"
            f"💡 <i>В браузере телефона можно нажать «Поделиться / Меню» → «На экран Домой», чтобы установить как приложение.</i>"
        )
    else:
        text = (
            f"📱 <b>Dzetva — веб-приложение</b>\n\n"
            f"Публичный HTTPS-туннель сейчас не активен, поэтому приложение открывается по локальному Wi-Fi (в одной сети с сервером):\n"
            f"👉 <code>{html.escape(personal_url)}</code>\n\n"
            f"🏠 Адрес домашнего сервера: <code>http://192.168.1.35:8080</code>\n\n"
            f"💡 <i>Когда активен HTTPS-туннель (или задан WEBAPP_PUBLIC_URL), здесь появится кнопка для Telegram Mini App из любой точки мира.</i>"
        )

    bot.send_message(message.chat.id, text, reply_markup=markup)


@bot.message_handler(commands=["expenses", "spend"])
@bot.message_handler(func=lambda m: m.text and ("расходы" in m.text.lower() or "💸" in m.text))
def handle_expenses(message: types.Message):
    user = resolve_user_context(message)
    if not user:
        bot.send_message(message.chat.id, "Сначала выполните вход: /start")
        return
    children = db.ensure_children_synced(user["tg_id"])
    cat_labels = {
        "canteen": "🥗 Столовая",
        "buffet": "🥐 Буфет",
        "club": "🤖 Кружки",
        "textbooks": "📚 Учебники",
        "other": "📌 Прочее",
    }
    lines = ["💸 <b>Школьные расходы (текущий месяц)</b>\n"]
    total_all = 0.0
    for ch in children:
        s = db.get_expenses_summary(user["tg_id"], child_id=ch["id"], period="month")
        total_all += s["total"]
        lines.append(f"👤 <b>{ch['name']}: {s['total']:.2f} BYN</b>".replace(".", ","))
        for k, lbl in cat_labels.items():
            v = s["by_category"].get(k, 0.0)
            if v > 0:
                lines.append(f"  • {lbl}: {v:.2f} BYN".replace(".", ","))
    if not children:
        lines.append("Профили детей пока пустые. Откройте 📱 Dzetva!")
    else:
        lines.append(f"\n💰 <b>Итого по семье: {total_all:.2f} BYN</b>".replace(".", ","))
    lines.append("\n<i>Подробные графики (неделя / месяц / четверть / год) — в приложении 📱 Dzetva.</i>")
    bot.send_message(message.chat.id, "\n".join(lines))

@bot.message_handler(commands=["settings", "setup"])
@bot.message_handler(func=lambda m: m.text and ("настройки" in m.text.lower() or "⚙️" in m.text or m.text.startswith("/settings")))
def handle_settings(message: types.Message):
    user = resolve_user_context(message)
    if not user:
        bot.send_message(message.chat.id, "Сначала выполните вход: /start")
        return
    pupils = db.get_user_pupils(user["tg_id"])
    has_multi = len(pupils) > 1
    has_login = bool(user.get("login"))
    is_on = user.get("notify_new_grades", 1) == 1
    pupil_name = user.get("active_pupil_name") or ("Ученик" if has_login else "не подключён (только ЕРИП)")
    notify_val = "ВКЛ 🔔" if is_on else "ВЫКЛ 🔕"
    text = (
        f"⚙️ <b>Настройки:</b>\n\n"
        f"👤 Активный дневник: <b>{html.escape(str(pupil_name))}</b>\n"
        f"🔔 Автоуведомления: <b>{notify_val}</b>\n\n"
        f"Нажмите на кнопку ниже, чтобы изменить:"
    )
    bot.send_message(
        message.chat.id,
        text,
        reply_markup=get_settings_keyboard(has_multi, is_on, has_login=has_login),
    )

@bot.message_handler(func=lambda m: m.text and ("сменить дневник" in m.text.lower() or "переключить" in m.text.lower()))
def handle_switch_pupil_btn(message: types.Message):
    user = _require_schools_diary(message)
    if not user:
        return
    pupils = db.get_user_pupils(user["tg_id"])
    if not pupils:
        bot.send_message(message.chat.id, "Дети не найдены в аккаунте.")
        return
    bot.send_message(message.chat.id, "Выберите ученика из списка:", reply_markup=get_pupils_selection_keyboard(pupils))

@bot.callback_query_handler(func=lambda c: c.data == "switch_pupil")
def callback_switch_pupil(call: types.CallbackQuery):
    user = resolve_callback_user(call)
    if not user:
        bot.answer_callback_query(call.id, "Сначала выполните вход: /start")
        return
    if not user.get("login") or not user.get("active_pupil_id"):
        bot.answer_callback_query(call.id)
        _send_diary_connect_prompt(call.message.chat.id)
        return
    pupils = db.get_user_pupils(user["tg_id"])
    if not pupils:
        bot.answer_callback_query(call.id, "Дети не найдены")
        return
    bot.edit_message_text(
        "Выберите ученика из списка:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=get_pupils_selection_keyboard(pupils)
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("sel_pupil:"))
def callback_select_pupil(call: types.CallbackQuery):
    pupil_id = call.data.split(":")[1]
    user = resolve_callback_user(call)
    if not user:
        bot.answer_callback_query(call.id, "Сначала выполните вход: /start")
        return
    pupils = db.get_user_pupils(user["tg_id"])
    target = next((p for p in pupils if str(p["pupil_id"]) == str(pupil_id)), None)
    if target:
        db.set_active_pupil(user["tg_id"], target["pupil_id"], target["name"], target["subdomain"])
        bot.edit_message_text(f"✅ Активный дневник переключен на: <b>{target['name']}</b>", call.message.chat.id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "Ученик не найден")

@bot.callback_query_handler(func=lambda c: c.data == "toggle_notify")
def callback_toggle_notify(call: types.CallbackQuery):
    user = resolve_callback_user(call)
    target_uid = user["tg_id"] if user else call.from_user.id
    is_on = db.toggle_notifications(target_uid)
    pupils = db.get_user_pupils(target_uid) if user else []
    has_login = bool(user and user.get("login"))
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_settings_keyboard(len(pupils) > 1, is_on, has_login=has_login),
        )
    except Exception:
        pass
    status_str = "включены" if is_on else "отключены"
    bot.answer_callback_query(call.id, f"Уведомления {status_str}!")


def _render_meal_card(account_row: Dict[str, Any]) -> Tuple[str, types.InlineKeyboardMarkup]:
    acc = account_row["account_number"]
    district = account_row.get("district") or "Фрунзенский район"
    label = account_row.get("label") or "Ученик"
    threshold = float(account_row.get("alert_threshold") or 0.0)
    status = get_pupil_meal_status(district, acc)
    text = format_meal_message(label, district, acc, status, alert_threshold=threshold)
    markup = get_meal_actions_keyboard(acc, threshold)
    return text, markup


def _send_meal_setup_prompt(chat_id: int):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ Подключить счёт ЕРИП", callback_data="meal_add_start"))
    bot.send_message(
        chat_id,
        "🍽 <b>Школьное питание (Столовая + Буфет)</b>\n\n"
        "Лицевой счёт ЕРИП ещё не привязан.\n\n"
        "Нажмите кнопку ниже или сразу отправьте команду в виде:\n"
        "<code>/meal_add 1234567890 Иван</code>",
        reply_markup=markup,
    )


def _save_and_show_meal_account(
    chat_id: int,
    owner_tg_id: int,
    account_number: str,
    label: str,
    district: str = "Фрунзенский район",
):
    wait_msg = bot.send_message(chat_id, f"⏳ Проверяю счёт <code>{account_number}</code> в ЕРИП ({district})...")
    status = get_pupil_meal_status(district, account_number)
    if not status.get("ok"):
        err = status.get("error") or "Счёт не найден в ЕРИП"
        bot.edit_message_text(
            f"❌ Не удалось найти счёт <code>{account_number}</code> ({district}):\n<i>{err}</i>\n\n"
            f"Проверьте номер счёта и район, затем попробуйте снова: /meal_add",
            chat_id,
            wait_msg.message_id,
        )
        return

    db.save_meal_account(owner_tg_id, account_number, label=label, district=district, alert_threshold=5.0)
    text = format_meal_message(label, district, account_number, status, alert_threshold=5.0)
    markup = get_meal_actions_keyboard(account_number, 5.0)
    bot.edit_message_text(
        f"✅ <b>Счёт питания сохранён!</b>\n\n{text}",
        chat_id,
        wait_msg.message_id,
        reply_markup=markup,
    )


@bot.message_handler(commands=["meal", "food", "eat"])
@bot.message_handler(func=lambda m: m.text and (m.text in ("🍽 Питание", "🍽 Питание и кружки", "Питание", "Буфет") or m.text.startswith("🍽")))
def handle_meal_balance(message: types.Message):
    user = resolve_user_context(message)
    if not user:
        bot.send_message(message.chat.id, "Сначала выполните вход: /start")
        return

    clubs = db.get_child_clubs(user["tg_id"])
    accounts = db.get_meal_accounts(user["tg_id"])
    if not clubs and not accounts:
        _send_meal_setup_prompt(message.chat.id)
        return

    wait_msg = bot.send_message(message.chat.id, "⏳ Запрашиваю актуальные данные в ЕРИП...")
    if clubs:
        children_map = {c["id"]: c["name"] for c in db.ensure_children_synced(user["tg_id"])}
        lines = ["🍽 <b>Питание и кружки (ЕРИП)</b>\n"]
        by_child: Dict[int, List[Dict[str, Any]]] = {}
        for cl in clubs:
            by_child.setdefault(cl["child_id"], []).append(cl)
        today_str = date.today().isoformat()
        month_key = date.today().strftime("%Y-%m")
        for cid, cl_list in by_child.items():
            lines.append(f"👤 <b>{children_map.get(cid, 'Ученик')}</b>")
            for cl in cl_list:
                res = query_erip_service(cl["erip_code"], cl["account_number"], cl.get("di_type") or "9191")
                if res.get("ok"):
                    db.update_club_erip_state(
                        user["tg_id"], cl["id"], res.get("fio", ""),
                        res.get("balance"), res.get("to_pay"), today_str,
                        res.get("saldo_start"), res.get("paid_period"),
                    )
                    if res.get("balance") is not None:
                        bal_txt = f"<b>{res['balance']:.2f} BYN</b>".replace(".", ",")
                    elif cl.get("last_paid_month") == month_key:
                        bal_txt = "✅ оплачено в этом мес."
                    elif cl.get("monthly_amount"):
                        bal_txt = f"абон. {float(cl['monthly_amount']):.2f} BYN".replace(".", ",")
                    else:
                        bal_txt = f"к оплате {float(res.get('to_pay') or 0):.2f} BYN".replace(".", ",")
                else:
                    bal_txt = f"<i>{res.get('error', 'ошибка')}</i>"
                lines.append(f"• {cl['org_name']}: {bal_txt}")
            lines.append("")
        bot.edit_message_text("\n".join(lines), message.chat.id, wait_msg.message_id)
        return

    first = True
    for acc_row in accounts:
        text, markup = _render_meal_card(acc_row)
        if first:
            bot.edit_message_text(text, message.chat.id, wait_msg.message_id, reply_markup=markup)
            first = False
        else:
            bot.send_message(message.chat.id, text, reply_markup=markup)


@bot.message_handler(commands=["meal_add"])
def handle_meal_add_cmd(message: types.Message):
    user = resolve_user_context(message)
    if not user:
        bot.send_message(message.chat.id, "Сначала выполните вход: /start")
        return
    args = re.sub(r"^/meal_add(@[a-zA-Z0-9_]+)?\s*", "", message.text.strip(), flags=re.IGNORECASE).strip()
    if not args:
        bot.send_message(
            message.chat.id,
            "📍 Выберите район Минска для школы (КШП):",
            reply_markup=get_meal_districts_keyboard(),
        )
        return

    parts = args.split(maxsplit=1)
    acc = re.sub(r"\D", "", parts[0])
    if len(acc) < 6:
        bot.send_message(
            message.chat.id,
            "❌ Укажите корректный 10-значный номер ученика.\nПример: <code>/meal_add 1234567890 Иван</code>",
        )
        return
    label = parts[1].strip() if len(parts) > 1 else (user.get("active_pupil_name") or "Ученик")
    _save_and_show_meal_account(message.chat.id, user["tg_id"], acc, label, district="Фрунзенский район")


@bot.message_handler(commands=["check_alerts"])
def handle_check_alerts(message: types.Message):
    user = resolve_user_context(message)
    if not user:
        bot.send_message(message.chat.id, "Сначала выполните вход: /start")
        return

    clubs = db.get_child_clubs(user["tg_id"])
    if not clubs:
        bot.send_message(
            message.chat.id,
            "🔔 У вас пока нет привязанных счетов питания или кружков в <b>Dzetva</b>.\n"
            "Добавьте профиль ребёнка и счета в приложении (/app) или через /meal.",
        )
        return

    wait_msg = bot.send_message(message.chat.id, "⏳ Проверяю счета в ЕРИП и условия уведомлений...")
    today_str = date.today().isoformat()
    stats = check_and_send_erip_alerts_for_user(user, today_str, force_refresh=True)

    err_suffix = f" (ошибок: {stats['errors']})" if stats["errors"] else ""
    alert_breakdown = (
        f" (низкий баланс: {stats['low_alerts']}, кружки: {stats['due_alerts']})"
        if stats["alerts_sent"]
        else ""
    )
    lines = [
        "🔔 <b>Проверка уведомлений Dzetva завершена</b>\n",
        f"📋 Проверено счетов: <b>{stats['checked']}</b>",
        f"🔄 Обновлено в ЕРИП: <b>{stats['refreshed']}</b>{err_suffix}",
        f"📨 Отправлено уведомлений: <b>{stats['alerts_sent']}</b>{alert_breakdown}",
    ]
    if stats.get("details"):
        lines.append("\n<b>Состояние счетов:</b>")
        lines.extend(stats["details"])

    summary_text = "\n".join(lines)
    try:
        bot.edit_message_text(summary_text, message.chat.id, wait_msg.message_id)
    except Exception:
        bot.send_message(message.chat.id, summary_text)


@bot.callback_query_handler(func=lambda c: c.data == "meal_manage")
def callback_meal_manage(call: types.CallbackQuery):
    user = resolve_callback_user(call)
    if not user:
        bot.answer_callback_query(call.id, "Сначала выполните вход: /start")
        return
    accounts = db.get_meal_accounts(user["tg_id"])
    markup = types.InlineKeyboardMarkup()
    for a in accounts:
        lbl = f"🍽 {a['label']} ({a['account_number']})"
        markup.add(types.InlineKeyboardButton(lbl, callback_data=f"meal_refresh:{a['account_number']}"))
    markup.add(types.InlineKeyboardButton("➕ Добавить счёт ЕРИП", callback_data="meal_add_start"))
    msg_text = (
        "🍽 <b>Привязанные счета школьного питания:</b>\n\nНажмите на счёт, чтобы посмотреть баланс, или добавьте новый."
        if accounts
        else "🍽 У вас пока нет привязанных счетов питания.\nНажмите кнопку ниже, чтобы подключить!"
    )
    bot.edit_message_text(msg_text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "meal_add_start")
def callback_meal_add_start(call: types.CallbackQuery):
    user = resolve_callback_user(call)
    if not user and call.message and call.message.chat.type == "private":
        user = db.ensure_basic_user(call.from_user.id, call.from_user.first_name or "")
    if not user:
        bot.answer_callback_query(call.id, "Сначала выполните вход: /start")
        return
    bot.edit_message_text(
        "📍 Выберите район Минска для школы (КШП):",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=get_meal_districts_keyboard(),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("meal_dist:"))
def callback_meal_select_district(call: types.CallbackQuery):
    user = resolve_callback_user(call)
    if not user:
        bot.answer_callback_query(call.id, "Сначала выполните вход: /start")
        return
    try:
        idx = int(call.data.split(":")[1])
        district = DISTRICT_SHORT_KEYS[idx]
    except Exception:
        bot.answer_callback_query(call.id, "Неизвестный район")
        return

    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        f"📍 Район: <b>{district}</b>\n\n"
        f"Теперь отправьте <b>10-значный лицевой счёт ученика</b> и (по желанию) имя через пробел.\n"
        f"Например: <code>1234567890 Иван</code>",
    )
    bot.register_next_step_handler(msg, process_meal_account_step, user["tg_id"], district)


def process_meal_account_step(message: types.Message, owner_tg_id: int, district: str):
    raw = (message.text or "").strip()
    if not raw or raw.startswith("/"):
        return
    parts = raw.split(maxsplit=1)
    acc = re.sub(r"\D", "", parts[0])
    if len(acc) < 6:
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат номера счёта. Попробуйте через меню <b>🍽 Питание</b> или командой:\n"
            "<code>/meal_add 1234567890 Иван</code>",
        )
        return
    user = db.get_user(owner_tg_id)
    default_label = (user or {}).get("active_pupil_name") or "Ученик"
    label = parts[1].strip() if len(parts) > 1 else default_label
    _save_and_show_meal_account(message.chat.id, owner_tg_id, acc, label, district=district)


@bot.callback_query_handler(func=lambda c: c.data.startswith("meal_refresh:"))
def callback_meal_refresh(call: types.CallbackQuery):
    user = resolve_callback_user(call)
    if not user:
        bot.answer_callback_query(call.id, "Сначала выполните вход: /start")
        return
    acc = call.data.split(":", 1)[1]
    acc_row = db.get_meal_account(user["tg_id"], acc)
    if not acc_row:
        bot.answer_callback_query(call.id, "Счёт не найден")
        return
    bot.answer_callback_query(call.id, "Обновляю данные из ЕРИП...")
    text, markup = _render_meal_card(acc_row)
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("meal_thr_menu:"))
def callback_meal_thr_menu(call: types.CallbackQuery):
    acc = call.data.split(":", 1)[1]
    bot.edit_message_reply_markup(
        call.message.chat.id,
        call.message.message_id,
        reply_markup=get_meal_threshold_keyboard(acc),
    )
    bot.answer_callback_query(call.id, "Выберите порог напоминания")


@bot.callback_query_handler(func=lambda c: c.data.startswith("meal_set_thr:"))
def callback_meal_set_thr(call: types.CallbackQuery):
    user = resolve_callback_user(call)
    if not user:
        bot.answer_callback_query(call.id, "Сначала выполните вход: /start")
        return
    _, acc, thr_str = call.data.split(":", 2)
    thr = float(thr_str)
    db.update_meal_threshold(user["tg_id"], acc, thr)
    acc_row = db.get_meal_account(user["tg_id"], acc)
    if acc_row:
        text, markup = _render_meal_card(acc_row)
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        except Exception:
            pass
    bot.answer_callback_query(call.id, f"Порог обновлён: {int(thr)} BYN" if thr > 0 else "Напоминания выключены")


@bot.callback_query_handler(func=lambda c: c.data.startswith("meal_del:"))
def callback_meal_delete(call: types.CallbackQuery):
    user = resolve_callback_user(call)
    if not user:
        bot.answer_callback_query(call.id, "Сначала выполните вход: /start")
        return
    acc = call.data.split(":", 1)[1]
    db.delete_meal_account(user["tg_id"], acc)
    bot.edit_message_text(f"🗑 Счёт питания <code>{acc}</code> удалён.", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id, "Удалено")


@bot.callback_query_handler(func=lambda c: c.data.startswith("club_paid:"))
def callback_club_paid(call: types.CallbackQuery):
    user = resolve_callback_user(call)
    if not user:
        bot.answer_callback_query(call.id, "Сначала выполните вход: /start")
        return
    try:
        club_id = int(call.data.split(":", 1)[1])
    except ValueError:
        bot.answer_callback_query(call.id, "Ошибка ID")
        return
    club = db.confirm_club_payment(user["tg_id"], club_id, amount=None, date_str=date.today().isoformat())
    if not club:
        bot.answer_callback_query(call.id, "Кружок не найден")
        return
    amt = club.get("monthly_amount") or club.get("last_to_pay") or 0
    amt_str = f" ({float(amt):.2f} BYN)".replace(".", ",") if amt else ""
    try:
        bot.edit_message_text(
            f"✅ <b>Оплата подтверждена!</b>\n"
            f"📌 {club['org_name']} ({club['service_name']}){amt_str}\n"
            f"Записано в статистику расходов за {date.today().strftime('%m.%Y')}.",
            call.message.chat.id,
            call.message.message_id,
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Оплата зафиксирована!")




# ================= NOTIFIER BACKGROUND WORKER =================


def _build_club_due_trigger(
    now_date: date,
    month_key: str,
    due_day: Any,
) -> Tuple[Optional[str], str]:
    """Return (dedup_tag, human_when_text) for a club's payment schedule."""
    try:
        due_day_int = int(due_day)
    except (TypeError, ValueError):
        return None, ""
    if due_day_int <= 0:
        return None, ""

    max_d = calendar.monthrange(now_date.year, now_date.month)[1]
    due_dt = now_date.replace(day=min(due_day_int, max_d))
    days_left = (due_dt - now_date).days
    due_label = due_dt.strftime("%d.%m")

    if days_left == 0:
        return f"{month_key}:d0", "ПОСЛЕДНИЙ ДЕНЬ сегодня!"
    if days_left == 1:
        return f"{month_key}:d1", "остался 1 день (завтра срок)!"
    if days_left == 2:
        return f"{month_key}:d2", f"осталось 2 дня (до {due_label})"
    if days_left == 5:
        return f"{month_key}:d5", f"осталось 5 дней (до {due_label})"
    if days_left == -1:
        return f"{month_key}:overdue1", f"просрочка 1 день — срок был {due_label}"
    if now_date.day == 1:
        return f"{month_key}:start", f"начало месяца (срок до {due_label})"
    return None, ""


def check_and_send_erip_alerts_for_user(
    u: Dict[str, Any],
    today_str: str,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Check a user's child_clubs balances/deadlines and send deduplicated Dzetva alerts."""
    stats: Dict[str, Any] = {
        "checked": 0,
        "refreshed": 0,
        "errors": 0,
        "low_alerts": 0,
        "due_alerts": 0,
        "alerts_sent": 0,
        "details": [],
    }
    clubs = db.get_child_clubs(u["tg_id"])
    if not clubs:
        return stats

    now_date = date.fromisoformat(today_str)
    month_key = today_str[:7]
    children_map = {c["id"]: c["name"] for c in db.ensure_children_synced(u["tg_id"])}
    target_chats = db.get_all_target_chat_ids_for_user(u["tg_id"])

    for club in clubs:
        stats["checked"] += 1
        child_name = children_map.get(club["child_id"], "Ученик")
        safe_child = html.escape(str(child_name))
        org_name = str(club.get("org_name") or "Счёт")
        safe_org = html.escape(org_name)

        balance: Optional[float] = None
        to_pay: Optional[float] = club.get("last_to_pay")
        erip_error: Optional[str] = None

        if force_refresh or club.get("last_checked") != today_str:
            try:
                res = query_erip_service(
                    club["erip_code"],
                    club["account_number"],
                    club.get("di_type") or "9191",
                )
            except Exception as e:
                logger.warning(f"ERIP query error for club {club.get('id')}: {e}")
                res = {"ok": False, "error": str(e)}

            if res.get("ok"):
                db.update_club_erip_state(
                    tg_id=u["tg_id"],
                    club_id=club["id"],
                    fio=res.get("fio", ""),
                    balance=res.get("balance"),
                    to_pay=res.get("to_pay"),
                    snap_date=today_str,
                    saldo_start=res.get("saldo_start"),
                    paid_period=res.get("paid_period"),
                )
                balance = res.get("balance")
                to_pay = res.get("to_pay")
                stats["refreshed"] += 1
            else:
                erip_error = res.get("error") or "Ошибка ЕРИП"
                stats["errors"] += 1
        else:
            balance = club.get("last_balance")
            to_pay = club.get("last_to_pay")

        kind = club.get("kind")
        raw_thr = club.get("alert_threshold")
        thr = float(raw_thr) if raw_thr not in (None, "") else 5.0

        # Низкий баланс питания (столовая / буфет)
        if kind in ("canteen", "buffet") and balance is not None and thr > 0 and balance <= thr:
            low_tag = f"low:{today_str}"
            if club.get("last_reminded_tag") != low_tag:
                svc_title = "🥗 КШП Столовая" if kind == "canteen" else "🥐 КШП Буфет"
                bal_str = f"{float(balance):.2f}".replace(".", ",")
                thr_str = f"{thr:.2f}".replace(".", ",")
                short_code = re.sub(r"^1000", "", str(club.get("erip_code") or ""))
                safe_acc = html.escape(str(club.get("account_number") or ""))
                safe_code = html.escape(short_code)
                code_line = f"🔢 Код услуги ЕРИП: <code>{safe_code}</code>\n" if safe_code else ""
                alert = (
                    f"⚠️ <b>Dzetva: заканчиваются средства на питание!</b>\n\n"
                    f"👤 <b>{safe_child}</b>\n"
                    f"{svc_title}\n"
                    f"💳 Остаток: <b>{bal_str} BYN</b> (порог: {thr_str} BYN)\n"
                    f"📄 Лицевой счёт: <code>{safe_acc}</code>\n"
                    f"{code_line}\n"
                    f"<i>Пополните счёт в ЕРИП или откройте /meal для подробностей.</i>"
                )
                sent_count = 0
                for cid in target_chats:
                    try:
                        bot.send_message(cid, alert)
                        sent_count += 1
                    except Exception as e:
                        logger.warning(f"Failed to send low-balance alert to {cid}: {e}")
                db.mark_club_reminded(club["id"], low_tag)
                if sent_count > 0:
                    stats["low_alerts"] += 1
                    stats["alerts_sent"] += 1

        # Напоминания для кружков с датой оплаты
        due_day = club.get("due_day")
        if due_day and club.get("last_paid_month") != month_key:
            tag, when_txt = _build_club_due_trigger(now_date, month_key, due_day)
            if tag and db.get_club_alert_tag(club["id"]) != tag:
                amt = club.get("monthly_amount") or to_pay
                amt_txt = f" • Сумма: <b>{float(amt):.2f} BYN</b>".replace(".", ",") if amt else ""
                service_name = html.escape(str(club.get("service_name") or ""))
                svc_suffix = f" ({service_name})" if service_name else ""
                safe_acc = html.escape(str(club.get("account_number") or ""))
                alert = (
                    f"💳 <b>Напоминание об оплате кружка</b>\n\n"
                    f"👤 <b>{safe_child}</b>\n"
                    f"🤖 <b>{safe_org}</b>{svc_suffix}\n"
                    f"📄 Счёт/договор: <code>{safe_acc}</code>{amt_txt}\n"
                    f"⏰ Срок: <b>{when_txt}</b>"
                )
                kb = types.InlineKeyboardMarkup()
                btn_lbl = (
                    f"✅ Оплатил ({float(amt):.2f} BYN)".replace(".", ",")
                    if amt
                    else "✅ Оплатил в этом месяце"
                )
                kb.add(types.InlineKeyboardButton(btn_lbl, callback_data=f"club_paid:{club['id']}"))
                sent_count = 0
                for cid in target_chats:
                    try:
                        bot.send_message(cid, alert, reply_markup=kb)
                        sent_count += 1
                    except Exception as e:
                        logger.warning(f"Failed to send club payment alert to {cid}: {e}")
                db.mark_club_reminded(club["id"], tag)
                if sent_count > 0:
                    stats["due_alerts"] += 1
                    stats["alerts_sent"] += 1

        if erip_error:
            stats["details"].append(f"• {safe_child} — {safe_org}: ⚠️ <i>{html.escape(str(erip_error))}</i>")
        elif balance is not None:
            bal_s = f"{float(balance):.2f} BYN".replace(".", ",")
            mark = "⚠️ " if (kind in ("canteen", "buffet") and thr > 0 and balance <= thr) else ""
            stats["details"].append(f"• {safe_child} — {safe_org}: {mark}<b>{bal_s}</b>")
        elif club.get("last_paid_month") == month_key:
            stats["details"].append(f"• {safe_child} — {safe_org}: ✅ оплачено")
        elif to_pay is not None:
            tp_s = f"{float(to_pay):.2f} BYN".replace(".", ",")
            stats["details"].append(f"• {safe_child} — {safe_org}: к оплате <b>{tp_s}</b>")
        elif club.get("monthly_amount"):
            ma_s = f"{float(club['monthly_amount']):.2f} BYN".replace(".", ",")
            stats["details"].append(f"• {safe_child} — {safe_org}: абон. {ma_s}")
        else:
            stats["details"].append(f"• {safe_child} — {safe_org}: проверено")

    return stats


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
                    if not u.get("login") or not u.get("active_pupil_id"):
                        continue
                    if u.get("notify_new_grades", 1):
                        if not db.has_daily_reminder_been_sent(u["tg_id"], today_str):
                            client = get_client_for_user(u)
                            if not client:
                                continue
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
                    if not u.get("login") or not u.get("active_pupil_id"):
                        continue
                    if u.get("notify_new_grades", 1):
                        client = get_client_for_user(u)
                        if not client:
                            continue
                        pupil_id = u["active_pupil_id"]
                        subdomain = u["active_subdomain"]
                        name = u.get("active_pupil_name") or "Ученик"
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
                    if not u.get("login") or not u.get("active_pupil_id"):
                        continue
                    digest_key = f"digest_{today_str}"
                    if not db.has_daily_reminder_been_sent(u["tg_id"], digest_key):
                        client = get_client_for_user(u)
                        if not client:
                            continue
                        q_data = client.get_quarter_marks(u["active_pupil_id"], u["active_subdomain"])
                        avg = q_data.get("average", "—")
                        days_to_holidays = max((WINTER_HOLIDAYS_START - now).days, 0)
                        text = (
                            f"🎉 <b>Пятничный дайджест недели!</b>\n\n"
                            f"👤 <b>{u.get('active_pupil_name') or 'Ученик'}</b> отлично потрудилась на этой неделе!\n"
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

            # 5. Контроль баланса питания в 18:00 (Пн-Пт)
            if now.weekday() < 5 and now.hour == 18 and now.minute < 30:
                for u in users:
                    if not u.get("notify_new_grades", 1):
                        continue
                    for acc_row in db.get_meal_accounts(u["tg_id"]):
                        thr = float(acc_row.get("alert_threshold") or 0.0)
                        if thr <= 0:
                            continue
                        last_dt_str = acc_row.get("last_alert_date")
                        if last_dt_str:
                            try:
                                if (now.date() - date.fromisoformat(last_dt_str)).days < 2:
                                    continue
                            except ValueError:
                                pass
                        status = get_pupil_meal_status(
                            acc_row.get("district") or "Фрунзенский район",
                            acc_row["account_number"],
                        )
                        if not status.get("ok"):
                            continue
                        c_bal = status.get("canteen", {}).get("balance")
                        b_bal = status.get("buffet", {}).get("balance")
                        low_parts = []
                        if c_bal is not None and c_bal <= thr:
                            low_parts.append(f"🥗 Столовая: <b>{c_bal:.2f} BYN</b>".replace(".", ","))
                        if b_bal is not None and b_bal <= thr:
                            low_parts.append(f"🥐 Буфет: <b>{b_bal:.2f} BYN</b>".replace(".", ","))
                        if low_parts:
                            alert = (
                                f"⚠️ <b>Заканчиваются деньги на школьное питание!</b>\n\n"
                                f"👤 <b>{acc_row.get('label', 'Ученик')}</b> (счёт <code>{acc_row['account_number']}</code>)\n"
                                + "\n".join(low_parts)
                                + f"\n\nНажмите <b>🍽 Питание</b>, чтобы увидеть детали."
                            )
                            for cid in db.get_all_target_chat_ids_for_user(u["tg_id"]):
                                try:
                                    bot.send_message(cid, alert)
                                except Exception:
                                    pass
                            db.mark_meal_alert_sent(u["tg_id"], acc_row["account_number"], today_str)

            # 6. Синхронизация кружков/счетов и напоминания об оплате в 10:00 и 18:10
            if (now.hour == 10 and now.minute < 25) or (now.hour == 18 and 5 <= now.minute < 30):
                for u in users:
                    if not u.get("notify_new_grades", 1):
                        continue
                    try:
                        check_and_send_erip_alerts_for_user(u, today_str)
                    except Exception as e:
                        logger.error(f"Error checking ERIP alerts for user {u.get('tg_id')}: {e}")

        except Exception as e:
            logger.error(f"Error in background notifier loop: {e}")

        time.sleep(30)

def main():
    db.init_db()
    logger.info("Database initialized.")
    
    # Запуск фонового планировщика
    t = threading.Thread(target=notifier_loop, daemon=True)
    t.start()

    # Запуск PWA веб-приложения «Dzetva»
    w = threading.Thread(target=webapp_server.run_webapp_in_thread, kwargs={"host": "0.0.0.0", "port": webapp_server.DEFAULT_PORT}, daemon=True)
    w.start()

    existing_public_url = tunnel_manager.get_public_webapp_url()
    if existing_public_url.startswith("https://"):
        update_bot_webapp_menu_button(existing_public_url)

    if os.environ.get("DISABLE_TUNNEL") != "1" and (
        os.path.exists("/data/data/com.termux") or os.environ.get("ENABLE_PUBLIC_TUNNEL") == "1"
    ):
        tunnel_manager.start_tunnel_thread(
            local_port=webapp_server.DEFAULT_PORT,
            on_new_url=update_bot_webapp_menu_button,
        )

    logger.info("Bot starting polling on Xiaomi Mi Box...")
    while True:
        try:
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=30)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
