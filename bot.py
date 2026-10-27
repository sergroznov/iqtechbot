"""Бот-флаер для открытия точки сервисного центра.

Клиент: проверка подписки на канал -> телефон/ник/устройство -> выбор услуги -> QR-флаер.
Сотрудник: сканирует QR камерой телефона -> открывается бот с карточкой -> "Погасить".
Один клиент = один флаер (по Telegram ID и по номеру телефона), повторно получить нельзя.
"""
import asyncio
import csv
import html
import io
import logging
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiosqlite
import qrcode
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ---------------------------------------------------------------- настройки
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL = os.getenv("CHANNEL", "")  # @username канала или -100... id
CHANNEL_URL = os.getenv("CHANNEL_URL", "") or (
    f"https://t.me/{CHANNEL[1:]}" if CHANNEL.startswith("@") else ""
)
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
DB_PATH = os.getenv("DB_PATH", "data/bot.db")
FLYER_DAYS = int(os.getenv("FLYER_DAYS", "30"))
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
SC_NAME = os.getenv("SC_NAME", "наш сервисный центр")

SERVICES = {
    "film": "Поклейка плёнки на экран",
    "clean": "Чистка и чекап устройства",
}

log = logging.getLogger("flyerbot")
router = Router()
db: aiosqlite.Connection | None = None
_bot_username: str | None = None


# ------------------------------------------------------------------- утилиты
def esc(s) -> str:
    return html.escape(str(s or ""))


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def fmt(ts: str) -> str:
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def norm_phone(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    if len(d) == 10:
        d = "7" + d
    return d


def make_qr(link: str) -> bytes:
    img = qrcode.make(link, box_size=12, border=3)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


async def bot_username(bot: Bot) -> str:
    global _bot_username
    if not _bot_username:
        _bot_username = (await bot.me()).username
    return _bot_username


# ---------------------------------------------------------------------- БД
SCHEMA = """
CREATE TABLE IF NOT EXISTS flyers(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    tg_id INTEGER UNIQUE NOT NULL,
    username TEXT,
    full_name TEXT,
    phone TEXT UNIQUE NOT NULL,
    device TEXT NOT NULL,
    service TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    redeemed_at TEXT,
    redeemed_by INTEGER
);
CREATE TABLE IF NOT EXISTS staff(
    tg_id INTEGER PRIMARY KEY,
    name TEXT,
    added_at TEXT NOT NULL
);
"""


async def init_db():
    global db
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(SCHEMA)
    await db.commit()


async def fetchone(q, p=()):
    async with db.execute(q, p) as c:
        return await c.fetchone()


async def fetchall(q, p=()):
    async with db.execute(q, p) as c:
        return await c.fetchall()


async def execute(q, p=()) -> int:
    c = await db.execute(q, p)
    await db.commit()
    n = c.rowcount
    await c.close()
    return n


async def get_flyer(code: str):
    return await fetchone("SELECT * FROM flyers WHERE code=?", (code,))


async def get_flyer_by_tg(tg_id: int):
    return await fetchone("SELECT * FROM flyers WHERE tg_id=?", (tg_id,))


async def phone_taken(phone: str) -> bool:
    return await fetchone("SELECT 1 FROM flyers WHERE phone=?", (phone,)) is not None


def eff_status(f) -> str:
    if f["status"] == "active" and f["expires_at"] < now_utc():
        return "expired"
    return f["status"]


async def create_flyer(tg_id, username, full_name, phone, device, service):
    code = secrets.token_hex(5).upper()
    created = datetime.now(timezone.utc)
    expires = created + timedelta(days=FLYER_DAYS)
    try:
        await execute(
            "INSERT INTO flyers(code,tg_id,username,full_name,phone,device,service,"
            "status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,'active',?,?)",
            (code, tg_id, username, full_name, phone, device, service,
             created.strftime("%Y-%m-%dT%H:%M:%S"),
             expires.strftime("%Y-%m-%dT%H:%M:%S")),
        )
    except sqlite3.IntegrityError:
        return None
    return code


async def redeem(code: str, staff_id: int):
    """Атомарно гасит флаер. Возвращает (результат, флаер)."""
    f = await get_flyer(code)
    if not f:
        return "notfound", None
    if f["status"] == "redeemed":
        return "used", f
    if f["expires_at"] < now_utc():
        return "expired", f
    n = await execute(
        "UPDATE flyers SET status='redeemed', redeemed_at=?, redeemed_by=? "
        "WHERE code=? AND status='active'",
        (now_utc(), staff_id, code),
    )
    if n == 0:
        return "used", f
    return "ok", await get_flyer(code)


async def is_staff(uid: int) -> bool:
    if uid in ADMIN_IDS:
        return True
    return await fetchone("SELECT 1 FROM staff WHERE tg_id=?", (uid,)) is not None


async def is_subscribed(bot: Bot, uid: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL, uid)
    except Exception as e:  # бот не админ канала / неверный CHANNEL
        log.warning("get_chat_member failed: %s", e)
        return False
    if m.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(m, "is_member", False))
    return m.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    )


