# mt5-trading-system

A MetaTrader 5 algorithmic trading bot — the engine, the research harness, and an honest account of the session where it lost **82.3% of its account in 21 hours**.

This repository exists because the post-mortem is the interesting part. The bot is a working, tested system: it connects to a live MT5 terminal, sizes positions with a Kelly criterion, manages trailing stops on live ticks, and writes every fill to a SQLite ledger. It also traded with a risk model that could not work, and the ledger proves it line by line.

**This was a demo account.** No real money was ever at risk. The numbers below are from the bot's own database, and `analysis/trade_forensics.py` reproduces every one of them from that database — you can re-run it yourself.

---

## The headline

| Metric | Value |
|---|---|
| Session | 2026-09-21 04:14 → 2026-09-22 01:04 UTC (20h 50m) |
| Closed trades | 696 |
| Equity | 565.34 → 100.17 |
| **Max drawdown** | **82.32%** |
| Win rate | 50.43% |
| Average win / average loss | +1.2978 / −2.6379 |
| **Realized payoff (b)** | **0.492** |
| Config *intended* payoff | 3.0 (RR 1:3) |
| **Breakeven win rate at realized b** | **67.03%** |
| Actual win rate | 50.43% |
| Net P&L | −454.56 USD |
| Expectancy | −0.6531 USD / −0.2401 R per trade |

Read those two rows together: the strategy needed a **67%** win rate to break even at the payoff it actually achieved, and delivered **50%**. The gap is the entire loss. A 50/50 coin-flip win rate is not a failure — it is a failure *given a payoff of 0.49*, which is half of the 1.0 you get from pure chance with symmetric exits.

---

## Why it lost: five findings, all measured

### 1. The Stop Was Inside the Noise

The stop distance was **0.06% of price** — and one minute of price action is wider than that:

| Symbol | Median M1 bar range | vs the 0.06% stop |
|---|---|---|
| XAUUSD | 0.0424% | 0.71× |
| BTCUSD | 0.0528% | 0.88× |
| USOIL | 0.0816% | **1.36×** |

The bot polled the terminal once per second (`TICK_POLL_SEC = 1.0`). It could not track a stop that a single minute's noise crosses. Of the 240 trades that died as `BROKER_CLOSE`, **235 (97.9%) exited at the exact price level of the stop the bot had placed** — the broker's stop trigger won the race, the poll loop never got to evaluate its own exit condition, and every one of those trades was recorded with a reason the engine never assigned.

That is also why the exit-reason table looks so strange at first:

| Exit Reason | n | % | Win Rate | Net USD | Avg USD |
|---|---|---|---|---|---|
| TRAIL | 324 | 46.6% | 94.8% | **+401.05** | +1.238 |
| BROKER_CLOSE | 240 | 34.5% | 1.2% | **−716.93** | −2.987 |
| TIME | 104 | 14.9% | 39.4% | −48.08 | −0.462 |
| SL | 28 | 4.0% | 0.0% | −90.60 | −3.236 |

The trailing stop was a genuinely good exit: 94.8% of the trades it closed were winners. It made +401 USD. The stop-loss exits lost −808 USD across the two buckets that represent them. The bot's only profitable behaviour was letting winners run into a trail; its stops were simply too tight to survive contact with the market.

### 2. The 1:3 Reward-to-Risk Ratio Was Never Real

The Kelly sizing was built on `b = 3.0` — the config's `tp_mult`. The ledger's realized payoff was **b = 0.492**:

| Symbol | n | Win Rate | Net USD | Expectancy R |
|---|---|---|---|---|
| BTCUSD | 355 | 52.1% | −191.84 | −0.2251 |
| XAUUSD | 193 | 50.8% | −88.57 | −0.2281 |
| USOIL | 145 | 45.5% | −174.40 | −0.2985 |
| EURUSD | 3 | 66.7% | +0.25 | +0.0373 |

