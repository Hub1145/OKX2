## OKX Trading Bot Dashboard: Workflow Documentation

This document outlines the operational workflow and key functionalities of the OKX Trading Bot Dashboard, designed to provide a clear understanding for clients.

### 1. Introduction
The OKX Trading Bot Dashboard is a real-time web application that allows users to monitor and control an automated trading bot specifically designed for the OKX exchange. The bot operates based on a single, configurable limit order strategy, focusing on transparent control and immediate operational feedback.

### 2. Core Components

The system is comprised of three main components:

*   **Frontend (Web Dashboard)**: A user-friendly web interface built with Bootstrap 5 and Vanilla JavaScript, providing real-time data visualization, bot controls, and configuration management.
*   **Backend (Flask Application)**: A Python-based Flask application that handles API requests from the frontend, manages the trading bot's lifecycle, and facilitates real-time communication via Flask-SocketIO.
*   **Trading Bot Engine (`bot_engine.py`)**: The core logic of the trading bot, written in Python, responsible for interacting with the OKX exchange via its REST and Public WebSocket APIs, executing the trading strategy, and managing orders and positions. **(Note: The bot now uses public WebSocket endpoints only, meaning real-time private account data like orders and positions are no longer streamed via WebSocket. This data is fetched periodically via REST API.)**

### 3. Operational Workflow

The system operates through the following interconnected steps:

#### 3.1. Initialization and Setup

1.  **Dashboard Access**: The client accesses the web dashboard, typically via a web browser (desktop or mobile-responsive).
2.  **Configuration Loading**: Upon loading, the dashboard fetches the current trading parameters from the backend (`config.json`).
3.  **Theme Initialization**: The dashboard initializes its visual theme (Light/Dark Mode) based on user preference.
4.  **Bot Status Check**: The dashboard queries the backend for the current status of the trading bot (running/stopped).

#### 3.2. Configuration Management

1.  **Accessing Configuration**: The client clicks the "Config" button on the dashboard to open a modal window displaying all configurable trading parameters.
2.  **Parameter Display**: All parameters from `config.json` (e.g., API keys, symbol, leverage, safety lines, price offsets, loop times, batch offset, Max Allowed Used, Max Amount, and new cancel conditions) are displayed in editable input fields within the modal.
3.  **Updating Configuration**: The client can modify any of these parameters.
4.  **Saving Changes**: Upon clicking "Save Changes", the new parameters are sent to the backend via an API request.
5.  **Backend Validation**: The backend receives the new configuration. If the bot is currently running, the update is rejected to prevent conflicts.
6.  **Configuration Persistence**: If valid, the new parameters are saved to `config.json` on the server.
7.  **Confirmation**: The dashboard displays a success or error notification.

#### 3.3. Bot Control and Monitoring

1.  **Starting the Bot**:
    *   The user clicks the "Start" button.
    *   A signal is sent to the backend (`app.py`).
    *   The backend initiates the `TradingBotEngine`.
    *   The `TradingBotEngine` performs a sequence of startup actions:
        *   Synchronizes server time with OKX.
        *   Fetches product information for the specified trading symbol.
        *   Sets the configured leverage on the OKX account.
        *   Checks for and closes any existing open positions to ensure a clean start.
        *   Initializes a Public WebSocket connection to OKX for real-time market data. **(Note: Private account data like orders and positions are no longer streamed via WebSocket due to public-only mode.)**
    *   The dashboard updates to show the bot's "Running" status.
2.  **Stopping the Bot**:
    *   The user clicks the "Stop" button.
    *   A signal is sent to the backend (`app.py`).
    *   The backend signals the `TradingBotEngine` to stop.
    *   The `TradingBotEngine` gracefully closes its WebSocket connection and halts its trading logic.
    *   The dashboard updates to show the bot's "Stopped" status.
3.  **Real-Time Data Display**:
    *   **Account Overview**: Displays the current balance and total trades. **(Note: Account balance is fetched periodically via REST API, not real-time via WebSocket.)**
    *   **Current Parameters**: A dedicated section on the dashboard constantly displays the currently active trading parameters, ensuring transparency.
    *   **Current Position**: Shows details of any active trading position, including entry price, quantity, current Take Profit (TP), and Stop Loss (SL) levels. **(Note: Position details are fetched periodically via REST API, not real-time via WebSocket.)**
    *   **Open Orders**: Lists any pending or partially filled orders. **(Note: Open orders are fetched periodically via REST API, not real-time via WebSocket.)**
    *   **Console Output**: Provides a live stream of log messages from the bot, detailing actions, signals, and any errors or warnings.
