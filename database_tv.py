import sqlite3
import os
import base64
from typing import Optional, Dict, Any, List

DB_PATH = os.path.join(os.path.dirname(__file__), "schools_bot.db")
KEY_PATH = os.path.join(os.path.dirname(__file__), ".secret.key")

def _get_key() -> bytes:
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as f:
            return f.read().strip()
    return b"sc199_secret_key_minsk_belarus_2026"

def _xor_cipher(data: str) -> str:
    key = _get_key()
    data_bytes = data.encode("utf-8")
    xor_bytes = bytes([b ^ key[i % len(key)] for i, b in enumerate(data_bytes)])
    return "XOR:" + base64.b64encode(xor_bytes).decode("ascii")

def _decrypt_pass(enc_data: str) -> str:
    if enc_data.startswith("XOR:"):
        try:
            raw = enc_data[4:]
            key = _get_key()
            xor_bytes = base64.b64decode(raw.encode("ascii"))
            data_bytes = bytes([b ^ key[i % len(key)] for i, b in enumerate(xor_bytes)])
            return data_bytes.decode("utf-8")
        except Exception:
            return enc_data
    return "Cofe911."

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
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
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_pupils (
            tg_id INTEGER,
            pupil_id TEXT,
            name TEXT,
            subdomain TEXT,
            PRIMARY KEY (tg_id, pupil_id)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS known_marks (
            pupil_id TEXT,
            quarter_id TEXT,
            day_title TEXT,
            lesson_name TEXT,
            mark TEXT,
            PRIMARY KEY (pupil_id, quarter_id, day_title, lesson_name, mark)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_reminders (
            tg_id INTEGER,
            remind_date TEXT,
            PRIMARY KEY (tg_id, remind_date)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS weather_reminders (
            tg_id INTEGER,
            remind_date TEXT,
            PRIMARY KEY (tg_id, remind_date)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS family_chats (
            chat_id INTEGER PRIMARY KEY,
            bind_tg_user_id INTEGER,
            chat_title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS meal_accounts (
            tg_id INTEGER NOT NULL,
            account_number TEXT NOT NULL,
            label TEXT DEFAULT 'Ученик',
            district TEXT DEFAULT 'Фрунзенский район',
            alert_threshold REAL DEFAULT 5.0,
            last_alert_date TEXT,
            PRIMARY KEY (tg_id, account_number)
        );
        """)
        conn.commit()

def save_user(tg_id: int, login: str, password: str, active_pupil: Dict[str, Any], pupils: List[Dict[str, Any]]):
    enc_password = _xor_cipher(password)
    with get_db() as conn:
        conn.execute("""
        INSERT INTO users (tg_id, login, password_enc, active_pupil_id, active_pupil_name, active_subdomain, notify_time)
        VALUES (?, ?, ?, ?, ?, ?, '16:00')
        ON CONFLICT(tg_id) DO UPDATE SET
            login=excluded.login,
            password_enc=excluded.password_enc,
            active_pupil_id=excluded.active_pupil_id,
            active_pupil_name=excluded.active_pupil_name,
            active_subdomain=excluded.active_subdomain;
        """, (
            tg_id,
            login,
            enc_password,
            active_pupil.get("id"),
            active_pupil.get("name"),
            active_pupil.get("subdomain")
        ))
        conn.execute("DELETE FROM user_pupils WHERE tg_id = ?", (tg_id,))
        for p in pupils:
            conn.execute("""
            INSERT OR REPLACE INTO user_pupils (tg_id, pupil_id, name, subdomain)
            VALUES (?, ?, ?, ?)
            """, (tg_id, p.get("id"), p.get("name"), p.get("subdomain")))
        conn.commit()

def get_user(tg_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["password"] = _decrypt_pass(d["password_enc"])
        return d

def get_all_users() -> List[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM users")
        rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["password"] = _decrypt_pass(d["password_enc"])
            result.append(d)
        return result

def get_user_pupils(tg_id: int) -> List[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM user_pupils WHERE tg_id = ?", (tg_id,))
        return [dict(r) for r in cur.fetchall()]

def set_active_pupil(tg_id: int, pupil_id: str, name: str, subdomain: str):
    with get_db() as conn:
        conn.execute("""
        UPDATE users SET active_pupil_id = ?, active_pupil_name = ?, active_subdomain = ?
        WHERE tg_id = ?
        """, (pupil_id, name, subdomain, tg_id))
        conn.commit()

def bind_family_chat(chat_id: int, owner_tg_id: int, title: str):
    with get_db() as conn:
        conn.execute("""
        INSERT INTO family_chats (chat_id, bind_tg_user_id, chat_title)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            bind_tg_user_id=excluded.bind_tg_user_id,
            chat_title=excluded.chat_title;
        """, (chat_id, owner_tg_id, title))
        conn.commit()

def get_family_chat_owner(chat_id: int) -> Optional[int]:
    with get_db() as conn:
        cur = conn.execute("SELECT bind_tg_user_id FROM family_chats WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        return row[0] if row else None

def get_all_target_chat_ids_for_user(user_tg_id: int) -> List[int]:
    chats = [user_tg_id]
    with get_db() as conn:
        cur = conn.execute("SELECT chat_id FROM family_chats WHERE bind_tg_user_id = ?", (user_tg_id,))
        for r in cur.fetchall():
            if r[0] not in chats:
                chats.append(r[0])
    return chats

def is_initial_marks_sync(pupil_id: str, quarter_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM known_marks WHERE pupil_id = ? AND quarter_id = ?", (pupil_id, quarter_id))
        return cur.fetchone()[0] == 0

def is_mark_known(pupil_id: str, quarter_id: str, day_title: str, lesson_name: str, mark: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("""
        SELECT COUNT(*) FROM known_marks 
        WHERE pupil_id = ? AND quarter_id = ? AND day_title = ? AND lesson_name = ? AND mark = ?
        """, (pupil_id, quarter_id, day_title, lesson_name, mark))
        return cur.fetchone()[0] > 0

def save_known_mark(pupil_id: str, quarter_id: str, day_title: str, lesson_name: str, mark: str):
    with get_db() as conn:
        conn.execute("""
        INSERT OR IGNORE INTO known_marks (pupil_id, quarter_id, day_title, lesson_name, mark)
        VALUES (?, ?, ?, ?, ?)
        """, (pupil_id, quarter_id, day_title, lesson_name, mark))
        conn.commit()

def has_daily_reminder_been_sent(tg_id: int, date_str: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM daily_reminders WHERE tg_id = ? AND remind_date = ?", (tg_id, date_str))
        return cur.fetchone()[0] > 0

def mark_daily_reminder_sent(tg_id: int, date_str: str):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO daily_reminders (tg_id, remind_date) VALUES (?, ?)", (tg_id, date_str))
        conn.commit()

def has_weather_reminder_been_sent(tg_id: int, date_str: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM weather_reminders WHERE tg_id = ? AND remind_date = ?", (tg_id, date_str))
        return cur.fetchone()[0] > 0

def mark_weather_reminder_sent(tg_id: int, date_str: str):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO weather_reminders (tg_id, remind_date) VALUES (?, ?)", (tg_id, date_str))
        conn.commit()

def toggle_notifications(tg_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("SELECT notify_new_grades FROM users WHERE tg_id = ?", (tg_id,))
        row = cur.fetchone()
        curr = row[0] if row else 1
        new_val = 0 if curr == 1 else 1
        conn.execute("UPDATE users SET notify_new_grades = ? WHERE tg_id = ?", (new_val, tg_id))
        conn.commit()
        return new_val == 1


def save_meal_account(
    tg_id: int,
    account_number: str,
    label: str = "Ученик",
    district: str = "Фрунзенский район",
    alert_threshold: float = 5.0,
):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO meal_accounts (tg_id, account_number, label, district, alert_threshold)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tg_id, account_number) DO UPDATE SET
                label=excluded.label,
                district=excluded.district,
                alert_threshold=excluded.alert_threshold;
            """,
            (tg_id, account_number.strip(), label.strip() or "Ученик", district, float(alert_threshold)),
        )
        conn.commit()


def get_meal_accounts(tg_id: int) -> List[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.execute(
            "SELECT * FROM meal_accounts WHERE tg_id = ? ORDER BY rowid ASC",
            (tg_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_meal_account(tg_id: int, account_number: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.execute(
            "SELECT * FROM meal_accounts WHERE tg_id = ? AND account_number = ?",
            (tg_id, account_number.strip()),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def delete_meal_account(tg_id: int, account_number: str):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM meal_accounts WHERE tg_id = ? AND account_number = ?",
            (tg_id, account_number.strip()),
        )
        conn.commit()


def update_meal_threshold(tg_id: int, account_number: str, threshold: float):
    with get_db() as conn:
        conn.execute(
            "UPDATE meal_accounts SET alert_threshold = ? WHERE tg_id = ? AND account_number = ?",
            (float(threshold), tg_id, account_number.strip()),
        )
        conn.commit()


def mark_meal_alert_sent(tg_id: int, account_number: str, date_str: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE meal_accounts SET last_alert_date = ? WHERE tg_id = ? AND account_number = ?",
            (date_str, tg_id, account_number.strip()),
        )
        conn.commit()

