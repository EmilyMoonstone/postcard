import pytest

tray = pytest.importorskip("postcard.tray")


def test_a_menu_item_names_sender_and_subject_and_marks_unread():
    assert tray.menu_label("Ada", "Lunch", is_unread=True) == "● Ada — Lunch"
    assert tray.menu_label("", "Lunch", is_unread=False) == "Lunch"


def test_a_long_item_is_shortened_and_underscores_are_not_mnemonics():
    label = tray.menu_label("snake_case", "x" * 100, is_unread=False)

    assert label.startswith("snake__case — ")
    assert label.endswith("…")
