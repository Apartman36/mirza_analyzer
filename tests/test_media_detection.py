from pathlib import Path

from mirza_analyzer.telegram_export import (
    extract_media_candidates,
    is_placeholder_media_value,
    resolve_media_path,
)


def test_placeholder_detection() -> None:
    assert is_placeholder_media_value(
        "(File not included. Change data exporting settings to download.)"
    )
    assert not is_placeholder_media_value("photos/photo_1.jpg")


def test_real_media_path_detection(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    photos.mkdir()
    photo = photos / "photo_1.jpg"
    photo.write_bytes(b"fake-jpeg")

    assert resolve_media_path(tmp_path, "photos/photo_1.jpg") == photo.resolve()

    candidates = extract_media_candidates(
        {"id": 1, "photo": "photos/photo_1.jpg"},
        tmp_path,
    )

    assert len(candidates) == 1
    assert candidates[0].is_real_file
    assert candidates[0].media_kind == "photo"
    assert candidates[0].relative_path == "photos/photo_1.jpg"


def test_placeholder_is_not_real_media(tmp_path: Path) -> None:
    candidates = extract_media_candidates(
        {
            "id": 1,
            "photo": "(File not included. Change data exporting settings to download.)",
        },
        tmp_path,
    )

    assert len(candidates) == 1
    assert not candidates[0].is_real_file
    assert candidates[0].missing_reason == "placeholder"


def test_missing_local_path_is_not_real_media(tmp_path: Path) -> None:
    candidates = extract_media_candidates(
        {"id": 1, "file": "files/missing.webp"},
        tmp_path,
    )

    assert len(candidates) == 1
    assert not candidates[0].is_real_file
    assert candidates[0].missing_reason == "missing"


def test_nested_existing_document_id_is_detected(tmp_path: Path) -> None:
    stickers = tmp_path / "stickers"
    stickers.mkdir()
    sticker = stickers / "sticker.webp"
    sticker.write_bytes(b"webp")

    candidates = extract_media_candidates(
        {
            "id": 1,
            "text": [
                "Hi ",
                {"type": "custom_emoji", "text": "x", "document_id": "stickers/sticker.webp"},
            ],
        },
        tmp_path,
    )

    assert len(candidates) == 1
    assert candidates[0].is_real_file
    assert candidates[0].media_kind == "sticker"
    assert candidates[0].absolute_path == sticker.resolve()
