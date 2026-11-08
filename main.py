# =============================================================================
# pump.fun competition bot — strategy reference
# =============================================================================
#
# OVERVIEW
# --------
# Deterministic, paper-ordering Solana momentum bot written for pump.fun
# slot-based competitions.  Every decision — token admission, entry sizing,
# exit timing, and portfolio risk control — is a pure function of the event
# stream delivered by the harness.  Given identical input the bot produces
# byte-for-byte identical output with no randomness, no I/O side-effects, and
# no external state.
#
# The strategy has four sequential layers:
#
#   1. Token admission   (hello / CreateEvent gate)
#   2. Momentum signal   (price history → decayed momentum → confidence score)
#   3. Position sizing   (confidence × reserve cap × cash cap → order lamports)
#   4. Risk management   (stop-loss, take-profit, portfolio drawdown halt)
#
# Each layer is described in full below.
#
# =============================================================================
# LAYER 1 — TOKEN ADMISSION
# =============================================================================
#
# Trigger:  CreateEvent arriving in any batch.
#
# A token is admitted to tracking — and therefore becomes eligible for future
# buy orders — only when both exclusion flags are absent:
#
#   • is_mayhem_mode      → excluded (non-standard curve mechanics)
#   • is_cashback_enabled → excluded (rebate distorts real reserve accounting)
#
# Admitted tokens receive a MarketState object initialised with the reserves
# published in the CreateEvent.  Excluded tokens are silently ignored; no
# MarketState is ever created for them, so they can never appear in a momentum
# calculation or trigger a buy in any subsequent slot.
#
# =============================================================================
# LAYER 2 — MOMENTUM SIGNAL
# =============================================================================
#
# 2a. Price derivation
# --------------------
# Each TradeEvent updates the token's real_sol_reserves and real_token_reserves.
# The implied price is:
#
#   price = real_sol_reserves / real_token_reserves          (SOL per token)
#
# The price is only stored when both reserves are strictly positive.
#
# 2b. Per-slot price history
# --------------------------
# At most one price sample is retained per slot.  If a second TradeEvent
# arrives in the same slot it replaces the earlier sample in-place, so the
# history list is always a deduplicated, chronological series of
# (slot, price) pairs.  The list is capped at max_history_slots (default 8);
# once full, the oldest entry is evicted to make room for the newest.
#
# Empty slots (batches with no events) never add a price sample, so their slot
# numbers simply create a gap in the history.  The gap is not stored explicitly;
# it manifests as an increased elapsed-slot count in the decay calculation.
#
# 2c. Recency-weighted momentum
# -----------------------------
# Momentum is computed when a TradeEvent arrives for an admitted token that
# already has at least minimum_history_slots (default 3) recorded samples.
#
# Step 1 — build two price windows from the history list:
#
#   short_window  = last min(3, N) prices    (most recent slot prices)
#   full_history  = all N retained prices    (oldest to newest)
#
# Step 2 — compute a linearly recency-weighted average for each window.
# Within a window of length W, the oldest sample receives weight 1 and each
# subsequent sample receives weight +1, so the newest sample receives weight W:
#
#   weighted_avg = Σ(weight_i × price_i) / Σ(weight_i)
#   where weight_i ∈ {1, 2, …, W} and Σ(weight_i) = W(W+1)/2
#
# Step 3 — raw momentum is the fractional excess of the short window over the
# full history:
#
#   raw_momentum = (avg_short − avg_full) / avg_full
#
# A positive value means recent prices are running above the longer-term
# average — the signal the strategy is designed to catch on pump.fun launches.
#
# 2d. Slot-based momentum decay
# ------------------------------
# Raw momentum is multiplied by a decay factor raised to the number of slots
# elapsed since the most recent price sample:
#
#   decayed_momentum = raw_momentum × decay_per_slot ^ elapsed_slots
#
# With the default decay_per_slot = 0.90, one slot without a new trade costs
# 10 % of the signal's remaining strength:
#
#   elapsed 0 slots  →  ×1.000   (trade just arrived, no decay)
#   elapsed 1 slot   →  ×0.900
#   elapsed 2 slots  →  ×0.810
#   elapsed 5 slots  →  ×0.590
#   elapsed 10 slots →  ×0.349
#
# This ensures that a momentum reading cannot survive many silent slots and
# trigger a buy on a token that has gone cold.  A new TradeEvent in any slot
# resets elapsed_slots to zero for that token.
#
# 2e. Confidence mapping
# ----------------------
# Decayed momentum is mapped linearly to a confidence score on [0, 1]:
#
#   confidence = clamp((momentum − min_momentum) /
#                      (full_momentum − min_momentum), 0, 1)
#
# Default thresholds:
#
#   min_momentum  (→ confidence 0.0)  =  0.001  ( 0.1 %)
#   full_momentum (→ confidence 1.0)  =  0.020  ( 2.0 %)
#
# Any confidence below minimum_confidence (default 0.10) is rejected outright
# and no buy order is placed for that token in that slot.
#
# =============================================================================
# LAYER 3 — POSITION SIZING
# =============================================================================
#
# When confidence meets the minimum threshold, the order size is computed in
# three steps, each of which can only reduce the size from the previous step:
#
# Step 1 — confidence scaling
#
#   raw_size = base_order + round((max_order − base_order) × confidence)
#
#   base_order_lamports    =  10_000_000  ( 0.01 SOL, at minimum confidence)
#   maximum_order_lamports =  50_000_000  ( 0.05 SOL, at full confidence)
#
#   At confidence 0.10:  raw_size ≈ 10_000_000 + round(40_000_000 × 0.10)
#                                 = 14_000_000 lamports
#   At confidence 1.00:  raw_size = 50_000_000 lamports
#
# Step 2 — reserve impact cap
#
#   reserve_cap = int(real_sol_reserves × maximum_reserve_fraction)
#
#   maximum_reserve_fraction = 0.01  (1 % of the token's bonding-curve SOL)
#
#   This prevents a single order from moving the curve price materially.
#   Tokens with thin liquidity will have a low reserve_cap and will often
#   produce orders that fall below MIN_ORDER_LAMPORTS and are discarded.
#
# Step 3 — available cash cap
#
#   final_size = min(raw_size, reserve_cap, portfolio.cash_lamports)
#
# If final_size < MIN_ORDER_LAMPORTS (10_000_000), the buy is silently skipped
# and no position is opened for that slot.
#
# At most one buy order is emitted per mint per slot.  At most max_positions
# (default 5) positions may be open simultaneously; a full portfolio blocks new
# buys regardless of confidence or sizing.
#
# =============================================================================
# LAYER 4 — RISK MANAGEMENT
# =============================================================================
#
# 4a. Per-position exits
# ----------------------
# Every batch, after all events have been processed, the bot scans every open
# position in deterministic sorted-mint order.  For each position it checks the
# token's most recently observed last_price against two thresholds:
#
#   Stop-loss    — fires when last_price ≤ peak_price × (1 − stop_loss_fraction)
#
#                  default stop_loss_fraction = 0.40
#                  → sell if price drops 40 % below the position's peak
#
#   Take-profit  — fires when last_price ≥ entry_price × (1 + take_profit_fraction)
#
#                  default take_profit_fraction = 0.50
#                  → sell if price rises 50 % above the entry price
#
# peak_price is updated on every TradeEvent for the token while the position is
# open, so the stop-loss level trails the market upward but never moves down.
# This is a classic trailing stop.
#
# A CompleteEvent (bonding-curve migration to a DEX) immediately releases the
# full invested principal back to paper cash without attempting a sell order,
# since no further pump.fun trades are possible on a completed curve.
#
# Sell orders are full-position (all estimated tokens).  Estimated proceeds are
# computed by scaling the invested principal by the price ratio at exit:
#
#   proceeds = invested_lamports × (last_price / entry_price)
#
# This avoids floating-point round-trips through the token quantity and keeps
# the accounting deterministic.
#
# 4b. Portfolio drawdown halt
# ---------------------------
# Portfolio equity at any point is:
#
#   equity = cash_lamports + Σ(estimated_tokens_i × last_price_i)
#
# where positions with no post-entry trade fall back to entry_price.
#
# The equity high-water mark (peak_equity_lamports) is advanced whenever equity
# exceeds the previous peak, but it is never decremented.  Drawdown is:
#
#   drawdown = (peak_equity − current_equity) / peak_equity
#
# This is evaluated by DrawdownTracker.record() on every call to
# _refresh_portfolio_risk(), which is invoked:
#
#   • once at the start of every batch (before any events are dispatched)
#   • once after every individual event within a batch
#
# When max_drawdown_fraction reaches or exceeds halt_threshold (default 0.45,
# i.e. 45 %), trading_halted is set permanently to True.  After that point:
#
#   • no buy orders are placed regardless of momentum or confidence
#   • no sell orders are placed (exits are also suppressed)
#   • the bot continues reading batches and emitting empty orders responses
#
# The halt is irreversible within a run; equity recovering above the previous
# high-water mark does not clear the flag.
#
# Every call to DrawdownTracker.record() appends a DrawdownSnapshot to the
# history list, providing a complete per-batch audit trail of equity,
# drawdown fraction, and halt status for post-competition analysis.
#
# 4c. Empty-slot behaviour
# ------------------------
# A batch whose events list is empty still executes steps 1 and 4b above:
#
#   1. _refresh_portfolio_risk() runs → drawdown check, snapshot appended
#   2. _process_exit_triggers() runs  → stop-loss / take-profit checked using
#                                        the last known price from a prior slot
#
# No new price sample is recorded and no momentum decay is applied explicitly.
# The elapsed-slot count used in the decay formula grows implicitly because the
# latest_sample_slot in price_by_slot falls further behind current_slot with
# each empty batch that passes.
#
# =============================================================================
# WIRE PROTOCOL
# =============================================================================
#
# The bot reads one JSON object per line from stdin and writes one JSON object
# per line to stdout.  Three message types are recognised:
#
#   hello  — received once at start-up; currently a no-op because the strategy
#            is fully deterministic and requires no seed or configuration from
#            the harness.  The Bot constructor (the hello-phase initialisation)
#            is called before the first message is read.
#
#   batch  — the main processing message, delivered once per slot.  Fields:
#              type   : "batch"
#              slot   : int    — Solana slot number
#              seq    : int    — harness sequence counter (echoed in response)
#              events : list   — zero or more CreateEvent / TradeEvent /
#                                CompleteEvent objects
#            Response: {"type": "reply", "seq": <echoed>, "orders": [...]}
#            stdout is flushed immediately after each response so the harness
#            can pipeline slot results without buffering.
#
#   done   — signals end of stream; the main loop exits cleanly.
#
# Malformed JSON lines and invalid batch messages are logged to stderr and
# skipped; a single bad line cannot terminate the bot mid-competition.
#
# =============================================================================

