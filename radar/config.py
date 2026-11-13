import os

BOT_TOKEN = os.environ.get("RADAR_BOT_TOKEN", "9018827965:AAE60roT9VNh-gNLLOiXpFnLgkEZpcBSdmg")
DEFAULT_ADMIN_ID = 211785022

AI_PROXY_URL = "https://school-bot-zbh4.onrender.com/api/gemini"
AI_HEALTH_URL = "https://school-bot-zbh4.onrender.com/health"

# Расписание по умолчанию (Минск, UTC+3)
DEFAULT_MORNING_TIME = "08:30"
DEFAULT_EVENING_TIME = "19:30"

# 5 ключевых рубрик ИИ-Радара
CATEGORIES = {
    "news": "🔥 Новости и релизы ИИ",
    "apps": "🚀 Новые приложения, быт и идеи",
    "money": "💰 Заработок: YouTube/TikTok, услуги, быт, бизнес",
    "github": "🧩 GitHub, скиллы агентов и тулзы",
    "prompts": "🧠 Промпты, сценарии и лайфхаки",
}

# Стартовая база публичных Telegram-каналов (парсятся через t.me/s/<username> без аккаунта)
DEFAULT_TG_CHANNELS = [
    ("ai_newz", "AI News — новости и релизы ИИ", "news"),
    ("gonzo_ML", "Gonzo ML — разборы моделей и статей", "news"),
    ("denissexy", "Denis Sexy IT — инсайды ИИ, агенты, треды из X", "news"),
    ("seeallochnaya", "Сиолошная — свежие релизы и ИИ-тулзы", "apps"),
    ("cgevent", "Нейросети, генеративка и пайплайны", "apps"),
    ("pechenka_ai", "Печенька ИИ — новые нейросети и сервисы", "apps"),
    ("dnative_ru", "DNative — маркетинг, контент и заработок с ИИ", "money"),
    ("vcnews", "VC.ru — стартапы, кейсы и бизнес", "money"),
    ("habr_com", "Хабр — лучшие статьи и практика", "money"),
    ("chatgpt_prompters", "Промпт-инжиниринг и готовые промпты", "prompts"),
    ("burningskills", "Новые скиллы, автоматизация и ИИ-боты", "prompts"),
    ("neuro_channel", "Нейро-канал — практика применения ИИ", "apps"),
    ("ai_machinelearning_big_data", "Machine Learning & AI Tools", "github"),
    ("llm_under_hood", "LLM под капотом — агенты и опенсорс", "github"),
    ("tproger", "Tproger — разработка и ИИ-инструменты", "github"),
    ("sberlogabig", "Сберлога — исследования и тренды ИИ", "news"),
    ("techno_yandex", "Яндекс про технологии и нейросети", "news"),
    ("aidigest", "AI Digest — выжимки мировых трендов", "news"),
    ("n8n_ru", "Автоматизация и ИИ-агенты (n8n / сценарии)", "prompts"),
    ("cursor_ide_ru", "Cursor / Claude Code / вайб-кодинг", "github"),
    ("neiroseti_praktika", "Нейросети на практике: быт, контент, видео", "money"),
    ("ai_video_news", "Генерация видео, озвучка, аватары для YouTube/TikTok", "money"),
]

# Мировые RSS и агрегаторы (X/Twitter треды, Product Hunt, Habr, VC, Simon Willison и др.)
RSS_FEEDS = [
    # Агрегаторы AI Twitter (X), Discord и мировых релизов
    {
        "name": "Smol AI (X/Twitter AI Threads)",
        "url": "https://news.smol.ai/rss.xml",
        "category": "news",
    },
    {
        "name": "TechCrunch AI",
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "category": "news",
    },
    {
        "name": "The Verge AI",
        "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "category": "news",
    },
    {
        "name": "OpenAI News",
        "url": "https://openai.com/news/rss.xml",
        "category": "news",
    },
    # Новые приложения и стартап-идеи
    {
        "name": "Product Hunt (Новые продукты)",
        "url": "https://www.producthunt.com/feed",
        "category": "apps",
    },
    # Практика, скиллы агентов, промпты и LLM-инструменты
    {
        "name": "Simon Willison (LLM, Skills, MCP, Prompts)",
        "url": "https://simonwillison.net/atom/everything/",
        "category": "prompts",
    },
    # Русскоязычные статьи и кейсы внедрения / заработка
    {
        "name": "Habr — Искусственный интеллект",
        "url": "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/?fl=ru",
        "category": "money",
    },
    {
        "name": "Habr — Машинное обучение и практика",
        "url": "https://habr.com/ru/rss/hubs/machine_learning/articles/?fl=ru",
        "category": "prompts",
    },
    {
        "name": "VC.ru — ИИ и кейсы заработка",
        "url": "https://vc.ru/rss/all",
        "category": "money",
    },
    {
        "name": "DEV.to — AI Tutorials & Prompts",
        "url": "https://dev.to/feed/tag/ai",
        "category": "prompts",
    },
    {
        "name": "DEV.to — LLM & Agents Practice",
        "url": "https://dev.to/feed/tag/llm",
        "category": "money",
    },
]

# Сабреддиты для парсинга реальных кейсов заработка (контент, YouTube/TikTok, быт, бизнес), агентов и промптов
REDDIT_SOURCES = [
    ("ChatGPT", "apps", "Reddit r/ChatGPT (Нестандартное применение ИИ в жизни и быту)"),
    ("aivideo", "money", "Reddit r/aivideo (ИИ-видео для YouTube, TikTok и Reels)"),
    ("EntrepreneurRideAlong", "money", "Reddit r/Entrepreneur (Реальные бизнесы и заработок в разных сферах)"),
    ("SideProject", "apps", "Reddit r/SideProject (Новые проекты, приложения и идеи)"),
    ("microsaas", "money", "Reddit r/MicroSaaS (Монетизация микро-продуктов)"),
    ("AI_Agents", "github", "Reddit r/AI_Agents (ИИ-агенты и автоматизация рутины)"),
    ("LocalLLaMA", "news", "Reddit r/LocalLLaMA (Новые открытые модели, голос, видео)"),
    ("PromptEngineering", "prompts", "Reddit r/PromptEngineering (Промпты и пайплайны)"),
    ("ChatGPTPromptGenius", "prompts", "Reddit r/ChatGPTPromptGenius (Готовые промпты для жизни и работы)"),
]