The reason is the trailing stop. `TRAIL_TRIG_PCT = 0.00025` and `TRAIL_DIST_PCT = 0.00012` arm a trail after a **0.025%** favourable move — less than half the 0.06% stop distance — and trail **0.012%** behind. So the exit was structurally capped: the trade had to move 0.025% before the trail armed, and then the trail locked in roughly a third of the stop distance. Measured on the real ledger, the median `TRAIL` exit captured **0.0192% of price, 0.32× the stop**.

Out of 351 winners, **exactly one** reached +3R. Thirty-four reached +1R. The 1:3 target was almost never touched — the trail was taking the trade off the table first. The R-multiple distribution is what a 1:3 design looks like when the trail runs the show:

```
min −3.396   p5 −1.370   p25 −1.007   median 0.008   p75 0.343   p95 0.983   max 3.01
trades ≤ −1R: 183      trades ≥ +1R: 34      trades ≥ +2R: 5
```

183 full-R losses against 34 full-R wins. That is not a 1:3 strategy with a bad patch; that is a strategy whose upside was amputated at the exit.

### 3. Spread Was 0.67× the Entire Net Loss

The strategy sized positions as if fills were free. They were not:

| Symbol | Median spread as % of the 1R stop | Spread paid (sum) |
|---|---|---|
| USOIL | **24.87%** | +144.26 USD |
| BTCUSD | 13.50% | +123.24 USD |
| XAUUSD | 6.96% | +34.81 USD |
| | | **+302.31 USD total** |

302.31 USD of spread against a 454.56 USD net loss. The bot paid **two thirds of its entire loss in spread alone** — every round trip at a 0.06% stop donated 7% to 25% of its risk budget to the broker before the trade even started.

The research harness reproduces this independently, and cleanly separates signal from cost. On the same 60,000-bar M1 history, the live bot's exact configuration:

| Symbol | n | WR | exp_R **gross** | exp_R **net** | Avg spread as % of R |
|---|---|---|---|---|---|
| XAUUSD | 3750 | 58.2% | +0.1043 | +0.0346 | 6.96% |
| BTCUSD | 3464 | 48.9% | +0.0614 | **−0.0867** | 14.81% |
| USOIL | 3715 | 42.4% | −0.0017 | **−0.2711** | 26.94% |
| XAGUSD | 3551 | 33.6% | +0.0366 | **−0.5053** | 54.19% |

The signal had positive gross expectancy on three of four symbols. It was negative net on three of four. **The stop was so tight that the spread was the edge.**

### 4. The Risk Cap Was Not Enforced

`RISK_PCT = 0.01` — 1% of equity per trade. Measured against the equity snapshot at the moment of each trade, using the position the broker actually filled:

| | Median | Max | Trades over 1% |
|---|---|---|---|
| What the engine intended (`risk_amount`) | 0.999% | 1.027% | 299 / 696 (43.0%) |
| What the position really carried | **1.019%** | **2.560%** | **385 / 696 (55.3%)** |

The engine's own arithmetic was fine. The gap is **lot granularity**: MT5 quotes lots on a 0.01 grid, and the engine rounded to the nearest step. Median lot/ideal ratio was 1.021, worst case 2.61×.

The effect compounds as the account shrinks. XAUUSD with a 0.01-lot minimum:

| Time | Equity | Lot | Risk USD | % of Equity |
|---|---|---|---|---|
| 04:14 | 565.61 | 0.02 | 5.23 | 0.93% |
| 20:33 | 220.46 | **0.01** (floor) | 2.60 | 1.18% |
| 01:04 | 105.19 | **0.01** (floor) | 2.69 | **2.56%** |

At 565 USD the 1% cap was achievable. At 105 USD the *smallest possible position* risked 2.56%. The bot could not reduce risk below a hard floor, so as it lost it bet relatively more. Worst offenders were all XAUUSD at 2.36–2.56% of equity.

### 5. The Daily Loss Cap Fired 58 Times and Stopped Nothing

`daily_cap` is supposed to halt trading after 1% of equity is lost in a day. The event was written to the ledger **58 times** between 07:14 and 01:04:

