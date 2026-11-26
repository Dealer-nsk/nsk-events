#!/usr/bin/env python3
"""Агент для таксиста: матчи, концерты и массовые события в Новосибирске
на сегодня и завтра -> личное сообщение в Telegram.

Режимы (переменная MODE):
  morning - дайджест на сегодня и завтра (по умолчанию)
  evening - только сегодняшний вечер, молчит, если нечего показать
"""
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ───────────────────────── НАСТРОЙКИ (правь здесь) ─────────────────────────

# Площадки, где событие даёт ВЫСОКИЙ спрос (проверяется подстрока, строчными буквами)
HIGH_VENUES = [
    "сибирь-арена", "сибирь арена", "ледовый дворец", "экспоцентр",
    "центральный стадион", "спартак",
]
# Площадки со СРЕДНИМ спросом
MEDIUM_VENUES = [
    "горького", "новат", "оперы и балета", "филармони", "глобус",
    "железнодорожников", "октябрьской революции", "центральный парк",
    "площадь ленина",
]
# Публичные Telegram-каналы с афишей города (имя без @). Пусто = не используем.
TG_CHANNELS = []

# Слова в названии, по которым событие выбрасываем
EXCLUDE = ["мастер-класс", "лекци", "экскурси", "выставк", "кинопоказ",
           "воркшоп", "квиз"]

# Тип события: (название, ключевые слова)
KINDS = [
    ("хоккей", ["хоккей", "кхл", "хк сибирь", "хк «сибирь»"]),
    ("футбол", ["футбол"]),
    ("баскетбол", ["баскетбол"]),
    ("матч", ["матч", "волейбол", "гандбол", "регби", "турнир", "кубок",
              "чемпионат", "бокс", "мма", "единоборств"]),
    ("концерт", ["концерт", "гастроли", "оркестр", "выступление", "тур "]),
    ("шоу", ["шоу", "представление", "цирк", "стендап", "стенд-ап",
             "балет", "опера", "спектакл", "мюзикл"]),
    ("праздник", ["фестиваль", "ярмарка", "праздник", "масленица", "салют",
                  "день города", "ёлк", "елк", "новогодн"]),
]
# Примерная длительность в минутах (для оценки времени разъезда)
DURATION = {"хоккей": 150, "футбол": 120, "баскетбол": 120, "матч": 120,
            "концерт": 130, "шоу": 120, "праздник": 180, "событие": 120}
EMOJI = {"хоккей": "🏒", "футбол": "⚽", "баскетбол": "🏀", "матч": "🏆",
         "концерт": "🎤", "шоу": "🎭", "праздник": "🎉", "событие": "📍"}
EXIT_WINDOW_MIN = 60  # сколько минут после конца события считаем «разъездом»

# ──────────────────────────────── ТЕХНИЧЕСКОЕ ───────────────────────────────

NSK = timezone(timedelta(hours=7))  # Новосибирск, без перехода на летнее время
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
MODE = os.environ.get("MODE", "morning")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
UA = {"User-Agent": "Mozilla/5.0 (compatible; nsk-taxi-events-bot)"}
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


def mk(src, title, place, start, end=None, url="", text="",
       time_known=True, level=None):
    return {"src": src, "title": (title or "").strip(), "place": (place or "").strip(),
            "start": start, "end": end, "url": url or "", "text": text or "",
            "time_known": time_known, "level": level}


# ───────────────────────────────── ИСТОЧНИКИ ────────────────────────────────

def fetch_kudago(day0, win_end):
    out = []
    for page in range(1, 6):
        r = requests.get(
            "https://kudago.com/public-api/v1.4/events/",
            params={
                "location": "nsk",
                "actual_since": int(day0.timestamp()),
                "actual_until": int(win_end.timestamp()),
                "fields": "id,title,description,dates,place,site_url",
                "expand": "place",
                "text_format": "text",
                "page_size": 100,
                "page": page,
            },
            headers=UA, timeout=30)
        r.raise_for_status()
        data = r.json()
        for e in data.get("results", []):
            place = e.get("place")
            place_title = place.get("title", "") if isinstance(place, dict) else ""
            for d in e.get("dates") or []:
                s, en = d.get("start") or 0, d.get("end") or 0
                if s <= 0:
                    continue
                start = datetime.fromtimestamp(s, NSK)
                if not (day0 <= start < win_end):
                    continue
                end = datetime.fromtimestamp(en, NSK) if en > 0 else None
                if end and not (timedelta(hours=1) <= end - start <= timedelta(hours=6)):
                    end = None  # серия дат или мусор - оценим сами
                out.append(mk("KudaGo", e.get("title"), place_title, start, end,
                              e.get("site_url"), e.get("description")))
        if not data.get("next"):
            break
    return out


