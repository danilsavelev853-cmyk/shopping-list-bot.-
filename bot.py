import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

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
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Башкортостан/Уфа — UTC+5 круглый год, без перехода на летнее время.
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "5"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler(timezone=LOCAL_TZ)

DB_PATH = "assistant.db"


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
        """CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            run_at TEXT NOT NULL,
            text TEXT NOT NULL
        )"""
    )
    conn.commit()
    conn.close()


# --- список покупок ---

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


def delete_item(user_id: int, item_id: int) -> str | None:
    conn = db()
    row = conn.execute(
        "SELECT name FROM items WHERE id=? AND user_id=?", (item_id, user_id)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM items WHERE id=? AND user_id=?", (item_id, user_id))
        conn.commit()
    conn.close()
    return row["name"] if row else None


def delete_items_by_name(user_id: int, query: str) -> list[str]:
    """Удаляет товары, чьё название содержит query (без учёта регистра)."""
    conn = db()
    rows = conn.execute(
        "SELECT id, name FROM items WHERE user_id=?", (user_id,)
    ).fetchall()
    q = query.lower()
    removed = []
    for r in rows:
        if q in r["name"].lower() or r["name"].lower() in q:
            conn.execute("DELETE FROM items WHERE id=?", (r["id"],))
            removed.append(r["name"])
    conn.commit()
    conn.close()
    return removed


# --- заметки ---

def add_note(user_id: int, text: str) -> int:
    conn = db()
    cur = conn.execute(
        "INSERT INTO notes (user_id, text, created_at) VALUES (?, ?, ?)",
        (user_id, text.strip(), datetime.now(LOCAL_TZ).isoformat()),
    )
    conn.commit()
    note_id = cur.lastrowid
    conn.close()
    return note_id


def get_notes(user_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT id, text, created_at FROM notes WHERE user_id=? ORDER BY id DESC", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def delete_note(user_id: int, note_id: int):
    conn = db()
    conn.execute("DELETE FROM notes WHERE id=? AND user_id=?", (note_id, user_id))
    conn.commit()
    conn.close()


# --- напоминания ---

def add_reminder(user_id: int, run_at: datetime, text: str) -> int:
    conn = db()
    cur = conn.execute(
        "INSERT INTO reminders (user_id, run_at, text) VALUES (?, ?, ?)",
        (user_id, run_at.isoformat(), text.strip()),
    )
    conn.commit()
    reminder_id = cur.lastrowid
    conn.close()
    return reminder_id


def get_reminders(user_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT id, run_at, text FROM reminders WHERE user_id=? ORDER BY run_at", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def get_all_future_reminders():
    conn = db()
    rows = conn.execute("SELECT id, user_id, run_at, text FROM reminders").fetchall()
    conn.close()
    return rows


def delete_reminder(reminder_id: int):
    conn = db()
    conn.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
    conn.commit()
    conn.close()


# ---------- Keyboards ----------

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✍️ Добавить товар"), KeyboardButton(text="📋 Список покупок")],
        [KeyboardButton(text="📝 Заметки"), KeyboardButton(text="⏰ Напоминания")],
        [KeyboardButton(text="🗑 Очистить список")],
    ],
    resize_keyboard=True,
)

CANCEL_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="🚫 Отмена", callback_data="cancel")]]
)

ADD_MORE_ITEM_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="more_manual")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="done_adding")],
    ]
)


