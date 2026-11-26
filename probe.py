#!/usr/bin/env python3
"""Проверка: какие источники доступны с серверов GitHub. Только ручной запуск.
Результат приходит в Telegram и пишется в лог запуска."""
import os

import requests

BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9",
}
PLAIN = {"User-Agent": "python-requests"}

KUDAGO_LOC = "https://kudago.com/public-api/v1.4/locations/?lang=ru&fields=slug,name"
KUDAGO_EVENTS = ("https://kudago.com/public-api/v1.4/events/"
                 "?location=nsk&page_size=1&fields=id,title")

# (название, url, заголовки, текст, который должен быть в ответе)
TESTS = [
    ("контроль: example.com", "https://example.com", BROWSER, "Example"),
    ("контроль: Telegram web", "https://t.me/s/telegram", BROWSER, "tgme"),
    ("KudaGo: города (браузерный UA)", KUDAGO_LOC, BROWSER, "nsk"),
    ("KudaGo: города (обычный UA)", KUDAGO_LOC, PLAIN, "nsk"),
    ("KudaGo: события nsk", KUDAGO_EVENTS, BROWSER, "results"),
    ("Афиша.ру: Новосибирск", "https://www.afisha.ru/novosibirsk/events/", BROWSER, "Новосибирск"),
    ("Афиша.ру: концерты", "https://www.afisha.ru/novosibirsk/events/concerts/", BROWSER, "Новосибирск"),
    ("Яндекс.Афиша: Новосибирск", "https://afisha.yandex.ru/novosibirsk", BROWSER, "Новосибирск"),
    ("hcsibir.ru (сайт клуба)", "https://hcsibir.ru/", BROWSER, "Сибирь"),
]
PROTECTION = ("captcha", "smartcaptcha", "just a moment", "access denied")


def check(name, url, headers, needle):
    try:
        r = requests.get(url, headers=headers, timeout=20)
    except requests.RequestException as ex:
        return f"❌ {name}: нет соединения ({type(ex).__name__})"
    body = r.text or ""
    protected = any(w in body.lower() for w in PROTECTION)
    ok = r.status_code == 200 and needle in body and not protected
    line = f"{'✅' if ok else '❌'} {name}: HTTP {r.status_code}, {len(body) // 1024} КБ"
    if protected:
        line += ", похоже на капчу/защиту"
    elif r.status_code == 200 and needle not in body:
        line += ", ожидаемого текста нет"
    return line


def main():
    lines = [check(*t) for t in TESTS]
    report = "🔎 Проверка источников с серверов GitHub\n\n" + "\n".join(lines)
    print(report)
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": report,
                                "disable_web_page_preview": True}, timeout=30)
        except requests.RequestException:
            print("Не удалось отправить отчёт в Telegram")


if __name__ == "__main__":
    main()
