"""Symbol aliasing and per-symbol filling-mode resolution.

Ported from the ad-hoc script that caught two live bugs:

1. The bot's config/news layer speaks ``OILUSD``; Exness MT5 only knows
   ``USOIL``. ``symbol_info("OILUSD")`` is ``None`` — a missing alias silently
   degrades to "no such symbol".
2. ``symbol_info(sym).filling_mode`` is an ENUM (0=FOK, 1=IOC, 2=RETURN), not a
   bitmask. Exness returns 3 for BTCUSD ("all market fills allowed") and 1 for
   XAUUSD/XAGUSD/USOIL/EURUSD. Passing the enum straight through as
   ``type_filling`` is an invalid request — that was the original 10030
   "Unsupported filling mode" bug on BTCUSD.

These tests are pure stdlib and hit no network.
"""
import pytest

from core.symbols import (CANONICAL_SYMBOLS, FALLBACK_SYMBOL,
                          RETRYABLE_FILL_RETCODES, SYMBOL_ALIASES,
                          bitmask_allows, describe_filling, filling_candidates,
                          resolve_symbol)


# ---------------------------------------------------------------------------
# aliases
# ---------------------------------------------------------------------------
def test_oilusd_maps_to_the_broker_symbol():
    """The bug that motivated this module: the broker has no OILUSD."""
    assert resolve_symbol("OILUSD") == "USOIL"


def test_aliases_are_case_and_whitespace_insensitive():
    for raw in ("oilusd", "OILUSD", "  OilUsd  "):
        assert resolve_symbol(raw) == "USOIL"


def test_known_passthrough_symbols_are_unchanged():
    for sym in ("BTCUSD", "XAUUSD", "XAGUSD", "EURUSD", "USOIL"):
        assert resolve_symbol(sym) == sym


def test_unknown_symbol_is_uppercased_not_aliased():
    assert resolve_symbol("gbpusd") == "GBPUSD"


def test_empty_and_none_symbols_return_what_they_were_given():
    """Callers pass optional symbols; resolve_symbol must not explode on them."""
    assert resolve_symbol("") == ""
    assert resolve_symbol(None) is None


def test_custom_alias_table_overrides_the_default():
    assert resolve_symbol("OILUSD", aliases={"OILUSD": "UKOIL"}) == "UKOIL"


def test_every_alias_target_is_a_real_exness_symbol():
    """Guards against someone aliasing to a symbol the broker does not list."""
    known = {"USOIL", "UKOIL"}
    for target in SYMBOL_ALIASES.values():
        assert target in known, f"alias target {target} is not an Exness symbol"


def test_fallback_symbol_is_one_the_broker_actually_has():
    assert FALLBACK_SYMBOL in {"USOIL", "UKOIL"}


def test_canonical_symbols_are_all_resolvable():
    for sym in CANONICAL_SYMBOLS:
        assert resolve_symbol(sym) in {"BTCUSD", "XAUUSD", "USOIL", "XAGUSD"}


# ---------------------------------------------------------------------------
# filling modes
# ---------------------------------------------------------------------------
def test_filling_mode_3_prefers_fok_then_ioc():
    """BTCUSD returns 3 live; FOK and IOC both filled (10009), RETURN did not."""
    assert filling_candidates(3)[:2] == [0, 1]


def test_filling_mode_1_prefers_ioc():
    """XAUUSD/XAGUSD/USOIL/EURUSD return 1 live."""
    assert filling_candidates(1)[0] == 1


def test_filling_mode_0_prefers_fok():
    assert filling_candidates(0)[0] == 0


def test_filling_mode_2_prefers_return():
    assert filling_candidates(2)[0] == 2


def test_unknown_filling_mode_still_yields_a_valid_candidate_set():
    for mode in (None, 7, 99):
        cands = filling_candidates(mode)
        assert cands, "must never return an empty candidate list"
        assert all(c in (0, 1, 2) for c in cands), "only 0/1/2 are valid fill types"


def test_candidate_lists_are_never_empty_and_always_start_with_a_valid_type():
    """An empty list would make callers skip the symbol entirely."""
    for mode in (0, 1, 2, 3, None):
        cands = filling_candidates(mode)
        assert len(cands) >= 1
        assert cands[0] in (0, 1, 2)


def test_every_candidate_list_is_a_permutation_of_the_valid_set():
    for mode in (0, 1, 2, 3, None):
        assert sorted(filling_candidates(mode)) == [0, 1, 2]


def test_retryable_retcodes_cover_the_observed_failures():
    """10030 was seen live; 10027/10028 are the sibling 'bad fill mode' codes."""
    assert set(RETRYABLE_FILL_RETCODES) == {10030, 10027, 10028}


def test_bitmask_allows_is_documented_as_misleading_for_enum_values():
    """value 1 means IOC, so bit 1 is NOT 'the FOK bit' — pin the trap."""
    assert bitmask_allows(1, 0) is True      # naive bitmask reading
    assert filling_candidates(1)[0] == 1     # correct reading: IOC first


def test_bitmask_allows_returns_false_for_none():
    assert bitmask_allows(None, 0) is False


def test_describe_filling_names_every_live_mode():
    assert describe_filling(0) == "FOK"
    assert describe_filling(1) == "IOC"
    assert describe_filling(2) == "RETURN"
    assert "FOK" in describe_filling(3) and "IOC" in describe_filling(3)


def test_describe_filling_handles_an_unexpected_value():
    assert describe_filling(42).startswith("unknown")
