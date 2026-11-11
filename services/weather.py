import requests
from typing import Dict, Any, Optional

WEATHER_CODES = {
    0: ("Ясно ☀️", "Отличная солнечная погода!"),
    1: ("В основном ясно 🌤", "Хорошая погода с легкими облаками."),
    2: ("Переменная облачность ⛅️", "Облачно с прояснениями."),
    3: ("Пасмурно ☁️", "Серое небо, без существенных осадков."),
    45: ("Туман 🌫", "Видимость снижена, будьте внимательны на дорогах."),
    48: ("Изморозь ❄️", "Возможен гололед на дорогах."),
    51: ("Небольшая морось 🌦", "Моросящий дождь, может понадобиться зонт."),
    53: ("Морось 🌦", "Сыро и промозгло."),
    55: ("Плотная морось 🌧", "Возьмите зонт!"),
    61: ("Небольшой дождь 🌧", "Обязательно возьмите зонт!"),
    63: ("Умеренный дождь 🌧", "Идет дождь, захватите зонт и непромокаемую обувь!"),
    65: ("Сильный дождь ⛈", "Ливень! Зонт и сапоги обязательны."),
    71: ("Небольшой снег 🌨", "Идет легкий снежок."),
    73: ("Снегопад ❄️", "Снежная погода, одевайтесь теплее!"),
    75: ("Сильный снегопад 🌨", "Метель и сугробы."),
    77: ("Снежные зерна 🌨", "Снежная крупа."),
    80: ("Кратковременный дождь 🌦", "Возможен внезапный дождь."),
    81: ("Ливневый дождь 🌧", "Возможны сильные порывы дождя."),
    82: ("Шквалистый ливень ⛈", "Штормовой дождь."),
    85: ("Снегопад 🌨", "Снежные осадки."),
    86: ("Сильный снегопад ❄️", "Обильный снег."),
    95: ("Гроза ⚡️", "Осторожно, гроза!")
}

def get_minsk_weather() -> Dict[str, Any]:
    """
    Получает актуальный прогноз погоды для Минска
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=53.9006&longitude=27.5590"
        "&current_weather=true"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode"
        "&timezone=Europe%2FMinsk"
    )
    try:
        resp = requests.get(url, timeout=6)
        if resp.status_code != 200:
            return {"success": False, "error": "Сервер погоды временно недоступен"}

        data = resp.json()
        current = data.get("current_weather", {})
        daily = data.get("daily", {})

        curr_temp = round(current.get("temperature", 0))
        wind_speed = round(current.get("windspeed", 0))
        code = current.get("weathercode", 0)

        t_max = round(daily.get("temperature_2m_max", [curr_temp])[0])
        t_min = round(daily.get("temperature_2m_min", [curr_temp])[0])
        precip = daily.get("precipitation_sum", [0])[0]

        desc, tip = WEATHER_CODES.get(code, ("Облачно ⛅️", "Хорошего дня!"))

        # Советы по одежде
        clothing_tips = []
        if curr_temp < 0:
            clothing_tips.append("🧣 Зимняя куртка, шапка и перчатки обязательны!")
        elif curr_temp < 6:
            clothing_tips.append("🧥 Теплая куртка и шапка, на улице прохладно.")
        elif curr_temp < 13:
            clothing_tips.append("🧥 Демисезонная куртка или пальто.")
        else:
            clothing_tips.append("👕 Комфортная теплая кофта или легкая куртка.")

        if precip > 0.5 or code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
            clothing_tips.append("☔️ Не забудьте зонт, на улице сыро/дождь!")

        if wind_speed > 25:
            clothing_tips.append("💨 Порывистый ветер, застегнитесь поплотнее.")

        return {
            "success": True,
            "curr_temp": curr_temp,
            "t_min": t_min,
            "t_max": t_max,
            "wind_speed": wind_speed,
            "description": desc,
            "advice": " ".join(clothing_tips)
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

def format_weather_message(morning_alert: bool = False) -> str:
    w = get_minsk_weather()
    if not w.get("success"):
        return "❌ Не удалось получить данные о погоде в Минске."

    prefix = "🌅 <b>Доброе утро! Погода в Минске (06:40):</b>" if morning_alert else "🌤 <b>Погода в Минске прямо сейчас:</b>"

    lines = [
        prefix,
        f"\n🌡 Температура: <b>{w['curr_temp']:+d}°C</b> (днем от {w['t_min']:+d}°C до {w['t_max']:+d}°C)",
        f"Состояние: <b>{w['description']}</b>",
        f"Ветер: <b>{w['wind_speed']} км/ч</b>",
        f"\n💡 <i>Совет: {w['advice']}</i>"
    ]
    return "\n".join(lines)
