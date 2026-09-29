# Post-mortem: how a 0.06% stop lost 82% of an account

Long-form writeup of the session summarised in the [README](../README.md). Every
number here comes from `data/trading_bot.db` and is reproduced by
`analysis/trade_forensics.py`, or from `research/results.json` and reproduced by
`research/edge_harness.py`. Nothing in this document is estimated.

---

## 1. The system

Two bots ran against the same Exness MT5 **demo** account, sharing a SQLite ledger:

| tag | what it is |
|---|---|
| `gamble_multi_v3` | fixed 1%-of-equity risk, seconds-EMA(3/8) + momentum-burst + Donchian breakout, SL 0.06% / TP 3× / 150s max hold |
| `kelly_momentum` | the same signal, sized with Kelly `f* = (p(b+1) − 1)/b` at `b = 3`, half-Kelly, floored at 0, capped at 1% equity |

Both used the same exit machinery: a stop at 0.06% of mid, a target at 0.18%, a
trailing stop that arms after 0.025% of favourable movement and trails 0.012%
behind, and a 150-second time stop. A 1%-of-equity daily loss cap was configured.
A shared equity floor of 100 USD would flatten everything.

Performance split by tag, from the ledger:

| tag | n | win rate | net USD | expectancy R |
|---|---|---|---|---|
| kelly_momentum | 375 | 50.1% | −177.93 | −0.2663 |
| gamble_multi_v3 | 321 | 50.8% | −276.63 | −0.2094 |

Neither was better. The Kelly sizing made no measurable difference, because the
input `b` was wrong (finding 2) and the output was capped anyway (finding 4).

---

## 2. The arithmetic of the loss

The whole failure fits in one table:

```
win rate                              50.43%
average win                          +1.2978
average loss                         −2.6379
realized payoff b                      0.492
config INTENDED RR                      1:3  (b=3)
breakeven WR at realized b            67.03%
net P&L                           −454.56 USD
expectancy per trade  −0.6531 USD / −0.2401 R
```

At `b = 0.492` you need a 67% win rate to break even, because
`p₀ = 1/(1+b)`. The bot won 50.43% of its trades — which is *exactly* what an
unbiased entry with symmetric exits should produce. The entry was not broken. The
exit geometry was: it converted a fair coin flip into a negative-expectancy bet by
paying more on losses than it collected on wins.

This is worth stating plainly because it is the most common way a systematic
strategy fails: **not by being wrong about direction, but by being wrong about the
shape of the outcome distribution.**

---

## 3. Finding 1 — the stop was inside the noise floor

The stop sat 0.06% from entry. The market's own noise, measured from the exported
M1 bars over the same 60,000-bar window:

| symbol | median M1 range | p90 M1 range | median range ÷ stop |
|---|---|---|---|
| XAUUSD | 0.0424% | 0.0860% | 0.71× |
| BTCUSD | 0.0528% | 0.1257% | 0.88× |
| USOIL | 0.0816% | 0.1675% | **1.36×** |

On USOIL, the median single minute of price action is *wider than the stop*. On
XAUUSD and BTCUSD the p90 bar is 1.4–2.1× the stop. A stop placed there is not a
risk limit; it is a guaranteed exit.

### Why it surfaced as `BROKER_CLOSE` and not `SL`

The engine detects its stop in software: it compares the current bid against
`entry − sl_move` once per second. But it also *sends* that stop to the broker as a
real `sl` on the order, and the broker's trigger fires the instant price touches —
no polling required. The broker always wins that race.

The ledger shows it directly. Of the 240 trades whose recorded reason is
`BROKER_CLOSE`:

- **235 (97.9%)** exited within 0.05% of the recorded `stop_loss` price
- **237 (98.8%)** exited on the adverse side of entry
- median `|exit − entry| / entry` = **0.0608%**, i.e. **1.014×** the 0.06% stop
- 165 of 240 landed within ±10% of exactly the stop distance

`BROKER_CLOSE` is not a third kind of exit. It is the stop-loss bucket, recorded
under a different label because the software's exit handler never ran — the
position was already gone when the poll loop looked. `reconcile_open.py` assigns
that label when it true-ups positions the bot's own shutdown left open, which is
why the bucket exists at all.

Consequence for the analysis: the true stop-loss cost is
`BROKER_CLOSE + SL = −716.93 + −90.60 = −807.53 USD` across 268 trades, an average
of **−3.013 USD per full stop** against an average intended risk of ~2.8 USD.

### The exit-reason table, correctly read

