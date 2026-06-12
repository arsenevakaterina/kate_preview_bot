"""Adstail creative-upload bot — throwaway prototype.

Prototypes the creative-upload UX. No real backend, no file storage —
all state lives in memory (a dict keyed by user_id). Telegram constraints
are reproduced faithfully: media groups carry no inline keyboard, the draft
panel is a SEPARATE text message edited in place, albums are debounced.

Run:
    pip install aiogram
    BOT_TOKEN=... python bot.py
Optional: TARGET_CHAT_ID=... (where "upload" sends the post; default = same chat)
"""

import asyncio
import logging
import os
import re
import uuid
from collections import deque
from typing import Awaitable, Callable, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("creativebot")

# Unique per-process id — lets us tell in the logs whether two updates were
# handled by the SAME instance (Telegram double-delivery) or DIFFERENT instances
# (more than one poller running, which in-memory dedup can't fix).
INSTANCE = uuid.uuid4().hex[:8]

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    MenuButtonCommands,
    Message,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TARGET_CHAT_ID_RAW = os.environ.get("TARGET_CHAT_ID")
TARGET_CHAT_ID: Optional[int] = int(TARGET_CHAT_ID_RAW) if TARGET_CHAT_ID_RAW else None

MAX_MEDIA = 10
CAPTION_LIMIT = 1024
ALBUM_DEBOUNCE = 1.0  # seconds

# ---------------------------------------------------------------------------
# UI strings (all English, final — per spec)
# ---------------------------------------------------------------------------

S = {
    "start.intro": (
        "🤖 Бот для тестирования сценариев и сообщений.\n\n"
        "Выберите команду, запускающую флоу."
    ),
    "panel.title": '📤 New creative for kate_test_channel_mar26 (campaign "Ттт")',
    "hint.empty": (
        "Send photos, videos and text — together or one at a time.\n"
        "📎 Media is added in the order you send it.\n"
        "✏️ Sending new text replaces the current caption."
    ),
    "hint.need_text": "Add text — it will become the caption.",
    "hint.need_media": "Now send a photo or video.",
    "hint.ready": "Ready to upload.",
    "status.text_done": "✅ Text added",
    "status.text_todo": "❗️ Add text (required)",
    "status.media_done": "✅ Media added · {n}/10",
    "status.media_todo": "⬜️ Add media (optional)",
    "btn.confirm": "✅ Confirm and upload",
    "link.none": (
        "⚠️ No link found in your text.\n"
        "Your ad will be uploaded without a target link. Continue?"
    ),
    "link.one": "🎯 This link will be your ad's target link:",
    "link.choose": "🔗 Several links found — pick the one to use as the ad's target link:",
    "link.picked": "🎯 This link will be your ad's target link:",
    "btn.upload_nolink": "⬆️ Upload without a link",
    "btn.upload": "⬆️ Confirm & upload",
    "btn.back": "↩️ Back to editing",
    "detail.target": "🎯 target link: {url}",
    "detail.no_target": "🎯 no target link",
    "btn.preview": "👁 Show preview",
    "btn.restart": "↻ Start over",
    "btn.cancel": "✖️ Cancel",
    "btn.new": "➕ New creative",
    "preview.header": "☝️ This is exactly how the post will look.",
    "err.too_long": "Text is too long for a media caption (max 1024 chars). Please shorten it.",
    "err.album_full": 'Album is full — 10 is Telegram\'s max. Use "Start over" to change the set.',
    "err.unsupported": "Only photos and videos are accepted here.",
    "success": "✅ Done! Creative uploaded to kate_test_channel_mar26.",
    "canceled": "Upload canceled.",
}

# ---------------------------------------------------------------------------
# State — per-user, in memory
# ---------------------------------------------------------------------------

# user_id -> draft dict
drafts: dict[int, dict] = {}


def new_draft() -> dict:
    return {
        "media": [],            # list[{type, file_id}], max 10
        "text": None,           # caption
        "panel_msg_id": None,   # the draft panel message we edit in place
        "preview_msg_ids": [],  # ids of last preview album (to delete)
        "album_buffer": None,   # {media_group_id, items, task}
        "stage": "composing",   # composing | done | canceled
        "pending_links": [],    # links found in text at the upload gate
        "target_link": None,    # the chosen target link (or None = no link)
    }


def get_draft(user_id: int) -> dict:
    d = drafts.get(user_id)
    if d is None:
        d = new_draft()
        drafts[user_id] = d
    return d


