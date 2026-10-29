import asyncio
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, ChatJoinRequest, ChatMemberUpdated, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, BotCommand,
    BotCommandScopeAllPrivateChats, BotCommandScopeChat
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from storage import Store

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("lavender_moderator")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID_RAW = os.getenv("CHAT_ID", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
DB_PATH = os.getenv("DB_PATH", "moderator.sqlite3")
WELCOME_TEXT = os.getenv(
    "WELCOME_TEXT",
    "Добро пожаловать в Lavender Dream! 💜\nПожалуйста, ознакомься с правилами чата."
)
RAID_WINDOW_SECONDS = int(os.getenv("RAID_WINDOW_SECONDS", "60"))
RAID_THRESHOLD = int(os.getenv("RAID_THRESHOLD", "10"))
USER_REQUEST_COOLDOWN = int(os.getenv("USER_REQUEST_COOLDOWN", "30"))
BOT_PUBLIC_LINK = os.getenv("BOT_PUBLIC_LINK", "").strip()
RULES_TEXT = os.getenv("RULES_TEXT", "Перед заполнением анкеты ознакомься с правилами группы.").strip()
RULES_URL = os.getenv("RULES_URL", "").strip()
INFO_CHANNEL_URL = os.getenv("INFO_CHANNEL_URL", "").strip()
LESSONS_CHANNEL_URL = os.getenv("LESSONS_CHANNEL_URL", "").strip()

if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN. Скопируй .env.example в .env и заполни настройки.")
try:
    CHAT_ID = int(CHAT_ID_RAW)
except ValueError:
    raise SystemExit("CHAT_ID должен быть числовым ID группы/супергруппы, например -1001234567890.")
try:
    ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()}
except ValueError:
    raise SystemExit("ADMIN_IDS должен содержать Telegram ID администраторов через запятую.")
if not ADMIN_IDS:
    raise SystemExit("Добавь хотя бы один Telegram ID в ADMIN_IDS.")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
store = Store(DB_PATH)
recent_requests = deque()
raid_until = 0.0

class ApplicationForm(StatesGroup):
    photo = State()
    character_name = State()
    nickname = State()
    telegram_username = State()

class RejectionForm(StatesGroup):
    reason = State()



def esc(value) -> str:
    # Minimal HTML escaping for untrusted Telegram fields.
    s = str(value or "")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def user_label(user) -> str:
    name = " ".join(x for x in [user.first_name, user.last_name] if x).strip()
    if user.username:
        name += f" (@{esc(user.username)})"
    return esc(name or "Без имени")


def admin_keyboard(request_id: int, user_id: int, suspicious: bool) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{request_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{request_id}"),
    ]
    rows = [buttons, [
        InlineKeyboardButton(text="🚫 В чёрный список", callback_data=f"ban:{user_id}:{request_id}")
    ]]
    if suspicious:
        rows.insert(0, [InlineKeyboardButton(text="⚠️ Подозрительная заявка", callback_data="noop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def is_admin(user_id: int, chat_id: int | None = None) -> bool:
    if user_id in ADMIN_IDS:
        return True
    # Also honor current Telegram chat administrators, but only if explicitly requested.
    if chat_id is not None:
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
        except Exception:
            return False
    return False


def detect_raid(user_id: int) -> bool:
    global raid_until
    now = time.monotonic()
    while recent_requests and now - recent_requests[0] > RAID_WINDOW_SECONDS:
        recent_requests.popleft()
    recent_requests.append(now)
    if len(recent_requests) >= RAID_THRESHOLD:
        raid_until = max(raid_until, now + RAID_WINDOW_SECONDS)
    # A raid is a warning only: never auto-approve or auto-reject.
    return now < raid_until


def application_review_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить анкету", callback_data=f"appapprove:{application_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"appreject:{application_id}"),
    ]])


