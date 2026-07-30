"""Focused inert tests for the unified Telegram UX contract."""

import importlib.util
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "tg_ux_ui",
    Path(__file__).resolve().parents[1] / "handlers" / "ui.py",
)
_UI = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_UI)

TELEGRAM_TEXT_LIMIT = _UI.TELEGRAM_TEXT_LIMIT
format_market_preview = _UI.format_market_preview
format_market_signal = _UI.format_market_signal
format_bybit_error_detail = _UI.format_bybit_error_detail
format_order_accepted = _UI.format_order_accepted
format_order_rejected = _UI.format_order_rejected
format_position_card = _UI.format_position_card
format_start_message = _UI.format_start_message
format_stop_message = _UI.format_stop_message
format_warning_list = _UI.format_warning_list


def _signal(**overrides):
    values = dict(
        sym="BTCUSDT",
        side="LONG",
        lev=5,
        entry_price=67500.0,
        stop_val=66300.0,
        qty=0.001,
        pos_value_usd=67.5,
        source_tag="#Manual",
        risk_usd=2.0,
    )
    values.update(overrides)
    return format_market_signal(**values)


def test_signal_preview_contract():
    message = _signal()
    assert "BYBIT BOT | SIGNAL" in message
    assert "Long: BTCUSDT · x5" in message
    assert "💰 <b>Сделка</b>" in message
    assert "🛡 <b>Риск</b>" in message
    assert "▶️ <b>Действие:</b>" in message


def test_empty_warning_list_has_no_empty_section():
    assert format_warning_list([]) == ""
    assert "Предупреждения" not in _signal(warnings=[])


def test_rejected_message_hides_traceback_and_secrets():
    message = format_order_rejected(
        "BTCUSDT",
        "LONG",
        "Traceback: api_secret=SUPERSECRET 110007 insufficient margin",
    )
    assert "BYBIT BOT | ORDER REJECTED" in message
    assert "Traceback" not in message
    assert "SUPERSECRET" not in message
    assert "110007" in message
    assert "▶️ <b>Действие:</b>" in message


def test_bybit_codes_and_safe_retmsg_are_preserved():
    class RateLimitError(Exception):
        status_code = 429

    cases = [
        ({"retCode": 33004, "retMsg": "API key expired"}, "33004", "API key expired"),
        ("retCode: 3400214, retMsg: account mode mismatch", "3400214", "account mode mismatch"),
        ("ErrCode=110007 retMsg=insufficient margin", "110007", "insufficient margin"),
        (RateLimitError("rate limit reached"), "429", "rate limit reached"),
    ]
    for detail, code, safe_message in cases:
        message = format_order_rejected("BTCUSDT", "LONG", detail)
        assert code in message
        assert safe_message in message


def test_bybit_error_does_not_invent_code_from_random_number():
    message = format_order_rejected(
        "BTCUSDT", "LONG", "Цена 67500 не прошла локальную проверку"
    )
    assert "Bybit code: 67500" not in message
    assert "Bybit code: не предоставлен" in message


def test_bybit_retmsg_masks_secret_and_removes_traceback():
    detail = {
        "retCode": 33004,
        "retMsg": (
            "Ключ просрочен api_secret=SUPERSECRET\n"
            "Traceback (most recent call last):\n"
            '  File "api.py", line 7, in call'
        ),
    }
    message = format_order_rejected("BTCUSDT", "LONG", detail)
    assert "33004" in message
    assert "Ключ просрочен" in message
    assert "[REDACTED]" in message
    assert "SUPERSECRET" not in message
    assert "Traceback" not in message
    assert 'File "api.py"' not in message


def test_bybit_retmsg_html_special_chars_are_escaped():
    message = format_order_rejected(
        "BTCUSDT",
        "LONG",
        {"retCode": 33004, "retMsg": "bad <tag> & value"},
    )
    assert "<tag>" not in message
    assert "&lt;tag&gt;" in message
    assert "&amp; value" in message


def test_stop_contract_does_not_claim_cleanup():
    message = format_stop_message()
    assert "BYBIT BOT | TRADING STOPPED" in message
    assert "Новые входы запрещены" in message
    assert "позиции не закрыты" in message
    assert "ордера могут оставаться" in message
    assert "проверьте позиции и открытые ордера вручную" in message
    assert "позиции закрыты" not in message
    assert "ордера отменены" not in message


def test_dynamic_html_values_are_escaped():
    message = _signal(
        sym="<BTC&USDT>",
        source_tag="<script>secret</script>",
    )
    assert "<script>" not in message
    assert "&lt;BTC&amp;USDT&gt;" in message
    assert "&lt;script&gt;secret&lt;/script&gt;" in message


def test_primary_renders_fit_telegram_text_limit():
    messages = [
        _signal(),
        format_market_preview(
            "BTCUSDT", "LONG", 5, 67500.0, 66300.0, 0.001, 67.5,
            2.0, "#Manual", 27.0, 100.0, ttl_sec=300,
        ),
        format_order_accepted(
            "BTCUSDT", "LONG", 0.001, stop=66300.0, leverage=5, risk_usd=2.0,
        ),
        format_order_rejected("BTCUSDT", "LONG", "110007 insufficient margin"),
        format_start_message(25.0, "Mainnet"),
        format_stop_message(),
        format_position_card(
            "BTCUSDT", "Buy", 2.35, 0.94,
            entry=67500.0, qty=0.001, leverage=5, stop=66300.0,
        ),
    ]
    assert all(
        len(message.encode("utf-8")) <= TELEGRAM_TEXT_LIMIT
        for message in messages
    )


def test_accepted_message_does_not_claim_filled():
    message = format_order_accepted(
        "BTCUSDT", "LONG", 0.001,
        stop=66300.0, leverage=5, risk_usd=2.0,
    )
    assert "FILLED" not in message.upper()
