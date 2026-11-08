# Solana Momentum Trading Bot — make.fun Competition Entry

A deterministic paper-trading bot that trades pump.fun bonding-curve token
launches on Solana, built for the make.fun memecoin backtest competition.

## Strategy Overview

- **Token admission**: filters out mayhem-mode and cashback-enabled tokens
- **Momentum signal**: recency-weighted price history with exponential decay
- **Position sizing**: confidence-based scaling from 0.01 to 0.05 SOL
- **Risk management**: per-token stop-loss, take-profit, portfolio drawdown halt

See [STRATEGY.md](STRATEGY.md) for the full strategy description.

## How it works

The bot reads one JSON message per line from stdin:
- `hello` — initializes the bot with a deterministic seed
- `batch` — one batch of events per Solana slot to process
- `done` — signals end of data

For each batch it processes CreateEvents, TradeEvents and CompleteEvents,
then emits buy and sell orders as JSON to stdout.

## Requirements

- Python 3.13
- Standard library only (no pip installs)
- No network access — runs fully offline and deterministically

## Environment

| Variable | Description |
|---|---|
| None required | All config is hardcoded for determinism |

## Usage

```bash
python main.py < input_data.jsonl
```

## Guarantees

- Two runs on identical input produce byte-for-byte identical output
- No wall clock, no network, no external files
- Maximum drawdown capped at 45% before trading halts

## License

MIT
