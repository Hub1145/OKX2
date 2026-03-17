import math
import time
import threading
from handlers.utils import safe_float

class AutoCalManager:
    def __init__(self, engine):
        self.engine = engine
        self.config = engine.config
        self.lock = threading.Lock()
        self.auto_add_step_count = {'long': 0, 'short': 0}
        self.last_add_price = {'long': 0.0, 'short': 0.0}
        self.last_order_time = 0
        self._is_adding = {'long': False, 'short': False}

    def check_auto_exit(self, net_pnl, unrealized_pnl):
        notional = self.engine.cached_pos_notional
        if notional <= 0: return False, ""

        fee_pct = self.config.get('trade_fee_percentage', 0.08) / 100.0
        # Use aggregate fees for thresholds
        used_fees = sum(self.engine.position_manager.current_entry_fees.values())
        size_fees = notional * fee_pct

        # Check if any add orders have been placed in this cycle
        has_add_orders = any(self.auto_add_step_count[s] > 0 for s in ['long', 'short'])
        times2 = float(self.config.get('add_pos_times2', 1.1)) if has_add_orders else 1.0

        # 1. Above Zero (Mode 1)
        if self.config.get('use_add_pos_above_zero') and net_pnl >= 0:
            return True, "Above Zero Target Met (Mode 1)"

        if self.config.get('use_add_pos_profit_target'):
            mult = float(self.config.get('add_pos_profit_multiplier', 1.5))
            # Apply Times2 when add orders are active
            target = notional * fee_pct * mult * times2

            if self.engine.monitoring_tick % 10 == 0:
                self.engine.log(f"Auto-Exit Check (Mode 2): Unrealized PnL=${unrealized_pnl:.2f}, Target=${target:.2f} ({mult}x Fees{f' x {times2} Times2' if has_add_orders else ''})", level="info")

            if unrealized_pnl >= target:
                return True, f"Profit Target Met (Mode 2: Unrealized PnL > {target:.2f})"

        # 3. Auto-Manual Threshold
        if self.config.get('use_pnl_auto_manual'):
            threshold = self.config.get('pnl_auto_manual_threshold', 100.0)
            if unrealized_pnl >= threshold:
                return True, f"Manual PnL Threshold {threshold} Met"

        # 4. Auto-Cal Profit (Based on Entry Fees)
        if self.config.get('use_pnl_auto_cal'):
            times = self.config.get('pnl_auto_cal_times', 1.2)
            if unrealized_pnl >= (used_fees * times):
                return True, f"Auto-Cal Profit Met ({times}x Entry Fees)"

        # 5. Auto-Cal Loss (Based on Entry Fees)
        if self.config.get('use_pnl_auto_cal_loss'):
            times = self.config.get('pnl_auto_cal_loss_times', 15.0)
            if unrealized_pnl <= -(used_fees * times):
                return True, f"Auto-Cal Loss Met ({times}x Entry Fees)"

        # 6. Size Auto-Cal Profit (Based on Current Notional Fee, with Times2 if add orders active)
        if self.config.get('use_size_auto_cal'):
            times = self.config.get('size_auto_cal_times', 2.0)
            target = size_fees * times * times2
            if unrealized_pnl >= target:
                return True, f"Size Auto-Cal Profit Met ({times}x Size Fees{f' x {times2} Times2' if has_add_orders else ''})"

        # 7. Size Auto-Cal Loss (Based on Current Notional Fee, with Times2 if add orders active)
        if self.config.get('use_size_auto_cal_loss'):
            times = self.config.get('size_auto_cal_loss_times', 1.5)
            target = size_fees * times * times2
            if unrealized_pnl <= -target:
                return True, f"Size Auto-Cal Loss Met ({times}x Size Fees{f' x {times2} Times2' if has_add_orders else ''})"

        return False, ""

    def check_auto_margin(self):
        # Persistent: Runs even if is_running is False, BUT only after the bot has been started at least once.
        # This prevents rogue trades when switching accounts before clicking "Start".
        if not self.engine.persistent_mode_active: return

        if not self.config.get('use_auto_margin'): return
        for side in ['long', 'short']:
            if self.engine.in_position[side]:
                pos = self.engine.position_manager.position_details.get(side, {})
                liqp = self.engine.position_manager.position_liq[side]
                sl = self.engine.current_stop_loss[side]
                if pos.get('mgnMode') == 'isolated' and liqp > 0 and sl > 0:
                    if (side == 'long' and liqp >= sl) or (side == 'short' and liqp <= sl):
                        amt = abs(sl - liqp) + self.config.get('auto_margin_offset', 30.0)
                        self.engine.okx_client.request("POST", "/api/v5/account/position/margin-balance", body_dict={"instId": self.config['symbol'], "posSide": pos.get('posSide', 'net'), "type": "add", "amt": str(round(amt, 2))})

    def check_auto_add(self):
        # Persistent: Runs even if is_running is False, BUT only after the bot has been started at least once.
        if not self.engine.persistent_mode_active: return

        with self.lock:
            # Only run if gap-based auto-add is configured
            if not self.config.get('add_pos_gap_threshold', 0): return

            # Lockout to prevent rapid-fire adds before position sync
            if time.time() - self.last_order_time < 3: return

            mkt = self.engine.latest_trade_price
            if not mkt: return

            for side in ['long', 'short']:
                if self.engine.in_position[side]:
                    if self._is_adding[side]: continue


                    # Gap logic: Use current average entry price from position
                    entry = self.engine.position_entry_price[side]
                    if entry == 0: continue

                    gap_threshold = float(self.config.get('add_pos_gap_threshold', 5.0))
                    gap_offset = float(self.config.get('add_pos_gap_offset', 0.0))
                    gap = gap_threshold + (self.auto_add_step_count[side] * gap_offset)

                    # Gap = Entry - Market (for Longs going down) or Market - Entry (for Shorts going up)
                    price_diff = (entry - mkt) if side == 'long' else (mkt - entry)

                    gap_trigger = (price_diff >= gap)

                    if self.engine.monitoring_tick % 10 == 0:
                        self.engine.log(f"Auto-Add Check ({side.upper()}): Gap={price_diff:.2f}/{gap:.2f} (Avg Entry: {entry:.2f}, Mark: {mkt:.2f})", level="info")

                    if gap_trigger:
                        max_adds = int(self.config.get('add_pos_max_count', 10))
                        if self.auto_add_step_count[side] >= max_adds:
                            if self.engine.monitoring_tick % 100 == 0:
                                self.engine.log(f"Auto-Add ({side.upper()}): Max steps reached ({self.auto_add_step_count[side]}/{max_adds}). Skipping.", level="info")
                            continue

                        self.engine.log(f"Auto-Add Triggered ({side.upper()}) via Gap Threshold ({price_diff:.2f} >= {gap:.2f}). Executing Add.", level="info")
                        self._is_adding[side] = True
                        threading.Thread(target=self._execute_add_threaded, args=(side, mkt), daemon=True).start()
                else:
                    self.auto_add_step_count[side] = 0
                    self.last_add_price[side] = 0.0

    def _execute_add_threaded(self, side, price):
        try:
            self._execute_add(side, price)
        finally:
            with self.lock:
                self._is_adding[side] = False

    def _execute_add(self, side, price):
        current_notional = self.engine.position_manager.position_notional[side]

        # Calculate add size based on percentage of current position
        pct_base = float(self.config.get('add_pos_size_pct', 5.0))
        pct_offset = float(self.config.get('add_pos_size_pct_offset', 0.0))
        pct = (pct_base + (self.auto_add_step_count[side] * pct_offset)) / 100.0

        final_notional = current_notional * pct

        # Safety Cap 1: Prevent order explosion against absolute sanity ceiling.
        max_notional_cap = self.engine.max_amount_display
        equity = self.engine.total_equity
        sanity_ceiling = float('inf')
        if max_notional_cap > 0:
            sanity_ceiling = min(sanity_ceiling, max_notional_cap * 2.0)
        if equity > 0:
            sanity_ceiling = min(sanity_ceiling, equity * 2.0)

        if final_notional > sanity_ceiling:
            self.engine.log(f"Auto-Add ({side.upper()}): Capping order notional {final_notional:.2f} to sanity ceiling {sanity_ceiling:.2f}", level="warning")
            final_notional = sanity_ceiling

        # Safety Cap 2: Add orders draw from Total Capital 2nd budget.
        cap2nd = self.engine.total_capital_2nd
        if cap2nd > 0 and final_notional > cap2nd:
            self.engine.log(f"Auto-Add ({side.upper()}): Capping to Total Capital 2nd budget {cap2nd:.2f} (requested {final_notional:.2f})", level="info")
            final_notional = cap2nd

        if final_notional <= 0:
            self.engine.log(f"Auto-Add ({side.upper()}): Calculated add amount is 0 or less. Skipping.", level="info")
            return False

        self.engine.log(f"Auto-Add Calc ({side.upper()}): Current Notional={current_notional:.2f}, Size Pct={pct*100:.1f}% -> Final={final_notional:.2f} (Cap2nd={cap2nd:.2f})")

        contract_multiplier = safe_float(self.engine.product_info.get('contractSize', 1.0))
        sz = final_notional / (price * contract_multiplier)

        # Apply quantity precision and step size
        lot_sz = safe_float(self.engine.product_info.get('qtyStepSize', 1.0))
        sz = round(math.floor(sz / lot_sz) * lot_sz, 8)

        if sz < safe_float(self.engine.product_info.get('minOrderQty', 0)):
            self.engine.log(f"Auto-Add quantity {sz} is below minOrderQty (Target Notional {final_notional:.2f}). Skipping.", level="info")
            return False

        # Calculate TP/SL using standard offsets
        tp, sl = self.engine.order_manager._calculate_tpsl_prices(side, price)

        # Apply TP Offset2 (additional TP offset for add orders)
        tp_offset2 = safe_float(self.config.get('add_pos_tp_offset2', 0))
        if tp_offset2 > 0 and tp > 0:
            p_prec = self.engine.product_info.get('pricePrecision', 2)
            if side == 'long':
                tp = round(tp + tp_offset2, p_prec)
            else:
                tp = round(tp - tp_offset2, p_prec)
            self.engine.log(f"Auto-Add TP Offset2 applied: TP={tp:.4f} (offset2={tp_offset2})")

        # Step 2 Exit Offset Override (Relative to New Average Entry)
        step2 = safe_float(self.config.get('add_pos_step2_offset'), 0)
        if step2 > 0:
            p_prec = self.engine.product_info.get('pricePrecision', 2)
            entry = self.engine.position_entry_price[side]
            qty = abs(self.engine.position_qty[side])
            new_total_qty = qty + sz
            if new_total_qty > 0:
                new_avg_entry = ((qty * entry) + (sz * price)) / new_total_qty
                if side == 'long': tp = round(new_avg_entry + step2, p_prec)
                else: tp = round(new_avg_entry - step2, p_prec)
                self.engine.log(f"Auto-Add Step 2: New Avg Entry Est {new_avg_entry:.4f}, TP set at {tp:.4f} (Offset {step2})")

        # Use actual posSide and mgnMode from existing position
        pos_detail = self.engine.position_manager.position_details.get(side, {})
        actual_pos_side = pos_detail.get('posSide', 'net')
        actual_mgn_mode = pos_detail.get('mgnMode', self.config.get('mode', 'cross'))

        # Use configurable order type for add orders
        add_order_type = self.config.get('add_pos_order_type', 'Market')

        # Optimistically update step count and last_order_time before placing to prevent race
        with self.lock:
            self.auto_add_step_count[side] += 1
            self.last_order_time = time.time()
            self.last_add_price[side] = price

        if self.engine.order_manager.place_order(self.config['symbol'], "buy" if side == "long" else "sell", sz,
                                                 order_type=add_order_type, posSide=actual_pos_side, tdMode=actual_mgn_mode,
                                                 take_profit_price=tp, stop_loss_price=sl,
                                                 context='autocal'):
            return True
        else:
            # Revert step count on failure
            with self.lock:
                self.auto_add_step_count[side] -= 1
        return False
