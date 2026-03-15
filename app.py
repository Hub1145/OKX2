from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for, flash
from flask_socketio import SocketIO, emit
import json
import logging
import os
import functools
import threading
import time
from logging.handlers import RotatingFileHandler
from bot_engine import TradingBotEngine

# Configure root logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(console_handler)

# File handler for INFO logs (required for Download Logs)
info_handler = RotatingFileHandler('info.log', maxBytes=10*1024*1024, backupCount=5)
info_handler.setLevel(logging.INFO)
info_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(info_handler)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SESSION_SECRET', 'dev-secret-key-change-in-production')

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', ping_timeout=60, ping_interval=25)

config_file = 'config.json'
login_file = 'login.json'
bot_engine = None

def load_login_creds():
    try:
        if not os.path.exists(login_file):
            with open(login_file, 'w') as f:
                json.dump({"username": "admin", "password": "password"}, f, indent=2)
        with open(login_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Error loading login creds: {e}")
        return {"username": "admin", "password": "password"}

def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def load_config():
    with open(config_file, 'r') as f:
        return json.load(f)

def save_config(config):
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)

def emit_to_client(event, data):
    socketio.emit(event, data)

@app.route('/favicon.ico')
def favicon():
    return app.send_static_file('favicon.ico')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        creds = load_login_creds()
        if username == creds.get('username') and password == creds.get('password'):
            session['logged_in'] = True
            flash('Successfully logged in!', 'success')
            next_url = request.args.get('next')
            return redirect(next_url or url_for('index'))
        else:
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('dashboard.html')

@app.route('/api/config', methods=['GET'])
@login_required
def get_config():
    config = load_config()
    return jsonify(config)

@app.route('/api/config', methods=['POST'])
@login_required
def update_config():
    global bot_engine

    try:
        new_config = request.json
        current_config = load_config()

        # Whitelist of all valid parameters
        allowed_params = [
            'okx_api_key', 'okx_api_secret', 'okx_passphrase', 'okx_demo_api_key', 'okx_demo_api_secret', 'okx_demo_api_passphrase',
            'dev_api_key', 'dev_api_secret', 'dev_passphrase', 'dev_demo_api_key', 'dev_demo_api_secret', 'dev_demo_api_passphrase',
            'use_developer_api', 'use_testnet', 'symbol',
            'short_safety_line_price', 'long_safety_line_price', 'leverage', 'max_allowed_used',
            'entry_price_offset', 'batch_offset', 'tp_price_offset', 'sl_price_offset',
            'loop_time_seconds', 'rate_divisor', 'batch_size_per_loop', 'min_order_amount',
            'target_order_amount', 'cancel_unfilled_seconds', 'cancel_on_tp_price_below_market',
            'cancel_on_entry_price_below_market', 'cancel_on_tp_price_above_market',
            'cancel_on_entry_price_above_market', 'direction', 'mode', 'tp_amount', 'sl_amount',
            'trigger_price', 'tp_mode', 'tp_type', 'use_chg_open_close', 'min_chg_open_close',
            'max_chg_open_close', 'use_chg_high_low', 'min_chg_high_low', 'max_chg_high_low',
            'use_chg_high_close', 'min_chg_high_close', 'max_chg_high_close', 'candlestick_timeframe',
            'use_candlestick_conditions', 'log_level', 'use_pnl_auto_cancel', 'pnl_auto_cancel_threshold', 'okx_pos_mode', 'trade_fee_percentage',
            'use_pnl_auto_manual', 'pnl_auto_manual_threshold', 'use_pnl_auto_cal', 'pnl_auto_cal_times',
            'use_pnl_auto_cal_loss', 'pnl_auto_cal_loss_times',
            'use_auto_margin', 'auto_margin_offset',
            'use_size_auto_cal', 'size_auto_cal_times', 'use_size_auto_cal_loss', 'size_auto_cal_loss_times',
            'use_add_pos_auto_cal', 'add_pos_recovery_percent', 'add_pos_profit_multiplier',
            'add_pos_gap_threshold', 'add_pos_size_pct', 'add_pos_max_count', 'add_pos_step2_offset',
            'add_pos_gap_offset', 'add_pos_size_pct_offset',
            'add_pos_order_type', 'add_pos_times2', 'add_pos_tp_offset2',
            'use_add_pos_above_zero', 'use_add_pos_profit_target',
            'tp_close_limit', 'tp_close_same_as_trigger', 'tp_close_price',
            'sl_close_limit', 'sl_close_same_as_trigger', 'sl_close_price'
        ]

        # Update current_config with only allowed and present keys from new_config
        updates_made = False
        for key, value in new_config.items():
            if key in allowed_params:
                # Type conversion safety could be added here if needed, but JSON usually handles it well enough for basic types
                if current_config.get(key) != value:
                    current_config[key] = value
                    updates_made = True

        if bot_engine and bot_engine.is_running:
             # Relaxed restrictions: Let the engine handle sensitive swaps dynamically
             # We only block things that absolutely cannot be changed (none currently identified as engine handles them)
             pass
        
        if updates_made:
            save_config(current_config)

            warning_msg = None
            if bot_engine:
                # Bot engine is modular and always runs a management loop once started.
                # apply_live_config_update handles sensitive changes like API keys and symbol.
                result = bot_engine.apply_live_config_update(current_config)
                if result.get('warnings'):
                    warning_msg = " | ".join(result['warnings'])
                bot_engine.log("Configuration updated live from dashboard.", level="debug")

            def background_init():
                global bot_engine
                # Ensure bot engine exists
                if not bot_engine:
                    bot_engine = TradingBotEngine(config_file, emit_to_client)

                # Ensure it's started (at least in passive monitoring mode)
                if not bot_engine.mgmt_thread or not bot_engine.mgmt_thread.is_alive():
                    bot_engine.start(passive_monitoring=True)
                
                # Check if the currently selected credentials are valid
                valid, msg = bot_engine.check_credentials()
                if not valid:
                    emit_to_client('error', {'message': f'API Credentials Error: {msg}'})
            
            threading.Thread(target=background_init, daemon=True).start()
            
            final_msg = 'Configuration updated successfully'
            if warning_msg:
                final_msg += f" (Note: {warning_msg})"
            
            return jsonify({'success': True, 'message': final_msg})
        else:
            return jsonify({'success': True, 'message': 'No changes detected'})

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/shutdown', methods=['POST'])
@login_required
def shutdown():
    global bot_engine
    if bot_engine:
        bot_engine.stop_bot()
    
    # Save config before shutdown
    config = load_config()
    save_config(config)
    
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        return jsonify({'success': False, 'message': 'Not running with the Werkzeug Server'})
    
    func()
    return jsonify({'success': True, 'message': 'Server shutting down...'})

