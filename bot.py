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
        "Доступные флоу:\n"
        "📤 /creative — собрать и загрузить креатив в канал.\n"
        "🔮 /price_predict — предсказать цену размещения в любом TG-канале.\n\n"
        "Выберите команду, запускающую флоу."
    ),
    "panel.title": '📤 New creative for kate_test_channel_mar26 (campaign "Ттт")',
    "hint.edit": (
        "This draft stays open:\n"
        "✏️ Send new text to replace the caption.\n"
        "📎 Send photos or videos to add more media."
    ),
    "hint.ready": "✅ Confirm and upload when ready.",
    "status.text_done": "✅ Text added",
    "status.text_todo": "❗️ Add text (required)",
    "status.media_done": "✅ Media added · {n}/10",
    "status.media_todo": "⬜️ Add media (optional) · {n}/10",
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
    media_line = (S["status.media_done"] if n else S["status.media_todo"]).format(n=n)
    status = f"{text_line}\n{media_line}"

    title = S["panel.title"]
    # The editing reminder is shown in EVERY state. Users were not realising the
    # draft stays live after the first message — the panel read like a final
    # "Ready to upload" screen — so the "keep sending to change it" guidance must
    # never disappear. The readiness line is added on top only when ready.
    parts = [status, title, S["hint.edit"]]
    if has_text:
        parts.append(S["hint.ready"])
    body = "\n\n".join(parts)
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
# Price-predict flow (/price_predict) — a separate conversational flow that
# estimates the ad-placement price for any public Telegram channel.
#
# Telegram forbids a hyphen in command names, so the command is `price_predict`
# even though the feature is colloquially "price-predict".
# ---------------------------------------------------------------------------

# Channels we already have first-hand data for. Seeded with one demo channel so
# there's always a link that returns rich data even when Telegram can't be
# reached. The user never learns whether a channel is "known" to us or not — the
# bot is meant to look like it knows every channel.
KNOWN_CHANNELS: dict[str, dict] = {
    "adstail_demo": {
        "title": "Adstail Demo Channel",
        "username": "adstail_demo",
        "category": "Crypto & Web3",
        "region": "Worldwide",
        "geo": "Worldwide",
        "subscribers": 48_000,
        "bio": "Daily Web3 alpha, on-chain signals and market recaps. Ads: @adstail",
        "cpm_usd": 4.5,
    },
}

PP_CATEGORIES = [
    "News & Politics", "Crypto & Web3", "Tech & IT", "Business & Finance",
    "Entertainment", "Lifestyle", "Education", "Other",
]

# Geo is asked in two light taps: region first, then a country inside it. The
# regions cover the WHOLE world, so every channel fits somewhere. Inside a region
# the user can pick a top market, "All of {region}" (region-wide), or "Other
# country" — the catch-all for smaller markets, priced at the region rate. That
# keeps any country reachable without listing all ~200 of them.
PP_REGIONS = [
    "Europe", "CIS", "North America", "Latin America", "MENA",
    "Africa", "South Asia", "Asia-Pacific", "Worldwide",
]
# NOTE: keep each list an ODD length. The "Other country in {region}" button is
# appended as the final cell of the 2-column grid, so an odd country count makes
# it land in the bottom-RIGHT slot (countries + Other = even → full rows).
PP_COUNTRIES_BY_REGION = {
    "Europe": ["UK", "Germany", "France", "Italy", "Spain", "Poland", "Netherlands"],
    "CIS": ["Russia", "Ukraine", "Kazakhstan", "Belarus", "Uzbekistan", "Azerbaijan", "Georgia"],
    "North America": ["USA", "Canada", "Mexico"],
    "Latin America": ["Brazil", "Argentina", "Colombia", "Chile", "Peru"],
    "MENA": ["UAE", "Saudi Arabia", "Egypt", "Israel", "Turkey", "Qatar", "Morocco"],
    "Africa": ["Nigeria", "South Africa", "Kenya", "Ghana", "Ethiopia"],
    "South Asia": ["India", "Pakistan", "Bangladesh", "Sri Lanka", "Nepal"],
    "Asia-Pacific": ["China", "Japan", "Indonesia", "Vietnam", "Philippines",
                     "South Korea", "Australia"],
    # "Worldwide" has no country breakdown — it's the region-wide answer itself.
}
PP_FORMATS = ["1/24h", "1/48h", "1/7d", "1/30d"]

