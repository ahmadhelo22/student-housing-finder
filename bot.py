import os
import logging
import itertools
import threading
from collections import OrderedDict
from typing import Optional

import telebot
from telebot.apihelper import ApiTelegramException
from telebot.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from core import (
    MAX_LISTINGS_IN_REPLY,
    fetch_next_batch,
    format_details,
    format_listing_caption,
    get_listing_details,
    listing_key,
    process_message,
    search_pipeline,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, num_threads=8)

MAX_DETAIL_PHOTOS = 5

MAX_STORED_LISTINGS = 5000

user_sessions: dict[int, dict] = {}

user_searches: dict[int, dict] = {}
_search_ids = itertools.count(1)

_listing_store: "OrderedDict[str, str]" = OrderedDict()
_listing_ids = itertools.count(1)
_store_lock = threading.Lock()


def _remember_listing(property_url: str) -> str:
    with _store_lock:
        listing_id = str(next(_listing_ids))
        _listing_store[listing_id] = property_url
        while len(_listing_store) > MAX_STORED_LISTINGS:
            _listing_store.popitem(last=False)  # forget the oldest
        return listing_id


def _get_listing_url(listing_id: str) -> Optional[str]:
    with _store_lock:
        return _listing_store.get(listing_id)


def _download_image(url: str) -> Optional[bytes]:
    """Fetch an image ourselves (with the scraper's browser fingerprint), for CDNs that block Telegram."""
    content = search_pipeline.scraper.fetch_all([(url, {"User-Agent": "Mozilla/5.0"})], timeout=10, binary=True)
    return content[0] if content else None


def _card_keyboard(listing_id: str, property_url: Optional[str], show_more: bool = True) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    buttons = []
    if show_more:
        buttons.append(InlineKeyboardButton("Show more", callback_data=f"details:{listing_id}"))
    if property_url:
        buttons.append(InlineKeyboardButton("Open listing", url=property_url))
    keyboard.row(*buttons)
    return keyboard


def send_listing_card(chat_id: int, listing: dict, index: int) -> None:
    """One message per listing: its photo, a short caption, and the buttons."""
    listing_id = _remember_listing(listing.get("property_url"))
    caption = format_listing_caption(listing, index)
    keyboard = _card_keyboard(listing_id, listing.get("property_url"))
    photo_url = listing.get("thumbnail_url")

    if photo_url:

        for photo in (photo_url, None):
            try:
                if photo is None:
                    photo = _download_image(photo_url)
                    if not photo:
                        break
                bot.send_photo(chat_id, photo, caption=caption, reply_markup=keyboard)
                return
            except ApiTelegramException as exc:
                logger.warning("send_photo failed for %s: %s", photo_url, exc)

    bot.send_message(chat_id, caption, reply_markup=keyboard, disable_web_page_preview=True)


def send_detail_photos(chat_id: int, image_urls: list, reply_to: int) -> None:
    urls = [u for u in image_urls if u][:MAX_DETAIL_PHOTOS]
    if len(urls) < 2:
        return  
    try:
        bot.send_media_group(chat_id, [InputMediaPhoto(u) for u in urls], reply_to_message_id=reply_to)
        return
    except ApiTelegramException as exc:
        logger.warning("send_media_group by URL failed: %s", exc)
   
    contents = search_pipeline.scraper.fetch_all(
        [(u, {"User-Agent": "Mozilla/5.0"}) for u in urls], timeout=10, binary=True
    )
    media = [InputMediaPhoto(c) for c in contents if c]
    if len(media) >= 2:
        try:
            bot.send_media_group(chat_id, media, reply_to_message_id=reply_to)
        except ApiTelegramException as exc:
            logger.warning("send_media_group by upload failed: %s", exc)


@bot.message_handler(commands=["start"])
def handle_start(message: Message):
    user_sessions.pop(message.chat.id, None)
    bot.reply_to(
        message,
        "Hi! Tell me what kind of place you're looking for "
        "location, housing type, and your budget, and I'll search for you. -",
    )