"""Deterministic, paper-ordering Solana momentum bot for pump.fun competitions."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Global constants
# ---------------------------------------------------------------------------

LAMPORTS_PER_SOL: int = 1_000_000_000
"""Number of lamports in one SOL; used to express human-readable SOL amounts."""

MIN_ORDER_LAMPORTS: int = 10_000_000
"""Hard floor below which a computed buy order is discarded rather than placed."""


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------

@dataclass
class BotConfig:
    """Collect every deterministic threshold and account limit for the strategy.

    All fields have sensible defaults so a bare ``BotConfig()`` produces a
    competition-ready configuration.  Override individual fields to tune the
    strategy without subclassing.

    Attributes
    ----------
    initial_cash_lamports:
        Starting paper cash issued to the portfolio at hello-phase initialisation.
    max_positions:
        Maximum number of simultaneous open positions the portfolio may hold.
    max_history_slots:
        Maximum number of per-slot price samples retained per token.  Older
        samples are evicted once this cap is reached.
    minimum_history_slots:
        Minimum number of recorded slots required before momentum is computed
        for a token.  Tokens with fewer samples are silently skipped.
    minimum_confidence:
        Confidence scores below this threshold do not trigger a buy order.
    base_order_lamports:
        Smallest order size produced at the lowest accepted confidence level.
    maximum_order_lamports:
        Largest order size produced at full (1.0) confidence.
    maximum_reserve_fraction:
        Order size is additionally capped at this fraction of the token's real
        SOL reserves to avoid moving the market excessively.
    max_slippage_bps:
        Maximum tolerated slippage in basis points included in every order.
    stop_loss_fraction:
        Fraction below an open position's peak price at which a stop-loss sell
        is triggered (e.g. 0.40 means sell at 60 % of the peak).
    take_profit_fraction:
        Fraction above an open position's entry price at which a take-profit
        sell is triggered (e.g. 0.50 means sell at 150 % of entry).
    max_portfolio_drawdown_fraction:
        Drawdown from the equity high-water mark at which all trading halts
        permanently for the remainder of the batch stream.  Defaults to 0.45
        (45 %).  The ``DrawdownTracker`` embedded in the portfolio enforces this
        limit and records a full per-batch history of equity and drawdown values.
    minimum_momentum:
        Lower bound of the momentum range that maps to non-zero confidence.
        Momentum at or below this value scores zero.
    full_confidence_momentum:
        Upper bound of the momentum range; at or above this level confidence
        saturates at 1.0.
    momentum_decay_per_slot:
        Multiplicative factor applied per elapsed slot since the most recent
        price sample.  Values closer to 1.0 preserve signal longer; values
        closer to 0.0 make momentum decay rapidly without fresh trades.
    """

    initial_cash_lamports: int = 10 * LAMPORTS_PER_SOL
    max_positions: int = 5
    max_history_slots: int = 8
    minimum_history_slots: int = 3
    minimum_confidence: float = 0.10
    base_order_lamports: int = 10_000_000
    maximum_order_lamports: int = 50_000_000
    maximum_reserve_fraction: float = 0.01
    max_slippage_bps: int = 500
    stop_loss_fraction: float = 0.40
    take_profit_fraction: float = 0.50
    max_portfolio_drawdown_fraction: float = 0.45
    minimum_momentum: float = 0.001
    full_confidence_momentum: float = 0.02
    momentum_decay_per_slot: float = 0.90


# ---------------------------------------------------------------------------
# Drawdown tracker
# ---------------------------------------------------------------------------

@dataclass
class DrawdownSnapshot:
    """One per-batch record captured by ``DrawdownTracker``.

    Attributes
    ----------
    batch_seq:
        Sequence number of the batch in which this snapshot was taken.
        ``None`` for the synthetic snapshot recorded at hello-phase boot.
    slot:
        Slot number associated with the batch, or 0 for the boot snapshot.
    equity_lamports:
        Mark-to-market portfolio equity at the time of the snapshot.
    drawdown_fraction:
        Fraction of the equity high-water mark that has been lost at this
        point in time.  Zero until equity falls below the initial peak.
    trading_halted:
        ``True`` if the drawdown halt threshold had already been breached by
        the time this snapshot was recorded.
    """

    batch_seq: int | None
    slot: int
    equity_lamports: int
    drawdown_fraction: float
    trading_halted: bool


class DrawdownTracker:
    """Record the full per-batch drawdown history and enforce the halt threshold.

    ``DrawdownTracker`` is the single source of truth for all drawdown
    accounting.  The ``Portfolio`` owns one instance and delegates every
    drawdown check to it; nothing else in the codebase modifies ``trading_halted``
    directly.

    The tracker maintains:

    * ``peak_equity_lamports`` — the highest equity observed since hello-phase
      initialisation.  Updated whenever a new equity reading exceeds the current
      peak; never decremented.
    * ``max_drawdown_fraction`` — the largest drawdown fraction recorded across
      all batches.  Retained even after a halt so post-mortem analysis is easy.
    * ``trading_halted`` — set permanently to ``True`` the first time
      ``max_drawdown_fraction`` reaches or exceeds ``halt_threshold``.  Never
      reset, even if equity later recovers past the previous high-water mark.
    * ``history`` — an ordered list of ``DrawdownSnapshot`` objects, one per
      ``record()`` call, covering the full life of the bot from hello-phase boot
      through the most recent batch processed.

    Attributes
    ----------
    halt_threshold:
        Drawdown fraction at which trading is permanently halted (e.g. 0.45
        means halt when equity falls 45 % below its peak).
    peak_equity_lamports:
        Running maximum of all equity readings since hello-phase boot.
    max_drawdown_fraction:
        Running maximum of all drawdown fractions since hello-phase boot.
    trading_halted:
        Permanently ``True`` once ``max_drawdown_fraction >= halt_threshold``.
    history:
        Ordered list of ``DrawdownSnapshot`` records, oldest first.
    """

    def __init__(
        self,
        initial_equity_lamports: int,
        halt_threshold: float,
    ) -> None:
        """Initialise the tracker and record the hello-phase boot snapshot.

        Parameters
        ----------
        initial_equity_lamports:
            Starting equity, equal to the portfolio's initial cash balance.
            Used to seed the high-water mark before the first batch arrives.
        halt_threshold:
            Drawdown fraction that triggers a permanent trading halt.
            Must be in (0, 1].

        Raises
        ------
        ValueError
            If ``halt_threshold`` is outside (0, 1].
        """
        if not 0 < halt_threshold <= 1:
            raise ValueError(
                f"halt_threshold must be in (0, 1], got {halt_threshold}"
            )
        self.halt_threshold: float = halt_threshold
        self.peak_equity_lamports: int = initial_equity_lamports
        self.max_drawdown_fraction: float = 0.0
        self.trading_halted: bool = False
        # Seed history with a boot snapshot so the record is never empty.
        boot_snapshot = DrawdownSnapshot(
            batch_seq=None,
            slot=0,
            equity_lamports=initial_equity_lamports,
            drawdown_fraction=0.0,
            trading_halted=False,
        )
        self.history: list[DrawdownSnapshot] = [boot_snapshot]

    def record(
        self,
        current_equity_lamports: int,
        batch_seq: int | None = None,
        slot: int = 0,
    ) -> float:
        """Update state from a fresh equity reading and append a history snapshot.

        This method is called by ``Portfolio.refresh_drawdown_and_halt_check``
        once per batch (and once per event within a batch) so the tracker stays
        synchronised with every mark-to-market equity computation.

        The halt decision is irreversible: once ``trading_halted`` is ``True``
        it remains ``True`` regardless of subsequent equity readings.

        Parameters
        ----------
        current_equity_lamports:
            Latest mark-to-market equity reading in lamports.
        batch_seq:
            Sequence number of the current batch; ``None`` between batches.
        slot:
            Slot number of the current batch; 0 between batches.

        Returns
        -------
        float
            The drawdown fraction computed from this equity reading.
        """
        # Advance the high-water mark if equity has reached a new peak.
        self.peak_equity_lamports = max(
            self.peak_equity_lamports,
            current_equity_lamports,
        )

        # Compute the current drawdown fraction relative to the high-water mark.
        if self.peak_equity_lamports > 0:
            current_drawdown = (
                self.peak_equity_lamports - current_equity_lamports
            ) / self.peak_equity_lamports
        else:
            current_drawdown = 0.0

        # Advance the worst-case drawdown recorder.
        self.max_drawdown_fraction = max(
            self.max_drawdown_fraction,
            current_drawdown,
        )

        # Trigger and latch the halt flag when the threshold is first breached.
        # The 45 % default means: halt permanently once equity drops to 55 % of
        # its all-time peak, even if it subsequently recovers.
        if not self.trading_halted and self.max_drawdown_fraction >= self.halt_threshold:
            self.trading_halted = True

        # Append a snapshot so the full drawdown history is always available.
        snapshot = DrawdownSnapshot(
            batch_seq=batch_seq,
            slot=slot,
            equity_lamports=current_equity_lamports,
            drawdown_fraction=current_drawdown,
            trading_halted=self.trading_halted,
        )
        self.history.append(snapshot)
        return current_drawdown

    def latest_snapshot(self) -> DrawdownSnapshot:
        """Return the most recently recorded ``DrawdownSnapshot``.

        Returns
        -------
        DrawdownSnapshot
            The last entry in ``history``; always present because the boot
            snapshot is inserted during ``__init__``.
        """
        return self.history[-1]

    def drawdown_summary(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary of drawdown statistics.

        Useful for diagnostics, post-competition analysis, and logging.  The
        summary includes the peak equity observed, the worst drawdown recorded,
        the current halt status, and the total number of history snapshots.

        Returns
        -------
        dict
            Keys: ``peak_equity_lamports``, ``max_drawdown_fraction``,
            ``halt_threshold``, ``trading_halted``, ``snapshot_count``.
        """
        return {
            "peak_equity_lamports": self.peak_equity_lamports,
            "max_drawdown_fraction": round(self.max_drawdown_fraction, 6),
            "halt_threshold": self.halt_threshold,
            "trading_halted": self.trading_halted,
            "snapshot_count": len(self.history),
        }


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """Represent a single open paper position and its entry accounting basis.

    A ``Position`` is created when the bot places a buy order and closed either
    by a stop-loss/take-profit sell or by a ``CompleteEvent`` for the token's
    bonding curve.

    Attributes
    ----------
    mint:
        On-chain mint address that uniquely identifies the token.
    invested_lamports:
        Paper cash committed to this position in lamports.
    estimated_token_quantity:
        Approximate number of tokens purchased, computed as
        ``invested_lamports / entry_price`` at the time of the buy.
    entry_price:
        SOL-per-token price recorded when the buy order was placed.
    opened_slot:
        Slot number at which this position was opened; retained for diagnostics.
    peak_price:
        Highest SOL-per-token price observed since the position was opened;
        updated on every ``TradeEvent`` and used to compute the stop-loss level.
    """

    mint: str
    invested_lamports: int
    estimated_token_quantity: float
    entry_price: float
    opened_slot: int
    peak_price: float


