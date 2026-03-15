import time
import logging
import json
import os
from datetime import datetime, timezone
from handlers.utils import safe_float

class AccountManager:
    def __init__(self, engine):
        self.engine = engine
        self.config = engine.config
        self.analytics_path = "analytics.json"
        self.daily_reports = []
        self._load_analytics()

    def _load_analytics(self):
        try:
            if os.path.exists(self.analytics_path):
                with open(self.analytics_path, 'r') as f:
                    data = json.load(f)
                    self.daily_reports = data.get('daily_reports', [])
        except Exception as e:
            self.engine.log(f"Error loading analytics: {e}", level="error")

    def save_analytics(self):
        try:
            data = {'daily_reports': self.daily_reports}
            with open(self.analytics_path, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            self.engine.log(f"Error saving analytics: {e}", level="error")

    def sync_server_time(self):
        return self.engine.okx_client.sync_server_time()

    def _get_precision(self, sz_str):
        if not sz_str: return 0
        sz_str = str(sz_str)
        if '.' in sz_str:
            return len(sz_str.split('.')[-1].rstrip('0'))
        return 0

    def fetch_product_info(self, symbol):
        info = self.engine.okx_client.fetch_product_info(symbol)
        if info:
            self.engine.product_info = {
                'priceTickSize': float(info.get('tickSz')),
                'qtyPrecision': self._get_precision(info.get('lotSz')),
                'pricePrecision': self._get_precision(info.get('tickSz')),
                'qtyStepSize': float(info.get('lotSz')),
                'minOrderQty': float(info.get('minSz')),
                'contractSize': float(info.get('ctVal', '1')),
                'is_loaded': True
            }
            self.engine.log(f"Product Info Updated for {symbol}: ContractSize={self.engine.product_info['contractSize']}, LotSz={self.engine.product_info['qtyStepSize']}")
            return True
        else:
            self.engine.log(f"Failed to fetch product info for {symbol}. Check API connectivity.", level="error")
        return False

    def sync_account_data(self):
        path = "/api/v5/account/balance"
        params = {"ccy": "USDT"}
        res = self.engine.okx_client.request("GET", path, params=params)
        if res and res.get('code') == '0':
            data = res.get('data', [{}])[0]
            self.engine.total_equity = float(data.get('totalEq', 0))
            for d in data.get('details', []):
                if d.get('ccy') == 'USDT':
                    self.engine.account_balance = float(d.get('bal', 0))
                    self.engine.available_balance = float(d.get('availBal', 0))
                    break

    def check_daily_report(self):
        now = datetime.now(timezone.utc)
        today_str = now.strftime('%Y-%m-%d')

        if not self.daily_reports or self.daily_reports[-1].get('date') != today_str:
            compound = 1.0
            if self.daily_reports:
                initial_capital = self.daily_reports[0].get('total_capital', self.engine.total_equity)
                if initial_capital > 0:
                    compound = self.engine.total_equity / initial_capital

            report = {
                "date": today_str,
                "total_capital": self.engine.total_equity,
                "net_trade_profit": self.engine.net_trade_profit,
                "compound_interest": round(compound, 4)
            }
            self.daily_reports.append(report)
            self.save_analytics()
