import json
import logging
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

import requests


VK_API_URL = "https://api.vk.ru/method"
VK_API_VERSION = os.getenv("VK_API_VERSION", "5.199")
GROUP_ID = int(os.getenv("VK_GROUP_ID", "98171239"))
TOKEN = os.getenv("VK_TOKEN") or os.getenv("BOT_TOKEN")
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("VK_ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
SEND_DELAY = float(os.getenv("VK_SEND_DELAY", "0.08"))

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
if not DATA_DIR.parent.exists():
    DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "subscribers.db"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("vk-mailing-bot")


class VkApiError(RuntimeError):
    def __init__(self, error: dict):
        self.code = error.get("error_code")
        super().__init__(error.get("error_msg", "Неизвестная ошибка VK API"))


def api(method: str, **params):
    params.update(access_token=TOKEN, v=VK_API_VERSION)
    response = requests.post(
        f"{VK_API_URL}/{method}", data=params, timeout=30
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise VkApiError(payload["error"])
    return payload["response"]


def db_connection():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with db_connection() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1,
                subscribed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def set_subscription(user_id: int, active: bool):
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO subscribers (user_id, active)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                active = excluded.active,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, int(active)),
        )


def active_subscribers():
    with db_connection() as connection:
        rows = connection.execute(
            "SELECT user_id FROM subscribers WHERE active = 1 ORDER BY user_id"
        ).fetchall()
    return [row["user_id"] for row in rows]


def keyboard():
    return json.dumps(
        {
            "one_time": False,
            "inline": False,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "text",
                            "label": "Подписаться",
                            "payload": json.dumps({"command": "subscribe"}),
                        },
                        "color": "positive",
                    },
                    {
                        "action": {
                            "type": "text",
                            "label": "Стоп",
                            "payload": json.dumps({"command": "unsubscribe"}),
                        },
                        "color": "negative",
                    },
                ]
            ],
        },
        ensure_ascii=False,
    )


def send_message(user_id: int, message: str, attachment: str = "", show_keyboard=False):
    params = {
        "peer_id": user_id,
        "random_id": random.randint(1, 2_147_483_647),
        "message": message,
    }
    if attachment:
        params["attachment"] = attachment
    if show_keyboard:
        params["keyboard"] = keyboard()
    return api("messages.send", **params)


def attachment_string(message: dict) -> str:
    result = []
    for item in message.get("attachments", []):
        kind = item.get("type")
        obj = item.get(kind, {})
        owner_id = obj.get("owner_id")
        media_id = obj.get("id")
        if kind and owner_id is not None and media_id is not None:
            access_key = obj.get("access_key")
            value = f"{kind}{owner_id}_{media_id}"
            if access_key:
                value += f"_{access_key}"
            result.append(value)
    return ",".join(result)


def payload_command(message: dict) -> str:
    raw = message.get("payload")
    if not raw:
        return ""
    try:
        return str(json.loads(raw).get("command", "")).lower()
    except (TypeError, json.JSONDecodeError):
        return ""


pending_broadcasts = {}


def begin_broadcast(user_id: int, message: dict, text: str):
    body = text.partition(" ")[2].strip()
    attachment = attachment_string(message)
    if not body and not attachment:
        send_message(
            user_id,
            "После команды добавьте текст рассылки. Можно также прикрепить фото.\n\n"
            "Пример: /рассылка Завтра в 19:00 состоится встреча.",
        )
        return

    pending_broadcasts[user_id] = {"message": body, "attachment": attachment}
    count = len(active_subscribers())
    preview = body or "(только вложение)"
    send_message(
        user_id,
        f"Предпросмотр рассылки для {count} подписчиков:\n\n{preview}\n\n"
        "Для отправки напишите /подтвердить. Для отмены — /отмена.",
        attachment,
    )