@bot.message_handler(commands=["reset"])
def handle_reset(message: Message):
    user_sessions.pop(message.chat.id, None)
    bot.reply_to(message, "Okay, starting fresh. What are you looking for?")


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(message: Message):
    chat_id = message.chat.id
    session = user_sessions.get(chat_id, {})

    def notify(text: str):
        bot.reply_to(message, text)
        bot.send_chat_action(chat_id, "typing")

    result = process_message(session, message.text, on_search_start=notify)
    user_sessions[chat_id] = result["session"]

    if not result["complete"]:
        bot.reply_to(message, result["reply"])
        return

    bot.reply_to(message, result["summary"])
    all_listings = result["scraper_result"]["listings"]
    if not all_listings:
        return


    search = {
        "id": next(_search_ids),
        "search_input": result["search_input"],
        "seen": set(),
        "page": result["search_input"].get("page") or 1,
        "lock": threading.Lock(),
    }
    user_searches[chat_id] = search

    batch = all_listings[:MAX_LISTINGS_IN_REPLY]
    if len(all_listings) <= MAX_LISTINGS_IN_REPLY:
        search["page"] += 1
    send_batch(chat_id, search, batch, exhausted=False)


def send_batch(chat_id: int, search: dict, batch: list, exhausted: bool) -> None:
    """Send one card per listing, record their IDs as seen, then the "Show next" button."""
    start = len(search["seen"]) + 1
    for index, listing in enumerate(batch, start):
        key = listing_key(listing)
        if key:
            search["seen"].add(key)
        try:
            send_listing_card(chat_id, listing, index)
        except Exception:
            logger.exception("Failed to send listing card %s", listing.get("property_url"))

    if exhausted:
        bot.send_message(chat_id, "That's all the listings I found. Send a new message to search again.")
        return
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton(f"Show next {MAX_LISTINGS_IN_REPLY}", callback_data=f"next:{search['id']}"))
    bot.send_message(
        chat_id,
        "Tap Show more on any listing for full details, or load more results below.",
        reply_markup=keyboard,
    )


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("next:"))
def handle_show_next(call: CallbackQuery):
    chat_id = call.message.chat.id
    search = user_searches.get(chat_id)
    if not search or call.data != f"next:{search['id']}":
        bot.answer_callback_query(call.id, "This search has expired. Please send a new search.", show_alert=True)
        return

    if not search["lock"].acquire(blocking=False):
        bot.answer_callback_query(call.id, "Still loading, one moment...")
        return
    try:
        bot.answer_callback_query(call.id, "Searching for more...")

        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        except ApiTelegramException:
            pass
        bot.send_chat_action(chat_id, "typing")

        result = fetch_next_batch(search["search_input"], search["seen"], search["page"])
        search["page"] = result["page"]

        if result["status"] != "ok" and not result["listings"]:
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("Try again", callback_data=f"next:{search['id']}"))
            bot.send_message(chat_id, "Sorry, the search failed. Please try again.", reply_markup=keyboard)
            return
        if not result["listings"]:
            bot.send_message(chat_id, "No more new listings. Send a new message to search again.")
            return
        send_batch(chat_id, search, result["listings"], exhausted=result["exhausted"])
    finally:
        search["lock"].release()


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("details:"))
def handle_show_more(call: CallbackQuery):
    listing_id = call.data.split(":", 1)[1]
    property_url = _get_listing_url(listing_id)
    if not property_url:
        bot.answer_callback_query(call.id, "This listing has expired. Please search again.", show_alert=True)
        return
    listing = {"property_url": property_url}

    bot.answer_callback_query(call.id, "Loading details...")
    chat_id = call.message.chat.id
    card_id = call.message.message_id
    bot.send_chat_action(chat_id, "typing")

    try:
        bot.edit_message_reply_markup(
            chat_id, card_id, reply_markup=_card_keyboard(listing_id, listing["property_url"], show_more=False)
        )
    except ApiTelegramException:
        pass

    details = get_listing_details(listing["property_url"])
    if not details:
        bot.send_message(
            chat_id,
            "Sorry, I couldn't load the details for this listing. You can still open it with the button above.",
            reply_to_message_id=card_id,
        )
     
        try:
            bot.edit_message_reply_markup(chat_id, card_id, reply_markup=_card_keyboard(listing_id, listing["property_url"]))
        except ApiTelegramException:
            pass
        return

    send_detail_photos(chat_id, details.get("images") or [], reply_to=card_id)
    bot.send_message(chat_id, format_details(details), reply_to_message_id=card_id, disable_web_page_preview=True)


if __name__ == "__main__":
    logger.info("Bot starting...")
    bot.infinity_polling()
