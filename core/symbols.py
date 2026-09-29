"""Symbol aliasing + per-symbol filling-mode resolution — STDLIB ONLY.

Single source of truth for two things the bot got wrong in several places:

1. **Symbol aliases.** The bot's config/news/strategy layer speaks ``OILUSD``;
   the broker only knows ``USOIL`` (verified live: ``symbol_info("OILUSD")`` is
   ``None`` while ``USOIL`` resolves with live ticks). Every symbol that crosses
   the MT5 boundary MUST go through :func:`resolve_symbol` — a missing alias
   silently degrades to "no such symbol" (no trade, or worse, a rejected order).

2. **Filling mode.** ``symbol_info(sym).filling_mode`` is an ENUM value
   (0=FOK, 1=IOC, 2=RETURN), *not* a bitmask of allowed types. VERIFIED LIVE on
   Exness-MT5Trial6 (2026-09-29) with real order_send calls:
     * BTCUSD ``filling_mode=3`` -> FOK (10009 ok), IOC (10009 ok),
       RETURN (10030 Unsupported filling mode).
     * XAUUSD/XAGUSD/USOIL/EURUSD ``filling_mode=1`` -> FOK (10009 ok),
       IOC (10030 unsupported).
   So the value 3 is the broker's "all market fills allowed" marker, and
   passing it straight through as ``type_filling`` is an INVALID request
   (only 0/1/2 are valid). :func:`filling_candidates` therefore returns a
   per-symbol PRIORITISED LIST of concrete, valid fill types derived from the
   enum, and callers fall back down the list while treating retcodes
   10030/10027/10028 as retryable.

NEVER hardcode a single fill type: one symbol rejecting it kills that symbol's
trading (this was the original 10030 bug on BTCUSD).
"""

from typing import List, Optional, Sequence

# ---------------------------------------------------------------------------
# Symbol aliases: bot/config name -> real broker symbol
# ---------------------------------------------------------------------------
SYMBOL_ALIASES = {
    "OILUSD": "USOIL",   # bot says OILUSD, Exness MT5 only has USOIL
    "WTIUSD": "USOIL",
    "BRENTUSD": "UKOIL",
}

# Symbol we substitute when a caller asks for something that does not resolve.
FALLBACK_SYMBOL = "USOIL"


def resolve_symbol(symbol: str, aliases: Optional[dict] = None) -> str:
    """Return the broker symbol for a bot-side ``symbol`` (case-insensitive)."""
    if not symbol:
        return symbol
    table = aliases if aliases is not None else SYMBOL_ALIASES
    key = str(symbol).strip().upper()
    return table.get(key, key)


# Canonical bot-side names (the news analyzer + config speak these).
CANONICAL_SYMBOLS = ["BTCUSD", "XAUUSD", "OILUSD", "XAGUSD"]


# ---------------------------------------------------------------------------
# Filling modes
# ---------------------------------------------------------------------------
# Retcodes that mean "this fill type is not acceptable — try another".
RETRYABLE_FILL_RETCODES = (10030, 10027, 10028)


def filling_candidates(filling_mode: Optional[int]) -> List[int]:
    """Valid ``ORDER_FILLING_*`` values to try, best-first, for a symbol.

    ``filling_mode`` is the raw ``symbol_info(...).filling_mode`` enum:
      0 -> FOK only
      1 -> IOC (FOK also accepted by this broker in practice)
      2 -> RETURN only
      3 -> broker's "market fills allowed" marker (BTCUSD): FOK and IOC both
           verified working, RETURN rejected -> prefer FOK, then IOC.
    Unknown / None -> the full valid set, safest-first.
    """
    if filling_mode == 0:                 # FOK
        return [0, 1, 2]
    if filling_mode == 1:                 # IOC (FOK verified accepted too)
        return [1, 0, 2]
    if filling_mode == 2:                 # RETURN
        return [2, 1, 0]
    if filling_mode == 3:                 # market-fills marker: FOK+IOC work
        return [0, 1, 2]
    return [0, 1, 2]


def bitmask_allows(filling_mode: Optional[int], fill_type: int) -> bool:
    """Kept for callers that genuinely want bitmask semantics.

    NOTE: for the real broker this returns misleading results (value 1 means
    IOC, not "FOK bit set"), so prefer :func:`filling_candidates`.
    """
    if filling_mode is None:
        return False
    return bool(int(filling_mode) & (1 << int(fill_type)))


def describe_filling(filling_mode: Optional[int]) -> str:
    """Human-readable fill-mode description for startup logs."""
    names = {0: "FOK", 1: "IOC", 2: "RETURN", 3: "FOK+IOC (market)"}
    return names.get(filling_mode, f"unknown({filling_mode})")
