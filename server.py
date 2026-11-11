"""Локальный сервер розыгрыша среди комментаторов поста Telegram и чатов трансляций.

Запуск: python server.py  (или start.bat). Только стандартная библиотека.
Комментарии читаются с публичной страницы-виджета t.me, вход в Telegram не нужен.
"""
import html
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "Крутка Vilknz (Vilknz prize wheel)"
VERSION = "1.0.1"
APP_ID = "vilknz-prize-wheel"
HOST, PORT = "127.0.0.1", 8765
PORT_TRIES = 20  # если 8765 занят чужой программой, пробуем следующие порты
# В собранном exe (PyInstaller) файлы лежат во временной папке sys._MEIPASS.
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
PAGE_LIMIT = 100     # комментариев на один запрос к виджету (максимум у t.me)
MAX_REQUESTS = 600   # предохранитель от бесконечного цикла
REQUEST_PAUSE = 0.3  # пауза между запросами, сек
# t.me при частых запросах отдаёт страницу без имён авторов; ждём и повторяем (паузы в секундах)
RETRY_WAITS = (5, 10, 20, 30, 30, 30, 30, 30)

progress = {"text": ""}  # текущее состояние сбора для страницы

POST_URL_RE = re.compile(r"t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,})/(\d+)")
BLOCK_SPLIT = 'js-widget_message_wrap">'
POST_ID_RE = re.compile(r'data-post-id="(\d+)"')
AUTHOR_RE = re.compile(r'<div class="tgme_widget_message_author[^"]*">(.*?)</div>', re.S)
HREF_RE = re.compile(r'href="https?://t\.me/([^"/?]+)"')
COLOR_RE = re.compile(r'class="name (color\d+)"')
USER_RE = re.compile(r'<div class="tgme_widget_message_user">(.*?)</div>', re.S)
IMG_RE = re.compile(r'<img src="(https?://[^"]+)"')
TAG_RE = re.compile(r"<[^>]+>")
TEXT_RE = re.compile(r'js-message_text"[^>]*>(.*?)</div>', re.S)

YT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/126.0 Safari/537.36")
YT_HEADERS = {"User-Agent": YT_UA, "Accept-Language": "en", "Cookie": "SOCS=CAI"}  # SOCS — без экрана согласия
YT_ID_RE = re.compile(r"(?:youtu\.be/|[?&]v=|/(?:embed|shorts|live)/)([\w-]{11})")
YT_INITIAL_RE = re.compile(r'ytInitialData"?\]?\s*=\s*(\{.*?\});\s*</script>', re.S)