| exit_reason | n | % | win rate | net USD | avg USD |
|---|---|---|---|---|---|
| TRAIL | 324 | 46.6% | 94.8% | **+401.05** | +1.238 |
| BROKER_CLOSE | 240 | 34.5% | 1.2% | **−716.93** | −2.987 |
| TIME | 104 | 14.9% | 39.4% | −48.08 | −0.462 |
| SL | 28 | 4.0% | 0.0% | −90.60 | −3.236 |

Bucketed properly:

| bucket | n | % | net USD | avg USD |
|---|---|---|---|---|
| trail | 324 | 46.6% | +401.05 | +1.238 |
| full stop | 268 | 38.5% | **−807.53** | −3.013 |
| timeout | 104 | 14.9% | −48.08 | −0.462 |

The trail is the only thing that made money. Full stops cost nearly twice what the
trail earned.

---

## 4. Finding 2 — the 1:3 payoff was structurally impossible

The bot's sizing assumed `b = 3`: risk 1 to make 3. The realized payoff was 0.492.
The cause is in the trail parameters:

```
sl_pct            0.0006    # stop distance = 0.06% of mid
tp_mult           3.0       # target = 0.18% from entry
trail_trig_pct    0.00025   # trail ARMS after 0.025% favourable move
trail_dist_pct    0.00012   # trail sits 0.012% behind the best price
max_hold_sec      150
```

Read the ratio: the trail **arms at 0.025%, which is 42% of the stop distance**,
and then **trails 0.012%, which is 20% of the stop distance**. Once armed, the
best case for the trade is roughly `arm + something`, and the trail gives back
0.012% of it. There is no path to 0.18% except a single instantaneous spike.

Measured on the ledger: the median `TRAIL` exit moved the price **0.0192%, which is
0.32× the stop distance**. That is the realized win size — about a third of one R.

And the target:

- trades reaching **≥ +3R: 1** (out of 351 winners)
- trades reaching **≥ +2R: 5**
- trades reaching **≥ +1R: 34**
- trades at or below **−1R: 183**

R-multiple distribution:

```
min −3.396   p5 −1.370   p25 −1.007   median 0.008
p75  0.343   p95  0.983  max  3.01
```

The distribution is asymmetric in the wrong direction: the left tail extends to
−3.4R (broker stop slippage past the intended level, plus the lot oversizing in
finding 4) while the right tail is clipped at +1R for 95% of trades.

### What this did to Kelly

The Kelly fraction the engine computed was built on `b = 3` and `p` = the trailing
50-trade win rate:

| assumption | `f* = (p(b+1) − 1)/b` | half-Kelly | verdict |
|---|---|---|---|
| `b = 3.0` (the config) | **+0.3391** | +0.1695 | bet, and bet big |
| `b = 0.492` (realized) | **−0.5032** | −0.2516 | **do not bet at all** |

With the config's payoff assumption and a 50.43% win rate, Kelly says bet 34% of
equity per trade. With the payoff the bot actually achieved, Kelly says the
expectancy is negative and the correct position size is zero. The sizing model was
not mis-tuned — it was answering a different question than the one the market was
asking. Half-Kelly at 16.95% per trade only survived because the 1% cap
(`max_risk_per_trade: 0.01`) overrode it. **The cap is the only reason the account
took 21 hours to die instead of 20 trades.**

---

## 5. Finding 3 — spread was two thirds of the loss

The sizing model treated the round trip as free. Measured broker spreads, applied
per symbol against each trade's own reconstructed R distance:

| symbol | n | median spread as % of R | spread paid |
|---|---|---|---|
| BTCUSD | 355 | 13.50% | +123.24 USD |
| XAUUSD | 193 | 6.96% | +34.81 USD |
| USOIL | 145 | **24.87%** | +144.26 USD |
| | | | **+302.31 USD** |

`302.31 / 454.56 = 0.67` — spread is 0.67× the entire net loss.

The live broker's quoted spreads make the mechanism obvious:

| symbol | quoted spread | as % of price | as % of the 0.06% stop | as % of the 0.012% trail |
|---|---|---|---|---|
| XAUUSD | 0.168 | 0.0040% | 6.7% | 33.7% |
| BTCUSD | 7.00 | 0.0083% | 13.8% | 69.2% |
| USOIL | 0.014 | 0.0157% | 26.1% | **130.7%** |

On USOIL the quoted spread is **wider than the trailing stop distance**. The trail
cannot function: by the time price has moved far enough to arm it, the spread alone
would have closed the position at a loss. That symbol's expectancy was −0.2985 R,
the worst of the three traded.

### The harness confirms it, and separates signal from cost

