#!/usr/bin/env python3
"""
Профессиональный Telegram-бот: YouTube → Audio / Video
• Кнопки выбора формата и качества
• MP3 320 / 192 / 128
• Лучшее аудио (m4a/opus)
• Видео 1080p / 720p / 480p / 360p
• Красивый интерфейс + анти-спам
"""

import os
import re
import time
import logging
import tempfile
import shutil
import asyncio
from pathlib import Path
from collections import defaultdict
from typing import Optional

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
import yt_dlp

load_dotenv()

# ====================== НАСТРОЙКИ ======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN. Создай файл .env")

MAX_FILE_SIZE = 49 * 1024 * 1024          # ~49 МБ
COOLDOWN_SECONDS = 25
MAX_DURATION = 90 * 60                    # 90 минут максимум

YOUTUBE_REGEX = re.compile(
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/[\w\-?=&%.]+",
    re.IGNORECASE,
)

# user_id → timestamp последнего запроса
last_request: dict[int, float] = defaultdict(float)
# video_id → info
pending: dict[str, dict] = {}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ====================== ВСПОМОГАТЕЛЬНЫЕ ======================
def is_youtube_url(text: str) -> bool:
    return bool(YOUTUBE_REGEX.search(text.strip()))


def extract_info(url: str) -> dict:
    """Получает информацию о видео без скачивания."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "id": info.get("id"),
            "title": info.get("title") or "Без названия",
            "duration": info.get("duration") or 0,
            "uploader": info.get("uploader") or info.get("channel") or "—",
            "thumbnail": info.get("thumbnail"),
            "webpage_url": info.get("webpage_url") or url,
            "view_count": info.get("view_count"),
        }


def format_duration(seconds: int) -> str:
    if not seconds:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_views(n: Optional[int]) -> str:
    if not n:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M просмотров"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K просмотров"
    return f"{n} просмотров"


def build_keyboard(video_id: str) -> InlineKeyboardMarkup:
    """Красивые кнопки: MP3 (включая 320) + MP4."""
    buttons = [
        # Главная кнопка — MP3 320
        [
            InlineKeyboardButton("🎵 MP3 320 kbps ★", callback_data=f"a|{video_id}|320"),
        ],
        [
            InlineKeyboardButton("🎵 MP3 192 kbps", callback_data=f"a|{video_id}|192"),
            InlineKeyboardButton("🎵 MP3 128 kbps", callback_data=f"a|{video_id}|128"),
        ],
        [
            InlineKeyboardButton("🎧 Лучшее аудио", callback_data=f"a|{video_id}|best"),
        ],
        # Видео как MP4
        [
            InlineKeyboardButton("🎬 MP4 1080p", callback_data=f"v|{video_id}|1080"),
            InlineKeyboardButton("🎬 MP4 720p", callback_data=f"v|{video_id}|720"),
        ],
        [
            InlineKeyboardButton("🎬 MP4 480p", callback_data=f"v|{video_id}|480"),
            InlineKeyboardButton("🎬 MP4 360p", callback_data=f"v|{video_id}|360"),
        ],
        [
            InlineKeyboardButton("❌ Отмена", callback_data=f"x|{video_id}|0"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def download_media(url: str, media_type: str, quality: str, output_dir: str) -> Path:
    """
    media_type: 'a' = audio, 'v' = video
    quality: '320','192','128','best' или '1080','720'...
    """
    outtmpl = os.path.join(output_dir, "%(title).70s [%(id)s].%(ext)s")

    if media_type == "a":
        if quality == "best":
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": outtmpl,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "postprocessors": [
                    {"key": "FFmpegExtractAudio"},
                ],
            }
        else:
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": outtmpl,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": quality,
                    }
                ],
            }
    else:
        height = quality
        ydl_opts = {
            "format": f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={height}][ext=mp4]/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "merge_output_format": "mp4",
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    files = list(Path(output_dir).glob("*.*"))
    files = [f for f in files if f.suffix.lower() in {".mp3", ".m4a", ".opus", ".webm", ".mp4", ".mkv"}]
    if not files:
        raise FileNotFoundError("Файл не найден после скачивания")
    return max(files, key=lambda f: f.stat().st_size)


# ====================== ХЕНДЛЕРЫ ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>YouTube Downloader Pro</b>\n\n"
        "Просто отправь ссылку на YouTube-видео.\n"
        "Я покажу превью и кнопки выбора формата.\n\n"
        "<b>Доступно:</b>\n"
        "• 🎵 <b>MP3 320 kbps</b> (максимальное качество)\n"
        "• 🎵 MP3 192 / 128 kbps\n"
        "• 🎧 Лучшее аудио (без перекодирования)\n"
        "• 🎬 <b>MP4</b> 1080p / 720p / 480p / 360p\n\n"
        "Отправь ссылку 👇"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Как пользоваться</b>\n\n"
        "1. Отправь ссылку на YouTube\n"
        "2. Выбери нужный формат кнопкой\n"
        "3. Получи файл\n\n"
        "<b>Ограничения</b>\n"
        "• Максимум ~90 минут\n"
        "• Файл ≤ 50 МБ (лимит Telegram)\n"
        "• Пауза 25 сек между запросами\n"
        "• Только одиночные видео",
        parse_mode=ParseMode.HTML,
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    if not is_youtube_url(text):
        await update.message.reply_text(
            "Это не похоже на ссылку YouTube.\n"
            "Поддерживаются:\n"
            "• youtube.com\n• youtu.be\n• music.youtube.com"
        )
        return

    now = time.time()
    if now - last_request[user.id] < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last_request[user.id]))
        await update.message.reply_text(f"⏳ Подожди ещё {wait} сек.")
        return
    last_request[user.id] = now

    status = await update.message.reply_text("🔍 Получаю информацию о видео...")

    try:
        info = await asyncio.to_thread(extract_info, text)

        if info["duration"] > MAX_DURATION:
            await status.edit_text(
                f"❌ Видео слишком длинное ({format_duration(info['duration'])}).\n"
                f"Максимум {MAX_DURATION // 60} минут."
            )
            return

        vid = info["id"]
        pending[vid] = {
            "url": info["webpage_url"],
            "title": info["title"],
            "duration": info["duration"],
            "uploader": info["uploader"],
        }

        caption = (
            f"<b>{info['title'][:80]}</b>\n\n"
            f"👤 {info['uploader']}\n"
            f"⏱ {format_duration(info['duration'])}"
        )
        if info.get("view_count"):
            caption += f"\n👁 {format_views(info['view_count'])}"

        caption += "\n\nВыбери формат:"

        keyboard = build_keyboard(vid)

        if info.get("thumbnail"):
            try:
                await status.delete()
                await update.message.reply_photo(
                    photo=info["thumbnail"],
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                return
            except Exception:
                pass

        await status.edit_text(
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.exception("extract error")
        await status.edit_text(f"❌ Не удалось получить информацию.\n{str(e)[:150]}")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    parts = data.split("|")
    if len(parts) != 3:
        await query.edit_message_text("❌ Некорректные данные.")
        return

    action, video_id, quality = parts

    if action == "x":
        try:
            await query.edit_message_caption(caption="❌ Отменено.")
        except BadRequest:
            try:
                await query.edit_message_text("❌ Отменено.")
            except BadRequest:
                pass
        pending.pop(video_id, None)
        return

    info = pending.get(video_id)
    if not info:
        await query.edit_message_text("⏳ Сессия устарела. Отправь ссылку заново.")
        return

    url = info["url"]
    title = info["title"]

    try:
        if query.message.photo:
            await query.edit_message_caption(
                caption=f"⏳ Скачиваю...\n<b>{title[:60]}</b>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text(
                f"⏳ Скачиваю...\n<b>{title[:60]}</b>",
                parse_mode=ParseMode.HTML,
            )
    except BadRequest:
        pass

    await context.bot.send_chat_action(
        chat_id=query.message.chat_id,
        action=ChatAction.UPLOAD_DOCUMENT,
    )

    temp_dir = tempfile.mkdtemp(prefix="ytpro_")
    try:
        filepath = await asyncio.to_thread(
            download_media, url, action, quality, temp_dir
        )

        size = filepath.stat().st_size
        if size > MAX_FILE_SIZE:
            await query.message.reply_text(
                f"❌ Файл слишком большой ({size / 1024 / 1024:.1f} МБ).\n"
                "Попробуй более низкое качество."
            )
            return

        with open(filepath, "rb") as f:
            if action == "a":
                await query.message.reply_audio(
                    audio=f,
                    title=title[:60],
                    performer="YouTube",
                    filename=filepath.name,
                    caption=f"✅ <b>{title[:70]}</b>\nФормат: MP3 {quality} kbps",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await query.message.reply_video(
                    video=f,
                    caption=f"✅ <b>{title[:70]}</b>\nФормат: MP4 {quality}p",
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                )

        try:
            if query.message.photo:
                await query.edit_message_caption(
                    caption=f"✅ Готово: <b>{title[:60]}</b>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await query.edit_message_text(
                    f"✅ Готово: <b>{title[:60]}</b>",
                    parse_mode=ParseMode.HTML,
                )
        except BadRequest:
            pass

    except Exception as e:
        logger.exception("download error")
        err = str(e).lower()
        if "private" in err or "sign in" in err:
            msg = "❌ Видео приватное."
        elif "unavailable" in err:
            msg = "❌ Видео недоступно."
        elif "429" in err:
            msg = "❌ YouTube временно ограничил запросы. Попробуй позже."
        else:
            msg = f"❌ Ошибка: {str(e)[:160]}"
        await query.message.reply_text(msg)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    logger.info("🚀 Профессиональный бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