# --------------------------------------------------------------- клиент: FSM
class Form(StatesGroup):
    phone = State()
    device = State()
    service = State()


def phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def service_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"svc:{key}")]
            for key, name in SERVICES.items()
        ]
    )


def sub_kb():
    rows = []
    if CHANNEL_URL:
        rows.append([InlineKeyboardButton(text="📢 Перейти в канал", url=CHANNEL_URL)])
    rows.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="chk")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_flyer(bot: Bot, uid: int, f):
    st = eff_status(f)
    if st == "redeemed":
        await bot.send_message(
            uid,
            f"Вы уже воспользовались флаером ({fmt(f['redeemed_at'])}). "
            "Спасибо, что выбрали нас! Акция действует один раз на клиента.",
        )
        return
    if st == "expired":
        await bot.send_message(uid, "Срок действия вашего флаера истёк 😔")
        return
    link = f"https://t.me/{await bot_username(bot)}?start=f_{f['code']}"
    caption = (
        f"🎁 <b>Подарочный флаер — {esc(SERVICES.get(f['service'], f['service']))}</b>\n\n"
        f"Устройство: {esc(f['device'])}\n"
        f"Код: <code>{f['code']}</code>\n"
        f"Действует до: {fmt(f['expires_at'])}\n\n"
        "Покажите этот QR-код сотруднику сервисного центра. "
        "Флаер одноразовый и привязан к вам."
    )
    await bot.send_photo(
        uid, BufferedInputFile(make_qr(link), filename="flyer.png"),
        caption=caption,
    )


async def client_entry(bot: Bot, uid: int, state: FSMContext):
    f = await get_flyer_by_tg(uid)
    if f:
        await send_flyer(bot, uid, f)
        return
    if not await is_subscribed(bot, uid):
        await bot.send_message(
            uid,
            f"👋 Добро пожаловать в {esc(SC_NAME)}!\n\n"
            "Для получения подарочного флаера подпишитесь на наш канал, "
            "затем нажмите «Я подписался».",
            reply_markup=sub_kb(),
        )
        return
    await state.set_state(Form.phone)
    await bot.send_message(
        uid,
        "Отлично, подписка есть! 🎉\n\n"
        "Нажмите кнопку ниже, чтобы отправить номер телефона. "
        "Отправляя номер, вы соглашаетесь на обработку персональных данных "
        "для оформления флаера.",
        reply_markup=phone_kb(),
    )


@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    arg = command.args or ""
    if await is_staff(uid):
        if arg.startswith("f_"):
            await show_redeem_card(m.bot, uid, arg[2:].upper())
        else:
            await m.answer(STAFF_HELP)
        return
    await client_entry(m.bot, uid, state)


@router.callback_query(F.data == "chk")
async def cb_check_sub(c: CallbackQuery, state: FSMContext):
    if await get_flyer_by_tg(c.from_user.id) is None and not await is_subscribed(
        c.bot, c.from_user.id
    ):
        await c.answer("Подписка не найдена. Подпишитесь и нажмите ещё раз.", show_alert=True)
        return
    await c.answer()
    await client_entry(c.bot, c.from_user.id, state)


