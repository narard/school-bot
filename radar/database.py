import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlite3
import hashlib
from datetime import datetime
from typing import List, Dict, Any, Optional

try:
    from radar.config import (
        DEFAULT_ADMIN_ID,
        DEFAULT_MORNING_TIME,
        DEFAULT_EVENING_TIME,
        DEFAULT_TG_CHANNELS,
    )
except ImportError:
    from config import (
        DEFAULT_ADMIN_ID,
        DEFAULT_MORNING_TIME,
        DEFAULT_EVENING_TIME,
        DEFAULT_TG_CHANNELS,
    )

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "radar.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_url(url: str) -> str:
    clean = (url or "").strip().lower()
    clean = re.sub(r"[?&](utm_[^&]+|ref=[^&]+|source=[^&]+)", "", clean)
    clean = clean.rstrip("/?&")
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:32]


def normalize_title(title: str) -> str:
    clean = re.sub(r"[^a-zа-яё0-9]", "", (title or "").lower())
    return clean[:65]


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            morning_time TEXT DEFAULT '08:30',
            evening_time TEXT DEFAULT '19:30',
            is_active INTEGER DEFAULT 1,
            last_morning_date TEXT DEFAULT '',
            last_evening_date TEXT DEFAULT '',
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_items (
            url_hash TEXT PRIMARY KEY,
            title_norm TEXT,
            url TEXT,
            title TEXT,
            category TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS digest_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            digest_type TEXT,
            item_num INTEGER,
            category TEXT,
            title_ru TEXT,
            summary_ru TEXT,
            idea_ru TEXT,
            url TEXT,
            source_name TEXT,
            raw_text TEXT,
            deep_dive_cache TEXT DEFAULT '',
            monetize_cache TEXT DEFAULT '',
            video_script_cache TEXT DEFAULT '',
            prototype_cache TEXT DEFAULT '',
            created_at TEXT
        )
    """)

    # Безопасная миграция новых колонок кэша для существующей БД
    for col_name in ("video_script_cache", "prototype_cache"):
        try:
            cur.execute(f"ALTER TABLE digest_items ADD COLUMN {col_name} TEXT DEFAULT ''")
        except Exception:
            pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS money_tracker (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            amount_usd REAL DEFAULT 0,
            title TEXT,
            status TEXT DEFAULT 'launched',
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            item_id INTEGER,
            category TEXT,
            title_ru TEXT,
            summary_ru TEXT,
            idea_ru TEXT,
            url TEXT,
            source_name TEXT,
            saved_at TEXT,
            UNIQUE(tg_id, url)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tg_channels (
            username TEXT PRIMARY KEY,
            title TEXT,
            category TEXT DEFAULT 'news',
            is_active INTEGER DEFAULT 1
        )
    """)

    # Регистрируем владельца по умолчанию
    cur.execute(
        """
        INSERT OR IGNORE INTO users (tg_id, morning_time, evening_time, is_active, created_at)
        VALUES (?, ?, ?, 1, ?)
        """,
        (DEFAULT_ADMIN_ID, DEFAULT_MORNING_TIME, DEFAULT_EVENING_TIME, datetime.now().isoformat()),
    )

    # Заполняем стартовую базу ТГ-каналов
    for username, title, cat in DEFAULT_TG_CHANNELS:
        cur.execute(
            """
            INSERT OR IGNORE INTO tg_channels (username, title, category, is_active)
            VALUES (?, ?, ?, 1)
            """,
            (username.lstrip("@").strip(), title, cat),
        )

    conn.commit()
    conn.close()


