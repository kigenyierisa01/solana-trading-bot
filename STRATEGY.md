# Trading Strategy

## What the bot trades on

The bot monitors pump.fun bonding-curve token launches on Solana. It reads
one JSON message per line from stdin — a hello message, then one batch per
slot containing CreateEvents, TradeEvents and CompleteEvents.

Tokens marked as mayhem-mode or cashback-enabled are excluded entirely at
the CreateEvent gate. Only clean, standard bonding-curve tokens are tracked.

Entry decisions are driven by a **decayed momentum signal**. The bot derives
price as real SOL reserves divided by real token reserves at each TradeEvent.
It records at most one price sample per slot, capping history at 8 slots.
Momentum compares a recency-weighted average of the 3 most recent prices
against a recency-weighted average of all retained history. A token needs
at least 3 recorded price slots before any buy is considered.

The momentum signal decays by a factor of 0.90 per elapsed slot since the
last trade, so stale signals fade quickly and only actively-traded tokens
score well.

## How it sizes positions

Momentum between 0.1% and 2% maps linearly to a confidence score from 0
to 1. The minimum accepted confidence is 0.10 — weak signals are ignored.

Buy size scales linearly from 10,000,000 to 50,000,000 lamports (0.01 to
0.05 SOL) according to confidence. The order is then capped at 1% of the
token's real SOL reserves to avoid moving the market, and by available cash.
Orders below 10,000,000 lamports (0.01 SOL) are skipped entirely.

The portfolio holds at most 5 simultaneous positions. No new buys are
placed when the position limit is reached.

## When it exits

Every open position tracks its peak observed price and entry price:

- **Stop loss**: full position sell if price drops 40% from its peak
- **Take profit**: full position sell if price rises 50% from entry
- **CompleteEvent**: position closed automatically, principal returned to cash

Portfolio equity (cash plus marked-to-market positions) is tracked against
a high-water mark. If total drawdown reaches 45%, all trading halts
permanently for that run — no further buys or sells are emitted even if
equity later recovers.

Slots with no events still check open positions against latest known prices
and refresh the drawdown calculation, ensuring risk controls are always live.
