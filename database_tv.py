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
        conn.execute("""
        CREATE TABLE IF NOT EXISTS children_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            pupil_id TEXT,
            subdomain TEXT,
            district TEXT DEFAULT 'Фрунзенский район',
            erip_account TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS child_clubs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            child_id INTEGER NOT NULL,
            org_name TEXT NOT NULL,
            service_name TEXT DEFAULT '',
            erip_code TEXT NOT NULL,
            di_type TEXT DEFAULT '9191',
            account_number TEXT NOT NULL,
            kind TEXT DEFAULT 'club',
            monthly_amount REAL,
            due_day INTEGER,
            last_paid_month TEXT,
            last_reminded_tag TEXT,
            alert_threshold REAL DEFAULT 5.0,
            last_erip_fio TEXT,
            last_balance REAL,
            last_to_pay REAL,
            last_checked TEXT
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS balance_snapshots (
            tg_id INTEGER NOT NULL,
            club_id INTEGER NOT NULL,
            snap_date TEXT NOT NULL,
            balance REAL,
            saldo_start REAL,
            paid_period REAL,
            PRIMARY KEY (tg_id, club_id, snap_date)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS expenses_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            child_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            amount REAL NOT NULL,
            expense_date TEXT NOT NULL,
            source TEXT DEFAULT 'manual',
            unique_key TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        conn.execute("DROP INDEX IF EXISTS idx_expenses_unique;")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_expenses_unique ON expenses_log(tg_id, unique_key);"
        )
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


# ================= SCHOOL HUB: CHILDREN, CLUBS, EXPENSES =================

def ensure_children_synced(tg_id: int) -> List[Dict[str, Any]]:
    """Sync schools.by pupils into children_profiles if missing, and return all children."""
    with get_db() as conn:
        pupils = conn.execute("SELECT * FROM user_pupils WHERE tg_id = ?", (tg_id,)).fetchall()
        existing = conn.execute("SELECT * FROM children_profiles WHERE tg_id = ? ORDER BY id ASC", (tg_id,)).fetchall()
        existing_pids = {str(r["pupil_id"]) for r in existing if r["pupil_id"]}
        for p in pupils:
            pid = str(p["pupil_id"])
            if pid not in existing_pids:
                conn.execute(
                    """
                    INSERT INTO children_profiles (tg_id, name, pupil_id, subdomain, district)
                    VALUES (?, ?, ?, ?, 'Фрунзенский район')
                    """,
                    (tg_id, p["name"], pid, p["subdomain"]),
                )
        conn.commit()
        rows = conn.execute("SELECT * FROM children_profiles WHERE tg_id = ? ORDER BY id ASC", (tg_id,)).fetchall()
        return [dict(r) for r in rows]


def add_child_profile(
    tg_id: int,
    name: str,
    district: str = "Фрунзенский район",
    erip_account: str = "",
    pupil_id: Optional[str] = None,
    subdomain: Optional[str] = None,
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO children_profiles (tg_id, name, pupil_id, subdomain, district, erip_account)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tg_id, name.strip(), pupil_id, subdomain, district, erip_account.strip() or None),
        )
        child_id = cur.lastrowid
        conn.commit()
    if erip_account.strip():
        attach_school_erip_pack(tg_id, child_id, erip_account.strip(), district)
    return child_id


def update_child_profile(tg_id: int, child_id: int, name: str, district: str, erip_account: str = ""):
    acc = erip_account.strip()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE children_profiles
            SET name = ?, district = ?, erip_account = ?
            WHERE id = ? AND tg_id = ?
            """,
            (name.strip(), district, acc or None, child_id, tg_id),
        )
        conn.commit()
    if acc:
        attach_school_erip_pack(tg_id, child_id, acc, district)


