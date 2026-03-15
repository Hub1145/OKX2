import threading
import time
from handlers.utils import safe_float

class PositionManager:
    def __init__(self, engine):
        self.engine = engine
        self.config = engine.config
        self.reset_session_metrics()
        self.reset()

    def reset_session_metrics(self):
        self.net_trade_profit = 0.0
        self.total_trade_profit = 0.0
        self.total_trade_loss = 0.0
        self.total_fees = 0.0

    def reset(self):
        self.pending_fill_logs = {} # ordId -> {pnl, fee, time}
        self.in_position = {'long': False, 'short': False}
        self.position_qty = {'long': 0.0, 'short': 0.0}
        self.position_entry_price = {'long': 0.0, 'short': 0.0}
        self.position_liq = {'long': 0.0, 'short': 0.0}
        self.position_details = {'long': {}, 'short': {}}
        self.position_notional = {'long': 0.0, 'short': 0.0}
        self.position_upl = {'long': 0.0, 'short': 0.0}
        self.loop_qty = {'long': 0.0, 'short': 0.0}
        self.session_baseline_qty = {'long': 0.0, 'short': 0.0}
        self.baseline_initialized = False
        self.cached_active_positions_count = 0
        self.cached_pos_notional = 0.0
        self.cached_unrealized_pnl = 0.0
        self.used_amount_notional = 0.0
        self.current_entry_fees = {'long': 0.0, 'short': 0.0}
        self.realized_loss_this_cycle = {'long': 0.0, 'short': 0.0}

    def process_positions(self, positions_data, is_snapshot=True):
        target_symbol = self.config['symbol'].strip().upper()
        found_sides = set()
        contract_size = safe_float(self.engine.product_info.get('contractSize', 1.0))

        with self.engine.lock:
            initial_run = not self.baseline_initialized and is_snapshot
            if initial_run:
                # We initialize baseline from the first set of positions we receive
                for pos in positions_data:
                    if pos.get('instId', '').strip().upper() == target_symbol:
                        q = abs(safe_float(pos.get('pos')))
                        if q > 0:
                            s_key = self._map_side(pos.get('posSide', 'net'), qty=safe_float(pos.get('pos')))
                            self.session_baseline_qty[s_key] = q
                self.baseline_initialized = True
                self.engine.log(f"Baseline initialized: Long={self.session_baseline_qty['long']}, Short={self.session_baseline_qty['short']} contracts")

            prev_qtys = {k: v for k, v in self.position_qty.items()}
            for pos in positions_data:
                if pos.get('instId', '').strip().upper() == target_symbol:
                    qty_raw = safe_float(pos.get('pos'))
                    if qty_raw == 0 and is_snapshot: continue
                    side_key = self._map_side(pos.get('posSide', 'net'), qty=qty_raw)

                    if qty_raw != 0:
                        found_sides.add(side_key)
                        mkt_px = self.engine.latest_trade_price if self.engine.latest_trade_price else safe_float(pos.get('avgPx'))
                        side_notional = abs(qty_raw) * mkt_px * contract_size

                        self.in_position[side_key] = True
                        self.position_qty[side_key] = qty_raw
                        self.position_entry_price[side_key] = safe_float(pos.get('avgPx'))
                        self.position_notional[side_key] = side_notional
                        self.position_upl[side_key] = safe_float(pos.get('upl', '0'))
                        self.position_liq[side_key] = safe_float(pos.get('liqp', '0'))
                        self.position_details[side_key] = pos

                        # Loop margin tracking: startup positions are treated as manual (don't adopt)
                        self.loop_qty[side_key] = min(self.loop_qty[side_key], abs(qty_raw))

                        if abs(qty_raw - prev_qtys.get(side_key, 0.0)) > 1e-6:
                            if abs(qty_raw) > abs(prev_qtys.get(side_key, 0.0)):
                                self.engine.total_trades_count += 1
                            self.engine.log(f"Position Detected: {side_key.upper()} Qty={qty_raw}", level="debug")

            # Handle closures
            for s in ['long', 'short']:
                if is_snapshot:
                    if s not in found_sides and self.in_position[s]: self._handle_closure(s)
                else:
                    for pos in positions_data:
                        if self._map_side(pos.get('posSide', 'net'), qty=safe_float(pos.get('pos'))) == s and safe_float(pos.get('pos')) == 0:
                            self._handle_closure(s)
                            break

            # Calculate Global Totals from ALL tracked positions (prevents flickering during incremental updates)
            self.cached_active_positions_count = 0
            self.cached_pos_notional = 0.0
            self.cached_unrealized_pnl = 0.0
            self.used_amount_notional = 0.0

            for side in ['long', 'short']:
                if self.in_position[side]:
                    self.cached_active_positions_count += 1
                    self.cached_pos_notional += self.position_notional[side]
                    self.cached_unrealized_pnl += self.position_upl[side]

                    # Recalculate used_amount_notional based on latest loop_qty and price
                    mkt_px = self.engine.latest_trade_price if self.engine.latest_trade_price else self.position_entry_price[side]
                    self.used_amount_notional += self.loop_qty[side] * mkt_px * contract_size

    def _handle_closure(self, s):
        self.engine.log(f"Position Detected Closed: {s.upper()}", level="info")
        self.in_position[s] = False
        self.position_qty[s] = 0.0
        self.position_entry_price[s] = 0.0
        self.position_notional[s] = 0.0
        self.position_upl[s] = 0.0
        self.position_details[s] = {}
        self.loop_qty[s] = 0.0
        self.engine.current_take_profit[s] = 0.0
        self.engine.current_stop_loss[s] = 0.0
        # Reset side-specific cycle metrics
        self.current_entry_fees[s] = 0.0
        self.realized_loss_this_cycle[s] = 0.0

    def update_realtime_metrics(self, current_price):
        if not current_price: return
        temp_upl = 0.0
        temp_notional = 0.0
        contract_size = safe_float(self.engine.product_info.get('contractSize', 1.0))
        for side in ['long', 'short']:
            if self.in_position[side]:
                qty = abs(self.position_qty[side])
                entry = self.position_entry_price[side]
                upl = 0.0
                if side == 'long': upl = (current_price - entry) * qty * contract_size
                else: upl = (entry - current_price) * qty * contract_size

                self.position_upl[side] = upl
                temp_upl += upl

                side_notional = qty * current_price * contract_size
                self.position_notional[side] = side_notional
                temp_notional += side_notional
            else:
                self.position_upl[side] = 0.0
        self.cached_unrealized_pnl = temp_upl
        self.cached_pos_notional = temp_notional

    def _map_side(self, raw_side, qty=0):
        if raw_side == 'short': return 'short'
        if raw_side == 'long': return 'long'
        if raw_side == 'net' or not raw_side:
            if qty > 0: return 'long'
            if qty < 0: return 'short'

            # If qty is 0, we are likely closing.
            # Check if we are currently in a position to determine the side.
            if self.in_position['long'] and not self.in_position['short']: return 'long'
            if self.in_position['short'] and not self.in_position['long']: return 'short'

        side_key = self.config.get('direction', 'long')
        return 'long' if side_key == 'both' else side_key

    def add_fee(self, fee, raw_side, qty=0):
        self.total_fees += abs(fee)
        side = self._map_side(raw_side, qty=qty)
        self.current_entry_fees[side] += abs(fee)

    def update_loop_qty(self, side, delta):
        with self.engine.lock:
            if self.config.get('okx_pos_mode', 'net_mode') == 'net_mode':
                # In One-way mode, trades on one side affect the other side
                other_side = 'short' if side == 'long' else 'long'

                if delta > 0: # Adding to 'side'
                    # First reduce the other side if it exists
                    if self.loop_qty[other_side] > 0:
                        reduction = min(self.loop_qty[other_side], delta)
                        self.loop_qty[other_side] -= reduction
                        delta -= reduction

                    # Then add the rest to 'side'
                    self.loop_qty[side] += delta
                else: # Reducing 'side'
                    abs_delta = abs(delta)
                    # First reduce 'side'
                    reduction = min(self.loop_qty[side], abs_delta)
                    self.loop_qty[side] -= reduction
                    abs_delta -= reduction

                    # Then "add" to the other side (meaning increasing its negative-ness if it was a reduction of other side)
                    # But delta < 0 for update_loop_qty usually means reduction of that side's intended position.
                    # This logic gets complex if we allow mixed context orders.
                    # For now, let's just stick to the simple reduction.
                    if abs_delta > 0:
                        self.loop_qty[other_side] += abs_delta
            else:
                # Hedge mode: sides are independent
                self.loop_qty[side] = max(0.0, self.loop_qty[side] + delta)

            # Re-sync used_amount_notional for UI
            contract_size = safe_float(self.engine.product_info.get('contractSize', 1.0))

            # Recalculate based on current prices
            total_used = 0.0
            for s in ['long', 'short']:
                px = self.engine.latest_trade_price if self.engine.latest_trade_price else self.position_entry_price[s]
                total_used += self.loop_qty[s] * px * contract_size
            self.used_amount_notional = total_used

    def sync_positions(self):
        target_symbol = self.config['symbol'].strip().upper()
        res = self.engine.okx_client.request("GET", "/api/v5/account/positions", params={"instId": target_symbol})
        if res and res.get('code') == '0':
            self.process_positions(res.get('data', []), is_snapshot=True)

    def add_realized_pnl(self, ord_id, pnl, fee, raw_side, qty=0):
        with self.engine.lock:
            net = pnl + fee
            if net > 0: self.total_trade_profit += net
            else:
                self.total_trade_loss += abs(net)
                # Track cycle losses to include in "Need Add" calculation
                side = self._map_side(raw_side, qty=qty)
                self.realized_loss_this_cycle[side] += abs(net)
            self.net_trade_profit = self.total_trade_profit - self.total_trade_loss

            # Aggregate fill logs to avoid spamming
            if ord_id not in self.pending_fill_logs:
                self.pending_fill_logs[ord_id] = {'pnl': 0.0, 'fee': 0.0, 'time': time.time()}

            self.pending_fill_logs[ord_id]['pnl'] += pnl
            self.pending_fill_logs[ord_id]['fee'] += fee
            self.pending_fill_logs[ord_id]['time'] = time.time()

    def flush_fill_logs(self, force=False):
        now = time.time()
        to_log = []
        with self.engine.lock:
            to_remove = []
            for ord_id, data in self.pending_fill_logs.items():
                if force or (now - data['time'] > 1.0):
                    to_log.append((ord_id, data['pnl'], data['fee']))
                    to_remove.append(ord_id)
            for oid in to_remove:
                del self.pending_fill_logs[oid]

        for ord_id, pnl, fee in to_log:
            net = pnl + fee
            self.engine.log(f"Trade Closed (Order {ord_id}) - Realized PnL: {pnl:.4f}, Fee: {fee:.4f}, Net: {net:.4f}", level="info")
