import re
import logging
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Any
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

class SchoolsClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        self.logged_in = False
        self.pupils: List[Dict[str, Any]] = []

    def login(self, username: str, password: str) -> bool:
        """
        Авторизация на schools.by. Возвращает True в случае успеха.
        """
        login_url = "https://schools.by/login"
        try:
            resp = self.session.get(login_url, timeout=10)
            if resp.status_code != 200:
                return False

            soup = BeautifulSoup(resp.text, "html.parser")
            csrf_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
            if not csrf_input:
                return False

            csrf_token = csrf_input.get("value")

            data = {
                "csrfmiddlewaretoken": csrf_token,
                "username": username,
                "password": password,
                "|123": "|123",
            }
            headers = {
                "Referer": login_url,
                "Origin": "https://schools.by",
            }

            post_resp = self.session.post(login_url, data=data, headers=headers, allow_redirects=True, timeout=12)
            final_url = post_resp.url

            if "schools.by/login" in final_url:
                self.logged_in = False
                return False

            self.logged_in = True
            self._detect_pupils(post_resp.text, final_url)
            return True
        except Exception as e:
            logger.error(f"SchoolsClient login error: {type(e).__name__}: {e}")
            return False

    def _detect_pupils(self, page_html: str, current_url: str):
        """
        Находит всех детей, привязанных к родительскому профилю.
        """
        self.pupils = []
        soup = BeautifulSoup(page_html, "html.parser")

        subdomain_match = re.search(r"https?://([^.]+)\.schools\.by", current_url)
        subdomain = subdomain_match.group(1) if subdomain_match else "199minsk"

        # Ищем ссылки на профили учеников /pupil/<id>
        links = soup.find_all("a", href=re.compile(r"/pupil/\d+"))
        found_ids = set()

        for a in links:
            href = a.get("href", "")
            m = re.search(r"/pupil/(\d+)", href)
            if m:
                p_id = m.group(1)
                if p_id not in found_ids:
                    found_ids.add(p_id)
                    raw_text = a.get_text(" ", strip=True)
                    # Если текст ссылки содержит имя ученика
                    if raw_text and not any(w in raw_text.lower() for w in ["дневник", "оценки", "профиль"]):
                        name = raw_text
                    else:
                        name = a.get("title") or f"Ученик #{p_id}"

                    self.pupils.append({
                        "id": p_id,
                        "name": name,
                        "subdomain": subdomain,
                    })

        # Если не нашли по ссылкам, проверяем текущий URL страницы
        if not self.pupils:
            m = re.search(r"/pupil/(\d+)", current_url)
            if m:
                p_id = m.group(1)
                title = soup.find("h1") or soup.find("title")
                name = title.get_text(strip=True) if title else f"Ученик #{p_id}"
                # Очищаем имя от лишнего
                name = re.sub(r",?\s*\d+\s*\"[^\"]+\"\s*класс.*", "", name).strip()
                self.pupils.append({
                    "id": p_id,
                    "name": name,
                    "subdomain": subdomain,
                })

        # Уточняем точные имена детей через страницу ученика
        for p in self.pupils:
            try:
                p_resp = self.session.get(f"https://{p['subdomain']}.schools.by/pupil/{p['id']}", timeout=8)
                if p_resp.status_code == 200:
                    p_soup = BeautifulSoup(p_resp.text, "html.parser")
                    h1 = p_soup.find("h1")
                    if h1:
                        # формат "Репина Эвита, 4 "Б" класс, ID 3588552"
                        clean_name = h1.get_text(" ", strip=True).split(",")[0].strip()
                        if clean_name:
                            p["name"] = clean_name
            except Exception:
                pass

    def get_quarters(self, pupil_id: str, subdomain: str) -> Dict[str, Any]:
        """
        Получает список четвертей и ID текущей активной четверти.
        """
        url = f"https://{subdomain}.schools.by/pupil/{pupil_id}/dnevnik"
        try:
            resp = self.session.get(url, timeout=10)
            if resp.status_code != 200:
                return {"current": None, "quarters": []}
        except Exception:
            return {"current": None, "quarters": []}

        soup = BeautifulSoup(resp.text, "html.parser")
        quarters = []
        current_qid = None

        menu = soup.find("ul", id=re.compile(r"db_quarters_menu"))
        if menu:
            for li in menu.find_all("li"):
                a = li.find("a")
                if not a or not a.get("quarter_id"):
                    continue
                qid = a.get("quarter_id")
                qnum = a.get_text(strip=True)
                is_curr = "current" in (a.get("class") or [])
                if is_curr:
                    current_qid = qid
                quarters.append({
                    "id": qid,
                    "name": qnum,
                    "is_current": is_curr
                })

        return {
            "current": current_qid,
            "quarters": quarters
        }

    def get_week_diary(self, pupil_id: str, subdomain: str, quarter_id: Optional[str] = None, week_date: Optional[str] = None) -> Dict[str, Any]:
        """
        Забирает расписание, ДЗ и оценки за неделю.
        Если quarter_id не указан, автоматически берется текущая четверть.
        week_date: дата в формате YYYY-MM-DD
        """
        if not quarter_id:
            q_info = self.get_quarters(pupil_id, subdomain)
            quarter_id = q_info["current"]

        if week_date and quarter_id:
            url = f"https://{subdomain}.schools.by/pupil/{pupil_id}/dnevnik/quarter/{quarter_id}/week/{week_date}"
        elif quarter_id:
            url = f"https://{subdomain}.schools.by/pupil/{pupil_id}/dnevnik/quarter/{quarter_id}"
        else:
            url = f"https://{subdomain}.schools.by/pupil/{pupil_id}/dnevnik"

        try:
            resp = self.session.get(url, timeout=12)
            if resp.status_code != 200:
                return {"period": "", "days": [], "attendance": ""}
        except Exception:
            return {"period": "", "days": [], "attendance": ""}

        soup = BeautifulSoup(resp.text, "html.parser")

        # Период недели
        period_tag = soup.find("div", class_="db_period")
        period_text = period_tag.get_text(" ", strip=True) if period_tag else ""

        # Пропуски / посещаемость
        under_tag = soup.find("div", class_="db_under")
        attendance_text = under_tag.get_text(" ", strip=True) if under_tag else ""

        # Находим контейнеры дней, отбрасывая скрытые display:none
        days_containers = soup.find_all("div", class_="db_days")
        visible_container = None
        for c in days_containers:
            style = c.get("style") or ""
            if "display: none" not in style and "display:none" not in style:
                visible_container = c
                break

        if not visible_container and days_containers:
            visible_container = days_containers[0]

        parsed_days = []
        if visible_container:
            day_elements = visible_container.find_all("div", class_="db_day")
            for day in day_elements:
                th = day.find("th")
                day_title = th.get_text(" ", strip=True) if th else ""

                lessons = []
                tbody = day.find("tbody")
                if tbody:
                    for row in tbody.find_all("tr"):
                        lesson_td = row.find("td", class_="lesson")
                        if not lesson_td:
                            continue

                        lesson_full = lesson_td.get_text(" ", strip=True)
                        m_num = re.match(r"^(\d+)\.\s*(.*)", lesson_full)
                        if m_num:
                            num = m_num.group(1)
                            subject = m_num.group(2).strip()
                        else:
                            num = ""
                            subject = lesson_full

                        # Домашнее задание
                        ht_td = row.find("td", class_="ht")
                        homework = ""
                        if ht_td:
                            ht_text_div = ht_td.find("div", class_="ht-text")
                            if ht_text_div:
                                homework = ht_text_div.get_text(" ", strip=True)
                            else:
                                # фильтруем служебные подсказки
                                for hint in ht_td.find_all("div", class_="ht-box"):
                                    hint.decompose()
                                homework = ht_td.get_text(" ", strip=True)

                        # Оценка
                        mark_td = row.find("td", class_="mark")
                        mark = ""
                        if mark_td:
                            mark_box = mark_td.find("div", class_="mark_box")
                            if mark_box:
                                mark = mark_box.get_text(" ", strip=True)

                        if subject or homework or mark:
                            lessons.append({
                                "number": num,
                                "subject": subject,
                                "homework": homework,
                                "mark": mark,
                            })

                if day_title or lessons:
                    parsed_days.append({
                        "title": day_title,
                        "lessons": lessons
                    })

        return {
            "period": period_text,
            "days": parsed_days,
            "attendance": attendance_text,
            "quarter_id": quarter_id
        }

    def get_quarter_marks(self, pupil_id: str, subdomain: str) -> Dict[str, Any]:
        """
        Забирает итоговые четвертные оценки со страницы last-page.
        """
        url = f"https://{subdomain}.schools.by/pupil/{pupil_id}/dnevnik/last-page"
        try:
            resp = self.session.get(url, timeout=12)
            if resp.status_code != 200:
                return {"subjects": [], "average": "", "behavior": ""}
        except Exception:
            return {"subjects": [], "average": "", "behavior": ""}

        soup = BeautifulSoup(resp.text, "html.parser")
        t_left = soup.find("table", class_="ltable")
        t_marks = soup.find("table", class_="mtable")

        if not t_left or not t_marks:
            return {"subjects": [], "average": "", "behavior": ""}

        left_rows = t_left.find_all("tr")
        mark_rows = t_marks.find_all("tr")

        # Поиск смещения: где начинаются предметы
        subjects_data = []
        average_score = ""
        behavior = ""

        # Собираем левую колонку
        for i in range(1, len(left_rows)):
            row_title = left_rows[i].get_text(" ", strip=True)
            if not row_title:
                continue

            # Соответствующая строка в таблице отметок
            # В mtable 2 строки заголовка, поэтому сдвиг на +1 относительно ltable
            m_index = i + 1
            if m_index >= len(mark_rows):
                break

            m_cols = [td.get_text(" ", strip=True) for td in mark_rows[m_index].find_all(["td", "th"])]

            if "средний балл" in row_title.lower():
                average_score = m_cols[0] if m_cols else ""
            elif "поведение" in row_title.lower():
                behavior = m_cols[0] if m_cols else ""
            elif "подпись" in row_title.lower():
                continue
            else:
                # Название предмета (убираем ведущий номер "1. Англ. яз." -> "Англ. яз.")
                clean_subj = re.sub(r"^\d+\.\s*", "", row_title).strip()
                subjects_data.append({
                    "subject": clean_subj,
                    "q1": m_cols[0] if len(m_cols) > 0 else "",
                    "q2": m_cols[1] if len(m_cols) > 1 else "",
                    "q3": m_cols[2] if len(m_cols) > 2 else "",
                    "q4": m_cols[3] if len(m_cols) > 3 else "",
                    "year": m_cols[4] if len(m_cols) > 4 else "",
                })

        return {
            "subjects": subjects_data,
            "average": average_score,
            "behavior": behavior
        }