def is_ready(d: dict) -> bool:
    # Text alone is enough to upload; media is optional.
    return d["text"] is not None and len(d["text"]) <= CAPTION_LIMIT


# ---------------------------------------------------------------------------
# Panel rendering
# ---------------------------------------------------------------------------

def panel_text(d: dict, header: Optional[str] = None) -> str:
    n = len(d["media"])
    has_text = d["text"] is not None

    # Persistent two-line checklist at the very top — always shows what's done
    # and what can still be added, so the user never has to guess. After sending
    # only photos, the text line stays visible as a call to action. Text is
    # required to upload; media is optional.
    text_line = S["status.text_done"] if has_text else S["status.text_todo"]
    media_line = S["status.media_done"].format(n=n) if n else S["status.media_todo"]
    status = f"{text_line}\n{media_line}"

    if has_text:
        hint = S["hint.ready"]
    elif n >= 1:
        hint = S["hint.need_text"]
    else:
        hint = S["hint.empty"]

    title = S["panel.title"]
    body = f"{status}\n\n{title}\n\n{hint}"
    if header:
        body = f"{header}\n\n{body}"
    return body


def panel_keyboard(d: dict) -> InlineKeyboardMarkup:
    rows = []
    if is_ready(d):
        rows.append([InlineKeyboardButton(text=S["btn.confirm"], callback_data="confirm")])
    if len(d["media"]) >= 1 or d["text"] is not None:
        rows.append([InlineKeyboardButton(text=S["btn.preview"], callback_data="preview")])
    if len(d["media"]) >= 1 or d["text"] is not None:
        rows.append([InlineKeyboardButton(text=S["btn.restart"], callback_data="restart")])
    rows.append([InlineKeyboardButton(text=S["btn.cancel"], callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def edit_panel(bot: Bot, chat_id: int, d: dict, header: Optional[str] = None) -> None:
    """Edit the existing panel in place. Never sends a new panel."""
    if d["panel_msg_id"] is None:
        return
    try:
        await bot.edit_message_text(
            text=panel_text(d, header),
            chat_id=chat_id,
            message_id=d["panel_msg_id"],
            reply_markup=panel_keyboard(d),
        )
    except TelegramBadRequest:
        # "message is not modified" or similar — safe to ignore for a prototype.
        pass


async def send_panel(bot: Bot, chat_id: int, d: dict, header: Optional[str] = None) -> None:
    """Send a fresh panel and store its id (entry, or after a preview album)."""
    msg = await bot.send_message(
        chat_id=chat_id,
        text=panel_text(d, header),
        reply_markup=panel_keyboard(d),
    )
    d["panel_msg_id"] = msg.message_id


def apply_caption(d: dict, caption: Optional[str]) -> bool:
    """Process a media caption exactly like a text message: set it as the draft
    caption. The panel renders the resulting state itself, so this only reports
    whether the caption was rejected: returns True if it is too long (caller
    should warn; the draft text is left unchanged), False otherwise.
    """
    if caption is None:
        return False
    if len(caption) > CAPTION_LIMIT:
        return True
    d["text"] = caption
    return False


async def refresh_panel(bot: Bot, chat_id: int, d: dict, header: Optional[str] = None) -> None:
    """React to a user *message* with a fresh panel below it, then drop the old one.

    Editing the panel in place (edit_panel) is invisible to users: their message
    gets no apparent reply and the panel silently mutates somewhere above. So for
    every incoming message we send a NEW panel (a clear reply with buttons) and
    delete the previous one, keeping exactly ONE interactive panel alive.
    """
    # A shown preview no longer matches the draft the moment the user adds
    # anything, so drop it right away instead of waiting for the next preview.
    await delete_preview(bot, chat_id, d)
    old_panel_id = d["panel_msg_id"]
    await send_panel(bot, chat_id, d, header)
    if old_panel_id is not None and old_panel_id != d["panel_msg_id"]:
        try:
            await bot.delete_message(chat_id, old_panel_id)
        except TelegramBadRequest:
            pass


# ---------------------------------------------------------------------------
# Media helpers
# ---------------------------------------------------------------------------

def media_group_with_caption(media: list[dict], caption: Optional[str]) -> list:
    out = []
    for i, item in enumerate(media):
        cap = caption if i == 0 else None
        if item["type"] == "photo":
            out.append(InputMediaPhoto(media=item["file_id"], caption=cap))
        else:
            out.append(InputMediaVideo(media=item["file_id"], caption=cap))
    return out


async def delete_preview(bot: Bot, chat_id: int, d: dict) -> None:
    for mid in d["preview_msg_ids"]:
        try:
            await bot.delete_message(chat_id, mid)
        except TelegramBadRequest:
            pass
    d["preview_msg_ids"] = []


# ---------------------------------------------------------------------------
# Target-link gate — the user must explicitly accept the target link (or its
# absence) before the final upload.
# ---------------------------------------------------------------------------

# http(s):// or www. URLs; stop at whitespace/quotes/brackets.
_URL_RE = re.compile(r'(?:https?://|www\.)[^\s<>"\')]+', re.IGNORECASE)


def extract_links(text: Optional[str]) -> list[str]:
    """Pull candidate links out of the caption text, de-duplicated, in order.

    Plain-text scan (the draft keeps no entities) — good enough for visible
    URLs; hidden text_link hyperlinks are out of scope for this prototype.
    """
    if not text:
        return []
    out: list[str] = []
    for raw in _URL_RE.findall(text):
        url = raw.rstrip('.,;:!?)»"\'')
        if url and url not in out:
            out.append(url)
    return out


def _short_url(url: str, limit: int = 45) -> str:
    return url if len(url) <= limit else url[: limit - 1] + "…"


async def _edit_gate(bot: Bot, chat_id: int, d: dict, text: str, kb: InlineKeyboardMarkup) -> None:
    """Render an arbitrary text+keyboard onto the existing panel in place."""
    if d["panel_msg_id"] is None:
        return
    try:
        await bot.edit_message_text(
            text=text, chat_id=chat_id, message_id=d["panel_msg_id"], reply_markup=kb
        )
    except TelegramBadRequest:
        pass


def _confirm_kb(upload_label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=upload_label, callback_data="upload")],
        [InlineKeyboardButton(text=S["btn.back"], callback_data="back")],
    ])


