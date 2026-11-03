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

CATEGORIES = [
    ("shops", "🛍 Магазины"),
    ("food", "🍔 Кафе и рестораны"),
    ("services", "💼 Услуги"),
    ("fun", "🎮 Спорт и развлечения"),
    ("other", "➕ Другая (администрация и др.)"),
]
CAT_NAME = dict(CATEGORIES)
PAGE = 10

# Стартовый список компаний ТЦ (заполняется один раз, дальше правится в панели)
SEED = {
    "shops": [
        ("Campione", 2), ("SELA", 2), ("Zielinski&Rozen", 1), ("Аптека 36,6", 1),
        ("Детский мир", 2), ("КуулКлевер", 1), ("Перекресток", 1), ("Элизе", 1),
        ("Зоозавр", 1), ("Летуаль", 1), ("Оптимист Оптика", 1), ("Икорный", 1),
        ("Алёнка", 1), ("Marc & André", 1), ("MIUZ Diamonds", 1), ("Storiz", 2),
        ("Комната сладостей", 1), ("Evola", 2), ("Читай город", 2), ("Азбука вкуса", 2),
        ("Amareta", 2), ("Togas", 1), ("Секретный сад", 2), ("2банки", 3),
        ("Золотое Яблоко (пункт выдачи)", 1), ("Табак", 1), ("DNS", 2),
        ("Лаборатория парфюма", 1), ("ПАЛЕРМО", 1), ("Арбатская сказка", 2),
    ],
    "food": [
        ("BAO BEI", 1), ("Frank by Баста", 1), ("French Bakery", 1), ("Итальяно", 1),
        ("Грузинская кухня", 1), ("PhoNgon", 1), ("Ростикс", 2), ("Топ том ям", 1),
        ("Бургер Кинг", 2), ("Китайский чайник", 1),
        ("Кулинарная лавка братьев Караваевых", 1), ("Нон", 1), ("Сыроварня", 1),
        ("Теремок", 1), ("PIMS", 1), ("Лайки.Бургер", 1), ("Art Doner", 1),
        ("Стрелка", 1), ("Mosdesserts", 1), ("Bageatteria", 1), ("Турецкая лавка", 1),
        ("ZИL bar", 1), ("Милти", 1), ("ДоДо пицца", 1), ("Тануки", 1),
        ("GyrozzGorilloz", 1),
    ],
    "services": [
        ("Anextour", 1), ("Selfie Nail & Beauty Club", 1), ("Барбершоп «Раздевалка»", 1),
        ("Салон цифровой экосистемы МТС", 1), ("Т2Мобайл", 1), ("Химчистка Caritta", 1),
        ("Форабанк", 1), ("First Step", 2), ("Салон «Билайн»", 1), ("Сбербанк", 2),
        ("ВТБ", 2), ("Т-банк", 2), ("Альфа Банк", 2), ("Светлана Ситникова", 3),
        ("Ozon", 1), ("Автосалон «Х-Авто»", 2), ("Фотостудия/лофт Эскимо", 3),
        ("Офтальмологическая клиника Review", 3), ("Студия гидромассажа «АКВАБЬЮТИ»", 2),
    ],
    "fun": [
        ("Yobody Fitness", 3), ("Европейский Гимнастический Центр", 3),
        ("Компьютерный клуб Cyber Rush", 3), ("Студия-школа Аллы Духовой Тодес", 3),
        ("MetaRacing", 3), ("VR-арена Another World", 3), ("Аква Пупс", 2),
        ("Клуб единоборств BRAVE", 2), ("PiPong", 2),
    ],
    "other": [
        ("Администрация ТЦ", None), ("Охрана", None),
        ("Клининг и технический персонал", None), ("Другая организация", None),
    ],
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


def make_qr(link: str, box: int = 12, border: int = 3) -> bytes:
    img = qrcode.make(link, box_size=box, border=border)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


def make_qr_svg(link: str) -> bytes:
    import qrcode.image.svg
    img = qrcode.make(link, image_factory=qrcode.image.svg.SvgPathImage, border=4)
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
CREATE TABLE IF NOT EXISTS companies(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    floor INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(category, name)
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mall_intents(
    tg_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL
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
    n = (await fetchone("SELECT COUNT(*) n FROM companies"))["n"]
    if n == 0:  # первичное заполнение списка компаний
        for cat, items in SEED.items():
            for name, floor in items:
                await db.execute(
                    "INSERT OR IGNORE INTO companies(category,name,floor) VALUES(?,?,?)",
                    (cat, name, floor))
    await db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('mall_code',?)",
                     (secrets.token_hex(4).upper(),))
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


async def get_setting(key: str) -> str:
    return (await fetchone("SELECT value FROM settings WHERE key=?", (key,)))["value"]


async def list_companies(cat: str, active_only: bool = True):
    q = "SELECT * FROM companies WHERE category=?" + (" AND active=1" if active_only else "")
    rows = await fetchall(q, (cat,))
    return sorted(rows, key=lambda r: r["name"].casefold())


def page_slice(items, page: int):
    pages = max(1, (len(items) + PAGE - 1) // PAGE)
    page = min(max(page, 0), pages - 1)
    return items[page * PAGE:(page + 1) * PAGE], page, pages


async def pending_invite(tg_id: int):
    return await fetchone(
        "SELECT * FROM invites WHERE claimed_by=? AND used_at IS NULL", (tg_id,))


async def needs_company(tg_id: int) -> bool:
    """Пришёл по печатному коду для сотрудников ТЦ и ещё не выбрал компанию."""
    if await pending_invite(tg_id):
        return False
    return await fetchone("SELECT 1 FROM mall_intents WHERE tg_id=?", (tg_id,)) is not None


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
    company = State()


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
    if arg.startswith("m_") and await get_flyer_by_tg(uid) is None:
        if arg[2:].upper() == await get_setting("mall_code"):
            await execute("INSERT OR REPLACE INTO mall_intents(tg_id,created_at) VALUES(?,?)",
                          (uid, now_utc()))
        else:
            await m.answer("⚠️ Этот код устарел. Вы можете получить флаер как обычный клиент.")
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
    await c.answer()
    await state.update_data(service=key)
    if await needs_company(uid):
        await state.set_state(Form.company)
        await safe_edit(c.message, "🏬 Выберите категорию, в которой работает ваша компания:",
                        cat_kb())
        return
    await issue_flyer(c.bot, c.from_user, c.message, state)


async def safe_edit(msg: Message, text: str, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            log.warning("edit failed: %s", e)


async def issue_flyer(bot: Bot, user, msg: Message, state: FSMContext, company: str | None = None):
    data = await state.get_data()
    uid = user.id
    username = f"@{user.username}" if user.username else ""
    code = await create_flyer(uid, username, user.full_name, data["phone"],
                              data["device"], data["service"])
    await state.clear()
    if not code:
        await msg.answer("Флаер для вас или для этого номера уже оформлен.")
        return
    await touch_client(user)
    if company:
        await execute("UPDATE clients SET segment='mall', company=? WHERE tg_id=?",
                      (company, uid))
    else:
        await finalize_invite(uid)
    await execute("DELETE FROM mall_intents WHERE tg_id=?", (uid,))
    try:
        await msg.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await send_flyer(bot, uid, await get_flyer(code))


def cat_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"co:c:{key}:0")]
                         for key, label in CATEGORIES])


