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
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
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
INVITE_DAYS = int(os.getenv("INVITE_DAYS", "14"))
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

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
CREATE TABLE IF NOT EXISTS clients(
    tg_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    phone TEXT,
    segment TEXT NOT NULL DEFAULT 'client',   -- client | mall (сотрудник ТЦ)
    company TEXT,
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites(
    code TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    claimed_by INTEGER,
    claimed_at TEXT,
    used_at TEXT
);
"""


async def init_db():
    global db
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(SCHEMA)
    # миграция: все, кто уже получил флаер, становятся профилями «обычный клиент»
    await db.execute(
        "INSERT OR IGNORE INTO clients(tg_id,username,full_name,phone,segment,created_at,last_seen) "
        "SELECT tg_id,username,full_name,phone,'client',created_at,created_at FROM flyers")
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


SEGMENT_TXT = {"client": "Обычный клиент", "mall": "Сотрудник ТЦ"}


async def touch_client(user):
    """Запоминает клиента при любом заходе (ник/имя обновляются)."""
    username = f"@{user.username}" if user.username else ""
    now = now_utc()
    await execute(
        "INSERT INTO clients(tg_id,username,full_name,created_at,last_seen) VALUES(?,?,?,?,?) "
        "ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, "
        "full_name=excluded.full_name, last_seen=excluded.last_seen",
        (user.id, username, user.full_name, now, now),
    )


async def set_client_phone(tg_id: int, phone: str):
    await execute("UPDATE clients SET phone=? WHERE tg_id=?", (phone, tg_id))


async def get_client(tg_id: int):
    return await fetchone("SELECT * FROM clients WHERE tg_id=?", (tg_id,))


async def create_invite(company: str, created_by: int) -> str:
    code = secrets.token_hex(6).upper()
    now = datetime.now(timezone.utc)
    await execute(
        "INSERT INTO invites(code,company,created_by,created_at,expires_at) VALUES(?,?,?,?,?)",
        (code, company, created_by, now.strftime("%Y-%m-%dT%H:%M:%S"),
         (now + timedelta(days=INVITE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")),
    )
    return code


async def claim_invite(code: str, tg_id: int) -> str:
    """Закрепляет одноразовую ссылку за первым, кто её открыл. ok | used | expired | bad"""
    inv = await fetchone("SELECT * FROM invites WHERE code=?", (code,))
    if not inv:
        return "bad"
    if inv["claimed_by"] == tg_id:
        return "ok"          # тот же человек вернулся по своей ссылке
    if inv["claimed_by"] is not None:
        return "used"
    if inv["expires_at"] < now_utc():
        return "expired"
    n = await execute(
        "UPDATE invites SET claimed_by=?, claimed_at=? WHERE code=? AND claimed_by IS NULL",
        (tg_id, now_utc(), code),
    )
    return "ok" if n else "used"


async def finalize_invite(tg_id: int):
    """После оформления флаера помечает клиента сотрудником ТЦ (если он пришёл по спец-ссылке)."""
    inv = await fetchone(
        "SELECT * FROM invites WHERE claimed_by=? AND used_at IS NULL "
        "ORDER BY claimed_at DESC LIMIT 1", (tg_id,))
    if not inv:
        return
    await execute("UPDATE clients SET segment='mall', company=? WHERE tg_id=?",
                  (inv["company"], tg_id))
    await execute("UPDATE invites SET used_at=? WHERE code=?", (now_utc(), inv["code"]))


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


def welcome_image():
    for ext in ("jpg", "jpeg", "png"):
        p = os.path.join(ASSETS_DIR, f"welcome.{ext}")
        if os.path.isfile(p):
            return p
    return None


async def send_welcome(bot: Bot, uid: int, text: str, kb, with_image: bool):
    """Сообщение с картинкой-приветствием (если файл есть), иначе просто текст."""
    path = welcome_image() if with_image else None
    if path:
        try:
            await bot.send_photo(uid, FSInputFile(path), caption=text, reply_markup=kb)
            return
        except Exception as e:
            log.warning("welcome image failed: %s", e)
    await bot.send_message(uid, text, reply_markup=kb)


async def client_entry(bot: Bot, uid: int, state: FSMContext, with_image: bool = True):
    f = await get_flyer_by_tg(uid)
    if f:
        await send_flyer(bot, uid, f)
        return
    if not await is_subscribed(bot, uid):
        await send_welcome(
            bot, uid,
            f"👋 Добро пожаловать в {esc(SC_NAME)}!\n\n"
            "Для получения подарочного флаера подпишитесь на наш канал, "
            "затем нажмите «Я подписался».",
            sub_kb(), with_image,
        )
        return
    await state.set_state(Form.phone)
    await send_welcome(
        bot, uid,
        "Отлично, подписка есть! 🎉\n\n"
        "Нажмите кнопку ниже, чтобы отправить номер телефона. "
        "Отправляя номер, вы соглашаетесь на обработку персональных данных "
        "для оформления флаера.",
        phone_kb(), with_image,
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
            await show_panel(m.bot, m.chat.id, uid)
        return
    await touch_client(m.from_user)
    if arg.startswith("i_") and await get_flyer_by_tg(uid) is None:
        res = await claim_invite(arg[2:].upper(), uid)
        if res != "ok":
            await m.answer(
                "⚠️ Эта персональная ссылка уже использована или недействительна. "
                "Вы можете получить флаер как обычный клиент."
            )
    await client_entry(m.bot, uid, state)


@router.callback_query(F.data == "chk")
async def cb_check_sub(c: CallbackQuery, state: FSMContext):
    if await get_flyer_by_tg(c.from_user.id) is None and not await is_subscribed(
        c.bot, c.from_user.id
    ):
        await c.answer("Подписка не найдена. Подпишитесь и нажмите ещё раз.", show_alert=True)
        return
    await c.answer()
    await client_entry(c.bot, c.from_user.id, state, with_image=False)


@router.message(Form.phone, F.contact)
async def got_phone(m: Message, state: FSMContext):
    if m.contact.user_id != m.from_user.id:
        await m.answer("Пожалуйста, отправьте именно свой номер кнопкой ниже.",
                       reply_markup=phone_kb())
        return
    phone = norm_phone(m.contact.phone_number)
    await touch_client(m.from_user)
    await set_client_phone(m.from_user.id, phone)
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
        await client_entry(c.bot, uid, state, with_image=False)
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
    await touch_client(c.from_user)
    await finalize_invite(uid)
    await c.message.edit_reply_markup(reply_markup=None)
    await send_flyer(c.bot, uid, await get_flyer(code))


@router.message(Form.service)
async def service_wrong(m: Message):
    await m.answer("Выберите услугу кнопкой:", reply_markup=service_kb())


# -------------------------------------------------------------- сотрудники
class Staff(StatesGroup):
    code = State()
    add_id = State()
    invite = State()


STATUS_TXT = {
    "active": "🟢 Действителен",
    "redeemed": "🔴 УЖЕ ИСПОЛЬЗОВАН",
    "expired": "⚪️ Срок истёк",
}


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


MENU_ROW = [btn("🏠 В меню", "p:menu")]


def menu_text() -> str:
    return (
        "🛠 <b>Панель сотрудника</b>\n\n"
        "Выберите действие кнопкой ниже.\n"
        "Чтобы погасить флаер, просто отсканируйте QR клиента камерой телефона."
    )


def menu_kb(admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [btn("🔎 Найти флаер по коду", "p:code")],
        [btn("📊 Статистика", "p:stats"), btn("🕘 Последние погашения", "p:last")],
        [btn("🔗 Спец-ссылка для сотрудника ТЦ", "p:inv")],
    ]
    if admin:
        rows.append([btn("👥 Сотрудники", "p:staff"), btn("📥 Выгрузка CSV", "p:export")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_of(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


async def show_panel(bot: Bot, chat_id: int, uid: int):
    await bot.send_message(chat_id, menu_text(), reply_markup=menu_kb(uid in ADMIN_IDS))


async def panel_edit(bot: Bot, chat_id: int, msg_id: int, text: str, kb=None):
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                    reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            log.warning("edit failed: %s", e)


async def build_card(bot: Bot, code: str):
    f = await get_flyer(code)
    if not f:
        return "❌ Флаер с таким кодом не найден.", kb_of(MENU_ROW)
    st = eff_status(f)
    subscribed = await is_subscribed(bot, f["tg_id"])
    cl = await get_client(f["tg_id"])
    plate = ""
    if cl and cl["segment"] == "mall":
        plate = (f"🏬 <b>СОТРУДНИК ТЦ · {esc(cl['company'])}</b>\n"
                 "Тарифы для сотрудников ТЦ, сверьте бейдж/пропуск.\n\n")
    text = (
        f"🎟 <b>Флаер {f['code']}</b>\n"
        f"Статус: <b>{STATUS_TXT[st]}</b>\n\n" + plate +
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
    rows = []
    if st == "active":
        rows.append([btn("✅ Погасить флаер", f"rd:{f['code']}")])
    rows.append(MENU_ROW)
    return text, kb_of(*rows)


async def show_redeem_card(bot: Bot, chat_id: int, code: str):
    text, kb = await build_card(bot, code)
    await bot.send_message(chat_id, text, reply_markup=kb)


@router.callback_query(F.data.startswith("rd:"))
async def cb_redeem(c: CallbackQuery):
    if not await is_staff(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    code = c.data.split(":", 1)[1]
    res, f = await redeem(code, c.from_user.id)
    back = kb_of(MENU_ROW)
    if res == "ok":
        await c.answer("Флаер погашен ✅", show_alert=True)
        await c.message.edit_text(
            c.message.html_text + f"\n\n✅ <b>Погашено</b> {fmt(f['redeemed_at'])}",
            reply_markup=back,
        )
        try:
            await c.bot.send_message(
                f["tg_id"],
                "✅ Флаер использован. Спасибо, что выбрали нас! Ждём вас снова 🙌",
            )
        except Exception:
            pass
    elif res == "used":
        await c.answer("Этот флаер уже был использован!", show_alert=True)
        await c.message.edit_reply_markup(reply_markup=back)
    elif res == "expired":
        await c.answer("Срок действия флаера истёк", show_alert=True)
        await c.message.edit_reply_markup(reply_markup=back)
    else:
        await c.answer("Флаер не найден", show_alert=True)


async def stats_text() -> str:
    now = now_utc()
    total = (await fetchone("SELECT COUNT(*) n FROM flyers"))["n"]
    used = (await fetchone("SELECT COUNT(*) n FROM flyers WHERE status='redeemed'"))["n"]
    expired = (await fetchone(
        "SELECT COUNT(*) n FROM flyers WHERE status='active' AND expires_at<?", (now,)))["n"]
    lines = [
        "📊 <b>Статистика</b>",
        f"Выдано флаеров: {total}",
        f"Погашено: {used}",
        f"Активных: {total - used - expired}",
        f"Просрочено: {expired}",
        "",
        "<b>По услугам (выдано / погашено):</b>",
    ]
    for key, name in SERVICES.items():
        t = (await fetchone("SELECT COUNT(*) n FROM flyers WHERE service=?", (key,)))["n"]
        u = (await fetchone(
            "SELECT COUNT(*) n FROM flyers WHERE service=? AND status='redeemed'", (key,)))["n"]
        lines.append(f"{esc(name)}: {t} / {u}")
    lines += ["", "<b>По клиентам (выдано / погашено):</b>"]
    seg = await fetchall(
        "SELECT COALESCE(c.segment,'client') seg, COUNT(*) t, "
        "SUM(CASE WHEN f.status='redeemed' THEN 1 ELSE 0 END) u "
        "FROM flyers f LEFT JOIN clients c ON c.tg_id=f.tg_id GROUP BY seg")
    d = {r["seg"]: (r["t"], r["u"]) for r in seg}
    for key in ("client", "mall"):
        t, u = d.get(key, (0, 0))
        lines.append(f"{SEGMENT_TXT[key]}: {t} / {u}")
    comp = await fetchall(
        "SELECT c.company co, COUNT(*) t, "
        "SUM(CASE WHEN f.status='redeemed' THEN 1 ELSE 0 END) u "
        "FROM flyers f JOIN clients c ON c.tg_id=f.tg_id "
        "WHERE c.segment='mall' GROUP BY c.company ORDER BY t DESC LIMIT 15")
    if comp:
        lines += ["", "<b>Компании ТЦ (выдано / погашено):</b>"]
        lines += [f"{esc(r['co'])}: {r['t']} / {r['u']}" for r in comp]
    return "\n".join(lines)


async def last_text() -> str:
    rows = await fetchall(
        "SELECT f.*, c.segment seg, c.company comp FROM flyers f "
        "LEFT JOIN clients c ON c.tg_id=f.tg_id "
        "WHERE f.status='redeemed' ORDER BY f.redeemed_at DESC LIMIT 10")
    if not rows:
        return "🕘 <b>Последние погашения</b>\n\nПока ни один флаер не погашен."
    lines = ["🕘 <b>Последние погашения</b>\n"]
    for r in rows:
        lines.append(
            f"{fmt(r['redeemed_at'])} · <code>{r['code']}</code>\n"
            f"   {'🏬 ' if r['seg'] == 'mall' else ''}{esc(r['full_name'])} · {esc(r['device'])} · "
            f"{esc(SERVICES.get(r['service'], r['service']))}"
        )
    return "\n".join(lines)


async def export_csv() -> bytes:
    rows = await fetchall(
        "SELECT f.*, COALESCE(c.segment,'client') seg, COALESCE(c.company,'') comp "
        "FROM flyers f LEFT JOIN clients c ON c.tg_id=f.tg_id ORDER BY f.id")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["код", "tg_id", "ник", "имя", "телефон", "устройство", "услуга",
                "статус", "выдан", "действует до", "погашен", "кем погашен", "сегмент", "компания"])
    for r in rows:
        w.writerow([
            r["code"], r["tg_id"], r["username"], r["full_name"], "+" + r["phone"],
            r["device"], SERVICES.get(r["service"], r["service"]), eff_status(r),
            fmt(r["created_at"]), fmt(r["expires_at"]),
            fmt(r["redeemed_at"]) if r["redeemed_at"] else "", r["redeemed_by"] or "",
            SEGMENT_TXT.get(r["seg"], r["seg"]), r["comp"],
        ])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM для Excel


async def staff_screen(note: str = ""):
    rows = await fetchall("SELECT * FROM staff ORDER BY added_at")
    admins = ", ".join(f"<code>{i}</code>" for i in sorted(ADMIN_IDS)) or "—"
    text = (note + "\n\n" if note else "") + "👥 <b>Сотрудники</b>\n\n" \
        f"Админы (задаются в настройках): {admins}\n\n"
    if rows:
        text += "Сотрудники — нажмите на имя, чтобы удалить:"
    else:
        text += "Сотрудников пока нет."
    kb_rows = [
        [btn(f"❌ {r['name'] or r['tg_id']} ({r['tg_id']})", f"p:del:{r['tg_id']}")]
        for r in rows
    ]
    kb_rows.append([btn("➕ Добавить сотрудника", "p:add")])
    kb_rows.append(MENU_ROW)
    return text, kb_of(*kb_rows)


async def invites_screen(bot: Bot, note: str = ""):
    uname = await bot_username(bot)
    rows = await fetchall("SELECT * FROM invites ORDER BY created_at DESC LIMIT 10")
    lines = [(note + "\n") if note else "",
             "🔗 <b>Спец-ссылки для сотрудников ТЦ</b>\n",
             f"Каждая ссылка одноразовая: сработает только у первого, кто её откроет "
             f"(действует {INVITE_DAYS} дн.). Пересылать её бесполезно.\n"]
    now = now_utc()
    for r in rows:
        if r["used_at"]:
            st = "✅ зарегистрирован"
        elif r["claimed_by"]:
            st = "⏳ открыта, ждём регистрации"
        elif r["expires_at"] < now:
            st = "⌛️ истекла"
        else:
            st = "🟡 не использована"
        line = f"• <b>{esc(r['company'])}</b> — {st}"
        if st.startswith("🟡"):
            line += f"\n  <code>https://t.me/{uname}?start=i_{r['code']}</code>"
        lines.append(line)
    if not rows:
        lines.append("Пока ни одной ссылки не создано.")
    return "\n".join(lines), kb_of([btn("➕ Создать ссылку", "p:invnew")], MENU_ROW)


@router.callback_query(F.data.startswith("p:"))
async def cb_panel(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    if not await is_staff(uid):
        await c.answer("Нет доступа", show_alert=True)
        return
    admin = uid in ADMIN_IDS
    action = c.data[2:]
    bot, chat, mid = c.bot, c.message.chat.id, c.message.message_id
    await c.answer()

    if action == "menu":
        await state.clear()
        await panel_edit(bot, chat, mid, menu_text(), menu_kb(admin))
    elif action == "code":
        await state.set_state(Staff.code)
        await state.update_data(panel=mid)
        await panel_edit(
            bot, chat, mid,
            "🔎 <b>Поиск флаера</b>\n\nОтправьте код флаера (10 символов, "
            "указан под QR-кодом у клиента).",
            kb_of([btn("✖️ Отмена", "p:menu")]),
        )
    elif action == "inv":
        await state.clear()
        text, kb = await invites_screen(bot)
        await panel_edit(bot, chat, mid, text, kb)
    elif action == "invnew":
        await state.set_state(Staff.invite)
        await state.update_data(panel=mid)
        await panel_edit(
            bot, chat, mid,
            "➕ <b>Новая спец-ссылка</b>\n\nНапишите, для кого она: компанию и, если нужно, "
            "имя сотрудника. Например: <i>Магазин «Х», Иван</i>.",
            kb_of([btn("✖️ Отмена", "p:inv")]),
        )
    elif action == "stats":
        await panel_edit(bot, chat, mid, await stats_text(),
                         kb_of([btn("🔄 Обновить", "p:stats")], MENU_ROW))
    elif action == "last":
        await panel_edit(bot, chat, mid, await last_text(),
                         kb_of([btn("🔄 Обновить", "p:last")], MENU_ROW))
    elif action == "export" and admin:
        await bot.send_document(chat, BufferedInputFile(await export_csv(),
                                                        filename="flyers.csv"))
    elif action == "staff" and admin:
        await state.clear()
        text, kb = await staff_screen()
        await panel_edit(bot, chat, mid, text, kb)
    elif action == "add" and admin:
        await state.set_state(Staff.add_id)
        await state.update_data(panel=mid)
        await panel_edit(
            bot, chat, mid,
            "➕ <b>Добавить сотрудника</b>\n\nОтправьте Telegram ID сотрудника (число). "
            "Узнать его можно у бота @userinfobot.",
            kb_of([btn("✖️ Отмена", "p:staff")]),
        )
    elif action.startswith("del:") and admin:
        tid = int(action.split(":")[1])
        await panel_edit(
            bot, chat, mid, f"Удалить сотрудника <code>{tid}</code>?",
            kb_of([btn("✅ Да, удалить", f"p:deldo:{tid}"), btn("↩️ Нет", "p:staff")]),
        )
    elif action.startswith("deldo:") and admin:
        tid = int(action.split(":")[1])
        await execute("DELETE FROM staff WHERE tg_id=?", (tid,))
        text, kb = await staff_screen(f"Сотрудник {tid} удалён")
        await panel_edit(bot, chat, mid, text, kb)


@router.message(Staff.code, F.text)
async def staff_code_input(m: Message, state: FSMContext):
    if not await is_staff(m.from_user.id):
        return
    panel = (await state.get_data()).get("panel")
    await state.clear()
    try:
        await m.delete()
    except Exception:
        pass
    code = re.sub(r"\s+", "", m.text).upper()
    text, kb = await build_card(m.bot, code)
    if panel:
        await panel_edit(m.bot, m.chat.id, panel, text, kb)
    else:
        await m.answer(text, reply_markup=kb)


@router.message(Staff.invite, F.text)
async def staff_invite_input(m: Message, state: FSMContext):
    if not await is_staff(m.from_user.id):
        return
    panel = (await state.get_data()).get("panel")
    try:
        await m.delete()
    except Exception:
        pass
    company = " ".join(m.text.split())
    if not 2 <= len(company) <= 60:
        if panel:
            await panel_edit(m.bot, m.chat.id, panel,
                             "❌ Название должно быть от 2 до 60 символов. Попробуйте ещё раз.",
                             kb_of([btn("✖️ Отмена", "p:inv")]))
        return
    await state.clear()
    code = await create_invite(company, m.from_user.id)
    link = f"https://t.me/{await bot_username(m.bot)}?start=i_{code}"
    text = (
        f"✅ Ссылка для <b>{esc(company)}</b> создана:\n\n<code>{link}</code>\n\n"
        f"Нажмите на ссылку, чтобы скопировать, и отправьте человеку. Она одноразовая: "
        f"привяжется к первому, кто её откроет, и действует {INVITE_DAYS} дн."
    )
    kb = kb_of([btn("➕ Создать ещё", "p:invnew")], [btn("📋 Все ссылки", "p:inv")], MENU_ROW)
    if panel:
        await panel_edit(m.bot, m.chat.id, panel, text, kb)
    else:
        await m.answer(text, reply_markup=kb)


@router.message(Staff.add_id, F.text)
async def staff_add_input(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    panel = (await state.get_data()).get("panel")
    try:
        await m.delete()
    except Exception:
        pass
    nums = re.findall(r"\d+", m.text)
    if not nums or len(nums[0]) < 5:
        if panel:
            await panel_edit(
                m.bot, m.chat.id, panel,
                "❌ Это не похоже на Telegram ID. Отправьте число, например "
                "<code>123456789</code>.",
                kb_of([btn("✖️ Отмена", "p:staff")]),
            )
        return
    tid = int(nums[0])
    await state.clear()
    if tid in ADMIN_IDS:
        note = f"ℹ️ {tid} уже админ, добавлять не нужно."
    else:
        name = ""
        try:
            ch = await m.bot.get_chat(tid)
            name = " ".join(filter(None, [ch.first_name, ch.last_name]))
            if ch.username:
                name += f" @{ch.username}"
        except Exception:
            pass
        await execute("INSERT OR REPLACE INTO staff(tg_id,name,added_at) VALUES(?,?,?)",
                      (tid, name.strip(), now_utc()))
        try:
            await m.bot.send_message(
                tid, "✅ Вас добавили как сотрудника. Нажмите /start, чтобы открыть панель.")
            note = f"✅ Сотрудник {tid} добавлен, уведомление отправлено."
        except Exception:
            note = (f"✅ Сотрудник {tid} добавлен. Пусть он откроет бота и нажмёт "
                    "/start — сейчас бот не смог ему написать.")
    text, kb = await staff_screen(note)
    if panel:
        await panel_edit(m.bot, m.chat.id, panel, text, kb)
    else:
        await m.answer(text, reply_markup=kb)


@router.message(Command("menu"))
async def cmd_menu(m: Message, state: FSMContext):
    if not await is_staff(m.from_user.id):
        return
    await state.clear()
    await show_panel(m.bot, m.chat.id, m.from_user.id)


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
