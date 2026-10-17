# ALGO2e

Python market maker for the RSM434 Fall 2026 **RIT ALGO2e simulation**, based on the supplied capstone brief, evaluation methodology, and REST primer. Uses the same `requests.Session`, `X-API-Key`, and local client address as the LT3 strategy.

## Run

Use Python 3.10 or newer on the computer running the connected RIT Client.

```sh
python -m pip install -r requirements.txt
python strategy.py --check
python strategy.py
```

The first command installs the only dependency. `--check` reads case, securities, orders, and limits without placing/cancelling orders. Running without it starts trading when the case becomes ACTIVE. Start each round flat, with no open orders; use **one bot and no manual trading on this account**. Restart the process for each round. A reset stops the bot rather than silently reusing old order IDs.

Connection defaults at the top of `strategy.py` are `http://localhost:9999/v1` and `Rotman`, matching the supplied primer/LT3. Match these to the RIT Client's API icon and enable API Orders. These are client API settings, separate from the server login. You can edit the two defaults or override with `RIT_BASE_URL` and `RIT_API_KEY` environment variables. Keep personal keys out of Git.

Ctrl+C once stops new exposure and attempts to close inventory. Ctrl+C again exits immediately, with best-effort order cancellation; remaining inventory needs attention in RIT. If the API disconnects or an order submission has an uncertain outcome, the program stops and attempts cancellation rather than submitting the order again.

## Case rules and grading

| Security | Active fee/share | Passive rebate/share |
|---|---:|---:|
| CNR | $0.0027 | $0.0023 |
| RY | -$0.0014 | -$0.0020 |
| AC | $0.0015 | $0.0011 |

RY pays a taker rebate but **charges** makers. A marketable limit order also pays/receives the active feebate for its immediately executed portion. Passive round-trip economics are approximately `sell price - buy price + 2 × maker rebate`, before price risk. This bot does not cross solely to collect RY's rebate: two taker rebates typically do not cover a one-cent spread.

- Each round lasts 300 seconds. Maximum order size is 5,000 shares; gross and absolute net limits are 25,000 shares. The case states a 500 orders/second ceiling; this bot deliberately operates far below that and honors HTTP 429 responses.
- **October 20 initial run:** participation, requiring nonzero final P&L for full participation marks; no ranked-run volume requirement is stated for this initial run.
- **November 24 ranked evaluation:** five group runs; each requires **100,000 shares bought plus sold**. Automatic end-of-case closeouts do not count. Missing the volume minimum gives zero for that run.
- Qualifying runs rank by P&L. Downside deviation across all runs is `sqrt(mean(min(P&L, 0)^2))`; losses in nonqualifying runs count too. The downside grade only adjusts the final grade downward, weighted by the number of losing runs. This favors qualifying consistently without taking large losses.

Sources: supplied *RIT Case Brief – ALGO2e*, Build 1.00, pp. 1–2; *RSM434H1F Fall 2026 — RIT Performance Evaluations*, pp. 1–3; *RSM434 ALGO2e Primer – REST.py*. Course files are not redistributed here. Endpoint conventions also follow the [official RIT ALGO2 Python REST tutorial](https://inside.rotman.utoronto.ca/financelab/files/2022/09/RIT-Case-Tutorial-ALGO2-Python-REST-API.pdf).

## Strategy

1. **Quote all three securities with their own fees.** Fair value blends midpoint, top-of-book size imbalance and a bounded short-term trend estimate. Quotes account for the maker rebate/charge, widen with observed volatility, and normally join the best price; one-cent improvement is allowed when the spread is at least three cents. Book imbalance is only a noisy proxy, not an estimate of guaranteed future prices.
2. **Keep inventory modest.** Long inventory shifts quotes downward, short inventory shifts them upward. Stop adding to a stock beyond 3,500 shares in either direction. Every outstanding share reserves risk, including both sides of partially filled orders. Worst-case independent fills must fit the gross, net and per-stock budgets.
3. **Pace participation.** Target 110,000 confirmed shares by tick 240, giving a buffer over the ranked requirement. Behind pace, use up to 2,000-share normal clips; reduce clip sizes with volatility and after meeting the target. This increases opportunities but cannot guarantee fills. There is no forced loss-making round-trip volume loop.
4. **Preserve some queue priority.** Keep orders until their target moves two cents, they become marketable, or their four-second maximum age expires. Cancel/reconcile before replacement; never assume a cancelled order had zero fills. Retained opposite-side orders constrain new prices to avoid intentional self-crossing.
5. **Exit before the end.** From tick 260, submit only inventory-reducing orders at the best passive price. From tick 280, use marketable limits against observed depth, capped at 15 cents from the current best price; from tick 292 the cap expands to 50 cents. Thin liquidity, fast moves, or connectivity failures can leave inventory unclosed. No strategy can guarantee closeout without counterparties.
6. **Limit downside.** A $750 loss from startup, or a $600 retreat after reaching at least $300 profit, triggers early liquidation. These are uncalibrated starting values. They use the API's `realized + unrealized` securities fields when available; the console explicitly says when those fields are missing and the dollar stops are unavailable. Stops can overshoot and can stop a run before qualifying. Server-reported P&L remains authoritative for grading.

Risk budgets are 18,000 gross/net, with 6,500 per stock. At startup the bot reads `/limits` and takes the smaller of these budgets and 90% of the live limits. It does not override instructor settings. This assumes the ALGO2e share-based limit model; don't reuse it for another case with different risk weights.

## Validation and practice

```sh
python -m unittest discover -s tests -v
```

Tests cover independent fill combinations, gross/net reservation, partial fills, cancellation races, missing order lookup recovery, ambiguous submissions, rate limiting, fee signs, passive prices, inventory skew, depth-limited exits, case resets, and a full 300-tick fake exchange lifecycle. The fake exchange validates bookkeeping and control flow; **it is not a realistic backtest and establishes no expected profit or qualification probability**.

Live practice has not been run as part of this build. The strategy parameters are starting hypotheses, not fitted or proven optimal values. Before evaluation, run multiple practice rounds and compare against the primer under comparable conditions. Record final RIT P&L, confirmed volume, any fines/rejections, and residual positions. Retain poor runs, not just winners.

The console and ignored `logs/run-*.csv` files show five-second snapshots of confirmed volume, positions, session P&L and mode. Watch pacing: roughly 27,500 shares at tick 60, 55,000 at 120, 82,500 at 180, and 110,000 at 240. If volume is consistently short, investigate quote distance, queue age, and API latency before increasing size. If losses dominate, inspect adverse fills, reduce inventory and clip sizes, and adjust volatility widening. Tune one change at a time, then validate on fresh rounds.

`confirmed_volume` counts only fills acknowledged for this process's orders, once per cumulative fill update. It excludes automatic liquidation, earlier manual trades, and other accounts/group members. An accepted order whose response is lost can leave the local total understated; inspect the official RIT report after any such failure.
