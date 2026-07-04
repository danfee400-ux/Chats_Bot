# bot.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from telegram import Update, ReactionTypeEmoji
from telegram.error import TelegramError, TimedOut, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================== НАСТРОЙКИ ==================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
#7770851583:AAHuPLv95jtUbVQttkecwY3U7ROuT5WcBnQ
# ID группы-форума. Обычно выглядит так: -1001234567890
# Сначала можешь поставить 0, запустить бота, добавить в группу и написать /id в группе.
SUPPORT_GROUP_ID = int(os.getenv("SUPPORT_GROUP_ID", "0"))
    #-1004303968055

# Твои Telegram ID. Узнать можно командой /myid в личке с ботом.
# Если оставить пустым, команды /m /mute /ban смогут использовать админы группы.



OWNER_IDS = {
    int(x)
    for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",")
    if x
}
#7441694878


DB_FILE = Path("bd.json")

# =================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error

    if isinstance(error, (TimedOut, NetworkError)):
        logging.warning("Временная ошибка сети Telegram: %s", error)
        return

    logging.error(
        "Ошибка в обработчике.",
        exc_info=(type(error), error, error.__traceback__),
    )

DB_LOCK = asyncio.Lock()


def load_db() -> dict:
    if not DB_FILE.exists():
        return {"users": {}}

    try:
        with DB_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        data = {"users": {}}

    data.setdefault("users", {})
    return data


def save_db(data: dict) -> None:
    tmp = DB_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(DB_FILE)


def user_display_name(user) -> str:
    if user.username:
        return f"@{user.username}"

    name = " ".join(part for part in [user.first_name, user.last_name] if part)
    return name or f"user_{user.id}"


def make_topic_name(user) -> str:
    # Название темы в Telegram: 1-128 символов.
    raw_name = user_display_name(user)
    raw_name = re.sub(r"\s+", " ", raw_name).strip()

    topic_name = f"{raw_name} ({user.id})"
    if len(topic_name) > 128:
        max_name_len = 128 - len(f" ({user.id})")
        topic_name = f"{raw_name[:max_name_len]} ({user.id})"

    return topic_name


def format_time_left(seconds: int) -> str:
    seconds = max(0, int(seconds))

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes}м")
    if seconds or not parts:
        parts.append(f"{seconds}с")

    return " ".join(parts)


def parse_duration(args: list[str]) -> int | None:
    """
    Поддерживает:
    /mute 10 m
    /mute 10m
    /mute 2 h
    /mute 1 d
    """
    if not args:
        return None

    if len(args) >= 2:
        number = args[0]
        unit = args[1]
    else:
        match = re.fullmatch(r"(\d+)([smhd])", args[0].lower())
        if not match:
            return None
        number, unit = match.group(1), match.group(2)

    if not number.isdigit():
        return None

    value = int(number)
    unit = unit.lower()

    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
    }

    if unit not in multipliers:
        return None

    return value * multipliers[unit]


async def is_allowed_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return False

    if OWNER_IDS:
        return user.id in OWNER_IDS

    if chat.id != SUPPORT_GROUP_ID:
        return False

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False


def get_user_id_by_thread(db: dict, thread_id: int | None) -> int | None:
    if thread_id is None:
        return None

    for user_id, data in db["users"].items():
        if data.get("topic_id") == thread_id:
            return int(user_id)

    return None


