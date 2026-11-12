"""Telegram: what a run sends, in what order, how the watermark moves, that a failure never
reaches the run, and `bodh telegram` finding the chat."""

from __future__ import annotations

import httpx
import pytest
from conftest import seed_scored
from test_cli import (
    _env,
    _project,
    _secrets_out_of_the_environment,  # noqa: F401  # the autouse fixture: no key outlives a test
    invoke,
)

from bodh import store, telegram
from bodh.config import Secrets, load_config

TOKEN = "123456:ABC-telegram-test-token"
SECRETS = Secrets(telegram_bot_token=TOKEN)


class FakeBotApi:
    """api.telegram.org: records every sendMessage; answers getUpdates with `updates`."""

    def __init__(self, monkeypatch, updates: list[dict] | None = None, fail_after: int | None = None):
        self.sent: list[tuple[str, str]] = []  # (chat_id, text)
        self.updates = updates or []
        self.fail_after = fail_after
        self.urls: list[str] = []
        monkeypatch.setattr(telegram.httpx, "post", self.post)

    def post(self, url: str, json: dict, timeout: float) -> httpx.Response:
        self.urls.append(url)
        method = url.rsplit("/", 1)[1]
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": self.updates})
        assert method == "sendMessage" and json["parse_mode"] == "HTML"
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            return httpx.Response(429, json={"ok": False, "description": "Too Many Requests"})
        self.sent.append((json["chat_id"], json["text"]))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})


def _scored_at(conn, article_id: str, when: str) -> None:
    conn.execute("UPDATE scores SET scored_at = ? WHERE article_id = ?", (when, article_id))


def test_run_hook_sends_each_new_article_at_the_band_once_oldest_first(cfg, conn, monkeypatch):
    api = FakeBotApi(monkeypatch)
    store.kv_set(conn, telegram.CHAT_KEY, "4242")
    ids = seed_scored(
        conn,
        cfg,
        [
            {"n": 1, "score": 91, "title": "Best & brightest", "folder": "Tech"},
            {"n": 2, "score": 80, "title": "At the band"},
            {"n": 3, "score": 79.9, "title": "Just under"},
            {"n": 4, "score": 85, "title": "Already read"},
        ],
    )
    whens = (
        "2026-11-12T10:00:00+00:00",
        "2026-11-12T09:00:00+00:00",
        "2026-11-12T11:00:00+00:00",
        "2026-11-12T12:00:00+00:00",
    )
    for i, when in zip(ids, whens, strict=True):
        _scored_at(conn, i, when)
    conn.execute("UPDATE state SET status = 'read' WHERE article_id = ?", (ids[3],))

    assert telegram.send_new(cfg, conn, SECRETS) == 2
    assert [c for c, _ in api.sent] == ["4242", "4242"]
    first, second = (t for _, t in api.sent)
    assert first.startswith("<b>At the band</b>") and second.startswith("<b>Best &amp; brightest</b>")
    assert "Feed A · Tech · 91" in second and "\nl\n" in second and "http" in second
    assert store.kv_get(conn, telegram.SENT_THROUGH_KEY) == "2026-11-12T10:00:00+00:00"
    # the next run: nothing new, nothing sent, the watermark stays
    assert telegram.send_new(cfg, conn, SECRETS) == 0 and len(api.sent) == 2
    # a new judgment after the watermark goes; a lower band is a setting
    new = seed_scored(conn, cfg, [{"n": 5, "score": 88, "title": "Fresh"}])
    _scored_at(conn, new[0], "2026-11-12T13:00:00+00:00")
    assert telegram.send_new(cfg, conn, SECRETS) == 1 and api.sent[-1][1].startswith("<b>Fresh</b>")
    cfg.telegram_band = 70
    assert telegram.send_new(cfg, conn, SECRETS) == 0  # "Just under" was scored before the watermark