`research/edge_harness.py` re-runs the bot's exact configuration on 60,000 M1 bars,
charging exactly one spread per round trip, executing at the next bar's open, and
assuming the stop fills first when a bar touches both stop and target. Bootstrap
95% CI on net expectancy, 5000 resamples:

| symbol | n | WR | exp_R gross | exp_R net | CI95(net) | avg spread as % of R | max DD |
|---|---|---|---|---|---|---|---|
| XAUUSD | 3750 | 58.2% | **+0.1043** | +0.0346 | [+0.0086, +0.0600] | 6.96% | 32.5% |
| BTCUSD | 3464 | 48.9% | **+0.0614** | −0.0867 | [−0.1221, −0.0498] | 14.81% | 97.0% |
| XAGUSD | 3551 | 33.6% | **+0.0366** | −0.5053 | [−0.5446, −0.4661] | 54.19% | 100.0% |
| USOIL | 3715 | 42.4% | −0.0017 | −0.2711 | [−0.3102, −0.2322] | 26.94% | 100.0% |

**Gross expectancy was positive on three of four symbols. Net expectancy was
negative on three of four.** The signal found something; the cost model ate it and
then some. This is the cleanest single piece of evidence in the whole exercise:
the stop was so tight that spread, not direction, determined the outcome.

`max_dd_pct` in the harness is a 1%-risk fixed-fractional equity curve — and it
reaches **100.0%** on XAGUSD and USOIL. A 100% drawdown on a fixed-fractional
curve means the compounding equity went to zero. The live account only survived to
−82.3% because the equity floor stopped it at 100 USD.

---

## 6. Finding 4 — the 1% risk cap was not enforced, and could not be

`RISK_PCT = 0.01`. The engine computed `risk_usd = equity × 0.01`, converted it to
lots via `risk_usd / (sl_move × contract_size)`, then clamped to the broker's
`[vol_min, vol_max]` grid — a 0.01 step.

Two different answers depending on which risk you measure, against the equity
snapshot nearest each trade:

| measure | median | max | trades over 1% |
|---|---|---|---|
| `risk_amount` (what the engine intended) | 0.999% | 1.027% | 299 / 696 (43.0%) |
| `lot × contract × |entry − stop|` (what the position carried) | **1.019%** | **2.560%** | **385 / 696 (55.3%)** |

The engine's own arithmetic was accurate to within 3%. The gap is entirely lot
granularity: median `lot / ideal_lot` was **1.021**, mean 1.102, worst **2.61×**.
392 trades rounded up, 304 rounded down, none landed exactly.

The failure mode is asymmetric and self-reinforcing. As equity falls, the ideal lot
falls below the 0.01 floor, and the *smallest allowed position* becomes an
increasing fraction of the account. XAUUSD:

| time | equity | lot | risk USD | % of equity |
|---|---|---|---|---|
| 04:14 | 565.61 | 0.02 | 5.23 | 0.93% |
| 20:33 | 220.46 | **0.01** (floor) | 2.60 | 1.18% |
| 01:04 | 105.19 | **0.01** (floor) | 2.69 | **2.56%** |

At 565 USD the cap was achievable. At 105 USD the minimum position risked 2.56% —
2.6× the configured limit — and there was no smaller trade available. Per-symbol
medians tell the same story:

| symbol | n | min | median | max |
|---|---|---|---|---|
| BTCUSD | 355 | 0.34% | 1.00% | 2.08% |
| XAUUSD | 193 | 0.64% | **1.27%** | **2.56%** |
| USOIL | 145 | 0.74% | 0.98% | 1.32% |
| EURUSD | 3 | 0.94% | 1.00% | 1.02% |

XAUUSD's median risk was 1.27% — 27% over the cap — because 0.01 lots of gold at
4,000 USD is a ~2.6 USD position, which is 2.5% of a 105 USD account.

**This is a small-account artifact, and it is important not to over-generalise it.**
At 50,000 USD the same code would hit 1.000% on nearly every trade. The lesson is
narrower and more useful: *a percentage-based risk cap is only as precise as the
instrument's minimum tradable size, and the bot never checked whether the minimum
was compatible with the account.* A startup assertion — `vol_min × contract ×
sl_move ≤ equity × RISK_PCT` — would have refused to trade XAUUSD below ~260 USD
of equity.

---

## 7. Finding 5 — the daily cap fired 58 times and stopped nothing

`daily_risk_cap: 0.01` with `max_daily_loss: 0.01`. The event was written 58 times
between 07:14:30 and 01:04:05:

