#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генератор статей для канала "Интересные факты" в Яндекс.Дзен.

Что делает:
1. Выбирает свежую тему из пула (не повторяя уже использованные).
2. Просит Claude написать статью 3500-5000 знаков в чистом HTML.
3. Подбирает 3-4 подходящих фото через Pexels API.
4. Сохраняет статью как отдельную HTML-страницу в docs/articles/.
5. Обновляет docs/articles.json (общий реестр статей сайта).
6. Пересобирает docs/feed.xml — RSS-ленту в формате, который понимает Дзен.

Запуск:
    python scripts/generate.py --count 3

Переменные окружения (задаются как GitHub Secrets):
    GEMINI_API_KEY      — ключ Google Gemini API (бесплатный, https://aistudio.google.com/apikey)
    PEXELS_API_KEY      — ключ Pexels API (бесплатный, https://www.pexels.com/api/)
    SITE_URL            — базовый URL сайта, напр. https://username.github.io/dzen-autopost
    TELEGRAM_BOT_TOKEN  — опционально, для уведомлений о новых статьях
    TELEGRAM_CHAT_ID    — опционально, для уведомлений о новых статьях
"""

import os
import re
import sys
import json
import time
import random
import argparse
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

# Бесплатная модель Google Gemini (не требует привязки карты).
# Ключ берётся на https://aistudio.google.com/apikey
# Имя конкретной модели не хардкодим — Google переименовывает/деприкейтит
# модели каждые несколько месяцев (gemini-2.5-flash уже недоступен новым
# ключам на момент написания). Вместо этого запрашиваем список доступных
# моделей и берём самую свежую подходящую flash-модель — см. resolve_model().
GEMINI_LIST_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_GENERATE_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/{model}:generateContent"

# Опционально: уведомления в Telegram о новой партии статей
TELEGRAM_ENDPOINT = "https://api.telegram.org/bot{token}/sendMessage"

SITE_URL = os.environ.get("SITE_URL", "https://example.github.io/dzen-autopost").rstrip("/")
DOCS_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")
ARTICLES_DIR = os.path.join(DOCS_DIR, "articles")
REGISTRY_FILE = os.path.join(DOCS_DIR, "articles.json")
FEED_FILE = os.path.join(DOCS_DIR, "feed.xml")

# Пул тем — расширяйте под свою нишу. Скрипт старается не повторять
# темы, которые уже публиковались за последние 60 дней.
TOPIC_POOL = [
    "необычные факты о космосе и планетах",
    "странные и редкие природные явления",
    "малоизвестные факты из мировой истории",
    "удивительные способности животных",
    "любопытные факты о человеческом теле и мозге",
    "необычные традиции и обычаи разных народов мира",
    "интересные факты о еде и её происхождении",
    "загадки и тайны океана",
    "невероятные архитектурные сооружения прошлого",
    "малоизвестные факты о древних цивилизациях",
    "странные законы, которые существуют в разных странах",
    "факты о языках и происхождении слов",
    "удивительные изобретения, опередившие своё время",
    "интересные факты о цвете и восприятии",
    "необычные рекорды природы",
    "факты о деньгах и экономике прошлого",
    "малоизвестные факты о известных изобретениях",
    "странности человеческой психологии",
    "факты о погоде и климатических аномалиях",
    "интересные факты о числах и математике вокруг нас",
]

PEXELS_ENDPOINT = "https://api.pexels.com/v1/search"

ARTICLE_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<meta name="description" content="{description}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:image" content="{cover}">
<link rel="canonical" href="{url}">
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; max-width: 720px; margin: 0 auto; padding: 24px 16px 64px; line-height: 1.6; color: #1a1a1a; }}
  h1 {{ font-size: 28px; line-height: 1.25; }}
  h2 {{ font-size: 22px; margin-top: 36px; }}
  figure {{ margin: 24px 0; }}
  figure img {{ width: 100%; height: auto; border-radius: 8px; display: block; }}
  figcaption {{ font-size: 13px; color: #777; margin-top: 6px; }}
  p {{ margin: 16px 0; }}
</style>
</head>
<body>
<article>
<h1>{title}</h1>
{content}
</article>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def slugify(title: str) -> str:
    translit = {
        "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z",
        "и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r",
        "с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"ts","ч":"ch","ш":"sh","щ":"sch",
        "ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
    }
    s = title.lower()
    s = "".join(translit.get(ch, ch) for ch in s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    ts = int(time.time() * 1000) % 100000
    return f"{s[:60]}-{ts}"


def load_registry():
    if not os.path.exists(REGISTRY_FILE):
        return []
    with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_registry(items):
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def pick_topic(used_titles):
    used_lower = " ".join(used_titles[-40:]).lower()
    candidates = [t for t in TOPIC_POOL if t.split()[1] not in used_lower]
    pool = candidates if candidates else TOPIC_POOL
    return random.choice(pool)


# ---------------------------------------------------------------------------
# Генерация текста через Claude
# ---------------------------------------------------------------------------

def resolve_model(api_key):
    """Спрашивает у Gemini API список доступных моделей и выбирает подходящую
    flash-модель с поддержкой generateContent. Так скрипт переживёт очередное
    переименование моделей Google без правки кода."""
    r = requests.get(GEMINI_LIST_MODELS_URL, params={"key": api_key}, timeout=30)
    r.raise_for_status()
    models = r.json().get("models", [])

    def supports_generate(m):
        return "generateContent" in m.get("supportedGenerationMethods", [])

    flash_stable = [
        m["name"] for m in models
        if supports_generate(m)
        and "flash" in m["name"].lower()
        and "lite" not in m["name"].lower()
        and "preview" not in m["name"].lower()
        and "exp" not in m["name"].lower()
        and "latest" not in m["name"].lower()  # алиасы вида gemini-flash-latest нестабильны/перегружены
    ]
    flash_any = [
        m["name"] for m in models
        if supports_generate(m) and "flash" in m["name"].lower() and "latest" not in m["name"].lower()
    ]
    any_model = [
        m["name"] for m in models
        if supports_generate(m) and "latest" not in m["name"].lower()
    ]

    candidates = flash_stable or flash_any or any_model
    if not candidates:
        raise RuntimeError("Gemini API не вернул ни одной модели с поддержкой generateContent")

    candidates.sort()  # имена вида models/gemini-3.7-flash — лексикографически новее версии оказываются последними
    chosen = candidates[-1]
    print(f"[..] выбрана модель Gemini: {chosen}")
    return chosen


def generate_article(api_key, model_name, topic):
    prompt = f"""Ты — редактор популярного канала интересных фактов в Яндекс.Дзен.

Напиши статью на тему: "{topic}".

Требования к тексту:
- Объём 3500-5000 знаков с пробелами (считается только текст, без HTML-тегов).
- Заголовок цепляющий, но честный, без кликбейта-обмана, до 90 символов.
- Раздели материал на 4-6 смысловых блоков с подзаголовками <h2>.
- Стиль: живой научно-популярный, но фактологически точный, без воды.
- В финале — короткий неожиданный факт-бонус (2-3 предложения).
- Используй только теги <h2>, <p>, <ul><li>, <b>, <i> — без markdown, без <html>/<body>.

Ответь СТРОГО валидным JSON без markdown-обрамления (без ```), одним объектом:
{{
  "title": "заголовок статьи",
  "description": "1-2 предложения для анонса и превью, до 200 символов",
  "image_queries": ["4 запроса на английском для поиска фото по теме, каждый 2-4 слова"],
  "html": "полный текст статьи в HTML по описанным выше правилам"
}}"""

    last_error = None
    for attempt in range(3):
        resp = requests.post(
            GEMINI_GENERATE_URL_TMPL.format(model=model_name),
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.9,
                    "maxOutputTokens": 4096,
                    "responseMimeType": "application/json",
                },
            },
            timeout=60,
        )
        if resp.status_code == 503 and attempt < 2:
            print(f"[warn] Gemini временно перегружен (503), повтор через 5 сек ({attempt + 1}/3)", file=sys.stderr)
            time.sleep(5)
            last_error = resp
            continue
        resp.raise_for_status()
        payload = resp.json()
        raw = payload["candidates"][0]["content"]["parts"][0]["text"].strip()
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
        return json.loads(raw)

    last_error.raise_for_status()


def notify_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            TELEGRAM_ENDPOINT.format(token=token),
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[warn] не удалось отправить уведомление в Telegram: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Подбор изображений через Pexels
# ---------------------------------------------------------------------------

def fetch_images(queries, api_key, need=4):
    images = []
    headers = {"Authorization": api_key}
    for q in queries:
        if len(images) >= need:
            break
        try:
            r = requests.get(
                PEXELS_ENDPOINT,
                headers=headers,
                params={"query": q, "per_page": 5, "orientation": "landscape"},
                timeout=20,
            )
            r.raise_for_status()
            photos = r.json().get("photos", [])
            if photos:
                photo = random.choice(photos)
                # "large" в Pexels обычно >= 940px по ширине — с запасом проходит
                # требование Дзена "минимальная ширина 700px"
                images.append(photo["src"]["large"])
        except requests.RequestException as e:
            print(f"[warn] не удалось получить фото по запросу '{q}': {e}", file=sys.stderr)
    return images


# ---------------------------------------------------------------------------
# Сборка HTML-статьи с вставленными figure
# ---------------------------------------------------------------------------

def interleave_images(html_body, images):
    if not images:
        return html_body
    parts = re.split(r"(</h2>)", html_body)
    # parts выглядит как [текст_до_h2_1, "</h2>", текст_после, "</h2>", ...]
    h2_positions = [i for i, p in enumerate(parts) if p == "</h2>"]
    if not h2_positions:
        # если подзаголовков нет — просто вставим фото после первого абзаца
        return html_body.replace("</p>", "</p>" + make_figure(images[0]), 1) if images else html_body

    out = []
    img_i = 0
    for i, part in enumerate(parts):
        out.append(part)
        if part == "</h2>" and img_i < len(images) and i != h2_positions[0]:
            out.append(make_figure(images[img_i]))
            img_i += 1
    result = "".join(out)
    return result


def make_figure(url, caption=""):
    cap = f"<figcaption>{caption}</figcaption>" if caption else ""
    return f'<figure><img src="{url}">{cap}</figure>'


# ---------------------------------------------------------------------------
# Сохранение статьи и обновление реестра
# ---------------------------------------------------------------------------

def save_article(data, images, pub_dt):
    slug = slugify(data["title"])
    url = f"{SITE_URL}/articles/{slug}.html"
    cover = images[0] if images else ""

    body_html = interleave_images(data["html"], images)
    if cover:
        body_html = make_figure(cover) + body_html

    page = ARTICLE_PAGE_TEMPLATE.format(
        title=data["title"],
        description=data["description"],
        cover=cover,
        url=url,
        content=body_html,
    )

    os.makedirs(ARTICLES_DIR, exist_ok=True)
    with open(os.path.join(ARTICLES_DIR, f"{slug}.html"), "w", encoding="utf-8") as f:
        f.write(page)

    registry = load_registry()
    registry.append({
        "slug": slug,
        "title": data["title"],
        "description": data["description"],
        "cover": cover,
        "url": url,
        "pub_date": pub_dt.strftime("%a, %d %b %Y %H:%M:%S %z"),
    })
    save_registry(registry)
    print(f"[ok] статья сохранена: {slug}")
    return registry[-1]


# ---------------------------------------------------------------------------
# Сборка RSS-ленты по требованиям Дзена
# ---------------------------------------------------------------------------

def build_feed(fresh_days=3, max_items=500):
    registry = load_registry()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=fresh_days)

    def parse_dt(s):
        try:
            return datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z")
        except ValueError:
            return now

    fresh = [a for a in registry if parse_dt(a["pub_date"]) >= cutoff]
    fresh = fresh[-max_items:]

    items_xml = []
    for a in fresh:
        # читаем сохранённый HTML статьи, чтобы включить его целиком в content:encoded
        article_path = os.path.join(ARTICLES_DIR, f"{a['slug']}.html")
        content_html = ""
        if os.path.exists(article_path):
            with open(article_path, "r", encoding="utf-8") as f:
                page = f.read()
            m = re.search(r"<article>(.*)</article>", page, re.S)
            if m:
                content_html = m.group(1)

        items_xml.append(f"""    <item>
      <title>{escape_xml(a['title'])}</title>
      <link>{a['url']}</link>
      <guid>{a['slug']}</guid>
      <pubDate>{a['pub_date']}</pubDate>
      <description>{escape_xml(a['description'])}</description>
      <enclosure url="{a['cover']}" type="image/jpeg"/>
      <content:encoded><![CDATA[{content_html}]]></content:encoded>
    </item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Интересные факты</title>
    <link>{SITE_URL}/</link>
    <language>ru</language>
{chr(10).join(items_xml)}
  </channel>
</rss>
"""
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(FEED_FILE, "w", encoding="utf-8") as f:
        f.write(feed)
    print(f"[ok] feed.xml пересобран, свежих материалов в ленте: {len(fresh)}")


def escape_xml(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=3, help="сколько статей сгенерировать за запуск")
    args = parser.parse_args()

    gemini_key = os.environ.get("GEMINI_API_KEY")
    pexels_key = os.environ.get("PEXELS_API_KEY")
    if not gemini_key:
        sys.exit("Ошибка: не задана переменная окружения GEMINI_API_KEY")
    if not pexels_key:
        sys.exit("Ошибка: не задана переменная окружения PEXELS_API_KEY")

    registry = load_registry()
    used_titles = [a["title"] for a in registry]

    try:
        model_name = resolve_model(gemini_key)
    except Exception as e:
        sys.exit(f"Ошибка: не удалось определить доступную модель Gemini: {e}")

    now = datetime.now(timezone.utc)
    published = []
    for i in range(args.count):
        topic = pick_topic(used_titles)
        print(f"[..] генерирую статью по теме: {topic}")
        try:
            data = generate_article(gemini_key, model_name, topic)
        except Exception as e:
            print(f"[error] генерация не удалась: {e}", file=sys.stderr)
            continue

        images = fetch_images(data.get("image_queries", []), pexels_key)
        pub_dt = now + timedelta(minutes=i * 2)
        entry = save_article(data, images, pub_dt)
        published.append(entry)
        used_titles.append(data["title"])
        time.sleep(2)  # не долбить API слишком часто

    build_feed()

    if published:
        lines = "\n".join(f"• {a['title']}\n{a['url']}" for a in published)
        notify_telegram(f"Опубликована новая партия статей ({len(published)}):\n\n{lines}")


if __name__ == "__main__":
    main()
