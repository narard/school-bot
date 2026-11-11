import requests
from datetime import datetime
from typing import Dict, Any

def get_nbrb_rates() -> Dict[str, Any]:
    """
    Получает официальные курсы валют Нацбанка Республики Беларусь
    """
    url = "https://api.nbrb.by/exrates/rates?periodicity=0"
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200:
            return {"success": False, "error": "Сервер НБРБ временно недоступен"}

        data = resp.json()
        rates = {}
        for r in data:
            abbr = r.get("Cur_Abbreviation")
            if abbr in ("USD", "EUR", "RUB", "CNY"):
                rates[abbr] = {
                    "rate": r.get("Cur_OfficialRate"),
                    "scale": r.get("Cur_Scale", 1),
                    "name": r.get("Cur_Name")
                }

        return {"success": True, "rates": rates, "date": datetime.now().strftime("%d.%m.%Y")}
    except Exception as e:
        return {"success": False, "error": str(e)}

def format_currency_message() -> str:
    res = get_nbrb_rates()
    if not res.get("success"):
        return "❌ Не удалось загрузить курсы валют НБРБ."

    rates = res["rates"]
    usd = rates.get("USD", {}).get("rate", "—")
    eur = rates.get("EUR", {}).get("rate", "—")
    rub = rates.get("RUB", {}).get("rate", "—")
    cny = rates.get("CNY", {}).get("rate", "—")

    text = (
        f"💱 <b>Официальные курсы валют НБРБ ({res['date']}):</b>\n\n"
        f"🇺🇸 <b>1 USD</b> = <code>{usd:.4f}</code> BYN\n"
        f"🇪🇺 <b>1 EUR</b> = <code>{eur:.4f}</code> BYN\n"
        f"🇷🇺 <b>100 RUB</b> = <code>{rub:.4f}</code> BYN\n"
        f"🇨🇳 <b>10 CNY</b> = <code>{cny:.4f}</code> BYN\n"
    )
    return text