@app.route('/api/download_logs')
@login_required
def download_logs():
    try:
        log_file = 'info.log'
        if not os.path.exists(log_file):
             return jsonify({'error': 'Log file not found'}), 404
        
        # Flush handlers to ensure latest logs are written
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.FileHandler): # Only flush file handlers
                handler.flush()
            
        return send_file(
            log_file,
            mimetype='text/plain',
            as_attachment=True,
            download_name='bot_log.log'
        )
    except Exception as e:
        logging.error(f'Error downloading logs: {str(e)}', exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/api/test_api_key', methods=['POST'])
@login_required
def test_api_key_route():
    try:
        data = request.json
        api_key = data.get('api_key')
        api_secret = data.get('api_secret')
        passphrase = data.get('passphrase')
        use_testnet = data.get('use_testnet')

        if not all([api_key, api_secret, passphrase]):
            return jsonify({'success': False, 'message': 'All API credentials (Key, Secret, Passphrase) are required.'}), 400

        # Temporarily create a bot_engine instance to test credentials
        # This bypasses the global bot_engine state
        temp_bot_engine = TradingBotEngine(config_file, emit_to_client)
        temp_bot_engine.config['okx_api_key'] = api_key
        temp_bot_engine.config['okx_api_secret'] = api_secret
        temp_bot_engine.config['okx_passphrase'] = passphrase
        temp_bot_engine.config['okx_demo_api_key'] = api_key # Also set for demo if testnet is used
        temp_bot_engine.config['okx_demo_api_secret'] = api_secret
        temp_bot_engine.config['okx_demo_api_passphrase'] = passphrase
        temp_bot_engine.config['use_testnet'] = use_testnet
        
        # Re-initialize global API credentials for the temp bot engine based on the provided data
        if use_testnet:
            temp_bot_engine.config['okx_api_key'] = temp_bot_engine.config['okx_demo_api_key']
            temp_bot_engine.config['okx_api_secret'] = temp_bot_engine.config['okx_demo_api_secret']
            temp_bot_engine.config['okx_passphrase'] = temp_bot_engine.config['okx_demo_api_passphrase']
            temp_bot_engine.okx_simulated_trading_header = {'x-simulated-trading': '1'}
        else:
            temp_bot_engine.okx_simulated_trading_header = {}

        if temp_bot_engine.test_api_credentials():
            return jsonify({'success': True, 'message': 'API credentials are valid.'})
        else:
            return jsonify({'success': False, 'message': 'Invalid API credentials or connection error.'}), 401

    except Exception as e:
        logging.error(f'Error testing API key: {str(e)}', exc_info=True)
        return jsonify({'success': False, 'message': f'An unexpected error occurred: {str(e)}'}), 500


@app.route('/api/status', methods=['GET'])
@login_required
def get_status():
    global bot_engine
    if not bot_engine:
        try:
            bot_engine = TradingBotEngine(config_file, emit_to_client)
            bot_engine.start(passive_monitoring=True)
        except Exception as e:
            logging.error(f"Error initializing bot engine for status: {e}")
            return jsonify({'running': False, 'error': str(e)}), 500

    # Background sync is already handling data updates
    # if not bot_engine.is_running:
    #     try:
    #         bot_engine.fetch_account_data_sync()
    #     except Exception as e:
    #         logging.error(f"Error fetching sync account data: {e}")

    # Centralized metric calculation logic (matches bot_engine._emit_socket_updates)
    total_active_trades_count = bot_engine.total_trades_count + len(bot_engine.open_trades)
    fee_pct = bot_engine.config.get('trade_fee_percentage', 0.08) / 100.0

    status = {
        'running': bot_engine.is_running,
        'open_trades': bot_engine.open_trades,
        'total_trades': total_active_trades_count,
        'total_capital': bot_engine.total_equity,
        'total_capital_2nd': max(0.0, bot_engine.total_equity - bot_engine.cumulative_margin_used),
        'total_balance': bot_engine.account_balance,
        'available_balance': bot_engine.available_balance,
        'used_amount': bot_engine.used_amount_notional,
        'remaining_amount': bot_engine.remaining_amount_notional,
        'max_allowed_used_display': bot_engine.max_allowed_display,
        'max_amount_display': bot_engine.max_amount_display,
        'trade_fees': bot_engine.trade_fees,
        'used_fees': sum(bot_engine.position_manager.current_entry_fees.values()),
        'size_fees': bot_engine.size_amount * fee_pct,
        'net_profit': bot_engine.net_profit,
        'in_position': bot_engine.in_position,
        'position_entry_price': bot_engine.position_entry_price,
        'position_qty': bot_engine.position_qty,
        'position_upl': bot_engine.position_upl,
        'position_net_pnl': {
                'long': bot_engine.position_upl.get('long', 0.0) - bot_engine.position_manager.current_entry_fees.get('long', 0.0) - bot_engine.position_manager.realized_loss_this_cycle.get('long', 0.0),
                'short': bot_engine.position_upl.get('short', 0.0) - bot_engine.position_manager.current_entry_fees.get('short', 0.0) - bot_engine.position_manager.realized_loss_this_cycle.get('short', 0.0)
        },
        'position_liq': bot_engine.position_manager.position_liq,
        'current_take_profit': bot_engine.current_take_profit,
        'current_stop_loss': bot_engine.current_stop_loss,
        'positions': {
            'long': {
                'in': bot_engine.in_position.get('long', False),
                'qty': bot_engine.position_qty.get('long', 0.0),
                'upl': bot_engine.position_upl.get('long', 0.0),
                'net_pnl': bot_engine.position_upl.get('long', 0.0) - bot_engine.position_manager.current_entry_fees.get('long', 0.0) - bot_engine.position_manager.realized_loss_this_cycle.get('long', 0.0),
                'price': bot_engine.position_entry_price.get('long', 0.0),
                'tp': bot_engine.current_take_profit.get('long', 0.0),
                'sl': bot_engine.current_stop_loss.get('long', 0.0),
                'liq': bot_engine.position_manager.position_liq.get('long', 0.0)
            },
            'short': {
                'in': bot_engine.in_position.get('short', False),
                'qty': bot_engine.position_qty.get('short', 0.0),
                'upl': bot_engine.position_upl.get('short', 0.0),
                'net_pnl': bot_engine.position_upl.get('short', 0.0) - bot_engine.position_manager.current_entry_fees.get('short', 0.0) - bot_engine.position_manager.realized_loss_this_cycle.get('short', 0.0),
                'price': bot_engine.position_entry_price.get('short', 0.0),
                'tp': bot_engine.current_take_profit.get('short', 0.0),
                'sl': bot_engine.current_stop_loss.get('short', 0.0),
                'liq': bot_engine.position_manager.position_liq.get('short', 0.0)
            }
        },
        'primary_in_position': any(bot_engine.in_position.values()),
        'size_amount': bot_engine.size_amount,
        'need_add_usdt': getattr(bot_engine, 'need_add_usdt_profit_target', 0.0),
        'need_add_above_zero': getattr(bot_engine, 'need_add_usdt_above_zero', 0.0),
        'raw_need_add_usdt': getattr(bot_engine, 'raw_need_add_usdt_profit_target', 0.0),
        'raw_need_add_above_zero': getattr(bot_engine, 'raw_need_add_usdt_above_zero', 0.0),
        # Realized profit tracking
        'net_trade_profit': getattr(bot_engine, 'net_trade_profit', 0.0),
        'total_trade_profit': getattr(bot_engine, 'total_trade_profit', 0.0),
        'total_trade_loss': getattr(bot_engine, 'total_trade_loss', 0.0)
    }

    response = jsonify(status)
    response.headers.add('Access-Control-Allow-Origin', '*')
    return response
 
@socketio.on('connect')
def handle_connect(auth=None):
    sid = request.sid
    global bot_engine
    logging.info(f'Client connected: {sid}')
    emit('connection_status', {'connected': True}, room=sid)
 
    if not bot_engine:
        try:
            bot_engine = TradingBotEngine(config_file, emit_to_client)
            bot_engine.start(passive_monitoring=True)
        except Exception as e:
            logging.error(f"Error auto-initializing bot engine on connect: {e}")

    if bot_engine:
        emit('bot_status', {'running': bot_engine.is_running}, room=sid)
        if bot_engine:
            # Use cached data for immediate response
            payload = {
                'total_capital': bot_engine.total_equity,
                'total_capital_2nd': max(0.0, bot_engine.total_equity - bot_engine.cumulative_margin_used),
                'max_allowed_used_display': bot_engine.max_allowed_display,
                'max_amount_display': bot_engine.max_amount_display,
                'used_amount': bot_engine.used_amount_notional,
                'size_amount': getattr(bot_engine, 'cached_pos_notional', 0.0),
                'trade_fees': bot_engine.trade_fees,
                'remaining_amount': bot_engine.remaining_amount_notional,
                'total_balance': bot_engine.account_balance,
                'available_balance': bot_engine.available_balance,
                'net_profit': bot_engine.net_profit,
                'total_trades': len(bot_engine.open_trades) + bot_engine.total_trades_count,
                'net_trade_profit': getattr(bot_engine, 'net_trade_profit', 0.0),
                'total_trade_profit': getattr(bot_engine, 'total_trade_profit', 0.0),
                'total_trade_loss': getattr(bot_engine, 'total_trade_loss', 0.0),
                'need_add_usdt': getattr(bot_engine, 'need_add_usdt_profit_target', 0.0),
                'need_add_above_zero': getattr(bot_engine, 'need_add_usdt_above_zero', 0.0)
            }
            emit('account_update', payload, room=sid)
        
        emit('trades_update', {'trades': bot_engine.open_trades}, room=sid)
        # Emit current position data
        emit('position_update', {
            'in_position': bot_engine.in_position,
            'position_entry_price': bot_engine.position_entry_price,
            'position_qty': bot_engine.position_qty,
            'position_upl': bot_engine.position_upl,
            'position_net_pnl': {
                'long': bot_engine.position_upl.get('long', 0.0) - bot_engine.position_manager.current_entry_fees.get('long', 0.0) - bot_engine.position_manager.realized_loss_this_cycle.get('long', 0.0),
                'short': bot_engine.position_upl.get('short', 0.0) - bot_engine.position_manager.current_entry_fees.get('short', 0.0) - bot_engine.position_manager.realized_loss_this_cycle.get('short', 0.0)
            },
            'position_liq': bot_engine.position_manager.position_liq,
            'current_take_profit': bot_engine.current_take_profit,
            'current_stop_loss': bot_engine.current_stop_loss
        }, room=sid)
 
        # Batch logs to avoid flooding and race conditions on client side
        logs = list(bot_engine.console_logs)
        if logs:
            emit('console_log_batch', {'logs': logs}, room=sid)

@socketio.on('disconnect')
def handle_disconnect():
    logging.info('Client disconnected')

@socketio.on('start_bot')
def handle_start_bot(data=None):
    global bot_engine

    try:
        if bot_engine and bot_engine.is_running:
            emit('error', {'message': 'Bot is already running'})
            return

        if not bot_engine:
             bot_engine = TradingBotEngine(config_file, emit_to_client)

        # 1. Check Credentials before starting
        valid, msg = bot_engine.check_credentials()
        if not valid:
            emit('error', {'message': f'API Credentials Error: {msg}'})
            return

        try:
            bot_engine.start()
            if bot_engine.is_running:
                socketio.emit('bot_status', {'running': True}) # Broadcast status to all
                emit('success', {'message': 'Bot started successfully'})
            else:
                # If bot_engine.start() returned False internally (e.g. position mode error),
                # it already emitted its own error log and 'bot_status': False.
                # However, we'll re-sync just in case.
                socketio.emit('bot_status', {'running': False})
        except Exception as e:
            logging.error(f'Error during bot_engine instantiation or start: {str(e)}', exc_info=True)
            emit('error', {'message': f'Failed to start bot: {str(e)}'})
    except Exception as e: # Catch errors from load_config()
        logging.error(f'Error loading configuration in handle_start_bot: {str(e)}', exc_info=True)
        emit('error', {'message': f'Failed to start bot due to config error: {str(e)}'})

@socketio.on('stop_bot')
def handle_stop_bot(data=None):
    global bot_engine

    try:
        if not bot_engine or not bot_engine.is_running:
            emit('error', {'message': 'Bot is not running'})
            return

        bot_engine.stop()
        socketio.emit('bot_status', {'running': False}) # Broadcast status to all
        emit('success', {'message': 'Bot stopped successfully'})

    except Exception as e:
        logging.error(f'Error stopping bot: {str(e)}')
        emit('error', {'message': f'Failed to stop bot: {str(e)}'})

@socketio.on('stop_all')
def handle_stop_all(data=None):
    global bot_engine
    try:
        if bot_engine:
            bot_engine.stop_bot() # This sets stop_event and stops WS
            socketio.emit('bot_status', {'running': False})
            emit('success', {'message': 'All bot processes stopped completely.'})
        else:
            emit('error', {'message': 'Bot engine not initialized.'})
    except Exception as e:
        logging.error(f'Error in stop_all: {str(e)}')
        emit('error', {'message': f'Failed to stop all: {str(e)}'})

@socketio.on('clear_console')
def handle_clear_console(data=None):
    if bot_engine:
        bot_engine.console_logs.clear()
    emit('console_cleared', {})

@socketio.on('batch_modify_tpsl')
def handle_batch_modify_tpsl(data=None):
    global bot_engine
    if not bot_engine:
         bot_engine = TradingBotEngine(config_file, emit_to_client)
         bot_engine.start(passive_monitoring=True)
    
    bot_engine.batch_modify_tpsl()

@socketio.on('batch_cancel_orders')
def handle_batch_cancel_orders(data=None):
    global bot_engine
    if not bot_engine:
         bot_engine = TradingBotEngine(config_file, emit_to_client)
         bot_engine.start(passive_monitoring=True)
    
    bot_engine.batch_cancel_orders()

@socketio.on('emergency_sl')
def handle_emergency_sl(data=None):
    global bot_engine
    if not bot_engine:
         bot_engine = TradingBotEngine(config_file, emit_to_client)
         bot_engine.start(passive_monitoring=True)
    
    bot_engine.execute_auto_exit(reason="Manual Emergency SL Triggered")


if __name__ == '__main__':
    # Initialize and start in passive monitoring mode on startup
    # This allows Auto-Cal features to run even before user clicks "Start"
    if not bot_engine:
        bot_engine = TradingBotEngine(config_file, emit_to_client)
        bot_engine.start(passive_monitoring=True)
        
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