```
2026-09-21T07:14:30  daily_cap  286.06
2026-09-21T19:16:28  daily_cap  273.95
2026-09-21T19:26:08  daily_cap  270.77
2026-09-21T19:31:07  daily_cap  264.95
2026-09-21T19:34:24  daily_cap  260.45
2026-09-21T19:40:00  daily_cap  255.44
2026-09-21T19:44:20  daily_cap  ...
...
2026-09-22T01:04:05  daily_cap  (equity floor at 100.00 hit 42s later)
```

Every single one of the 58 was followed by more trades. **373 of 696 trades — 53.6%
of the session — happened after the first cap fired.** Between 19:16 and 01:04 the
cap fired roughly every 5–10 minutes while equity went 276.35 → 105.19, a further
−61.9%.

The circuit breaker logged the breach and let the bot continue. It was
instrumentation, not control: `risk_events` records the event, and nothing in the
trading loop consults it to halt. Compare with the two mechanisms that *did* stop
trading — the 12-hour runtime limit (19:11:18) and the equity floor (01:04:47) —
both of which called `close_all()` and `break`.

### The session's actual shape

| time (UTC) | event | equity |
|---|---|---|
| 09-21 04:13 | start | 565.34 |
| 09-21 07:14 | first `daily_cap` | 289.55 |
| 09-21 19:11 | `runtime_stop` (12h limit) | 277.61 |
| 09-21 19:16 | `daily_cap` again | 276.35 |
| 09-22 01:04 | `equity_floor` 100.00 | 105.19 → 100.17 |

Two legs, both about −50% to −64%:

- **Leg 1** 04:14 → 07:14, 323 trades: 565.34 → 289.55, **−48.8%**
- **Leg 2** 19:16 → 01:04, 373 trades: 276.35 → 105.19, **−63.8%**

Peak equity 565.61, trough 100.00, **max drawdown 82.32%**. Peak trade rate was
127 trades in a single hour.

The bot ran, halted for 12 hours on its runtime limit, was restarted, and did the
same thing again. Nothing in the ledger suggests the halt changed any parameter.

---

## 8. What the harness says would have worked

72 cells in the research grid. Ten earn a bootstrap 95% CI that excludes zero. All
ten change one thing — the stop distance — and keep the signal identical:

| variant | symbol | n | WR | spread % of R | exp_R net | 95% CI | max DD |
|---|---|---|---|---|---|---|---|
| live config SL 0.06% | XAUUSD | 3750 | 58.2% | 6.96% | +0.0346 | [+0.0086, +0.0600] | 32.5% |
| live config SL 0.06% | BTCUSD | 3464 | 48.9% | 14.81% | −0.0867 | [−0.1221, −0.0498] | 97.0% |
| live config SL 0.06% | USOIL | 3715 | 42.4% | 26.94% | −0.2711 | [−0.3102, −0.2322] | 100.0% |
| **SL 0.3%** TP3× | XAUUSD | 1240 | 88.2% | **1.39%** | **+0.0355** | [+0.0197, +0.0512] | **6.7%** |
| **SL 0.3%** TP3× | BTCUSD | 1213 | 87.1% | **2.96%** | **+0.0600** | [+0.0389, +0.0811] | **5.1%** |
| **SL 0.3%** TP3× | USOIL | 1236 | 84.9% | **5.39%** | **+0.0648** | [+0.0414, +0.0875] | **8.8%** |
| **SL 0.3%** TP1.5× | BTCUSD | 1213 | 87.1% | 2.96% | +0.0586 | [+0.0379, +0.0790] | — |
| **SL 0.5%** TP3× | XAUUSD | 773 | 91.2% | 0.84% | +0.0172 | [+0.0026, +0.0313] | 4.6% |
| **SL 0.5%** TP3× | BTCUSD | 775 | 93.0% | 1.77% | +0.0489 | [+0.0320, +0.0670] | 3.1% |
| **SL 0.5%** TP3× | USOIL | 776 | 89.6% | 3.24% | +0.0426 | [+0.0234, +0.0616] | 3.8% |

Widening the stop 5× reduces spread's share of R by 5×, flips three of four symbols
from negative to positive net expectancy, and moves max drawdown from 97–100% to
**5–9%**. Notably the win rate *rises* to 85–93%, because a wider stop is no longer
inside the noise — which also means the realized payoff falls, and the strategy
becomes a high-win-rate/low-payoff system. That is a different strategy than the
1:3 design intended, and the Kelly `b` would have to be re-estimated accordingly.

### The grid's verdict distribution

| signal family | EDGE | NO EDGE | NEGATIVE | INCONCLUSIVE |
|---|---|---|---|---|
| `a) kelly_baseline` — the live config | 1 | 1 | 6 | — |
| `b) kelly SL 0.3–0.5%` | **9** | 7 | — | — |
| `c) mean reversion` | — | 6 | 6 | — |
| `d) session / prior-day breakout` | — | 13 | 3 | — |
| `e) trend following` | — | 12 | — | 4 |
| `f) random entry (control)` | — | 3 | 1 | — |

