import asyncio
import logging
import os
import sqlite3
import json
from datetime import datetime, time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from anthropic import Anthropic
from aiohttp import web

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")  # опционально, для голосовых

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

claude = Anthropic(api_key=ANTHROPIC_API_KEY)
scheduler = AsyncIOScheduler()

DB_PATH = "shopping.db"


# ---------- DB ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            checked INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reminders (
            user_id INTEGER PRIMARY KEY,
            hour INTEGER NOT NULL,
            minute INTEGER NOT NULL
        )"""
    )
    conn.commit()
    conn.close()


def add_items(user_id: int, names: list[str]):
    conn = db()
    conn.executemany(
        "INSERT INTO items (user_id, name) VALUES (?, ?)",
        [(user_id, n.strip()) for n in names if n.strip()],
    )
    conn.commit()
    conn.close()


def get_items(user_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT id, name, checked FROM items WHERE user_id=? ORDER BY id", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def clear_items(user_id: int):
    conn = db()
    conn.execute("DELETE FROM items WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def toggle_item(user_id: int, item_id: int):
    conn = db()
    row = conn.execute(
        "SELECT checked FROM items WHERE id=? AND user_id=?", (item_id, user_id)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE items SET checked=? WHERE id=?", (0 if row["checked"] else 1, item_id)
        )
        conn.commit()
    conn.close()


def set_reminder(user_id: int, hour: int, minute: int):
    conn = db()
    conn.execute(
        "INSERT INTO reminders (user_id, hour, minute) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET hour=excluded.hour, minute=excluded.minute",
        (user_id, hour, minute),
    )
    conn.commit()
    conn.close()


def get_all_reminders():
    conn = db()
    rows = conn.execute("SELECT user_id, hour, minute FROM reminders").fetchall()
    conn.close()
    return rows


def remove_reminder(user_id: int):
    conn = db()
    conn.execute("DELETE FROM reminders WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


# ---------- Keyboards ----------

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍽 По блюду"), KeyboardButton(text="✍️ Вручную")],
        [KeyboardButton(text="📋 Список"), KeyboardButton(text="🗑 Очистить")],
        [KeyboardButton(text="⏰ Напоминание")],
    ],
    resize_keyboard=True,
)


# ---------- States ----------

class States(StatesGroup):
    waiting_dish = State()
    waiting_portions = State()
    waiting_manual = State()
    waiting_reminder_time = State()


pending_dish: dict[int, str] = {}


# ---------- Helpers ----------

def format_list(rows) -> str:
    if not rows:
        return "Список пуст."
    lines = []
    for r in rows:
        mark = "✅" if r["checked"] else "▫️"
        lines.append(f"{mark} {r['name']} (#{r['id']})")
    return "\n".join(lines)


def ask_claude_ingredients_sync(dish: str, portions: int) -> list[str]:
    prompt = (
        f"Дай список продуктов для блюда «{dish}» на {portions} порций(и). "
        "Ответь ТОЛЬКОJSON-массивом строк вида \"Название — количество\", "
        "без markdown и пояснений. Пример: [\"Мука — 200 г\", \"Яйца — 2 шт\"]"
    )
    resp = claude.messages.create(
        model="claude-sonnet-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return [line.strip("-• ") for line in text.splitlines() if line.strip()]


async def ask_claude_ingredients(dish: str, portions: int) -> list[str]:
    return await asyncio.to_thread(ask_claude_ingredients_sync, dish, portions)


async def transcribe_voice(file_path: str) -> str | None:
    if not OPENAI_API_KEY:
        return None
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        with open(file_path, "rb") as f:
            result = client.audio.transcriptions.create(model="whisper-1", file=f)
        return result.text
    except Exception:
        return None
    finally:
        try:
            os.remove(file_path)
        except OSError:
            pass


# ---------- Handlers ----------

@router.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я собираю список покупок.\n\n"
        "🍽 По блюду — назови блюдо, накидаю ингредиенты\n"
        "✍️ Вручную — впиши продукты сам (через запятую или голосом)\n"
        "📋 Список — показать текущий список\n"
        "⏰ Напоминание — во сколько присылать список каждый день",
        reply_markup=MAIN_KB,
    )


@router.message(F.text == "🍽 По блюду")
async def ask_dish(message: Message, state: FSMContext):
    await state.set_state(States.waiting_dish)
    await message.answer("Какое блюдо?", reply_markup=ReplyKeyboardRemove())


@router.message(StateFilter(States.waiting_dish))
async def got_dish(message: Message, state: FSMContext):
    pending_dish[message.from_user.id] = message.text
    await state.set_state(States.waiting_portions)
    await message.answer("На сколько порций? (число)")


@router.message(StateFilter(States.waiting_portions))
async def got_portions(message: Message, state: FSMContext):
    try:
        portions = int(message.text.strip())
    except ValueError:
        await message.answer("Пришли просто число, например 4")
        return
    dish = pending_dish.pop(message.from_user.id, "")
    if not dish.strip():
        await state.clear()
        await message.answer("Название блюда пустое, начни заново.", reply_markup=MAIN_KB)
        return
    await message.answer("Считаю...")
    try:
        ingredients = await ask_claude_ingredients(dish, portions)
    except Exception:
        await state.clear()
        await message.answer(
            "Не получилось спросить Claude — сбой API или закончился баланс. Попробуй ещё раз чуть позже.",
            reply_markup=MAIN_KB,
        )
        return
    add_items(message.from_user.id, ingredients)
    await state.clear()
    await message.answer(
        f"Добавил в список для «{dish}»:\n\n" + "\n".join(f"• {i}" for i in ingredients),
        reply_markup=MAIN_KB,
    )


@router.message(F.text == "✍️ Вручную")
async def ask_manual(message: Message, state: FSMContext):
    await state.set_state(States.waiting_manual)
    await message.answer(
        "Пиши продукты через запятую, одним сообщением или голосом.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(StateFilter(States.waiting_manual), F.voice)
async def manual_voice(message: Message, state: FSMContext):
    file = await bot.get_file(message.voice.file_id)
    local_path = f"/tmp/{message.voice.file_id}.oga"
    await bot.download_file(file.file_path, local_path)
    text = await transcribe_voice(local_path)
    if not text:
        await message.answer(
            "Не получилось распознать голосовое (нет ключа или сбой сервиса). Напиши текстом."
        )
        return
    names = [n.strip() for n in text.split(",")]
    add_items(message.from_user.id, names)
    await state.clear()
    await message.answer(f"Добавил: {', '.join(names)}", reply_markup=MAIN_KB)


@router.message(StateFilter(States.waiting_manual))
async def manual_text(message: Message, state: FSMContext):
    names = [n.strip() for n in message.text.split(",")]
    add_items(message.from_user.id, names)
    await state.clear()
    await message.answer(f"Добавил: {', '.join(names)}", reply_markup=MAIN_KB)


@router.message(F.text == "📋 Список")
async def show_list(message: Message):
    rows = get_items(message.from_user.id)
    await message.answer(format_list(rows))
    if rows:
        await message.answer("Отметить купленное: /done ID (например /done 3)")


@router.message(Command("done"))
async def mark_done(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /done ID")
        return
    toggle_item(message.from_user.id, int(parts[1]))
    rows = get_items(message.from_user.id)
    await message.answer(format_list(rows))


@router.message(F.text == "🗑 Очистить")
async def clear_list(message: Message):
    clear_items(message.from_user.id)
    await message.answer("Список очищен.", reply_markup=MAIN_KB)


@router.message(F.text == "⏰ Напоминание")
async def ask_reminder(message: Message, state: FSMContext):
    await state.set_state(States.waiting_reminder_time)
    await message.answer(
        "Во сколько присылать список каждый день? Формат ЧЧ:ММ (например 18:00). "
        "Пришли \"выкл\", чтобы отключить.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(StateFilter(States.waiting_reminder_time))
async def got_reminder_time(message: Message, state: FSMContext):
    text = message.text.strip().lower()
    await state.clear()
    if text in ("выкл", "off", "отключить"):
        remove_reminder(message.from_user.id)
        unschedule_reminder(message.from_user.id)
        await message.answer("Напоминание отключено.", reply_markup=MAIN_KB)
        return
    try:
        hour, minute = map(int, text.split(":"))
    except ValueError:
        await message.answer("Не понял время. Формат ЧЧ:ММ, например 18:00")
        return
    if not (0 <= hour < 24 and 0 <= minute < 60):
        await message.answer("Часы 0-23, минуты 0-59. Формат ЧЧ:ММ, например 18:00")
        return
    set_reminder(message.from_user.id, hour, minute)
    schedule_reminder(message.from_user.id, hour, minute)
    await message.answer(f"Готово, буду напоминать в {hour:02d}:{minute:02d} (время сервера, UTC).", reply_markup=MAIN_KB)


# ---------- Scheduler ----------

def schedule_reminder(user_id: int, hour: int, minute: int):
    job_id = f"reminder_{user_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        send_reminder,
        "cron",
        hour=hour,
        minute=minute,
        id=job_id,
        args=[user_id],
    )


def unschedule_reminder(user_id: int):
    job_id = f"reminder_{user_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


async def send_reminder(user_id: int):
    rows = get_items(user_id)
    if not rows:
        return
    await bot.send_message(user_id, "Напоминание о покупках:\n\n" + format_list(rows))


def load_reminders():
    for r in get_all_reminders():
        schedule_reminder(r["user_id"], r["hour"], r["minute"])


# ---------- Фиктивный HTTP-сервер (для Render Web Service + UptimeRobot) ----------

async def health(request):
    return web.Response(text="ok")


async def start_http_server():
    port = int(os.environ.get("PORT", 10000))
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


# ---------- Entrypoint ----------

async def main():
    init_db()
    load_reminders()
    scheduler.start()
    await start_http_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
