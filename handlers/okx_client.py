import threading
import time
import requests
import json
import logging
from datetime import datetime, timedelta, timezone
from handlers.utils import safe_float, safe_int, generate_okx_signature

class RateLimiter:
    def __init__(self):
        self.locks = {}
        self.buckets = {}
        self.limits = {
            'account': {'rate': 3, 'capacity': 6},
            'trade': {'rate': 3, 'capacity': 6},
            'market': {'rate': 10, 'capacity': 20},
            'public': {'rate': 10, 'capacity': 20},
            'default': {'rate': 5, 'capacity': 10}
        }
        for category in self.limits:
            self.locks[category] = threading.Lock()
            self.buckets[category] = {'tokens': self.limits[category]['capacity'], 'last_update': time.time()}

    def _get_category(self, path):
        if '/account/' in path: return 'account'
        if '/trade/' in path: return 'trade'
        if '/market/' in path: return 'market'
        if '/public/' in path: return 'public'
        return 'default'

    def acquire(self, path, tokens=1):
        category = self._get_category(path)
        lock = self.locks[category]
        while True:
            sleep_time = 0
            with lock:
                now = time.time()
                bucket = self.buckets[category]
                limit = self.limits[category]
                # Update tokens based on time elapsed
                elapsed = now - bucket['last_update']
                bucket['tokens'] = min(limit['capacity'], bucket['tokens'] + (elapsed * limit['rate']))
                bucket['last_update'] = now

                if bucket['tokens'] >= tokens:
                    bucket['tokens'] -= tokens
                    return

                # Calculate required sleep time to get enough tokens
                sleep_time = (tokens - bucket['tokens']) / limit['rate']

            if sleep_time > 0:
                time.sleep(min(sleep_time, 1.0))

class OKXClient:
    def __init__(self, logger_func, config):
        self.log = logger_func
        self.config = config
        self.rate_limiter = RateLimiter()
        self.credentials_invalid = False
        self.server_time_offset = 0
        self.okx_rest_api_base_url = "https://www.okx.com"
        self.apply_api_credentials()

    def apply_api_credentials(self):
        use_testnet = self.config.get('use_testnet', False)
        use_developer_api = self.config.get('use_developer_api', False)
        if use_developer_api:
            if use_testnet:
                self.okx_api_key = self.config.get('dev_demo_api_key', '')
                self.okx_api_secret = self.config.get('dev_demo_api_secret', '')
                self.okx_passphrase = self.config.get('dev_demo_api_passphrase', '')
            else:
                self.okx_api_key = self.config.get('dev_api_key', '')
                self.okx_api_secret = self.config.get('dev_api_secret', '')
                self.okx_passphrase = self.config.get('dev_passphrase', '')
        else:
            if use_testnet:
                self.okx_api_key = self.config.get('okx_demo_api_key', '')
                self.okx_api_secret = self.config.get('okx_demo_api_secret', '')
                self.okx_passphrase = self.config.get('okx_demo_api_passphrase', '')
            else:
                self.okx_api_key = self.config.get('okx_api_key', '')
                self.okx_api_secret = self.config.get('okx_api_secret', '')
                self.okx_passphrase = self.config.get('okx_passphrase', '')
        self.okx_simulated_trading_header = {'x-simulated-trading': '1'} if use_testnet else {}
        self.credentials_invalid = False

    def request(self, method, path, params=None, body_dict=None, max_retries=3):
        if self.credentials_invalid: return None
        local_dt = datetime.now(timezone.utc)
        adjusted_dt = local_dt + timedelta(milliseconds=self.server_time_offset)
        timestamp = adjusted_dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        body_str = json.dumps(body_dict, separators=(',', ':'), sort_keys=True) if body_dict else ''
        request_path_for_signing = path
        final_url = f"{self.okx_rest_api_base_url}{path}"
        if params and method.upper() == 'GET':
            query_string = '?' + '&'.join([f'{k}={v}' for k, v in sorted(params.items())])
            request_path_for_signing += query_string
            final_url += query_string
        signature = generate_okx_signature(self.okx_api_secret, timestamp, method, request_path_for_signing, body_str)
        headers = {"OK-ACCESS-KEY": self.okx_api_key, "OK-ACCESS-SIGN": signature, "OK-ACCESS-TIMESTAMP": timestamp, "OK-ACCESS-PASSPHRASE": self.okx_passphrase, "Content-Type": "application/json"}
        headers.update(self.okx_simulated_trading_header)

        for attempt in range(max_retries):
            if self.credentials_invalid: return None
            try:
                self.rate_limiter.acquire(path)
                req_func = getattr(requests, method.lower(), None)
                if not req_func: return None
                kwargs = {'headers': headers, 'timeout': 15}
                if body_dict and method.upper() in ['POST', 'PUT', 'DELETE']: kwargs['data'] = body_str

                # self.log(f"API Request: {method} {path} (Attempt {attempt+1})")
                response = req_func(final_url, **kwargs)
                # self.log(f"API Response: {response.status_code} for {method} {path}")

                if response.status_code != 200:
                    try:
                        error_json = response.json()
                        okx_err = error_json.get('code')
                        if okx_err in ['50110', '50111', '50113'] or response.status_code == 401: self.credentials_invalid = True
                        return error_json
                    except:
                        if attempt < max_retries - 1: time.sleep(2**attempt); continue
                        return None
                return response.json()
            except Exception:
                if attempt < max_retries - 1: time.sleep(2**attempt); continue
        return None

    def sync_server_time(self):
        try:
            resp = requests.get(f"{self.okx_rest_api_base_url}/api/v5/public/time", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('code') == '0':
                    self.server_time_offset = int(data['data'][0]['ts']) - int(time.time() * 1000)
                    return True
        except: pass
        return False

    def fetch_product_info(self, symbol):
        try:
            path = "/api/v5/public/instruments"
            params = {"instType": "SWAP", "instId": symbol}
            response = self.request("GET", path, params=params)
            if response and response.get('code') == '0':
                return response.get('data', [{}])[0]
        except: pass
        return None