def fetch(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_post_url(url):
    m = POST_URL_RE.search(url or "")
    if not m:
        raise ValueError("Нужна ссылка вида https://t.me/канал/123 (публичный канал).")
    return m.group(1), int(m.group(2))


def parse_comments(page):
    """Возвращает список комментариев страницы виджета: id, имя, ник, цвет, аватар, текст."""
    comments = []
    for block in page.split(BLOCK_SPLIT)[1:]:
        id_m = POST_ID_RE.search(block)
        author_m = AUTHOR_RE.search(block)  # первый author в блоке — автор, дальше идёт шаблон ответа
        if not id_m or not author_m:
            continue
        author = author_m.group(1)
        name = html.unescape(TAG_RE.sub("", author)).strip()
        href_m = HREF_RE.search(author)
        color_m = COLOR_RE.search(author)
        user_m = USER_RE.search(block)
        img_m = IMG_RE.search(user_m.group(1)) if user_m else None
        text_m = TEXT_RE.search(block)
        comments.append({
            "id": int(id_m.group(1)),
            "name": name,
            "username": href_m.group(1) if href_m else None,
            "color": color_m.group(1) if color_m else "",
            "avatar": html.unescape(img_m.group(1)) if img_m else None,
            "text": html.unescape(TAG_RE.sub(" ", text_m.group(1))) if text_m else "",
        })
    return comments


def fetch_comments_page(url):
    """Страница комментариев с повтором: t.me иногда отдаёт её с пустыми авторами."""
    for wait in (*RETRY_WAITS, None):
        comments = parse_comments(fetch(url))
        if all(c["name"] for c in comments):
            return comments
        if wait is None:
            break
        for left in range(wait, 0, -1):
            progress["text"] = f"Telegram ограничил частоту запросов, жду {left} с…"
            time.sleep(1)
    raise OSError("Telegram слишком долго ограничивает запросы, попробуйте через несколько минут")


def collect_comments(channel, post_id):
    """Собирает все комментарии поста, листая виджет назад от самых новых."""
    base = f"https://t.me/{channel}/{post_id}?embed=1&discussion=1&comments_limit={PAGE_LIMIT}"
    by_id = {}
    anchor = None
    for _ in range(MAX_REQUESTS):
        found = fetch_comments_page(base + (f"&comment={anchor}" if anchor else ""))
        new = [c for c in found if c["id"] not in by_id]
        for c in new:
            by_id[c["id"]] = c
        progress["text"] = f"Собрано комментариев: {len(by_id)}…"
        oldest = min(by_id) if by_id else None
        # Останавливаемся, когда запрос не принёс ничего старше уже известного.
        if not new or oldest == anchor:
            break
        anchor = oldest
        time.sleep(REQUEST_PAUSE)
    return [by_id[i] for i in sorted(by_id)]


def comment_key(c, channel):
    """Ключ уникального автора комментария; None — комментарий от имени самого канала."""
    if c["username"]:
        if c["username"].lower() == channel.lower():
            return None
        return "@" + c["username"].lower()
    # Без ника публичного ID нет: различаем по имени и цвету имени (он зависит от ID).
    return f"{c['name']}|{c['color']}"


def channel_info(channel):
    try:
        page = fetch(f"https://t.me/{channel}")
    except (urllib.error.URLError, OSError):
        return {"title": channel, "avatar": None}

    def meta(prop):
        m = re.search(rf'<meta property="og:{prop}" content="([^"]*)"', page)
        return html.unescape(m.group(1)) if m else None

    avatar = meta("image")
    if avatar and "telesco.pe" not in avatar and "cdn-telegram" not in avatar:
        avatar = None  # у каналов без аватарки там стандартный логотип Telegram
    return {"title": meta("title") or channel, "avatar": avatar}


def collect(url):
    """Все комментарии поста; уникальных участников и ключевую фразу считает страница."""
    channel, post_id = parse_post_url(url)
    progress["text"] = "Собираю комментарии…"
    messages = []
    for c in collect_comments(channel, post_id):
        key = comment_key(c, channel)
        if key:
            messages.append({"key": key, "name": c["name"], "username": c["username"],
                             "avatar": c["avatar"], "text": c["text"]})
    return {"channel": {"username": channel, **channel_info(channel)}, "messages": messages}


# ---------- чат трансляции YouTube ----------
# Тот же способ, которым чат читает сама страница YouTube: открыть всплывающий чат и опрашивать продолжение.

def yt_text(node):
    if not node:
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(r.get("text") or (r.get("emoji", {}).get("shortcuts") or [""])[0] for r in node.get("runs", []))


def yt_continuation(chat):
    for c in chat.get("continuations", []):
        for data in c.values():
            if data.get("continuation"):
                return data["continuation"], data.get("timeoutMs", 5000)
    return None, None


def yt_messages(chat):
    messages = []
    for action in chat.get("actions", []):
        item = action.get("addChatItemAction", {}).get("item", {})
        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not r or not r.get("authorExternalChannelId"):
            continue
        thumbs = r.get("authorPhoto", {}).get("thumbnails") or [{}]
        messages.append({
            "id": r.get("id"),
            "key": "yt:" + r["authorExternalChannelId"],
            "name": yt_text(r.get("authorName")),
            "username": None,
            "avatar": thumbs[-1].get("url"),
            "text": yt_text(r.get("message")),
        })
    return messages


def yt_video_id(url):
    url = (url or "").strip()
    m = YT_ID_RE.search(url)
    if not m:
        if "youtube.com/" not in url:
            raise ValueError("Нужна ссылка на трансляцию YouTube.")
        # ссылка на канал (youtube.com/@канал/live): берём трансляцию, которая идёт сейчас
        page = fetch(url if url.startswith("http") else "https://" + url, YT_HEADERS)
        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', page)
        if not m:
            raise ValueError("На этом канале сейчас нет трансляции.")
    return m.group(1)


def yt_open(url):
    """Находит чат идущей трансляции. Возвращает данные для дальнейшего опроса."""
    video_id = yt_video_id(url)
    page = fetch(f"https://www.youtube.com/live_chat?v={video_id}&is_popout=1", YT_HEADERS)
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', page)
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', page)
    data = YT_INITIAL_RE.search(page)
    if not (key and ver and data):
        raise ValueError("Не удалось открыть чат: YouTube изменил страницу или трансляция недоступна.")
    contents = json.loads(data.group(1)).get("contents", {})
    chat = contents.get("liveChatRenderer")
    continuation = yt_continuation(chat or {})[0]
    if not continuation:
        reason = yt_text(contents.get("messageRenderer", {}).get("text")) or "чат недоступен"
        raise ValueError(reason)
    return {"key": key.group(1), "ver": ver.group(1), "continuation": continuation, "video": video_id}


def yt_next(key, ver, continuation):
    """Новые сообщения чата с прошлого опроса."""
    body = json.dumps({"context": {"client": {"clientName": "WEB", "clientVersion": ver}},
                       "continuation": continuation}).encode()
    api = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false&key="
    raw = fetch(api + urllib.parse.quote(key), {**YT_HEADERS, "Content-Type": "application/json"}, body)
    chat = json.loads(raw).get("continuationContents", {}).get("liveChatContinuation", {})
    continuation, timeout = yt_continuation(chat)
    return {"messages": yt_messages(chat), "continuation": continuation, "timeoutMs": timeout}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, data):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif parsed.path == "/favicon.ico":
            self._send(200, (ROOT / "assets" / "icon.ico").read_bytes(), "image/x-icon")
        elif parsed.path == "/api/progress":
            self._json(200, progress)
        elif parsed.path in API:
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            try:
                self._json(200, API[parsed.path](query))
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except urllib.error.URLError as e:
                self._json(502, {"error": f"Не удалось получить данные: {e.reason}"})
            except OSError as e:
                self._json(502, {"error": str(e)})
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):
        pass


