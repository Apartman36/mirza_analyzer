from mirza_analyzer.telegram_export import normalize_text


def test_normalize_text_accepts_plain_string() -> None:
    assert normalize_text("plain text") == "plain text"


def test_normalize_text_concatenates_telegram_entity_list() -> None:
    value = [
        "Hello ",
        {"type": "bold", "text": "world"},
        " ",
        {"type": "link", "text": ["from ", {"type": "italic", "text": "Telegram"}]},
    ]

    assert normalize_text(value) == "Hello world from Telegram"


def test_normalize_text_handles_none_and_fallback_values() -> None:
    assert normalize_text(None) == ""
    assert normalize_text(123) == "123"

