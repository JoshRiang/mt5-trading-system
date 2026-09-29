"""Live news loop: poll FF calendar + Investing RSS every 5min.

- Analyze via NewsAnalyzer.analyze_batch
- Update 6-var StateMachine + upsert_asset_state (with per-cycle decay)
- Log each item to news_log (dedupe by headline+symbol)
- risk_event on high-impact |impact| >= 0.7 (bot_tag='news_loop')
- Logs to logs/news_loop_*.log
- Never logs credentials.
"""
import sys
import time
import logging
import sqlite3
import traceback
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.news_analyzer import NewsAnalyzer
from core.state_machine import StateMachine
from core.storage import init_db, log_news_analysis, upsert_asset_state
from core.erp import init_erp, risk_event

POLL_SECONDS = 300
HIGH_IMPACT = 0.7
BOT_TAG = "news_loop"
DB_PATH = ROOT / "data" / "trading_bot.db"

logfile = ROOT / "logs" / f"news_loop_{datetime.now():%Y%m%d_%H%M%S}.log"
logfile.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [news_loop] %(levelname)s: %(message)s",
    handlers=[logging.FileHandler(str(logfile)), logging.StreamHandler()],
)
log = logging.getLogger("news_loop")


def load_states(sm: StateMachine):
    # Raw query (no PARSE_DECLTYPES): storage writes ISO 'T' timestamps
    # which sqlite3's TIMESTAMP converter cannot parse.
    try:
        c = sqlite3.connect(str(DB_PATH))
        c.row_factory = sqlite3.Row
        for d in c.execute("SELECT * FROM asset_state").fetchall():
            s = sm.get_or_create(d["symbol"])
            for k in ("bullish_minutes", "bullish_hours", "bullish_daily",
                      "bearish_minutes", "bearish_hours", "bearish_daily",
                      "last_news_impact"):
                if k in d.keys() and d[k] is not None:
                    setattr(s, k, float(d[k]))
        c.close()
    except Exception as e:
        log.warning(f"load_states failed: {e}")


def seen_in_db(headline: str, symbol: str) -> bool:
    try:
        c = sqlite3.connect(str(DB_PATH))
        r = c.execute(
            "SELECT 1 FROM news_log WHERE headline=? AND symbol=? LIMIT 1",
            (headline, symbol)).fetchone()
        c.close()
        return r is not None
    except Exception:
        return False


def persist_state(sm: StateMachine, symbol: str):
    s = sm.get_or_create(symbol)
    upsert_asset_state(
        symbol,
        bullish_minutes=s.bullish_minutes, bullish_hours=s.bullish_hours,
        bullish_daily=s.bullish_daily, bearish_minutes=s.bearish_minutes,
        bearish_hours=s.bearish_hours, bearish_daily=s.bearish_daily,
        last_news_impact=s.last_news_impact)


def run_cycle(analyzer: NewsAnalyzer, sm: StateMachine):
    ff = analyzer.fetch_forexfactory_rss()
    inv = analyzer.fetch_investingcom_rss()
    log.info(f"fetched FF={len(ff)} Investing={len(inv)}")
    items = ff + inv
    if not items:
        log.warning("no news items fetched this cycle")
        return {"symbols": {}, "new_rows": 0, "risk_events": 0}

    analysis = analyzer.analyze_batch(items)
    # decay existing state once per cycle so old news fades
    sm.apply_decay_all()

    new_rows, risk_n = 0, 0
    for symbol, data in analysis.items():
        impact = float(data.get("total_impact", 0.0))
        if impact != 0:
            sm.update_from_news(symbol, impact, "daily")
        try:
            persist_state(sm, symbol)
        except Exception as e:
            log.warning(f"upsert_asset_state {symbol} failed: {e}")
        for it in data.get("items", []):
            hl, src, imp = it["headline"], it["source"], float(it["impact"])
            if seen_in_db(hl, symbol):
                continue
            try:
                log_news_analysis(source=src, headline=hl, symbol=symbol,
                                  impact_score=imp, variable_updates={"daily": imp})
                new_rows += 1
            except Exception as e:
                log.warning(f"log_news_analysis failed: {e}")
            if abs(imp) >= HIGH_IMPACT:
                try:
                    risk_event(str(DB_PATH), "news_high_impact",
                               f"{symbol} {imp:+.2f} [{src}] {hl[:160]}", BOT_TAG)
                    risk_n += 1
                except Exception as e:
                    log.warning(f"risk_event failed: {e}")
    # persist decayed states for symbols with no news too
    for symbol in list(sm.states.keys()):
        if symbol not in analysis:
            try:
                persist_state(sm, symbol)
            except Exception:
                pass
    log.info(f"cycle done: symbols={list(analysis.keys())} new_news_rows={new_rows} risk_events={risk_n}")
    for symbol, data in analysis.items():
        log.info(f"  {symbol}: total_impact={data['total_impact']:+.2f} n={data['news_count']}")
        for it in data.get("items", [])[:3]:
            log.info(f"    [{it['source']}] {it['impact']:+.2f} {it['headline'][:120]}")
    return {"symbols": {k: {"total_impact": v["total_impact"], "news_count": v["news_count"]} for k, v in analysis.items()},
            "new_rows": new_rows, "risk_events": risk_n}


def main():
    once = "--once" in sys.argv
    init_db()
    init_erp()
    analyzer = NewsAnalyzer()
    sm = StateMachine()
    load_states(sm)
    log.info(f"news_loop start (poll={POLL_SECONDS}s, once={once})")
    try:
        risk_event(str(DB_PATH), "bot_start", "news_loop started", BOT_TAG)
    except Exception as e:
        log.warning(f"bot_start event failed: {e}")
    while True:
        try:
            run_cycle(analyzer, sm)
        except Exception:
            log.error("cycle exception:\n" + traceback.format_exc())
        if once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
