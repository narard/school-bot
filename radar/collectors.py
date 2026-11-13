import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import re
import html
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any

import requests
from bs4 import BeautifulSoup

try:
    from radar.config import RSS_FEEDS, REDDIT_SOURCES
    from radar import database as db
except ImportError:
    from config import RSS_FEEDS, REDDIT_SOURCES
    import database as db

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

AI_KEYWORDS = re.compile(
    r"\b(ai|ии|нейросет|gpt|claude|gemini|llm|agent|агент|mcp|prompt|промпт|"
    r"cursor|openai|anthropic|deepseek|qwen|flux|midjourney|sora|kling|runway|elevenlabs|"
    r"youtube|tiktok|shorts|reels|видео|озвучк|аватар|контент|монетизац|"
    r"saas|bot|бот|автоматизац|rag|open-source|skill|скилл|copilot|n8n)\b",
    re.IGNORECASE,
)

AD_MARKERS = re.compile(
    r"(erid\s*[:=]|#реклама|#промо|реклама\.\s*ооо|инн\s*\d{10}|купить курс|скидка\s*\d+% на курс)",
    re.IGNORECASE,
)


def clean_html_text(raw: str) -> str:
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def auto_categorize(title: str, text: str, default_cat: str) -> str:
    combined = f"{title} {text}".lower()
    if any(k in combined for k in ["prompt", "промпт", "system prompt", "гайд", "инструкц", "n8n", "воркфлоу", "workflow"]):
        return "prompts"
    if any(k in combined for k in ["github.com", "mcp", "claude code", "cursor", "open-source", "опенсорс", "репозитор", "skill", "скилл"]):
        return "github"
    if any(k in combined for k in ["mrr", "arr", "$", "выручк", "заработ", "монетизац", "кейс", "внедрен", "окупаем", "клиент", "продаж"]):
        return "money"
    if any(k in combined for k in ["show hn", "product hunt", "запустил", "сделал приложение", "стартап", "сервис", "приложени", "launched"]):
        return "apps"
    return default_cat