def list_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []
    for r in rows:
        mark = "✅" if r["checked"] else "▫️"
        buttons.append(
            [
                InlineKeyboardButton(text=f"{mark} {r['name']}", callback_data=f"toggle:{r['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"delitem:{r['id']}"),
            ]
        )
    buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data="more_manual")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def notes_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []
    for r in rows:
        preview = r["text"] if len(r["text"]) <= 30 else r["text"][:27] + "..."
        buttons.append(
            [
                InlineKeyboardButton(text=preview, callback_data="noop"),
                InlineKeyboardButton(text="🗑", callback_data=f"delnote:{r['id']}"),
            ]
        )
    buttons.append([InlineKeyboardButton(text="➕ Добавить заметку", callback_data="more_note")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def reminders_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []
    for r in rows:
        dt = datetime.fromisoformat(r["run_at"])
        label = f"{dt.strftime('%d.%m %H:%M')} — {r['text'][:25]}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"delreminder:{r['id']}")])
    buttons.append([InlineKeyboardButton(text="➕ Новое напоминание", callback_data="more_reminder")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------- States ----------

class States(StatesGroup):
    waiting_manual = State()
    waiting_note = State()
    waiting_reminder_datetime = State()
    waiting_reminder_text = State()


pending_reminder_dt: dict[int, datetime] = {}


# ---------- Helpers ----------

def format_list(rows) -> str:
    if not rows:
        return "Список пуст."
    return "\n".join(f"{'✅' if r['checked'] else '▫️'} {r['name']}" for r in rows)


def format_notes(rows) -> str:
    if not rows:
        return "Заметок нет."
    return "\n\n".join(f"📝 {r['text']}" for r in rows)


def format_reminders(rows) -> str:
    if not rows:
        return "Напоминаний нет."
    lines = []
    for r in rows:
        dt = datetime.fromisoformat(r["run_at"])
        lines.append(f"🕒 {dt.strftime('%d.%m.%Y %H:%M')} — {r['text']}")
    return "\n".join(lines)


def parse_datetime(date_str: str, time_str: str) -> datetime | None:
    date_str = date_str.strip().replace("/", ".")
    time_str = time_str.strip().replace(".", ":")
    for date_fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            date_part = datetime.strptime(date_str, date_fmt)
            break
        except ValueError:
            date_part = None
    if date_part is None:
        return None
    try:
        hour, minute = map(int, time_str.split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None
    except ValueError:
        return None
    return date_part.replace(hour=hour, minute=minute, tzinfo=LOCAL_TZ)


def split_items(raw: str) -> list[str]:
    raw = raw.replace(" и ", ",")
    return [p.strip() for p in raw.split(",") if p.strip()]


# ---------- Handlers: старт / отмена ----------

@router.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я Марк — помогаю со списком покупок, заметками и напоминаниями.\n\n"
        "✍️ Добавить товар — впиши продукты через запятую\n"
        "📋 Список покупок — список с кнопками (отметить/удалить)\n"
        "📝 Заметки — короткие записи, которые не хочешь забыть\n"
        "⏰ Напоминания — напомню в нужный день и час\n\n"
        "Также понимаю команды в свободной форме, если написать с обращения «Марк»:\n"
        "• Марк добавь молоко в список покупок\n"
        "• Марк удали молоко\n"
        "• Марк напомни мне в 18:00 дата 09.07.2026 позвонить клиенту\n"
        "• Марк запиши купить подарок на др\n\n"
        "В любой момент — /cancel, чтобы выйти из текущего действия.",
        reply_markup=MAIN_KB,
    )


@router.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    pending_reminder_dt.pop(message.from_user.id, None)
    await message.answer("Отменено.", reply_markup=MAIN_KB)


@router.callback_query(F.data == "cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    pending_reminder_dt.pop(callback.from_user.id, None)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Отменено.", reply_markup=MAIN_KB)
    await callback.answer()


@router.callback_query(F.data == "done_adding")
async def done_adding(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Ок, что дальше?", reply_markup=MAIN_KB)
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer()


# ---------- Список покупок ----------

@router.message(F.text == "✍️ Добавить товар")
async def ask_manual(message: Message, state: FSMContext):
    await state.set_state(States.waiting_manual)
    await message.answer("Пиши товары через запятую, одним сообщением.", reply_markup=CANCEL_KB)


@router.callback_query(F.data == "more_manual")
async def more_manual(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.set_state(States.waiting_manual)
    await callback.message.answer("Пиши товары через запятую, одним сообщением.", reply_markup=CANCEL_KB)
    await callback.answer()


@router.message(StateFilter(States.waiting_manual))
async def manual_text(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Пришли текстом, через запятую.")
        return
    added = add_items(message.from_user.id, split_items(message.text))
    await state.clear()
    if not added:
        await message.answer("Пусто, ничего не добавил.", reply_markup=MAIN_KB)
        return
    await message.answer(f"Добавил: {', '.join(added)}", reply_markup=ADD_MORE_ITEM_KB)


@router.message(F.text == "📋 Список покупок")
async def show_list(message: Message):
    rows = get_items(message.from_user.id)
    await message.answer(format_list(rows), reply_markup=list_keyboard(rows))


@router.callback_query(F.data.startswith("toggle:"))
async def toggle_callback(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    toggle_item(callback.from_user.id, item_id)
    rows = get_items(callback.from_user.id)
    await callback.message.edit_text(format_list(rows), reply_markup=list_keyboard(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("delitem:"))
async def delete_item_callback(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    delete_item(callback.from_user.id, item_id)
    rows = get_items(callback.from_user.id)
    await callback.message.edit_text(format_list(rows), reply_markup=list_keyboard(rows))
    await callback.answer("Удалено")


@router.message(F.text == "🗑 Очистить список")
async def clear_list(message: Message):
    clear_items(message.from_user.id)
    await message.answer("Список очищен.", reply_markup=MAIN_KB)


# ---------- Заметки ----------

@router.message(F.text == "📝 Заметки")
async def show_notes(message: Message):
    rows = get_notes(message.from_user.id)
    await message.answer(format_notes(rows), reply_markup=notes_keyboard(rows))


@router.callback_query(F.data == "more_note")
async def more_note(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.set_state(States.waiting_note)
    await callback.message.answer("Что записать?", reply_markup=CANCEL_KB)
    await callback.answer()


@router.message(StateFilter(States.waiting_note))
async def got_note(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer("Пришли текст заметки.")
        return
    add_note(message.from_user.id, message.text)
    await state.clear()
    await message.answer("Записал.", reply_markup=MAIN_KB)


@router.callback_query(F.data.startswith("delnote:"))
async def delete_note_callback(callback: CallbackQuery):
    note_id = int(callback.data.split(":")[1])
    delete_note(callback.from_user.id, note_id)
    rows = get_notes(callback.from_user.id)
    await callback.message.edit_text(format_notes(rows), reply_markup=notes_keyboard(rows))
    await callback.answer("Удалено")


# ---------- Напоминания ----------

@router.message(F.text == "⏰ Напоминания")
async def show_reminders(message: Message):
    rows = get_reminders(message.from_user.id)
    await message.answer(format_reminders(rows), reply_markup=reminders_keyboard(rows))


@router.callback_query(F.data == "more_reminder")
async def more_reminder(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.set_state(States.waiting_reminder_datetime)
    await callback.message.answer(
        "Когда напомнить? Формат: ДД.ММ.ГГГГ ЧЧ:ММ\nНапример: 09.07.2026 18:00",
        reply_markup=CANCEL_KB,
    )
    await callback.answer()


@router.message(StateFilter(States.waiting_reminder_datetime))
async def got_reminder_datetime(message: Message, state: FSMContext):
    parts = (message.text or "").strip().split()
    if len(parts) != 2:
        await message.answer("Формат: ДД.ММ.ГГГГ ЧЧ:ММ, например 09.07.2026 18:00")
        return
    dt = parse_datetime(parts[0], parts[1])
    if not dt:
        await message.answer("Не понял дату/время. Формат: ДД.ММ.ГГГГ ЧЧ:ММ")
        return
    if dt <= datetime.now(LOCAL_TZ):
        await message.answer("Это время уже прошло, укажи будущую дату.")
        return
    pending_reminder_dt[message.from_user.id] = dt
    await state.set_state(States.waiting_reminder_text)
    await message.answer("Что напомнить?", reply_markup=CANCEL_KB)


@router.message(StateFilter(States.waiting_reminder_text))
async def got_reminder_text(message: Message, state: FSMContext):
    dt = pending_reminder_dt.pop(message.from_user.id, None)
    await state.clear()
    if not dt:
        await message.answer("Что-то пошло не так, начни заново.", reply_markup=MAIN_KB)
        return
    text = (message.text or "напоминание").strip()
    reminder_id = add_reminder(message.from_user.id, dt, text)
    schedule_reminder(reminder_id, message.from_user.id, dt, text)
    await message.answer(
        f"Ок, напомню {dt.strftime('%d.%m.%Y в %H:%M')}: {text}", reply_markup=MAIN_KB
    )


@router.callback_query(F.data.startswith("delreminder:"))
async def delete_reminder_callback(callback: CallbackQuery):
    reminder_id = int(callback.data.split(":")[1])
    delete_reminder(reminder_id)
    unschedule_reminder(reminder_id)
    rows = get_reminders(callback.from_user.id)
    await callback.message.edit_text(format_reminders(rows), reply_markup=reminders_keyboard(rows))
    await callback.answer("Удалено")


# ---------- Wake word "Марк" — команды без внешних API ----------

WAKE_RE = re.compile(r"(?i)^марк[,:]?\s*(.*)$")
REMIND_RE = re.compile(
    r"(?i)напомни\s+мне\s+в\s+(?P<time>\d{1,2}[:.]\d{2})\s+дата\s+"
    r"(?P<date>\d{1,2}[./]\d{1,2}[./]\d{2,4})(?:\s+(?P<text>.+))?"
)
ADD_RE = re.compile(r"(?i)^добавь\s+(?P<items>.+?)(?:\s+в\s+список\s+покупок)?\s*[.!]?$")
DEL_RE = re.compile(r"(?i)^удали\s+(?P<items>.+?)(?:\s+из\s+списка(?:\s+покупок)?)?\s*[.!]?$")
NOTE_RE = re.compile(r"(?i)^(?:запиши|заметка[:]?)\s+(?P<text>.+)$")
SHOW_LIST_RE = re.compile(r"(?i)^покажи\s+список$")
SHOW_NOTES_RE = re.compile(r"(?i)^покажи\s+заметки$")
SHOW_REMINDERS_RE = re.compile(r"(?i)^покажи\s+напоминания$")
CLEAR_RE = re.compile(r"(?i)^очисти\s+список$")


@router.message(F.text.regexp(WAKE_RE))
async def wake_word_handler(message: Message, state: FSMContext):
    match = WAKE_RE.match(message.text)
    rest = (match.group(1) or "").strip()
    if not rest:
        await message.answer("Слушаю. Что сделать?")
        return

    await state.clear()
    pending_reminder_dt.pop(message.from_user.id, None)
    user_id = message.from_user.id

    m = REMIND_RE.search(rest)
    if m:
        dt = parse_datetime(m.group("date"), m.group("time"))
        if not dt:
            await message.answer("Не понял дату/время. Формат: ЧЧ:ММ дата ДД.ММ.ГГГГ", reply_markup=MAIN_KB)
            return
        if dt <= datetime.now(LOCAL_TZ):
            await message.answer("Это время уже прошло, укажи будущую дату.", reply_markup=MAIN_KB)
            return
        text = (m.group("text") or "напоминание").strip()
        reminder_id = add_reminder(user_id, dt, text)
        schedule_reminder(reminder_id, user_id, dt, text)
        await message.answer(f"Ок, напомню {dt.strftime('%d.%m.%Y в %H:%M')}: {text}", reply_markup=MAIN_KB)
        return

    m = ADD_RE.match(rest)
    if m:
        added = add_items(user_id, split_items(m.group("items")))
        if added:
            await message.answer(f"Добавил: {', '.join(added)}", reply_markup=MAIN_KB)
        else:
            await message.answer("Не понял, что добавить.", reply_markup=MAIN_KB)
        return

    m = DEL_RE.match(rest)
    if m:
        removed = delete_items_by_name(user_id, m.group("items").strip())
        if removed:
            await message.answer(f"Удалил: {', '.join(removed)}", reply_markup=MAIN_KB)
        else:
            await message.answer("Не нашёл такого в списке.", reply_markup=MAIN_KB)
        return

    m = NOTE_RE.match(rest)
    if m:
        add_note(user_id, m.group("text"))
        await message.answer("Записал.", reply_markup=MAIN_KB)
        return

    if SHOW_LIST_RE.match(rest):
        rows = get_items(user_id)
        await message.answer(format_list(rows), reply_markup=list_keyboard(rows))
        return

    if SHOW_NOTES_RE.match(rest):
        rows = get_notes(user_id)
        await message.answer(format_notes(rows), reply_markup=notes_keyboard(rows))
        return

    if SHOW_REMINDERS_RE.match(rest):
        rows = get_reminders(user_id)
        await message.answer(format_reminders(rows), reply_markup=reminders_keyboard(rows))
        return

    if CLEAR_RE.match(rest):
        clear_items(user_id)
        await message.answer("Список очищен.", reply_markup=MAIN_KB)
        return

    await message.answer(
        "Не понял команду. Примеры:\n"
        "«Марк добавь молоко в список покупок»\n"
        "«Марк удали молоко»\n"
        "«Марк напомни мне в 18:00 дата 09.07.2026 позвонить клиенту»\n"
        "«Марк запиши купить подарок»",
        reply_markup=MAIN_KB,
    )


# ---------- Фоллбэк ----------

@router.message()
async def fallback(message: Message):
    await message.answer("Не понял. Выбери действие в меню.", reply_markup=MAIN_KB)


# ---------- Scheduler ----------

def schedule_reminder(reminder_id: int, user_id: int, run_at: datetime, text: str):
    job_id = f"reminder_{reminder_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        send_reminder,
        "date",
        run_date=run_at,
        id=job_id,
        args=[reminder_id, user_id, text],
        misfire_grace_time=3600,
    )


def unschedule_reminder(reminder_id: int):
    job_id = f"reminder_{reminder_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


async def send_reminder(reminder_id: int, user_id: int, text: str):
    await bot.send_message(user_id, f"⏰ Напоминание: {text}")
    delete_reminder(reminder_id)


def load_reminders():
    now = datetime.now(LOCAL_TZ)
    for r in get_all_future_reminders():
        run_at = datetime.fromisoformat(r["run_at"])
        if run_at <= now:
            delete_reminder(r["id"])
            continue
        schedule_reminder(r["id"], r["user_id"], run_at, r["text"])


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
