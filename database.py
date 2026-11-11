import aiosqlite
import os
import base64
from cryptography.fernet import Fernet
from typing import Optional, Dict, Any, List

DB_PATH = os.path.join(os.path.dirname(__file__), "schools_bot.db")
SECRET_KEY_PATH = os.path.join(os.path.dirname(__file__), ".secret.key")

def get_or_create_cipher():
    if not os.path.exists(SECRET_KEY_PATH):
        key = Fernet.generate_key()
        with open(SECRET_KEY_PATH, "wb") as f:
            f.write(key)
    else:
        with open(SECRET_KEY_PATH, "rb") as f:
            key = f.read().strip()
    return Fernet(key)

CIPHER = get_or_create_cipher()

def encrypt_val(val: str) -> str:
    return CIPHER.encrypt(val.encode("utf-8")).decode("utf-8")

def decrypt_val(val: str) -> str:
    return CIPHER.decrypt(val.encode("utf-8")).decode("utf-8")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                login TEXT NOT NULL,
                password_enc TEXT NOT NULL,
                active_pupil_id TEXT,
                active_pupil_name TEXT,
                active_subdomain TEXT,
                notify_new_grades INTEGER DEFAULT 1,
                notify_time TEXT DEFAULT '16:00',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_pupils (
                tg_id INTEGER,
                pupil_id TEXT,
                name TEXT,
                subdomain TEXT,
                PRIMARY KEY (tg_id, pupil_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS known_marks (
                pupil_id TEXT,
                quarter_id TEXT,
                day_title TEXT,
                lesson_name TEXT,
                mark TEXT,
                PRIMARY KEY (pupil_id, quarter_id, day_title, lesson_name, mark)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_reminders (
                tg_id INTEGER,
                remind_date TEXT,
                PRIMARY KEY (tg_id, remind_date)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS weather_reminders (
                tg_id INTEGER,
                remind_date TEXT,
                PRIMARY KEY (tg_id, remind_date)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS family_chats (
                chat_id INTEGER PRIMARY KEY,
                bind_tg_user_id INTEGER,
                chat_title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def get_user(tg_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            data = dict(row)
            data["password"] = decrypt_val(data["password_enc"])
            return data

async def get_all_users() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as cursor:
            rows = await cursor.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                try:
                    d["password"] = decrypt_val(d["password_enc"])
                    result.append(d)
                except Exception:
                    pass
            return result

async def save_user(tg_id: int, login: str, password: str, active_pupil: Dict[str, Any], all_pupils: List[Dict[str, Any]]):
    enc_pass = encrypt_val(password)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (tg_id, login, password_enc, active_pupil_id, active_pupil_name, active_subdomain, notify_time)
            VALUES (?, ?, ?, ?, ?, ?, '16:00')
            ON CONFLICT(tg_id) DO UPDATE SET
                login=excluded.login,
                password_enc=excluded.password_enc,
                active_pupil_id=excluded.active_pupil_id,
                active_pupil_name=excluded.active_pupil_name,
                active_subdomain=excluded.active_subdomain
        """, (tg_id, login, enc_pass, active_pupil.get("id"), active_pupil.get("name"), active_pupil.get("subdomain")))

        for p in all_pupils:
            await db.execute("""
                INSERT OR REPLACE INTO user_pupils (tg_id, pupil_id, name, subdomain)
                VALUES (?, ?, ?, ?)
            """, (tg_id, p.get("id"), p.get("name"), p.get("subdomain")))
        await db.commit()

async def get_user_pupils(tg_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM user_pupils WHERE tg_id = ?", (tg_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

async def set_active_pupil(tg_id: int, pupil_id: str, pupil_name: str, subdomain: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users SET active_pupil_id = ?, active_pupil_name = ?, active_subdomain = ?
            WHERE tg_id = ?
        """, (pupil_id, pupil_name, subdomain, tg_id))
        await db.commit()

async def toggle_notifications(tg_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT notify_new_grades FROM users WHERE tg_id = ?", (tg_id,)) as cursor:
            row = await cursor.fetchone()
            current = row[0] if row else 1
            new_val = 0 if current else 1

        await db.execute("UPDATE users SET notify_new_grades = ? WHERE tg_id = ?", (new_val, tg_id))
        await db.commit()
        return bool(new_val)

async def is_initial_marks_sync(pupil_id: str, quarter_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM known_marks WHERE pupil_id = ? AND quarter_id = ?", (pupil_id, quarter_id)) as cursor:
            row = await cursor.fetchone()
            return (row[0] == 0) if row else True

async def is_mark_known(pupil_id: str, quarter_id: str, day_title: str, lesson_name: str, mark: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT 1 FROM known_marks
            WHERE pupil_id = ? AND quarter_id = ? AND day_title = ? AND lesson_name = ? AND mark = ?
        """, (pupil_id, quarter_id, day_title, lesson_name, mark)) as cursor:
            row = await cursor.fetchone()
            return row is not None

async def save_known_mark(pupil_id: str, quarter_id: str, day_title: str, lesson_name: str, mark: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO known_marks (pupil_id, quarter_id, day_title, lesson_name, mark)
            VALUES (?, ?, ?, ?, ?)
        """, (pupil_id, quarter_id, day_title, lesson_name, mark))
        await db.commit()

async def has_reminder_been_sent_today(tg_id: int, date_str: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM daily_reminders WHERE tg_id = ? AND remind_date = ?", (tg_id, date_str)) as cursor:
            row = await cursor.fetchone()
            return row is not None

async def mark_reminder_sent(tg_id: int, date_str: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO daily_reminders (tg_id, remind_date) VALUES (?, ?)", (tg_id, date_str))
        await db.commit()

async def has_weather_sent_today(tg_id: int, date_str: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM weather_reminders WHERE tg_id = ? AND remind_date = ?", (tg_id, date_str)) as cursor:
            row = await cursor.fetchone()
            return row is not None

async def mark_weather_sent(tg_id: int, date_str: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO weather_reminders (tg_id, remind_date) VALUES (?, ?)", (tg_id, date_str))
        await db.commit()

async def bind_family_chat(chat_id: int, user_tg_id: int, chat_title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO family_chats (chat_id, bind_tg_user_id, chat_title)
            VALUES (?, ?, ?)
        """, (chat_id, user_tg_id, chat_title))
        await db.commit()

async def get_family_chat_owner(chat_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT bind_tg_user_id FROM family_chats WHERE chat_id = ?", (chat_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def get_all_target_chat_ids_for_user(user_tg_id: int) -> List[int]:
    """
    Возвращает список ID чатов, куда слать уведомления (личка + привязанные семейные группы)
    """
    chats = [user_tg_id]
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id FROM family_chats WHERE bind_tg_user_id = ?", (user_tg_id,)) as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                chats.append(r[0])
    return chats

async def delete_user(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
        await db.execute("DELETE FROM user_pupils WHERE tg_id = ?", (tg_id,))
        await db.execute("DELETE FROM family_chats WHERE bind_tg_user_id = ?", (tg_id,))
        await db.commit()