@dataclass
class MarketState:
    """Track live reserves and the per-slot price history for one token.

    At most one price sample is stored per slot: a later ``TradeEvent`` arriving
    in the same slot replaces the slot's earlier entry rather than appending a
    second point.  This keeps the history list a clean, deduplicated series of
    slot observations.

    Attributes
    ----------
    mint:
        On-chain mint address this state belongs to.
    real_sol_reserves:
        Latest observed SOL reserve from the bonding curve (in lamports as a
        raw float, as delivered by the event stream).
    real_token_reserves:
        Latest observed token reserve from the bonding curve.
    last_price:
        Most recently computed SOL-per-token price; zero until the first valid
        reserve pair is observed.
    price_by_slot:
        Ordered list of ``(slot, price)`` tuples, oldest first, capped at
        ``max_history_slots``.  Each slot appears at most once.
    """

    mint: str
    real_sol_reserves: float = 0.0
    real_token_reserves: float = 0.0
    last_price: float = 0.0
    price_by_slot: list[tuple[int, float]] = field(default_factory=list)

    def update_reserves(
        self,
        new_sol_reserves: float,
        new_token_reserves: float,
    ) -> None:
        """Store the latest reserve values and recompute the implied price.

        The implied price is updated only when both reserves are strictly
        positive, preventing division-by-zero and nonsensical negative prices.

        Parameters
        ----------
        new_sol_reserves:
            Observed SOL reserve for this slot's trade or create event.
        new_token_reserves:
            Observed token reserve for the same event.
        """
        self.real_sol_reserves = new_sol_reserves
        self.real_token_reserves = new_token_reserves
        if new_sol_reserves > 0 and new_token_reserves > 0:
            self.last_price = new_sol_reserves / new_token_reserves

    def record_slot_price(
        self,
        slot: int,
        price: float,
        history_slot_limit: int,
    ) -> None:
        """Record a price observation for ``slot``, enforcing the history cap.

        If the most recent entry already belongs to ``slot`` it is replaced
        in-place so that each slot contributes exactly one sample.  Once the
        list length exceeds ``history_slot_limit``, the oldest entry is dropped.
        Non-finite or non-positive prices are silently rejected.

        Parameters
        ----------
        slot:
            Slot number associated with the price observation.
        price:
            SOL-per-token price to record.
        history_slot_limit:
            Maximum number of per-slot samples to retain; older entries are
            evicted when this cap is exceeded.
        """
        if price <= 0 or not math.isfinite(price):
            return
        if self.price_by_slot and self.price_by_slot[-1][0] == slot:
            # Replace the earlier sample for this same slot with the latest
            # price so each slot contributes exactly one observation.
            self.price_by_slot[-1] = (slot, price)
        else:
            self.price_by_slot.append((slot, price))
        if len(self.price_by_slot) > history_slot_limit:
            self.price_by_slot = self.price_by_slot[-history_slot_limit:]
        self.last_price = price

    def compute_decayed_momentum(
        self,
        current_slot: int,
        decay_factor_per_slot: float,
    ) -> float:
        """Return the slot-decayed momentum signal for entry decisions.

        Raw momentum is the relative difference between a recency-weighted
        short-window average (last three slot samples) and a recency-weighted
        full-history average.  The raw value is then multiplied by
        ``decay_factor_per_slot`` raised to the number of slots elapsed since
        the most recent price sample, so stale signals lose strength rapidly.

        A signal computed in the same slot as the latest trade has zero elapsed
        slots and therefore no decay.  Each subsequent slot without a new trade
        applies one additional multiplicative step of ``decay_factor_per_slot``.

        Parameters
        ----------
        current_slot:
            The slot number at which the signal is being evaluated.
        decay_factor_per_slot:
            Multiplicative decay applied per elapsed slot; must be in [0, 1].

        Returns
        -------
        float
            The decayed momentum score.  Positive values indicate upward price
            momentum; negative values indicate downward momentum.  Zero is
            returned when fewer than two slot samples are available.

        Raises
        ------
        ValueError
            If ``decay_factor_per_slot`` is outside the [0, 1] range.
        """
        if not 0 <= decay_factor_per_slot <= 1:
            raise ValueError(
                f"decay_factor_per_slot must be in [0, 1], got {decay_factor_per_slot}"
            )
        if len(self.price_by_slot) < 2:
            # Not enough slot observations to form a meaningful momentum signal.
            return 0.0

        all_slot_prices = [price for _, price in self.price_by_slot]

        # Short window: use at most the last three slot samples so that very
        # recent price action dominates the numerator of the momentum ratio.
        short_window_size = min(3, len(all_slot_prices))
        short_window_prices = all_slot_prices[-short_window_size:]

        recency_weighted_short_avg = _recency_weighted_average(short_window_prices)
        recency_weighted_full_avg = _recency_weighted_average(all_slot_prices)

        if recency_weighted_full_avg <= 0:
            return 0.0

        # Raw momentum: fractional excess of recent prices over the full history.
        raw_momentum_signal = (
            recency_weighted_short_avg - recency_weighted_full_avg
        ) / recency_weighted_full_avg

        # Apply slot-based decay: each slot elapsed since the latest trade
        # multiplies the signal by decay_factor_per_slot once.
        most_recent_sample_slot = self.price_by_slot[-1][0]
        slots_elapsed_since_sample = max(0, current_slot - most_recent_sample_slot)
        decayed_momentum = (
            raw_momentum_signal * decay_factor_per_slot ** slots_elapsed_since_sample
        )
        return decayed_momentum