# Toy price model (the real backend doesn't exist yet).
_FORMAT_MULT = {"1/24h": 1.0, "1/48h": 1.7, "1/7d": 3.8, "1/30d": 9.0}
_CATEGORY_MULT = {
    "News & Politics": 1.0, "Crypto & Web3": 1.8, "Tech & IT": 1.4,
    "Business & Finance": 1.6, "Entertainment": 0.9, "Lifestyle": 1.0,
    "Education": 1.1, "Other": 1.0,
}
# Geo multiplier: a specific known country overrides its region; "All of region"
# and "Other country" both fall back to the region rate.
_REGION_MULT = {
    "Europe": 1.4, "CIS": 0.8, "North America": 1.9, "Latin America": 0.8,
    "MENA": 1.4, "Africa": 0.6, "South Asia": 0.6, "Asia-Pacific": 1.0,
    "Worldwide": 1.1,
}
_COUNTRY_MULT = {
    "USA": 1.9, "UK": 1.6, "Germany": 1.5, "France": 1.4, "Sweden": 1.5,
    "UAE": 1.7, "Saudi Arabia": 1.5, "Qatar": 1.7, "Israel": 1.5,
    "Japan": 1.6, "South Korea": 1.5, "Australia": 1.7, "China": 1.0,
    "Russia": 0.85, "India": 0.6, "Brazil": 0.75, "Mexico": 0.8,
}


def _geo_mult(geo: str, region: Optional[str] = None) -> float:
    # Known country rate first; otherwise the region rate (covers "All of
    # {region}" and "Other country"); otherwise neutral.
    return _COUNTRY_MULT.get(geo) or _REGION_MULT.get(region) or _REGION_MULT.get(geo) or 1.0


PPS = {
    "intro": (
        "💸 Ever wondered what an ad in a Telegram channel actually costs?\n\n"
        "Drop me a channel link — I'll size up its audience and tell you what a "
        "post there is worth."
    ),
    "ask_link": "🔗 Send a link to the channel (e.g. <code>https://t.me/durov</code> or <code>@durov</code>).",
    "bad_link": (
        "🚫 That doesn't look like a Telegram channel link.\n"
        "Send something like <code>https://t.me/durov</code> or <code>@durov</code>."
    ),
    "not_in_tg": (
        "🔍 I couldn't find <code>@{u}</code> on Telegram.\n"
        "Double-check the link and try again."
    ),
    "send_link_hint": "Please paste a channel link first (e.g. <code>https://t.me/durov</code>).",
    "use_buttons": "Please use the buttons above to answer 🙂",
    "cta_owner": (
        "👑 This is your channel?\n"
        "Connect {title} on adstail.com to manage placements and earn from your "
        "audience on autopilot."
    ),
    "cta_adv": (
        "📣 Want to advertise here?\n"
        "Buy a placement in {title} — and thousands of other channels — on adstail.com."
    ),
}

# Each PANEL is its own independent flow, so two pasted links don't share state.
# panel_msg_id -> session dict. A callback finds its session by the message its
# button is attached to, so tapping an older panel records onto THAT channel.
pp_sessions: dict[int, dict] = {}

# Users currently in the price-predict flow (entered via /price_predict). While a
# user is here, their free text is treated as channel links and the creative
# handlers stand aside. Cleared when they switch to /creative.
pp_mode_users: set[int] = set()


def new_predict_session(user_id: int) -> dict:
    return {
        "user_id": user_id,
        "step": "await_category",   # await_category | await_region | await_country
                                    # | await_format | await_role | done
        "url": None,
        "username": None,
        "channel_title": None,
        "bio": None,
        "subscribers": None,
        "db_record": None,      # internal only — never surfaced to the user
        "category": None,
        "region": None,
        "geo": None,            # the chosen geo label (a country or a region)
        "fmt": None,
        "role": None,           # owner | advertiser
        "panel_msg_id": None,
    }


def _pp_active(user_id: int) -> bool:
    """True while the user is in the price-predict flow (creative handlers defer)."""
    return user_id in pp_mode_users


def pp_session_for(cq: CallbackQuery) -> Optional[dict]:
    """The session a callback belongs to — keyed by the panel the button is on,
    so each panel acts on its own channel even when several are in the chat."""
    s = pp_sessions.get(cq.message.message_id)
    if s is None or s["user_id"] != cq.from_user.id:
        return None
    return s


