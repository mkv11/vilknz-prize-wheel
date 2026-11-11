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
VERSION = "1.1.0"
APP_ID = "vilknz-prize-wheel"
HOST, PORT = "127.0.0.1", 8765
PORT_TRIES = 20  # если 8765 занят чужой программой, пробуем следующие порты
# В собранном exe (PyInstaller) файлы лежат во временной папке sys._MEIPASS.
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
FROZEN = hasattr(sys, "_MEIPASS")
PAGE_CACHE = (ROOT / "index.html").read_bytes() if FROZEN else None  # из исходников читаем каждый раз (удобно править)
ICON_BYTES = (ROOT / "assets" / "icon.ico").read_bytes()
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
BR_RE = re.compile(r"<br\s*/?>", re.I)
HEADER_COUNT_RE = re.compile(r'js-header">\s*([\d,\s]+?)\s+comment')
DELETED_ACCOUNT = "Deleted Account"

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
            "text": html.unescape(TAG_RE.sub("", BR_RE.sub(" ", text_m.group(1)))) if text_m else "",
        })
    return comments


def fetch_retry(url, tries=3):
    """fetch с повтором при сетевой ошибке или таймауте (кроме ответов-ошибок вроде 404)."""
    for attempt in range(tries):
        try:
            return fetch(url)
        except urllib.error.HTTPError:
            raise
        except OSError:  # URLError, таймаут, обрыв соединения
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def fetch_comments_page(url):
    """Страница комментариев (комментарии, html) с повтором: t.me иногда отдаёт её с пустыми авторами."""
    for wait in (*RETRY_WAITS, None):
        page = fetch_retry(url)
        comments = parse_comments(page)
        if all(c["name"] for c in comments):
            return comments, page
        if wait is None:
            break
        for left in range(wait, 0, -1):
            progress["text"] = f"Telegram ограничил частоту запросов, жду {left} с…"
            time.sleep(1)
    raise OSError("Telegram слишком долго ограничивает запросы, попробуйте через несколько минут")


def collect_comments(channel, post_id):
    """Собирает все комментарии поста, листая виджет назад от самых новых.

    Возвращает (комментарии, предупреждение или None). Если сбор оборвался посередине, собранное
    не выбрасывается, а возвращается с предупреждением.
    """
    base = f"https://t.me/{channel}/{post_id}?embed=1&discussion=1&comments_limit={PAGE_LIMIT}"
    by_id = {}
    anchor = None
    expected = None
    warning = None
    for _ in range(MAX_REQUESTS):
        try:
            found, page = fetch_comments_page(base + (f"&comment={anchor}" if anchor else ""))
        except OSError as e:
            if not by_id:
                raise
            warning = f"сбор оборвался ({e}), учтены не все комментарии — нажмите «Добавить» ещё раз позже"
            break
        if expected is None:
            m = HEADER_COUNT_RE.search(page)
            expected = int(re.sub(r"\D", "", m.group(1))) if m else 0
            if not found:
                raise ValueError("Комментариев не найдено. Проверьте ссылку: нужен пост публичного канала "
                                 "с включёнными комментариями.")
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
    if not warning and expected and len(by_id) < expected:
        warning = f"Telegram показывает {expected} комментариев, а собрано {len(by_id)}"
    return [by_id[i] for i in sorted(by_id)], warning