```
07:14:30 daily_cap 286.06
19:16:28 daily_cap 273.95
19:26:08 daily_cap 270.77
19:31:07 daily_cap 264.95
19:34:24 daily_cap 260.45
19:40:00 daily_cap 255.44
...
01:04:05 daily_cap  (equity floor hit 43 seconds later)
```

Every one of the 58 was followed by more trading. **373 of the 696 trades — 54% of the entire session — happened after the first cap had already fired.** The breaker reported the breach and let the bot keep going; it was an alarm, not a stop.

---

## What actually worked

Worth being precise, because the fix follows from it:

- **The signal had a real edge.** Gross expectancy was positive on XAUUSD (+0.104 R/trade over 3750 trades), BTCUSD (+0.061) and XAGUSD (+0.037). The entry logic was not the problem.
- **The trailing stop was profitable.** 94.8% win rate, +401 USD, the only bucket in the black.
- **The ledger is complete and trustworthy.** 696 trades, 1242 equity snapshots, 1163 order-blotter rows, and enough columns to reconstruct every failure above without a single broker statement.

---

## What the harness says the fix is

The research grid re-runs the *same signal* with wider stops on the same data. Ten of 72 cells earn a bootstrap 95% CI that excludes zero, and nine of those ten share one change — **move the stop out of the noise**:

| Variant | Symbol | n | WR | Spread as % of R | exp_R net | 95% CI | Max DD |
|---|---|---|---|---|---|---|---|
| live config, SL 0.06% | XAUUSD | 3750 | 58.2% | 6.96% | +0.0346 | [+0.0086, +0.0600] | 32.5% |
| live config, SL 0.06% | BTCUSD | 3464 | 48.9% | 14.81% | −0.0867 | [−0.1221, −0.0498] | 97.0% |
| live config, SL 0.06% | USOIL | 3715 | 42.4% | 26.94% | −0.2711 | [−0.3102, −0.2322] | 100.0% |
| **SL 0.3%** TP3× | XAUUSD | 1240 | 88.2% | **1.39%** | **+0.0355** | [+0.0197, +0.0512] | **6.7%** |
| **SL 0.3%** TP3× | BTCUSD | 1213 | 87.1% | **2.96%** | **+0.0600** | [+0.0389, +0.0811] | **5.1%** |
| **SL 0.3%** TP3× | USOIL | 1236 | 84.9% | **5.39%** | **+0.0648** | [+0.0414, +0.0875] | **8.8%** |
| **SL 0.5%** TP3× | XAUUSD | 773 | 91.2% | **0.84%** | +0.0172 | [+0.0026, +0.0313] | 4.6% |
| **SL 0.5%** TP3× | BTCUSD | 775 | 93.0% | **1.77%** | +0.0489 | [+0.0320, +0.0670] | 3.1% |
| **SL 0.5%** TP3× | USOIL | 776 | 89.6% | **3.24%** | +0.0426 | [+0.0234, +0.0616] | 3.8% |

Widening the stop 5× cuts the spread's share of risk by 5×, turns three of four symbols from negative to positive net expectancy, and collapses max drawdown from 97–100% (account destroyed) to **3–9%**.

Two honest caveats, because the grid is not a victory lap:

- **The random-entry control is flat.** `f) random SL0.2% TP2x` lands at exp_R_net between −0.15 and +0.02 with CIs spanning zero on every symbol. A random entry with the same exit machinery performs about as well as the real signal. The measured "edge" in the wide-stop variants is largely **the exit geometry and the spread**, not the entry logic — treat the signal's contribution as unproven until a null-control comparison is run per variant.
- **These are in-sample results on one 60,000-bar window.** No walk-forward, no out-of-sample holdout, no live confirmation. `e4) donchian50 H4` posts the highest expectancy in the grid (+0.56 R) on 9–13 trades — a textbook small-sample artifact, and the harness labels it `INCONCLUSIVE (n=10)` rather than letting it through.

The harness is built so a result cannot pass by accident: it charges exactly one spread per round trip, executes each decision at the **next** bar's open, assumes the stop fills first when a bar touches both stop and target, and refuses the `EDGE` label unless the 95% bootstrap CI excludes zero. `research/test_engine.py` tests that machinery directly (13 assertions on fill prices, spread accounting, lookahead, determinism, and a random-entry control).