async def show_link_gate(bot: Bot, chat_id: int, d: dict) -> None:
    """Tapping 'Confirm and upload' lands here: force an explicit decision about
    the target link before the real upload runs."""
    links = extract_links(d["text"])
    d["pending_links"] = links

    if len(links) == 0:
        d["target_link"] = None
        await _edit_gate(bot, chat_id, d, S["link.none"], _confirm_kb(S["btn.upload_nolink"]))
    elif len(links) == 1:
        d["target_link"] = links[0]
        text = f"{S['link.one']}\n{links[0]}"
        await _edit_gate(bot, chat_id, d, text, _confirm_kb(S["btn.upload"]))
    else:
        d["target_link"] = None
        rows = [
            [InlineKeyboardButton(text=_short_url(u), callback_data=f"pick:{i}")]
            for i, u in enumerate(links)
        ]
        rows.append([InlineKeyboardButton(text=S["btn.back"], callback_data="back")])
        await _edit_gate(bot, chat_id, d, S["link.choose"], InlineKeyboardMarkup(inline_keyboard=rows))


# ---------------------------------------------------------------------------
# Bot / Dispatcher
# ---------------------------------------------------------------------------

dp = Dispatcher()

# Telegram can deliver the same callback_query more than once. Remember recently
# seen callback ids and drop repeats so one tap never fires a handler twice.
_seen_cb_ids: deque[str] = deque(maxlen=256)


@dp.callback_query.outer_middleware
async def dedup_callbacks(
    handler: Callable[[CallbackQuery, dict], Awaitable],
    event: CallbackQuery,
    data: dict,
):
    if event.id in _seen_cb_ids:
        log.info("DUP dropped: instance=%s id=%s data=%s", INSTANCE, event.id, event.data)
        await event.answer()
        return None
    _seen_cb_ids.append(event.id)
    return await handler(event, data)


@dp.message(CommandStart())
async def on_start(message: Message, bot: Bot) -> None:
    await message.answer(S["start.intro"])


@dp.message(Command("creative"))
async def on_creative(message: Message, bot: Bot) -> None:
    d = new_draft()
    drafts[message.from_user.id] = d
    await send_panel(bot, message.chat.id, d)


# --- album debounce -------------------------------------------------------