def ensure_user(tg_id: int) -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO users (tg_id, morning_time, evening_time, is_active, created_at)
        VALUES (?, ?, ?, 1, ?)
        """,
        (tg_id, DEFAULT_MORNING_TIME, DEFAULT_EVENING_TIME, datetime.now().isoformat()),
    )
    conn.commit()
    cur.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = dict(cur.fetchone())
    conn.close()
    return row


def get_active_users() -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users WHERE is_active = 1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_user_schedule(tg_id: int, morning_time: Optional[str] = None, evening_time: Optional[str] = None, is_active: Optional[int] = None) -> None:
    conn = get_conn()
    if morning_time is not None:
        conn.execute("UPDATE users SET morning_time = ? WHERE tg_id = ?", (morning_time, tg_id))
    if evening_time is not None:
        conn.execute("UPDATE users SET evening_time = ? WHERE tg_id = ?", (evening_time, tg_id))
    if is_active is not None:
        conn.execute("UPDATE users SET is_active = ? WHERE tg_id = ?", (is_active, tg_id))
    conn.commit()
    conn.close()


def mark_digest_sent(tg_id: int, edition: str, date_str: str) -> None:
    conn = get_conn()
    if edition == "morning":
        conn.execute("UPDATE users SET last_morning_date = ? WHERE tg_id = ?", (date_str, tg_id))
    elif edition == "evening":
        conn.execute("UPDATE users SET last_evening_date = ? WHERE tg_id = ?", (date_str, tg_id))
    conn.commit()
    conn.close()


def is_item_seen(url: str, title: str = "") -> bool:
    u_hash = normalize_url(url)
    t_norm = normalize_title(title)
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM seen_items WHERE url_hash = ? OR (title_norm != '' AND title_norm = ?)",
        (u_hash, t_norm),
    ).fetchone()
    conn.close()
    return row is not None


def mark_item_seen(url: str, title: str, category: str) -> None:
    u_hash = normalize_url(url)
    t_norm = normalize_title(title)
    conn = get_conn()
    conn.execute(
        """
        INSERT OR REPLACE INTO seen_items (url_hash, title_norm, url, title, category, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (u_hash, t_norm, url, title[:250], category, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def save_digest_item(
    digest_type: str,
    item_num: int,
    category: str,
    title_ru: str,
    summary_ru: str,
    idea_ru: str,
    url: str,
    source_name: str,
    raw_text: str,
) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO digest_items (
            digest_type, item_num, category, title_ru, summary_ru,
            idea_ru, url, source_name, raw_text, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            digest_type,
            item_num,
            category,
            title_ru,
            summary_ru,
            idea_ru,
            url,
            source_name,
            (raw_text or "")[:3000],
            datetime.now().isoformat(),
        ),
    )
    item_id = cur.lastrowid
    conn.commit()
    conn.close()
    return item_id


def get_digest_item(item_id: int) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM digest_items WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_item_cache(item_id: int, field: str, content: str) -> None:
    if field not in ("deep_dive_cache", "monetize_cache", "video_script_cache", "prototype_cache"):
        return
    conn = get_conn()
    conn.execute(f"UPDATE digest_items SET {field} = ? WHERE id = ?", (content, item_id))
    conn.commit()
    conn.close()


def add_tracker_entry(tg_id: int, amount_usd: float, title: str, status: str = "earned") -> None:
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO money_tracker (tg_id, amount_usd, title, status, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (tg_id, amount_usd, title[:200], status, datetime.now().strftime("%d.%m.%Y")),
    )
    conn.commit()
    conn.close()


def get_tracker_data(tg_id: int) -> Dict[str, Any]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM money_tracker WHERE tg_id = ? ORDER BY id DESC LIMIT 30",
        (tg_id,),
    ).fetchall()
    total_row = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0), COUNT(*) FROM money_tracker WHERE tg_id = ?",
        (tg_id,),
    ).fetchone()
    conn.close()

    total_usd = float(total_row[0] or 0.0)
    total_actions = int(total_row[1] or 0)

    if total_usd < 1000:
        stage_num = 1
        stage_name = "Этап 1: Первая $1,000"
        target_usd = 1000.0
    elif total_usd < 10000:
        stage_num = 2
        stage_name = "Этап 2: Система на $10,000"
        target_usd = 10000.0
    else:
        stage_num = 3
        stage_name = "Этап 3: Масштаб $100,000"
        target_usd = 100000.0

    pct = min(100.0, round((total_usd / target_usd) * 100, 1))
    return {
        "total_usd": total_usd,
        "total_actions": total_actions,
        "stage_num": stage_num,
        "stage_name": stage_name,
        "target_usd": target_usd,
        "pct": pct,
        "entries": [dict(r) for r in rows],
    }


def clear_last_tracker_entry(tg_id: int) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM money_tracker WHERE tg_id = ? ORDER BY id DESC LIMIT 1",
        (tg_id,),
    ).fetchone()
    if not row:
        conn.close()
        return False
    conn.execute("DELETE FROM money_tracker WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return True


def add_favorite(tg_id: int, item_id: int) -> bool:
    item = get_digest_item(item_id)
    if not item:
        return False
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO favorites (
                tg_id, item_id, category, title_ru, summary_ru,
                idea_ru, url, source_name, saved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tg_id,
                item_id,
                item["category"],
                item["title_ru"],
                item["summary_ru"],
                item["idea_ru"],
                item["url"],
                item["source_name"],
                datetime.now().strftime("%d.%m.%Y %H:%M"),
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_favorites(tg_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM favorites WHERE tg_id = ? ORDER BY id DESC LIMIT ?",
        (tg_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_favorite(tg_id: int, fav_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM favorites WHERE tg_id = ? AND id = ?", (tg_id, fav_id))
    conn.commit()
    conn.close()


def get_active_channels() -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM tg_channels WHERE is_active = 1 ORDER BY category, username").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_tg_channel(username: str, title: str = "", category: str = "news") -> str:
    clean = username.strip()
    clean = re.sub(r"^https?://(t\.me|telegram\.me)/(s/)?", "", clean)
    clean = clean.lstrip("@").split("/")[0].split("?")[0].strip()
    if not clean:
        return ""
    if not title:
        title = f"Канал @{clean}"
    conn = get_conn()
    conn.execute(
        """
        INSERT OR REPLACE INTO tg_channels (username, title, category, is_active)
        VALUES (?, ?, ?, 1)
        """,
        (clean, title, category),
    )
    conn.commit()
    conn.close()
    return clean


def remove_tg_channel(username: str) -> bool:
    clean = username.lstrip("@").strip()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM tg_channels WHERE LOWER(username) = LOWER(?)", (clean,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_stats() -> Dict[str, int]:
    conn = get_conn()
    seen_cnt = conn.execute("SELECT COUNT(*) FROM seen_items").fetchone()[0]
    digest_cnt = conn.execute("SELECT COUNT(*) FROM digest_items").fetchone()[0]
    fav_cnt = conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
    ch_cnt = conn.execute("SELECT COUNT(*) FROM tg_channels WHERE is_active = 1").fetchone()[0]
    conn.close()
    return {
        "seen": seen_cnt,
        "digests": digest_cnt,
        "favorites": fav_cnt,
        "channels": ch_cnt,
    }


# Авто-инициализация таблиц при загрузке модуля
init_db()