@router.message(Form.phone, F.contact)
async def got_phone(m: Message, state: FSMContext):
    if m.contact.user_id != m.from_user.id:
        await m.answer("Пожалуйста, отправьте именно свой номер кнопкой ниже.",
                       reply_markup=phone_kb())
        return
    phone = norm_phone(m.contact.phone_number)
    if await phone_taken(phone):
        await state.clear()
        await m.answer(
            "На этот номер флаер уже оформлен, повторно получить его нельзя.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await state.update_data(phone=phone)
    await state.set_state(Form.device)
    await m.answer(
        "Напишите марку и модель вашего гаджета, например: <i>iPhone 14 Pro</i> "
        "или <i>Samsung Galaxy S23</i>.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Form.phone)
async def phone_wrong(m: Message):
    await m.answer("Нажмите кнопку «📱 Отправить мой номер» под полем ввода.",
                   reply_markup=phone_kb())


@router.message(Form.device, F.text)
async def got_device(m: Message, state: FSMContext):
    device = (m.text or "").strip()
    if not 2 <= len(device) <= 80:
        await m.answer("Введите марку и модель текстом (от 2 до 80 символов).")
        return
    await state.update_data(device=device)
    await state.set_state(Form.service)
    await m.answer("Выберите услугу для подарочного флаера:", reply_markup=service_kb())


@router.callback_query(Form.service, F.data.startswith("svc:"))
async def got_service(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 1)[1]
    if key not in SERVICES:
        await c.answer()
        return
    data = await state.get_data()
    uid = c.from_user.id
    if await get_flyer_by_tg(uid):
        await state.clear()
        await c.answer()
        await send_flyer(c.bot, uid, await get_flyer_by_tg(uid))
        return
    if not await is_subscribed(c.bot, uid):
        await state.clear()
        await c.answer()
        await client_entry(c.bot, uid, state)
        return
    username = f"@{c.from_user.username}" if c.from_user.username else ""
    code = await create_flyer(
        uid, username, c.from_user.full_name, data["phone"], data["device"], key
    )
    await state.clear()
    await c.answer()
    if not code:
        await c.message.answer("Флаер для вас или для этого номера уже оформлен.")
        return
    await c.message.edit_reply_markup(reply_markup=None)
    await send_flyer(c.bot, uid, await get_flyer(code))


@router.message(Form.service)
async def service_wrong(m: Message):
    await m.answer("Выберите услугу кнопкой:", reply_markup=service_kb())


# -------------------------------------------------------------- сотрудники
STAFF_HELP = (
    "🛠 <b>Панель сотрудника</b>\n\n"
    "Отсканируйте QR клиента камерой телефона — откроется этот бот с карточкой "
    "флаера и кнопкой «Погасить».\n\n"
    "/check КОД — найти флаер вручную по коду\n"
    "/stats — статистика\n"
    "/export — выгрузка всех флаеров в CSV (админ)\n"
    "/addstaff ID — добавить сотрудника (админ)\n"
    "/delstaff ID — убрать сотрудника (админ)\n"
    "/staff — список сотрудников (админ)"
)

STATUS_TXT = {
    "active": "🟢 Действителен",
    "redeemed": "🔴 УЖЕ ИСПОЛЬЗОВАН",
    "expired": "⚪️ Срок истёк",
}


async def show_redeem_card(bot: Bot, chat_id: int, code: str):
    f = await get_flyer(code)
    if not f:
        await bot.send_message(chat_id, "❌ Флаер с таким кодом не найден.")
        return
    st = eff_status(f)
    subscribed = await is_subscribed(bot, f["tg_id"])
    text = (
        f"🎟 <b>Флаер {f['code']}</b>\n"
        f"Статус: <b>{STATUS_TXT[st]}</b>\n\n"
        f"Услуга: <b>{esc(SERVICES.get(f['service'], f['service']))}</b>\n"
        f"Клиент: {esc(f['full_name'])} {esc(f['username'])}\n"
        f"Телефон: +{esc(f['phone'])}\n"
        f"Устройство: {esc(f['device'])}\n"
        f"Выдан: {fmt(f['created_at'])}\n"
        f"Действует до: {fmt(f['expires_at'])}\n"
        f"Подписан на канал сейчас: {'да' if subscribed else 'нет'}"
    )
    if st == "redeemed":
        text += f"\nПогашен: {fmt(f['redeemed_at'])}"
    kb = None
    if st == "active":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(
                text="✅ Погасить флаер", callback_data=f"rd:{f['code']}")]]
        )
    await bot.send_message(chat_id, text, reply_markup=kb)