def delete_child_profile(tg_id: int, child_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM child_clubs WHERE tg_id = ? AND child_id = ?", (tg_id, child_id))
        conn.execute("DELETE FROM expenses_log WHERE tg_id = ? AND child_id = ?", (tg_id, child_id))
        conn.execute("DELETE FROM children_profiles WHERE id = ? AND tg_id = ?", (child_id, tg_id))
        conn.commit()


def attach_school_erip_pack(tg_id: int, child_id: int, account_number: str, district: str):
    """Ensure Canteen, Buffet, and School UO Clubs entries exist for a child's 10-digit ERIP number."""
    from services.meal_erip import MINSK_KSHP_DISTRICTS, MINSK_UO_SCHOOL_CLUBS

    if district not in MINSK_KSHP_DISTRICTS:
        return

    kshp = MINSK_KSHP_DISTRICTS[district]
    uo_club = MINSK_UO_SCHOOL_CLUBS.get(district) or MINSK_UO_SCHOOL_CLUBS["Фрунзенский район"]
    pack = [
        ("canteen", "КШП Столовая", "Питание", kshp["canteen"]),
        ("buffet", "КШП Буфет", "Питание в буфете", kshp["buffet"]),
        ("school_club", f"Школьные кружки ({district.replace(' район', '')})", "Кружки для учащихся", uo_club),
    ]
    with get_db() as conn:
        for kind, org_name, svc_name, erip_code in pack:
            row = conn.execute(
                "SELECT id FROM child_clubs WHERE tg_id = ? AND child_id = ? AND kind = ?",
                (tg_id, child_id, kind),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE child_clubs
                    SET account_number = ?, erip_code = ?, org_name = ?, service_name = ?
                    WHERE id = ?
                    """,
                    (account_number, erip_code, org_name, svc_name, row["id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO child_clubs (tg_id, child_id, org_name, service_name, erip_code, di_type, account_number, kind, alert_threshold)
                    VALUES (?, ?, ?, ?, ?, '9191', ?, ?, 5.0)
                    """,
                    (tg_id, child_id, org_name, svc_name, erip_code, account_number, kind),
                )
        conn.commit()


def add_child_club(
    tg_id: int,
    child_id: int,
    org_name: str,
    service_name: str,
    erip_code: str,
    di_type: str,
    account_number: str,
    kind: str = "club",
    monthly_amount: Optional[float] = None,
    due_day: Optional[int] = None,
    alert_threshold: float = 5.0,
    last_erip_fio: str = "",
    last_balance: Optional[float] = None,
    last_to_pay: Optional[float] = None,
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO child_clubs (
                tg_id, child_id, org_name, service_name, erip_code, di_type,
                account_number, kind, monthly_amount, due_day, alert_threshold,
                last_erip_fio, last_balance, last_to_pay, last_checked
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, date('now', 'localtime'))
            """,
            (
                tg_id,
                child_id,
                org_name.strip(),
                service_name.strip(),
                str(erip_code),
                str(di_type),
                account_number.strip(),
                kind,
                float(monthly_amount) if monthly_amount not in (None, "") else None,
                int(due_day) if due_day not in (None, "") else None,
                float(alert_threshold),
                last_erip_fio,
                last_balance,
                last_to_pay,
            ),
        )
        conn.commit()
        return cur.lastrowid


def get_child_clubs(tg_id: int, child_id: Optional[int] = None) -> List[Dict[str, Any]]:
    with get_db() as conn:
        if child_id is not None:
            rows = conn.execute(
                "SELECT * FROM child_clubs WHERE tg_id = ? AND child_id = ? ORDER BY kind IN ('canteen','buffet','school_club') DESC, id ASC",
                (tg_id, child_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM child_clubs WHERE tg_id = ? ORDER BY child_id ASC, kind IN ('canteen','buffet','school_club') DESC, id ASC",
                (tg_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_club_by_id(tg_id: int, club_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM child_clubs WHERE tg_id = ? AND id = ?",
            (tg_id, club_id),
        ).fetchone()
        return dict(row) if row else None


def delete_child_club(tg_id: int, club_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM balance_snapshots WHERE tg_id = ? AND club_id = ?", (tg_id, club_id))
        conn.execute("DELETE FROM child_clubs WHERE tg_id = ? AND id = ?", (tg_id, club_id))
        conn.commit()


def update_child_club(
    tg_id: int,
    club_id: int,
    org_name: Optional[str] = None,
    account_number: Optional[str] = None,
    monthly_amount: Optional[float] = None,
    due_day: Optional[int] = None,
    alert_threshold: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM child_clubs WHERE tg_id = ? AND id = ?",
            (tg_id, club_id),
        ).fetchone()
        if not existing:
            return None
        new_org = org_name.strip() if org_name and org_name.strip() else existing["org_name"]
        new_acc = account_number.strip() if account_number and account_number.strip() else existing["account_number"]
        conn.execute(
            """
            UPDATE child_clubs
            SET org_name = ?, account_number = ?, monthly_amount = ?, due_day = ?, alert_threshold = ?
            WHERE tg_id = ? AND id = ?
            """,
            (new_org, new_acc, monthly_amount, due_day, alert_threshold if alert_threshold is not None else existing["alert_threshold"], tg_id, club_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM child_clubs WHERE id = ?", (club_id,)).fetchone()
        return dict(row) if row else None


def update_club_erip_state(
    tg_id: int,
    club_id: int,
    fio: str,
    balance: Optional[float],
    to_pay: Optional[float],
    snap_date: str,
    saldo_start: Optional[float] = None,
    paid_period: Optional[float] = None,
):
    """Update cached ERIP state and record snapshot + automatic expense deltas."""
    with get_db() as conn:
        club = conn.execute("SELECT * FROM child_clubs WHERE tg_id = ? AND id = ?", (tg_id, club_id)).fetchone()
        if not club:
            return
        prev_snap = conn.execute(
            "SELECT * FROM balance_snapshots WHERE tg_id = ? AND club_id = ? AND snap_date < ? ORDER BY snap_date DESC LIMIT 1",
            (tg_id, club_id, snap_date),
        ).fetchone()

        conn.execute(
            """
            UPDATE child_clubs
            SET last_erip_fio = CASE WHEN ? != '' THEN ? ELSE last_erip_fio END,
                last_balance = ?,
                last_to_pay = ?,
                last_checked = ?
            WHERE id = ?
            """,
            (fio, fio, balance, to_pay, snap_date, club_id),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO balance_snapshots (tg_id, club_id, snap_date, balance, saldo_start, paid_period)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tg_id, club_id, snap_date, balance, saldo_start, paid_period),
        )

        # Автоматический учёт расходов:
        # 1) Для столовой за текущий месяц есть точная формула: spent = saldo_start + paid_period - balance
        if club["kind"] == "canteen" and saldo_start is not None and paid_period is not None and balance is not None:
            month_key = snap_date[:7]
            spent_month = round((saldo_start + paid_period) - balance, 2)
            if spent_month > 0:
                ukey = f"canteen_month:{club['child_id']}:{month_key}"
                conn.execute(
                    """
                    INSERT INTO expenses_log (tg_id, child_id, category, title, amount, expense_date, source, unique_key)
                    VALUES (?, ?, 'canteen', 'Школьная столовая', ?, ?, 'erip_canteen_month', ?)
                    ON CONFLICT(tg_id, unique_key) DO UPDATE SET
                        amount = excluded.amount,
                        expense_date = excluded.expense_date;
                    """,
                    (tg_id, club["child_id"], spent_month, snap_date, ukey),
                )
        # 2) Для буфета и школьных кружков отслеживаем суточное списание баланса
        elif prev_snap and prev_snap["balance"] is not None and balance is not None:
            delta = round(prev_snap["balance"] - balance, 2)
            if delta > 0.01:
                cat = "buffet" if club["kind"] == "buffet" else "club"
                ukey = f"snap_delta:{club_id}:{snap_date}"
                conn.execute(
                    """
                    INSERT INTO expenses_log (tg_id, child_id, category, title, amount, expense_date, source, unique_key)
                    VALUES (?, ?, ?, ?, ?, ?, 'erip_auto', ?)
                    ON CONFLICT(tg_id, unique_key) DO UPDATE SET amount = excluded.amount;
                    """,
                    (tg_id, club["child_id"], cat, club["org_name"], delta, snap_date, ukey),
                )
        conn.commit()


def confirm_club_payment(tg_id: int, club_id: int, amount: Optional[float], date_str: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        club = conn.execute("SELECT * FROM child_clubs WHERE tg_id = ? AND id = ?", (tg_id, club_id)).fetchone()
        if not club:
            return None
        paid_amt = float(amount) if amount is not None else float(club["monthly_amount"] or club["last_to_pay"] or 0.0)
        month_key = date_str[:7]
        conn.execute(
            "UPDATE child_clubs SET last_paid_month = ? WHERE id = ?",
            (month_key, club_id),
        )
        if paid_amt > 0:
            cat = "canteen" if club["kind"] == "canteen" else ("buffet" if club["kind"] == "buffet" else "club")
            ukey = f"club_paid:{club_id}:{month_key}"
            conn.execute(
                """
                INSERT INTO expenses_log (tg_id, child_id, category, title, amount, expense_date, source, unique_key)
                VALUES (?, ?, ?, ?, ?, ?, 'club_confirm', ?)
                ON CONFLICT(tg_id, unique_key) DO UPDATE SET amount = excluded.amount, expense_date = excluded.expense_date;
                """,
                (tg_id, club["child_id"], cat, f"{club['org_name']} ({club['service_name'] or 'кружок'})", round(paid_amt, 2), date_str, ukey),
            )
        conn.commit()
        return dict(club)


def get_club_alert_tag(club_id: int) -> Optional[str]:
    """Return the last_reminded_tag stored for a club/account."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT last_reminded_tag FROM child_clubs WHERE id = ?",
            (club_id,),
        ).fetchone()
        return row["last_reminded_tag"] if row else None


def mark_club_reminded(club_id: int, tag: str):
    with get_db() as conn:
        conn.execute("UPDATE child_clubs SET last_reminded_tag = ? WHERE id = ?", (tag, club_id))
        conn.commit()


def add_manual_expense(tg_id: int, child_id: int, category: str, title: str, amount: float, expense_date: str):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO expenses_log (tg_id, child_id, category, title, amount, expense_date, source)
            VALUES (?, ?, ?, ?, ?, ?, 'manual')
            """,
            (tg_id, child_id, category, title.strip(), round(float(amount), 2), expense_date),
        )
        conn.commit()


def delete_expense(tg_id: int, expense_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM expenses_log WHERE tg_id = ? AND id = ?", (tg_id, expense_id))
        conn.commit()


def get_expenses_summary(
    tg_id: int,
    child_id: Optional[int] = None,
    period: str = "month",
    today_str: Optional[str] = None,
) -> Dict[str, Any]:
    from datetime import date, timedelta

    today = date.fromisoformat(today_str) if today_str else date.today()
    if period == "week":
        start = (today - timedelta(days=today.weekday())).isoformat()
    elif period == "month":
        start = today.replace(day=1).isoformat()
    elif period == "quarter":
        # Текущая четверть / ~90 дней с начала четверти (упрощенно 1 число 2 месяца назад)
        m_start = ((today.month - 1) // 3) * 3 + 1
        start = today.replace(month=m_start, day=1).isoformat()
    else:  # academic year: с 1 сентября
        yr = today.year if today.month >= 9 else today.year - 1
        start = f"{yr}-09-01"

    params: List[Any] = [tg_id, start, today.isoformat()]
    child_clause = ""
    if child_id is not None:
        child_clause = " AND child_id = ?"
        params.append(child_id)

    # В отчёте за 7 дней не примешиваем агрегат за весь месяц столовой
    source_clause = " AND source != 'erip_canteen_month'" if period == "week" else ""

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM expenses_log
            WHERE tg_id = ? AND expense_date >= ? AND expense_date <= ? {child_clause} {source_clause}
            ORDER BY expense_date DESC, id DESC
            """,
            params,
        ).fetchall()

    by_cat: Dict[str, float] = {"canteen": 0.0, "buffet": 0.0, "club": 0.0, "textbooks": 0.0, "other": 0.0}
    items = []
    total = 0.0
    for r in rows:
        d = dict(r)
        amt = float(d["amount"] or 0.0)
        cat = d["category"] if d["category"] in by_cat else "other"
        by_cat[cat] = round(by_cat[cat] + amt, 2)
        total = round(total + amt, 2)
        items.append(d)

    return {
        "period": period,
        "start_date": start,
        "end_date": today.isoformat(),
        "total": total,
        "by_category": by_cat,
        "items": items[:50],
    }


