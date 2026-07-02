"""Tests for Telegram command handling (no network)."""

from __future__ import annotations

from datetime import datetime, timezone

from polybot.notify.telegram import ControlState, handle_command
from polybot.paper.storage import Position, Storage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_handle_command(tmp_path):
    store = Storage(str(tmp_path / "t.sqlite3"))
    control = ControlState()

    assert handle_command("/help", control, store).startswith("polybot commands")
    assert handle_command("", control, store) is None
    assert handle_command("/foo", control, store) == "unknown command — /help"

    # kill-switch toggle
    handle_command("/pause", control, store)
    assert control.paused is True
    handle_command("/resume", control, store)
    assert control.paused is False

    # empty store
    status = handle_command("/status", control, store)
    assert "open positions: 0" in status and "exposure: $0.00" in status
    assert handle_command("/positions", control, store) == "no open positions"
    assert handle_command("/report", control, store).startswith("report:")

    # with a position
    store.insert_position(Position(
        market_id="1", question="Will X happen?", side="YES", entry_price=0.5,
        model_prob=0.6, edge=0.1, size_usd=5.0, shares=10.0, ts_open=_now(),
    ))
    assert "Will X happen?" in handle_command("/positions", control, store)
    assert "open positions: 1" in handle_command("/status", control, store)

    # tolerate /cmd@BotName form
    assert handle_command("/status@PolyBot", control, store).startswith("paused:")
    store.close()
