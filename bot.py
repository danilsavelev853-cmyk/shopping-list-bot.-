import asyncio
import logging
import os
import sqlite3
import json

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from anthropic import Anthropic
from aiohttp import web

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")  # опционально, для голосовых (сейчас не используется)

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


def add_items(user_id: int, names: list[str]) -> list[str]:
    cleaned = [n.strip() for n in names if n.strip()]
    if not cleaned:
        return []
    conn = db()
    conn.executemany(
        "INSERT INTO items (user_id, name) VALUES (?, ?)",
        [(user_id, n) for n in cleaned],
    )
    conn.commit()
    conn.close()
    return cleaned


def get_items(user_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT id, name, checked FROM items WHERE user_id=? ORDER BY checked, id", (user_id,)
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


def delete_item(user_id: int, item_id: int):
    conn = db()
    conn.execute("DELETE FROM items WHERE id=? AND user_id=?", (item_id, user_id))
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

CANCEL_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="🚫 Отмена", callback_data="cancel")]]
)

ADD_MORE_MANUAL_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="more_manual")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="done_adding")],
    ]
)

ADD_MORE_DISH_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="➕ Ещё блюдо", callback_data="more_dish")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="done_adding")],
    ]
)


def list_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []
    for r in rows:
        mark = "✅" if r["checked"] else "▫️"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {r['name']}", callback_data=f"toggle:{r['id']}"
                ),
                InlineKeyboardButton(text="🗑", callback_data=f"delitem:{r['id']}"),
            ]
        )
    buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data="more_manual")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


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
        lines.append(f"{mark} {r['name']}")
    return "\n".join(lines)


def ask_claude_ingredients_sync(dish: str, portions: int) -> list[str]:
    prompt = (
        f"Дай список продуктов для блюда «{dish}» на {portions} порций(и). "
        "Ответь ТОЛЬКО JSON-массивом строк вида \"Название — количество\", "
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


# ---------- Handlers: старт / отмена ----------

@router.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я собираю список покупок.\n\n"
        "🍽 По блюду — назови блюдо, накидаю ингредиенты\n"
        "✍️ Вручную — впиши продукты сам (через запятую)\n"
        "📋 Список — показать текущий список с кнопками\n"
        "⏰ Напоминание — во сколько присылать список каждый день\n\n"
        "В любой момент можно написать /cancel, чтобы выйти из текущего действия.",
        reply_markup=MAIN_KB,
    )


@router.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    pending_dish.pop(message.from_user.id, None)
    await message.answer("Отменено.", reply_markup=MAIN_KB)


@router.callback_query(F.data == "cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    pending_dish.pop(callback.from_user.id, None)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Отменено.", reply_markup=MAIN_KB)
    await callback.answer()


