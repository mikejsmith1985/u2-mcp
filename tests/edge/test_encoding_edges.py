"""Encoding edge cases in output sanitization.

The sanitizer was changed to filter by Unicode category rather than by code
point, which fixed accented names but widened what reaches the user. These tests
check both directions: real text in any alphabet survives, and anything that
could corrupt a display, hide content, or crash a JSON encoder does not.
"""

import json

import pytest

from u2_mcp.connection import ConnectionManager

# Text that must survive intact: this is business data, not decoration.
REAL_TEXT = [
    ("German", "MÜLLER GmbH & Co. KG"),
    ("Spanish", "José Peña Construcción"),
    ("French", "Société Générale d'Électricité"),
    ("Currency", "£1,250.00 / €1,430.55 / ¥98,000"),
    ("Polish", "Łódź Kraśnik Zażółć"),
    ("Turkish", "İstanbul Şirketi Ağrı"),
    ("Greek", "Ελληνικά Ηλεκτρικά"),
    ("Cyrillic", "Электрика Москва"),
    ("Japanese", "東京電気株式会社"),
    ("Arabic", "شركة الكهرباء"),
    ("Hebrew", "חשמל בעמ"),
    ("Emoji", "Order status 🚚 shipped"),
    ("Combining", "électricité"),
    ("Symbols", "±10% ≈ 3.5Ω · 240V"),
]

# Characters that must never reach a display: control codes, and the invisible
# formatting characters that can hide text or reverse how it reads.
DANGEROUS_CHARACTERS = [
    ("null", "\x00"),
    ("bell", "\x07"),
    ("escape", "\x1b"),
    ("backspace", "\x08"),
    ("zero width space", "​"),
    ("zero width joiner", "‍"),
    ("right to left override", "‮"),
    ("left to right override", "‭"),
    ("byte order mark", "﻿"),
    ("soft hyphen", "­"),
]


class TestRealTextSurvives:
    """Business data in any alphabet must arrive intact."""

    @pytest.mark.parametrize(("label", "text"), REAL_TEXT, ids=[t[0] for t in REAL_TEXT])
    def test_text_is_unchanged(
        self, connection_manager: ConnectionManager, label: str, text: str
    ) -> None:
        """Sanitizing ordinary text changes nothing about it."""
        assert connection_manager._sanitize_output(text) == text


class TestDangerousCharactersAreRemoved:
    """Invisible and control characters must not reach a user's screen."""

    @pytest.mark.parametrize(
        ("label", "character"), DANGEROUS_CHARACTERS, ids=[c[0] for c in DANGEROUS_CHARACTERS]
    )
    def test_the_character_is_stripped(
        self, connection_manager: ConnectionManager, label: str, character: str
    ) -> None:
        """Each dangerous character is removed while its neighbours remain."""
        cleaned = connection_manager._sanitize_output(f"BEFORE{character}AFTER")

        assert character not in cleaned
        assert "BEFORE" in cleaned
        assert "AFTER" in cleaned


class TestMalformedInput:
    """Universe can return bytes that are not clean text."""

    def test_lone_surrogates_are_removed(self, connection_manager: ConnectionManager) -> None:
        """An unpaired surrogate would break JSON encoding, so it is dropped."""
        cleaned = connection_manager._sanitize_output("GOOD\ud800BAD")

        assert "\ud800" not in cleaned

    def test_the_result_is_always_json_encodable(
        self, connection_manager: ConnectionManager
    ) -> None:
        """Whatever arrives, what leaves can be serialized to the client."""
        messy = "".join(chr(n) for n in range(0, 1000)) + "𐏿"

        json.dumps({"output": connection_manager._sanitize_output(messy)})

    def test_an_empty_response_stays_empty(self, connection_manager: ConnectionManager) -> None:
        """Nothing in, nothing out."""
        assert connection_manager._sanitize_output("") == ""

    def test_a_large_response_is_not_truncated(self, connection_manager: ConnectionManager) -> None:
        """Sanitizing must not silently drop the tail of a big result."""
        text = "row of output data\n" * 50000

        assert len(connection_manager._sanitize_output(text)) == len(text)


class TestDelimiterConversionWithUnicode:
    """Delimiter conversion and Unicode preservation must not interfere."""

    def test_accented_text_around_delimiters_survives(
        self, connection_manager: ConnectionManager
    ) -> None:
        """Converting marks does not disturb the text on either side."""
        raw = "MÜLLER" + chr(254) + "José" + chr(253) + "Peña"

        cleaned = connection_manager._sanitize_output(raw)

        assert "MÜLLER" in cleaned
        assert "José" in cleaned
        assert "Peña" in cleaned

    def test_delimiters_are_still_converted(self, connection_manager: ConnectionManager) -> None:
        """The marks themselves are replaced with readable separators."""
        cleaned = connection_manager._sanitize_output("A" + chr(254) + "B" + chr(253) + "C")

        assert chr(254) not in cleaned
        assert chr(253) not in cleaned
        assert "|" in cleaned