# ---------------------------------------------------------------------------
# Portfolio accounting
# ---------------------------------------------------------------------------

class Portfolio:
    """Manage paper cash, open positions, and drawdown entirely independently.

    The ``Portfolio`` class owns no strategy logic.  It is a pure accounting
    layer that tracks available cash, open ``Position`` objects, and delegates
    all drawdown accounting to an embedded ``DrawdownTracker``.  The ``Bot``
    class calls into it to open and close positions; this separation makes
    unit-testing straightforward.

    Attributes
    ----------
    cash_lamports:
        Paper cash currently available for new buy orders.
    positions:
        Mapping from mint address to the corresponding open ``Position``.
    drawdown_tracker:
        Dedicated ``DrawdownTracker`` instance that owns all drawdown state,
        including the equity high-water mark, the worst-case drawdown fraction,
        the trading-halt flag, and the full per-batch snapshot history.  Access
        ``portfolio.drawdown_tracker.trading_halted`` to read the halt flag and
        ``portfolio.drawdown_tracker.drawdown_summary()`` for diagnostics.
    """

    def __init__(
        self,
        initial_cash_lamports: int,
        drawdown_halt_threshold: float = 0.45,
    ) -> None:
        """Initialise an empty portfolio with the specified starting cash balance.

        This is called once during the hello phase before any batch messages
        are processed.  A ``DrawdownTracker`` is constructed at the same time
        and seeded with the initial equity so its high-water mark is correct
        from the very first batch.

        Parameters
        ----------
        initial_cash_lamports:
            Paper cash to issue at start-up.  Must be non-negative.
        drawdown_halt_threshold:
            Fraction of peak equity at which the ``DrawdownTracker`` permanently
            halts trading.  Defaults to 0.45 (45 %).  Must be in (0, 1].

        Raises
        ------
        ValueError
            If ``initial_cash_lamports`` is negative.
        """
        if initial_cash_lamports < 0:
            raise ValueError(
                f"initial_cash_lamports cannot be negative, got {initial_cash_lamports}"
            )
        self.cash_lamports: int = initial_cash_lamports
        self.positions: dict[str, Position] = {}
        # Construct the tracker with the starting equity so the high-water mark
        # is correctly seeded before any batch events are processed.
        self.drawdown_tracker: DrawdownTracker = DrawdownTracker(
            initial_equity_lamports=initial_cash_lamports,
            halt_threshold=drawdown_halt_threshold,
        )

    @property
    def trading_halted(self) -> bool:
        """Convenience proxy: ``True`` when the drawdown tracker has halted trading.

        Reading ``portfolio.trading_halted`` is equivalent to reading
        ``portfolio.drawdown_tracker.trading_halted`` and is kept for backward
        compatibility with call sites that check the flag directly.
        """
        return self.drawdown_tracker.trading_halted

    def mark_to_market_equity(self, latest_market_prices: dict[str, float]) -> int:
        """Compute total equity as cash plus the marked-to-market position value.

        Each open position is valued using the most recently observed price for
        its mint.  When no post-entry price is available, the original entry
        price is used as a conservative fallback so equity is never zero while
        a position is held.

        Parameters
        ----------
        latest_market_prices:
            Mapping from mint address to its most recently known SOL-per-token
            price.  Mints absent from this dict fall back to entry price.

        Returns
        -------
        int
            Total portfolio equity in lamports.
        """
        open_position_value_lamports = 0
        for position_mint, position in self.positions.items():
            current_price = latest_market_prices.get(
                position_mint,
                position.entry_price,
            )
            if current_price > 0 and math.isfinite(current_price):
                open_position_value_lamports += int(
                    position.estimated_token_quantity * current_price
                )
        return self.cash_lamports + open_position_value_lamports

    def refresh_drawdown_and_halt_check(
        self,
        latest_market_prices: dict[str, float],
        drawdown_halt_limit: float,
        batch_seq: int | None = None,
        slot: int = 0,
    ) -> float:
        """Recompute mark-to-market equity and delegate drawdown tracking.

        Called once per batch (and once per event within a batch) so the
        ``DrawdownTracker`` receives a fresh equity reading after every state
        change.  All halt logic, high-water mark updates, and history recording
        are handled inside the tracker; this method is a thin bridge between
        ``mark_to_market_equity`` and ``DrawdownTracker.record``.

        Parameters
        ----------
        latest_market_prices:
            Current SOL-per-token prices for all tracked mints; used by
            ``mark_to_market_equity`` to value open positions.
        drawdown_halt_limit:
            Passed through for validation only; the tracker's own threshold
            (set at construction) is what actually governs the halt.  Kept as
            a parameter for call-site clarity.  Must be in (0, 1].
        batch_seq:
            Sequence number of the current batch, forwarded to the tracker's
            snapshot history.  ``None`` between batches.
        slot:
            Slot number of the current batch, forwarded to the tracker.

        Returns
        -------
        float
            The drawdown fraction computed from the latest equity reading.

        Raises
        ------
        ValueError
            If ``drawdown_halt_limit`` is outside (0, 1].
        """
        if not 0 < drawdown_halt_limit <= 1:
            raise ValueError(
                f"drawdown_halt_limit must be in (0, 1], got {drawdown_halt_limit}"
            )
        current_equity_lamports = self.mark_to_market_equity(latest_market_prices)
        # Delegate all drawdown arithmetic and halt logic to the tracker.
        # The tracker appends a DrawdownSnapshot to its history on every call,
        # giving a complete audit trail of equity and drawdown across all batches.
        return self.drawdown_tracker.record(
            current_equity_lamports=current_equity_lamports,
            batch_seq=batch_seq,
            slot=slot,
        )

    def has_open_position(self, mint: str) -> bool:
        """Return ``True`` when the portfolio currently holds a position in ``mint``.

        Parameters
        ----------
        mint:
            Token mint address to query.
        """
        return mint in self.positions

    def can_accept_new_position(self, mint: str, max_allowed_positions: int) -> bool:
        """Return ``True`` when a new position in ``mint`` fits within the slot limit.

        A new position is refused if one already exists for the same mint, or if
        the portfolio is already at ``max_allowed_positions`` open positions.

        Parameters
        ----------
        mint:
            Token mint address being considered for a buy order.
        max_allowed_positions:
            Upper bound on simultaneous open positions.
        """
        return (
            mint not in self.positions
            and len(self.positions) < max_allowed_positions
        )

    def open_new_position(
        self,
        mint: str,
        order_amount_lamports: int,
        entry_price_sol_per_token: float,
        opened_at_slot: int,
    ) -> Position:
        """Reserve cash and create an accounting record for a freshly filled buy.

        This method is called immediately after a buy order is appended to the
        slot's orders list so the portfolio reflects the commitment in the same
        batch.

        Parameters
        ----------
        mint:
            Token mint address being purchased.
        order_amount_lamports:
            Lamports committed to this position; deducted from available cash.
        entry_price_sol_per_token:
            SOL-per-token price used to estimate the token quantity received.
        opened_at_slot:
            Slot number at which the buy order is placed.

        Returns
        -------
        Position
            The newly created ``Position`` record.

        Raises
        ------
        ValueError
            On invalid inputs or if a position for ``mint`` already exists.
        """
        if order_amount_lamports <= 0:
            raise ValueError(
                f"order_amount_lamports must be positive, got {order_amount_lamports}"
            )
        if entry_price_sol_per_token <= 0 or not math.isfinite(
            entry_price_sol_per_token
        ):
            raise ValueError(
                f"entry_price must be finite and positive, got {entry_price_sol_per_token}"
            )
        if order_amount_lamports > self.cash_lamports:
            raise ValueError(
                f"cannot invest {order_amount_lamports} lamports; "
                f"only {self.cash_lamports} available"
            )
        if mint in self.positions:
            raise ValueError(
                f"a position for mint {mint} already exists; cannot open another"
            )
        estimated_token_quantity = order_amount_lamports / entry_price_sol_per_token
        new_position = Position(
            mint=mint,
            invested_lamports=order_amount_lamports,
            estimated_token_quantity=estimated_token_quantity,
            entry_price=entry_price_sol_per_token,
            opened_slot=opened_at_slot,
            peak_price=entry_price_sol_per_token,
        )
        self.positions[mint] = new_position
        self.cash_lamports -= order_amount_lamports
        return new_position

    def close_position_with_proceeds(
        self,
        mint: str,
        estimated_proceeds_lamports: int,
    ) -> Position | None:
        """Remove an open position and credit estimated proceeds to paper cash.

        Called by the bot when a stop-loss or take-profit sell order is placed
        in the current slot's batch.

        Parameters
        ----------
        mint:
            Token mint address being sold.
        estimated_proceeds_lamports:
            Mark-to-market value of the position at the sell price; credited to
            cash.  Negative values are coerced to zero.

        Returns
        -------
        Position or None
            The closed ``Position``, or ``None`` if no position existed.
        """
        closed_position = self.positions.pop(mint, None)
        if closed_position is None:
            return None
        self.cash_lamports += max(0, estimated_proceeds_lamports)
        return closed_position

    def close_completed_curve_position(self, mint: str) -> Position | None:
        """Return the invested principal when a bonding curve completes.

        A ``CompleteEvent`` signals that the curve has migrated to a DEX and
        no further trades are possible on pump.fun.  The bot returns the full
        invested principal to available cash rather than attempting to estimate
        market proceeds, keeping accounting deterministic.

        Parameters
        ----------
        mint:
            Token mint address whose curve has completed.

        Returns
        -------
        Position or None
            The released ``Position``, or ``None`` if no position existed.
        """
        completed_position = self.positions.pop(mint, None)
        if completed_position is not None:
            # Return the original invested principal; no price-based estimate
            # is possible once the bonding curve has completed.
            self.cash_lamports += completed_position.invested_lamports
        return completed_position


