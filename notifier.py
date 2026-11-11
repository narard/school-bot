import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any

from aiogram import Bot
from database import (
    get_all_users, is_initial_marks_sync, is_mark_known,
    save_known_mark, has_reminder_been_sent_today, mark_reminder_sent,
    has_weather_sent_today, mark_weather_sent, get_all_target_chat_ids_for_user
)
from schools_client import SchoolsClient
from services.weather import format_weather_message

logger = logging.getLogger(__name__)

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

# Примерная дата зимних каникул (конец 2-й четверти)
WINTER_HOLIDAYS_START = datetime(2026, 12, 26)

def format_day_monospace(day_data: Dict[str, Any], header: str) -> str:
    lessons = day_data.get("lessons", [])
    if not lessons:
        return f"{header}\n<i>Уроков нет</i>"

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
    return f"{header}\n<pre>{table_content}</pre>"

async def check_grades_for_user(bot: Bot, user: Dict[str, Any], client: SchoolsClient):
    pupil_id = user.get("active_pupil_id")
    subdomain = user.get("active_subdomain")
    pupil_name = user.get("active_pupil_name", "Ученик")
    tg_id = user["tg_id"]

    if not pupil_id or not subdomain:
        return

    try:
        diary = await asyncio.to_thread(client.get_week_diary, pupil_id, subdomain)
        quarter_id = str(diary.get("quarter_id") or "curr")
        days = diary.get("days", [])

        is_initial = await is_initial_marks_sync(pupil_id, quarter_id)
        target_chats = await get_all_target_chat_ids_for_user(tg_id)

        for day in days:
            day_title = day.get("title", "")
            for les in day.get("lessons", []):
                mark = les.get("mark", "").strip()
                subj = les.get("subject", "").strip()

                if not mark or not subj:
                    continue

                if is_initial:
                    await save_known_mark(pupil_id, quarter_id, day_title, subj, mark)
                else:
                    known = await is_mark_known(pupil_id, quarter_id, day_title, subj, mark)
                    if not known:
                        await save_known_mark(pupil_id, quarter_id, day_title, subj, mark)
                        msg = (
                            f"🔔 <b>Новая оценка!</b>\n\n"
                            f"👤 <b>{pupil_name}</b>\n"
                            f"📚 Предмет: <b>{subj}</b>\n"
                            f"🎯 Отметка: <b>[{mark}]</b>\n"
                            f"📅 День: <i>{day_title}</i>"
                        )
                        for cid in target_chats:
                            try:
                                await bot.send_message(cid, msg, parse_mode="HTML")
                            except Exception:
                                pass
                        logger.info(f"Notification sent: mark {mark} in {subj} for {pupil_name}")

    except Exception as e:
        logger.error(f"Error checking grades for user {tg_id}: {e}")

async def send_1600_reminder(bot: Bot, user: Dict[str, Any], client: SchoolsClient, today_str: str):
    tg_id = user["tg_id"]
    if await has_reminder_been_sent_today(tg_id, today_str):
        return

    pupil_id = user.get("active_pupil_id")
    subdomain = user.get("active_subdomain")
    pupil_name = user.get("active_pupil_name", "Ученик")

    now = datetime.now()
    tomorrow = now + timedelta(days=1)
    weekday_ru = RUSSIAN_WEEKDAYS[tomorrow.weekday()]

    if now.weekday() in (4, 5): # В пятницу и субботу не напоминаем
        return

    try:
        diary = await asyncio.to_thread(client.get_week_diary, pupil_id, subdomain)
        target_day = None
        for d in diary.get("days", []):
            if weekday_ru in d.get("title", "").lower():
                target_day = d
                break

        if target_day:
            header = f"⏰ <b>Расписание и ДЗ на завтра (16:00) | {pupil_name}:</b>"
            text = format_day_monospace(target_day, header)
            target_chats = await get_all_target_chat_ids_for_user(tg_id)
            for cid in target_chats:
                try:
                    await bot.send_message(cid, text, parse_mode="HTML")
                except Exception:
                    pass
            await mark_reminder_sent(tg_id, today_str)
            logger.info(f"16:00 reminder sent for user {tg_id}")
    except Exception as e:
        logger.error(f"Error sending 16:00 reminder to {tg_id}: {e}")