def test_a_failure_keeps_the_watermark_at_the_last_message_and_never_raises(cfg, conn, monkeypatch, caplog):
    api = FakeBotApi(monkeypatch, fail_after=1)
    store.kv_set(conn, telegram.CHAT_KEY, "4242")
    ids = seed_scored(conn, cfg, [{"n": 1, "score": 90}, {"n": 2, "score": 95}])
    _scored_at(conn, ids[0], "2026-11-12T10:00:00+00:00")
    _scored_at(conn, ids[1], "2026-11-12T11:00:00+00:00")
    assert telegram.send_new(cfg, conn, SECRETS) == 1
    assert store.kv_get(conn, telegram.SENT_THROUGH_KEY) == "2026-11-12T10:00:00+00:00"
    assert "telegram: could not send" in caplog.text and TOKEN not in caplog.text
    api.fail_after = None  # the next run sends the one that failed
    assert telegram.send_new(cfg, conn, SECRETS) == 1 and len(api.sent) == 2
    # no token, or no chat yet: a no-op, no call
    before = len(api.urls)
    assert telegram.send_new(cfg, conn, Secrets()) == 0
    store.kv_set(conn, telegram.CHAT_KEY, "")
    assert telegram.send_new(cfg, conn, SECRETS) == 0 and len(api.urls) == before


def test_network_errors_are_a_log_line_without_the_token(cfg, conn, monkeypatch, caplog):
    store.kv_set(conn, telegram.CHAT_KEY, "4242")
    seed_scored(conn, cfg, [{"n": 1, "score": 90}])

    def down(url, **kw):
        raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(telegram.httpx, "post", down)
    assert telegram.send_new(cfg, conn, SECRETS) == 0
    assert "ConnectError" in caplog.text and TOKEN not in caplog.text


def test_connect_finds_the_chat_from_the_first_message_and_remembers_it(cfg, conn, monkeypatch):
    api = FakeBotApi(monkeypatch)
    seed_scored(conn, cfg, [{"n": 1, "score": 90, "title": "Waiting"}])
    with pytest.raises(RuntimeError, match="press Start"):
        telegram.connect(cfg, conn, SECRETS)
    assert store.kv_get(conn, telegram.CHAT_KEY) is None
    api.updates = [
        {"update_id": 1, "message": {"chat": {"id": 1111}, "text": "hi"}},
        {"update_id": 2, "message": {"chat": {"id": 2222}, "text": "/start"}},
    ]
    line = telegram.connect(cfg, conn, SECRETS)
    assert line == "telegram: chat 2222, 1 article sent (from score 80)"
    assert store.kv_get(conn, telegram.CHAT_KEY) == "2222"
    assert [t[:9] for _, t in api.sent] == ["Bodh here", "<b>Waitin"]
    # again: the chat is remembered (no getUpdates), nothing pending
    n_updates = sum(u.endswith("getUpdates") for u in api.urls)
    assert telegram.connect(cfg, conn, SECRETS) == "telegram: chat 2222, 0 articles sent (from score 80)"
    assert sum(u.endswith("getUpdates") for u in api.urls) == n_updates
    with pytest.raises(RuntimeError, match="bodh keys set telegram"):
        telegram.connect(cfg, conn, Secrets())


def test_telegram_command_and_keys_set(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    _env(root)
    result = invoke(["telegram"])
    assert result.exit_code == 1 and "bodh keys set telegram" in result.output
    result = invoke(["keys", "set", "telegram"], input=TOKEN + "\n")
    assert result.exit_code == 0 and "TELEGRAM_BOT_TOKEN: written to" in result.output, result.output
    api = FakeBotApi(monkeypatch, updates=[{"update_id": 1, "message": {"chat": {"id": 77}}}])
    result = invoke(["telegram"])
    assert result.exit_code == 0 and "chat 77, 0 articles sent" in result.output, result.output
    assert api.sent == [("77", "Bodh here. New articles the judge scores highly will come to this chat.")]
    result = invoke(["doctor"])
    assert "telegram bot token: set" in result.output
    # the network down while finding the chat: one line, no traceback, no token
    conn = store.connect(load_config(root).db_path)
    conn.execute("DELETE FROM kv WHERE key = ?", (telegram.CHAT_KEY,))
    conn.close()

    def down(url, **kw):
        raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(telegram.httpx, "post", down)
    result = invoke(["telegram"])
    assert result.exit_code == 1 and "telegram: cannot reach" in result.output and "[secret]" in result.output
    result = invoke(["settings", "set", "telegram_band", "70"])
    assert result.exit_code == 0, result.output
    assert "telegram_band: 70" in (root / "config.yaml").read_text(encoding="utf-8")
