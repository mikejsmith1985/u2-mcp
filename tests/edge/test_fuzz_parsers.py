"""Property-based fuzzing of the parsers and the safety validator.

Hand-written tests check the cases someone thought of. These check properties
that must hold for every input, against inputs nobody would think to write:
random bytes, adversarial delimiters, and text drawn from the whole of Unicode.

The properties are chosen so that a failure is always a real defect rather than a
surprising-but-acceptable output.
"""

import json
import string

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from u2_mcp.auth.sqlite_storage import hash_secret
from u2_mcp.config import U2Config
from u2_mcp.connection import ConnectionManager
from u2_mcp.tools.dictionary import _parse_dict_item
from u2_mcp.tools.files import _parse_file_list, _parse_file_stat
from u2_mcp.utils.dynarray import AM, SM, VM, build_record, parse_record
from u2_mcp.utils.safety import ESCAPE_COMMANDS, CommandValidator

# Text that could plausibly arrive from a Universe server: any code point, plus
# the delimiters themselves, which is what makes this adversarial.
ANY_TEXT = st.text(
    alphabet=st.characters(codec="utf-8", exclude_characters=""),
    max_size=200,
)

# Field values that contain no delimiter, so a round trip is well defined.
CLEAN_VALUE = st.text(
    alphabet=st.characters(codec="utf-8", exclude_characters=AM + VM + SM),
    max_size=40,
)

FUZZ = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def field_dicts() -> st.SearchStrategy[dict]:
    """Build record structures the way real MultiValue data is shaped."""
    values = st.one_of(
        CLEAN_VALUE,
        st.lists(CLEAN_VALUE, min_size=2, max_size=5),
        st.lists(st.lists(CLEAN_VALUE, min_size=2, max_size=3), min_size=2, max_size=3),
    )
    return st.dictionaries(
        keys=st.integers(min_value=1, max_value=30).map(str),
        values=values,
        min_size=1,
        max_size=10,
    )


class TestRecordParsing:
    """Properties that must hold for every record."""

    @given(raw=ANY_TEXT)
    @FUZZ
    def test_parsing_never_raises(self, raw: str) -> None:
        """No input from a server may crash the parser."""
        parse_record(raw)

    @given(raw=ANY_TEXT)
    @FUZZ
    def test_a_parsed_record_is_always_serializable(self, raw: str) -> None:
        """Whatever is parsed can be returned to the client as JSON."""
        json.dumps(parse_record(raw))

    @given(raw=ANY_TEXT)
    @FUZZ
    def test_field_numbers_are_always_positive_integers(self, raw: str) -> None:
        """Field keys are always usable as field positions."""
        for key in parse_record(raw):
            assert int(key) >= 1

    @given(fields=field_dicts())
    @FUZZ
    def test_build_then_parse_is_lossless(self, fields: dict) -> None:
        """Any record this server builds, it can read back unchanged."""
        assume(all(value != "" for value in fields.values()))
        assume(all(v != [] for v in fields.values()))

        rebuilt = parse_record(build_record(fields))

        for key, value in fields.items():
            if value not in ("", []):
                assert rebuilt.get(key) == value

    @given(fields=field_dicts())
    @FUZZ
    def test_building_is_deterministic(self, fields: dict) -> None:
        """The same structure always produces the same record."""
        assert build_record(fields) == build_record(fields)


class TestSanitizerProperties:
    """Properties that must hold for every server response."""

    @given(raw=ANY_TEXT)
    @FUZZ
    def test_sanitizing_never_raises(self, mock_config: U2Config, raw: str) -> None:
        """No output from a server may crash the sanitizer."""
        ConnectionManager(mock_config)._sanitize_output(raw)

    @given(raw=ANY_TEXT)
    @FUZZ
    def test_output_is_always_json_encodable(self, mock_config: U2Config, raw: str) -> None:
        """Sanitized output can always be returned to the client."""
        json.dumps({"output": ConnectionManager(mock_config)._sanitize_output(raw)})

    @given(raw=ANY_TEXT)
    @FUZZ
    def test_no_delimiter_ever_survives(self, mock_config: U2Config, raw: str) -> None:
        """MultiValue marks are always converted, never passed through raw."""
        cleaned = ConnectionManager(mock_config)._sanitize_output(raw)

        assert AM not in cleaned
        assert VM not in cleaned
        assert SM not in cleaned

    @given(raw=ANY_TEXT)
    @FUZZ
    def test_no_control_character_ever_survives(self, mock_config: U2Config, raw: str) -> None:
        """Only newline and tab remain of the control characters."""
        cleaned = ConnectionManager(mock_config)._sanitize_output(raw)

        for char in cleaned:
            if char in ("\n", "\t"):
                continue
            assert ord(char) >= 32, f"control character {ord(char)} survived"

    @given(raw=ANY_TEXT)
    @FUZZ
    def test_sanitizing_is_idempotent(self, mock_config: U2Config, raw: str) -> None:
        """Cleaning already-clean output changes nothing further."""
        manager = ConnectionManager(mock_config)
        once = manager._sanitize_output(raw)

        assert manager._sanitize_output(once) == once