async def flush_album(bot: Bot, chat_id: int, user_id: int) -> None:
    """Fires after debounce window; appends all buffered album items at once."""
    await asyncio.sleep(ALBUM_DEBOUNCE)
    d = get_draft(user_id)
    buf = d["album_buffer"]
    if not buf:
        return
    items = buf["items"]
    caption = buf.get("caption")
    d["album_buffer"] = None

    # Apply the album's caption as the draft text, same as a plain text message.
    if apply_caption(d, caption):
        try:
            await bot.send_message(chat_id, S["err.too_long"])
        except TelegramBadRequest:
            pass

    overflowed = False
    for item in items:
        if len(d["media"]) >= MAX_MEDIA:
            overflowed = True
            break
        d["media"].append(item)

    await refresh_panel(bot, chat_id, d)
    if overflowed:
        # Best-effort notice; album messages have no callback to answer, so send text.
        try:
            await bot.send_message(chat_id, S["err.album_full"])
        except TelegramBadRequest:
            pass


@dp.message(F.media_group_id, F.photo | F.video)
async def on_album_item(message: Message, bot: Bot) -> None:
    d = get_draft(message.from_user.id)
    if d["stage"] != "composing":
        return

    if message.photo:
        item = {"type": "photo", "file_id": message.photo[-1].file_id}
    else:
        item = {"type": "video", "file_id": message.video.file_id}

    # An album's caption rides on one of its items (Telegram puts it on the
    # first); capture whichever item carries it so flush can apply it as text.
    buf = d["album_buffer"]
    if buf and buf["media_group_id"] == message.media_group_id:
        buf["items"].append(item)
        if message.caption is not None:
            buf["caption"] = message.caption
        # restart debounce
        buf["task"].cancel()
    else:
        buf = {
            "media_group_id": message.media_group_id,
            "items": [item],
            "caption": message.caption,
            "task": None,
        }
        d["album_buffer"] = buf

    buf["task"] = asyncio.create_task(
        flush_album(bot, message.chat.id, message.from_user.id)
    )


@dp.message(F.photo | F.video)
async def on_single_media(message: Message, bot: Bot) -> None:
    d = get_draft(message.from_user.id)
    if d["stage"] != "composing":
        return

    if message.photo:
        item = {"type": "photo", "file_id": message.photo[-1].file_id}
    else:
        item = {"type": "video", "file_id": message.video.file_id}

    # The caption travels with the media — process it just like a text message.
    too_long = apply_caption(d, message.caption)
    if too_long:
        await message.answer(S["err.too_long"])
    text_set = message.caption is not None and not too_long

    media_added = False
    if len(d["media"]) >= MAX_MEDIA:
        await message.answer(S["err.album_full"])
    else:
        d["media"].append(item)
        media_added = True

    # Nothing landed (album full, no usable caption) → error already sent, no panel.
    if not media_added and not text_set:
        return
    await refresh_panel(bot, message.chat.id, d)


@dp.message(F.text)
async def on_text(message: Message, bot: Bot) -> None:
    d = get_draft(message.from_user.id)
    if d["stage"] != "composing":
        return

    if apply_caption(d, message.text):
        await message.answer(S["err.too_long"])
        return
    await refresh_panel(bot, message.chat.id, d)


@dp.message()
async def on_unsupported(message: Message, bot: Bot) -> None:
    d = get_draft(message.from_user.id)
    if d["stage"] != "composing":
        return
    await message.answer(S["err.unsupported"])


# --- callbacks ------------------------------------------------------------

@dp.callback_query(F.data == "preview")
async def cb_preview(cq: CallbackQuery, bot: Bot) -> None:
    d = get_draft(cq.from_user.id)
    await cq.answer()
    if d["stage"] != "composing" or (len(d["media"]) < 1 and d["text"] is None):
        return

    chat_id = cq.message.chat.id
    old_panel_id = d["panel_msg_id"]
    await delete_preview(bot, chat_id, d)

    if d["media"]:
        sent = await bot.send_media_group(
            chat_id=chat_id,
            media=media_group_with_caption(d["media"], d["text"]),
        )
        d["preview_msg_ids"] = [m.message_id for m in sent]
    else:
        # Text-only preview: just the plain message, exactly as it would post.
        sent = await bot.send_message(chat_id, d["text"])
        d["preview_msg_ids"] = [sent.message_id]

    # The old panel is now scrolled above the preview — send a FRESH panel below
    # and point panel_msg_id at it, then remove the old panel so only ONE
    # interactive panel ever exists (stale panels would pile up duplicate previews).
    await send_panel(bot, chat_id, d, header=S["preview.header"])
    if old_panel_id is not None:
        try:
            await bot.delete_message(chat_id, old_panel_id)
        except TelegramBadRequest:
            pass