async def send_0640_weather(bot: Bot, user: Dict[str, Any], today_str: str):
    tg_id = user["tg_id"]
    if await has_weather_sent_today(tg_id, today_str):
        return

    # В выходные утром не беспокоим (суббота, воскресенье)
    if datetime.now().weekday() in (5, 6):
        return

    text = format_weather_message(morning_alert=True)
    target_chats = await get_all_target_chat_ids_for_user(tg_id)
    for cid in target_chats:
        try:
            await bot.send_message(cid, text, parse_mode="HTML")
        except Exception:
            pass
    await mark_weather_sent(tg_id, today_str)
    logger.info(f"06:40 weather sent for user {tg_id}")

async def send_friday_digest(bot: Bot, user: Dict[str, Any], client: SchoolsClient, today_str: str):
    tg_id = user["tg_id"]
    pupil_id = user.get("active_pupil_id")
    subdomain = user.get("active_subdomain")
    pupil_name = user.get("active_pupil_name", "Ученик")

    now = datetime.now()
    days_to_holidays = max((WINTER_HOLIDAYS_START - now).days, 0)

    try:
        q_data = await asyncio.to_thread(client.get_quarter_marks, pupil_id, subdomain)
        avg = q_data.get("average", "—")

        text = (
            f"🎉 <b>Пятничный дайджест недели!</b>\n\n"
            f"👤 <b>{pupil_name}</b> отлично потрудилась на этой неделе!\n"
            f"📊 Текущий средний балл: <b>{avg}</b>\n"
            f"🏖 До зимних каникул осталось: <b>{days_to_holidays} дней</b>!\n\n"
            f"Желаем всей семье прекрасных и уютных выходных! 🌟"
        )
        target_chats = await get_all_target_chat_ids_for_user(tg_id)
        for cid in target_chats:
            try:
                await bot.send_message(cid, text, parse_mode="HTML")
            except Exception:
                pass
        await mark_reminder_sent(tg_id, f"friday_{today_str}")
    except Exception as e:
        logger.error(f"Error sending Friday digest: {e}")

async def notification_worker(bot: Bot, user_clients: Dict[int, SchoolsClient]):
    logger.info("Notification worker started.")
    last_grades_check = 0

    while True:
        try:
            users = await get_all_users()
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            # 1. Прогноз погоды в 06:40 (по будням)
            if now.hour == 6 and now.minute >= 40 and now.minute < 55:
                for u in users:
                    if u.get("notify_new_grades", 1):
                        await send_0640_weather(bot, u, today_str)

            # 2. Напоминание расписания в 16:00 (воскресенье-четверг)
            if now.hour == 16 and now.minute >= 0 and now.minute < 30:
                for u in users:
                    if u.get("notify_new_grades", 1):
                        tg_id = u["tg_id"]
                        if tg_id not in user_clients or not user_clients[tg_id].logged_in:
                            c = SchoolsClient()
                            if await asyncio.to_thread(c.login, u["login"], u["password"]):
                                user_clients[tg_id] = c
                        if tg_id in user_clients:
                            await send_1600_reminder(bot, u, user_clients[tg_id], today_str)

            # 3. Пятничный дайджест в 17:00
            if now.weekday() == 4 and now.hour == 17 and now.minute < 30:
                for u in users:
                    if not await has_reminder_been_sent_today(u["tg_id"], f"friday_{today_str}"):
                        tg_id = u["tg_id"]
                        if tg_id in user_clients:
                            await send_friday_digest(bot, u, user_clients[tg_id], today_str)

            # 4. Проверка новых оценок каждые 15 минут (08:00 - 20:00)
            now_ts = datetime.now().timestamp()
            if 8 <= now.hour <= 20 and (now_ts - last_grades_check >= 15 * 60):
                last_grades_check = now_ts
                for u in users:
                    if u.get("notify_new_grades", 1):
                        tg_id = u["tg_id"]
                        if tg_id not in user_clients or not user_clients[tg_id].logged_in:
                            c = SchoolsClient()
                            if await asyncio.to_thread(c.login, u["login"], u["password"]):
                                user_clients[tg_id] = c
                        if tg_id in user_clients:
                            await check_grades_for_user(bot, u, user_clients[tg_id])

        except Exception as e:
            logger.error(f"Error in notification worker loop: {e}", exc_info=True)

        await asyncio.sleep(40)
