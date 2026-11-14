import argparse
import asyncio
import calendar
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

import database_tv as db
from schools_client import SchoolsClient
from services.meal_erip import (
    DISTRICT_SHORT_KEYS,
    inspect_erip_entry,
    normalize_erip_code,
    query_erip_service,
    search_minsk_clubs,
    sync_minsk_clubs_catalog,
)

logger = logging.getLogger(__name__)

WEBAPP_DIR = os.path.join(os.path.dirname(__file__), "webapp")
DEFAULT_PORT = int(os.environ.get("WEBAPP_PORT", "8080"))

BELLS = {
    "1": "08:30–09:15",
    "2": "09:25–10:10",
    "3": "10:30–11:15",
    "4": "11:35–12:20",
    "5": "12:35–13:20",
    "6": "13:35–14:20",
}

RUSSIAN_WEEKDAYS = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье",
}

_client_cache: Dict[int, SchoolsClient] = {}
_TTL_CACHE: Dict[str, Tuple[float, Any]] = {}

RUSSIAN_MONTHS = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


def _resolve_tg_id(request: Optional[web.Request] = None) -> int:
    if request:
        q_uid = request.query.get("uid")
        if q_uid and q_uid.isdigit():
            return int(q_uid)
    users = db.get_all_users()
    if users:
        return int(users[0]["tg_id"])
    return 1


def _get_schools_client(tg_id: int) -> Optional[SchoolsClient]:
    user = db.get_user(tg_id)
    if not user:
        return None
    c = _client_cache.get(tg_id)
    if c and c.logged_in:
        return c
    client = SchoolsClient()
    if client.login(user["login"], user["password"]):
        _client_cache[tg_id] = client
        return client
    return None


