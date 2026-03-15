import pandas as pd
import numpy as np
import ta
import time
from datetime import datetime, timedelta, timezone

class IndicatorManager:
    def __init__(self, engine):
        self.engine = engine
        self.config = engine.config
        self.historical_data = []

    def fetch_historical_data(self, symbol, timeframe):
        try:
            # Logic from _fetch_historical_data_okx
            path = "/api/v5/market/history-candles"
            interval_sec = self.engine.intervals.get(timeframe, 60)
            end_dt = datetime.now(timezone.utc)
            start_dt = end_dt - timedelta(seconds=interval_sec * 300)

            params = {
                "instId": symbol,
                "bar": timeframe,
                "after": str(int(end_dt.timestamp() * 1000)),
                "before": str(int(start_dt.timestamp() * 1000)),
                "limit": "300"
            }

            res = self.engine.okx_client.request("GET", path, params=params)
            if res and res.get('code') == '0':
                data = res.get('data', [])
                df = pd.DataFrame(data, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm'])
                df['ts'] = pd.to_datetime(pd.to_numeric(df['ts'], errors='coerce'), unit='ms', utc=True)
                for col in ['o', 'h', 'l', 'c', 'v']: df[col] = df[col].astype(float)
                df = df.sort_values('ts')
                self.historical_data = df.to_dict('records')
                return True
        except Exception as e:
            self.engine.log(f"Indicator fail: {e}", level="error")
        return False

    def check_candlestick_conditions(self):
        if not self.config.get('use_candlestick_conditions'): return True
        if not self.historical_data:
            self.engine.log("No historical data available for candlestick conditions", level="warning")
            return False

        df = pd.DataFrame(self.historical_data)
        last_row = df.iloc[-1]

        # Open-Close Chg
        if self.config.get('use_chg_open_close'):
            chg = abs(last_row['c'] - last_row['o']) / last_row['o'] * 100
            min_val = self.config.get('min_chg_open_close', 0)
            max_val = self.config.get('max_chg_open_close', 100)
            if not (min_val <= chg <= max_val):
                self.engine.log(f"Candlestick Fail: Open-Close Chg {chg:.4f}% not in range [{min_val}, {max_val}]", level="info")
                return False
            else:
                self.engine.log(f"Candlestick Pass: Open-Close Chg {chg:.4f}% within range", level="info")

        # High-Low Chg
        if self.config.get('use_chg_high_low'):
            chg = abs(last_row['h'] - last_row['l']) / last_row['l'] * 100
            min_val = self.config.get('min_chg_high_low', 0)
            max_val = self.config.get('max_chg_high_low', 100)
            if not (min_val <= chg <= max_val):
                self.engine.log(f"Candlestick Fail: High-Low Chg {chg:.4f}% not in range [{min_val}, {max_val}]", level="info")
                return False
            else:
                self.engine.log(f"Candlestick Pass: High-Low Chg {chg:.4f}% within range", level="info")

        # High-Close Chg
        if self.config.get('use_chg_high_close'):
            chg = abs(last_row['h'] - last_row['c']) / last_row['c'] * 100
            min_val = self.config.get('min_chg_high_close', 0)
            max_val = self.config.get('max_chg_high_close', 100)
            if not (min_val <= chg <= max_val):
                self.engine.log(f"Candlestick Fail: High-Close Chg {chg:.4f}% not in range [{min_val}, {max_val}]", level="info")
                return False
            else:
                self.engine.log(f"Candlestick Pass: High-Close Chg {chg:.4f}% within range", level="info")

        return True
