"""Credential display must never reconstruct a short key."""

import pytest

from thwip.utils import mask_key


@pytest.mark.parametrize("length", range(1, 14))
def test_short_keys_are_completely_hidden(length):
    assert mask_key("abcdefghijklm"[:length]) == "****"


def test_long_key_has_a_hidden_middle():
    key = "prefix-middle-secret-suffix"
    masked = mask_key(key)
    assert "secret" not in masked
    assert masked == "prefix-...suffix"
