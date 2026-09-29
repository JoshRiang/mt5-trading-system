"""MetaTrader 5 execution client — OFFICIAL `MetaTrader5` package only.

Execution path: official MetaTrader5 Python package
(https://www.mql5.com/en/docs/python_metatrader5) talking to a local
MT5 terminal (Exness MT5) via ``mt5.initialize(login, password, server)``.

Linux deployment model (this server is headless Ubuntu):
  The official ``MetaTrader5`` pip package is **Windows-only** — it cannot be
  imported by a native Linux CPython. The supported path on this box is
  **Windows-Python-under-Wine** (VERIFIED WORKING 2026-09-20, build 6180):

    1. Wine STABLE (wine-11.0, /opt/wine-stable/bin/wine) + Xvfb ``:20``
       (``Xvfb :20 &`` + ``export DISPLAY=:20``). CRITICAL: the Python
       process and the terminal MUST share the same Wine build AND the same
       WINEPREFIX (same wineserver) or IPC fails with (-10005, 'IPC timeout').
       This box has TWO prefixes: ``~/.wine`` (wine-stable, has the live
       terminals + Windows Python) and ``~/.mt5`` (wine-staging, from the
       MetaQuotes mt5linux.sh script — its terminals are NOT reachable from
       the .wine Python).
    2. Logged-in Exness MT5 terminal under Wine, prefix ``~/.wine``:
       ``C:\\Program Files\\Exness MT5 6180\\terminal64.exe``
       (started with ``/skipupdate`` on DISPLAY=:20).
    3. Windows Python 3.11 under the SAME prefix + ``pip install
       MetaTrader5 PyYAML``.
    4. Run the bot with Windows Python under Wine so the import works::

         set -a; . .env; set +a
         WINEPREFIX=~/.wine DISPLAY=:20 /opt/wine-stable/bin/wine \
           "C:\\users\\josh\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" \
           "Z:\\home\\josh\\trading_bot\\main.py"

  Verified 2026-09-20: ``mt5.initialize(path=<Exness 6180 terminal>,
  login, password, server, timeout=...)`` returns True with REAL
  account_info (login / server / balance / currency) + live ticks.
  Known-good and known-BAD (same box, same creds):
    OK   path=C:\\Program Files\\Exness MT5 6180\\terminal64.exe -> True
    FAIL no path (attach) -> (-10005, 'IPC timeout')
    FAIL path=C:\\Program Files\\MetaTrader 5\\terminal64.exe -> -10005
  i.e. the Exness-terminal path argument is REQUIRED, not optional.

  A native-Linux ``python3`` will ALWAYS hit the ``ImportError`` branch below
  and the client runs in PAPER mode (safe empty payloads, bot loop never
  crashes). That is by design, not a bug: live trading requires the
  Wine + Windows-Python path above.

Exposes the SAME interface as the old bridge so main.py needs no changes:
  account: get_account_info / get_balance / get_positions / get_orders
  market:  get_instruments / get_candles / get_ticks
  trading: place_order / close_position / modify_position
  stream:  connect_ws / start_ws
  history: get_trade_history

Credentials are NEVER hardcoded — pass via env (.env, chmod 600):
    MT5_LOGIN=... MT5_PASSWORD=... MT5_SERVER=... [MT5_PATH=...] [ENVIRONMENT=demo]

No cloud bridges, no third-party wrappers anywhere in this file —
only the official MetaTrader5 package + PAPER fallback.
"""

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

# Official package — Windows-only. Under native Linux this ImportError is
# EXPECTED and the client degrades to PAPER mode (see _MT5_AVAILABLE).
try:
    import MetaTrader5 as mt5  # type: ignore

    _MT5_AVAILABLE = True
    _MT5_IMPORT_ERROR: Optional[str] = None
except ImportError as _e:  # pragma: no cover — normal on native Linux
    mt5 = None  # type: ignore
    _MT5_AVAILABLE = False
    _MT5_IMPORT_ERROR = str(_e)

try:
    from core.symbols import (SYMBOL_ALIASES, resolve_symbol, filling_candidates,
                              describe_filling, RETRYABLE_FILL_RETCODES)
except ImportError:  # run as a bare script / different sys.path
    from symbols import (SYMBOL_ALIASES, resolve_symbol, filling_candidates,
                         describe_filling, RETRYABLE_FILL_RETCODES)  # type: ignore

