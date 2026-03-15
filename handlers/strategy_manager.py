import time
from handlers.utils import safe_float

class StrategyManager:
    def __init__(self, engine):
        self.engine = engine
        self.config = engine.config
        self.last_entry_time = 0
        self.last_eval_log_time = 0

    def check_entry_conditions(self):
        if not self.engine.is_running: return []

        if not self.engine.indicator_manager.check_candlestick_conditions():
            return []

        # If we are already in position or exiting, skip strategy entry (unless both directions allowed)
        # However, for One-Way mode especially, we should check if we already have the side we want.
        in_pos = self.engine.in_position
        authoritative_exit = getattr(self.engine, 'authoritative_exit_in_progress', False)
        if authoritative_exit: return []

        price = self.engine.latest_trade_price
        if price <= 0: return []

        direction = self.config.get('direction', 'both')
        offset = safe_float(self.config.get('entry_price_offset', 0))

        long_line = safe_float(self.config.get('long_safety_line_price'), 0)
        short_line = safe_float(self.config.get('short_safety_line_price'), 0)

        self.engine.log(f"Strategy Evaluation - Price: {price}, Safety Lines: [Long <= {long_line}, Short >= {short_line}], Direction: {direction}", level="info")

        signals = []
        # Check Long
        if direction in ['long', 'both']:
            if long_line > 0 and price <= long_line:
                # Continuous placement: check if we have remaining budget
                if self.engine.remaining_amount_notional > self.config.get('min_order_amount', 10):
                    self.engine.log(f"Long Signal Triggered: Price {price} <= {long_line}", level="info")
                    signals.append({'side': 'long', 'price': price - offset})
                else:
                    if time.time() - self.last_eval_log_time > 60:
                        self.engine.log("Long entry signal exists but no remaining capacity.", level="debug")
                        self.last_eval_log_time = time.time()

        # Check Short
        if direction in ['short', 'both']:
            if short_line > 0 and price >= short_line:
                # Continuous placement: check if we have remaining budget
                if self.engine.remaining_amount_notional > self.config.get('min_order_amount', 10):
                    self.engine.log(f"Short Signal Triggered: Price {price} >= {short_line}", level="info")
                    signals.append({'side': 'short', 'price': price + offset})
                else:
                    if time.time() - self.last_eval_log_time > 60:
                        self.engine.log("Short entry signal exists but no remaining capacity.", level="debug")
                        self.last_eval_log_time = time.time()

        return signals

    def execute_strategy(self):
        signals = self.check_entry_conditions()
        for sig in signals:
            self.engine.order_manager.initiate_entry_batch(sig['price'], sig['side'], self.config.get('batch_size_per_loop', 1))
            self.last_entry_time = time.time()