---

## Reproducing every number

```bash
# 1. The post-mortem, from the bot's own ledger
python analysis/trade_forensics.py            # documented session (closed < 2026-09-23)
python analysis/trade_forensics.py --all      # the whole ledger
python analysis/trade_forensics.py --json out.json

# 2. The tests (needs pytest; core/ and analysis/ are stdlib-only)
python -m pytest tests/ -v                    # 82 tests
python research/test_engine.py                # 33 simulator fill-model assertions

# 3. The backtest grid (needs numpy + the exported MT5 CSVs, ~200 MB)
python research/edge_harness.py --json research/results.json
```

`research/results.json` **is committed** — it is the raw output behind the harness table above. The 200 MB of MT5 bar/tick CSVs it reads are not; regenerate them with `research/export_data.py` (needs the MT5 terminal) or drop your own exports into `research/data/`. Note that `research/data` in a fresh clone does not exist until you do one of those two things.

The forensics script is stdlib-only and reads `data/trading_bot.db`, which **is** committed (602 KB) so the numbers are checkable without a broker connection. It contains no credentials — the CI job enforces that, and the DB was scanned for the account login and API keys before being committed.

---

## Layout

```
core/            engine library — stdlib only, no MT5 import
  kelly.py         Kelly sizing, momentum signal, timeout decisions
  symbols.py       broker symbol aliasing + filling-mode resolution
  erp.py           the SQLite ledger (trades, blotter, equity, risk events)
  risk_manager.py  daily cap / circuit breaker
  mt5_client.py    the only module that talks to the MetaTrader5 package
engine/          the two live bots (kelly_momentum, gamble_multi_v3)
strategies/      YAML strategy configs
analysis/        trade_forensics.py — the post-mortem
research/        edge_harness.py + harness_core.py + test_engine.py
tests/           pytest suite (symbols, Kelly, forensics)
data/            trading_bot.db (the real ledger, committed)
docs/            POSTMORTEM.md — the long-form writeup
```

---

## Two live bugs this repo documents

Both were found by testing against the real broker rather than trusting the docs:

1. **`OILUSD` does not exist on Exness.** The config, news and strategy layers all spoke `OILUSD`; `symbol_info("OILUSD")` returns `None`. Every oil trade needs to go through `core.symbols.resolve_symbol()`, which maps it to `USOIL`.

2. **`filling_mode` is an enum, not a bitmask.** Exness returns `3` for BTCUSD and `1` for XAUUSD/XAGUSD/USOIL/EURUSD. Passing that value straight through as `type_filling` is an invalid request — the original `10030 Unsupported filling mode` failure. `core.symbols.filling_candidates()` returns a per-symbol prioritised list of valid fill types instead.

---

## Setup

```bash
cp .env.example .env && chmod 600 .env   # fill in MT5_LOGIN / MT5_PASSWORD / MT5_SERVER / MT5_PATH
python -m pip install -r requirements.txt
```

The bot runs under Wine against a Windows MT5 terminal, driven by the official `MetaTrader5` Python package. `MT5_PATH` is **required** in practice — attaching without an explicit terminal path returns `(-10005, 'IPC timeout')` even with valid credentials.

`ENVIRONMENT=demo` in `.env` is the default. The engine refuses to trade a live account unless explicitly overridden on the command line.

---

## Honest limitations

- **One session, one account, one regime.** 696 trades over 21 hours on a demo account with a few hundred dollars of equity. Nothing here generalises to a different account size, and the lot-granularity failure in particular is a small-account artifact — it would look different at 50,000 USD.
- **The harness is in-sample.** No walk-forward validation was run. The wide-stop variants are hypotheses with a confidence interval, not validated strategies.
- **The random-entry control matches the signal.** The entry logic's contribution to the measured edge is unproven.
- **Not investment advice, and not a working strategy.** This is a post-mortem of a system that failed, published so the failure is inspectable.

---

## License

MIT — see [LICENSE](LICENSE).