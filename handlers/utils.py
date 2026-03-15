import hmac
import hashlib
import base64
import json

def safe_float(value, default=0.0):
    try: return float(value)
    except: return default

def safe_int(value, default=0):
    try: return int(float(value))
    except: return default

def generate_okx_signature(api_secret, timestamp, method, request_path, body_str=''):
    message = str(timestamp) + method.upper() + request_path + body_str
    hashed = hmac.new(api_secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256)
    return base64.b64encode(hashed.digest()).decode('utf-8')