async def ensure_user_topic(user, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Создает тему для пользователя, если ее еще нет в bd.json.
    Возвращает message_thread_id темы.
    """
    if SUPPORT_GROUP_ID == 0:
        raise RuntimeError("SUPPORT_GROUP_ID не настроен.")

    async with DB_LOCK:
        db = load_db()
        user_id = str(user.id)

        record = db["users"].setdefault(user_id, {})
        record["id"] = user.id
        record["username"] = user.username
        record["name"] = user_display_name(user)
        record.setdefault("muted_until", 0)
        record.setdefault("banned", False)

        if record.get("topic_id"):
            save_db(db)
            return int(record["topic_id"])

        topic_name = make_topic_name(user)

        topic = await context.bot.create_forum_topic(
            chat_id=SUPPORT_GROUP_ID,
            name=topic_name,
        )

        record["topic_id"] = topic.message_thread_id
        record["topic_name"] = topic_name

        save_db(db)

    await context.bot.send_message(
        chat_id=SUPPORT_GROUP_ID,
        message_thread_id=topic.message_thread_id,
        text=(
            "Новый диалог создан.\n\n"
            f"Пользователь: {user_display_name(user)}\n"
            f"ID: {user.id}\n\n"
            "Ответить пользователю: /m текст\n"
            "Замутить: /mute 10 m\n"
            "Забанить: /ban"
        ),
    )

    return topic.message_thread_id


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    try:
        await ensure_user_topic(user, context)
    except Exception as e:
        logging.exception("Не удалось создать тему")
        await update.message.reply_text(
            "Бот пока не настроен. Напиши владельцу, что группа/темы не подключены."
        )
        return

    await update.message.reply_text(
        "Напиши сообщение сюда, скоро вам ответят"
    )


async def react_heart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await context.bot.set_message_reaction(
            chat_id=update.effective_chat.id,
            message_id=update.effective_message.message_id,
            reaction=[ReactionTypeEmoji("❤")],
            is_big=False,
        )
    except TelegramError:
        logging.warning("Не удалось поставить реакцию на сообщение.", exc_info=True)

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(f"Твой Telegram ID: {user.id}")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    thread_id = update.effective_message.message_thread_id

    text = f"ID этого чата: {chat.id}"
    if thread_id:
        text += f"\nID этой темы: {thread_id}"

    await update.message.reply_text(text)


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message

    async with DB_LOCK:
        db = load_db()
        record = db["users"].get(str(user.id))

        if record:
            if record.get("banned"):
                await message.reply_text("Вы заблокированы.")
                return

            muted_until = int(record.get("muted_until", 0))
            now = int(time.time())

            if muted_until > now:
                left = format_time_left(muted_until - now)
                await message.reply_text(f"Вас не слышно. До окончания мута {left}")
                return

    try:
        topic_id = await ensure_user_topic(user, context)

        # Копируем любое обычное сообщение: текст, фото, видео, файл, стикер и т.д.
        # copy_message не показывает ссылку на оригинал, но переносит содержимое.
        await context.bot.copy_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=topic_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )

    except TelegramError as e:
        logging.exception("Ошибка пересылки сообщения")
        await message.reply_text(
            "Не получилось передать сообщение владельцу. Возможно, бот не имеет прав в группе."
        )
    except Exception:
        logging.exception("Неизвестная ошибка")
        await message.reply_text("Ошибка настройки бота.")


async def cmd_m(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != SUPPORT_GROUP_ID:
        await update.message.reply_text("Команда /m работает только в группе поддержки.")
        return

    if not await is_allowed_admin(update, context):
        await update.message.reply_text("Нет прав.")
        return

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Использование: /m текст сообщения")
        return

    thread_id = update.effective_message.message_thread_id

    async with DB_LOCK:
        db = load_db()
        target_user_id = get_user_id_by_thread(db, thread_id)

    if not target_user_id:
        await update.message.reply_text(
            "Не нашел пользователя для этой темы. Возможно, bd.json был удален или тема создана вручную."
        )
        return

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=text,
        )
        await react_heart(update, context)
    except TelegramError:
        logging.exception("Не удалось отправить сообщение пользователю")
        await update.message.reply_text(
            "Не удалось отправить сообщение. Возможно, пользователь заблокировал бота."
        )


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != SUPPORT_GROUP_ID:
        await update.message.reply_text("Команда /mute работает только в группе поддержки.")
        return

    if not await is_allowed_admin(update, context):
        await update.message.reply_text("Нет прав.")
        return

    duration = parse_duration(context.args)
    if duration is None:
        await update.message.reply_text(
            "Использование: /mute 10 m\n"
            "Величины времени: s, m, h, d\n"
            "Пример: /mute 30 m"
        )
        return

    thread_id = update.effective_message.message_thread_id
    now = int(time.time())
    muted_until = now + duration

    async with DB_LOCK:
        db = load_db()
        target_user_id = get_user_id_by_thread(db, thread_id)

        if not target_user_id:
            await update.message.reply_text("Не нашел пользователя для этой темы.")
            return

        record = db["users"][str(target_user_id)]
        record["muted_until"] = muted_until
        save_db(db)

    await update.message.reply_text(
        f"Пользователь замучен на {format_time_left(duration)}."
    )


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != SUPPORT_GROUP_ID:
        return

    if not await is_allowed_admin(update, context):
        await update.message.reply_text("Нет прав.")
        return

    thread_id = update.effective_message.message_thread_id

    async with DB_LOCK:
        db = load_db()
        target_user_id = get_user_id_by_thread(db, thread_id)

        if not target_user_id:
            await update.message.reply_text("Не нашел пользователя для этой темы.")
            return

        db["users"][str(target_user_id)]["muted_until"] = 0
        save_db(db)

    await update.message.reply_text("Мут снят.")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != SUPPORT_GROUP_ID:
        await update.message.reply_text("Команда /ban работает только в группе поддержки.")
        return

    if not await is_allowed_admin(update, context):
        await update.message.reply_text("Нет прав.")
        return

    thread_id = update.effective_message.message_thread_id

    async with DB_LOCK:
        db = load_db()
        target_user_id = get_user_id_by_thread(db, thread_id)

        if not target_user_id:
            await update.message.reply_text("Не нашел пользователя для этой темы.")
            return

        db["users"][str(target_user_id)]["banned"] = True
        save_db(db)

    await update.message.reply_text("Пользователь заблокирован.")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != SUPPORT_GROUP_ID:
        return

    if not await is_allowed_admin(update, context):
        await update.message.reply_text("Нет прав.")
        return

    thread_id = update.effective_message.message_thread_id

    async with DB_LOCK:
        db = load_db()
        target_user_id = get_user_id_by_thread(db, thread_id)

        if not target_user_id:
            await update.message.reply_text("Не нашел пользователя для этой темы.")
            return

        db["users"][str(target_user_id)]["banned"] = False
        save_db(db)

    await update.message.reply_text("Пользователь разблокирован.")


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != SUPPORT_GROUP_ID:
        return

    if not await is_allowed_admin(update, context):
        await update.message.reply_text("Нет прав.")
        return

    thread_id = update.effective_message.message_thread_id

    async with DB_LOCK:
        db = load_db()
        target_user_id = get_user_id_by_thread(db, thread_id)

        if not target_user_id:
            await update.message.reply_text("Не нашел пользователя для этой темы.")
            return

        record = db["users"][str(target_user_id)]

    muted_until = int(record.get("muted_until", 0))
    now = int(time.time())

    if muted_until > now:
        mute_status = f"да, осталось {format_time_left(muted_until - now)}"
    else:
        mute_status = "нет"

    await update.message.reply_text(
        f"ID: {record.get('id')}\n"
        f"Ник: {record.get('name')}\n"
        f"Username: {record.get('username')}\n"
        f"Тема: {record.get('topic_name')}\n"
        f"Мут: {mute_status}\n"
        f"Бан: {'да' if record.get('banned') else 'нет'}"
    )


def main() -> None:
    if BOT_TOKEN == "ВСТАВЬ_ТОКЕН_БОТА":
        raise RuntimeError("Вставь токен бота в BOT_TOKEN.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("id", cmd_id))

    app.add_handler(CommandHandler("m", cmd_m))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("info", cmd_info))

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            handle_private_message,
        )
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()