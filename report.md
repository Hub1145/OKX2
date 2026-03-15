# Comprehensive Project Audit Report: Data Handling, Fees, and Auto-Cal Logic

## 1. Executive Summary
This report details the findings from an audit of the Trading Bot project, specifically focusing on the correctness of position mapping (especially for Short positions), the consistency of fee usage throughout the platform, and the mathematical robustness of the Auto-Cal recovery system.

## 2. Position Side Mapping & Directional Correctness
### Finding: Negative Position Handling
In OKX One-way mode, positions are often reported with a `posSide` of `"net"`. Our audit confirms that the `PositionManager` correctly handles this by mapping negative quantities to the internal `short` side:
- **Code Reference**: `handlers/position_manager.py` -> `_map_side()`
- **Impact**: This mapping ensures that the bot correctly identifies Short positions even when the exchange provides minimal metadata.

### Finding: Directional PnL and Gap Logic
The Unrealized PnL (UPL) and "Gap" triggers for Short positions have been verified as mathematically correct:
- **UPL Calculation**: For Shorts, UPL is calculated as `(Entry - Market) * Qty`, meaning PnL increases as the market price falls.
- **Gap Trigger**: The "Add" trigger for Shorts correctly measures `(Market - Entry)`, triggering an "Add" only when the price rises above the short entry (into a loss).
- **Auto-Cal Math**: The recovery formulas in `AutoCalManager` use the side-mapped UPL, ensuring the "Need Add" amounts are directionally accurate for both Long and Short positions.

## 3. Fee Handling Consistency
### Finding: Centralized Fee Tracking
Previously noted inconsistencies in fee handling have been resolved. The platform now utilizes a centralized tracking system:
- **PositionManager**: Tracks `current_entry_fees` specifically for the current open position side. This is reset upon position closure.
- **Auto-Exit Triggers**: Triggers like "Mode 2" (Profit Target) now consistently reference these `used_fees`.
- **Dashboard Synchronization**: The `Net Profit` metric on the dashboard now reflects: `Unrealized PnL - Entry Fees - Cycle Realized Losses`.

## 4. Auto-Cal System (Mode 1 & 2)
### Finding: Mathematical Sensitivity ("Order Explosion") - RESOLVED
The "Need Add" calculation in `AutoCalManager` was identified as the root cause for extreme order sizes (e.g., the $210,774 incident).

- **The Root Cause**: The recovery formula `V = (-upl / (recovery_percent - target_surplus)) - current_notional` uses a denominator that approaches zero when the user's `Recovery %` is nearly equal to the `Profit Multiplier * Fee%`. Previously, a tiny `0.0001` buffer allowed a modest -$21 loss to balloon into a ~$210,000 order.
- **The Fix**:
    1. **Epsilon Hardening**: Increased the minimum denominator from `0.0001` to `0.005`. This alone prevents orders from exceeding safe multiples of the unrealized loss.
    2. **Sanity Ceiling**: Implemented a hard cap on all recovery trades. Orders are now restricted to **2x the Notional Cap** (Max Allowed * Leverage) or **2x Account Equity**, whichever is lower.
- **Impact**: Even with aggressive settings, the bot can no longer place orders that exceed a sane multiple of the account's total capacity, fixing the "bug" where the $50 limit was bypassed by 4000x.

### Finding: Unrestricted Recovery
As per design requirements, Auto-Cal trades are **unrestricted**. They bypass the `Max Allowed` loop budget and `Remaining Amount` limits. This allows the bot to utilize the full account balance to execute mathematically required recovery trades, preventing "Trade Leakage" where a position is left unmanaged because its specific loop ran out of budget.

## 5. Platform Data & UI Persistence
### Finding: Nullish Coalescing in Frontend
The UI has been audited for data persistence bugs.
- **Refactoring**: In `static/js/app.js`, logic has been updated from `||` (logical OR) to `??` (nullish coalescing) for critical fields like `add_pos_max_count` and `add_pos_gap_offset`.
- **Benefit**: This allows the user to save `0` or `false` without the frontend overwriting these values with default ones.

### Finding: Dashboard Quick-Save Parity
The "Quick Sync" inputs on the main dashboard cards now correctly update the global `currentConfig` object in the background. This ensures that if a user adjusts a value on the dashboard and later clicks "Save Changes" in the main configuration modal, their dashboard adjustments are preserved and sent to the backend correctly.

## 6. Conclusions & Recommendations
The system is currently functioning as designed with high directional accuracy for both Long and Short positions. The fee handling is consistent across all core modules.

**Immediate Recommendations:**
1. **Safety Cap**: Add a `max_recovery_order_usdt` config setting to protect against the "Order Explosion" scenario.
2. **Audit Logs**: Maintain a separate log of Auto-Cal calculations to help users understand why a specific "Need Add" amount was generated during high-volatility events.