def pager_row(prefix: str, page: int, pages: int):
    if pages <= 1:
        return None
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}:{page - 1}"))
    row.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        row.append(InlineKeyboardButton(text="➡️", callback_data=f"{prefix}:{page + 1}"))
    return row


@router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@router.callback_query(Form.company, F.data.startswith("co:"))
async def cb_company(c: CallbackQuery, state: FSMContext):
    parts = c.data.split(":")
    act = parts[1]
    await c.answer()
    if act == "cats":
        await safe_edit(c.message, "🏬 Выберите категорию, в которой работает ваша компания:",
                        cat_kb())
    elif act == "c":
        key, page = parts[2], int(parts[3])
        items, page, pages = page_slice(await list_companies(key), page)
        rows = [[InlineKeyboardButton(text=r["name"], callback_data=f"co:p:{r['id']}")]
                for r in items]
        pr = pager_row(f"co:c:{key}", page, pages)
        if pr:
            rows.append(pr)
        rows.append([InlineKeyboardButton(text="↩️ К категориям", callback_data="co:cats")])
        await safe_edit(c.message, f"{CAT_NAME.get(key, '')}\n\nНайдите свою компанию в списке:",
                        InlineKeyboardMarkup(inline_keyboard=rows))
    elif act == "p":
        co = await fetchone("SELECT * FROM companies WHERE id=? AND active=1", (int(parts[2]),))
        if not co:
            await c.message.answer("Компания не найдена. Нажмите /start и попробуйте снова.")
            return
        await safe_edit(
            c.message, f"Ваша компания: <b>{esc(co['name'])}</b>. Всё верно?",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да", callback_data=f"co:ok:{co['id']}"),
                 InlineKeyboardButton(text="↩️ Назад", callback_data=f"co:c:{co['category']}:0")]]))
    elif act == "ok":
        co = await fetchone("SELECT * FROM companies WHERE id=? AND active=1", (int(parts[2]),))
        if not co:
            await c.message.answer("Компания не найдена. Нажмите /start и попробуйте снова.")
            return
        await issue_flyer(c.bot, c.from_user, c.message, state, company=co["name"])


