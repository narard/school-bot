import html
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

ERIP_PAY_LIST_URL = "https://api.bepaid.by/beyag/komplat/v2/get_pay_list"
ERIP_HEADERS = {
    "Origin": "https://erip.paritetbank.by",
    "Referer": "https://erip.paritetbank.by/",
    "Content-Type": "application/json",
}

CATALOG_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "minsk_erip_clubs.json")
CATALOG_TTL_SECONDS = 7 * 24 * 3600

# Корневые ветки Минска с кружками, секциями и школьными услугами
MINSK_CLUB_BRANCHES = [
    ("Доп. образование", "11000892095"),
    ("Спорт и секции", "11000891420"),
    ("УО районов", "11000965562"),
]

# Карта школьных кружков в УО 9 районов Минска
MINSK_UO_SCHOOL_CLUBS: Dict[str, str] = {
    "Фрунзенский район": "10004739481",
    "Заводской район": "10000004081",
    "Ленинский район": "10005180261",
    "Московский район": "10004927181",
    "Октябрьский район": "10004342261",
    "Партизанский район": "10000379091",
    "Первомайский район": "10004335631",
    "Советский район": "10000404921",
    "Центральный район": "10000394291",
}

MINSK_KSHP_DISTRICTS: Dict[str, Dict[str, str]] = {
    "Фрунзенский район": {"canteen": "10004435821", "buffet": "10005160011"},
    "Заводской район": {"canteen": "10004435641", "buffet": "10005159941"},
    "Ленинский район": {"canteen": "10004435811", "buffet": "10005159951"},
    "Московский район": {"canteen": "10004436041", "buffet": "10005159961"},
    "Октябрьский район": {"canteen": "10004435801", "buffet": "10005159971"},
    "Партизанский район": {"canteen": "10004436051", "buffet": "10005159981"},
    "Первомайский район": {"canteen": "10004435791", "buffet": "10005159991"},
    "Советский район": {"canteen": "10004436061", "buffet": "10005160001"},
    "Центральный район": {"canteen": "10004436031", "buffet": "10005160021"},
}

DISTRICT_SHORT_KEYS: List[str] = list(MINSK_KSHP_DISTRICTS.keys())