4.  **Emergency SL (Close All Positions)**:
    *   The client clicks the "Emergency SL" button.
    *   A confirmation prompt appears.
    *   If confirmed, a signal is sent to the backend.
    *   The `TradingBotEngine` attempts to immediately close all open positions at market price and cancels all pending exit orders (TP/SL). This button serves as the single mechanism for batch closing positions.
5.  **Batch Modify TP/SL**:
    *   The client clicks the "Batch Modify TP/SL" button.
    *   A confirmation prompt appears.
    *   If confirmed, a signal is sent to the backend.
    *   The `TradingBotEngine` fetches all open positions, recalculates new TP/SL prices based on current market price and configured offsets, and attempts to modify the existing algo orders on OKX.
6.  **Batch Cancel Orders**:
    *   The client clicks the "Batch Cancel Orders" button.
    *   A confirmation prompt appears.
    *   If confirmed, a signal is sent to the backend.
    *   The `TradingBotEngine` fetches all pending orders and attempts to cancel them on OKX.

#### 3.4. Trading Strategy Execution (within `bot_engine.py`)

1.  **Main Loop**: The bot runs a continuous trading loop at a configurable interval (`loop_time_seconds`).
2.  **Market Data Acquisition**: In each loop, the bot fetches the current market price for the configured symbol.
3.  **Entry Condition Check**:
    *   The bot checks if it's already in a position or has a pending entry order. If so, it skips entry.
    *   It compares the current market price against `short_safety_line_price` and `long_safety_line_price` to determine if a buy (long) or sell (short) signal is present.
4.  **Batch Order Initiation**:
    *   If an entry signal is detected and no position/pending order exists, the bot initiates a batch of limit orders as defined by `batch_size_per_loop`.
    *   Order sizing will now be based on `Max Allowed Used` and `Rate Divisor` to calculate `Max Amount`.
    *   The first order in the batch uses the `limit_entry_price` calculated from the `current_market_price` and `entry_price_offset`.
    *   Subsequent orders in the batch adjust their `limit_entry_price` by adding (`for short`) or subtracting (`for long`) the `batch_offset` from the previous order's price.
    *   A limit order is placed on OKX for each order in the batch. The order ID and details are stored as pending entries.
5.  **Position Lifecycle Management**:
    *   A dedicated thread monitors the status of pending entry orders. If an order is not filled within `cancel_unfilled_seconds`, it is automatically canceled. Additionally, if enabled in configuration, orders will be cancelled immediately if:
        *   Condition 2: TP price becomes below market price (for short positions) or above market price (for long positions).
        *   Condition 3: Entry price becomes below market price (for long positions) or above market price (for short positions).
    *   Once an entry order is filled (or partially filled), the bot confirms the actual position details from OKX.
    *   **Dynamic TP/SL Placement**: Based on the confirmed entry price and the trade direction (long/short), the bot calculates the precise `current_take_profit` and `current_stop_loss` prices using the `tp_price_offset` and `sl_price_offset`. These are then placed as conditional (algo) orders on OKX. Future enhancements may include OCO (One Cancels the Other) and Split TP/SL order types, which will involve more advanced order management.
    *   **TP/SL Monitoring**: The bot continuously monitors for TP or SL hits through periodic checks. **(Note: Real-time WebSocket updates for TP/SL hits are no longer available. Monitoring is done via periodic REST API calls.)**
        *   If a TP order is filled on OKX, or if the market price crosses the calculated TP price (e.g., for short, market price drops below TP price; for long, market price rises above TP price), the `_handle_tp_hit` protocol is executed.
        *   If an SL order is filled on OKX, or if the position is closed unexpectedly (indicating an SL hit), the `_handle_sl_hit` protocol is executed.
6.  **Exit Protocols (`_handle_tp_hit`, `_handle_sl_hit`)**:
    *   These protocols manage the closure of positions. They involve:
        *   Canceling any remaining pending entry orders.
        *   Confirming the actual open position quantity.
        *   Placing market orders to close any remaining partial positions.
        *   Canceling any active TP/SL algo orders associated with the closed position.
        *   Resetting the bot's internal position state.
        *   Updating account balance information.
7.  **Batch Action Execution**:
    *   The `batch_modify_tpsl` and `batch_cancel_orders` methods are triggered by corresponding UI actions. They interact with the OKX API to perform their respective batch operations on orders and positions. The "Emergency SL" function now covers batch closing of positions.

---