def run_broadcast(admin_id: int):
    draft = pending_broadcasts.pop(admin_id, None)
    if not draft:
        send_message(admin_id, "Нет рассылки, ожидающей подтверждения.")
        return

    users = active_subscribers()
    delivered = 0
    failed = 0
    blocked = 0
    send_message(admin_id, f"Начинаю рассылку для {len(users)} подписчиков.")

    for user_id in users:
        try:
            send_message(user_id, draft["message"], draft["attachment"])
            delivered += 1
        except VkApiError as error:
            failed += 1
            # 901: пользователь запретил сообщения от сообщества.
            if error.code == 901:
                set_subscription(user_id, False)
                blocked += 1
            logger.warning("Не отправлено пользователю %s: %s", user_id, error)
        except requests.RequestException as error:
            failed += 1
            logger.warning("Сетевая ошибка для пользователя %s: %s", user_id, error)
        time.sleep(SEND_DELAY)

    send_message(
        admin_id,
        "Рассылка завершена.\n"
        f"Доставлено: {delivered}\n"
        f"Не доставлено: {failed}\n"
        f"Запретили сообщения: {blocked}",
    )


def handle_message(message: dict):
    user_id = int(message.get("from_id", 0))
    peer_id = int(message.get("peer_id", 0))
    if user_id <= 0 or peer_id != user_id:
        return

    text = message.get("text", "").strip()
    command = payload_command(message)
    normalized = text.lower()

    if command == "subscribe" or normalized in {
        "начать", "старт", "подписаться", "/start", "/subscribe"
    }:
        set_subscription(user_id, True)
        send_message(
            user_id,
            "Готово! Вы подписаны на рассылку сообщества. "
            "Отписаться можно в любой момент кнопкой «Стоп».",
            show_keyboard=True,
        )
    elif command == "unsubscribe" or normalized in {
        "стоп", "отписаться", "/stop", "/unsubscribe"
    }:
        set_subscription(user_id, False)
        send_message(
            user_id,
            "Вы отписались от рассылки. Чтобы вернуться, нажмите «Подписаться».",
            show_keyboard=True,
        )
    elif normalized in {"мой id", "мой_id", "/id"}:
        send_message(user_id, f"Ваш VK ID: {user_id}")
    elif user_id in ADMIN_IDS and (
        normalized.startswith("/рассылка") or normalized.startswith("/broadcast")
    ):
        begin_broadcast(user_id, message, text)
    elif user_id in ADMIN_IDS and normalized in {"/подтвердить", "/confirm"}:
        run_broadcast(user_id)
    elif user_id in ADMIN_IDS and normalized in {"/отмена", "/cancel"}:
        pending_broadcasts.pop(user_id, None)
        send_message(user_id, "Рассылка отменена.")
    elif user_id in ADMIN_IDS and normalized in {"/статистика", "/stats"}:
        send_message(user_id, f"Активных подписчиков: {len(active_subscribers())}")
    else:
        send_message(
            user_id,
            "Это бот рассылки сообщества. Нажмите «Подписаться», чтобы получать новости, "
            "или «Стоп», чтобы отказаться.",
            show_keyboard=True,
        )


def handle_event(event: dict):
    event_type = event.get("type")
    obj = event.get("object", {})
    if event_type == "message_new":
        message = obj.get("message", obj)
        handle_message(message)
    elif event_type == "message_deny":
        user_id = obj.get("user_id")
        if user_id:
            set_subscription(int(user_id), False)
    elif event_type == "message_allow":
        # Разрешение сообщений не считается согласием на рассылку.
        user_id = obj.get("user_id")
        if user_id:
            set_subscription(int(user_id), False)


def listen():
    logger.info("Бот запущен для сообщества %s; база: %s", GROUP_ID, DB_PATH)
    while True:
        try:
            server = api("groups.getLongPollServer", group_id=GROUP_ID)
            ts = server["ts"]
            while True:
                response = requests.get(
                    server["server"],
                    params={"act": "a_check", "key": server["key"], "ts": ts, "wait": 25},
                    timeout=35,
                )
                response.raise_for_status()
                payload = response.json()
                if "failed" in payload:
                    logger.warning("Long Poll попросил переподключение: %s", payload)
                    break
                ts = payload["ts"]
                for event in payload.get("updates", []):
                    try:
                        handle_event(event)
                    except Exception:
                        logger.exception("Ошибка обработки события")
        except (requests.RequestException, VkApiError, KeyError, ValueError) as error:
            logger.error("Ошибка Long Poll: %s. Повтор через 5 секунд.", error)
            time.sleep(5)


def main():
    if not TOKEN:
        logger.error("Не задана переменная VK_TOKEN (или BOT_TOKEN).")
        sys.exit(1)
    if not ADMIN_IDS:
        logger.warning("VK_ADMIN_IDS не задана: административные команды отключены.")
    init_db()
    listen()


if __name__ == "__main__":
    main()