def fetch_tg(day0):
    if not TG_CHANNELS:
        return []
    from bs4 import BeautifulSoup
    out = []
    for ch in TG_CHANNELS:
        r = requests.get(f"https://t.me/s/{ch}", headers=UA, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"t.me/s/{ch}: HTTP {r.status_code}")
        soup = BeautifulSoup(r.text, "html.parser")
        for m in soup.select("div.tgme_widget_message"):
            body = m.select_one(".tgme_widget_message_text")
            post = m.get("data-post")
            if not body or not post:
                continue
            text = body.get_text(" ", strip=True)
            low = text.lower()
            for i, word in ((0, "сегодня"), (1, "завтра")):
                day = day0 + timedelta(days=i)
                if day.strftime("%d.%m") in low or word in low:
                    tm = TIME_RE.search(text)
                    start = day.replace(hour=int(tm[1]) if tm else 12,
                                        minute=int(tm[2]) if tm else 0)
                    out.append(mk("Telegram", text[:100], "", start, None,
                                  f"https://t.me/{post}", text, time_known=bool(tm)))
                    break
    return out


def fetch_manual():
    """Ручные события из manual.json (например, домашние матчи из расписания клуба)."""
    p = Path("manual.json")
    if not p.exists():
        return []
    out = []
    for m in json.loads(p.read_text(encoding="utf-8")):
        start = datetime.strptime(f"{m['date']} {m.get('time', '19:00')}",
                                  "%Y-%m-%d %H:%M").replace(tzinfo=NSK)
        end = None
        if m.get("end"):
            end = datetime.strptime(f"{m['date']} {m['end']}",
                                    "%Y-%m-%d %H:%M").replace(tzinfo=NSK)
        out.append(mk("manual", m.get("title", ""), m.get("place", ""), start, end,
                      m.get("url", ""), "", level=m.get("level", "high")))
    return out


# ───────────────────────────────── ФИЛЬТР ──────────────────────────────────

def detect_kind(ev):
    title = ev["title"].lower()
    text = ev["text"][:300].lower()
    for name, words in KINDS:
        if any(w in title or w in text for w in words):
            return name
    return "событие"


def classify(ev):
    """Возвращает ((тип, уровень), '') или (None, причина отказа)."""
    title = ev["title"].lower()
    if any(w in title for w in EXCLUDE):
        return None, "исключено"
    level = ev["level"]
    if not level:
        hay = ev["place"].lower() or f"{title} {ev['text'][:200].lower()}"
        if any(v in hay for v in HIGH_VENUES):
            level = "high"
        elif any(v in hay for v in MEDIUM_VENUES):
            level = "medium"
    if not level:
        return None, "площадка не в списке"
    kind = detect_kind(ev)
    if kind == "событие" and level == "medium":
        return None, "тип не подходит"
    return (kind, level), ""


def estimate_end(ev):
    if ev["end"]:
        return ev["end"], False
    return ev["start"] + timedelta(minutes=DURATION[ev["kind"]]), True


# ───────────────────────────────── ОТПРАВКА ────────────────────────────────

def fmt_event(ev):
    end, est = estimate_end(ev)
    out_to = end + timedelta(minutes=EXIT_WINDOW_MIN)
    title = html.escape(ev["title"][:100])
    place = html.escape(ev["place"]) if ev["place"] else "площадка не указана"
    t = f"{ev['start']:%H:%M}" + ("" if ev["time_known"] else " (время уточнить)")
    level = "🔥 Спрос: высокий" if ev["level"] == "high" else "🟡 Спрос: средний"
    lines = [
        f"{EMOJI[ev['kind']]} <b>{title}</b>",
        f"🕒 {t} · 📍 {place}",
        f"🚕 разъезд ≈ {end:%H:%M}–{out_to:%H:%M}" + (" (оценка)" if est else ""),
        level,
    ]
    if ev["url"]:
        lines.append(f'<a href="{html.escape(ev["url"], quote=True)}">ссылка</a>')
    return "\n".join(lines)


