from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from typing import List, Dict, Any

def get_main_keyboard(has_multiple_pupils: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [
        [
            KeyboardButton(text="📅 Сегодня"),
            KeyboardButton(text="📆 Завтра")
        ],
        [
            KeyboardButton(text="🗓 Вся неделя"),
            KeyboardButton(text="📊 Четвертные оценки")
        ],
        [
            KeyboardButton(text="🌤 Погода"),
            KeyboardButton(text="💱 Курсы валют")
        ],
        [
            KeyboardButton(text="🍿 Что посмотреть"),
            KeyboardButton(text="⚙️ Настройки")
        ]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_settings_keyboard(has_multiple_pupils: bool = False, notify_on: bool = True) -> InlineKeyboardMarkup:
    notify_text = "🔔 Уведомления: ВКЛ" if notify_on else "🔕 Уведомления: ВЫКЛ"
    buttons = [
        [InlineKeyboardButton(text=notify_text, callback_data="toggle_notify")]
    ]
    if has_multiple_pupils:
        buttons.append([InlineKeyboardButton(text="🔄 Сменить ребёнка", callback_data="switch_pupil")])
    buttons.append([InlineKeyboardButton(text="🚪 Выйти из аккаунта", callback_data="logout")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_pupils_selection_keyboard(pupils: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    buttons = []
    for p in pupils:
        buttons.append([InlineKeyboardButton(text=f"👤 {p['name']}", callback_data=f"sel_pupil:{p['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_movie_categories_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="👻 Ужасы", callback_data="movie:horror"),
            InlineKeyboardButton(text="🔥 Свежие новинки", callback_data="movie:new_hd")
        ],
        [
            InlineKeyboardButton(text="🍿 Триллеры", callback_data="movie:thriller"),
            InlineKeyboardButton(text="👨‍👩‍👧 Семейное / Мультфильмы", callback_data="movie:family")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_movie_card_keyboard(watch_url: str, category: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="▶️ Смотреть на rezka-ua.tv", url=watch_url)],
        [
            InlineKeyboardButton(text="🎲 Другой фильм", callback_data=f"movie:{category}"),
            InlineKeyboardButton(text="📂 Другой жанр", callback_data="movie_genres")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