# --- link parsing & validation -------------------------------------------

# Public-username links: t.me/<name>, telegram.me/<name>, with optional scheme.
_TG_LINK_RE = re.compile(
    r'(?:https?://)?(?:www\.)?t(?:elegram)?\.me/(@?[A-Za-z0-9_]{3,32})', re.IGNORECASE
)
_TG_AT_RE = re.compile(r'^@([A-Za-z0-9_]{3,32})$')
# Slugs that look like usernames but aren't channels (invite/share endpoints).
_TG_RESERVED = {"joinchat", "s", "c", "addstickers", "share", "proxy"}


def parse_tg_username(text: Optional[str]) -> Optional[str]:
    """Extract a public-channel username (lowercased, no @) from user text."""
    if not text:
        return None
    text = text.strip()
    m = _TG_AT_RE.match(text)
    if m:
        return m.group(1).lower()
    m = _TG_LINK_RE.search(text)
    if m:
        username = m.group(1).lstrip("@").lower()
        if username in _TG_RESERVED:
            return None
        return username
    return None


async def pp_fetch_channel(bot: Bot, username: str) -> Optional[dict]:
    """Pull public channel data from Telegram: title, bio/description, subscriber
    count. Returns None if Telegram can't resolve the channel at all.

    All of this is public for open channels. Some fields can still be missing
    (e.g. member count is hidden) — callers handle None per field.
    """
    try:
        chat = await bot.get_chat(f"@{username}")
    except Exception:  # noqa: BLE001 — network/notfound/forbidden all mean "no"
        return None
    title = chat.title or chat.full_name or f"@{username}"
    bio = getattr(chat, "description", None) or getattr(chat, "bio", None)
    try:
        subs = await bot.get_chat_member_count(chat.id)
    except Exception:  # noqa: BLE001 — count is often hidden; not fatal
        subs = None
    return {"title": title, "bio": bio, "subscribers": subs}


def predict_price(category: str, geo: str, region: Optional[str], fmt: str,
                  subscribers: Optional[int], cpm: Optional[float] = None):
    """Toy price estimator. Returns (low, high) in USD."""
    fmt_mult = _FORMAT_MULT.get(fmt, 1.0)
    cat_mult = _CATEGORY_MULT.get(category, 1.0)
    geo_mult = _geo_mult(geo, region)
    if subscribers:
        base = subscribers / 1000 * (cpm or 3.0)   # CPM-style base for a 1/24h post
    else:
        base = 60.0                                  # baseline if size is hidden
    price = base * fmt_mult * cat_mult * geo_mult
    low = int(round(price * 0.80 / 5) * 5)
    high = int(round(price * 1.25 / 5) * 5)
    return low, high


# --- panel rendering ------------------------------------------------------

def _pp_back_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="↩️ Back", callback_data="pp:back")]


