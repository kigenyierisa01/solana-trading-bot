# Trading Strategy

## What the bot trades on

The bot monitors pump.fun bonding-curve token launches on Solana. It tracks
every CreateEvent, TradeEvent and CompleteEvent from stdin, one JSON line per
slot. It ignores mayhem-mode and cashback-enabled tokens entirely.

Entry decisions are driven by a **momentum signal**: the bot compares a
recency-weighted average of the three most recent price samples against a
recency-weighted average of all retained price history (capped at 8 slots).
Newer samples receive larger linear weights. A token needs at least 3 recorded
price slots before the bot will consider buying it.

Momentum is decayed by a factor of 0.90 per elapsed slot since the last trade,
so stale signals fade quickly and only actively-traded tokens score well.

## How it sizes positions

Momentum between 0.1% and 2% maps linearly to a confidence score from 0 to 1.
The minimum accepted confidence is 0.10 — weak signals are ignored entirely.

Buy size scales linearly from 10,000,000 to 50,000,000 lamports (0.01 to
0.05 SOL) according to confidence. The order is then capped at 1% of the
token's real SOL reserves to avoid moving the market, and by available cash.
Orders below 10,000,000 lamports are skipped. The portfolio holds at most 5
simultaneous positions.

## When it exits

Every open position tracks its peak observed price and entry price:

- **Stop loss**: full position sell if price drops 40% from its peak
- **Take profit**: full position sell if price rises 50% from entry
- **CompleteEvent**: position is closed automatically, principal returned to cash

Portfolio equity (cash plus marked-to-market positions) is tracked against a
high-water mark. If total drawdown reaches 40%, all trading halts permanently
for that run — no further buys or sells are emitted.

Slots with no events still check open positions against the latest known prices
and refresh the drawdown calculation.
