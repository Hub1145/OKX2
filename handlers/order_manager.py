import threading
import time
import math
from handlers.utils import safe_float

class OrderManager:
    def __init__(self, engine):
        self.engine = engine
        self.config = engine.config
        self.reset()

    def reset(self):
        self.pending_entry_ids = set()
        self.order_contexts = {} # ordId -> context
        self.order_fills = {}    # ordId -> total filled qty
        self.position_exit_orders = {'long': {}, 'short': {}}
        self.open_trades = []
        self.batch_counter = 0

    def _get_tp_px(self, trigger_price, side):
        tp_px = "-1"
        p_prec = self.engine.product_info.get('pricePrecision', 2)
        if self.config.get('tp_mode', 'limit').lower() == 'limit' or self.config.get('tp_close_limit'):
            if self.config.get('tp_close_same_as_trigger'):
                tp_px = f"{trigger_price:.{p_prec}f}"
            else:
                conf_px = safe_float(self.config.get('tp_close_price', 0))
                # Safety: Check if absolute price is reasonable (within 50% of trigger)
                if conf_px > 0 and 0.5 * trigger_price < conf_px < 1.5 * trigger_price:
                    tp_px = f"{conf_px:.{p_prec}f}"
                else:
                    tp_px = f"{trigger_price:.{p_prec}f}"
        return tp_px

    def _get_sl_px(self, trigger_price, side):
        sl_px = "-1"
        p_prec = self.engine.product_info.get('pricePrecision', 2)
        if self.config.get('sl_close_limit'):
            if self.config.get('sl_close_same_as_trigger'):
                sl_px = f"{trigger_price:.{p_prec}f}"
            else:
                conf_px = safe_float(self.config.get('sl_close_price', 0))
                # Safety: Check if absolute price is reasonable (within 50% of trigger)
                if conf_px > 0 and 0.5 * trigger_price < conf_px < 1.5 * trigger_price:
                    sl_px = f"{conf_px:.{p_prec}f}"
                else:
                    sl_px = f"{trigger_price:.{p_prec}f}"
        return sl_px

    def _calculate_tpsl_prices(self, side, reference_price):
        """Calculates TP and SL prices. side is the trade direction ('buy'/'sell') or position side ('long'/'short')."""
        # Use PositionManager._map_side for robust side normalization
        pos_side = self.engine.position_manager._map_side(side)

        tp_offset = safe_float(self.config.get('tp_price_offset'), 0)
        sl_offset = safe_float(self.config.get('sl_price_offset'), 0)
        
        # Recovery/Auto-Add Offset Adjustments
        step_count = self.engine.auto_cal_manager.auto_add_step_count[pos_side]

        tp_price = 0.0
        sl_price = 0.0
        p_prec = self.engine.product_info.get('pricePrecision', 2)
        contract_size = safe_float(self.engine.product_info.get('contractSize', 1.0))

        # 1. Specialized Auto-Cal Targets (Apply only if we have an ACTIVE position of the SAME side)
        qty_abs = abs(self.engine.position_qty[pos_side])
        pos_entry = self.engine.position_entry_price[pos_side]

        if qty_abs > 0 and pos_entry > 0 and (step_count > 0 or self.config.get('use_add_pos_profit_target') or self.config.get('use_add_pos_above_zero')):
            # 1.1 Above Zero Target
            if self.config.get('use_add_pos_above_zero'):
                target_upl = self.engine.position_manager.current_entry_fees.get(pos_side, 0.0) + self.engine.position_manager.realized_loss_this_cycle.get(pos_side, 0.0)
                calc_tp = self.engine.position_manager.calculate_target_price(pos_side, target_upl)
                if calc_tp:
                    # Validate against reference_price to ensure it's a valid trigger for the NEW order
                    is_valid = (pos_side == 'long' and calc_tp > reference_price) or (pos_side == 'short' and calc_tp < reference_price)
                    if is_valid:
                        tp_price = calc_tp
                        self.engine.log(f"TP-CALC ({pos_side.upper()}): Above Zero Target trigger: {tp_price}", level="debug")

            # 1.2 Profit Target (Overrides Above Zero if more favorable)
            if self.config.get('use_add_pos_profit_target'):
                fee_pct = self.config.get('trade_fee_percentage', 0.08) / 100.0
                mult = float(self.config.get('add_pos_profit_multiplier', 1.5))
                times2 = float(self.config.get('add_pos_times2', 1.1)) if step_count > 0 else 1.0

                notional = qty_abs * pos_entry * contract_size
                target_upl = notional * fee_pct * mult * times2

                calc_tp = self.engine.position_manager.calculate_target_price(pos_side, target_upl)
                if calc_tp:
                    is_favorable = (pos_side == 'long' and calc_tp > reference_price) or (pos_side == 'short' and calc_tp < reference_price)
                    if is_favorable:
                        if tp_price == 0 or (pos_side == 'long' and calc_tp > tp_price) or (pos_side == 'short' and calc_tp < tp_price):
                            tp_price = calc_tp
                            self.engine.log(f"TP-CALC ({pos_side.upper()}): Profit Target trigger: {tp_price}", level="debug")

        # 2. Fallback/Default Offset Logic (relative to reference_price)
        if tp_price == 0:
            active_tp_offset = tp_offset
            if step_count > 0:
                # Priority 1: Step 2 Offset
                step2_offset = safe_float(self.config.get('add_pos_step2_offset'), 0)
                if step2_offset > 0:
                    active_tp_offset = step2_offset
                else:
                    # Priority 2: TP Offset 2
                    offset2 = safe_float(self.config.get('add_pos_tp_offset2'), 0)
                    if offset2 > 0:
                        active_tp_offset += offset2

            if pos_side == 'long':
                if active_tp_offset > 0: tp_price = round(reference_price + active_tp_offset, p_prec)
            else:
                if active_tp_offset > 0: tp_price = round(reference_price - active_tp_offset, p_prec)

        # 3. Stop Loss Calculation (Always relative to reference_price)
        if pos_side == 'long':
            if sl_offset > 0: sl_price = round(reference_price - sl_offset, p_prec)
        else:
            if sl_offset > 0: sl_price = round(reference_price + sl_offset, p_prec)

        return tp_price, sl_price

    def _round_to_step(self, value, step):
        if not step or step <= 0: return value
        # Use decimal-safe rounding
        precision = 0
        if '.' in str(step):
            precision = len(str(step).split('.')[-1].rstrip('0'))

        rounded = round(round(value / step) * step, precision)
        # Final check to ensure we don't return 0.0000000000001
        return float(f"{rounded:.{precision}f}")

    def place_order(self, symbol, side, qty, price=None, order_type="Market",
                    reduce_only=False, stop_loss_price=None, take_profit_price=None, posSide=None, tdMode=None, verbose=True, context=None):
        try:
            path = "/api/v5/trade/order"

            # Apply precision and step size rounding
            q_step = safe_float(self.engine.product_info.get('qtyStepSize', 1.0))
            p_step = safe_float(self.engine.product_info.get('priceTickSize', 0.01))
            q_prec = self.engine.product_info.get('qtyPrecision', 0)
            p_prec = self.engine.product_info.get('pricePrecision', 2)

            qty = self._round_to_step(qty, q_step)
            if qty <= 0:
                self.engine.log(f"Invalid order quantity after rounding: {qty}", level="warning")
                return None

            body = {
                "instId": symbol,
                "tdMode": tdMode if tdMode else self.config.get('mode', 'cross'),
                "side": side.lower(),
                "ordType": order_type.lower(),
                "sz": f"{qty:.{q_prec}f}" if q_prec > 0 else str(int(qty))
            }

            # Map the actual position side we are acting on to ensure correct TP/SL direction
            # In One-way mode, Buy -> Long, Sell -> Short
            mapped_pos_side = self.engine.position_manager._map_side(posSide if posSide else side, qty=(qty if side.lower() == 'buy' else -qty))

            # Determine correct posSide based on account mode
            mode = self.config.get('okx_pos_mode', 'net_mode')
            if mode == 'long_short_mode':
                if posSide in ['long', 'short']:
                    body["posSide"] = posSide
                else:
                    body["posSide"] = mapped_pos_side
            else:
                body["posSide"] = "net"

            if order_type.lower() == "limit" and price is not None:
                price = self._round_to_step(price, p_step)
                body["px"] = f"{price:.{p_prec}f}"

            algo_list = []
            algo = {"posSide": body.get("posSide", "net")}
            has_algo = False

            # Validation logic to prevent OKX 51052/51053 errors (TP/SL price validation)
            # Short position (Sell entry): TP must be < price, SL must be > price
            # Long position (Buy entry): TP must be > price, SL must be < price
            ref_px = price if (order_type.lower() == "limit" and price) else self.engine.latest_trade_price

            if take_profit_price and safe_float(take_profit_price) > 0:
                # Validation logic to prevent OKX 51052/51053 errors (TP/SL price validation)
                # Long position (buy entry/adding): TP must be > ref_px
                # Short position (sell entry/adding): TP must be < ref_px
                is_valid_tp = False
                if mapped_pos_side == 'long':
                    is_valid_tp = take_profit_price > (ref_px * 1.0001) # Add tiny buffer
                else: # Short
                    is_valid_tp = take_profit_price < (ref_px * 0.9999)

                if is_valid_tp or reduce_only:
                    tp_px = self._get_tp_px(take_profit_price, mapped_pos_side)
                    algo.update({"tpTriggerPx": f"{take_profit_price:.{p_prec}f}", "tpOrdPx": tp_px, "tpTriggerPxType": "last"})
                    has_algo = True
                else:
                    self.engine.log(f"TP Validation FAILED: {take_profit_price} not favorable for {mapped_pos_side} at {ref_px}.", level="debug")

            if stop_loss_price and safe_float(stop_loss_price) > 0:
                is_valid_sl = False
                if mapped_pos_side == 'long':
                    is_valid_sl = stop_loss_price < (ref_px * 0.9999)
                else: # Short
                    is_valid_sl = stop_loss_price > (ref_px * 1.0001)

                if is_valid_sl or reduce_only:
                    sl_px = self._get_sl_px(stop_loss_price, mapped_pos_side)
                    algo.update({"slTriggerPx": f"{stop_loss_price:.{p_prec}f}", "slOrdPx": sl_px, "slTriggerPxType": "last"})
                    has_algo = True
                else:
                    self.engine.log(f"SL Validation FAILED: {stop_loss_price} not favorable for {mapped_pos_side} at {ref_px}.", level="debug")

            if has_algo:
                algo_list.append(algo)
                body["attachAlgoOrds"] = algo_list

            if reduce_only: body["reduceOnly"] = True

            if verbose:
                self.engine.log(f"Placing {order_type} {side} order for {qty} {symbol} (tdMode: {body['tdMode']}, posSide: {body['posSide']})")
                if "attachAlgoOrds" in body:
                    self.engine.log(f"Attached Algos: {body['attachAlgoOrds']}", level="debug")
            res = self.engine.okx_client.request("POST", path, body_dict=body)
            if res and res.get('code') == '0':
                data = res.get('data', [{}])[0]
                oid = data.get('ordId')
                if oid and context:
                    self.order_contexts[oid] = context

                if not reduce_only and oid:
                    with self.engine.lock:
                        # Optimistic update for UI responsiveness and capital management
                        if context == 'loop':
                            self.pending_entry_ids.add(oid)

                        new_order = {
                            'id': oid,
                            'type': side.upper(),
                            'posSide': posSide if posSide else body.get("posSide"),
                            'entry_spot_price': price if price else self.engine.latest_trade_price,
                            'stake': qty * (price if price else self.engine.latest_trade_price) * self.engine.product_info.get('contractSize', 1.0),
                            'tp_price': take_profit_price,
                            'sl_price': stop_loss_price,
                            'time_left': self.config.get('cancel_unfilled_seconds', 30),
                            'ordId': oid,
                            'cTime': (time.time() * 1000) + self.engine.okx_client.server_time_offset
                        }
                        # Check if already exists (shouldn't, but safety first)
                        if not any(o['id'] == oid for o in self.open_trades):
                            self.open_trades.append(new_order)
                return data
            else:
                msg = res.get('msg') if res else 'No Response'
                code = res.get('code') if res else 'N/A'

                # Extract detailed error from data if available
                detail_msg = ""
                if res and 'data' in res and isinstance(res['data'], list) and len(res['data']) > 0:
                    d = res['data'][0]
                    if 'sMsg' in d:
                        detail_msg = f" | Detail: {d.get('sMsg')} (sCode: {d.get('sCode')})"

                # Log more details for non-zero codes to help debugging
                self.engine.log(f"Order failed: {msg}{detail_msg} (Code: {code}). Request: sz={body.get('sz')}, px={body.get('px', 'MKT')}, side={body.get('side')}, ordType={body.get('ordType')}, tdMode={body.get('tdMode')}, posSide={body.get('posSide')}, algo={bool(body.get('attachAlgoOrds'))}", level="error")
            return None
        except Exception as e:
            self.engine.log(f"Order fail: {e}", level="error")
            return None

    def initiate_entry_batch(self, initial_limit_price, side, batch_size):
        batch_offset = self.config.get('batch_offset', 0)
        self.batch_counter += 1

        placed_count = 0
        total_qty = 0

        # Determine extra offset for this loop to "place differently"
        # We rotate through 3 different sub-offsets to stagger orders across loops
        loop_stagger = (self.batch_counter % 3) * (batch_offset / 3.0) if batch_offset > 0 else 0

        for i in range(batch_size):
            price = initial_limit_price
            # Stagger prices: each batch order is offset, and each loop is staggered
            price_offset = (batch_offset * i) + loop_stagger
            price = (price - price_offset) if side == 'long' else (price + price_offset)

            if price <= 0: continue

            remaining = self.engine.remaining_amount_notional

            target = self.config.get('target_order_amount', 100)
            if remaining < self.config.get('min_order_amount', 10):
                if i == 0: self.engine.log(f"Insufficient capacity to place new {side} orders (Remaining: {remaining:.2f})", level="debug")
                break

            trade_amt = min(target, remaining)
            qty = trade_amt / (price * self.engine.product_info.get('contractSize', 1.0))

            if qty < safe_float(self.engine.product_info.get('minOrderQty', 0)): continue

            tp, sl = self._calculate_tpsl_prices(side, price)
            # Use verbose=False to suppress individual logs, we'll log the batch instead
            if self.place_order(self.config['symbol'], "buy" if side == 'long' else "sell", qty, price,
                                order_type="Limit", posSide=side, take_profit_price=tp, stop_loss_price=sl,
                                verbose=False, context='loop'):
                placed_count += 1
                total_qty += qty

        if placed_count > 0:
            self.engine.log(f"Placed batch #{self.batch_counter} of {placed_count} {side} orders (Total Qty: {total_qty:.4f})")

    def cancel_order(self, symbol, order_id, reason=None):
        return self.engine.okx_client.request("POST", "/api/v5/trade/cancel-order", body_dict={"instId": symbol, "ordId": order_id})

    def batch_cancel_orders(self, symbol, order_ids):
        if not order_ids: return True
        body = [{"instId": symbol, "ordId": oid} for oid in order_ids]
        return self.engine.okx_client.request("POST", "/api/v5/trade/cancel-batch-orders", body_dict=body)

    def fetch_algo_orders(self, symbol):
        # instType is required. instId is optional but recommended.
        params = {"instType": "SWAP", "instId": symbol}
        res = self.engine.okx_client.request("GET", "/api/v5/trade/orders-algo-pending", params=params)

        # If 400 with code 51000, it might be an issue with instId/instType combination
        if res and res.get('code') == '51000':
             # Try without instId, just instType
             res = self.engine.okx_client.request("GET", "/api/v5/trade/orders-algo-pending", params={"instType": "SWAP"})

        return res.get('data', []) if res and res.get('code') == '0' else []

    def place_position_tpsl(self, side, entry_price):
        # This method is used to refresh/place TP/SL for an existing position.
        if not entry_price: return

        tp_price, sl_price = self._calculate_tpsl_prices(side, entry_price)
        qty = abs(self.engine.position_qty[side])

        if qty > 0 and (tp_price > 0 or sl_price > 0):
            self.engine.log(f"Placing/Refreshing TP/SL for {side} position: TP={tp_price}, SL={sl_price} (Qty: {qty})")

            # Cancel existing for this side first to avoid conflicts
            self.cancel_algo_orders(self.config['symbol'], side=side)

            # Determine correct posSide based on account mode
            pos_mode = self.config.get('okx_pos_mode', 'net_mode')
            actual_pos_side = side if pos_mode == 'long_short_mode' else 'net'

            body = {
                "instId": self.config['symbol'],
                "tdMode": self.config.get('mode', 'cross'),
                "side": "sell" if side == "long" else "buy",
                "posSide": actual_pos_side,
                "sz": str(qty),
                "reduceOnly": "true" # Ensure it only reduces the position
            }

            if tp_price > 0 and sl_price > 0:
                # Use OCO to link TP and SL
                body["ordType"] = "oco"
                tp_px = self._get_tp_px(tp_price, side)
                sl_px = self._get_sl_px(sl_price, side)

                body.update({
                    "tpTriggerPx": f"{tp_price:.{p_prec}f}", "tpOrdPx": tp_px, "tpTriggerPxType": "last",
                    "slTriggerPx": f"{sl_price:.{p_prec}f}", "slOrdPx": sl_px, "slTriggerPxType": "last"
                })
            elif tp_price > 0:
                body["ordType"] = "conditional"
                tp_px = self._get_tp_px(tp_price, side)
                body.update({"tpTriggerPx": f"{tp_price:.{p_prec}f}", "tpOrdPx": tp_px, "tpTriggerPxType": "last"})
            elif sl_price > 0:
                body["ordType"] = "conditional"
                sl_px = self._get_sl_px(sl_price, side)
                body.update({"slTriggerPx": f"{sl_price:.{p_prec}f}", "slOrdPx": sl_px, "slTriggerPxType": "last"})

            res = self.engine.okx_client.request("POST", "/api/v5/trade/order-algo", body_dict=body)
            if res and res.get('code') != '0':
                self.engine.log(f"Failed to place position TP/SL: {res.get('msg')} (Code: {res.get('code')})", level="error")

    def cancel_algo_orders(self, symbol, side=None):
        algos = self.fetch_algo_orders(symbol)
        if side:
            # Map posSide to what OKX returns
            algos = [a for a in algos if a.get('posSide') == side]

        if algos:
            body = [{"instId": symbol, "algoId": a['algoId']} for a in algos]
            return self.engine.okx_client.request("POST", "/api/v5/trade/cancel-algos", body_dict=body)
        return True

    def batch_modify_tpsl(self, symbol):
        self.engine.log("Executing Batch Modify TP/SL for all positions")
        for side, in_pos in self.engine.in_position.items():
            if in_pos:
                entry = self.engine.position_entry_price[side]
                self.place_position_tpsl(side, entry)

    def sync_open_orders(self, symbol):
        res = self.engine.okx_client.request("GET", "/api/v5/trade/orders-pending", params={"instType": "SWAP", "instId": symbol})
        if res and res.get('code') == '0':
            raw_orders = res.get('data', [])
            formatted = []
            now_ms = (time.time() * 1000) + self.engine.okx_client.server_time_offset
            limit = self.config.get('cancel_unfilled_seconds', 30)

            current_ids = set()
            for o in raw_orders:
                oid = o.get('ordId')
                current_ids.add(oid)
                c_time = safe_float(o.get('cTime'))
                time_left = None
                if c_time > 0:
                    elapsed = (now_ms - c_time) / 1000
                    time_left = max(0, int(limit - elapsed))

                # Normalize posSide for One-way mode consistency
                raw_side = o.get('posSide', 'net')
                mapped_side = raw_side
                if raw_side == 'net' or not raw_side:
                    # In net mode, buy=long, sell=short for entry tracking
                    mapped_side = 'long' if o.get('side') == 'buy' else 'short'

                # Map OKX fields to dashboard fields
                formatted.append({
                    'id': oid,
                    'type': o.get('side', '').upper(),
                    'posSide': mapped_side,
                    'entry_spot_price': safe_float(o.get('px')),
                    'stake': abs(safe_float(o.get('sz'))) * safe_float(o.get('px')) * safe_float(self.engine.product_info.get('contractSize', 1.0)),
                    'tp_price': safe_float(o.get('tpTriggerPx')),
                    'sl_price': safe_float(o.get('slTriggerPx')),
                    'time_left': time_left,
                    'ordId': oid,
                    'cTime': c_time
                })

            with self.engine.lock:
                # Merge logic: keep optimistic orders that are very new but not yet in the sync result
                now_ms = (time.time() * 1000) + self.engine.okx_client.server_time_offset
                merged = formatted
                sync_ids = {o['id'] for o in formatted}

                for opt_o in self.open_trades:
                    # If it's an entry order we placed but not yet seen in sync
                    if opt_o['id'] in self.pending_entry_ids and opt_o['id'] not in sync_ids:
                        # Keep it if it's less than 10 seconds old
                        if (now_ms - opt_o['cTime']) < 10000:
                            merged.append(opt_o)
                            current_ids.add(opt_o['id'])

                self.open_trades = merged
                self.pending_entry_ids &= current_ids
        return self.open_trades

    def check_unfilled_timeouts(self):
        limit = self.config.get('cancel_unfilled_seconds', 0)
        # Use server-time adjusted "now" for accurate timeout checks
        now_ms = (time.time() * 1000) + self.engine.okx_client.server_time_offset
        mkt = self.engine.latest_trade_price

        cancel_tp_below = self.config.get('cancel_on_tp_price_below_market')
        cancel_tp_above = self.config.get('cancel_on_tp_price_above_market')
        cancel_ent_below = self.config.get('cancel_on_entry_price_below_market')
        cancel_ent_above = self.config.get('cancel_on_entry_price_above_market')

        to_cancel = []
        reasons = []

        with self.engine.lock:
            # Create a copy for safe iteration
            trades_to_check = list(self.open_trades)

        for o in trades_to_check:
            # We generally only auto-cancel ENTRY orders based on these conditions
            if o['ordId'] not in self.pending_entry_ids: continue

            # 1. Time-based cancel (Strictly respect limit)
            c_time = o.get('cTime')
            # Use current time in ms to compare with c_time
            if limit > 0 and c_time:
                elapsed_seconds = (now_ms - c_time) / 1000
                if elapsed_seconds >= limit:
                    to_cancel.append(o['ordId'])
                    reasons.append(f"Timeout ({int(elapsed_seconds)}s >= {limit}s)")
                    continue

            # 2. Condition-based cancel (Only if enabled in config)
            if mkt <= 0: continue

            ent = o.get('entry_spot_price', 0)
            tp = o.get('tp_price', 0)
            is_long = (o.get('type') == 'BUY')

            # 1. Entry conditions (Only if enabled in config)
            if is_long:
                if cancel_ent_above and ent > 0 and mkt > ent:
                    to_cancel.append(o['ordId']); reasons.append(f"Long Entry {ent} already passed by Market {mkt}")
            else: # short
                if cancel_ent_below and ent > 0 and mkt < ent:
                    to_cancel.append(o['ordId']); reasons.append(f"Short Entry {ent} already passed by Market {mkt}")

            # 2. TP conditions (Only if we have a valid TP price)
            if tp > 0:
                if is_long:
                    # Target is ABOVE entry. Cancel if price hit target.
                    if cancel_tp_above and mkt >= tp:
                        to_cancel.append(o['ordId']); reasons.append(f"Long TP {tp} reached by Market {mkt}")
                else: # short
                    # Target is BELOW entry. Cancel if price hit target.
                    if cancel_tp_below and mkt <= tp:
                        to_cancel.append(o['ordId']); reasons.append(f"Short TP {tp} reached by Market {mkt}")

        if to_cancel:
            for i, oid in enumerate(to_cancel):
                self.engine.log(f"Auto-canceling order {oid}: {reasons[i]}", level="info")
            self.batch_cancel_orders(self.config['symbol'], to_cancel)
            with self.engine.lock:
                self.pending_entry_ids -= set(to_cancel)