@router.callback_query(F.data == "done_adding")
async def done_adding(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    pending_dish.pop(callback.from_user.id, None)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Ок, что дальше?", reply_markup=MAIN_KB)
    await callback.answer()


@router.callback_query(F.data == "more_dish")
async def more_dish(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.set_state(States.waiting_dish)
    await callback.message.answer("Какое блюдо?", reply_markup=CANCEL_KB)
    await callback.answer()


@router.callback_query(F.data == "more_manual")
async def more_manual(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.set_state(States.waiting_manual)
    await callback.message.answer(
        "Пиши продукты через запятую, одним сообщением.", reply_markup=CANCEL_KB
    )
    await callback.answer()


# ---------- Handlers: по блюду ----------

@router.message(F.text == "🍽 По блюду")
async def ask_dish(message: Message, state: FSMContext):
    await state.set_state(States.waiting_dish)
    await message.answer("Какое блюдо?", reply_markup=CANCEL_KB)


@router.message(StateFilter(States.waiting_dish))
async def got_dish(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer("Напиши название блюда текстом.")
        return
    pending_dish[message.from_user.id] = message.text.strip()
    await state.set_state(States.waiting_portions)
    await message.answer("На сколько порций? (число)", reply_markup=CANCEL_KB)


@router.message(StateFilter(States.waiting_portions))
async def got_portions(message: Message, state: FSMContext):
    try:
        portions = int(message.text.strip())
        if portions <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Пришли просто число больше нуля, например 4")
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
    added = add_items(message.from_user.id, ingredients)
    await state.clear()
    if not added:
        await message.answer("Claude не вернул ни одного продукта, попробуй переформулировать блюдо.", reply_markup=MAIN_KB)
        return
    await message.answer(
        f"Добавил в список для «{dish}»:\n\n" + "\n".join(f"• {i}" for i in added),
        reply_markup=ADD_MORE_DISH_KB,
    )


# ---------- Handlers: вручную ----------

@router.message(F.text == "✍️ Вручную")
async def ask_manual(message: Message, state: FSMContext):
    await state.set_state(States.waiting_manual)
    await message.answer(
        "Пиши продукты через запятую, одним сообщением.",
        reply_markup=CANCEL_KB,
    )


@router.message(StateFilter(States.waiting_manual), F.voice)
async def manual_voice(message: Message, state: FSMContext):
    await message.answer("Голосовые пока отключены. Напиши продукты текстом через запятую.")


@router.message(StateFilter(States.waiting_manual))
async def manual_text(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Пришли текстом, через запятую.")
        return
    names = [n.strip() for n in message.text.split(",")]
    added = add_items(message.from_user.id, names)
    await state.clear()
    if not added:
        await message.answer("Пусто, ничего не добавил.", reply_markup=MAIN_KB)
        return
    await message.answer(
        f"Добавил: {', '.join(added)}",
        reply_markup=ADD_MORE_MANUAL_KB,
    )


# ---------- Handlers: список ----------

@router.message(F.text == "📋 Список")
async def show_list(message: Message):
    rows = get_items(message.from_user.id)
    if not rows:
        await message.answer("Список пуст.", reply_markup=list_keyboard(rows))
        return
    await message.answer(format_list(rows), reply_markup=list_keyboard(rows))


@router.callback_query(F.data.startswith("toggle:"))
async def toggle_callback(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    toggle_item(callback.from_user.id, item_id)
    rows = get_items(callback.from_user.id)
    text = format_list(rows) if rows else "Список пуст."
    await callback.message.edit_text(text, reply_markup=list_keyboard(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("delitem:"))
async def delete_callback(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    delete_item(callback.from_user.id, item_id)
    rows = get_items(callback.from_user.id)
    text = format_list(rows) if rows else "Список пуст."
    await callback.message.edit_text(text, reply_markup=list_keyboard(rows))
    await callback.answer("Удалено")


@router.message(F.text == "🗑 Очистить")
async def clear_list(message: Message):
    clear_items(message.from_user.id)
    await message.answer("Список очищен.", reply_markup=MAIN_KB)


# ---------- Handlers: напоминание ----------

@router.message(F.text == "⏰ Напоминание")
async def ask_reminder(message: Message, state: FSMContext):
    await state.set_state(States.waiting_reminder_time)
    await message.answer(
        "Во сколько присылать список каждый день? Формат ЧЧ:ММ (например 18:00). "
        "Пришли \"выкл\", чтобы отключить.",
        reply_markup=CANCEL_KB,
    )


@router.message(StateFilter(States.waiting_reminder_time))
async def got_reminder_time(message: Message, state: FSMContext):
    text = (message.text or "").strip().lower()
    await state.clear()
    if text in ("выкл", "off", "отключить"):
        remove_reminder(message.from_user.id)
        unschedule_reminder(message.from_user.id)
        await message.answer("Напоминание отключено.", reply_markup=MAIN_KB)
        return
    try:
        hour, minute = map(int, text.split(":"))
    except ValueError:
        await message.answer("Не понял время. Формат ЧЧ:ММ, например 18:00", reply_markup=MAIN_KB)
        return
    if not (0 <= hour < 24 and 0 <= minute < 60):
        await message.answer("Часы 0-23, минуты 0-59. Формат ЧЧ:ММ, например 18:00", reply_markup=MAIN_KB)
        return
    set_reminder(message.from_user.id, hour, minute)
    schedule_reminder(message.from_user.id, hour, minute)
    await message.answer(
        f"Готово, буду напоминать в {hour:02d}:{minute:02d} (время сервера, UTC).",
        reply_markup=MAIN_KB,
    )


# ---------- Фоллбэк: сообщение вне известных состояний ----------

@router.message()
async def fallback(message: Message, state: FSMContext):
    await message.answer("Не понял. Выбери действие в меню.", reply_markup=MAIN_KB)


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