def stream_avatar(source, link):
    """Аватарка канала стримера для центра колеса; None, если получить не удалось."""
    try:
        if source == "twitch":
            m = re.search(r"twitch\.tv/(\w+)", link) or re.fullmatch(r"@?(\w+)", link.strip())
            page = fetch(f"https://www.twitch.tv/{m.group(1).lower()}", {"User-Agent": YT_UA})
            m = re.search(r'https://static-cdn\.jtvnw\.net/jtv_user_pictures/[^"\\]+?profile_image[^"\\]+', page)
            return m.group(0) if m else None
        if source == "youtube":
            page = fetch(f"https://www.youtube.com/watch?v={yt_video_id(link)}", YT_HEADERS)
            m = re.search(r'"videoOwnerRenderer":\{"thumbnail":\{"thumbnails":\[\{"url":"([^"]+)"', page)
            return re.sub(r"=s\d+-", "=s176-", m.group(1)) if m else None
    except (ValueError, AttributeError, OSError):
        pass
    return None


API = {
    "/api/ping": lambda q: {"app": APP_ID, "version": VERSION},
    "/api/collect": lambda q: collect(q.get("url", "")),
    "/api/avatar": lambda q: {"avatar": stream_avatar(q.get("source", ""), q.get("link", ""))},
    "/api/yt/open": lambda q: yt_open(q.get("url", "")),
    "/api/yt/next": lambda q: yt_next(q.get("key", ""), q.get("ver", ""), q.get("cont", "")),
}


class Server(ThreadingHTTPServer):
    # На Windows SO_REUSEADDR позволяет второй копии программы занять тот же порт,
    # и проверка «программа уже запущена» не срабатывает. Поэтому повторное использование отключено.
    allow_reuse_address = sys.platform != "win32"

    def handle_error(self, request, client_address):
        # браузер оборвал соединение (обновление или закрытие страницы) — это не ошибка
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


CONSOLE_TEXT = """
  {name} {version}

  Программа работает. Её страница открылась в браузере:
      {url}
  (если не открылась — скопируйте этот адрес в браузер)

  ┌──────────────────────────────────────────────────────────────┐
  │  НЕ ЗАКРЫВАЙТЕ ЭТО ОКНО, пока проводите розыгрыш.            │
  │  Без него страница не сможет собирать участников из          │
  │  Telegram и YouTube.                                         │
  │                                                              │
  │  Закройте окно, когда розыгрыш закончен, — программа         │
  │  выключится. Сворачивать окно можно.                         │
  └──────────────────────────────────────────────────────────────┘
"""


def is_our_app(port):
    """Отвечает ли на этом порту уже запущенная копия программы (а не чужая)."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/ping", timeout=2) as resp:
            return json.load(resp).get("app") == APP_ID
    except (OSError, ValueError):
        return False


def main():
    if sys.stdout:
        sys.stdout.reconfigure(errors="replace")  # рамка не должна ронять запуск, если консоль не умеет UTF-8
    # Порт 8765, а если он занят чужой программой — следующий свободный.
    for port in range(PORT, PORT + PORT_TRIES):
        try:
            server = Server((HOST, port), Handler)
            break
        except OSError:
            if is_our_app(port):
                print("Программа уже запущена в другом окне — открываю её страницу в браузере.")
                print("Это окно закроется само.")
                webbrowser.open(f"http://{HOST}:{port}/")
                time.sleep(3)
                return
    else:
        print(f"Не удалось запустить: порты {PORT}–{PORT + PORT_TRIES - 1} заняты другими программами.")
        print("Закройте лишние программы и запустите снова. Окно закроется через 30 секунд.")
        time.sleep(30)
        return
    url = f"http://{HOST}:{port}/"
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(f"{APP_NAME} — не закрывайте, пока идёт розыгрыш")
    print(CONSOLE_TEXT.format(name=APP_NAME, version=VERSION, url=url))
    if port != PORT:
        print(f"  Порт {PORT} занят другой программой, поэтому выбран {port}.")
        print("  Ссылки и настройки, сохранённые на прошлом порту, здесь не видны.\n")
    threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if len(sys.argv) > 1:  # проверка без браузера: python server.py <ссылка на пост>
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(collect(sys.argv[1]), ensure_ascii=False, indent=1))
    else:
        main()