class TestValidatorProperties:
    """Properties that must hold for every command string."""

    @given(command=ANY_TEXT)
    @FUZZ
    def test_validation_never_raises(self, command: str) -> None:
        """No input may crash the validator; it must return a verdict."""
        is_valid, message = CommandValidator([], read_only=False).validate(command)

        assert isinstance(is_valid, bool)
        assert isinstance(message, str)

    @given(command=ANY_TEXT)
    @FUZZ
    def test_a_refusal_always_explains_itself(self, command: str) -> None:
        """Every refusal carries a reason a caller can act on."""
        is_valid, message = CommandValidator([], read_only=False).validate(command)

        if not is_valid:
            assert message.strip()

    @given(
        verb=st.sampled_from(sorted(ESCAPE_COMMANDS)),
        leading=st.text(alphabet=" \t\r\n", max_size=5),
        trailing=st.text(alphabet=string.printable, max_size=30),
    )
    @FUZZ
    def test_an_escape_verb_is_refused_however_it_is_dressed(
        self, verb: str, leading: str, trailing: str
    ) -> None:
        """Whitespace, case and arguments never let an escape verb through.

        What follows the verb must not be a character a verb is made of, since
        `BASIC_FOO` is a genuinely different command rather than a disguised one.
        """
        assume(trailing[:1] not in set(string.ascii_letters + string.digits + "._-"))
        command = f"{leading}{verb.lower()}{trailing}"

        is_valid, _ = CommandValidator([], read_only=False).validate(command)

        assert is_valid is False, f"{command!r} was allowed"

    @given(query=ANY_TEXT)
    @FUZZ
    def test_query_validation_never_raises(self, query: str) -> None:
        """No query text may crash the query allowlist."""
        CommandValidator([], read_only=False).is_query_safe(query)

    @given(query=ANY_TEXT)
    @FUZZ
    def test_only_allowlisted_verbs_pass(self, query: str) -> None:
        """A query is accepted only when it starts with a known read verb."""
        is_safe, _ = CommandValidator([], read_only=False).is_query_safe(query)

        if is_safe:
            assert query.strip().split()[0].upper() in {
                "LIST",
                "SELECT",
                "SSELECT",
                "SORT",
                "COUNT",
                "SUM",
                "GET.LIST",
                "QSELECT",
                "SEARCH",
            }


class TestOutputParsersNeverCrash:
    """Parsers of free-form server output must survive anything."""

    @given(output=ANY_TEXT)
    @FUZZ
    def test_file_list_parsing_never_raises(self, output: str) -> None:
        """Unexpected LISTFILES output yields a list, not an exception."""
        assert isinstance(_parse_file_list(output), list)

    @given(output=ANY_TEXT)
    @FUZZ
    def test_file_stat_parsing_never_raises(self, output: str) -> None:
        """Unexpected FILE.STAT output yields a dict, not an exception."""
        assert isinstance(_parse_file_stat(output), dict)

    @given(raw=ANY_TEXT)
    @FUZZ
    def test_dictionary_item_parsing_never_raises(self, raw: str) -> None:
        """A malformed dictionary record still produces a described item."""
        item = _parse_dict_item("FIELD", parse_record(raw))

        assert item["name"] == "FIELD"
        json.dumps(item)


class TestHashingProperties:
    """The token hash is a lookup key, so it must behave like one."""

    @given(value=ANY_TEXT)
    @FUZZ
    def test_hashing_is_deterministic(self, value: str) -> None:
        """The same secret always produces the same key."""
        assert hash_secret(value) == hash_secret(value)

    @given(value=ANY_TEXT)
    @FUZZ
    def test_a_hash_is_always_a_fixed_length_hex_key(self, value: str) -> None:
        """Whatever the input, the stored key is a safe fixed-width string."""
        digest = hash_secret(value)

        assert len(digest) == 64
        assert all(char in string.hexdigits for char in digest)

    @given(first=ANY_TEXT, second=ANY_TEXT)
    @FUZZ
    def test_different_secrets_hash_differently(self, first: str, second: str) -> None:
        """Two different tokens never share a lookup key."""
        assume(first != second)

        assert hash_secret(first) != hash_secret(second)