def _pp_grid_kb(items: list[str], prefix: str, per_row: int = 2, back: bool = False) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, label in enumerate(items):
        row.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}{i}"))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if back:
        rows.append(_pp_back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pp_country_kb(region: str, back: bool = False) -> InlineKeyboardMarkup:
    """Countries inside a region in a 2-column grid. "Other country in {region}"
    is the final grid cell — with an odd country count it lands bottom-right.
    "All of {region}" stays a full-width row below the grid."""
    countries = PP_COUNTRIES_BY_REGION.get(region, [])
    rows, row = [], []
    for i, c in enumerate(countries):
        row.append(InlineKeyboardButton(text=c, callback_data=f"pp:country:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    # Append "Other" as the next grid cell (not a full-width row).
    row.append(InlineKeyboardButton(text=f"Other country in {region}", callback_data="pp:country:other"))
    if len(row) == 2:
        rows.append(row)
        row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=f"🌐 All of {region}", callback_data="pp:country:all")])
    if back:
        rows.append(_pp_back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pp_role_kb(back: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👑 I run this channel", callback_data="pp:role:owner")],
        [InlineKeyboardButton(text="📣 I'm looking to advertise", callback_data="pp:role:adv")],
    ]
    if back:
        rows.append(_pp_back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _clip(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pp_header(s: dict) -> str:
    """Channel snapshot shown above every question — public title/bio/audience.

    Deliberately says nothing about whether we 'know' the channel; the bot
    presents the same confident snapshot for every channel.
    """
    lines = [f"📡 {s['channel_title']}  (@{s['username']})"]
    if s.get("subscribers"):
        lines.append(f"👥 {s['subscribers']:,} subscribers")
    if s.get("bio"):
        lines.append(f"ℹ️ {_clip(s['bio'])}")
    facts = []
    if s.get("category"):
        facts.append(f"🏷 {s['category']}")
    if s.get("geo"):
        facts.append(f"🌍 {s['geo']}")
    if facts:
        lines.append(" · ".join(facts))
    return "\n".join(lines)


def pp_category_q(s: dict) -> str:
    return pp_header(s) + "\n\nWhat's this channel about? Pick a category:"


def pp_region_q(s: dict) -> str:
    return pp_header(s) + "\n\nWhere's the audience? Pick a region:"


def pp_country_q(s: dict) -> str:
    return pp_header(s) + ("\n\nPick the country — or keep the whole region. "
                           "Smaller markets go under “Other country”:")


def pp_format_q(s: dict) -> str:
    return pp_header(s) + "\n\nHow long should the post stay up? Pick a placement format:"


def pp_role_q(s: dict) -> str:
    return pp_header(s) + "\n\nLast one — what brings you here?"


async def pp_send_panel(bot: Bot, chat_id: int, s: dict, text: str,
                        kb: Optional[InlineKeyboardMarkup], parse_mode: Optional[str] = None) -> None:
    msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=parse_mode)
    s["panel_msg_id"] = msg.message_id


async def pp_edit_panel(bot: Bot, chat_id: int, s: dict, text: str,
                        kb: Optional[InlineKeyboardMarkup], parse_mode: Optional[str] = None) -> None:
    if s.get("panel_msg_id") is None:
        await pp_send_panel(bot, chat_id, s, text, kb, parse_mode)
        return
    try:
        await bot.edit_message_text(
            text=text, chat_id=chat_id, message_id=s["panel_msg_id"],
            reply_markup=kb, parse_mode=parse_mode,
        )
    except TelegramBadRequest:
        pass


# --- step rendering & navigation -----------------------------------------
# Each panel renders one channel's flow. A step carries Back only when there's a
# previous QUESTION to return to within this same panel (you switch channels by
# pasting a new link, which opens a separate panel — never by going "back").

def _pp_has_back(s: dict) -> bool:
    step = s["step"]
    if step in ("await_region", "await_country", "await_role"):
        return True
    if step == "await_format":
        return not s.get("db_record")   # known channel: format is the first step
    return False                        # await_category (and anything else)


def pp_step_view(s: dict) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """(text, keyboard) for the session's current step."""
    step = s["step"]
    back = _pp_has_back(s)
    if step == "await_category":
        return pp_category_q(s), _pp_grid_kb(PP_CATEGORIES, "pp:cat:", back=back)
    if step == "await_region":
        return pp_region_q(s), _pp_grid_kb(PP_REGIONS, "pp:region:", back=back)
    if step == "await_country":
        return pp_country_q(s), pp_country_kb(s.get("region"), back=back)
    if step == "await_format":
        return pp_format_q(s), _pp_grid_kb(PP_FORMATS, "pp:fmt:", back=back)
    if step == "await_role":
        return pp_role_q(s), pp_role_kb(back=back)
    return pp_category_q(s), _pp_grid_kb(PP_CATEGORIES, "pp:cat:")


def pp_prev_step(s: dict) -> Optional[str]:
    """Where Back goes from the current step (None if there's no previous step)."""
    step = s["step"]
    if step == "await_region":
        return "await_category"
    if step == "await_country":
        return "await_region"
    if step == "await_format":
        if s.get("db_record"):
            return None                  # known channel: format is the first step
        region = s.get("region")
        if region and PP_COUNTRIES_BY_REGION.get(region):
            return "await_country"
        return "await_region"            # region had no country breakdown
    if step == "await_role":
        return "await_format"
    return None


async def pp_send_step(bot: Bot, chat_id: int, s: dict) -> None:
    """Send a NEW panel for this session and register it under its message id."""
    text, kb = pp_step_view(s)
    await pp_send_panel(bot, chat_id, s, text, kb)
    pp_sessions[s["panel_msg_id"]] = s


async def pp_edit_step(bot: Bot, chat_id: int, s: dict) -> None:
    text, kb = pp_step_view(s)
    await pp_edit_panel(bot, chat_id, s, text, kb)


async def pp_handle_link(bot: Bot, chat_id: int, user_id: int, text: str) -> None:
    """Validate a pasted link and open a NEW independent panel for that channel.

    Each link gets its own session/panel, so pasting a second link never
    disturbs the first one — its panel keeps working on its own channel.
    """
    username = parse_tg_username(text)
    if not username:
        await bot.send_message(chat_id, PPS["bad_link"], parse_mode="HTML")
        return

    rec = KNOWN_CHANNELS.get(username)
    if rec:
        # We already hold data for this one (and it may not be live on Telegram).
        info = {"title": rec["title"], "bio": rec.get("bio"), "subscribers": rec["subscribers"]}
    else:
        info = await pp_fetch_channel(bot, username)
    if info is None:
        await bot.send_message(chat_id, PPS["not_in_tg"].format(u=username), parse_mode="HTML")
        return

    s = new_predict_session(user_id)
    s["username"] = username
    s["url"] = text.strip()
    s["channel_title"] = info["title"] or f"@{username}"
    s["bio"] = info.get("bio")
    s["subscribers"] = info.get("subscribers")

    if rec:
        # Category/geo already on hand → just ask format, then role.
        s["db_record"] = rec
        s["category"] = rec["category"]
        s["region"] = rec.get("region")
        s["geo"] = rec["geo"]
        s["step"] = "await_format"
    else:
        # Otherwise ask the full set: category → region → (country) → format → role.
        s["step"] = "await_category"
    # Reply to the pasted link with a FRESH panel below it — never overwrite the
    # "send a link" example. Button steps from here edit this panel in place.
    await pp_send_step(bot, chat_id, s)


async def pp_show_result(bot: Bot, chat_id: int, s: dict) -> None:
    """Pretend to call the (non-existent) pricing backend and render the result."""
    rec = s.get("db_record")
    cpm = rec["cpm_usd"] if rec else None
    low, high = predict_price(s["category"], s["geo"], s.get("region"), s["fmt"], s.get("subscribers"), cpm)
    lines = [
        "💸 Here's what an ad here is worth",
        "",
        f"📡 Channel: {s['channel_title']} (@{s['username']})",
    ]
    if s.get("subscribers"):
        lines.append(f"👥 Audience: {s['subscribers']:,} subscribers")
    lines += [
        f"🏷 Category: {s['category']}",
        f"🌍 Geo: {s['geo']}",
        f"🗓 Format: {s['fmt']}",
        "",
        f"💰 Estimated price: ${low:,}–${high:,} per post",
        "",
    ]
    lines.append(PPS["cta_owner" if s["role"] == "owner" else "cta_adv"].format(title=s["channel_title"]))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Open adstail.com", url="https://adstail.com")],
        [InlineKeyboardButton(text="↻ Predict another channel", callback_data="pp:restart")],
    ])
    await pp_edit_panel(bot, chat_id, s, "\n".join(lines), kb)


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
    pp_mode_users.discard(message.from_user.id)   # leave the price-predict flow
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
    if _pp_active(message.from_user.id):
        return
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
    if _pp_active(message.from_user.id):
        await message.answer(PPS["send_link_hint"], parse_mode="HTML")
        return
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