@router.callback_query(F.data.startswith("co:"))
async def cb_company_stale(c: CallbackQuery):
    await c.answer("Сессия устарела, нажмите /start", show_alert=True)


@router.message(Form.service)
async def service_wrong(m: Message):
    await m.answer("Выберите услугу кнопкой:", reply_markup=service_kb())


# -------------------------------------------------------------- сотрудники
class Staff(StatesGroup):
    code = State()
    add_id = State()
    invite = State()
    company = State()


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
        rows.append([btn("🏬 Компании ТЦ", "p:cos"), btn("🖨 Флаер для ТЦ", "p:print")])
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
        co = await fetchone("SELECT * FROM companies WHERE name=?", (cl["company"],))
        where = f" ({co['floor']} этаж)" if co and co["floor"] else ""
        plate = (f"🏬 <b>СОТРУДНИК ТЦ · {esc(cl['company'])}</b>{where}\n"
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
    cats = await fetchall(
        "SELECT COALESCE(co.category,'other') cat, COUNT(*) t, "
        "SUM(CASE WHEN f.status='redeemed' THEN 1 ELSE 0 END) u "
        "FROM flyers f JOIN clients c ON c.tg_id=f.tg_id AND c.segment='mall' "
        "LEFT JOIN companies co ON co.name=c.company GROUP BY cat")
    if cats:
        lines += ["", "<b>Сотрудники ТЦ по категориям (выдано / погашено):</b>"]
        lines += [f"{esc(CAT_NAME.get(r['cat'], r['cat']))}: {r['t']} / {r['u']}"
                  for r in cats]
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


async def mall_link(bot: Bot) -> str:
    return f"https://t.me/{await bot_username(bot)}?start=m_{await get_setting('mall_code')}"


async def print_screen(bot: Bot, note: str = ""):
    link = await mall_link(bot)
    text = ((note + "\n\n") if note else "") + (
        "🖨 <b>Флаер для сотрудников ТЦ</b>\n\n"
        "Общая ссылка для печати (по ней клиент выбирает свою компанию из списка и "
        f"получает отметку «сотрудник ТЦ»):\n\n<code>{link}</code>\n\n"
        "Нажмите «Прислать QR», чтобы получить код в PNG и SVG для печати.")
    return text, kb_of([btn("📥 Прислать QR", "p:printqr")],
                       [btn("🔄 Перевыпустить код", "p:printnew")], MENU_ROW)


async def cos_screen():
    rows = []
    for key, label in CATEGORIES:
        a = (await fetchone("SELECT COUNT(*) n FROM companies WHERE category=? AND active=1",
                            (key,)))["n"]
        t = (await fetchone("SELECT COUNT(*) n FROM companies WHERE category=?", (key,)))["n"]
        rows.append([btn(f"{label} ({a}/{t})" if a != t else f"{label} ({t})", f"p:cc:{key}:0")])
    rows.append(MENU_ROW)
    return ("🏬 <b>Компании ТЦ</b>\n\nИз этого списка клиенты выбирают место работы. "
            "Выберите категорию:"), kb_of(*rows)


async def cat_screen(key: str, page: int = 0, note: str = ""):
    items, page, pages = page_slice(await list_companies(key, active_only=False), page)
    rows = [[btn(("🚫 " if not r["active"] else "") + r["name"], f"p:co:{r['id']}")]
            for r in items]
    pr = pager_row(f"p:cc:{key}", page, pages)
    if pr:
        rows.append(pr)
    rows.append([btn("➕ Добавить компанию", f"p:coadd:{key}")])
    rows.append([btn("↩️ Категории", "p:cos")])
    text = ((note + "\n\n") if note else "") + (
        f"{esc(CAT_NAME.get(key, key))}\n\nНажмите на компанию, чтобы скрыть или удалить. "
        "🚫 — скрыта из списка.")
    return text, kb_of(*rows)


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
    elif action == "cos" and admin:
        await state.clear()
        text, kb = await cos_screen()
        await panel_edit(bot, chat, mid, text, kb)
    elif action.startswith("cc:") and admin:
        _, key, page = action.split(":")
        text, kb = await cat_screen(key, int(page))
        await panel_edit(bot, chat, mid, text, kb)
    elif action.startswith("co:") and admin:
        co = await fetchone("SELECT * FROM companies WHERE id=?", (int(action.split(":")[1]),))
        if co:
            st = "показывается в списке" if co["active"] else "скрыта из списка"
            fl = f"{co['floor']} этаж" if co["floor"] else "этаж не указан"
            await panel_edit(
                bot, chat, mid,
                f"🏬 <b>{esc(co['name'])}</b>\n{esc(CAT_NAME[co['category']])} · {fl}\n"
                f"Сейчас: {st}",
                kb_of([btn("🙈 Скрыть" if co["active"] else "👁 Показать", f"p:cotg:{co['id']}"),
                       btn("🗑 Удалить", f"p:codel:{co['id']}")],
                      [btn("↩️ Назад", f"p:cc:{co['category']}:0")]))
    elif action.startswith("cotg:") and admin:
        cid = int(action.split(":")[1])
        await execute("UPDATE companies SET active=1-active WHERE id=?", (cid,))
        co = await fetchone("SELECT * FROM companies WHERE id=?", (cid,))
        text, kb = await cat_screen(co["category"], 0, "✅ Готово")
        await panel_edit(bot, chat, mid, text, kb)
    elif action.startswith("codel:") and admin:
        cid = int(action.split(":")[1])
        co = await fetchone("SELECT * FROM companies WHERE id=?", (cid,))
        if co:
            await panel_edit(
                bot, chat, mid,
                f"Удалить «{esc(co['name'])}» из списка? У клиентов, которые её уже выбрали, "
                "название сохранится.",
                kb_of([btn("✅ Да, удалить", f"p:codel2:{cid}"), btn("↩️ Нет", f"p:co:{cid}")]))
    elif action.startswith("codel2:") and admin:
        cid = int(action.split(":")[1])
        co = await fetchone("SELECT * FROM companies WHERE id=?", (cid,))
        await execute("DELETE FROM companies WHERE id=?", (cid,))
        text, kb = await cat_screen(co["category"] if co else "other", 0, "🗑 Удалено")
        await panel_edit(bot, chat, mid, text, kb)
    elif action.startswith("coadd:") and admin:
        key = action.split(":")[1]
        await state.set_state(Staff.company)
        await state.update_data(panel=mid, cat=key)
        await panel_edit(
            bot, chat, mid,
            f"➕ <b>Новая компания</b>\n{esc(CAT_NAME.get(key, ''))}\n\n"
            "Отправьте название. Если нужно указать этаж, добавьте его через ` | `, "
            "например: <i>Кофейня Х | 2</i>",
            kb_of([btn("✖️ Отмена", f"p:cc:{key}:0")]))
    elif action == "print" and admin:
        text, kb = await print_screen(bot)
        await panel_edit(bot, chat, mid, text, kb)
    elif action == "printqr" and admin:
        link = await mall_link(bot)
        await bot.send_document(chat, BufferedInputFile(make_qr(link, box=24, border=4),
                                                        filename="qr_flyer_tc.png"))
        await bot.send_document(chat, BufferedInputFile(make_qr_svg(link),
                                                        filename="qr_flyer_tc.svg"),
                                caption="Векторный QR для типографии")
    elif action == "printnew" and admin:
        await panel_edit(
            bot, chat, mid,
            "🔄 <b>Перевыпустить код?</b>\n\nСтарый QR на уже напечатанных флаерах "
            "перестанет работать. Сделайте это, только если код попал не в те руки.",
            kb_of([btn("✅ Да, перевыпустить", "p:printnew2"), btn("↩️ Нет", "p:print")]))
    elif action == "printnew2" and admin:
        await execute("UPDATE settings SET value=? WHERE key='mall_code'",
                      (secrets.token_hex(4).upper(),))
        text, kb = await print_screen(bot, "✅ Новый код создан. Отправьте QR в печать заново.")
        await panel_edit(bot, chat, mid, text, kb)
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


@router.message(Staff.company, F.text)
async def staff_company_input(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    panel, key = data.get("panel"), data.get("cat", "other")
    try:
        await m.delete()
    except Exception:
        pass
    name, _, fl = m.text.partition("|")
    name = " ".join(name.split())
    floor = int(fl.strip()) if fl.strip().isdigit() else None
    if not 2 <= len(name) <= 60:
        if panel:
            await panel_edit(m.bot, m.chat.id, panel,
                             "❌ Название должно быть от 2 до 60 символов. Попробуйте ещё раз.",
                             kb_of([btn("✖️ Отмена", f"p:cc:{key}:0")]))
        return
    await state.clear()
    n = await execute("INSERT OR IGNORE INTO companies(category,name,floor) VALUES(?,?,?)",
                      (key, name, floor))
    note = f"✅ «{esc(name)}» добавлена" if n else f"ℹ️ «{esc(name)}» уже есть в списке"
    text, kb = await cat_screen(key, 0, note)
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