def _parse_byn(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    cleaned = raw.replace(" ", "").replace(",", ".").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fmt_byn(val: Optional[float]) -> str:
    if val is None:
        return "—"
    return f"{val:.2f}".replace(".", ",") + " BYN"


def _parse_erip_information(info_text: str, summa_raw: Any) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {
        "balance": None,
        "daily_norm": None,
        "school_days": None,
        "saldo_start": None,
        "paid_period": None,
        "paid_period_dates": None,
        "to_pay": _parse_byn(str(summa_raw)) if summa_raw is not None else None,
        "raw_info": info_text or "",
    }
    if not info_text:
        return parsed

    m_bal = re.search(r"Баланс:\s*([+-]?\d+(?:[.,]\d+)?)", info_text, re.IGNORECASE)
    if m_bal:
        parsed["balance"] = _parse_byn(m_bal.group(1))

    m_norm = re.search(r"норма в день\s*=\s*([+-]?\d+(?:[.,]\d+)?)", info_text, re.IGNORECASE)
    if m_norm:
        parsed["daily_norm"] = _parse_byn(m_norm.group(1))

    m_days = re.search(r"учебных дней\s*=\s*(\d+)", info_text, re.IGNORECASE)
    if m_days:
        parsed["school_days"] = int(m_days.group(1))

    m_saldo = re.search(r"сальдо[^=]*=\s*([+-]?\d+(?:[.,]\d+)?)", info_text, re.IGNORECASE)
    if m_saldo:
        parsed["saldo_start"] = _parse_byn(m_saldo.group(1))

    m_paid = re.search(
        r"оплачено за период\s*([0-9.\-\s]+)\s*=\s*([+-]?\d+(?:[.,]\d+)?)",
        info_text,
        re.IGNORECASE,
    )
    if m_paid:
        parsed["paid_period_dates"] = m_paid.group(1).strip()
        parsed["paid_period"] = _parse_byn(m_paid.group(2))

    return parsed


def query_erip_service(
    pay_code: str,
    account_number: str,
    di_type: str = "9191",
    timeout: int = 12,
) -> Dict[str, Any]:
    """Perform the standard two-step read-only ERIP inquiry without starting any payment."""
    acc = account_number.strip()
    session = requests.Session()
    session.headers.update(ERIP_HEADERS)

    try:
        r1 = session.post(
            ERIP_PAY_LIST_URL,
            json={"version": "3", "test": False, "di_type": str(di_type), "pay_code": str(pay_code)},
            timeout=timeout,
        )
        r1.raise_for_status()
        d1 = r1.json()
    except Exception as e:
        logger.warning(f"ERIP step 1 failed ({pay_code}): {e}")
        return {"ok": False, "error": "Нет связи со шлюзом ЕРИП"}

    if str(d1.get("error_code", "")) != "0" or not isinstance(d1.get("pay_record"), dict):
        return {"ok": False, "error": d1.get("error_text") or "Услуга ЕРИП недоступна"}

    pr1 = d1["pay_record"]
    session_id = pr1.get("erip_session_id", "")
    attrs_out: List[Dict[str, Any]] = []
    for attr in pr1.get("attr_records") or []:
        is_editable = str(attr.get("edit", "0")) == "1"
        attrs_out.append(
            {
                "name": attr.get("name"),
                "code": attr.get("code"),
                "code_out": attr.get("code_out"),
                "value": acc if is_editable else attr.get("value"),
                "edit": attr.get("edit"),
                "change": "1" if is_editable else "0",
            }
        )

    try:
        r2 = session.post(
            ERIP_PAY_LIST_URL,
            json={
                "version": "3",
                "test": False,
                "di_type": str(pr1.get("di_type", di_type)),
                "pay_code": str(pr1.get("next_pay_code") or pr1.get("code") or pay_code),
                "erip_session_id": session_id,
                "attr_records": attrs_out,
            },
            timeout=timeout,
        )
        r2.raise_for_status()
        d2 = r2.json()
    except Exception as e:
        logger.warning(f"ERIP step 2 failed ({pay_code}): {e}")
        return {"ok": False, "error": "Таймаут при проверке счёта в ЕРИП"}

    if str(d2.get("error_code", "")) != "0" or not isinstance(d2.get("pay_record"), dict):
        err_msg = d2.get("error_text") or "Лицевой счёт не найден"
        return {"ok": False, "error": err_msg}

    pr2 = d2["pay_record"]
    fio_parts = []
    for attr in pr2.get("attr_records") or []:
        if attr.get("name") in ("Фамилия", "Имя", "Отчество") and attr.get("value"):
            fio_parts.append(str(attr["value"]).strip())

    parsed = _parse_erip_information(pr2.get("information", ""), pr2.get("summa"))
    parsed["ok"] = True
    parsed["fio"] = " ".join(fio_parts) if fio_parts else ""
    parsed["service_name"] = pr2.get("name", "")
    return parsed


def get_pupil_meal_status(district: str, account_number: str) -> Dict[str, Any]:
    """Query both school canteen ('Питание') and buffet ('Питание в буфете') in parallel."""
    codes = MINSK_KSHP_DISTRICTS.get(district) or MINSK_KSHP_DISTRICTS["Фрунзенский район"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_canteen = pool.submit(query_erip_service, codes["canteen"], account_number)
        f_buffet = pool.submit(query_erip_service, codes["buffet"], account_number)
        canteen = f_canteen.result()
        buffet = f_buffet.result()

    if not canteen.get("ok") and not buffet.get("ok"):
        return {
            "ok": False,
            "error": canteen.get("error") or buffet.get("error") or "Не удалось получить данные из ЕРИП",
            "canteen": canteen,
            "buffet": buffet,
        }

    fio = canteen.get("fio") or buffet.get("fio") or ""
    c_bal = canteen.get("balance")
    b_bal = buffet.get("balance")
    total_bal = None
    if c_bal is not None or b_bal is not None:
        total_bal = (c_bal or 0.0) + (b_bal or 0.0)

    return {
        "ok": True,
        "fio": fio,
        "canteen": canteen,
        "buffet": buffet,
        "total_balance": total_bal,
    }


def _balance_badge(balance: Optional[float], threshold: float) -> str:
    if balance is None:
        return "⚪"
    if balance < 0:
        return "🔴"
    if threshold > 0 and balance <= threshold:
        return "🟡"
    return "🟢"


def format_meal_message(
    label: str,
    district: str,
    account_number: str,
    status: Dict[str, Any],
    alert_threshold: float = 5.0,
) -> str:
    """Format a compact, readable Telegram HTML card with canteen and buffet balances."""
    safe_label = html.escape(label or "Ученик")
    safe_district = html.escape(district)
    safe_acc = html.escape(account_number)

    if not status.get("ok"):
        err = html.escape(str(status.get("error", "Ошибка ЕРИП")))
        return (
            f"🍽 <b>Питание — {safe_label}</b>\n"
            f"📍 {safe_district} • Счёт: <code>{safe_acc}</code>\n\n"
            f"❌ {err}"
        )

    fio = html.escape(status.get("fio") or "")
    canteen = status.get("canteen", {})
    buffet = status.get("buffet", {})

    lines = [f"🍽 <b>Школьное питание — {safe_label}</b>"]
    meta = f"📍 {safe_district} • <code>{safe_acc}</code>"
    if fio:
        meta += f"\n👤 ЕРИП: <code>{fio}</code>"
    lines.append(meta)

    if canteen.get("ok"):
        c_bal = canteen.get("balance")
        badge = _balance_badge(c_bal, alert_threshold)
        lines.append(f"\n🥗 <b>Столовая: {badge} {_fmt_byn(c_bal)}</b>")
        norm = canteen.get("daily_norm")
        if c_bal is not None and norm and norm > 0:
            days_left = max(int(c_bal // norm), 0)
            lines.append(f"• Норма в день: <b>{_fmt_byn(norm)}</b> (хватит на ~{days_left} дн.)")
        elif norm:
            lines.append(f"• Норма в день: <b>{_fmt_byn(norm)}</b>")
        details = []
        if canteen.get("saldo_start") is not None:
            details.append(f"на 1 число: {_fmt_byn(canteen['saldo_start'])}")
        if canteen.get("paid_period") is not None:
            p_dates = canteen.get("paid_period_dates")
            p_lbl = f"оплачено ({p_dates})" if p_dates else "оплачено"
            details.append(f"{p_lbl}: {_fmt_byn(canteen['paid_period'])}")
        if details:
            lines.append("• " + " • ".join(details))
        if canteen.get("to_pay") is not None and canteen["to_pay"] > 0:
            days_txt = f" ({canteen['school_days']} уч. дн.)" if canteen.get("school_days") else ""
            lines.append(f"• К оплате по расчёту КШП{days_txt}: <b>{_fmt_byn(canteen['to_pay'])}</b>")
    else:
        c_err = html.escape(str(canteen.get("error", "не найдена")))
        lines.append(f"\n🥗 <b>Столовая:</b> — <i>({c_err})</i>")

    if buffet.get("ok"):
        b_bal = buffet.get("balance")
        badge = _balance_badge(b_bal, alert_threshold)
        lines.append(f"\n🥐 <b>Буфет: {badge} {_fmt_byn(b_bal)}</b>")
    else:
        b_err = html.escape(str(buffet.get("error", "нет данных")))
        lines.append(f"\n🥐 <b>Буфет:</b> — <i>({b_err})</i>")

    total_bal = status.get("total_balance")
    if total_bal is not None and canteen.get("ok") and buffet.get("ok"):
        lines.append(f"\n💰 <b>Всего на двух счетах: {_fmt_byn(total_bal)}</b>")

    lines.append(
        f"\n<i>Путь ЕРИП: Образование → Организации шк. питания → Минск → КШП → {safe_district}</i>"
    )
    return "\n".join(lines)


def _post_pay_list(session: requests.Session, code: str, di_type: str = "9120", timeout: int = 12) -> Any:
    r = session.post(
        ERIP_PAY_LIST_URL,
        json={"version": "3", "test": False, "di_type": str(di_type), "pay_code": str(code)},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json().get("pay_record")


def sync_minsk_clubs_catalog(force: bool = False) -> List[Dict[str, Any]]:
    """Download and cache Minsk club/education/sport organizations from ERIP."""
    if not force and os.path.exists(CATALOG_CACHE_PATH):
        try:
            age = time.time() - os.path.getmtime(CATALOG_CACHE_PATH)
            if age < CATALOG_TTL_SECONDS:
                with open(CATALOG_CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list) and data:
                    return data
        except Exception:
            pass

    session = requests.Session()
    session.headers.update(ERIP_HEADERS)
    catalog: List[Dict[str, Any]] = []
    seen_codes = set()

    # 1. Школьные кружки УО 9 районов Минска
    for dist_name, club_code in MINSK_UO_SCHOOL_CLUBS.items():
        short_d = dist_name.replace(" район", "")
        catalog.append(
            {
                "code": club_code,
                "di_type": "9191",
                "name": f"Школьные кружки — УО {short_d} р-н",
                "category": "Школьные кружки (УО)",
                "is_leaf": True,
            }
        )
        seen_codes.add(club_code)

    # 2. Обходим ветки Доп. образования и Спорта -> Минск
    sub_tasks = []
    for root_label, root_code in [("Доп. образование", "11000892095"), ("Спорт", "11000891420")]:
        try:
            cats = _post_pay_list(session, root_code, "9120")
            if isinstance(cats, list):
                for cat in cats:
                    sub_tasks.append((root_label, cat))
        except Exception as e:
            logger.warning(f"Catalog fetch root {root_label} error: {e}")

    def fetch_minsk_orgs(task):
        root_label, cat = task
        res_orgs = []
        try:
            regions = _post_pay_list(session, cat["code"], cat.get("di_type", "9120"))
            if not isinstance(regions, list):
                return []
            minsk_nodes = [x for x in regions if x.get("name") == "Минск"]
            for mnode in minsk_nodes:
                orgs = _post_pay_list(session, mnode["code"], mnode.get("di_type", "9120"))
                if isinstance(orgs, list):
                    for org in orgs:
                        res_orgs.append(
                            {
                                "code": str(org.get("code", "")),
                                "di_type": str(org.get("di_type", "9120")),
                                "name": str(org.get("name", "")).strip(),
                                "category": f"{root_label}: {cat.get('name', '')}",
                                "is_leaf": str(org.get("di_type", "")) == "9191",
                            }
                        )
        except Exception as e:
            logger.warning(f"Catalog branch {cat.get('name')} error: {e}")
        return res_orgs

    with ThreadPoolExecutor(max_workers=6) as pool:
        for batch in pool.map(fetch_minsk_orgs, sub_tasks):
            for item in batch:
                if item["code"] and item["code"] not in seen_codes:
                    seen_codes.add(item["code"])
                    catalog.append(item)

    if catalog:
        try:
            with open(CATALOG_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(catalog, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to write ERIP catalog cache: {e}")

    return catalog


def search_minsk_clubs(query: str, limit: int = 25) -> List[Dict[str, Any]]:
    """Search cached Minsk ERIP clubs catalog by substring tokens."""
    catalog = sync_minsk_clubs_catalog(force=False)
    q = (query or "").strip().lower()
    if not q:
        return catalog[:limit]
    tokens = [t for t in re.split(r"\s+", q) if t]
    hits = []
    for item in catalog:
        hay = f"{item.get('name', '')} {item.get('category', '')}".lower()
        if all(tok in hay for tok in tokens):
            hits.append(item)
            if len(hits) >= limit:
                break
    return hits


def inspect_erip_entry(code: str, di_type: str = "9120") -> Dict[str, Any]:
    """Inspect a selected ERIP organization or leaf service to discover services and required input field."""
    session = requests.Session()
    session.headers.update(ERIP_HEADERS)
    try:
        pr = _post_pay_list(session, code, di_type)
    except Exception as e:
        return {"ok": False, "error": f"Ошибка ЕРИП: {e}"}

    if isinstance(pr, list):
        services = [
            {
                "code": str(x.get("code", "")),
                "di_type": str(x.get("di_type", "9120")),
                "name": str(x.get("name", "")).strip(),
            }
            for x in pr
        ]
        # Если внутри 1 конкретная услуга — сразу раскрываем её поля
        if len(services) == 1 and services[0]["di_type"] == "9191":
            return inspect_erip_entry(services[0]["code"], "9191")
        return {"ok": True, "type": "list", "services": services}

    if isinstance(pr, dict):
        attrs = pr.get("attr_records") or []
        editable = [a for a in attrs if str(a.get("edit", "0")) == "1"]
        field_name = editable[0].get("name") if editable else "Лицевой счёт / договор"
        hint = editable[0].get("hint") or pr.get("information") or ""
        return {
            "ok": True,
            "type": "leaf",
            "service": {
                "code": str(pr.get("code", code)),
                "di_type": str(pr.get("di_type", "9191")),
                "name": str(pr.get("name", "Услуга")),
                "field_name": field_name,
                "hint": hint.strip(),
                "editable_count": len(editable),
            },
        }

    return {"ok": False, "error": "Неизвестный ответ ЕРИП"}


def get_school_club_balance(district: str, account_number: str) -> Dict[str, Any]:
    """Query district UO school club ('Кружки для учащихся') using the pupil ERIP number."""
    code = MINSK_UO_SCHOOL_CLUBS.get(district) or MINSK_UO_SCHOOL_CLUBS["Фрунзенский район"]
    return query_erip_service(code, account_number)