# --- price-predict handlers ----------------------------------------------
# Registered ABOVE the generic creative handlers so an active session captures
# the user's text before on_text can treat it as a creative caption.

@dp.message(Command("price_predict"))
async def on_price_predict(message: Message, bot: Bot) -> None:
    pp_mode_users.add(message.from_user.id)
    await message.answer(PPS["intro"])
    await message.answer(PPS["ask_link"], parse_mode="HTML")


def _pp_text_filter(message: Message) -> bool:
    return _pp_active(message.from_user.id)


@dp.message(F.text, _pp_text_filter)
async def pp_on_text(message: Message, bot: Bot) -> None:
    # In price-predict mode every message is treated as a channel link: each
    # valid link opens its OWN panel, so pasting a second link leaves the first
    # panel fully usable. Non-links get the bad-link hint.
    await pp_handle_link(bot, message.chat.id, message.from_user.id, message.text)


@dp.callback_query(F.data.startswith("pp:cat:"))
async def pp_cb_category(cq: CallbackQuery, bot: Bot) -> None:
    s = pp_session_for(cq)
    await cq.answer()
    if not s or s["step"] != "await_category":
        return
    try:
        idx = int(cq.data.split(":")[2])
    except (ValueError, IndexError):
        return
    if not 0 <= idx < len(PP_CATEGORIES):
        return
    s["category"] = PP_CATEGORIES[idx]
    s["step"] = "await_region"
    await pp_edit_step(bot, cq.message.chat.id, s)


