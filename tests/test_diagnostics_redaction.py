"""The profile line scrub, at the level the tricky cases live at.

The bundle-level assertion (a profile's key never reaches the download) is in
``tests/integration/test_diagnostics.py``. What is here is the text handling
underneath it, which has edge cases -- block scalars, comments, list items --
that are far cheaper to state directly than through a running Home Assistant.
"""

from __future__ import annotations

from homeassistant.components.diagnostics import REDACTED

from custom_components.emhass_companion.diagnostics import _redact_secret_lines


def test_a_matching_key_loses_its_value_and_keeps_its_key():
    scrubbed = _redact_secret_lines("emhass:\n  solcast_api_key: hunter2\n")

    assert scrubbed == f"emhass:\n  solcast_api_key: {REDACTED}\n"


def test_every_pattern_the_module_documents_is_caught():
    content = "\n".join(
        (
            "password: leak1",
            "auth_token: leak2",
            "API_KEY: leak3",
            "client_secret: leak4",
            "Authorization: leak5",
        )
    )

    scrubbed = _redact_secret_lines(content)

    assert "leak" not in scrubbed
    assert scrubbed.count(REDACTED) == 5


def test_an_ordinary_setting_is_left_exactly_as_written():
    content = "emhass:\n  weather_forecast_method: solcast   # trailing comment\n"

    assert _redact_secret_lines(content) == content


def test_a_key_inside_a_comment_is_not_mistaken_for_a_setting():
    """Comments are most of a profile's explanatory value; a `#` line cannot
    be a live credential, so redacting one only costs readability."""
    content = "# set api_key: to whatever Solcast gave you\napi_key: real\n"

    scrubbed = _redact_secret_lines(content)

    assert "# set api_key: to whatever Solcast gave you" in scrubbed
    assert "real" not in scrubbed


def test_a_secret_in_a_list_item_is_caught():
    scrubbed = _redact_secret_lines("headers:\n  - authorization: Bearer abc\n")

    assert "abc" not in scrubbed
    assert scrubbed == f"headers:\n  - authorization: {REDACTED}\n"


def test_a_block_scalar_is_redacted_through_its_indented_body():
    """A long token is exactly the thing somebody writes as `|`, and the value
    is then on the following lines rather than on the key's own."""
    content = "api_key: |\n  first-half\n  second-half\nname: Mine\n"

    scrubbed = _redact_secret_lines(content)

    assert "first-half" not in scrubbed
    assert "second-half" not in scrubbed
    # The block ends where the indentation does.
    assert scrubbed.endswith("name: Mine\n")


def test_a_mapping_under_a_matching_key_goes_with_it():
    """`_redact_config` replaces a matching key's whole subtree rather than a
    scalar leaf, and the scrub has to mean the same thing."""
    content = "secrets:\n  solcast: hunter2\n  forecast_solar: hunter3\nname: Mine\n"

    scrubbed = _redact_secret_lines(content)

    assert "hunter2" not in scrubbed
    assert "hunter3" not in scrubbed
    assert scrubbed.endswith("name: Mine\n")


def test_line_count_survives_redaction():
    """A profile that failed to load is reported with the line the parser
    objected to, and that number has to still mean something here."""
    content = "kind: pv\napi_key: |\n  a\n\n  b\nname: Mine\n"

    assert len(_redact_secret_lines(content).splitlines()) == len(content.splitlines())


def test_a_file_truncated_mid_secret_still_loses_the_value():
    """Content is cut to _MAX_PROFILE_BYTES before this runs, so the last line
    it sees may be half of one."""
    scrubbed = _redact_secret_lines("api_key: hunter")

    assert "hunter" not in scrubbed
