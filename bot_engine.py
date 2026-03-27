import threading
import time
import logging
import json
from datetime import datetime, timezone
from collections import deque

from handlers.okx_client import OKXClient
from handlers.utils import safe_float, safe_int
from handlers.websocket_handler import WebSocketHandler
from handlers.account_manager import AccountManager
from handlers.position_manager import PositionManager
from handlers.order_manager import OrderManager
from handlers.auto_cal_manager import AutoCalManager
from handlers.indicator_manager import IndicatorManager
from handlers.strategy_manager import StrategyManager

class TradingBotEngine:
    def __init__(self, config_file, emit_func):
        self.config_file = config_file
        self.emit = emit_func
        self.config = self._load_config()
        self.is_running = False
        self.persistent_mode_active = True # Controls AutoCal and Exits (Now UNRESTRICTED)
        self.stop_event = threading.Event()
        self.console_logs = deque(maxlen=1000)
        self.product_info = {'contractSize': 1.0, 'lotSz': '1', 'tickSz': '0.01', 'pricePrecision': 2, 'qtyPrecision': 2, 'qtyStepSize': 1.0, 'minOrderQty': 0.01}
        self.latest_trade_price = 0.0
        self.total_trades_count = 0
        self.last_emit_time = 0
        self.monitoring_tick = 0
        self.current_take_profit = {'long': 0.0, 'short': 0.0}
        self.current_stop_loss = {'long': 0.0, 'short': 0.0}
        self._should_update_tpsl = False
        self.account_balance = 0.0
        self.total_equity = 0.0
        self.available_balance = 0.0
        self.authoritative_exit_in_progress = False
        self.exit_lock = threading.Lock()
        self.lock = threading.Lock()
        self.intervals = {'1m': 60, '3m': 180, '5m': 300, '15m': 900, '1h': 3600}

        # Handlers
        self.okx_client = OKXClient(self.log, self.config)
        self.account_manager = AccountManager(self)
        self.position_manager = PositionManager(self)
        self.order_manager = OrderManager(self)
        self.auto_cal_manager = AutoCalManager(self)
        self.indicator_manager = IndicatorManager(self)
        self.strategy_manager = StrategyManager(self)
        self.ws_handler = WebSocketHandler(self.log, self.config, self.okx_client, self._on_ws_message)

        self.mgmt_thread = None

    def _load_config(self):
        with open(self.config_file, 'r') as f: return json.load(f)

    def log(self, message, level='info'):
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = {'timestamp': timestamp, 'message': message, 'level': level}
        self.console_logs.append(log_entry)
        self.emit('console_log', log_entry)
        if level == 'info': logging.info(message)
        elif level == 'error': logging.error(message)
        elif level == 'debug': logging.debug(message)
        elif level == 'warning': logging.warning(message)

    @property
    def in_position(self): return self.position_manager.in_position
    @property
    def position_qty(self): return self.position_manager.position_qty
    @property
    def position_entry_price(self): return self.position_manager.position_entry_price
    @property
    def cached_pos_notional(self): return self.position_manager.cached_pos_notional
    @property
    def cached_unrealized_pnl(self): return self.position_manager.cached_unrealized_pnl
    @property
    def position_upl(self): return self.position_manager.position_upl
    @property
    def open_trades(self): return self.order_manager.open_trades
    @property
    def used_amount_notional(self):
        # Sum of active position notional + pending entry orders notional
        pos_notional = self.position_manager.used_amount_notional
        with self.lock:
            # We copy the list or use a lock during iteration to prevent RuntimeError
            pending_notional = sum(o.get('stake', 0.0) for o in self.order_manager.open_trades if o.get('id') in self.order_manager.pending_entry_ids)
        return pos_notional + pending_notional
    @property
    def remaining_amount_notional(self):
        leverage = safe_float(self.config.get('leverage', 1), 1.0)
        equity = self.total_equity
        # Cap max_allowed by equity to be realistic about margin, but ensure it's at least something
        # if equity is not yet synced.
        config_max = float(self.config.get('max_allowed_used', 0.0))
        max_allowed = min(config_max, equity if equity > 0 else config_max)

        rate_divisor = max(1, self.config.get('rate_divisor', 1))
        capacity = (max_allowed / rate_divisor) * leverage
        return max(0.0, capacity - self.used_amount_notional)
    @property
    def max_allowed_display(self): return float(self.config.get('max_allowed_used', 0.0))
    @property
    def max_amount_display(self):
        # Point 3: Clarity on Max Amount.
        # Max Amount (Notional Cap) = (Max Allowed / Rate) * Leverage
        leverage = safe_float(self.config.get('leverage', 1), 1.0)
        rate = max(1.0, float(self.config.get('rate_divisor', 1.0)))
        return (self.max_allowed_display / rate) * leverage
    @property
    def net_profit(self):
        # Sum of net_pnl for all active positions (UPL - Fees - Cycle Losses)
        total = 0.0
        for side in ['long', 'short']:
            if self.position_manager.in_position[side]:
                upl = self.position_manager.position_upl.get(side, 0.0)
                fees = self.position_manager.current_entry_fees.get(side, 0.0)
                loss = self.position_manager.realized_loss_this_cycle.get(side, 0.0)
                total += (upl - fees - loss)
        return total
    @property
    def daily_reports(self): return self.account_manager.daily_reports
    @property
    def need_add_usdt_profit_target(self): return self.auto_cal_manager.need_add_usdt_profit_target
    @property
    def need_add_usdt_above_zero(self): return self.auto_cal_manager.need_add_usdt_above_zero
    @property
    def raw_need_add_usdt_profit_target(self): return self.auto_cal_manager.raw_need_add_usdt_profit_target
    @property
    def raw_need_add_usdt_above_zero(self): return self.auto_cal_manager.raw_need_add_usdt_above_zero
    @property
    def trade_fees(self): return self.position_manager.total_fees
    @property
    def net_trade_profit(self): return self.position_manager.net_trade_profit
    @property
    def total_trade_profit(self): return self.position_manager.total_trade_profit
    @property
    def total_trade_loss(self): return self.position_manager.total_trade_loss
    @property
    def cumulative_margin_used(self): return self.position_manager.used_amount_notional / safe_float(self.config.get('leverage', 1), 1.0)
    @property
    def total_capital_2nd(self): return max(0.0, self.total_equity - self.cumulative_margin_used)
    @property
    def size_amount(self): return self.cached_pos_notional

    def check_credentials(self):
        try:
            path = "/api/v5/account/balance"
            params = {"ccy": "USDT"}
            res = self.okx_client.request("GET", path, params=params, max_retries=1)
            if res and res.get('code') == '0':
                return True, "Credentials valid."
            elif res and res.get('msg'):
                return False, f"API Error: {res.get('msg')}"
            return False, "Invalid API credentials."
        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def test_api_credentials(self):
        valid, _ = self.check_credentials()
        return valid

    def start(self, passive_monitoring=False):
        if not passive_monitoring:
            self.is_running = True
            # Ensure features are active
            self.persistent_mode_active = True
        self.stop_event.clear()

        # Move slow initialization to a background thread to keep the main thread (Flask) responsive
        threading.Thread(target=self._async_start_init, args=(passive_monitoring,), daemon=True).start()

    def _async_start_init(self, passive_monitoring):
        try:
            self.okx_client.apply_api_credentials()
            self.account_manager.sync_server_time()
            self.account_manager.fetch_product_info(self.config['symbol'])
            self.account_manager.sync_account_data() # Ensure equity is known for capacity calcs

            # Point 4: Secure Data Sync before enabling any trading logic
            self.position_manager.sync_positions()

            self.indicator_manager.fetch_historical_data(self.config['symbol'], self.config.get('candlestick_timeframe', '1m'))
            self.ws_handler.start()

            if not self.mgmt_thread or not self.mgmt_thread.is_alive():
                self.mgmt_thread = threading.Thread(target=self._mgmt_loop, daemon=True)
                self.mgmt_thread.start()

            self.log(f"Bot initialized and started (Mode: {'Passive' if passive_monitoring else 'Active'})")
        except Exception as e:
            self.log(f"Error during bot initialization: {e}", level="error")

    def stop(self):
        self.is_running = False
        self.log("Strategy Loop stopped. Auto-Cal recovery remains ACTIVE.")

    def stop_bot(self):
        self.stop_event.set()
        self.is_running = False
        self.persistent_mode_active = False
        self.log("Shutting down bot completely...")
        if self.ws_handler:
            self.ws_handler.stop()

    def _mgmt_loop(self):
        while not self.stop_event.is_set():
            try:
                loop_interval = max(1, int(self.config.get('loop_time_seconds', 10)))
                self.monitoring_tick += 1

                # 1. Background Tasks (Silent syncs)
                if self.monitoring_tick % 15 == 0:
                    if not self.product_info.get('is_loaded'):
                        self.account_manager.fetch_product_info(self.config['symbol'])
                    self.account_manager.sync_account_data()
                    self.position_manager.sync_positions()
                    self.indicator_manager.fetch_historical_data(self.config['symbol'], self.config.get('candlestick_timeframe', '1m'))
                    self.order_manager.sync_open_orders(self.config['symbol'])
                    self.order_manager.check_unfilled_timeouts()

                # 2. Auto-Add / Margin (Polling remains for these as they are gap-based)
                if not self.authoritative_exit_in_progress and self.monitoring_tick % 5 == 0:
                    self.auto_cal_manager.check_auto_add()
                    self.auto_cal_manager.check_auto_margin()

                # 3. Strategy Analysis Loop (Structured logs, respects loop_interval)
                if self.is_running and self.monitoring_tick % loop_interval == 0:
                    self.log("-" * 46)
                    self.log("Entry check logs")
                    if not self.authoritative_exit_in_progress:
                        self.strategy_manager.execute_strategy()
                    self.log(f"Waiting for next loop ({loop_interval}s)")
                    self.log("-" * 46)

                # 3. Post-Add TP/SL Sync
                if self._should_update_tpsl:
                    self.log("Refreshing position TP/SL after Auto-Cal Add fill.")
                    self.order_manager.batch_modify_tpsl(self.config['symbol'])
                    self._should_update_tpsl = False

                # 4. WebSocket Health Check (Fallback)
                if self.monitoring_tick % 30 == 0:
                    last_ws = self.ws_handler.last_message_time
                    if time.time() - last_ws > 60:
                        self.log("WebSocket Health Check Failed: No data for 60s. Forcing restart.", level="warning")
                        self.ws_handler.restart()

                self.account_manager.check_daily_report()
                self.position_manager.flush_fill_logs()
                if time.time() - self.last_emit_time >= 1.5:
                    self._emit_socket_updates()
                    self.last_emit_time = time.time()
            except Exception as e: self.log(f"Error: {e}", level="error")
            time.sleep(1)

    def _on_ws_message(self, msg, is_private):
        if 'data' in msg:
            channel = msg.get('arg', {}).get('channel', '')
            data = msg.get('data', [])
            action = msg.get('action', '')
            if channel == 'tickers' and data:
                price = safe_float(data[0].get('last'))
                if price > 0:
                    self.latest_trade_price = price
                    self.position_manager.update_realtime_metrics(price)
                    if not self.authoritative_exit_in_progress:
                        # Only execute persistent features if activated
                        if self.persistent_mode_active:
                            self.auto_cal_manager.check_auto_add()

                        # REAL-TIME EXIT CHECK (ALIGNED WITH OKX UPL)
                        if self.persistent_mode_active:
                            net_pnl = self.net_profit
                            triggered, reason, target_prices, is_tp = self.auto_cal_manager.check_auto_exit(net_pnl, self.cached_unrealized_pnl)
                            if triggered:
                                threading.Thread(target=self.execute_auto_exit, args=(reason, target_prices, is_tp), daemon=True).start()
                    self._emit_socket_updates(throttle=True)
            elif channel == 'positions' and data:
                self.position_manager.process_positions(data, is_snapshot=(action == 'snapshot'))
                self._emit_socket_updates()
            elif channel == 'account' and data:
                for d in data[0].get('details', []):
                    if d.get('ccy') == 'USDT':
                        self.account_balance = safe_float(d.get('bal'))
                        self.total_equity = safe_float(data[0].get('totalEq'))
                        self.available_balance = safe_float(d.get('availBal'))
                self._emit_socket_updates()
            elif channel == 'orders' and data:
                for o in data:
                    ord_id = o.get('ordId')
                    raw_side = o.get('posSide', 'net')
                    side = o.get('side') # buy/sell
                    sz = safe_float(o.get('sz'))
                    acc_fill = safe_float(o.get('accFillSz', 0))
                    prev_fill = self.order_manager.order_fills.get(ord_id, 0.0)
                    fill_delta = acc_fill - prev_fill
                    state = o.get('state')
                    fee = safe_float(o.get('fillFee', 0))
                    pnl = safe_float(o.get('fillPnl', 0))

                    # Disambiguate position side for One-way mode
                    target_pos_side = raw_side
                    if raw_side == 'net' or not raw_side:
                        if pnl != 0: # Closing trade (only closing trades have fillPnl)
                            target_pos_side = 'short' if side == 'buy' else 'long'
                        else: # Opening trade
                            target_pos_side = 'long' if side == 'buy' else 'short'

                    # Ensure it's mapped to internal 'long' or 'short'
                    mapped_side = self.position_manager._map_side(target_pos_side)

                    # Track loop quantity based on order context
                    context = self.order_manager.order_contexts.get(ord_id)

                    # Trigger TP/SL readjustment if an autocal add order is filled
                    if context == 'autocal' and acc_fill > prev_fill and state in ['filled', 'partially_filled']:
                        self.log(f"Auto-Cal Add Order Filled. Triggering TP/SL readjustment for {mapped_side} position.", level="info")
                        # We defer the actual placement slightly to allow position to sync or use the last known avgPx
                        self._should_update_tpsl = True

                    if context == 'loop' and fill_delta > 0:
                        # Update tracked fill size
                        self.order_manager.order_fills[ord_id] = acc_fill
                        # In One-way mode, buy always adds to long (or reduces short)
                        is_adding = (side == 'buy' and mapped_side == 'long') or (side == 'sell' and mapped_side == 'short')
                        delta = fill_delta if is_adding else -fill_delta
                        self.position_manager.update_loop_qty(mapped_side, delta)
                        self.log(f"Loop Qty Updated: {mapped_side} {delta:+.4f} (Context: {context})", level="debug")

                    if fee != 0: self.position_manager.add_fee(fee, mapped_side)
                    if pnl != 0: self.position_manager.add_realized_pnl(ord_id, pnl, fee, mapped_side)
                self.order_manager.sync_open_orders(self.config['symbol'])

    def _emit_socket_updates(self, throttle=False):
        if throttle and time.time() - self.last_emit_time < 0.2: return
        self.last_emit_time = time.time()

        fee_pct = self.config.get('trade_fee_percentage', 0.08) / 100.0

        payload = {
            'total_trades': self.total_trades_count, 'total_capital': self.total_equity,
            'total_capital_2nd': self.total_capital_2nd,
            'total_balance': self.account_balance, 'available_balance': self.available_balance,
            'used_amount': self.used_amount_notional, 'remaining_amount': self.remaining_amount_notional,
            'max_allowed_used_display': self.max_allowed_display, 'max_amount_display': self.max_amount_display,
            'size_amount': self.size_amount,
            'net_profit': self.net_profit, 'in_position': self.in_position,
            'position_qty': self.position_qty, 'position_entry_price': self.position_entry_price,
            'position_upl': self.position_upl,
            'position_net_pnl': {
                'long': self.position_upl.get('long', 0.0) - self.position_manager.current_entry_fees.get('long', 0.0) - self.position_manager.realized_loss_this_cycle.get('long', 0.0),
                'short': self.position_upl.get('short', 0.0) - self.position_manager.current_entry_fees.get('short', 0.0) - self.position_manager.realized_loss_this_cycle.get('short', 0.0)
            },
            'position_liq': self.position_manager.position_liq,
            'daily_reports': self.daily_reports,
            'running': self.is_running,
            'trade_fees': self.trade_fees, 'net_trade_profit': self.net_trade_profit,
            'used_fees': sum(self.position_manager.current_entry_fees.values()),
            'size_fees': self.size_amount * fee_pct,
            'total_trade_profit': self.total_trade_profit, 'total_trade_loss': self.total_trade_loss,
            'current_take_profit': self.current_take_profit, 'current_stop_loss': self.current_stop_loss
        }
        self.emit('account_update', payload)
        self.emit('bot_status', {'running': self.is_running})
        self.emit('trades_update', {'trades': self.open_trades})

    def execute_auto_exit(self, reason="Manual", target_prices=None, is_tp=False):
        with self.exit_lock:
            if self.authoritative_exit_in_progress: return
            self.authoritative_exit_in_progress = True
        try:
            if "Target" in reason or "Above Zero" in reason:
                self.log(f"AUTO-CAL EXIT: {reason}", level="info")
            else:
                self.log(f"EMERGENCY EXIT: {reason}", level="warning")

            self.order_manager.batch_cancel_orders(self.config['symbol'], [o['ordId'] for o in self.open_trades])
            self.order_manager.cancel_algo_orders(self.config['symbol'])

            for s, in_p in self.in_position.items():
                if in_p:
                    qty = abs(self.position_qty[s])
                    if qty > 0:
                        # Use the actual posSide and mgnMode from OKX for this position to ensure we can close manual trades
                        pos_detail = self.position_manager.position_details.get(s, {})
                        actual_pos_side = pos_detail.get('posSide', 'net')
                        actual_mgn_mode = pos_detail.get('mgnMode', self.config.get('mode', 'cross'))

                        # Determine Order Type based on config and reason
                        order_type = "Market"
                        limit_price = None

                        # Respect TP Mode for Auto-Cal Exits if it's a TP event
                        if is_tp:
                            tp_mode = self.config.get('tp_mode', 'limit').lower()
                            use_limit = (tp_mode == 'limit') or self.config.get('tp_close_limit', False)

                            if use_limit:
                                order_type = "Limit"
                                if target_prices and target_prices.get(s):
                                    limit_price = target_prices[s]
                                else:
                                    limit_price = self.latest_trade_price

                        self.log(f"Closing {s} position: {qty} contracts ({order_type} @ {limit_price if limit_price else 'MKT'}, posSide: {actual_pos_side}, tdMode: {actual_mgn_mode})", level="info")
                        self.order_manager.place_order(
                            self.config['symbol'],
                            "sell" if s == "long" else "buy",
                            qty,
                            price=limit_price,
                            order_type=order_type,
                            posSide=actual_pos_side,
                            tdMode=actual_mgn_mode,
                            reduce_only=True
                        )

            time.sleep(3)
            self.account_manager.sync_account_data()
            self.order_manager.sync_open_orders(self.config['symbol'])
        finally:
            time.sleep(2)
            with self.exit_lock: self.authoritative_exit_in_progress = False

    def emergency_sl(self, reason="Manual"):
        self.execute_auto_exit(reason)

    def apply_live_config_update(self, new_config):
        # Point 2: Reliable account switch detection
        # Create a snapshot of critical credentials before updating
        keys_to_watch = ['okx_api_key', 'okx_api_secret', 'okx_passphrase', 'okx_demo_api_key', 'okx_demo_api_secret', 'okx_demo_api_passphrase', 'use_developer_api', 'use_testnet', 'symbol', 'okx_pos_mode']
        old_keys = {k: self.config.get(k) for k in keys_to_watch}

        self.config = new_config
        for h in [self.okx_client, self.account_manager, self.position_manager, self.order_manager, self.auto_cal_manager, self.indicator_manager, self.strategy_manager, self.ws_handler]:
            h.config = new_config

        self.log("Live configuration updated. Propagating changes to all handlers.", level="debug")
        self.okx_client.apply_api_credentials()

        # Immediate sync for sensitive changes
        if any(old_keys.get(k) != new_config.get(k) for k in keys_to_watch):
            # Point 2: API Key / Mode Switch Security
            # If sensitive credentials or mode changes, we MUST stop the bot and reset all handlers.
            # This prevents trades from Account A leaking into Account B's state.
            self.log("SENSITIVE CONFIG CHANGE: Resetting all handlers and DEACTIVATING all trading features.", level="warning")
            self.is_running = False
            self.persistent_mode_active = False
            self.position_manager.reset(); self.order_manager.reset()
            self.position_manager.reset_session_metrics()

            # Always reset recovery counts on account/mode switch for safety
            self.auto_cal_manager.auto_add_step_count = {'long': 0, 'short': 0}
            self.auto_cal_manager.last_add_price = {'long': 0.0, 'short': 0.0}

            # Re-fetch info if symbol changed
            if old_keys.get('symbol') != new_config.get('symbol'):
                self.account_manager.fetch_product_info(new_config['symbol'])

            self.ws_handler.restart()
        return {'success': True}

    def batch_modify_tpsl(self): self.order_manager.batch_modify_tpsl(self.config['symbol'])
    def batch_cancel_orders(self): self.order_manager.batch_cancel_orders(self.config['symbol'], [o['ordId'] for o in self.open_trades])