# ---------------------------------------------------------------------------
# 1. GITHUB: Трендовые репозитории, открытые скиллы, MCP-серверы и ИИ-агенты
# ---------------------------------------------------------------------------
def fetch_github_items() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    date_7d = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    date_14d = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d")

    queries = [
        # Открытые скиллы, MCP-серверы, правила для Cursor/Claude
        (
            f"(mcp-server OR claude-skills OR agent-skills OR mcp) created:>{date_14d}",
            "github",
            "GitHub (Skills & MCP)",
        ),
        # Самые быстрорастущие новые AI-инструменты и агенты за 7 дней
        (
            f"(ai-agent OR llm OR ai-tools OR automation) created:>{date_7d}",
            "github",
            "GitHub Trending AI",
        ),
        # Новые открытые промпты, системные промпты и правила для ИИ-кодинга
        (
            f"(prompt OR prompts OR cursor-rules OR system-prompt) created:>{date_14d}",
            "prompts",
            "GitHub (Prompts & Rules)",
        ),
        # Генерация контента: YouTube Shorts, TikTok, AI-видео, озвучка, аватары
        (
            f"(tiktok OR youtube-shorts OR ai-video OR tts OR voice-cloning OR faceless) created:>{date_14d}",
            "money",
            "GitHub (YouTube/TikTok & AI Video)",
        ),
    ]

    gh_headers = {
        "User-Agent": "AIRadarBot/1.0",
        "Accept": "application/vnd.github.v3+json",
    }

    for q, cat, source_name in queries:
        try:
            resp = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": q, "sort": "stars", "order": "desc", "per_page": 10},
                headers=gh_headers,
                timeout=10,
            )
            if resp.status_code == 200:
                for repo in resp.json().get("items", []):
                    url = repo.get("html_url", "")
                    full_name = repo.get("full_name", "")
                    desc = repo.get("description") or "Новый AI-репозиторий на GitHub"
                    stars = repo.get("stargazers_count", 0)
                    lang = repo.get("language") or "AI"
                    topics = ", ".join(repo.get("topics", [])[:6])
                    title = f"{full_name} (⭐ {stars})"
                    raw_text = (
                        f"Репозиторий: {full_name} | Звёзд: {stars} | Язык: {lang}. "
                        f"Описание: {desc}. Теги: {topics}"
                    )
                    items.append(
                        {
                            "title": title,
                            "url": url,
                            "raw_text": raw_text,
                            "category": cat,
                            "source_name": source_name,
                            "score": min(100, 55 + stars // 5),
                        }
                    )
        except Exception as e:
            logger.warning(f"GitHub collector error ({source_name}): {e}")

    return items


# ---------------------------------------------------------------------------
# 2. HACKER NEWS: Show HN (новые приложения) + Топ мировых релизов ИИ
# ---------------------------------------------------------------------------
def fetch_hackernews_items() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    ts_48h = int((datetime.utcnow() - timedelta(hours=48)).timestamp())

    queries = [
        # Show HN — какие новые приложения сделали разработчики
        (
            "https://hn.algolia.com/api/v1/search_by_date",
            {"query": "Show HN AI", "tags": "show_hn", "numericFilters": f"created_at_i>{ts_48h},points>3", "hitsPerPage": 10},
            "apps",
            "Hacker News (Show HN)",
        ),
        # Главные AI-истории и релизы
        (
            "https://hn.algolia.com/api/v1/search",
            {"query": "AI OR LLM OR Claude OR GPT OR MCP OR Agent", "tags": "story", "numericFilters": f"created_at_i>{ts_48h},points>25", "hitsPerPage": 12},
            "news",
            "Hacker News Top AI",
        ),
    ]

    for api_url, params, cat, source_name in queries:
        try:
            resp = requests.get(api_url, params=params, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                for hit in resp.json().get("hits", []):
                    title = hit.get("title") or ""
                    if not title:
                        continue
                    url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                    points = hit.get("points", 0)
                    comments = hit.get("num_comments", 0)
                    story_text = clean_html_text(hit.get("story_text") or "")
                    raw_text = f"{title}. {story_text} (Рейтинг HN: {points}, комментариев: {comments})"
                    final_cat = auto_categorize(title, story_text, cat)
                    items.append(
                        {
                            "title": title,
                            "url": url,
                            "raw_text": raw_text[:1500],
                            "category": final_cat,
                            "source_name": source_name,
                            "score": min(100, 45 + points // 4),
                        }
                    )
        except Exception as e:
            logger.warning(f"HN collector error ({source_name}): {e}")

    return items


# ---------------------------------------------------------------------------
# 3. REDDIT: Кейсы заработка (MicroSaaS, SideProject), Агенты и Промпты
# ---------------------------------------------------------------------------
def fetch_single_subreddit(sub_name: str, cat: str, source_name: str) -> List[Dict[str, Any]]:
    sub_items: List[Dict[str, Any]] = []
    # Пробуем RSS сабреддита (работает стабильно без OAuth) и JSON как резерв
    rss_url = f"https://www.reddit.com/r/{sub_name}/hot/.rss?limit=10"
    try:
        resp = requests.get(rss_url, headers={"User-Agent": "Mozilla/5.0 (compatible; AIRadarBot/2.0)"}, timeout=10)
        if resp.status_code == 200 and "<entry>" in resp.text:
            root = ET.fromstring(resp.content)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("atom:entry", ns)[:8]:
                title_el = entry.find("atom:title", ns)
                link_el = entry.find("atom:link", ns)
                content_el = entry.find("atom:content", ns)
                title = title_el.text.strip() if title_el is not None and title_el.text else ""
                url = link_el.attrib.get("href", "") if link_el is not None else ""
                body = clean_html_text(content_el.text if content_el is not None and content_el.text else "")
                if not title or len(title) < 12 or "megathread" in title.lower() or "welcome to r/" in title.lower():
                    continue
                final_cat = auto_categorize(title, body, cat)
                sub_items.append(
                    {
                        "title": title,
                        "url": url,
                        "raw_text": f"{title}. {body[:1200]}",
                        "category": final_cat,
                        "source_name": source_name,
                        "score": 68,
                    }
                )
    except Exception as e:
        logger.debug(f"Reddit RSS error ({sub_name}): {e}")
    return sub_items


# ---------------------------------------------------------------------------
# 4. МИРОВЫЕ И РУССКОЯЗЫЧНЫЕ RSS (Smol AI X/Twitter, Product Hunt, Habr, VC)
# ---------------------------------------------------------------------------
def _local_tag(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def fetch_single_rss(feed: Dict[str, str]) -> List[Dict[str, Any]]:
    feed_items: List[Dict[str, Any]] = []
    name = feed["name"]
    url = feed["url"]
    default_cat = feed["category"]

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return []

        root = ET.fromstring(resp.content)
        entries = [el for el in root.iter() if _local_tag(el.tag) in ("item", "entry")]

        for entry in entries[:10]:
            title = ""
            link = ""
            desc = ""

            for child in entry:
                tname = _local_tag(child.tag)
                if tname == "title" and not title:
                    title = clean_html_text(child.text or "")
                elif tname == "link" and not link:
                    link = (child.attrib.get("href") or child.text or "").strip()
                elif tname == "guid" and not link and (child.text or "").startswith("http"):
                    link = (child.text or "").strip()
                elif tname in ("description", "summary", "content", "encoded") and len(child.text or "") > len(desc):
                    desc = clean_html_text(child.text or "")

            if not title or not link:
                continue

            if AD_MARKERS.search(f"{title} {desc}"):
                continue

            # Для общих лент (VC.ru) проверяем релевантность теме ИИ/стартапов
            if "vc.ru" in url and not AI_KEYWORDS.search(f"{title} {desc}"):
                continue

            # Для специализированных лент сохраняем их целевую рубрику
            if default_cat in ("apps", "prompts", "money"):
                final_cat = default_cat
            else:
                final_cat = auto_categorize(title, desc, default_cat)

            feed_items.append(
                {
                    "title": title[:200],
                    "url": link,
                    "raw_text": f"{title}. {desc[:1400]}",
                    "category": final_cat,
                    "source_name": name,
                    "score": 75 if any(x in name for x in ("Smol", "Habr", "Product Hunt", "Simon")) else 68,
                }
            )
    except Exception as e:
        logger.debug(f"RSS error ({name}): {e}")

    return feed_items


# ---------------------------------------------------------------------------
# 5. ПУБЛИЧНЫЕ TELEGRAM-КАНАЛЫ (через веб-превью t.me/s/<channel>)
# ---------------------------------------------------------------------------
def fetch_single_tg_channel(ch: Dict[str, Any]) -> List[Dict[str, Any]]:
    username = ch["username"]
    ch_title = ch.get("title") or f"@{username}"
    default_cat = ch.get("category") or "news"
    tg_items: List[Dict[str, Any]] = []

    try:
        url = f"https://t.me/s/{username}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        wraps = soup.select(".tgme_widget_message_wrap")
        # Берём последние 4 поста канала
        for wrap in wraps[-4:]:
            msg_div = wrap.select_one(".tgme_widget_message")
            text_div = wrap.select_one(".tgme_widget_message_text")
            if not msg_div or not text_div:
                continue

            post_id = msg_div.get("data-post", "")
            post_url = f"https://t.me/{post_id}" if post_id else url
            raw_text = clean_html_text(str(text_div))

            # Отсекаем слишком короткие посты и рекламу
            if len(raw_text) < 90 or AD_MARKERS.search(raw_text):
                continue

            # Для широких IT-каналов оставляем только посты про ИИ / разработку / промпты / сервисы
            if username in ("vcnews", "habr_com", "tproger", "dnative_ru") and not AI_KEYWORDS.search(raw_text):
                continue

            first_line = raw_text.split(".")[0][:120].strip()
            if len(first_line) < 20:
                first_line = raw_text[:100].strip() + "..."

            final_cat = auto_categorize(first_line, raw_text, default_cat)
            tg_items.append(
                {
                    "title": first_line,
                    "url": post_url,
                    "raw_text": raw_text[:1500],
                    "category": final_cat,
                    "source_name": f"TG @{username}",
                    "score": 70,
                }
            )
    except Exception as e:
        logger.debug(f"TG channel error (@{username}): {e}")

    return tg_items


# ---------------------------------------------------------------------------
# ЕДИНЫЙ ПАРАЛЛЕЛЬНЫЙ СБОРЩИК ВСЕХ ИСТОЧНИКОВ
# ---------------------------------------------------------------------------
def collect_all_fresh_items(target_categories: List[str] = None, ignore_seen: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    """
    Параллельно опрашивает GitHub, Hacker News, Reddit, мировые RSS, Habr, VC и 20+ ТГ-каналов.
    Отсекает дубли и уже показанные материалы.
    Возвращает словарь {category: [items...]}.
    """
    all_raw: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [
            pool.submit(fetch_github_items),
            pool.submit(fetch_hackernews_items),
        ]
        for feed in RSS_FEEDS:
            futures.append(pool.submit(fetch_single_rss, feed))
        for sub_name, cat, src_name in REDDIT_SOURCES:
            futures.append(pool.submit(fetch_single_subreddit, sub_name, cat, src_name))
        for ch in db.get_active_channels():
            futures.append(pool.submit(fetch_single_tg_channel, ch))

        for fut in as_completed(futures):
            try:
                res = fut.result()
                if res:
                    all_raw.extend(res)
            except Exception as e:
                logger.debug(f"Collector future exception: {e}")

    # Дедупликация внутри текущего сбора и по базе seen_items
    by_cat: Dict[str, List[Dict[str, Any]]] = {
        "news": [],
        "apps": [],
        "money": [],
        "github": [],
        "prompts": [],
    }
    seen_local = set()

    for item in all_raw:
        url = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip()
        cat = item.get("category", "news")
        if not url or not title:
            continue
        if target_categories and cat not in target_categories:
            continue

        norm_key = db.normalize_url(url)
        title_key = db.normalize_title(title)
        if norm_key in seen_local or (title_key and title_key in seen_local):
            continue
        if not ignore_seen and db.is_item_seen(url, title):
            continue

        seen_local.add(norm_key)
        if title_key:
            seen_local.add(title_key)

        by_cat.setdefault(cat, []).append(item)

    # Если все свежие материалы уже были прочитаны (например, пользователь нажал "Обновить" несколько раз подряд),
    # подгружаем материалы без фильтра seen_items, чтобы дайджест никогда не был пустым
    total_found = sum(len(v) for v in by_cat.values())
    if total_found < 10 and not ignore_seen:
        return collect_all_fresh_items(target_categories=target_categories, ignore_seen=True)

    # Сортируем внутри каждой категории по весу (score)
    for cat in by_cat:
        by_cat[cat].sort(key=lambda x: x.get("score", 50), reverse=True)

    return by_cat


# ---------------------------------------------------------------------------
# ГЛУБОКОЕ ЧТЕНИЕ ПЕРВОИСТОЧНИКА ПО КНОПКЕ «📖 ПОДРОБНЕЕ»
# ---------------------------------------------------------------------------
def fetch_full_article_or_repo(url: str, fallback_text: str = "") -> str:
    """
    Скачивает полный текст статьи или README репозитория GitHub для глубокого разбора.
    """
    if not url:
        return fallback_text

    try:
        # 1. Если это GitHub-репозиторий — скачиваем README.md
        gh_match = re.match(r"^https?://github\.com/([^/]+)/([^/#?]+)", url)
        if gh_match:
            owner, repo = gh_match.group(1), gh_match.group(2)
            for branch in ("HEAD", "main", "master"):
                raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
                r = requests.get(raw_url, headers=HEADERS, timeout=8)
                if r.status_code == 200 and len(r.text) > 100:
                    clean_md = re.sub(r"\s+", " ", r.text).strip()
                    return f"{fallback_text}\n\nПолный README репозитория {owner}/{repo}:\n{clean_md[:4500]}"

        # 2. Если это пост в Telegram — берём embed-страницу
        if "t.me/" in url and "/s/" not in url:
            r = requests.get(f"{url}?embed=1&mode=tme", headers=HEADERS, timeout=8)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                txt_div = soup.select_one(".tgme_widget_message_text")
                if txt_div:
                    return clean_html_text(str(txt_div))[:4500]

        # 3. Любая веб-статья (Habr, VC, блог, документация)
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for junk in soup(["script", "style", "nav", "header", "footer", "aside", "svg", "form"]):
                junk.decompose()
            article = soup.find("article") or soup.find("main") or soup.body
            if article:
                text = clean_html_text(str(article))
                if len(text) > 200:
                    return f"{fallback_text}\n\nТекст страницы:\n{text[:4500]}"
    except Exception as e:
        logger.debug(f"Full fetch error for {url}: {e}")

    return fallback_text


# ---------------------------------------------------------------------------
# 6. РАЗВЕДЧИК БОЛЕЙ И ЗАКАЗОВ (Живые боли бизнеса и людей под закрытие через ИИ)
# ---------------------------------------------------------------------------
def collect_pain_points() -> List[Dict[str, Any]]:
    """
    Собирает свежие посты предпринимателей, фрилансеров и пользователей,
    где они жалуются на рутину, ищут автоматизацию или просят инструмент.
    """
    pains: List[Dict[str, Any]] = []
    pain_subs = [
        ("smallbusiness", "Reddit r/smallbusiness (Малый бизнес)"),
        ("Entrepreneur", "Reddit r/Entrepreneur (Предприниматели)"),
        ("Automate", "Reddit r/Automate (Запросы на автоматизацию)"),
        ("freelance", "Reddit r/freelance (Задачи фриланса)"),
    ]

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [
            pool.submit(fetch_single_subreddit, sub, "money", label)
            for sub, label in pain_subs
        ]
        for fut in as_completed(futures):
            try:
                pains.extend(fut.result())
            except Exception:
                pass

    # Дополняем запросами Ask HN (что ищут и о чём спрашивают фаундеры и разработчики)
    try:
        ts_14d = int((datetime.utcnow() - timedelta(days=14)).timestamp())
        resp = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "query": "automate OR manual OR workflow OR tool OR client",
                "tags": "ask_hn",
                "numericFilters": f"created_at_i>{ts_14d}",
                "hitsPerPage": 10,
            },
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            for hit in resp.json().get("hits", []):
                title = hit.get("title") or ""
                story_txt = clean_html_text(hit.get("story_text") or "")
                if title:
                    pains.append(
                        {
                            "title": title,
                            "url": f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                            "raw_text": f"{title}. {story_txt[:1000]}",
                            "source_name": "Hacker News (Ask HN)",
                        }
                    )
    except Exception as e:
        logger.debug(f"Ask HN pain error: {e}")

    return pains[:20]


# ---------------------------------------------------------------------------
# 7. МАШИНА ВРЕМЕНИ (США -> СНГ): Трендовые западные продукты и контент-ниши
# ---------------------------------------------------------------------------
def collect_western_trends() -> List[Dict[str, Any]]:
    """
    Собирает самые свежие англоязычные запуски (Product Hunt, Show HN, Reddit MicroSaaS/SideProject/aivideo),
    которые уже набирают обороты на Западе, но ещё не скопированы в СНГ.
    """
    west: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        f_hn = pool.submit(fetch_hackernews_items)
        f_ph = pool.submit(
            fetch_single_rss,
            {"name": "Product Hunt", "url": "https://www.producthunt.com/feed", "category": "apps"},
        )
        f_sp = pool.submit(fetch_single_subreddit, "SideProject", "apps", "Reddit r/SideProject")
        f_ms = pool.submit(fetch_single_subreddit, "microsaas", "money", "Reddit r/MicroSaaS")
        f_av = pool.submit(fetch_single_subreddit, "aivideo", "money", "Reddit r/aivideo")

        for fut in (f_hn, f_ph, f_sp, f_ms, f_av):
            try:
                west.extend(fut.result())
            except Exception:
                pass

    return west[:25]