@router.callback_query(F.data.startswith("rd:"))
async def cb_redeem(c: CallbackQuery):
    if not await is_staff(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    code = c.data.split(":", 1)[1]
    res, f = await redeem(code, c.from_user.id)
    if res == "ok":
        await c.answer("Флаер погашен ✅", show_alert=True)
        await c.message.edit_text(
            c.message.html_text + f"\n\n✅ <b>Погашено</b> {fmt(f['redeemed_at'])}",
            reply_markup=None,
        )
        try:
            await c.bot.send_message(
                f["tg_id"],
                "✅ Флаер использован. Спасибо, что выбрали нас! "
                "Ждём вас снова 🙌",
            )
        except Exception:
            pass
    elif res == "used":
        await c.answer("Этот флаер уже был использован!", show_alert=True)
        await c.message.edit_reply_markup(reply_markup=None)
    elif res == "expired":
        await c.answer("Срок действия флаера истёк", show_alert=True)
        await c.message.edit_reply_markup(reply_markup=None)
    else:
        await c.answer("Флаер не найден", show_alert=True)


@router.message(Command("check"))
async def cmd_check(m: Message, command: CommandObject):
    if not await is_staff(m.from_user.id):
        return
    code = (command.args or "").strip().upper()
    if not code:
        await m.answer("Использование: /check КОД")
        return
    await show_redeem_card(m.bot, m.from_user.id, code)


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    if not await is_staff(m.from_user.id):
        return
    now = now_utc()
    total = (await fetchone("SELECT COUNT(*) n FROM flyers"))["n"]
    used = (await fetchone("SELECT COUNT(*) n FROM flyers WHERE status='redeemed'"))["n"]
    expired = (await fetchone(
        "SELECT COUNT(*) n FROM flyers WHERE status='active' AND expires_at<?", (now,)))["n"]
    active = total - used - expired
    lines = [
        "📊 <b>Статистика</b>",
        f"Выдано флаеров: {total}",
        f"Погашено: {used}",
        f"Активных: {active}",
        f"Просрочено: {expired}",
        "",
        "<b>По услугам (выдано / погашено):</b>",
    ]
    for key, name in SERVICES.items():
        t = (await fetchone("SELECT COUNT(*) n FROM flyers WHERE service=?", (key,)))["n"]
        u = (await fetchone(
            "SELECT COUNT(*) n FROM flyers WHERE service=? AND status='redeemed'", (key,)))["n"]
        lines.append(f"{esc(name)}: {t} / {u}")
    await m.answer("\n".join(lines))


@router.message(Command("export"))
async def cmd_export(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    rows = await fetchall("SELECT * FROM flyers ORDER BY id")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["код", "tg_id", "ник", "имя", "телефон", "устройство", "услуга",
                "статус", "выдан", "действует до", "погашен", "кем погашен"])
    for r in rows:
        w.writerow([
            r["code"], r["tg_id"], r["username"], r["full_name"], "+" + r["phone"],
            r["device"], SERVICES.get(r["service"], r["service"]), eff_status(r),
            fmt(r["created_at"]), fmt(r["expires_at"]),
            fmt(r["redeemed_at"]) if r["redeemed_at"] else "", r["redeemed_by"] or "",
        ])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM для Excel
    await m.answer_document(BufferedInputFile(data, filename="flyers.csv"))


@router.message(Command("addstaff"))
async def cmd_addstaff(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS:
        return
    nums = re.findall(r"\d+", command.args or "")
    if not nums:
        await m.answer("Использование: /addstaff TELEGRAM_ID\n"
                       "ID можно узнать у бота @userinfobot")
        return
    await execute("INSERT OR IGNORE INTO staff(tg_id,name,added_at) VALUES(?,?,?)",
                  (int(nums[0]), "", now_utc()))
    await m.answer(f"Сотрудник {nums[0]} добавлен ✅")


@router.message(Command("delstaff"))
async def cmd_delstaff(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS:
        return
    nums = re.findall(r"\d+", command.args or "")
    if not nums:
        await m.answer("Использование: /delstaff TELEGRAM_ID")
        return
    await execute("DELETE FROM staff WHERE tg_id=?", (int(nums[0]),))
    await m.answer(f"Сотрудник {nums[0]} удалён")


@router.message(Command("staff"))
async def cmd_staff(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    rows = await fetchall("SELECT tg_id FROM staff ORDER BY added_at")
    admins = ", ".join(map(str, sorted(ADMIN_IDS))) or "—"
    staff = ", ".join(str(r["tg_id"]) for r in rows) or "—"
    await m.answer(f"Админы: {admins}\nСотрудники: {staff}")


# ------------------------------------------------------------------- запуск
async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN or not CHANNEL or not ADMIN_IDS:
        raise SystemExit("Заполните BOT_TOKEN, CHANNEL и ADMIN_IDS в .env")
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    me = await bot.me()
    log.info("Бот запущен: @%s", me.username)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