def split_blocks(blocks, limit=3800):
    chunks, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > limit:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        chunks.append(cur)
    return chunks


def send(blocks):
    for chunk in split_blocks(blocks):
        if DRY_RUN:
            print(chunk, "\n" + "-" * 40)
            continue
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": chunk, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=30)
        except requests.RequestException:
            raise RuntimeError("Telegram: сетевая ошибка") from None
        if not r.ok:
            raise RuntimeError(f"Telegram: HTTP {r.status_code} {r.text[:200]}")


# ────────────────────────────────── ГЛАВНОЕ ────────────────────────────────

def main():
    if not DRY_RUN and not (TG_TOKEN and TG_CHAT_ID):
        sys.exit("Не заданы секреты TG_TOKEN и TG_CHAT_ID")

    now = datetime.now(NSK)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win_end = day0 + timedelta(days=2)
    today, tomorrow = day0.date(), (day0 + timedelta(days=1)).date()

    raw, errors = [], []
    sources = [("KudaGo", lambda: fetch_kudago(day0, win_end)),
               ("Telegram", lambda: fetch_tg(day0)),
               ("manual.json", fetch_manual)]
    for name, fn in sources:
        try:
            raw += fn()
        except Exception as ex:  # один упавший источник не должен ронять остальные
            errors.append(f"{name}: {ex}")

    events, rejected, seen = [], [], set()
    for ev in raw:
        if not (day0 <= ev["start"] < win_end):
            continue
        key = (ev["title"].lower(), ev["start"])
        if key in seen:
            continue
        seen.add(key)
        res, why = classify(ev)
        if res is None:
            if why == "площадка не в списке" and detect_kind(ev) != "событие":
                rejected.append(f"{ev['title'][:60]} — {ev['place'] or '?'}")
            continue
        ev["kind"], ev["level"] = res
        events.append(ev)
    events.sort(key=lambda e: e["start"])

    if MODE == "evening":
        events = [e for e in events if e["start"].date() == today and
                  estimate_end(e)[0] + timedelta(minutes=EXIT_WINDOW_MIN) > now]
        if events:
            send(["🌆 <b>Сегодня вечером</b>"] + [fmt_event(e) for e in events])
    else:
        blocks = ["🚕 <b>Спрос на сегодня и завтра</b>"]
        for label, d in (("Сегодня", today), ("Завтра", tomorrow)):
            items = [e for e in events if e["start"].date() == d]
            if items:
                blocks.append(f"📅 <b>{label}, {d:%d.%m}</b>")
                blocks += [fmt_event(e) for e in items]
        if not events:
            blocks.append("Крупных событий не нашёл.")
        if errors:
            blocks.append("⚠️ Часть источников не ответила, данные неполные:\n" +
                          html.escape("\n".join(errors)))
        send(blocks)

    if MANUAL_RUN:  # диагностика только при ручном запуске
        per_src = {}
        for ev in raw:
            if day0 <= ev["start"] < win_end:
                per_src[ev["src"]] = per_src.get(ev["src"], 0) + 1
        diag = ["ℹ️ <b>Диагностика (ручной запуск)</b>",
                f"Получено на 2 дня: {html.escape(str(per_src) or '{}')}",
                f"Прошли фильтр: {len(events)}"]
        if rejected:
            diag.append("Похожи на матчи/концерты, но площадки нет в списке "
                        "(добавь в HIGH_VENUES / MEDIUM_VENUES):\n" +
                        html.escape("\n".join("• " + r for r in rejected[:10])))
        if errors:
            diag.append("Ошибки:\n" + html.escape("\n".join(errors)))
        send(diag)

    if errors:
        print("\n".join(errors), file=sys.stderr)
        sys.exit(1)  # GitHub пришлёт письмо о падении


if __name__ == "__main__":
    main()