def comment_key(c, channel):
    """Ключ уникального автора комментария; None — от имени самого канала или удалённый аккаунт."""
    if c["username"]:
        if c["username"].lower() == channel.lower():
            return None
        return "@" + c["username"].lower()
    if c["name"] == DELETED_ACCOUNT:
        return None  # удалённые аккаунты неразличимы и не могут получить приз
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
    comments, warning = collect_comments(channel, post_id)
    for c in comments:
        key = comment_key(c, channel)
        if key:
            messages.append({"uid": f"tgw:{channel.lower()}:{c['id']}", "key": key, "name": c["name"],
                             "username": c["username"], "avatar": c["avatar"], "text": c["text"]})
    return {"channel": {"username": channel, **channel_info(channel)}, "messages": messages, "warning": warning}


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
        parsed = urllib.parse.urlparse(url if "://" in url else "https://" + url)
        host = (parsed.hostname or "").lower()
        if host != "youtube.com" and not host.endswith(".youtube.com"):
            raise ValueError("Нужна ссылка на трансляцию YouTube.")
        # ссылка на канал (youtube.com/@канал/live): берём трансляцию, которая идёт сейчас
        page = fetch(f"https://www.youtube.com{parsed.path}", YT_HEADERS)
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
    def _send(self, code, body, ctype, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, data):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _own_hosts(self):
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _api_allowed(self):
        """Запрос к /api пришёл со страницы Крутки, а не с чужого сайта в браузере.

        Host отсекает DNS-rebinding (чужое имя, указывающее на 127.0.0.1); Sec-Fetch-Site отсекает
        запросы, которые чужая страница шлёт к нам картинками, формами или fetch.
        """
        if self.headers.get("Host", "") not in self._own_hosts():
            return False
        return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/") and not self._api_allowed():
            return self._json(403, {"error": "Запрос не со страницы Крутки"})
        if parsed.path == "/":
            self._send(200, PAGE_CACHE or (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif parsed.path == "/favicon.ico":
            self._send(200, ICON_BYTES, "image/x-icon", cache=True)
        elif parsed.path == "/api/progress":
            self._json(200, progress)
        elif parsed.path == "/api/tgbot/photo":
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            kind = "user" if "user" in query else "chat"
            ident = query.get(kind, "")
            if not isinstance(ident, str):
                ident = ""
            data = tg_photo(kind, ident) if re.fullmatch(r"-?\d+", ident) and tg_bot["token"] else None
            if data:
                self._send(200, data, "image/jpeg", cache=True)
            else:
                self._send(404, b"no photo", "text/plain")
        elif parsed.path in API:
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            self._call(API[parsed.path], query)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in POST_API:
            return self._send(404, b"not found", "text/plain")
        # Только со своей страницы: чужой сайт в браузере не должен управлять ботом. JSON-тип запроса
        # заставляет браузер спрашивать разрешение (CORS), а мы его не даём; Origin проверяем явно.
        origin = self.headers.get("Origin")
        own_origins = {f"http://{h}" for h in self._own_hosts()}
        if (not self._api_allowed() or (origin and origin not in own_origins)
                or self.headers.get("Content-Type", "").split(";")[0] != "application/json"):
            return self._json(403, {"error": "Запрос не со страницы Крутки"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._json(400, {"error": "Неверный запрос"})
        self._call(POST_API[path], body)

    def _call(self, func, arg):
        try:
            self._json(200, func(arg))
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except urllib.error.URLError as e:
            self._json(502, {"error": f"Не удалось получить данные: {e.reason}"})
        except OSError as e:
            self._json(502, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 — сайт поменял формат ответа и т. п.: странице нужна понятная ошибка
            self._json(500, {"error": f"Неожиданный ответ сайта ({type(e).__name__}). Возможно, сайт изменился — "
                                      "сообщите автору Крутки."})

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


# ---------- бот Telegram для закрытых каналов ----------
# Пользователь создаёт своего бота и делает его администратором группы обсуждения канала.
# Bot API не отдаёт историю: бот получает только новые сообщения, Telegram хранит их для него 24 часа.
# Сервер лишь пересказывает их странице, а копит и фильтрует по посту страница (в хранилище браузера).
# Токен держим только в памяти и никогда не кладём в адреса страницы.
TG_TOKEN_RE = re.compile(r"\d{5,}:[\w-]{30,}", re.ASCII)
tg_bot = {"token": None, "photos": {}}
tg_lock = threading.Lock()


def tg_api(method, **params):
    token = tg_bot["token"]
    if not token:
        raise ValueError("Бот не подключён: откройте ⚙ справа от вкладок и вставьте токен.")
    body = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in params.items()}).encode()
    try:
        raw = fetch(f"https://api.telegram.org/bot{token}/{method}", data=body)
    except urllib.error.HTTPError as e:
        try:
            description = json.loads(e.read() or b"{}").get("description", "")
        except ValueError:
            description = ""
        if e.code in (401, 404):
            raise ValueError("Telegram не принял токен — проверьте, что скопировали его целиком.")
        if e.code == 409:
            raise ValueError("Этого бота уже опрашивает другая программа или у него включён вебхук. "
                             "Создайте для Крутки отдельного бота.")
        raise ValueError(f"Telegram: {description or e.code}")
    return json.loads(raw)["result"]


def tg_person(m):
    """Автор сообщения: человек или канал, от имени которого написан комментарий."""
    chat = m.get("sender_chat")
    if chat and chat["id"] == m["chat"]["id"]:
        return None  # анонимный администратор группы обсуждения
    if chat:
        return {"key": f"tgc:{chat['id']}", "name": chat.get("title", ""), "username": chat.get("username"),
                "avatar": f"/api/tgbot/photo?chat={chat['id']}"}
    user = m.get("from") or {}
    if not user or user.get("is_bot"):
        return None
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
    return {"key": f"tg:{user['id']}", "name": name, "username": user.get("username"),
            "avatar": f"/api/tgbot/photo?user={user['id']}"}


def tg_root(m):
    """Если сообщение — пост канала, автоматически пересланный в группу обсуждения: (id канала, id поста)."""
    if not m.get("is_automatic_forward"):
        return None
    origin = m.get("forward_origin") or {}
    if origin.get("type") == "channel":
        return origin["chat"]["id"], origin["message_id"], origin["chat"].get("title", "")
    if m.get("forward_from_chat"):
        return m["forward_from_chat"]["id"], m.get("forward_from_message_id"), m["forward_from_chat"].get("title", "")
    return None


def tg_parse(updates):
    roots, comments = [], []
    for update in updates:
        m = update.get("message")
        if not m:
            continue
        group = m["chat"]["id"]
        for candidate in (m, m.get("reply_to_message") or {}):
            root = tg_root(candidate) if candidate else None
            if root:
                roots.append({"g": group, "m": candidate["message_id"], "c": root[0], "p": root[1], "title": root[2]})
        thread = m.get("message_thread_id") or (m.get("reply_to_message") or {}).get("message_id")
        person = None if tg_root(m) or not thread else tg_person(m)
        if person:
            comments.append({**person, "g": group, "t": thread, "id": m["message_id"], "date": m.get("date", 0),
                             "text": m.get("text") or m.get("caption") or ""})
    return roots, comments


def tg_connect(token):
    token = (token or "").strip()
    if not TG_TOKEN_RE.fullmatch(token):
        raise ValueError("Это не похоже на токен бота. Он выглядит так: 1234567890:AAH…")
    with tg_lock:
        previous = dict(tg_bot)
        tg_bot.update(token=token, photos={})
        try:
            me = tg_api("getMe")
            if tg_api("getWebhookInfo").get("url"):
                raise ValueError("У этого бота включён вебхук — создайте для Крутки отдельного бота.")
        except Exception:
            tg_bot.update(previous)  # прежний бот продолжает работать
            raise
    return {"username": me.get("username"), "name": me.get("first_name")}


def tg_updates(offset):
    """Одна пачка (до 100) накопленных для бота сообщений начиная с offset.

    Запрос с offset подтверждает Telegram получение всех предыдущих — и он их удаляет. Поэтому за раз
    берём одну пачку: страница сохраняет её и только потом просит следующую (more=True).
    """
    offset = int(offset or 0)
    with tg_lock:
        updates = tg_api("getUpdates", offset=offset, limit=100, timeout=0, allowed_updates=["message"])
    roots, comments = tg_parse(updates)
    next_offset = updates[-1]["update_id"] + 1 if updates else offset
    return {"offset": next_offset, "roots": roots, "comments": comments, "more": len(updates) == 100}


def tg_photo(kind, ident):
    """Аватарка пользователя или канала (байты jpg) или None. Кэшируется до перезапуска."""
    key = f"{kind}:{ident}"
    if key in tg_bot["photos"]:
        return tg_bot["photos"][key]
    data = None
    try:
        if kind == "user":
            sizes = (tg_api("getUserProfilePhotos", user_id=int(ident), limit=1).get("photos") or [[]])[0]
            file_id = sizes[0]["file_id"] if sizes else None
        else:
            file_id = (tg_api("getChat", chat_id=int(ident)).get("photo") or {}).get("small_file_id")
        if file_id:
            path = tg_api("getFile", file_id=file_id)["file_path"]
            req = urllib.request.Request(f"https://api.telegram.org/file/bot{tg_bot['token']}/{path}",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
    except (ValueError, OSError, KeyError):
        return None  # временный сбой — попробуем снова при следующем запросе
    tg_bot["photos"][key] = data
    return data


POST_API = {
    "/api/tgbot/connect": lambda b: tg_connect(str(b.get("token") or "")),
    "/api/tgbot/disconnect": lambda b: tg_bot.update(token=None, photos={}) or {"ok": True},
    "/api/tgbot/updates": lambda b: tg_updates(b.get("offset")),
}


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
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost не через прокси
    try:
        with opener.open(f"http://{HOST}:{port}/api/ping", timeout=2) as resp:
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