# Bot symbols -> Exness MT5 symbols. Oil differs: MT5 uses USOIL.
# AUTHORITATIVE: core.symbols.resolve_symbol is the single source of truth;
# this dict is kept for callers that read `client.symbol_map` directly.
DEFAULT_SYMBOL_MAP = {
    "BTCUSD": "BTCUSD",
    "XAUUSD": "XAUUSD",
    "OILUSD": "USOIL",
    "XAGUSD": "XAGUSD",
    "USOIL": "USOIL",
    "UKOIL": "UKOIL",
    "EURUSD": "EURUSD",
    **SYMBOL_ALIASES,
}

# Bot timeframes -> official mt5 TIMEFRAME_* constant names
# (resolved lazily via getattr so import-time never touches mt5).
TIMEFRAME_MAP = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
    "W1": "TIMEFRAME_W1",
}

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "mt5.yaml"

_MAGIC = 20260917
_DEV = 20  # max price deviation in points for market orders


def _load_yaml_config(path: Path) -> Dict:
    if yaml is None or not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _default_terminal_path() -> str:
    """Best-guess terminal path: the LOGGED-IN Exness terminal (verified IPC).

    Verified 2026-09-20: only the Exness 6180 terminal accepts official
    Python IPC; the vanilla MetaTrader 5 terminal and attach-without-path
    both give (-10005, 'IPC timeout'). MT5_PATH env / mt5.yaml
    terminal_path still override this default.
    """
    home = Path.home()
    candidates = [
        home / ".wine/drive_c/Program Files/Exness MT5 6180/terminal64.exe",
        home / ".wine/drive_c/Program Files/Exness MT5/terminal64.exe",
        home / ".wine/drive_c/Program Files (x86)/Exness MT5/terminal64.exe",
        home / ".wine/drive_c/Program Files/MetaTrader 5/terminal64.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return str(candidates[0])


class MT5Client:
    """MT5 execution client on the official MetaTrader5 package."""

    def __init__(
        self,
        login: Optional[str] = None,
        password: Optional[str] = None,
        server: Optional[str] = None,
        environment: str = "demo",
        symbol_map: Optional[Dict[str, str]] = None,
        config_path: Optional[str] = None,
        terminal_path: Optional[str] = None,
        timeout: int = 60000,
    ):
        cfg = _load_yaml_config(Path(config_path) if config_path else DEFAULT_CONFIG_PATH)
        mt5_cfg = cfg.get("mt5", {})

        def _resolve(value: Optional[str], cfg_key: str, env_key: str) -> Optional[str]:
            if value:
                return None if value.startswith("${") else value
            raw = mt5_cfg.get(cfg_key, "")
            if isinstance(raw, str) and raw.startswith("${") and raw.endswith("}"):
                return os.getenv(raw[2:-1], "") or None
            return raw or os.getenv(env_key) or None

        self.login = _resolve(login, "login", "MT5_LOGIN")
        try:
            self.login_int = int(self.login) if self.login else None
        except (TypeError, ValueError):
            self.login_int = None
        self.password = _resolve(password, "password", "MT5_PASSWORD")
        self.server = _resolve(server, "server", "MT5_SERVER")
        self.environment = environment or mt5_cfg.get("environment", "demo")
        _tp = terminal_path or mt5_cfg.get("terminal_path") or os.getenv("MT5_PATH")
        self.terminal_path = _tp if _tp else _default_terminal_path()
        self.timeout = timeout
        self.symbol_map = dict(DEFAULT_SYMBOL_MAP)
        self.symbol_map.update(mt5_cfg.get("symbol_map", {}))
        if symbol_map:
            self.symbol_map.update(symbol_map)

        # PAPER when the official package is missing (native Linux) or no creds.
        # NOTE: terminal reachability is checked lazily at call time; a client
        # constructed while the terminal is down reports per-call {"error": ...}
        # rather than flipping the paper flag silently.
        self.paper = not (_MT5_AVAILABLE and self.login and self.password and self.server)
        self._paper_reason = (
            ""
            if not self.paper
            else (
                f"official MetaTrader5 package not importable ({_MT5_IMPORT_ERROR}); "
                "run under Windows-Python-in-Wine for live."
                if not _MT5_AVAILABLE
                else "MT5_LOGIN/MT5_PASSWORD/MT5_SERVER not configured."
            )
        )
        self._lock = threading.Lock()
        self._initialized = False
        self._ws_callbacks: List[Callable] = []
        self._ws_running = False

    # ==================== CONSTRUCTORS ====================

    @classmethod
    def from_env(cls, **kwargs) -> Optional["MT5Client"]:
        """Build from env vars; returns None when MT5 credentials are absent."""
        if not (os.getenv("MT5_LOGIN") and os.getenv("MT5_PASSWORD") and os.getenv("MT5_SERVER")):
            return None
        return cls(**kwargs)

    # ==================== INTERNALS ====================

    def _paper(self, **payload) -> Dict:
        out = {"paper": True, "message": f"MT5Client in paper mode ({self._paper_reason})."}
        out.update(payload)
        return out

    def _mt5_symbol(self, symbol: str) -> str:
        """Authoritative bot-symbol -> broker-symbol resolution.

        Tries the (possibly YAML-overridden) symbol_map first, then the
        canonical alias table, so OILUSD always lands on USOIL no matter
        which config a caller came in through.
        """
        if not symbol:
            return symbol
        key = str(symbol).strip().upper()
        if key in self.symbol_map:
            return self.symbol_map[key]
        return resolve_symbol(key)

    def _filling_for(self, mt5_symbol: str) -> List[int]:
        """Concrete ORDER_FILLING_* values to try for ``mt5_symbol``, best-first.

        Derived from the symbol's live ``filling_mode``. Returns a list so
        callers can fall back; NEVER a single hardcoded type (that was the
        10030 bug). Falls back to FOK->IOC->RETURN when symbol_info is absent.
        """
        fm = None
        try:
            si = mt5.symbol_info(mt5_symbol) if mt5 is not None else None
            if si is not None:
                fm = getattr(si, "filling_mode", None)
        except Exception:
            fm = None
        cands = filling_candidates(fm)
        self._last_filling_mode = fm
        return cands

    def _filling_for_ticket(self, mt5_symbol: str, position: Dict) -> List[int]:
        """Fill types for CLOSING a position (prefer the symbol's own mode)."""
        return self._filling_for(mt5_symbol)

    def _timeframe_const(self, timeframe: str):
        name = TIMEFRAME_MAP.get(timeframe.upper(), "TIMEFRAME_M1")
        return getattr(mt5, name)

    def _ensure_connected(self) -> None:
        """mt5.initialize(login, password, server); idempotent. Raises on failure."""
        if self.paper or mt5 is None:
            raise RuntimeError(f"MT5Client has no live connection (paper mode: {self._paper_reason}).")
        with self._lock:
            if self._initialized:
                try:
                    if mt5.account_info() is not None:
                        return
                except Exception:
                    self._initialized = False
            kwargs: Dict = {
                "login": int(self.login_int if self.login_int is not None else self.login),  # type: ignore[arg-type]
                "password": self.password,
                "server": self.server,
                "timeout": self.timeout,
            }
            if self.terminal_path and Path(self.terminal_path).exists():
                kwargs["path"] = self.terminal_path
            ok = mt5.initialize(**kwargs)
            if not ok:
                code, msg = mt5.last_error()
                raise RuntimeError(f"mt5.initialize failed for {self.server}: [{code}] {msg}")
            self._initialized = True

    def shutdown(self) -> None:
        """mt5.shutdown() — release the terminal IPC connection."""
        if mt5 is not None:
            with self._lock:
                try:
                    mt5.shutdown()
                finally:
                    self._initialized = False

    def close(self) -> None:
        self.shutdown()

    # ==================== ACCOUNT ====================

    def get_account_info(self) -> Dict:
        """Get account information via mt5.account_info()."""
        if self.paper:
            return self._paper(balance=0.0, equity=0.0, margin=0.0, currency="USD")
        try:
            self._ensure_connected()
            info = mt5.account_info()
            if info is None:
                return {"error": f"account_info failed: {mt5.last_error()}"}
            d = info._asdict()
            return {
                "login": d.get("login"), "server": d.get("server"),
                "name": d.get("name"), "currency": d.get("currency"),
                "balance": d.get("balance"), "equity": d.get("equity"),
                "margin": d.get("margin"), "free_margin": d.get("margin_free"),
                "margin_level": d.get("margin_level"), "profit": d.get("profit"),
                "leverage": d.get("leverage"), "trade_allowed": d.get("trade_allowed"),
            }
        except Exception as e:
            return {"error": str(e)}

    def get_balance(self) -> Dict:
        """Get account balance."""
        info = self.get_account_info()
        if "error" in info:
            return info
        return {"balance": info.get("balance"), "equity": info.get("equity"),
                "currency": info.get("currency", "USD"),
                **({"paper": True} if info.get("paper") else {})}

    def get_positions(self, symbol: Optional[str] = None) -> Dict:
        """Get open positions via mt5.positions_get()."""
        if self.paper:
            return self._paper(positions=[])
        try:
            self._ensure_connected()
            ms = self._mt5_symbol(symbol) if symbol else None
            raw = mt5.positions_get(symbol=ms) if ms else mt5.positions_get()
            out = []
            for p in raw or ():
                d = p._asdict()
                out.append({
                    "ticket": d.get("ticket"), "symbol": d.get("symbol"),
                    "type": "BUY" if d.get("type") == mt5.POSITION_TYPE_BUY else "SELL",
                    "volume": d.get("volume"), "open_price": d.get("price_open"),
                    "current_price": d.get("price_current"), "sl": d.get("sl"),
                    "tp": d.get("tp"), "profit": d.get("profit"),
                    "comment": d.get("comment"), "magic": d.get("magic"),
                })
            return {"positions": out}
        except Exception as e:
            return {"error": str(e)}

    def get_orders(self, symbol: Optional[str] = None) -> Dict:
        """Get pending orders via mt5.orders_get()."""
        if self.paper:
            return self._paper(orders=[])
        try:
            self._ensure_connected()
            ms = self._mt5_symbol(symbol) if symbol else None
            raw = mt5.orders_get(symbol=ms) if ms else mt5.orders_get()
            out = []
            for o in raw or ():
                d = o._asdict()
                out.append({
                    "ticket": d.get("ticket"), "symbol": d.get("symbol"),
                    "type": d.get("type"), "type_time": d.get("type_time"),
                    "volume": d.get("volume_initial") or d.get("volume_current"),
                    "open_price": d.get("price_open"), "sl": d.get("sl"),
                    "tp": d.get("tp"), "comment": d.get("comment"),
                })
            return {"orders": out}
        except Exception as e:
            return {"error": str(e)}

    # ==================== MARKET DATA ====================

    def get_instruments(self) -> Dict:
        """Get available instruments via mt5.symbols_get()."""
        if self.paper:
            return self._paper(symbols=list(self.symbol_map.values()))
        try:
            self._ensure_connected()
            syms = mt5.symbols_get()
            return {"symbols": [s.name for s in (syms or ())]}
        except Exception as e:
            return {"error": str(e)}

    def get_candles(self, symbol: str, timeframe: str = "M1", count: int = 100) -> Dict:
        """Historical candles via symbol_select + mt5.copy_rates_from_pos()."""
        mt5_symbol = self._mt5_symbol(symbol)
        if self.paper:
            return self._paper(symbol=mt5_symbol, timeframe=timeframe.upper(), candles=[])
        try:
            self._ensure_connected()
            tf = self._timeframe_const(timeframe)
            if not mt5.symbol_select(mt5_symbol, True):
                return {"error": f"symbol_select failed for {mt5_symbol}: {mt5.last_error()}"}
            rates = mt5.copy_rates_from_pos(mt5_symbol, tf, 0, int(count))
            if rates is None:
                return {"error": f"copy_rates_from_pos failed for {mt5_symbol}: {mt5.last_error()}"}
            candles = [
                {"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]),
                 "tick_volume": int(r["tick_volume"]), "spread": int(r["spread"]),
                 "real_volume": int(r["real_volume"])}
                for r in rates
            ]
            return {"symbol": symbol, "timeframe": timeframe.upper(), "candles": candles}
        except Exception as e:
            return {"error": str(e)}

    def get_ticks(self, symbol: str, count: int = 100) -> Dict:
        """Recent ticks via mt5.copy_ticks_from_pos()."""
        mt5_symbol = self._mt5_symbol(symbol)
        if self.paper:
            return self._paper(symbol=mt5_symbol, ticks=[])
        try:
            self._ensure_connected()
            if not mt5.symbol_select(mt5_symbol, True):
                return {"error": f"symbol_select failed for {mt5_symbol}: {mt5.last_error()}"}
            ticks = mt5.copy_ticks_from_pos(mt5_symbol, 0, int(count), mt5.COPY_TICKS_ALL)
            if ticks is None:
                return {"error": f"copy_ticks_from_pos failed for {mt5_symbol}: {mt5.last_error()}"}
            return {"symbol": symbol, "ticks": [
                {"symbol": symbol, "time": int(t["time"]), "time_msc": int(t["time_msc"]),
                 "bid": float(t["bid"]), "ask": float(t["ask"]),
                 "volume": float(t.get("volume", 0.0)), "flags": int(t.get("flags", 0))}
                for t in ticks
            ][-int(count):]}
        except Exception as e:
            return {"error": str(e)}

    # ==================== TRADING ====================

    def _send(self, request: Dict) -> Dict:
        """order_send with per-symbol filling-mode fallback.

        The candidate fill types are derived from the REQUEST's own symbol via
        ``symbol_info(sym).filling_mode`` — never hardcoded, never the raw enum
        passed through (value 3 is invalid as a fill type and value 1 means IOC,
        not a bitmask). Retcodes 10030/10027/10028 mean "wrong fill type" and
        are retried down the candidate list; anything else stops immediately.
        """
        if mt5 is None:
            return {"error": "MetaTrader5 package unavailable"}
        sym = request.get("symbol")
        candidates = self._filling_for(sym) if sym else [0, 1, 2]
        request = dict(request)  # never mutate the caller's dict
        last = None
        for filling in candidates:
            request["type_filling"] = filling
            with self._lock:
                result = mt5.order_send(request)
            if result is None:
                last = {"error": f"order_send failed (no result): {mt5.last_error()}"}
                continue
            if result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                return {"order_id": result.order, "deal_id": result.deal,
                        "retcode": result.retcode, "comment": result.comment,
                        "price": result.price, "volume": result.volume,
                        "type_filling": filling}
            last = {"error": f"order_send retcode={result.retcode} ({result.comment})",
                    "retcode": result.retcode, "comment": result.comment,
                    "type_filling": filling}
            if result.retcode not in RETRYABLE_FILL_RETCODES:
                break
        return last or {"error": "order_send failed"}

    def place_order(self, symbol: str, direction: str, lot_size: float,
                    stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
                    comment: Optional[str] = None) -> Dict:
        """Market order via mt5.order_send(). direction: BUY or SELL."""
        mt5_symbol = self._mt5_symbol(symbol)
        action = direction.upper()
        if action not in ("BUY", "SELL"):
            return {"error": f"direction must be BUY or SELL, got {direction!r}"}
        if self.paper:
            return self._paper(action="place_order", symbol=mt5_symbol,
                               direction=action, lot_size=lot_size,
                               stop_loss=stop_loss, take_profit=take_profit)
        try:
            self._ensure_connected()
            if not mt5.symbol_select(mt5_symbol, True):
                return {"error": f"symbol_select failed for {mt5_symbol}: {mt5.last_error()}"}
            tick = mt5.symbol_info_tick(mt5_symbol)
            if tick is None:
                return {"error": f"no tick for {mt5_symbol}: {mt5.last_error()}"}
            price = tick.ask if action == "BUY" else tick.bid
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": mt5_symbol, "volume": float(lot_size),
                "type": mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL,
                "price": price, "deviation": _DEV, "magic": _MAGIC,
                "comment": comment or "trading-bot",
                "type_time": mt5.ORDER_TIME_GTC,
                # Derived from THIS symbol's filling_mode; _send walks the
                # candidate list on 10030. Never hardcode a single fill type.
                "type_filling": self._filling_for(mt5_symbol)[0],
            }
            if stop_loss:
                request["sl"] = float(stop_loss)
            if take_profit:
                request["tp"] = float(take_profit)
            out = self._send(request)
            out.update({"symbol": symbol, "direction": action})
            return out
        except Exception as e:
            return {"error": str(e)}

    def close_position(self, position_id: str) -> Dict:
        """Close a position by ticket via an opposing DEAL with position_id set."""
        if self.paper:
            return self._paper(action="close_position", position_id=position_id)
        try:
            self._ensure_connected()
            ticket = int(position_id)
            poss = mt5.positions_get(ticket=ticket)
            if not poss:
                return {"error": f"position {ticket} not found"}
            p = poss[0]._asdict()
            sym, vol = p["symbol"], p["volume"]
            is_buy = p["type"] == mt5.POSITION_TYPE_BUY
            tick = mt5.symbol_info_tick(sym)
            if tick is None:
                return {"error": f"no tick for {sym}: {mt5.last_error()}"}
            request = {
                "action": mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": sym,
                "volume": float(vol),
                "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "price": tick.bid if is_buy else tick.ask,
                "deviation": _DEV, "magic": _MAGIC, "comment": "trading-bot-close",
                "type_time": mt5.ORDER_TIME_GTC,
                # Close legs follow the position's own symbol filling_mode.
                "type_filling": self._filling_for(sym)[0],
            }
            out = self._send(request)
            out.update({"position_id": position_id})
            return out
        except Exception as e:
            return {"error": str(e)}

    def modify_position(self, position_id: str, stop_loss: Optional[float] = None,
                        take_profit: Optional[float] = None) -> Dict:
        """Modify position SL/TP via TRADE_ACTION_SLTP."""
        if self.paper:
            return self._paper(action="modify_position", position_id=position_id,
                               stop_loss=stop_loss, take_profit=take_profit)
        try:
            self._ensure_connected()
            ticket = int(position_id)
            poss = mt5.positions_get(ticket=ticket)
            if not poss:
                return {"error": f"position {ticket} not found"}
            p = poss[0]._asdict()
            request = {
                "action": mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": p["symbol"],
                "sl": float(stop_loss) if stop_loss else float(p["sl"]),
                "tp": float(take_profit) if take_profit else float(p["tp"]),
                "magic": _MAGIC,
            }
            out = self._send(request)
            out.update({"position_id": position_id})
            return out
        except Exception as e:
            return {"error": str(e)}

    # ==================== STREAMING ====================

    async def connect_ws(self, callback=None):
        """Poll latest tick per mapped symbol; invokes callback(dict) per tick.

        Same shape as the old bridge: tick dict {symbol, bid, ask, time}.
        Blocking poll loop (matches previous blocking-stream semantics).
        """
        import asyncio as _aio

        if callback:
            self._ws_callbacks.append(callback)
        if self.paper:
            print("MT5Client paper mode: price streaming disabled "
                  "(run under Windows-Python-in-Wine with MT5_* env vars "
                  "for live streaming).")
            await _aio.sleep(0)
            return
        self._ensure_connected()
        print(f"MT5 tick poll streaming {len(self.symbol_map)} symbols on {self.server}.")
        self._ws_running = True
        try:
            while self._ws_running:
                for bot_sym, mt5_sym in self.symbol_map.items():
                    try:
                        ticks = mt5.copy_ticks_from_pos(mt5_sym, 0, 1, mt5.COPY_TICKS_ALL)
                    except Exception as exc:
                        print(f"Tick poll {mt5_sym} failed: {exc}")
                        continue
                    if not ticks:
                        continue
                    t = ticks[-1]
                    data = {"symbol": bot_sym, "bid": float(t["bid"]),
                            "ask": float(t["ask"]), "time": int(t["time"])}
                    for cb in self._ws_callbacks:
                        try:
                            cb(data)
                        except Exception as exc:  # never let a callback kill the stream
                            print(f"WS callback error: {exc}")
                await _aio.sleep(1.0)
        finally:
            self._ws_running = False

    def stop_ws(self):
        self._ws_running = False

    def start_ws(self, callback=None):
        """Start streaming (blocking, same as before)."""
        import asyncio as _aio

        _aio.run(self.connect_ws(callback))

    # ==================== HISTORY ====================

    def get_trade_history(self, days: int = 30) -> Dict:
        """Deal history via mt5.history_deals_get()."""
        if self.paper:
            return self._paper(deals=[])
        try:
            self._ensure_connected()
            now = datetime.now(timezone.utc)
            deals = mt5.history_deals_get(now - timedelta(days=int(days)), now)
            if deals is None:
                return {"error": f"history_deals_get failed: {mt5.last_error()}"}
            return {"deals": [d._asdict() for d in deals]}
        except Exception as e:
            return {"error": str(e)}


# Alias so `from core.mt5_client import ExnessClient` keeps working during migration.
ExnessClient = MT5Client


if __name__ == "__main__":
    client = MT5Client()
    print(f"MT5Client initialized (paper={client.paper}, mt5_available={_MT5_AVAILABLE})")
    if client.paper:
        print(f"Paper reason: {client._paper_reason}")
        print("Live: install Wine + Exness MT5 + Windows-Python + MetaTrader5 pkg; "
              "set MT5_LOGIN, MT5_PASSWORD, MT5_SERVER; run under `wine python`.")
    else:
        print(client.get_account_info())