@dp.callback_query(F.data == "confirm")
async def cb_confirm(cq: CallbackQuery, bot: Bot) -> None:
    # Step 1 of upload: don't post yet — make the user accept the target link.
    d = get_draft(cq.from_user.id)
    await cq.answer()
    if not is_ready(d) or d["stage"] != "composing":
        return
    await show_link_gate(bot, cq.message.chat.id, d)


@dp.callback_query(F.data.startswith("pick:"))
async def cb_pick(cq: CallbackQuery, bot: Bot) -> None:
    # User chose which of several links becomes the target → confirm that one.
    d = get_draft(cq.from_user.id)
    await cq.answer()
    if d["stage"] != "composing":
        return
    try:
        idx = int(cq.data.split(":", 1)[1])
    except ValueError:
        return
    links = d.get("pending_links") or []
    if not (0 <= idx < len(links)):
        return
    d["target_link"] = links[idx]
    text = f"{S['link.picked']}\n{links[idx]}"
    await _edit_gate(bot, cq.message.chat.id, d, text, _confirm_kb(S["btn.upload"]))


@dp.callback_query(F.data == "back")
async def cb_back(cq: CallbackQuery, bot: Bot) -> None:
    # Leave the link gate, back to the editing panel.
    d = get_draft(cq.from_user.id)
    await cq.answer()
    if d["stage"] != "composing":
        return
    d["pending_links"] = []
    d["target_link"] = None
    await edit_panel(bot, cq.message.chat.id, d)


@dp.callback_query(F.data == "upload")
async def cb_upload(cq: CallbackQuery, bot: Bot) -> None:
    # Step 2 of upload: the target link is accepted — post for real.
    d = get_draft(cq.from_user.id)
    await cq.answer()
    if not is_ready(d) or d["stage"] != "composing":
        return

    chat_id = cq.message.chat.id
    target = TARGET_CHAT_ID if TARGET_CHAT_ID is not None else chat_id
    if d["media"]:
        await bot.send_media_group(
            chat_id=target,
            media=media_group_with_caption(d["media"], d["text"]),
        )
    else:
        # Text-only creative: post as a plain message.
        await bot.send_message(chat_id=target, text=d["text"])

    d["stage"] = "done"
    n = len(d["media"])
    media_detail = f"{n} media · caption attached" if n else "text only"
    link_detail = (
        S["detail.target"].format(url=d["target_link"])
        if d["target_link"]
        else S["detail.no_target"]
    )
    success_body = f"{S['success']}\n\n{media_detail}\n{link_detail}"
    try:
        await bot.edit_message_text(
            text=success_body,
            chat_id=chat_id,
            message_id=d["panel_msg_id"],
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data == "restart")
async def cb_restart(cq: CallbackQuery, bot: Bot) -> None:
    d = get_draft(cq.from_user.id)
    await cq.answer()
    chat_id = cq.message.chat.id
    await delete_preview(bot, chat_id, d)
    d["media"] = []
    d["text"] = None
    d["album_buffer"] = None
    d["pending_links"] = []
    d["target_link"] = None
    await edit_panel(bot, chat_id, d)


@dp.callback_query(F.data == "cancel")
async def cb_cancel(cq: CallbackQuery, bot: Bot) -> None:
    d = get_draft(cq.from_user.id)
    await cq.answer()
    chat_id = cq.message.chat.id
    await delete_preview(bot, chat_id, d)
    d["stage"] = "canceled"
    try:
        await bot.edit_message_text(
            text=S["canceled"],
            chat_id=chat_id,
            message_id=d["panel_msg_id"],
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=S["btn.new"], callback_data="new")]]
            ),
        )
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data == "new")
async def cb_new(cq: CallbackQuery, bot: Bot) -> None:
    await cq.answer()
    d = new_draft()
    drafts[cq.from_user.id] = d
    await send_panel(bot, cq.message.chat.id, d)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env var is required. Run: BOT_TOKEN=... python bot.py")
    log.info("BOOT instance=%s starting polling", INSTANCE)
    bot = Bot(BOT_TOKEN)
    await bot.set_my_commands(
        [
            BotCommand(command="creative", description="тест флоу добавления креатива"),
        ]
    )
    # Make the chat "Menu" button open the commands list.
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    # Drop any backlog so a previously-conflicting poller can't replay updates.
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
