import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from aiohttp import web
from database import (
    init_db, get_user, save_user, get_user_pupils,
    set_active_pupil, delete_user, toggle_notifications,
    bind_family_chat, get_family_chat_owner
)
from schools_client import SchoolsClient
from notifier import notification_worker
from keyboards import (
    get_main_keyboard, get_settings_keyboard, get_pupils_selection_keyboard,
    get_movie_categories_keyboard, get_movie_card_keyboard
)
from services.weather import format_weather_message
from services.currency import format_currency_message
from services.movies import get_random_movie
from services.ai_helper import ask_homework_tutor

BOT_TOKEN = "8637861449:AAHFx0rVKG7kGWj9nWTUeFF25fPXgTeMHkc"
LOG_FILE = os.path.join(os.path.dirname(__file__), "bot.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

user_clients: Dict[int, SchoolsClient] = {}

class LoginState(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()

BELLS = {
    "1": "08:30–09:15",
    "2": "09:25–10:10",
    "3": "10:30–11:15",
    "4": "11:35–12:20",
    "5": "12:35–13:20",
    "6": "13:35–14:20",
    "7": "14:30–15:15",
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

async def get_client_for_user(user: Dict[str, Any]) -> SchoolsClient:
    tg_id = user["tg_id"]
    if tg_id in user_clients and user_clients[tg_id].logged_in:
        return user_clients[tg_id]

    client = SchoolsClient()
    ok = await asyncio.to_thread(client.login, user["login"], user["password"])
    if ok:
        user_clients[tg_id] = client
    return client

async def resolve_user_context(message: types.Message) -> Optional[Dict[str, Any]]:
    """
    Возвращает профиль пользователя, даже если команда отправлена в групповом чате
    """
    if message.chat.type in ("group", "supergroup"):
        owner_id = await get_family_chat_owner(message.chat.id)
        if owner_id:
            return await get_user(owner_id)
        return None
    return await get_user(message.from_user.id)

@dp.message(Command("bind"))
async def cmd_bind_group(message: types.Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Команда /bind предназначена для добавления в семейный чат!")
        return

    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала войдите в боте в личных сообщениях через /start")
        return

    pupil_name = user.get("active_pupil_name", "Ученик")
    await bind_family_chat(message.chat.id, message.from_user.id, message.chat.title or "Семейный чат")

    text = (
        f"👨‍👩‍👧 <b>Семейный чат успешно привязан!</b>\n\n"
        f"Дневник: <b>{pupil_name}</b> (СШ №199)\n\n"
        f"<b>Что будет приходить в этот чат автоматически:</b>\n"
        f"• 🌤 В <b>06:40</b> — утренний прогноз погоды в Минске и совет по одежде\n"
        f"• ⏰ В <b>16:00</b> — расписание и всё домашнее задание на завтра\n"
        f"• 🔔 В течение дня — мгновенные уведомления о новых отметках\n\n"
        f"<b>Быстрые команды для любого члена семьи:</b>\n"
        f"/today — уроки на сегодня\n"
        f"/tomorrow — уроки и ДЗ на завтра\n"
        f"/week — вся неделя\n"
        f"/marks — четвертные оценки\n"
        f"/weather — погода прямо сейчас\n"
        f"/rates — курсы валют НБРБ\n"
        f"/film — кино на вечер (ужасы, новинки, семейное)\n"
        f"/ai &lt;вопрос&gt; — спросить AI-помощника по урокам"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()

    # Если /start нажат в группе
    if message.chat.type in ("group", "supergroup"):
        await cmd_bind_group(message)
        return

    user = await get_user(message.from_user.id)
    if user:
        pupils = await get_user_pupils(message.from_user.id)
        has_multi = len(pupils) > 1
        name = user.get("active_pupil_name", "Ученик")
        await message.answer(
            f"👋 С возвращением!\n"
            f"👤 Текущий дневник: <b>{name}</b>\n\n"
            f"Выберите нужный раздел в меню ниже 👇",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(has_multi)
        )
    else:
        await message.answer(
            "👋 <b>Добро пожаловать в Школьный Дневник (Schools.by)!</b>\n\n"
            "Здесь вы можете без рекламы и лишних входов смотреть:\n"
            "• Расписание и домашнее задание на каждый день со временем\n"
            "• Полученные оценки и четвертную успеваемость\n"
            "• Погоду в Минске, курсы валют и кино на вечер\n\n"
            "🔐 Для начала введите ваш <b>логин</b> от schools.by (email или имя пользователя):",
            parse_mode="HTML"
        )
        await state.set_state(LoginState.waiting_for_login)

@dp.message(LoginState.waiting_for_login)
async def process_login(message: types.Message, state: FSMContext):
    login = message.text.strip()
    await state.update_data(login=login)
    await message.answer("🔑 Теперь введите ваш <b>пароль</b> от schools.by:", parse_mode="HTML")
    await state.set_state(LoginState.waiting_for_password)

@dp.message(LoginState.waiting_for_password)
async def process_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    login = data.get("login")
    await state.clear()

    status_msg = await message.answer("🔄 Проверяю авторизацию на schools.by...")

    client = SchoolsClient()
    try:
        success = await asyncio.to_thread(client.login, login, password)
    except Exception as e:
        logger.error(f"Login error: {e}", exc_info=True)
        success = False

    if not success or not client.pupils:
        await status_msg.edit_text(
            "❌ <b>Не удалось войти на schools.by.</b>\n"
            "Пожалуйста, проверьте логин и пароль и попробуйте снова: /start",
            parse_mode="HTML"
        )
        return

    active_pupil = client.pupils[0]
    await save_user(message.from_user.id, login, password, active_pupil, client.pupils)
    user_clients[message.from_user.id] = client

    pupils_count = len(client.pupils)
    has_multi = pupils_count > 1

    await status_msg.delete()
    await message.answer(
        f"✅ <b>Вход успешно выполнен!</b>\n\n"
        f"👤 Ученик: <b>{active_pupil['name']}</b>\n"
        f"🏫 Школа: {active_pupil['subdomain']}.schools.by\n\n"
        f"В меню ниже выберите интересующий раздел 👇",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(has_multi)
    )

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

@dp.message(F.text == "📅 Сегодня")
@dp.message(Command("today"))
async def handle_today(message: types.Message):
    user = await resolve_user_context(message)
    if not user:
        await message.answer("Сначала выполните вход: /start (или добавьте бота в семейную группу командой /bind)")
        return

    wait_msg = await message.answer("⏳ Загружаю дневник...")
    try:
        client = await get_client_for_user(user)
        pupil_id = user["active_pupil_id"]
        subdomain = user["active_subdomain"]
        diary = await asyncio.to_thread(client.get_week_diary, pupil_id, subdomain)

        now = datetime.now()
        weekday_ru = RUSSIAN_WEEKDAYS[now.weekday()]

        target_day = None
        for d in diary.get("days", []):
            if weekday_ru in d.get("title", "").lower():
                target_day = d
                break

        await wait_msg.delete()
        if target_day:
            text = format_day_message(target_day, header_prefix="📅 Расписание на Сегодня:")
            await message.answer(text, parse_mode="HTML")
        else:
            if now.weekday() in (5, 6):
                await message.answer("🎉 Сегодня выходной! Уроков нет.", parse_mode="HTML")
            else:
                await message.answer(f"На сегодня ({weekday_ru.capitalize()}) уроки в дневнике не найдены.")
    except Exception as e:
        logger.error(f"Error fetching today: {e}", exc_info=True)
        await wait_msg.edit_text("❌ Произошла ошибка при загрузке расписания.")

@dp.message(F.text == "📆 Завтра")
@dp.message(Command("tomorrow"))
async def handle_tomorrow(message: types.Message):
    user = await resolve_user_context(message)
    if not user:
        await message.answer("Сначала выполните вход: /start")
        return

    wait_msg = await message.answer("⏳ Загружаю дневник...")
    try:
        client = await get_client_for_user(user)
        pupil_id = user["active_pupil_id"]
        subdomain = user["active_subdomain"]

        now = datetime.now()
        tomorrow = now + timedelta(days=1)
        weekday_ru = RUSSIAN_WEEKDAYS[tomorrow.weekday()]

        is_next_week = False
        if tomorrow.weekday() == 6: # Воскресенье -> смотрим понедельник
            monday_next = now + timedelta(days=2)
            weekday_ru = "понедельник"
            diary = await asyncio.to_thread(client.get_week_diary, pupil_id, subdomain, week_date=monday_next.strftime("%Y-%m-%d"))
            is_next_week = True
        else:
            diary = await asyncio.to_thread(client.get_week_diary, pupil_id, subdomain)

        target_day = None
        for d in diary.get("days", []):
            if weekday_ru in d.get("title", "").lower():
                target_day = d
                break

        await wait_msg.delete()
        if target_day:
            prefix = "📆 Расписание на Понедельник:" if is_next_week else "📆 Расписание на Завтра:"
            text = format_day_message(target_day, header_prefix=prefix)
            await message.answer(text, parse_mode="HTML")
        else:
            if tomorrow.weekday() == 5:
                await message.answer("🎉 Завтра суббота! Уроков нет.", parse_mode="HTML")
            else:
                await message.answer(f"На завтра ({weekday_ru.capitalize()}) уроки в дневнике не найдены.")
    except Exception as e:
        logger.error(f"Error fetching tomorrow: {e}", exc_info=True)
        await wait_msg.edit_text("❌ Произошла ошибка при загрузке расписания.")

@dp.message(F.text == "🗓 Вся неделя")
@dp.message(Command("week"))
async def handle_full_week(message: types.Message):
    user = await resolve_user_context(message)
    if not user:
        await message.answer("Сначала выполните вход: /start")
        return

    wait_msg = await message.answer("⏳ Загружаю расписание на всю неделю...")
    try:
        client = await get_client_for_user(user)
        pupil_id = user["active_pupil_id"]
        subdomain = user["active_subdomain"]
        diary = await asyncio.to_thread(client.get_week_diary, pupil_id, subdomain)

        days = diary.get("days", [])
        await wait_msg.delete()

        if not days:
            await message.answer("Расписание на эту неделю пусто.")
            return

        period = diary.get("period", "")
        header = f"🗓 <b>Расписание на неделю ({period}):</b>\n\n"
        day_blocks = [format_day_message(d) for d in days]
        full_text = header + "\n\n━━━━━━━━━━━━━━━\n\n".join(day_blocks)

        if len(full_text) > 4000:
            parts = [header]
            curr = ""
            for block in day_blocks:
                if len(curr) + len(block) > 3500:
                    parts.append(curr)
                    curr = block + "\n\n━━━━━━━━━━━━━━━\n\n"
                else:
                    curr += block + "\n\n━━━━━━━━━━━━━━━\n\n"
            if curr:
                parts.append(curr)
            for p in parts:
                await message.answer(p, parse_mode="HTML")
        else:
            await message.answer(full_text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error fetching week: {e}", exc_info=True)
        await wait_msg.edit_text("❌ Ошибка при загрузке недели.")

@dp.message(F.text == "📊 Четвертные оценки")
@dp.message(Command("marks"))
async def handle_quarter_marks(message: types.Message):
    user = await resolve_user_context(message)
    if not user:
        await message.answer("Сначала выполните вход: /start")
        return

    wait_msg = await message.answer("⏳ Загружаю четвертные оценки...")
    try:
        client = await get_client_for_user(user)
        pupil_id = user["active_pupil_id"]
        subdomain = user["active_subdomain"]
        q_data = await asyncio.to_thread(client.get_quarter_marks, pupil_id, subdomain)

        subjects = q_data.get("subjects", [])
        avg = q_data.get("average", "")
        pupil_name = user.get("active_pupil_name", "Ученик")

        await wait_msg.delete()
        if not subjects:
            await message.answer("Четвертные оценки пока не выставлены.")
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
            lines.append(f"\n⭐️ <b>Средний балл (I четверть): {avg}</b>")

        await message.answer("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error fetching quarters: {e}", exc_info=True)
        await wait_msg.edit_text("❌ Ошибка при загрузке четвертных оценок.")

@dp.message(F.text == "🌤 Погода")
@dp.message(Command("weather"))
async def handle_weather(message: types.Message):
    text = format_weather_message(morning_alert=False)
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "💱 Курсы валют")
@dp.message(Command("rates"))
async def handle_currency(message: types.Message):
    text = format_currency_message()
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "🍿 Что посмотреть")
@dp.message(Command("film"))
async def handle_movie_menu(message: types.Message):
    text = (
        "🍿 <b>Что посмотреть вечером?</b>\n\n"
        "Выберите желаемый жанр. Бот подберет фильм с высоким рейтингом "
        "и кнопкой для мгновенного просмотра на <b>rezka-ua.tv</b>:"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_movie_categories_keyboard())

@dp.callback_query(F.data == "movie_genres")
async def callback_movie_genres(call: types.CallbackQuery):
    text = (
        "🍿 <b>Что посмотреть вечером?</b>\n\n"
        "Выберите желаемый жанр:"
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=get_movie_categories_keyboard())

@dp.callback_query(F.data.startswith("movie:"))
async def callback_movie_pick(call: types.CallbackQuery):
    category = call.data.split(":")[1]
    movie = get_random_movie(category)

    text = (
        f"🎬 <b>«{movie['title']}»</b> ({movie['orig_title']})\n\n"
        f"📅 Год: <b>{movie['year']}</b>\n"
        f"⭐️ Рейтинг: <b>{movie['rating']}</b>\n"
        f"🎭 Жанр: <i>{movie['genre']}</i>\n\n"
        f"📖 <b>Сюжет:</b>\n{movie['desc']}\n\n"
        f"👇 Нажмите кнопку ниже, чтобы смотреть в HD на <b>rezka-ua.tv</b>:"
    )
    await call.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=get_movie_card_keyboard(movie["watch_url"], category)
    )

@dp.message(Command("ai"))
async def handle_ai_command(message: types.Message):
    query = message.text.replace("/ai", "").strip()
    if not query:
        await message.answer(
            "💡 <b>Как пользоваться AI-помощником:</b>\n\n"
            "Напишите: <code>/ai Объясни, как найти площадь треугольника</code>\n"
            "Или отправьте фото задания с подписью <code>/ai реши эту задачу</code>",
            parse_mode="HTML"
        )
        return

    wait_msg = await message.answer("🧠 Думаю над ответом для 4 класса...")
    answer = await ask_homework_tutor(query)
    await wait_msg.edit_text(answer, parse_mode="HTML")

@dp.message(F.photo)
async def handle_photo_task(message: types.Message):
    caption = message.caption or "Объясни и помоги решить это задание"
    if "/ai" in caption or message.chat.type == "private":
        clean_prompt = caption.replace("/ai", "").strip() or "Объясни это задание для 4 класса"
        wait_msg = await message.answer("🔍 Распознаю задание по фото...")
        try:
            # Скачиваем самое качественное фото
            photo = message.photo[-1]
            file_io = await bot.download(photo)
            image_bytes = file_io.read()
            answer = await ask_homework_tutor(clean_prompt, image_bytes)
            await wait_msg.edit_text(answer, parse_mode="HTML")
        except Exception as e:
            await wait_msg.edit_text(f"❌ Не удалось обработать фото: {e}")

@dp.message(F.text == "⚙️ Настройки")
async def handle_settings(message: types.Message):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала выполните вход: /start")
        return

    pupils = await get_user_pupils(message.from_user.id)
    has_multi = len(pupils) > 1
    notify_on = bool(user.get("notify_new_grades", 1))

    text = (
        f"⚙️ <b>Настройки профиля</b>\n\n"
        f"👤 Активный ученик: <b>{user.get('active_pupil_name')}</b>\n"
        f"📧 Логин: <code>{user.get('login')}</code>\n"
        f"🏫 Школа: {user.get('active_subdomain')}.schools.by\n\n"
        f"Нажмите кнопку ниже для настройки:"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_settings_keyboard(has_multi, notify_on))

@dp.callback_query(F.data == "logout")
async def callback_logout(call: types.CallbackQuery):
    tg_id = call.from_user.id
    if tg_id in user_clients:
        del user_clients[tg_id]
    await delete_user(tg_id)
    await call.message.edit_text("🚪 Вы успешно вышли из аккаунта.\nЧтобы войти снова, нажмите /start")

@dp.callback_query(F.data == "switch_pupil")
async def callback_switch_pupil(call: types.CallbackQuery):
    pupils = await get_user_pupils(call.from_user.id)
    if not pupils:
        await call.answer("Дети не найдены")
        return
    await call.message.edit_text(
        "Выберите ученика из списка:",
        reply_markup=get_pupils_selection_keyboard(pupils)
    )

@dp.callback_query(F.data.startswith("sel_pupil:"))
async def callback_select_pupil(call: types.CallbackQuery):
    pupil_id = call.data.split(":")[1]
    pupils = await get_user_pupils(call.from_user.id)
    target = next((p for p in pupils if p["pupil_id"] == pupil_id), None)
    if target:
        await set_active_pupil(call.from_user.id, target["pupil_id"], target["name"], target["subdomain"])
        await call.message.edit_text(f"✅ Активный дневник переключен на: <b>{target['name']}</b>", parse_mode="HTML")
    else:
        await call.answer("Ученик не найден")

@dp.callback_query(F.data == "toggle_notify")
async def callback_toggle_notify(call: types.CallbackQuery):
    is_on = await toggle_notifications(call.from_user.id)
    pupils = await get_user_pupils(call.from_user.id)
    has_multi = len(pupils) > 1
    await call.message.edit_reply_markup(reply_markup=get_settings_keyboard(has_multi, is_on))
    status_text = "включены" if is_on else "отключены"
    await call.answer(f"Уведомления {status_text}!")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_KEYS = [k.strip() for k in [os.environ.get("GEMINI_API_KEY", ""), os.environ.get("GEMINI_API_KEY_BACKUP", "")] if k.strip()]

async def handle_ping(request):
    return web.Response(text="SchoolsByBot is running!")

async def handle_models(request):
    def get_m():
        import requests
        return requests.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_KEY}", timeout=15)
    r = await asyncio.to_thread(get_m)
    return web.Response(text=r.text, content_type="application/json")

async def handle_gemini_proxy(request):
    try:
        data = await request.json()
        question = data.get("question", "")
        image_b64 = data.get("image_b64")

        prompt_prefix = (
            "Ты дружелюбный, умный и всесторонний AI-помощник для всей семьи (живёшь в домашнем Telegram-боте семьи в Минске).\n"
            "Правила:\n"
            "1. Если вопрос школьный (для дочки 4 класса, 9-10 лет) — отвечай как добрый репетитор: понятно, пошагово, с наглядными примерами, без заумных терминов.\n"
            "2. Если вопрос взрослый или общий (быт, кулинария, ремонт, переводы, наука, советы) — отвечай развёрнуто, точно и практично как умный эксперт.\n\n"
        )
        parts = [{"text": f"{prompt_prefix}Вопрос: {question}"}]
        if image_b64:
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": image_b64
                }
            })

        models_to_try = [
            "gemini-flash-lite-latest",
            "gemini-2.5-flash-lite",
            "gemini-flash-latest",
            "gemini-2.5-flash",
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.8-flash"
        ]

        errors = {}
        keys = GEMINI_KEYS if GEMINI_KEYS else [GEMINI_KEY]
        for key in keys:
            for m in models_to_try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}"
                def call_g(u=url):
                    import requests
                    return requests.post(u, json={"contents": [{"parts": parts}]}, timeout=12)

                try:
                    resp = await asyncio.to_thread(call_g)
                    if resp.status_code == 200:
                        rj = resp.json()
                        candidates = rj.get("candidates", [])
                        if candidates:
                            ans = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                            return web.json_response({"ok": True, "answer": ans, "model": m})
                    errors[f"{key[-6:]}:{m}"] = f"{resp.status_code}: {resp.text}"
                except Exception as e:
                    errors[f"{key[-6:]}:{m}"] = f"EXC: {type(e).__name__}: {e}"

        return web.json_response({"ok": False, "errors": errors}, status=503)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

async def handle_probe(request):
    import time
    t0 = time.time()
    try:
        def test_conn():
            import requests
            r = requests.get(
                "https://schools.by/login",
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            return r.status_code, len(r.text)
        code, sz = await asyncio.to_thread(test_conn)
        return web.Response(text=f"CONNECTED: status={code}, size={sz}, time={time.time()-t0:.2f}s")
    except Exception as e:
        return web.Response(text=f"FAILED: {type(e).__name__}: {e}, time={time.time()-t0:.2f}s", status=500)

async def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_get("/probe", handle_probe)
    app.router.add_get("/api/models", handle_models)
    app.router.add_post("/api/gemini", handle_gemini_proxy)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Healthcheck/API web server started on port {port}")

async def main():
    await init_db()
    logger.info("Database initialized.")
    try:
        await start_web_server()
    except Exception as e:
        logger.warning(f"Web server not started: {e}")

    # Если мы в облаке Render — работаем только как облачный AI-прокси
    if os.environ.get("RENDER"):
        logger.info("Render detected: running in AI Cloud Proxy mode (no Telegram polling)")
        while True:
            await asyncio.sleep(3600)

    asyncio.create_task(notification_worker(bot, user_clients))
    logger.info("Bot starting polling...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
