"""Telegram: the best new articles, one message each, sent at the end of every run.

A bot token (`bodh keys set telegram`) and a chat: `bodh telegram` finds the chat from the
first message you send the bot and remembers it. Every run then sends each unread article the
judge scored at `telegram_band` or more since the last message, oldest first; the watermark
(the `scored_at` of the last one sent) moves with every message, so a failure half way is
retried from where it stopped. Like the heartbeat, a missing or failing Telegram never fails a
run."""

from __future__ import annotations

import html
import logging
import sqlite3
from typing import Any

import httpx

from . import store
from .config import Config, Secrets

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot"
CHAT_KEY = "telegram.chat_id"  # kv: the chat the bot writes to, found by `bodh telegram`
SENT_THROUGH_KEY = "telegram.sent_through"  # kv: scored_at of the last article sent
TIMEOUT = 10.0


class Refused(RuntimeError):
    """The API said no. `code` is its error_code: a 4xx other than 429 means the request itself
    (this message) is wrong and will be wrong next time too; anything else may pass later."""

    def __init__(self, method: str, code: int, description: str) -> None:
        super().__init__(f"telegram {method}: {description} ({code})")
        self.code = code

    @property
    def permanent(self) -> bool:
        return 400 <= self.code < 500 and self.code != 429


class Telegram:
    """The Bot API, two calls of it. Raises httpx errors and `Refused`; the run hook
    (`send_new`) catches, the command lets the user see."""

    def __init__(self, token: str) -> None:
        self.token = token.strip()

    def _call(self, method: str, **params: Any) -> Any:
        r = httpx.post(f"{API}{self.token}/{method}", json=params, timeout=TIMEOUT)
        body = r.json()
        if not body.get("ok"):
            raise Refused(
                method, int(body.get("error_code") or r.status_code), str(body.get("description", ""))
            )
        return body["result"]

    def find_chat_id(self) -> str | None:
        """The chat of the latest message anyone sent the bot, or None when nobody has."""
        updates = self._call("getUpdates")
        for update in reversed(updates):
            chat = (update.get("message") or update.get("channel_post") or {}).get("chat") or {}
            if chat.get("id") is not None:
                return str(chat["id"])
        return None

    def send(self, chat_id: str, text: str) -> None:
        self._call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


def _text(s: str) -> str:
    """Telegram's HTML: every <, > and & outside a tag must be an entity, in a URL too (the
    entities are decoded before the message is shown, so the link stays a link)."""
    return html.escape(s, quote=False)


def message(row: sqlite3.Row | dict[str, Any]) -> str:
    """One article as Telegram HTML: title, where it is from and the score, the lesson, the link."""
    where = " · ".join(p for p in (row["feed_title"], row["folder"]) if p)
    lines = [f"<b>{_text(row['title'] or '(untitled)')}</b>"]
    lines.append(_text(f"{where} · {row['score']:.0f}" if where else f"{row['score']:.0f}"))
    if row["lesson"]:
        lines.append(_text(row["lesson"]))
    if row["url"]:
        lines.append(_text(row["url"]))
    return "\n".join(lines)


def pending(conn: sqlite3.Connection, band: float) -> list[sqlite3.Row]:
    """Unread articles at `band` or more scored after the last one sent, oldest judgment first.
    Before the first message ever, that is every unread article at the band."""
    since = store.kv_get(conn, SENT_THROUGH_KEY, "") or ""
    return conn.execute(
        """SELECT a.id, a.title, a.url, a.feed_title, a.folder, s.score, s.lesson, s.scored_at
           FROM state st JOIN scores s ON s.article_id = st.article_id JOIN articles a ON a.id = st.article_id
           WHERE st.status = 'unread' AND s.score >= ? AND s.model != 'dedup' AND s.scored_at > ?
           ORDER BY s.scored_at ASC""",
        (band, since),
    ).fetchall()


def send_pending(cfg: Config, conn: sqlite3.Connection, secrets: Secrets) -> tuple[int, Exception | None]:
    """Send what `pending` lists, moving the watermark after each message. Returns how many
    went and the error that stopped it, if one did; the next call starts after the last one
    that went through."""
    token = secrets.value("telegram_bot_token")
    chat_id = store.kv_get(conn, CHAT_KEY)
    if not token or not chat_id:
        return 0, None
    bot = Telegram(token)
    sent = 0
    for row in pending(conn, cfg.telegram_band):
        try:
            bot.send(chat_id, message(row))
        except Refused as exc:
            if not exc.permanent:
                return sent, exc
            # this message will never go: skip it rather than hold every later one behind it
            log.warning("telegram: skipped %s (%s)", row["id"], exc)
        except Exception as exc:
            return sent, exc
        else:
            sent += 1
        store.kv_set(conn, SENT_THROUGH_KEY, row["scored_at"])
    return sent, None


def send_new(cfg: Config, conn: sqlite3.Connection, secrets: Secrets) -> int:
    """The run's hook: `send_pending`, never raising; a failure is a log line and the next run
    sends what is still pending."""
    try:
        sent, error = send_pending(cfg, conn, secrets)
    except Exception as exc:  # a run that judged and cut has done its work without the messages
        sent, error = 0, exc
    if error is not None:
        token = secrets.value("telegram_bot_token")
        reason = str(error).replace(token, "<token>") if token else str(error)
        log.warning("telegram: could not send (%s: %s)", type(error).__name__, reason)
    return sent


def connect(cfg: Config, conn: sqlite3.Connection, secrets: Secrets) -> str:
    """`bodh telegram`: find the chat when none is remembered, then send what is pending.
    Returns the line to print; raises `RuntimeError` when there is no token or no message yet."""
    token = secrets.value("telegram_bot_token")
    if not token:
        raise RuntimeError("no bot token: `bodh keys set telegram` (the token @BotFather gives you)")
    chat_id = store.kv_get(conn, CHAT_KEY)
    if not chat_id:
        bot = Telegram(token)
        chat_id = bot.find_chat_id()
        if not chat_id:
            raise RuntimeError("no chat yet: open the bot in Telegram, press Start, then run this again")
        store.kv_set(conn, CHAT_KEY, chat_id)
        bot.send(chat_id, "Bodh here. New articles the judge scores highly will come to this chat.")
    n, error = send_pending(cfg, conn, secrets)
    if error is not None:
        raise error
    return f"telegram: chat {chat_id}, {n} article{'s' if n != 1 else ''} sent (from score {cfg.telegram_band:g})"