def _compute_club_due_info(club: Dict[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    """Calculate due date status and reminder badge for a club."""
    today = today or date.today()
    month_key = today.strftime("%Y-%m")
    due_day = club.get("due_day")
    if not due_day:
        return {"due_status": "auto" if club["kind"] in ("canteen", "buffet", "school_club") else "none", "days_left": None, "paid_this_month": club.get("last_paid_month") == month_key}

    if club.get("last_paid_month") == month_key:
        return {"due_status": "paid", "days_left": None, "paid_this_month": True}

    # Безопасный день в текущем месяце (если due_day = 31 в ноябре -> 30)
    import calendar
    max_day = calendar.monthrange(today.year, today.month)[1]
    due_dt = today.replace(day=min(int(due_day), max_day))
    diff = (due_dt - today).days
    if diff < 0:
        st = "overdue"
    elif diff <= 1:
        st = "urgent"
    elif diff <= 5:
        st = "soon"
    else:
        st = "upcoming"
    return {"due_status": st, "days_left": diff, "due_date": due_dt.strftime("%d.%m"), "paid_this_month": False}


def _refresh_single_club(tg_id: int, club: Dict[str, Any]) -> Dict[str, Any]:
    res = query_erip_service(club["erip_code"], club["account_number"], club.get("di_type") or "9191")
    today_str = date.today().isoformat()
    if res.get("ok"):
        db.update_club_erip_state(
            tg_id=tg_id,
            club_id=club["id"],
            fio=res.get("fio", ""),
            balance=res.get("balance"),
            to_pay=res.get("to_pay"),
            snap_date=today_str,
            saldo_start=res.get("saldo_start"),
            paid_period=res.get("paid_period"),
        )
    updated = db.get_club_by_id(tg_id, club["id"]) or club
    updated.update(_compute_club_due_info(updated))
    updated["erip_live"] = res
    return updated


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(os.path.join(WEBAPP_DIR, "index.html"))


async def handle_manifest(request: web.Request) -> web.FileResponse:
    return web.FileResponse(os.path.join(WEBAPP_DIR, "manifest.json"), headers={"Content-Type": "application/manifest+json"})


async def handle_icon(request: web.Request) -> web.FileResponse:
    return web.FileResponse(os.path.join(WEBAPP_DIR, "icon.svg"), headers={"Content-Type": "image/svg+xml"})


async def handle_themes(request: web.Request) -> web.FileResponse:
    return web.FileResponse(os.path.join(WEBAPP_DIR, "themes.html"))


async def api_bootstrap(request: web.Request) -> web.Response:
    tg_id = _resolve_tg_id(request)
    children = db.ensure_children_synced(tg_id)
    clubs = db.get_child_clubs(tg_id)
    today = date.today()
    for c in clubs:
        c.update(_compute_club_due_info(c, today))
    expenses = db.get_expenses_summary(tg_id, period="month", today_str=today.isoformat())
    return web.json_response(
        {
            "ok": True,
            "today": today.strftime("%d.%m.%Y"),
            "districts": DISTRICT_SHORT_KEYS,
            "children": children,
            "clubs": clubs,
            "month_expenses": expenses,
        }
    )


async def api_add_child(request: web.Request) -> web.Response:
    tg_id = _resolve_tg_id(request)
    data = await request.json()
    name = (data.get("name") or "").strip()
    district = data.get("district") or "Фрунзенский район"
    erip_acc = (data.get("erip_account") or "").strip()
    if not name:
        return web.json_response({"ok": False, "error": "Укажите имя ребёнка"}, status=400)

    child_id = data.get("id")
    if child_id:
        db.update_child_profile(tg_id, int(child_id), name, district, erip_acc)
    else:
        child_id = db.add_child_profile(tg_id, name, district=district, erip_account=erip_acc)

    if erip_acc:
        clubs = db.get_child_clubs(tg_id, int(child_id))
        await asyncio.to_thread(_refresh_batch, tg_id, clubs)

    return web.json_response({"ok": True, "child_id": child_id})


async def api_delete_child(request: web.Request) -> web.Response:
    tg_id = _resolve_tg_id(request)
    cid = int(request.match_info["child_id"])
    db.delete_child_profile(tg_id, cid)
    return web.json_response({"ok": True})


def _refresh_batch(tg_id: int, clubs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not clubs:
        return []
    with ThreadPoolExecutor(max_workers=min(6, len(clubs))) as pool:
        return list(pool.map(lambda c: _refresh_single_club(tg_id, c), clubs))


async def api_erip_search(request: web.Request) -> web.Response:
    q = request.query.get("q", "").strip()
    force = request.query.get("force") == "1"
    if force:
        await asyncio.to_thread(sync_minsk_clubs_catalog, True)
    results = await asyncio.to_thread(search_minsk_clubs, q, 30)
    return web.json_response({"ok": True, "items": results})


async def api_erip_inspect(request: web.Request) -> web.Response:
    raw_code = request.query.get("code", "").strip()
    di_type = request.query.get("di_type", "9120")
    norm_code = normalize_erip_code(raw_code)
    if not norm_code:
        return web.json_response({"ok": False, "error": "Нет code"}, status=400)
    code = norm_code if di_type == "9191" else raw_code
    res = await asyncio.to_thread(inspect_erip_entry, code, di_type)
    return web.json_response(res)


async def api_erip_verify(request: web.Request) -> web.Response:
    data = await request.json()
    code = normalize_erip_code(data.get("code"))
    di_type = str(data.get("di_type") or "9191").strip()
    acc = str(data.get("account_number") or "").strip()
    if not code or not acc:
        return web.json_response({"ok": False, "error": "Введите код услуги и номер счёта/договора"}, status=400)
    res = await asyncio.to_thread(query_erip_service, code, acc, di_type)
    return web.json_response(res)


async def api_add_club(request: web.Request) -> web.Response:
    tg_id = _resolve_tg_id(request)
    data = await request.json()
    child_id = int(data["child_id"])
    org_name = (data.get("org_name") or "").strip()
    service_name = (data.get("service_name") or "Кружок").strip()
    erip_code = normalize_erip_code(data.get("erip_code"))
    di_type = str(data.get("di_type") or "9191").strip()
    account_number = str(data.get("account_number") or "").strip()
    kind = str(data.get("kind") or "club").strip()
    if kind not in ("club", "canteen", "buffet", "school_club"):
        kind = "club"
    monthly_amount = data.get("monthly_amount")
    due_day = data.get("due_day")

    if not org_name or not erip_code or not account_number:
        return web.json_response({"ok": False, "error": "Заполните организацию, услугу и номер договора/счёта"}, status=400)

    verify = await asyncio.to_thread(query_erip_service, erip_code, account_number, di_type)
    if not verify.get("ok"):
        return web.json_response({"ok": False, "error": verify.get("error") or "Счёт не найден в ЕРИП"}, status=400)

    club_id = db.add_child_club(
        tg_id=tg_id,
        child_id=child_id,
        org_name=org_name,
        service_name=service_name,
        erip_code=erip_code,
        di_type=di_type,
        account_number=account_number,
        kind=kind,
        monthly_amount=float(monthly_amount) if monthly_amount not in (None, "", 0, "0") else None,
        due_day=int(due_day) if due_day not in (None, "", 0, "0") else None,
        alert_threshold=float(data.get("alert_threshold") or 5.0),
        last_erip_fio=verify.get("fio", ""),
        last_balance=verify.get("balance"),
        last_to_pay=verify.get("to_pay"),
    )
    return web.json_response({"ok": True, "club_id": club_id, "verify": verify})


async def api_refresh_club(request: web.Request) -> web.Response:
    tg_id = _resolve_tg_id(request)
    club_id = int(request.match_info["club_id"])
    club = db.get_club_by_id(tg_id, club_id)
    if not club:
        return web.json_response({"ok": False, "error": "Не найдено"}, status=404)
    updated = await asyncio.to_thread(_refresh_single_club, tg_id, club)
    return web.json_response({"ok": True, "club": updated})


async def api_refresh_all_clubs(request: web.Request) -> web.Response:
    tg_id = _resolve_tg_id(request)
    q_cid = request.query.get("child_id")
    cid = int(q_cid) if q_cid and q_cid.isdigit() else None
    clubs = db.get_child_clubs(tg_id, cid)
    updated = await asyncio.to_thread(_refresh_batch, tg_id, clubs)
    return web.json_response({"ok": True, "clubs": updated})


async def api_confirm_club_payment(request: web.Request) -> web.Response:
    tg_id = _resolve_tg_id(request)
    club_id = int(request.match_info["club_id"])
    try:
        data = await request.json()
    except Exception:
        data = {}
    amt = data.get("amount")
    club = db.confirm_club_payment(
        tg_id,
        club_id,
        amount=float(amt) if amt not in (None, "") else None,
        date_str=date.today().isoformat(),
    )
    if not club:
        return web.json_response({"ok": False, "error": "Кружок не найден"}, status=404)
    return web.json_response({"ok": True})


async def api_delete_club(request: web.Request) -> web.Response:
    tg_id = _resolve_tg_id(request)
    club_id = int(request.match_info["club_id"])
    db.delete_child_club(tg_id, club_id)
    return web.json_response({"ok": True})


async def api_update_club(request: web.Request) -> web.Response:
    tg_id = _resolve_tg_id(request)
    club_id = int(request.match_info["club_id"])
    existing = db.get_club_by_id(tg_id, club_id)
    if not existing:
        return web.json_response({"ok": False, "error": "Кружок не найден"}, status=404)

    data = await request.json()
    org_name = (data.get("org_name") or "").strip() or existing["org_name"]
    account_number = (data.get("account_number") or "").strip() or existing["account_number"]
    raw_amt = data.get("monthly_amount")
    monthly_amount = float(str(raw_amt).replace(",", ".")) if raw_amt not in (None, "", 0, "0") else None
    raw_due = data.get("due_day")
    due_day = int(raw_due) if raw_due not in (None, "", 0, "0") else None
    raw_thr = data.get("alert_threshold")
    alert_threshold = float(str(raw_thr).replace(",", ".")) if raw_thr not in (None, "") else existing.get("alert_threshold", 5.0)

    updated = db.update_child_club(
        tg_id=tg_id,
        club_id=club_id,
        org_name=org_name,
        account_number=account_number,
        monthly_amount=monthly_amount,
        due_day=due_day,
        alert_threshold=alert_threshold,
    )
    if account_number != existing["account_number"] and updated:
        updated = await asyncio.to_thread(_refresh_single_club, tg_id, updated)
    return web.json_response({"ok": True, "club": updated})


async def api_get_expenses(request: web.Request) -> web.Response:
    tg_id = _resolve_tg_id(request)
    q_cid = request.query.get("child_id")
    cid = int(q_cid) if q_cid and q_cid.isdigit() else None
    period = request.query.get("period", "month")
    summary = db.get_expenses_summary(tg_id, child_id=cid, period=period)
    return web.json_response({"ok": True, **summary})


async def api_add_expense(request: web.Request) -> web.Response:
    tg_id = _resolve_tg_id(request)
    data = await request.json()
    child_id = int(data["child_id"])
    category = data.get("category") or "other"
    title = (data.get("title") or "").strip()
    amount = float(data.get("amount") or 0)
    exp_date = data.get("expense_date") or date.today().isoformat()
    if not title or amount <= 0:
        return web.json_response({"ok": False, "error": "Укажите название и сумму > 0"}, status=400)
    db.add_manual_expense(tg_id, child_id, category, title, amount, exp_date)
    return web.json_response({"ok": True})


async def api_delete_expense(request: web.Request) -> web.Response:
    tg_id = _resolve_tg_id(request)
    exp_id = int(request.match_info["expense_id"])
    db.delete_expense(tg_id, exp_id)
    return web.json_response({"ok": True})


def _cache_get(key: str, ttl: int = 300) -> Optional[Any]:
    entry = _TTL_CACHE.get(key)
    if entry and (time.time() - entry[0]) < ttl:
        return entry[1]
    return None


def _cache_set(key: str, val: Any) -> Any:
    _TTL_CACHE[key] = (time.time(), val)
    return val


def _parse_attendance(att_raw: str) -> Dict[str, Any]:
    att_clean = (att_raw or "").split("Числа вносятся")[0].strip()
    m_tot = re.search(r"Пропущенных уроков:\s*(\d+)", att_clean)
    m_unex = re.search(r"неуважительным причинам:\s*(\d+)", att_clean)
    return {
        "text": att_clean,
        "missed_total": int(m_tot.group(1)) if m_tot else 0,
        "missed_unexcused": int(m_unex.group(1)) if m_unex else 0,
    }


def _get_quarters_cached(client: SchoolsClient, pid: str, sub: str) -> Dict[str, Any]:
    key = f"q:{sub}:{pid}"
    cached = _cache_get(key, ttl=900)
    if cached:
        return cached
    return _cache_set(key, client.get_quarters(pid, sub))


def _get_quarter_marks_cached(client: SchoolsClient, pid: str, sub: str) -> Dict[str, Any]:
    key = f"qm:{sub}:{pid}"
    cached = _cache_get(key, ttl=300)
    if cached:
        return cached
    return _cache_set(key, client.get_quarter_marks(pid, sub))


def _pick_quarter_id_for_date(q_info: Dict[str, Any], target_dt: date) -> Optional[str]:
    quarters = q_info.get("quarters") or []
    if not quarters:
        return q_info.get("current")
    m = target_dt.month
    idx = 0 if m in (9, 10) else (1 if m in (11, 12) else (2 if m in (1, 2, 3) else 3))
    if idx < len(quarters):
        return quarters[idx]["id"]
    return q_info.get("current")


def _fetch_week_for_date(
    client: SchoolsClient, pid: str, sub: str, q_info: Dict[str, Any], monday_dt: date
) -> Dict[str, Any]:
    wstr = monday_dt.strftime("%Y-%m-%d")
    key = f"wk:{sub}:{pid}:{wstr}"
    cached = _cache_get(key, ttl=180)
    if cached:
        return cached

    primary_qid = _pick_quarter_id_for_date(q_info, monday_dt)
    diary = client.get_week_diary(pid, sub, quarter_id=primary_qid, week_date=wstr)
    if not diary.get("days"):
        # На стыке четвертей пробуем соседнюю четверть
        for q in (q_info.get("quarters") or []):
            if q["id"] != primary_qid:
                alt = client.get_week_diary(pid, sub, quarter_id=q["id"], week_date=wstr)
                if alt.get("days"):
                    diary = alt
                    break
    return _cache_set(key, diary)


def _enrich_days(days: List[Dict[str, Any]]) -> Dict[str, Any]:
    enriched = []
    total_lessons = 0
    marks_list = []
    numeric_marks = []

    for d in days:
        e_lessons = []
        for l in d.get("lessons", []):
            num = str(l.get("number", ""))
            raw_mark = (l.get("mark") or "").strip()
            e_lessons.append({**l, "time": BELLS.get(num, "")})
            if l.get("subject"):
                total_lessons += 1
            if raw_mark:
                marks_list.append({"day": d.get("title", ""), "subject": l.get("subject", ""), "mark": raw_mark})
                for tok in re.findall(r"\d+", raw_mark):
                    v = int(tok)
                    if 1 <= v <= 10:
                        numeric_marks.append(v)
        enriched.append({
            "title": d.get("title", ""),
            "lesson_count": len([x for x in e_lessons if x.get("subject")]),
            "lessons": e_lessons,
        })

    avg = round(sum(numeric_marks) / len(numeric_marks), 2) if numeric_marks else None
    return {
        "days": enriched,
        "total_lessons": total_lessons,
        "marks_list": marks_list,
        "numeric_marks": numeric_marks,
        "period_average": avg,
    }


def _fetch_diary_sync(
    tg_id: int, child_id: int, mode: str, week_offset: int = 0, month_offset: int = 0, day_offset: int = 0
) -> Dict[str, Any]:
    children = db.ensure_children_synced(tg_id)
    child = next((c for c in children if c["id"] == child_id), None)
    if not child or not child.get("pupil_id") or not child.get("subdomain"):
        return {"ok": False, "no_schools_by": True, "error": "Для этого ребёнка дневник Schools.by не привязан"}

    client = _get_schools_client(tg_id)
    if not client:
        return {"ok": False, "error": "Не удалось войти в Schools.by"}

    pid = str(child["pupil_id"])
    sub = str(child["subdomain"])
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())

    q_info = _get_quarters_cached(client, pid, sub)

    if mode == "marks":
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_qm = pool.submit(_get_quarter_marks_cached, client, pid, sub)
            f_wk = pool.submit(_fetch_week_for_date, client, pid, sub, q_info, this_monday)
            q_data = f_qm.result()
            wk = f_wk.result()
        att = _parse_attendance(wk.get("attendance", ""))
        return {
            "ok": True,
            "mode": "marks",
            "child_name": child["name"],
            "attendance": att["text"],
            "attendance_info": att,
            **q_data,
        }

    if mode == "week":
        target_monday = this_monday + timedelta(weeks=week_offset)
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_wk = pool.submit(_fetch_week_for_date, client, pid, sub, q_info, target_monday)
            f_qm = pool.submit(_get_quarter_marks_cached, client, pid, sub)
            diary = f_wk.result()
            q_data = f_qm.result()
        stats = _enrich_days(diary.get("days", []))
        att = _parse_attendance(diary.get("attendance", ""))
        return {
            "ok": True,
            "mode": "week",
            "child_name": child["name"],
            "week_offset": week_offset,
            "period": diary.get("period") or target_monday.strftime("%d.%m.%Y"),
            "days": stats["days"],
            "total_lessons": stats["total_lessons"],
            "week_average": stats["period_average"],
            "marks_list": stats["marks_list"],
            "attendance": att["text"],
            "attendance_info": att,
            "quarter_average": q_data.get("average") or "",
            "quarter_behavior": q_data.get("behavior") or "",
        }

    if mode == "month":
        # Сдвигаем (год, месяц) на month_offset
        m_idx = (today.year * 12 + (today.month - 1)) + month_offset
        tyear, tmonth = m_idx // 12, (m_idx % 12) + 1
        last_day = calendar.monthrange(tyear, tmonth)[1]
        first_dt = date(tyear, tmonth, 1)
        last_dt = date(tyear, tmonth, last_day)

        # Собираем все понедельники недель, относящихся к этому месяцу
        mondays: List[date] = []
        cur = first_dt - timedelta(days=first_dt.weekday())
        while cur <= last_dt:
            thursday = cur + timedelta(days=3)
            if (cur.month == tmonth or thursday.month == tmonth) and cur not in mondays:
                mondays.append(cur)
            cur += timedelta(days=7)

        with ThreadPoolExecutor(max_workers=min(6, len(mondays) + 1)) as pool:
            f_qm = pool.submit(_get_quarter_marks_cached, client, pid, sub)
            f_weeks = [pool.submit(_fetch_week_for_date, client, pid, sub, q_info, md) for md in mondays]
            q_data = f_qm.result()
            raw_weeks = [fw.result() for fw in f_weeks]

        weeks_out = []
        total_lessons = 0
        all_marks = []
        all_nums = []
        missed_sum = 0
        unexcused_sum = 0
        seen_periods = set()

        for md, wk in zip(mondays, raw_weeks):
            per = (wk.get("period") or "").strip()
            if per and per in seen_periods:
                continue
            if per:
                seen_periods.add(per)
            stats = _enrich_days(wk.get("days", []))
            att = _parse_attendance(wk.get("attendance", ""))
            total_lessons += stats["total_lessons"]
            all_marks.extend(stats["marks_list"])
            all_nums.extend(stats["numeric_marks"])
            missed_sum += att["missed_total"]
            unexcused_sum += att["missed_unexcused"]
            weeks_out.append({
                "monday": md.strftime("%Y-%m-%d"),
                "period": per or md.strftime("%d.%m.%Y"),
                "total_lessons": stats["total_lessons"],
                "week_average": stats["period_average"],
                "missed_total": att["missed_total"],
                "days": stats["days"],
            })

        month_avg = round(sum(all_nums) / len(all_nums), 2) if all_nums else None
        return {
            "ok": True,
            "mode": "month",
            "child_name": child["name"],
            "month_offset": month_offset,
            "month_title": f"{RUSSIAN_MONTHS[tmonth]} {tyear}",
            "total_lessons": total_lessons,
            "month_average": month_avg,
            "marks_list": all_marks,
            "weeks": weeks_out,
            "attendance_info": {
                "missed_total": missed_sum,
                "missed_unexcused": unexcused_sum,
                "text": f"Пропущено за месяц: {missed_sum} ур. (неув.: {unexcused_sum})",
            },
            "quarter_average": q_data.get("average") or "",
            "quarter_behavior": q_data.get("behavior") or "",
        }

    # Режим 'day' / 'today' / 'tomorrow' (поддержка любого сдвига day_offset свайпом)
    effective_offset = 1 if (mode == "tomorrow" and day_offset == 0) else day_offset
    target_dt = today + timedelta(days=effective_offset)
    wd_name = RUSSIAN_WEEKDAYS[target_dt.weekday()]
    target_monday = target_dt - timedelta(days=target_dt.weekday())
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_wk = pool.submit(_fetch_week_for_date, client, pid, sub, q_info, target_monday)
        f_qm = pool.submit(_get_quarter_marks_cached, client, pid, sub)
        diary = f_wk.result()
        q_data = f_qm.result()

    stats = _enrich_days(diary.get("days", []))
    target_day = next((d for d in stats["days"] if wd_name in d.get("title", "").lower()), None)
    lessons = target_day["lessons"] if target_day else []
    att = _parse_attendance(diary.get("attendance", ""))
    fallback_title = f"{wd_name.capitalize()}, {target_dt.day} {RUSSIAN_MONTHS[target_dt.month].lower()}"
    return {
        "ok": True,
        "mode": mode,
        "day_offset": effective_offset,
        "target_date": target_dt.strftime("%d.%m.%Y"),
        "child_name": child["name"],
        "day_title": target_day.get("title", fallback_title) if target_day else fallback_title,
        "lessons": lessons,
        "attendance": att["text"],
        "attendance_info": att,
        "quarter_average": q_data.get("average") or "",
        "quarter_behavior": q_data.get("behavior") or "",
    }


async def api_get_diary(request: web.Request) -> web.Response:
    tg_id = _resolve_tg_id(request)
    q_cid = request.query.get("child_id")
    mode = request.query.get("mode", "day")
    try:
        week_offset = max(-12, min(12, int(request.query.get("week_offset", "0"))))
    except ValueError:
        week_offset = 0
    try:
        month_offset = max(-6, min(6, int(request.query.get("month_offset", "0"))))
    except ValueError:
        month_offset = 0
    try:
        day_offset = max(-30, min(30, int(request.query.get("day_offset", "0"))))
    except ValueError:
        day_offset = 0

    if not q_cid or not q_cid.isdigit():
        children = db.ensure_children_synced(tg_id)
        linked = next((c for c in children if c.get("pupil_id")), None)
        if not linked:
            return web.json_response({"ok": False, "no_schools_by": True, "error": "Нет привязанного дневника Schools.by"})
        cid = linked["id"]
    else:
        cid = int(q_cid)
    res = await asyncio.to_thread(_fetch_diary_sync, tg_id, cid, mode, week_offset, month_offset, day_offset)
    return web.json_response(res)


def create_app() -> web.Application:
    db.init_db()
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/themes", handle_themes)
    app.router.add_get("/manifest.json", handle_manifest)
    app.router.add_get("/icon.svg", handle_icon)
    app.router.add_get("/api/bootstrap", api_bootstrap)
    app.router.add_post("/api/children", api_add_child)
    app.router.add_delete("/api/children/{child_id}", api_delete_child)
    app.router.add_get("/api/erip/search", api_erip_search)
    app.router.add_get("/api/erip/inspect", api_erip_inspect)
    app.router.add_post("/api/erip/verify", api_erip_verify)
    app.router.add_post("/api/clubs", api_add_club)
    app.router.add_post("/api/clubs/refresh_all", api_refresh_all_clubs)
    app.router.add_post("/api/clubs/{club_id}/refresh", api_refresh_club)
    app.router.add_post("/api/clubs/{club_id}/confirm_pay", api_confirm_club_payment)
    app.router.add_post("/api/clubs/{club_id}/update", api_update_club)
    app.router.add_delete("/api/clubs/{club_id}", api_delete_club)
    app.router.add_get("/api/expenses", api_get_expenses)
    app.router.add_post("/api/expenses", api_add_expense)
    app.router.add_delete("/api/expenses/{expense_id}", api_delete_expense)
    app.router.add_get("/api/diary", api_get_diary)
    return app


def run_webapp_in_thread(host: str = "0.0.0.0", port: int = DEFAULT_PORT):
    """Run aiohttp PWA server inside a background thread alongside bot_tv.py."""
    async def _runner():
        app = create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info(f"PWA School Hub running on http://{host}:{port}")
        while True:
            await asyncio.sleep(3600)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_runner())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    print(f"Starting School Hub PWA on http://{args.host}:{args.port} ...")
    web.run_app(create_app(), host=args.host, port=args.port)