Ten `EDGE` cells out of 72 — nine in the wide-stop variants of the bot's own signal,
one in the live config on XAUUSD.

---

## 9. What this does *not* prove

Being explicit about the limits, because the tempting reading of section 8 is
overconfident:

1. **The random-entry control is flat.** `f) random SL0.2% TP2x` produces
   exp_R_net between −0.152 and +0.019 with CIs spanning zero on every symbol. A
   random entry with the same exit machinery performs about as well as the real
   signal does. The measured edge in the wide-stop variants is therefore **largely
   the exit geometry and the spread reduction, not the entry logic**. The signal's
   own contribution is unproven and needs a per-variant null control that this
   grid does not contain.
2. **Everything is in-sample.** One 60,000-bar M1 window per symbol, no
   walk-forward, no out-of-sample holdout, no live confirmation. The CIs describe
   the sampling error of a single window; they do not describe regime risk.
3. **`e4) donchian50 H4` is a small-sample artifact.** +0.56 R expectancy on 9–13
   trades. The harness labels it `INCONCLUSIVE (n=10)` and refuses it an `EDGE`
   verdict, which is the correct handling — it is listed here only to show the
   guard working.
4. **The harness fill model is mildly optimistic on price.** Bar-mode stop/target
   fills assume the exact level is available; the reported verdicts lean on
   `exp_R_net` and its CI rather than the gross figure for that reason. `R_net`
   itself is exact by construction: the spread term is charged once, so a stop
   always realises −1.00 R net and a target exactly +tp_mult R net. Those two
   identities are asserted in `research/test_engine.py` (T1/T2/T3/T11).
5. **696 trades on a few-hundred-dollar demo account is a small sample** and the
   lot-granularity failure in particular does not transfer to a larger account.
6. **Not a strategy.** Nothing here is validated, published as profitable, or
   offered as investment advice. It is a post-mortem.

---

## 10. Engineering lessons that generalise

Independent of the market, these are the transferable findings:

1. **A stop tighter than the instrument's noise is not a risk control.** Measure
   the stop against the realised bar range and the quoted spread before trusting
   it. On USOIL the spread alone (0.0157%) was wider than the trail distance
   (0.012%).
2. **Never let a strategy's payoff ratio be an assumption in the sizing model.**
   `b = 3.0` was a config constant; the ledger's realized `b` was 0.492. Estimate
   `b` from closed trades the same way `p` was — and refuse to size when `b`
   makes `f*` negative. Here Kelly would have said *do not trade* after the first
   ~50 trades, and it was never asked.
3. **A risk cap is only as precise as the minimum tradable size.** Assert
   `vol_min × contract × sl_move ≤ equity × RISK_PCT` at startup and refuse the
   symbol when it fails.
4. **A circuit breaker that only writes a log row is not a circuit breaker.**
   The daily cap fired 58 times and halted nothing. If the loop does not `break`
   on the event, the event is decoration.
5. **Broker-side stops race software-side stop logic, and the broker always
   wins.** 235 of 240 `BROKER_CLOSE` exits were at the exact stop level. Any exit
   attribution that relies on the software's own reason label will misclassify
   these — reconcile against the broker's deal history.
6. **Test against the real broker, not the documentation.** `symbol_info(sym)
   .filling_mode` is an enum (`0=FOK, 1=IOC, 2=RETURN, 3=all-market`), not a
   bitmask; passing it through as `type_filling` produced `10030 Unsupported
   filling mode`. And `OILUSD` does not exist on Exness — the symbol is `USOIL`.
7. **Charge the spread in the sizing model and in the backtest.** The harness's
   gross-vs-net columns are what made finding 3 visible; the live bot had no such
   column and paid 0.67× its entire loss in spread.

---

## Appendix: reproducing this document

```bash
python analysis/trade_forensics.py                    # sections 2–7, stdlib only
python analysis/trade_forensics.py --json facts.json  # machine-readable
python research/edge_harness.py --json research/results.json   # section 8
python -m pytest tests/ -v                            # 82 tests
python research/test_engine.py                        # 33 simulator assertions
```

`data/trading_bot.db` is committed (602 KB) so sections 2–7 are checkable without
a broker connection. `research/results.json` is committed so section 8 is
checkable without re-running the grid. The 200 MB of MT5 CSVs the harness reads
are not committed; `research/export_data.py` regenerates them.