@dp.callback_query(F.data.startswith("pp:region:"))
async def pp_cb_region(cq: CallbackQuery, bot: Bot) -> None:
    s = pp_session_for(cq)
    await cq.answer()
    if not s or s["step"] != "await_region":
        return
    try:
        idx = int(cq.data.split(":")[2])
    except (ValueError, IndexError):
        return
    if not 0 <= idx < len(PP_REGIONS):
        return
    region = PP_REGIONS[idx]
    s["region"] = region
    if not PP_COUNTRIES_BY_REGION.get(region):
        # Region with no country breakdown (e.g. Worldwide) is a complete answer.
        s["geo"] = region
        s["step"] = "await_format"
    else:
        s["step"] = "await_country"
    await pp_edit_step(bot, cq.message.chat.id, s)


@dp.callback_query(F.data.startswith("pp:country:"))
async def pp_cb_country(cq: CallbackQuery, bot: Bot) -> None:
    s = pp_session_for(cq)
    await cq.answer()
    if not s or s["step"] != "await_country":
        return
    choice = cq.data.split(":")[2]
    region = s.get("region")
    countries = PP_COUNTRIES_BY_REGION.get(region, [])
    if choice == "all":
        s["geo"] = region            # keep it region-wide (e.g. "Asia-Pacific")
    elif choice == "other":
        s["geo"] = f"Other ({region})"   # a smaller market — priced at region rate
    else:
        try:
            idx = int(choice)
        except ValueError:
            return
        if not 0 <= idx < len(countries):
            return
        s["geo"] = countries[idx]
    s["step"] = "await_format"
    await pp_edit_step(bot, cq.message.chat.id, s)


@dp.callback_query(F.data.startswith("pp:fmt:"))
async def pp_cb_format(cq: CallbackQuery, bot: Bot) -> None:
    s = pp_session_for(cq)
    await cq.answer()
    if not s or s["step"] != "await_format":
        return
    try:
        idx = int(cq.data.split(":")[2])
    except (ValueError, IndexError):
        return
    if not 0 <= idx < len(PP_FORMATS):
        return
    s["fmt"] = PP_FORMATS[idx]
    s["step"] = "await_role"
    await pp_edit_step(bot, cq.message.chat.id, s)


@dp.callback_query(F.data.startswith("pp:role:"))
async def pp_cb_role(cq: CallbackQuery, bot: Bot) -> None:
    s = pp_session_for(cq)
    await cq.answer()
    if not s or s["step"] != "await_role":
        return
    s["role"] = "owner" if cq.data.endswith("owner") else "advertiser"
    s["step"] = "done"
    await pp_show_result(bot, cq.message.chat.id, s)


@dp.callback_query(F.data == "pp:back")
async def pp_cb_back(cq: CallbackQuery, bot: Bot) -> None:
    s = pp_session_for(cq)
    await cq.answer()
    if not s:
        return
    target = pp_prev_step(s)
    if target is None:
        return
    s["step"] = target
    await pp_edit_step(bot, cq.message.chat.id, s)


@dp.callback_query(F.data == "pp:restart")
async def pp_cb_restart(cq: CallbackQuery, bot: Bot) -> None:
    await cq.answer()
    pp_mode_users.add(cq.from_user.id)
    await bot.send_message(cq.message.chat.id, PPS["ask_link"], parse_mode="HTML")


# --- creative handlers ----------------------------------------------------

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
    if _pp_active(message.from_user.id):
        await message.answer(PPS["send_link_hint"], parse_mode="HTML")
        return
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
            BotCommand(command="price_predict", description="предсказать цену размещения в канале"),
        ]
    )
    # Make the chat "Menu" button open the commands list.
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    # Drop any backlog so a previously-conflicting poller can't replay updates.
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