# ---------------------------------------------------------------------------
# Core bot logic
# ---------------------------------------------------------------------------

class Bot:
    """Dispatch hello and batch messages into deterministic paper orders.

    The ``Bot`` owns the strategy logic and delegates all accounting to its
    ``Portfolio`` instance and all market data to its ``market`` dict of
    ``MarketState`` objects.  Given identical input messages and configuration
    the same orders are always produced.

    Attributes
    ----------
    config:
        Strategy thresholds and limits; constructed at hello-phase boot.
    portfolio:
        Paper portfolio tracking cash, open positions, and drawdown.
    market:
        Mapping from mint address to the token's live ``MarketState``.
    """

    def __init__(self, config: BotConfig | None = None) -> None:
        """Initialise strategy configuration, portfolio, and market state.

        This constructor doubles as the hello-phase initialisation routine.
        It is called once before the first batch message is delivered.  No
        randomness is involved; all outputs are fully determined by inputs.

        Parameters
        ----------
        config:
            Optional ``BotConfig`` override.  ``BotConfig()`` defaults are
            used when ``None`` is supplied.
        """
        # Hello phase: set up all mutable state before the first batch arrives.
        self.config: BotConfig = config or BotConfig()
        # Pass the configured halt threshold so the DrawdownTracker inside the
        # portfolio is seeded with the correct 45 % limit from the very start.
        self.portfolio: Portfolio = Portfolio(
            initial_cash_lamports=self.config.initial_cash_lamports,
            drawdown_halt_threshold=self.config.max_portfolio_drawdown_fraction,
        )
        self.market: dict[str, MarketState] = {}

    # ------------------------------------------------------------------
    # Batch message entry point
    # ------------------------------------------------------------------

    def process_batch_message(self, batch_message: dict[str, Any]) -> dict[str, Any]:
        """Process a single ``batch`` message and return an ``orders`` response.

        Each batch carries a slot number, a sequence counter, and a list of
        events.  The bot processes events in order, then scans open positions
        for exit triggers, and finally assembles the response object whose
        ``orders`` list may be empty when no trade conditions are met.

        Parameters
        ----------
        batch_message:
            Parsed JSON object with ``type``, ``slot``, ``seq``, and ``events``
            keys as delivered by the pump.fun harness.

        Returns
        -------
        dict
            Response object: ``{"type": "reply", "seq": ..., "orders": [...]}``.

        Raises
        ------
        ValueError
            If the ``events`` field is not a list.
        """
        current_slot: int = _to_int(batch_message.get("slot", 0), 0)
        sequence_number = batch_message.get("seq")
        raw_batch_events = batch_message.get("events", [])

        if not isinstance(raw_batch_events, list):
            raise ValueError(
                f"batch message events field must be a list, "
                f"got {type(raw_batch_events).__name__}"
            )

        slot_orders = self._build_orders_for_slot(
            raw_batch_events,
            current_slot,
            sequence_number,
        )
        return {"type": "reply", "seq": sequence_number, "orders": slot_orders}

    # ------------------------------------------------------------------
    # Slot-level order construction
    # ------------------------------------------------------------------

    def _build_orders_for_slot(
        self,
        batch_events: list[Any],
        current_slot: int,
        batch_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        """Coordinate all order-generation work for one slot's batch of events.

        This method is a pure coordinator.  It delegates each distinct phase of
        the per-slot pipeline to a clearly named helper and returns the
        accumulated orders list.  Reading it top-to-bottom gives a complete
        summary of what happens every slot:

          1. ``_validate_batch_events``      — reject malformed event objects
          2. ``_refresh_portfolio_risk``     — update drawdown, check halt flag
          3. ``_collect_orders_from_events`` — dispatch events, gather buy orders
          4. ``_collect_orders_from_exits``  — scan positions for sell triggers

        The shared ``already_ordered_mints`` set is threaded through steps 3
        and 4 to guarantee at most one order per mint per slot.

        Parameters
        ----------
        batch_events:
            List of raw event dicts extracted from the batch message.
        current_slot:
            Slot number for this batch; used for price recording, momentum
            decay, order client-id stamping, and drawdown snapshot labelling.
        batch_seq:
            Sequence number of the enclosing batch message; forwarded to the
            ``DrawdownTracker`` for per-snapshot provenance labels.

        Returns
        -------
        list of dict
            All buy and sell orders generated for this slot, in emission order.

        Raises
        ------
        ValueError
            If any element of ``batch_events`` is not a JSON object.
        """
        # Mints that already carry an order this slot; prevents contradictory
        # or duplicate orders within a single batch.
        already_ordered_mints: set[str] = set()

        # Phase 1: validate the entire batch before mutating any state.
        self._validate_batch_events(batch_events)

        # Phase 2: refresh mark-to-market equity and check the drawdown halt.
        self._refresh_portfolio_risk(batch_seq=batch_seq, slot=current_slot)

        # Phase 3: dispatch each event and collect any resulting buy orders.
        slot_orders = self._collect_orders_from_events(
            batch_events,
            current_slot,
            already_ordered_mints,
            batch_seq=batch_seq,
        )

        # Phase 4: scan open positions for stop-loss and take-profit exits.
        self._collect_orders_from_exits(
            current_slot,
            slot_orders,
            already_ordered_mints,
        )

        return slot_orders

    def _validate_batch_events(self, batch_events: list[Any]) -> None:
        """Raise ``ValueError`` if any event in the batch is not a JSON object.

        All events are validated before any state is mutated so that a single
        malformed entry cannot leave the bot in a partially updated state.
        Validation is intentionally a separate, named step so it is visibly
        distinct from dispatch in ``_build_orders_for_slot``.

        Parameters
        ----------
        batch_events:
            The raw list extracted from the batch message's ``events`` field.

        Raises
        ------
        ValueError
            If any element is not a ``dict``, naming the offending index and
            the type that was found instead.
        """
        for event_index, candidate_event in enumerate(batch_events):
            if not isinstance(candidate_event, dict):
                raise ValueError(
                    f"batch event at index {event_index} must be a JSON object, "
                    f"got {type(candidate_event).__name__}"
                )

    def _collect_orders_from_events(
        self,
        batch_events: list[Any],
        current_slot: int,
        already_ordered_mints: set[str],
        batch_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        """Dispatch every event in the batch and return any buy orders produced.

        Events are processed in arrival order.  After each ``TradeEvent`` the
        bot evaluates whether to open a new position; ``CreateEvent`` and
        ``CompleteEvent`` never produce orders directly but do update market
        and portfolio state that later events or the exit scan may rely on.

        The returned list contains only buy orders.  Sell orders are appended
        later by ``_collect_orders_from_exits``, which shares the same
        ``already_ordered_mints`` set to prevent per-mint duplicates.

        Parameters
        ----------
        batch_events:
            Validated list of event dicts from the batch message.
        current_slot:
            Slot number for this batch.
        already_ordered_mints:
            Mints that must not receive a second order this slot; updated
            in-place whenever a buy order is emitted.
        batch_seq:
            Sequence number of the enclosing batch, forwarded to
            ``_refresh_portfolio_risk`` for drawdown snapshot labelling.

        Returns
        -------
        list of dict
            Buy orders generated by the events in this batch, in arrival order.
        """
        slot_orders: list[dict[str, Any]] = []
        for slot_event in batch_events:
            self._dispatch_event(
                slot_event,
                current_slot,
                slot_orders,
                already_ordered_mints,
                batch_seq=batch_seq,
            )
        return slot_orders

    def _collect_orders_from_exits(
        self,
        current_slot: int,
        slot_orders: list[dict[str, Any]],
        already_ordered_mints: set[str],
    ) -> None:
        """Append stop-loss and take-profit sell orders to ``slot_orders``.

        Wraps ``_process_exit_triggers`` with a trading-halt guard so the
        caller in ``_build_orders_for_slot`` does not need to inspect the halt
        flag directly.  When trading is halted, no sell orders are appended and
        the method returns immediately without touching ``slot_orders``.

        This is a named coordination step rather than an inline ``if`` block
        so the exit-collection phase is as legible as the event-collection
        phase in the slot pipeline.

        Parameters
        ----------
        current_slot:
            Slot number for this batch; used for order client-id stamping.
        slot_orders:
            Accumulator shared with ``_collect_orders_from_events``; sell
            orders are appended in-place.
        already_ordered_mints:
            Mints that already have a buy order this slot; positions for these
            mints are skipped to avoid contradictory orders in the same batch.
        """
        if self.portfolio.trading_halted:
            return
        self._process_exit_triggers(
            current_slot,
            slot_orders,
            already_ordered_mints,
        )

    # ------------------------------------------------------------------
    # Event dispatching
    # ------------------------------------------------------------------

    def _dispatch_event(
        self,
        event: dict[str, Any],
        slot: int,
        slot_orders: list[dict[str, Any]],
        already_ordered_mints: set[str],
        batch_seq: int | None = None,
    ) -> None:
        """Route one event from the slot's batch to its handler.

        Unknown event types are silently ignored so that future protocol
        extensions do not break existing bot instances.

        ``batch_seq`` and ``slot`` are forwarded to ``_refresh_portfolio_risk``
        after each event so every ``DrawdownTracker`` snapshot carries correct
        provenance labels.

        Parameters
        ----------
        event:
            A single event dict from the batch.
        slot:
            Slot number for this batch.
        slot_orders:
            Accumulator for orders to be included in the slot response.
        already_ordered_mints:
            Mints that already have an order in this slot; updated in-place
            by buy and sell order emission to prevent duplicate orders.
        batch_seq:
            Sequence number of the enclosing batch, forwarded to the tracker.
        """
        event_type = event.get("type")
        raw_mint = event.get("mint")
        if not isinstance(raw_mint, str) or not raw_mint:
            return
        token_mint: str = raw_mint

        if event_type == "CreateEvent":
            # Hello phase for this individual token: register it in the market.
            self._handle_create_event(event, token_mint)
            self._refresh_portfolio_risk(batch_seq=batch_seq, slot=slot)

        elif event_type == "TradeEvent":
            self._handle_trade_event(event, token_mint, slot)
            self._refresh_portfolio_risk(batch_seq=batch_seq, slot=slot)
            # After updating the market, consider a new buy for this slot.
            if not self.portfolio.trading_halted:
                self._maybe_place_buy_order(
                    token_mint,
                    slot,
                    slot_orders,
                    already_ordered_mints,
                )

        elif event_type == "CompleteEvent":
            self._handle_complete_event(token_mint)
            self._refresh_portfolio_risk(batch_seq=batch_seq, slot=slot)

    # ------------------------------------------------------------------
    # Individual event handlers
    # ------------------------------------------------------------------

    def _handle_create_event(self, event: dict[str, Any], mint: str) -> None:
        """Hello-phase handler: register a new token's initial market state.

        Tokens flagged as mayhem-mode or cashback-enabled are excluded from
        tracking; no ``MarketState`` is created for them so they can never
        trigger a buy order in any subsequent slot.

        Parameters
        ----------
        event:
            The ``CreateEvent`` dict from the batch.
        mint:
            Token mint address extracted from the event.
        """
        # Exclude tokens with structural flags that indicate unreliable pricing.
        if event.get("is_mayhem_mode") or event.get("is_cashback_enabled"):
            return

        # Initialise a fresh MarketState if this mint has not been seen before.
        token_market = self.market.setdefault(mint, MarketState(mint=mint))
        initial_sol_reserves = _to_number(event.get("real_sol_reserves", 0), 0.0)
        initial_token_reserves = _to_number(event.get("real_token_reserves", 0), 0.0)
        token_market.update_reserves(initial_sol_reserves, initial_token_reserves)

    def _handle_trade_event(
        self,
        event: dict[str, Any],
        mint: str,
        slot: int,
    ) -> None:
        """Update reserves and record a slot price sample for a live trade.

        If the token was not registered during the hello phase (i.e. its
        ``CreateEvent`` was excluded), the event is silently ignored.  When a
        valid price is derived it is committed to the per-slot price history and
        the peak price of any open position in this token is updated.

        Parameters
        ----------
        event:
            The ``TradeEvent`` dict from the batch.
        mint:
            Token mint address extracted from the event.
        slot:
            Current slot number; used to index the per-slot price history.
        """
        token_market = self.market.get(mint)
        if token_market is None:
            return

        # Fall back to previously stored reserves when the event omits them.
        updated_sol_reserves = _to_number(
            event.get("real_sol_reserves", token_market.real_sol_reserves),
            token_market.real_sol_reserves,
        )
        updated_token_reserves = _to_number(
            event.get("real_token_reserves", token_market.real_token_reserves),
            token_market.real_token_reserves,
        )
        token_market.update_reserves(updated_sol_reserves, updated_token_reserves)

        if token_market.last_price > 0:
            # Commit a price sample for this slot; replaces any earlier sample
            # from the same slot so the history stays deduplicated.
            token_market.record_slot_price(
                slot,
                token_market.last_price,
                self.config.max_history_slots,
            )
            # Keep the open position's peak price current.
            open_position = self.portfolio.positions.get(mint)
            if open_position is not None:
                open_position.peak_price = max(
                    open_position.peak_price,
                    token_market.last_price,
                )

    def _handle_complete_event(self, mint: str) -> None:
        """Release the principal reserved for ``mint`` when its curve completes.

        Parameters
        ----------
        mint:
            Token mint address whose bonding curve has completed migration.
        """
        self.portfolio.close_completed_curve_position(mint)

    # ------------------------------------------------------------------
    # Risk management
    # ------------------------------------------------------------------

    def _refresh_portfolio_risk(
        self,
        batch_seq: int | None = None,
        slot: int = 0,
    ) -> float:
        """Recompute mark-to-market equity and feed the ``DrawdownTracker``.

        Builds a price snapshot for every tracked token and calls
        ``Portfolio.refresh_drawdown_and_halt_check``, which in turn calls
        ``DrawdownTracker.record`` to update the high-water mark, worst-case
        drawdown, halt flag, and snapshot history.

        Called at the start of each batch and after each event within a batch
        so the halt flag is always up-to-date before buy decisions are made.
        The ``batch_seq`` and ``slot`` arguments are forwarded so each
        ``DrawdownSnapshot`` in the tracker's history is correctly labelled.

        Parameters
        ----------
        batch_seq:
            Sequence number of the current batch; ``None`` between batches or
            when called outside of a specific batch context.
        slot:
            Slot number of the current batch; 0 when called outside a batch.

        Returns
        -------
        float
            The current drawdown fraction from the equity high-water mark.
        """
        snapshot_prices: dict[str, float] = {
            mint: state.last_price
            for mint, state in self.market.items()
            if state.last_price > 0
        }
        return self.portfolio.refresh_drawdown_and_halt_check(
            latest_market_prices=snapshot_prices,
            drawdown_halt_limit=self.config.max_portfolio_drawdown_fraction,
            batch_seq=batch_seq,
            slot=slot,
        )

    # ------------------------------------------------------------------
    # Entry decisions
    # ------------------------------------------------------------------

    def _maybe_place_buy_order(
        self,
        mint: str,
        slot: int,
        slot_orders: list[dict[str, Any]],
        already_ordered_mints: set[str],
    ) -> None:
        """Evaluate momentum and place one confidence-sized buy order if warranted.

        All entry guard conditions are checked before computing the momentum
        signal to avoid unnecessary arithmetic.  When all conditions pass, the
        order is appended to ``slot_orders`` and the position is opened in the
        same call so the portfolio is consistent for the remainder of the slot's
        batch processing.

        Parameters
        ----------
        mint:
            Token mint under evaluation.
        slot:
            Current slot number; used for momentum decay and order stamping.
        slot_orders:
            Accumulator for orders to be emitted in the slot response.
        already_ordered_mints:
            Mints that already have an order this slot; prevents duplicate
            orders within a single batch.
        """
        token_market = self.market.get(mint)
        if token_market is None or mint in already_ordered_mints:
            return

        # Require a minimum number of observed slot samples before trading.
        if len(token_market.price_by_slot) < self.config.minimum_history_slots:
            return

        # Refuse if the portfolio already holds this mint or is at capacity.
        if not self.portfolio.can_accept_new_position(mint, self.config.max_positions):
            return

        # Require valid reserves to allow a position-size cap computation.
        if token_market.real_sol_reserves <= 0 or token_market.last_price <= 0:
            return

        # Compute the slot-decayed momentum signal.
        decayed_momentum_signal = token_market.compute_decayed_momentum(
            slot,
            self.config.momentum_decay_per_slot,
        )
        confidence_score = self._confidence_score_for_momentum(decayed_momentum_signal)
        if confidence_score < self.config.minimum_confidence:
            return

        # Size the order based on confidence and reserve/cash constraints.
        proposed_order_lamports = self._compute_position_size_lamports(
            confidence_score,
            token_market.real_sol_reserves,
        )
        if proposed_order_lamports < MIN_ORDER_LAMPORTS:
            return

        # Open the position in the portfolio and emit the order for this slot.
        self.portfolio.open_new_position(
            mint,
            proposed_order_lamports,
            token_market.last_price,
            slot,
        )
        slot_orders.append(
            self._build_order_record(mint, "buy", proposed_order_lamports, slot)
        )
        already_ordered_mints.add(mint)

    def _confidence_score_for_momentum(self, momentum_signal: float) -> float:
        """Map a decayed momentum value to a deterministic [0, 1] confidence score.

        The mapping is a linear interpolation between ``minimum_momentum`` (→ 0)
        and ``full_confidence_momentum`` (→ 1), clamped so values outside the
        band saturate at the extremes.

        Parameters
        ----------
        momentum_signal:
            Decayed momentum value returned by ``compute_decayed_momentum``.

        Returns
        -------
        float
            Confidence score in [0.0, 1.0].

        Raises
        ------
        ValueError
            If ``full_confidence_momentum`` does not exceed ``minimum_momentum``.
        """
        lower_bound = self.config.minimum_momentum
        upper_bound = self.config.full_confidence_momentum
        if upper_bound <= lower_bound:
            raise ValueError(
                f"full_confidence_momentum ({upper_bound}) must exceed "
                f"minimum_momentum ({lower_bound})"
            )
        raw_score = (momentum_signal - lower_bound) / (upper_bound - lower_bound)
        return _clamp(raw_score, 0.0, 1.0)

    def _compute_position_size_lamports(
        self,
        confidence_score: float,
        sol_reserves: float,
    ) -> int:
        """Compute a confidence-scaled buy size subject to reserve and cash caps.

        The raw order size scales linearly from ``base_order_lamports`` at zero
        confidence to ``maximum_order_lamports`` at full confidence.  It is then
        capped at ``maximum_reserve_fraction`` of the token's real SOL reserves
        to avoid excessive market impact, and finally capped by available cash.

        Parameters
        ----------
        confidence_score:
            Entry confidence in [0.0, 1.0]; values outside this range are
            clamped internally.
        sol_reserves:
            Real SOL reserves of the token's bonding curve, in lamports.

        Returns
        -------
        int
            Proposed order size in lamports; may be below ``MIN_ORDER_LAMPORTS``
            and should be discarded by the caller in that case.
        """
        bounded_confidence = _clamp(confidence_score, 0.0, 1.0)
        order_size_range = (
            self.config.maximum_order_lamports - self.config.base_order_lamports
        )
        # Scale the raw order size linearly with confidence.
        confidence_scaled_lamports = self.config.base_order_lamports + round(
            order_size_range * bounded_confidence
        )
        # Cap by a fraction of the token's real reserves to limit market impact.
        reserve_impact_cap_lamports = int(
            sol_reserves * self.config.maximum_reserve_fraction
        )
        return min(
            confidence_scaled_lamports,
            reserve_impact_cap_lamports,
            self.portfolio.cash_lamports,
        )

    # ------------------------------------------------------------------
    # Exit triggers
    # ------------------------------------------------------------------

    def _process_exit_triggers(
        self,
        slot: int,
        slot_orders: list[dict[str, Any]],
        already_ordered_mints: set[str],
    ) -> None:
        """Scan every open position and emit stop-loss or take-profit sell orders.

        Positions are scanned in deterministic sorted-mint order to keep output
        reproducible across runs.  A mint that already has a buy order in this
        slot is skipped to avoid contradictory orders in the same batch.

        Parameters
        ----------
        slot:
            Current slot number; used for order client-id stamping.
        slot_orders:
            Accumulator for orders to be included in the slot response.
        already_ordered_mints:
            Mints that already have an order this slot.
        """
        for mint in sorted(self.portfolio.positions):
            if mint in already_ordered_mints:
                continue
            token_market = self.market.get(mint)
            open_position = self.portfolio.positions.get(mint)
            if (
                token_market is None
                or open_position is None
                or token_market.last_price <= 0
            ):
                continue

            stop_loss_trigger_price = open_position.peak_price * (
                1.0 - self.config.stop_loss_fraction
            )
            take_profit_trigger_price = open_position.entry_price * (
                1.0 + self.config.take_profit_fraction
            )

            if token_market.last_price <= stop_loss_trigger_price:
                self._emit_sell_order(
                    mint, open_position, slot, "stop_loss", slot_orders
                )
                already_ordered_mints.add(mint)

            elif token_market.last_price >= take_profit_trigger_price:
                self._emit_sell_order(
                    mint, open_position, slot, "take_profit", slot_orders
                )
                already_ordered_mints.add(mint)

    def _emit_sell_order(
        self,
        mint: str,
        position: Position,
        slot: int,
        exit_reason: str,
        slot_orders: list[dict[str, Any]],
    ) -> None:
        """Build a full-position sell order and update paper accounting.

        Estimated proceeds are computed by scaling the invested principal by the
        ratio of the current price to the entry price, keeping arithmetic
        deterministic and avoiding floating-point token-quantity round-trips.

        Parameters
        ----------
        mint:
            Token mint address being sold.
        position:
            The open ``Position`` record for this mint.
        slot:
            Current slot number.
        exit_reason:
            Label explaining the exit trigger; included in the order record.
        slot_orders:
            Accumulator for orders to be included in the slot response.
        """
        token_market = self.market[mint]
        # Estimate proceeds by scaling the original investment by the price ratio.
        estimated_exit_proceeds_lamports = int(
            position.invested_lamports
            * token_market.last_price
            / position.entry_price
        )
        sell_order = self._build_order_record(
            mint,
            "sell",
            position.invested_lamports,
            slot,
            token_quantity=position.estimated_token_quantity,
            reason=exit_reason,
        )
        slot_orders.append(sell_order)
        self.portfolio.close_position_with_proceeds(
            mint,
            estimated_exit_proceeds_lamports,
        )

    # ------------------------------------------------------------------
    # Order construction
    # ------------------------------------------------------------------

    def _build_order_record(
        self,
        mint: str,
        order_side: str,
        amount_lamports: int,
        slot: int,
        token_quantity: float | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Assemble the stable JSON-serialisable order dict for one trade.

        The ``client_id`` field encodes side, mint prefix, and slot so it is
        both human-readable during debugging and unique within any single slot.

        Parameters
        ----------
        mint:
            Token mint address.
        order_side:
            ``"buy"`` or ``"sell"``.
        amount_lamports:
            Lamport amount for the order.
        slot:
            Slot number at which the order is placed; included in client_id.
        token_quantity:
            Estimated token quantity for sell orders; omitted from buy orders.
        reason:
            Human-readable exit reason for sell orders (``"stop_loss"`` or
            ``"take_profit"``); omitted for buy orders.

        Returns
        -------
        dict
            Order record ready to be appended to a slot's orders list.
        """
        order_record: dict[str, Any] = {
            "client_id": f"{order_side}_{mint[:8]}_{slot}",
            "mint": mint,
            "side": order_side,
            "amount_sol": amount_lamports,
            "max_slippage_bps": self.config.max_slippage_bps,
        }
        if token_quantity is not None:
            order_record["amount_tokens"] = token_quantity
        if reason is not None:
            order_record["reason"] = reason
        return order_record


# ---------------------------------------------------------------------------
# Pure utility functions
# ---------------------------------------------------------------------------

def _recency_weighted_average(ordered_price_values: Iterable[float]) -> float:
    """Compute a linearly recency-weighted average over the supplied price series.

    The oldest sample receives weight 1, the second-oldest weight 2, and so on,
    so the most recent sample in a list of length N receives weight N.  This
    ensures the momentum signal gives precedence to recent slot prices without
    discarding earlier history entirely.

    Parameters
    ----------
    ordered_price_values:
        Iterable of prices in chronological order (oldest first).

    Returns
    -------
    float
        The recency-weighted average, or 0.0 when the input is empty.
    """
    chronological_prices = list(ordered_price_values)
    if not chronological_prices:
        return 0.0
    num_samples = len(chronological_prices)
    # Triangular-number denominator for linear recency weights 1, 2, ..., N.
    total_recency_weight = num_samples * (num_samples + 1) / 2
    weighted_sum = sum(
        price * recency_weight
        for recency_weight, price in enumerate(chronological_prices, start=1)
    )
    return weighted_sum / total_recency_weight


def _clamp(numeric_value: float, minimum_value: float, maximum_value: float) -> float:
    """Constrain ``numeric_value`` to the inclusive range [minimum, maximum].

    Parameters
    ----------
    numeric_value:
        Value to constrain.
    minimum_value:
        Lower bound of the allowed range.
    maximum_value:
        Upper bound of the allowed range.

    Returns
    -------
    float
        The clamped value.
    """
    return max(minimum_value, min(numeric_value, maximum_value))


def _to_number(raw_json_value: Any, fallback_value: float) -> float:
    """Convert a raw JSON value to a finite float, returning ``fallback_value`` on failure.

    Boolean JSON values are intentionally rejected and mapped to the fallback
    to avoid silently treating ``true``/``false`` as 1.0/0.0.

    Parameters
    ----------
    raw_json_value:
        Arbitrary value as deserialised from JSON.
    fallback_value:
        Returned when conversion fails or the result is non-finite.

    Returns
    -------
    float
        A finite float, or ``fallback_value``.
    """
    if isinstance(raw_json_value, bool):
        return fallback_value
    try:
        converted = float(raw_json_value)
    except (TypeError, ValueError):
        return fallback_value
    return converted if math.isfinite(converted) else fallback_value


def _to_int(raw_json_value: Any, fallback_value: int) -> int:
    """Convert a raw JSON value to an integer, returning ``fallback_value`` on failure.

    Boolean values are rejected for the same reason as in ``_to_number``.

    Parameters
    ----------
    raw_json_value:
        Arbitrary value as deserialised from JSON.
    fallback_value:
        Returned when conversion fails or overflows.

    Returns
    -------
    int
        An integer, or ``fallback_value``.
    """
    if isinstance(raw_json_value, bool):
        return fallback_value
    try:
        return int(raw_json_value)
    except (TypeError, ValueError, OverflowError):
        return fallback_value


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _iter_input_lines(stream: Iterable[str]) -> Iterable[tuple[int, str]]:
    """Yield non-empty stripped lines from ``stream`` with their one-based line numbers.

    Empty lines (after stripping) are skipped so that the main loop never
    attempts to parse a blank line as a JSON object.

    Parameters
    ----------
    stream:
        Any iterable of raw input lines, typically ``sys.stdin``.

    Yields
    ------
    tuple[int, str]
        ``(line_number, stripped_line)`` for each non-empty line.
    """
    for line_number, raw_line in enumerate(stream, start=1):
        stripped_line = raw_line.strip()
        if not stripped_line:
            continue
        yield line_number, stripped_line


def _parse_json_object(input_line: str) -> dict[str, Any]:
    """Parse one JSON line and require the top-level value to be an object.

    Parameters
    ----------
    input_line:
        A single non-empty line read from standard input.

    Returns
    -------
    dict
        The parsed JSON object.

    Raises
    ------
    ValueError
        If the line is not valid JSON or its top-level value is not a dict.
    """
    try:
        parsed_value = json.loads(input_line)
    except json.JSONDecodeError as parse_error:
        raise ValueError(
            f"invalid JSON at column {parse_error.colno}: {parse_error.msg}"
        ) from parse_error
    if not isinstance(parsed_value, dict):
        raise ValueError(
            f"expected a JSON object at the top level, got {type(parsed_value).__name__}"
        )
    return parsed_value


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the hello, batch, and done phases over standard input and output.

    The main loop reads one JSON object per line from stdin:

    * A ``hello`` message triggers the bot's hello-phase initialisation.  The
      strategy is fully deterministic so the hello payload is currently a no-op,
      but the message type is recognised and handled cleanly for future use.
    * Each ``batch`` message is dispatched to ``Bot.process_batch_message``.
      The resulting ``orders`` response is serialised to compact JSON and
      written immediately to stdout so the harness can pipeline slot results.
    * A ``done`` message signals end-of-stream; the loop exits cleanly.
    * Malformed JSON or unknown message types are logged to stderr and skipped
      so a single bad line cannot terminate the bot mid-competition.
    """
    # Hello phase: construct the bot with default configuration.
    competition_bot = Bot()

    for line_number, input_line in _iter_input_lines(sys.stdin):
        try:
            incoming_message = _parse_json_object(input_line)
        except ValueError as parse_error:
            sys.stderr.write(
                f"Skipping malformed input on line {line_number}: {parse_error}\n"
            )
            continue

        message_type = incoming_message.get("type")

        if message_type == "hello":
            # Hello phase is intentionally deterministic; no seed or state
            # from the hello payload is required by the current strategy.
            continue

        if message_type == "done":
            # End-of-stream sentinel; exit the main loop cleanly.
            break

        if message_type == "batch":
            try:
                orders_response = competition_bot.process_batch_message(
                    incoming_message
                )
            except ValueError as batch_error:
                sys.stderr.write(
                    f"Skipping invalid batch on line {line_number}: {batch_error}\n"
                )
                continue
            # Flush immediately so the harness receives orders for each slot
            # before the next batch message is delivered.
            sys.stdout.write(
                json.dumps(orders_response, separators=(",", ":")) + "\n"
            )
            sys.stdout.flush()


if __name__ == "__main__":
    main()
