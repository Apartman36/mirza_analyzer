from mirza_analyzer.candidate_mining import detect_project_article, score_category_text
from mirza_analyzer.categories import load_category_configs


def test_russian_utf8_category_config_loads_correctly() -> None:
    categories = {category.category_id: category for category in load_category_configs()}

    assert categories["flooring"].display_name == "Полы"
    assert "кварцвинил" in categories["flooring"].strong_keywords
    assert categories["wall_colors"].display_name == "Цвета стен"


def test_flooring_does_not_match_noise_words_as_weak_floor() -> None:
    for text in ["полка над диваном", "полотенце на крючке", "полезные советы"]:
        assert score_category_text("flooring", text) is None


def test_flooring_matches_flooring_terms() -> None:
    for text in ["плитка на пол", "напольное покрытие", "кварцвинил"]:
        match = score_category_text("flooring", text)
        assert match is not None
        assert match.score >= 3


def test_kitchen_matches_facades_and_decor() -> None:
    match = score_category_text("kitchens", "Кухня фасады Дуб Каселла")

    assert match is not None
    assert match.confidence_level == "high"
    assert "дуб каселла" in match.matched_strong_terms


def test_wall_colors_match_codes_and_paint_brands() -> None:
    ral_match = score_category_text("wall_colors", "Цвет стен RAL 7047")
    tikkurila_match = score_category_text("wall_colors", "Tikkurila Symphony G482")

    assert ral_match is not None
    assert ral_match.confidence_level == "high"
    assert tikkurila_match is not None
    assert tikkurila_match.confidence_level == "high"


def test_sofas_match_vendor_and_fabric_terms() -> None:
    match = score_category_text("sofas", "Divan.ru Слипсон Velvet Emerald")

    assert match is not None
    assert match.confidence_level == "high"
    assert "divan.ru" in match.matched_strong_terms
    assert "velvet emerald" in match.matched_strong_terms


def test_hallway_generic_wardrobe_is_not_high_confidence_without_context() -> None:
    match = score_category_text("hallway", "Шкаф распашной", photo_count=1)

    assert match is not None
    assert match.confidence_level == "low"


def test_project_article_detector_matches_article_phrases() -> None:
    first = detect_project_article("Артикулы проекта")
    second = detect_project_article("Купить все артикулы")

    assert first.is_project_article
    assert "Артикулы проекта" in first.detected_article_terms
    assert second.is_project_article
    assert "Купить все артикулы" in second.detected_article_terms