async def update_application_review_cards(application_id: int, status_line: str, fallback_message=None):
    """Update every admin's separate DM card for this application."""
    cards = store.application_review_messages(application_id)
    if not cards and fallback_message is not None:
        cards = [{
            "chat_id": fallback_message.chat.id,
            "message_id": fallback_message.message_id,
            "caption": fallback_message.caption or "",
        }]

    updated = 0
    for card in cards:
        caption = card.get("caption", "") or ""
        # Avoid adding the same result twice if a callback is retried.
        if status_line.strip() not in caption:
            caption += status_line
        try:
            await bot.edit_message_caption(
                chat_id=card["chat_id"],
                message_id=card["message_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
            updated += 1
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            # One admin may have deleted the message or blocked the bot; still update the rest.
            log.warning(
                "Не удалось обновить карточку анкеты №%s у chat_id=%s: %s",
                application_id, card["chat_id"], exc,
            )
            try:
                await bot.edit_message_reply_markup(
                    chat_id=card["chat_id"], message_id=card["message_id"], reply_markup=None
                )
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
    return updated

@dp.message(Command("start"), F.chat.type == "private")
async def start_cmd(message: Message, state: FSMContext, command: CommandObject):
    payload = (command.args or "").strip()
    if payload not in ("", "apply", "new"):
        await message.answer("Неизвестная ссылка. Напиши /start, чтобы начать заново.")
        return
    if store.is_banned(message.from_user.id):
        await message.answer("🚫 Ты не можешь подать анкету: аккаунт находится в чёрном списке.")
        return
    existing = store.latest_application(message.from_user.id)
    if existing and existing["status"] == "pending":
        await message.answer(f"Твоя анкета уже отправлена администраторам (№{existing['id']}). Дождись решения.")
        return
    if existing and existing["status"] == "approved":
        await message.answer("Твоя анкета уже одобрена. Используй последнюю ссылку, которую я отправил.")
        return
    await state.clear()
    links = []
    if RULES_URL:
        links.append(f'📜 Правила: {RULES_URL}')
    if INFO_CHANNEL_URL:
        links.append(f'📢 Информационный канал: {INFO_CHANNEL_URL}')
    if LESSONS_CHANNEL_URL:
        links.append(f'📚 Канал с уроками: {LESSONS_CHANNEL_URL}')
    text = "Привет! 💜 Добро пожаловать в проверку Lavender Dream.\n\n" + RULES_TEXT
    if links:
        text += "\n\n" + "\n".join(links)
    text += "\n\nСейчас заполним анкету. Отправь фотографию персонажа отдельным сообщением."
    await message.answer(text, disable_web_page_preview=True)
    await state.set_state(ApplicationForm.photo)

@dp.message(ApplicationForm.photo, F.photo)
async def form_photo(message: Message, state: FSMContext):
    await state.update_data(photo_file_id=message.photo[-1].file_id)
    await message.answer("2/4. Имя персонажа (если нет — отправь тире: -).")
    await state.set_state(ApplicationForm.character_name)

@dp.message(ApplicationForm.photo)
async def form_photo_reminder(message: Message):
    await message.answer("Пожалуйста, отправь именно фотографию персонажа.")

@dp.message(ApplicationForm.character_name, F.text)
async def form_character_name(message: Message, state: FSMContext):
    value = message.text.strip()
    await state.update_data(character_name="" if value == "-" else value[:200])
    await message.answer("3/4. Напиши свой псевдоним (кличку).")
    await state.set_state(ApplicationForm.nickname)

@dp.message(ApplicationForm.nickname, F.text)
async def form_nickname(message: Message, state: FSMContext):
    value = message.text.strip()
    if len(value) < 1:
        await message.answer("Напиши псевдоним или кличку.")
        return
    await state.update_data(nickname=value[:200])
    await message.answer("4/4. Напиши свой Telegram username с @. Если его нет, напиши «нет».")
    await state.set_state(ApplicationForm.telegram_username)

@dp.message(ApplicationForm.telegram_username, F.text)
async def form_telegram_username(message: Message, state: FSMContext):
    value = message.text.strip()
    if value.lower() != "нет" and not value.lstrip("@").replace("_", "").isalnum():
        await message.answer("Введи корректный username (буквы, цифры и подчёркивания) или напиши «нет».")
        return
    username = "нет" if value.lower() == "нет" else "@" + value.lstrip("@")
    data = await state.get_data()
    user = message.from_user
    app_id = store.create_application(user.id, user.username or "", " ".join(x for x in [user.first_name, user.last_name] if x), data["photo_file_id"], data.get("character_name", ""), data["nickname"], username)
    store.log_action("application_submitted", user.id, None, f"application_id={app_id}")
    await state.clear()
    await message.answer(f"Анкета №{app_id} заполнена и отправлена администраторам.\nКогда проверишь всё ещё раз, напиши «готово», чтобы подтвердить отправку.")
    await state.update_data(confirm_application_id=app_id)

@dp.message(F.chat.type == "private", F.text.func(lambda text: (text or "").strip().casefold() == "готово"))
async def confirm_application(message: Message, state: FSMContext):
    app = store.latest_application(message.from_user.id, ["draft"])
    if not app:
        await message.answer("У тебя нет заполненной анкеты на подтверждение. Напиши /start, чтобы начать.")
        return
    # Sending this keyword confirms completion; the application is then sent to admins.
    caption = (f"📝 <b>Новая анкета Lavender Dream №{app['id']}</b>\n"
               f"Имя в Telegram: {esc(app['full_name'])}\nUsername аккаунта: @{esc(app['username']) if app['username'] else 'нет'}\n"
               f"Telegram ID: <code>{app['user_id']}</code>\n"
               f"1. Фото персонажа — приложено\n"
               f"2. Имя персонажа: {esc(app['character_name'] or 'не указано')}\n"
               f"3. Псевдоним: {esc(app['nickname'])}\n"
               f"4. Telegram username: {esc(app['telegram_username'])}\n\n"
               f"Подтверждено словом «готово». Проверьте анкету и примите решение.")
    sent = 0
    for admin_id in ADMIN_IDS:
        try:
            sent_message = await bot.send_photo(
                admin_id, app["photo_file_id"], caption=caption,
                reply_markup=application_review_keyboard(app["id"]), parse_mode=ParseMode.HTML
            )
            store.add_application_review_message(app["id"], admin_id, sent_message.message_id, caption)
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            log.warning("Не удалось отправить анкету админу %s: %s", admin_id, exc)
    if sent:
        store.set_application_status(app["id"], "pending")
        await message.answer("Готово! Анкета отправлена администраторам. Напишу тебе, когда они примут решение.")
        store.log_action("application_sent_to_admins", app["user_id"], None, f"application_id={app['id']}")
    else:
        await message.answer("Не удалось доставить анкету администраторам. Попроси администратора открыть бота и проверить ADMIN_IDS.")

@dp.callback_query(F.data.startswith("appapprove:"))
async def approve_application(call: CallbackQuery):
    if not call.from_user or not await is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return
    app_id = int(call.data.split(":", 1)[1])
    app = store.get_application(app_id)
    if not app or app["status"] != "pending":
        await call.answer("Анкета уже обработана.", show_alert=True)
        return
    if store.is_banned(app["user_id"]):
        await call.answer("Пользователь в чёрном списке.", show_alert=True)
        return
    try:
        invite = await bot.create_chat_invite_link(
            CHAT_ID, name=f"LD application {app_id}", creates_join_request=True
        )
        await bot.send_message(
            app["user_id"],
            "✅ Твоя анкета одобрена! Вот твоя персональная ссылка для вступления в Lavender Dream. "
            "Ссылка предназначена только для тебя:\n" + invite.invite_link,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        await call.answer(
            "Не удалось создать ссылку или написать пользователю. Проверь права бота и доступность лички.",
            show_alert=True,
        )
        store.log_action("application_approve_failed", app["user_id"], call.from_user.id, str(exc)[:300])
        return

    store.set_application_status(app_id, "approved", call.from_user.id)
    store.log_action("application_approved", app["user_id"], call.from_user.id, f"application_id={app_id}")
    status_line = f"\n\n✅ Анкета одобрена администратором {user_label(call.from_user)}."
    updated_cards = await update_application_review_cards(app_id, status_line, call.message)
    log.info("Одобрена анкета №%s; обновлено карточек администраторов: %s", app_id, updated_cards)
    await call.answer("Анкета одобрена.")

@dp.callback_query(F.data.startswith("appreject:"))
async def begin_reject_application(call: CallbackQuery, state: FSMContext):
    if not call.from_user or not await is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True); return
    app_id = int(call.data.split(":", 1)[1])
    app = store.get_application(app_id)
    if not app or app["status"] != "pending":
        await call.answer("Анкета уже обработана.", show_alert=True); return
    await state.set_state(RejectionForm.reason)
    await state.update_data(
        reject_application_id=app_id,
        reject_admin_id=call.from_user.id,
        reject_review_chat_id=call.message.chat.id,
        reject_review_message_id=call.message.message_id,
        reject_review_caption=call.message.caption or "",
    )
    await call.message.answer(
        f"Напиши одним сообщением причину отклонения анкеты №{app_id}.\n"
        "Я отправлю её участнику и отмечу анкету как отклонённую.\n"
        "Для отмены используй /cancel."
    )
    await call.answer("Ожидаю причину в этом чате.")

@dp.message(Command("cancel"), RejectionForm.reason, F.chat.type == "private")
async def cancel_rejection(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    app_id = data.get("reject_application_id")
    await state.clear()
    await message.answer(
        f"Отклонение анкеты №{app_id} отменено. Анкета осталась ожидающей решения."
        if app_id else "Ввод причины отменён."
    )

@dp.message(RejectionForm.reason, F.chat.type == "private", F.text)
async def reject_application_reason(message: Message, state: FSMContext):
    if not message.from_user or not await is_admin(message.from_user.id):
        await state.clear()
        return
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Причина не должна быть пустой. Напиши причину или используй /cancel.")
        return
    reason = reason[:1000]
    data = await state.get_data()
    app_id = data.get("reject_application_id")
    app = store.get_application(app_id) if app_id else None
    if not app or app["status"] != "pending":
        await state.clear()
        await message.answer("Анкета уже обработана или не найдена.")
        return

    store.set_application_status(app_id, "rejected", message.from_user.id, reason)
    delivery_ok = True
    try:
        await bot.send_message(
            app["user_id"],
            f"❌ Твоя анкета Lavender Dream отклонена.\nПричина: {esc(reason)}\n"
            "Ты можешь исправить анкету и отправить её заново командой /start.",
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        delivery_ok = False
        log.warning("Не удалось отправить причину отказа пользователю %s: %s", app["user_id"], exc)

    store.log_action(
        "application_rejected", app["user_id"], message.from_user.id,
        f"application_id={app_id}; reason={reason}; delivered={int(delivery_ok)}",
    )
    status_line = f"\n\n❌ Анкета отклонена администратором {user_label(message.from_user)}."
    updated_cards = await update_application_review_cards(app_id, status_line)
    log.info("Отклонена анкета №%s; обновлено карточек администраторов: %s", app_id, updated_cards)
    await state.clear()
    if delivery_ok:
        await message.answer(f"❌ Анкета №{app_id} отклонена. Причина отправлена пользователю, кнопки убраны.")
    else:
        await message.answer(
            f"❌ Анкета №{app_id} отклонена, но отправить причину пользователю не удалось. "
            "Проверь, что он запускал бота. Кнопки убраны."
        )

@dp.chat_join_request(F.chat.id == CHAT_ID)
async def on_join_request(event: ChatJoinRequest):
    user = event.from_user
    approved_app = store.latest_application(user.id)
    if approved_app and approved_app["status"] == "approved" and not store.is_banned(user.id):
        try:
            await bot.approve_chat_join_request(chat_id=event.chat.id, user_id=user.id)
            store.set_application_status(approved_app["id"], "joined")
            store.log_action("approved_applicant_joined", user.id, None, f"application_id={approved_app['id']}")
            try:
                await bot.send_message(user.id, "Добро пожаловать в Lavender Dream! 💜")
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning("Не удалось автоматически принять одобренную анкету: %s", exc)
        return
    if store.is_banned(user.id):
        try:
            await bot.decline_chat_join_request(chat_id=event.chat.id, user_id=user.id)
        except TelegramBadRequest:
            pass
        store.log_action("blacklisted_join_request_declined", user.id, None, "")
        return
    now = time.time()
    suspicious = detect_raid(user.id)
    previous = store.last_request_time(user.id)
    if previous is not None and now - previous < USER_REQUEST_COOLDOWN:
        suspicious = True
    store.set_last_request_time(user.id, now)
    request_id = store.add_request(
        chat_id=event.chat.id,
        user_id=user.id,
        username=user.username or "",
        full_name=" ".join(x for x in [user.first_name, user.last_name] if x),
        date=now,
        suspicious=suspicious,
    )
    store.log_action("request_received", user.id, None, f"request_id={request_id}; suspicious={int(suspicious)}")
    if suspicious:
        store.log_action("suspicious_request", user.id, None, f"request_id={request_id}")
    text = (
        "📨 <b>Новая заявка в Lavender Dream</b>\n\n"
        f"Пользователь: {user_label(user)}\n"
        f"Telegram ID: <code>{user.id}</code>\n"
        f"Заявка: <code>#{request_id}</code>\n"
        f"Подана: {datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
    )
    if suspicious:
        text += "\n⚠️ <b>Подозрительная активность:</b> заявка пришла во время возможного рейда или слишком быстро повторилась.\n"
    if store.is_banned(user.id):
        text += "\n🚫 <b>Пользователь находится в чёрном списке.</b> Одобрять заявку нельзя."
    keyboard = admin_keyboard(request_id, user.id, suspicious)
    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=keyboard)
            delivered += 1
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            log.warning("Не удалось отправить заявку администратору %s: %s", admin_id, exc)
    if delivered == 0:
        store.log_action("admin_notification_failed", user.id, None, f"request_id={request_id}")
        log.error("Ни одному администратору не удалось отправить заявку #%s. Проверьте, что администраторы открыли бота.", request_id)


@dp.callback_query(F.data == "noop")
async def noop_callback(call: CallbackQuery):
    await call.answer("Заявка помечена как подозрительная.", show_alert=True)


@dp.callback_query(F.data.startswith("approve:"))
async def approve_request(call: CallbackQuery):
    if not call.from_user or not await is_admin(call.from_user.id):
        await call.answer("Нет доступа: действие доступно только настроенным администраторам.", show_alert=True)
        return
    request_id = int(call.data.split(":")[1])
    req = store.get_request(request_id)
    if not req or req["status"] != "pending":
        await call.answer("Заявка уже обработана или не найдена.", show_alert=True)
        return
    if store.is_banned(req["user_id"]):
        await call.answer("Пользователь в чёрном списке. Сначала снимите блокировку.", show_alert=True)
        return
    try:
        await bot.approve_chat_join_request(chat_id=req["chat_id"], user_id=req["user_id"])
        store.set_request_status(request_id, "approved")
        store.log_action("request_approved", req["user_id"], call.from_user.id, f"request_id={request_id}")
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer(f"✅ Заявка #{request_id} одобрена администратором {user_label(call.from_user)}.")
        await call.answer("Заявка одобрена.")
    except TelegramBadRequest as exc:
        await call.answer("Telegram не смог одобрить заявку. Возможно, она уже истекла или обработана.", show_alert=True)
        store.log_action("approve_failed", req["user_id"], call.from_user.id, str(exc)[:300])


@dp.callback_query(F.data.startswith("reject:"))
async def reject_request(call: CallbackQuery):
    if not call.from_user or not await is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return
    request_id = int(call.data.split(":")[1])
    req = store.get_request(request_id)
    if not req or req["status"] != "pending":
        await call.answer("Заявка уже обработана или не найдена.", show_alert=True)
        return
    try:
        await bot.decline_chat_join_request(chat_id=req["chat_id"], user_id=req["user_id"])
        store.set_request_status(request_id, "rejected")
        store.log_action("request_rejected", req["user_id"], call.from_user.id, f"request_id={request_id}")
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer(f"❌ Заявка #{request_id} отклонена администратором {user_label(call.from_user)}.")
        await call.answer("Заявка отклонена.")
    except TelegramBadRequest as exc:
        await call.answer("Не удалось отклонить заявку: возможно, она уже обработана.", show_alert=True)
        store.log_action("reject_failed", req["user_id"], call.from_user.id, str(exc)[:300])


@dp.callback_query(F.data.startswith("ban:"))
async def ban_from_request(call: CallbackQuery):
    if not call.from_user or not await is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return
    _, user_id_raw, request_id_raw = call.data.split(":")
    user_id, request_id = int(user_id_raw), int(request_id_raw)
    req = store.get_request(request_id)
    if not req or req["status"] != "pending":
        await call.answer("Заявка уже обработана.", show_alert=True)
        return
    store.ban_user(user_id, call.from_user.id, "Добавлен из заявки")
    try:
        await bot.decline_chat_join_request(chat_id=req["chat_id"], user_id=user_id)
    except TelegramBadRequest:
        pass
    store.set_request_status(request_id, "blacklisted")
    store.log_action("user_blacklisted", user_id, call.from_user.id, f"request_id={request_id}")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(f"🚫 Пользователь <code>{user_id}</code> добавлен в чёрный список.")
    await call.answer("Добавлено в чёрный список.")


@dp.message(Command("stats"))
async def stats_cmd(message: Message):
    if not await is_admin(message.from_user.id):
        return
    s = store.stats()
    await message.answer(
        "<b>Статистика Lavender Dream</b>\n\n"
        "<b>Анкеты новых участников</b>\n"
        f"Всего отправлено: <b>{s['total']}</b>\n"
        f"Ожидают решения: <b>{s['pending']}</b>\n"
        f"Одобрено (включая вступивших): <b>{s['approved']}</b>\n"
        f"Отклонено: <b>{s['rejected']}</b>\n\n"
        "<b>Заявки на вступление в группу</b>\n"
        f"Всего: <b>{s['join_total']}</b>\n"
        f"Ожидают: <b>{s['join_pending']}</b>\n"
        f"Принято: <b>{s['join_approved']}</b>\n"
        f"Отклонено: <b>{s['join_rejected']}</b>\n\n"
        f"В чёрном списке: <b>{s['banned']}</b>\n"
        f"Подозрительных заявок: <b>{s['suspicious']}</b>"
    )


@dp.message(Command("ankety"), F.chat.type == "private")
async def pending_applications_cmd(message: Message):
    if not message.from_user or not await is_admin(message.from_user.id):
        return
    rows = store.pending_applications()
    if not rows:
        await message.answer("✅ Необработанных анкет нет.")
        return

    await message.answer(f"📋 Необработанных анкет: <b>{len(rows)}</b>")
    sent = 0
    for app in rows:
        caption = (
            f"📝 <b>Анкета Lavender Dream №{app['id']}</b>\n"
            f"Имя в Telegram: {esc(app['full_name'])}\n"
            f"Username аккаунта: @{esc(app['username']) if app['username'] else 'нет'}\n"
            f"Telegram ID: <code>{app['user_id']}</code>\n"
            f"1. Фото персонажа — приложено\n"
            f"2. Имя персонажа: {esc(app['character_name'] or 'не указано')}\n"
            f"3. Псевдоним: {esc(app['nickname'])}\n"
            f"4. Telegram username: {esc(app['telegram_username'])}\n\n"
            "Ожидает решения администрации."
        )
        try:
            card = await message.answer_photo(
                app["photo_file_id"],
                caption=caption,
                reply_markup=application_review_keyboard(app["id"]),
                parse_mode=ParseMode.HTML,
            )
            store.add_application_review_message(app["id"], message.chat.id, card.message_id, caption)
            sent += 1
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning("Не удалось показать анкету №%s через /ankety: %s", app["id"], exc)
    if sent < len(rows):
        await message.answer(f"Показано {sent} из {len(rows)} анкет. Ошибки отправки записаны в журнал.")

@dp.message(Command("pending"))
async def pending_cmd(message: Message):
    if not await is_admin(message.from_user.id):
        return
    rows = store.pending_requests(10)
    if not rows:
        await message.answer("Ожидающих заявок нет.")
        return
    for req in rows:
        text = (
            f"📨 <b>Заявка #{req['id']}</b>\n"
            f"Пользователь: {esc(req['full_name'])}\n"
            f"Username: @{esc(req['username']) if req['username'] else 'нет'}\n"
            f"ID: <code>{req['user_id']}</code>\n"
            f"Подозрительная: {'да' if req['suspicious'] else 'нет'}"
        )
        await message.answer(text, reply_markup=admin_keyboard(req["id"], req["user_id"], bool(req["suspicious"])))


@dp.message(Command("ban"))
async def ban_cmd(message: Message, command: CommandObject):
    if not await is_admin(message.from_user.id):
        return
    parts = (command.args or "").split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        await message.answer("Использование: /ban TELEGRAM_ID причина")
        return
    user_id = int(parts[0])
    reason = parts[1] if len(parts) > 1 else "Причина не указана"
    store.ban_user(user_id, message.from_user.id, reason)
    store.log_action("user_blacklisted", user_id, message.from_user.id, reason)
    await message.answer(f"🚫 Пользователь <code>{user_id}</code> добавлен в чёрный список.")


@dp.message(Command("unban"))
async def unban_cmd(message: Message, command: CommandObject):
    if not await is_admin(message.from_user.id):
        return
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("Использование: /unban TELEGRAM_ID")
        return
    user_id = int(raw)
    store.unban_user(user_id)
    store.log_action("user_unbanned", user_id, message.from_user.id, "")
    await message.answer(f"✅ Пользователь <code>{user_id}</code> удалён из чёрного списка.")


@dp.message(Command("blacklist"))
async def blacklist_cmd(message: Message):
    if not await is_admin(message.from_user.id):
        return
    rows = store.blacklist(30)
    if not rows:
        await message.answer("Чёрный список пуст.")
        return
    text = "<b>Чёрный список</b>\n" + "\n".join(
        f"• <code>{r['user_id']}</code> — {esc(r['reason'])}" for r in rows
    )
    await message.answer(text[:4000])


@dp.message(Command("logs"))
async def logs_cmd(message: Message):
    if not await is_admin(message.from_user.id):
        return
    rows = store.logs(15)
    if not rows:
        await message.answer("Журнал пока пуст.")
        return
    text = "<b>Последние действия</b>\n" + "\n".join(
        f"• {esc(r['created_at'])} — <code>{esc(r['action'])}</code>; user=<code>{r['user_id'] or '-'}</code>; admin=<code>{r['admin_id'] or '-'}</code>"
        for r in rows
    )
    await message.answer(text[:4000])


@dp.chat_member(F.chat.id == CHAT_ID)
async def on_member_update(event: ChatMemberUpdated):
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    if new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR) and old_status in (
        ChatMemberStatus.LEFT, ChatMemberStatus.KICKED
    ):
        user = event.new_chat_member.user
        store.log_action("member_joined", user.id, None, "")
        try:
            await bot.send_message(CHAT_ID, WELCOME_TEXT.replace("{name}", esc(user.first_name)))
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning("Не удалось отправить приветствие: %s", exc)


@dp.message(Command("help"))
async def help_cmd(message: Message):
    if message.from_user and await is_admin(message.from_user.id):
        await message.answer(
            "<b>Команды модератора Lavender Dream</b>\n\n"
            "/stats — статистика анкет и заявок\n"
            "/ankety — все необработанные анкеты участников\n"
            "/pending — ожидающие заявки на вступление\n"
            "/ban TELEGRAM_ID причина — добавить в чёрный список\n"
            "/unban TELEGRAM_ID — убрать из чёрного списка\n"
            "/blacklist — показать чёрный список\n"
            "/logs — последние действия\n"
            "/cancel — отменить ввод причины отклонения"
        )
    else:
        await message.answer("Привет! 💜 Используй /start, чтобы заполнить анкету, или /help для этой подсказки.")


async def configure_bot_commands():
    public_commands = [
        BotCommand(command="start", description="Начать заполнение анкеты"),
        BotCommand(command="help", description="Помощь"),
    ]
    admin_commands = public_commands + [
        BotCommand(command="stats", description="Статистика анкет и заявок"),
        BotCommand(command="ankety", description="Все необработанные анкеты"),
        BotCommand(command="pending", description="Ожидающие заявки на вступление"),
        BotCommand(command="ban", description="Добавить в чёрный список"),
        BotCommand(command="unban", description="Убрать из чёрного списка"),
        BotCommand(command="blacklist", description="Показать чёрный список"),
        BotCommand(command="logs", description="Последние действия"),
        BotCommand(command="cancel", description="Отменить ввод причины"),
    ]
    await bot.set_my_commands(public_commands, scope=BotCommandScopeAllPrivateChats())
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramBadRequest as exc:
            log.warning("Не удалось настроить меню команд для администратора %s: %s", admin_id, exc)


async def main():
    me = await bot.get_me()
    log.info("Запуск @%s для чата %s; администраторов: %s", me.username, CHAT_ID, len(ADMIN_IDS))
    try:
        await configure_bot_commands()
    except TelegramBadRequest as exc:
        log.warning("Не удалось настроить меню команд: %s", exc)
    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
