const socket = io({
    transports: ['websocket', 'polling'],
    upgrade: true,
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 5000,
    timeout: 20000
});

let currentConfig = null;
const configModal = new bootstrap.Modal(document.getElementById('configModal'));
let isBotRunning = false;
let isTransitioning = false;
let orderExpirationCache = {}; // Cache to store calcualted expiration timestamps
let lastUsedFee = 0; // Track last known used fee for Auto-Cal calculation
let lastSizeFee = 0; // Track last known Size Fee for Auto-Cal Size calculation

const safeFix = (val, prec = 2) => {
    const n = Number(val);
    if (isNaN(n) || !isFinite(n)) return '0.00';
    return n.toFixed(prec);
};

document.addEventListener('DOMContentLoaded', () => {
    initializeTheme();

    // Setup listeners first so they work even if initial data load is slow/fails
    setupEventListeners();
    setupSocketListeners();
    startUITimers();

    // Load initial data
    loadConfig().catch(err => console.error('Load Config failed:', err));
    loadStatus().catch(err => console.error('Load Status failed:', err));
});

function initializeTheme() {
    const savedTheme = localStorage.getItem('theme') || 'dark';
    document.body.setAttribute('data-theme', savedTheme);
    document.getElementById('themeToggle').checked = savedTheme === 'light';
    updateThemeIcon(savedTheme);
}

function updateThemeIcon(theme) {
    const icon = document.getElementById('themeIcon');
    icon.className = theme === 'light' ? 'bi bi-sun-fill' : 'bi bi-moon-stars';
}

function setupEventListeners() {
    document.getElementById('themeToggle').addEventListener('change', (e) => {
        const theme = e.target.checked ? 'light' : 'dark';
        document.body.setAttribute('data-theme', theme);
        localStorage.setItem('theme', theme);
        updateThemeIcon(theme);
    });

    document.getElementById('stopAllBtn').addEventListener('click', () => {
        if (confirm('Are you sure you want to STOP ALL? This will shut down the bot completely, including background monitoring.')) {
            socket.emit('stop_all');
        }
    });

    document.getElementById('startStopBtn').addEventListener('click', () => {
        const btn = document.getElementById('startStopBtn');
        btn.disabled = true;
        isTransitioning = true; // Mark as transitioning to block early status syncs

        if (isBotRunning) {
            // Optimistic Update: Stopping
            btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Stopping...';
            btn.className = 'btn btn-warning w-100'; // Transition color
            socket.emit('stop_bot');
        } else {
            // Optimistic Update: Starting
            btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Starting...';
            btn.className = 'btn btn-info w-100'; // Transition color
            socket.emit('start_bot');
        }

        // Safety timeout to re-enable button if response is lost
        setTimeout(() => {
            if (isTransitioning) {
                console.warn('Start/Stop timeout - re-enabling button');
                isTransitioning = false;
                btn.disabled = false;
                loadStatus(); // Force state sync
            }
        }, 8000); // Increased to 8s to account for slow initialization
    });

    document.getElementById('configBtn').addEventListener('click', () => {
        loadConfigToModal();
        configModal.show();
    });

    document.getElementById('saveConfigBtn').addEventListener('click', () => {
        saveConfig();
    });

    document.getElementById('clearConsoleBtn').addEventListener('click', () => {
        socket.emit('clear_console');
        document.getElementById('consoleOutput').innerHTML = '<p class="text-muted">Console cleared</p>';
    });

    document.getElementById('downloadLogsBtn').addEventListener('click', () => {
        window.location.href = '/api/download_logs';
    });

    // Event listener for Emergency SL button
    document.getElementById('emergencySlBtn').addEventListener('click', () => {
        if (confirm('Are you sure you want to trigger an emergency Stop Loss? This will close all open positions at market price.')) {
            socket.emit('emergency_sl');
        }
    });

    // Event listener for Batch Modify TP/SL button
    document.getElementById('batchModifyTPSLBtn').addEventListener('click', () => {
        if (confirm('Are you sure you want to batch modify TP/SL for all open orders?')) {
            socket.emit('batch_modify_tpsl');
        }
    });

    // Event listener for Batch Cancel Orders button
    document.getElementById('batchCancelOrdersBtn').addEventListener('click', () => {
        if (confirm('Are you sure you want to batch cancel all open orders?')) {
            socket.emit('batch_cancel_orders');
        }
    });

    // Manual Fee Refresh
    const refreshFeesBtn = document.getElementById('refreshFeesBtn');
    if (refreshFeesBtn) {
        refreshFeesBtn.addEventListener('click', () => {
            loadConfig();
            loadStatus();
        });
    }

    // Refresh trade metric on fee percentage change
    const feeInput = document.getElementById('tradeFeePercentage');
    if (feeInput) {
        feeInput.addEventListener('change', () => {
            if (currentConfig) {
                currentConfig.trade_fee_percentage = parseFloat(feeInput.value);
            }
        });
    }

    // Event listener for useCandlestickConditions checkbox
    document.getElementById('useCandlestickConditions').addEventListener('change', toggleCandlestickInputs);
    // Call on load to set initial state
    toggleCandlestickInputs();

    // PnL Auto-Cancel listeners (New Dual Mode)
    document.getElementById('usePnlAutoManual').addEventListener('change', (e) => {
        const state = e.target.checked ? 'ACTIVATED' : 'DEACTIVATED';
        addConsoleLog({ message: `Auto-Manual Profit: ${state}`, level: 'info' });
        saveLiveConfigs();
    });
    document.getElementById('pnlAutoManualThreshold').addEventListener('change', saveLiveConfigs);

    document.getElementById('usePnlAutoCal').addEventListener('change', (e) => {
        const state = e.target.checked ? 'ACTIVATED' : 'DEACTIVATED';
        addConsoleLog({ message: `Auto-Cal Profit: ${state}`, level: 'info' });
        saveLiveConfigs();
    });
    document.getElementById('pnlAutoCalTimes').addEventListener('input', () => {
        updateAutoCalDisplay();
    });
    document.getElementById('pnlAutoCalTimes').addEventListener('change', saveLiveConfigs);

    // Auto-Cal Loss listeners
    document.getElementById('usePnlAutoCalLoss').addEventListener('change', (e) => {
        const state = e.target.checked ? 'ACTIVATED' : 'DEACTIVATED';
        addConsoleLog({ message: `Auto-Cal Loss (Close All): ${state}`, level: 'info' });
        saveLiveConfigs();
    });
    document.getElementById('pnlAutoCalLossTimes').addEventListener('input', () => {
        updateAutoCalDisplay();
    });
    document.getElementById('pnlAutoCalLossTimes').addEventListener('change', saveLiveConfigs);

    // Auto-Cal Size (Profit) listeners
    document.getElementById('useSizeAutoCal').addEventListener('change', (e) => {
        const state = e.target.checked ? 'ACTIVATED' : 'DEACTIVATED';
        addConsoleLog({ message: `Auto-Cal Size (Profit): ${state}`, level: 'info' });
        saveLiveConfigs();
    });
    document.getElementById('sizeAutoCalTimes').addEventListener('input', () => {
        updateAutoCalDisplay();
    });
    document.getElementById('sizeAutoCalTimes').addEventListener('change', saveLiveConfigs);

    // Auto-Cal Size Loss listeners
    document.getElementById('useSizeAutoCalLoss').addEventListener('change', (e) => {
        const state = e.target.checked ? 'ACTIVATED' : 'DEACTIVATED';
        addConsoleLog({ message: `Auto-Cal Size Loss: ${state}`, level: 'info' });
        saveLiveConfigs();
    });
    document.getElementById('sizeAutoCalLossTimes').addEventListener('input', () => {
        updateAutoCalDisplay();
    });
    document.getElementById('sizeAutoCalLossTimes').addEventListener('change', saveLiveConfigs);

    // Auto-Add Margin listeners
    document.getElementById('useAutoMargin').addEventListener('change', (e) => {
        const state = e.target.checked ? 'ACTIVATED' : 'DEACTIVATED';
        addConsoleLog({ message: `Auto-Add Margin: ${state}`, level: 'info' });
        saveLiveConfigs();
    });
    document.getElementById('autoMarginOffset').addEventListener('change', saveLiveConfigs);

    document.getElementById('tradeFeePercentage').addEventListener('change', saveLiveConfigs);

    // Safety Line live listeners
    document.getElementById('shortSafetyLinePrice').addEventListener('change', saveLiveConfigs);
    document.getElementById('longSafetyLinePrice').addEventListener('change', saveLiveConfigs);

    // Sync Dashboard Card with Modal Inputs
    document.querySelectorAll('.dashboard-sync').forEach(el => {
        el.addEventListener('change', (e) => {
            const syncId = e.target.dataset.syncId;
            const modalInput = document.getElementById(syncId);
            if (modalInput) {
                if (e.target.type === 'checkbox') {
                    modalInput.checked = e.target.checked;
                } else {
                    modalInput.value = e.target.value;
                }
                // Trigger change on modal input to invoke saveLiveConfigs
                modalInput.dispatchEvent(new Event('change'));
            }
        });
    });

    document.getElementById('testApiKeyBtn').addEventListener('click', testApiKey);

    document.getElementById('tpCloseLimit').addEventListener('change', () => toggleClosePrice('tpCloseLimit', 'tpCloseSameAsTrigger', 'tpClosePriceDiv'));
    document.getElementById('tpCloseSameAsTrigger').addEventListener('change', () => toggleClosePrice('tpCloseLimit', 'tpCloseSameAsTrigger', 'tpClosePriceDiv'));
    document.getElementById('slCloseLimit').addEventListener('change', () => toggleClosePrice('slCloseLimit', 'slCloseSameAsTrigger', 'slClosePriceDiv'));
    document.getElementById('slCloseSameAsTrigger').addEventListener('change', () => toggleClosePrice('slCloseLimit', 'slCloseSameAsTrigger', 'slClosePriceDiv'));
}

function toggleClosePrice(limitId, sameId, divId) {
    const limit = document.getElementById(limitId).checked;
    const same = document.getElementById(sameId).checked;
    const div = document.getElementById(divId);
    if (limit && !same) {
        div.style.display = 'block';
    } else {
        div.style.display = 'none';
    }
}

function toggleCandlestickInputs() {
    const isChecked = document.getElementById('useCandlestickConditions').checked;
    const elementsToToggle = [
        document.getElementById('candlestickTimeframe'),
        document.getElementById('useChgOpenClose'),
        document.getElementById('minChgOpenClose'),
        document.getElementById('maxChgOpenClose'),
        document.getElementById('useChgHighLow'),
        document.getElementById('minChgHighLow'),
        document.getElementById('maxChgHighLow'),
        document.getElementById('useChgHighClose'),
        document.getElementById('minChgHighClose'),
        document.getElementById('maxChgHighClose'),
    ];

    elementsToToggle.forEach(element => {
        element.disabled = !isChecked;
        // Also ensure checkboxes are unchecked if the main toggle is off
        if (element.type === 'checkbox' && !isChecked) {
            element.checked = false;
        }
        // Also clear numeric inputs if disabled
        if (element.type === 'number' && !isChecked) {
            element.value = 0;
        }
    });
}

async function testApiKey() {
    const testBtn = document.getElementById('testApiKeyBtn');
    const originalBtnHtml = testBtn.innerHTML; // Store original button content
    testBtn.disabled = true;
    testBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Testing...'; // Show loading spinner

    const useTestnet = document.getElementById('useTestnet').checked;
    const useDev = document.getElementById('useDeveloperApi').checked;
    let apiKey, apiSecret, passphrase;

    if (useDev) {
        if (useTestnet) {
            apiKey = document.getElementById('devDemoApiKey').value;
            apiSecret = document.getElementById('devDemoApiSecret').value;
            passphrase = document.getElementById('devDemoApiPassphrase').value;
        } else {
            apiKey = document.getElementById('devApiKey').value;
            apiSecret = document.getElementById('devApiSecret').value;
            passphrase = document.getElementById('devPassphrase').value;
        }
    } else {
        if (useTestnet) {
            apiKey = document.getElementById('okxDemoApiKey').value;
            apiSecret = document.getElementById('okxDemoApiSecret').value;
            passphrase = document.getElementById('okxDemoApiPassphrase').value;
        } else {
            apiKey = document.getElementById('okxApiKey').value;
            apiSecret = document.getElementById('okxApiSecret').value;
            passphrase = document.getElementById('okxPassphrase').value;
        }
    }

    try {
        const response = await fetch('/api/test_api_key', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                api_key: apiKey,
                api_secret: apiSecret,
                passphrase: passphrase,
                use_testnet: useTestnet
            }),
        });

        const result = await response.json();

        if (result.success) {
            showNotification('API Key test successful: ' + result.message, 'success');
        } else {
            showNotification('API Key test failed: ' + result.message, 'error');
        }
    } catch (error) {
        console.error('Error testing API key:', error);
        showNotification('Failed to connect to API test endpoint', 'error');
    } finally {
        testBtn.disabled = false;
        testBtn.innerHTML = originalBtnHtml; // Restore original button content
    }
}
function setupSocketListeners() {
    socket.on('connect_error', (err) => {
        console.error('Socket.IO Connection Error:', err);
        addConsoleLog({ message: `Dashboard Connection Error: ${err.message}. Retrying...`, level: 'error' });
    });

    socket.on('connection_status', (data) => {
        console.log('Connected to server:', data);
    });

    socket.on('bot_status', (data) => {
        updateBotStatus(data.running);
    });

    socket.on('account_update', (data) => {
        if (currentConfig?.debug_mode) console.log('Received account_update', data);
        updateAccountMetrics(data);
    });

    socket.on('trades_update', (data) => {
        updateOpenTrades(data.trades);
    });

    socket.on('position_update', (data) => {
        updatePositionDisplay(data);
    });

    socket.on('console_log', (data) => {
        addConsoleLog(data);
    });

    socket.on('console_log_batch', (data) => {
        if (data && data.logs) {
            const consoleEl = document.getElementById('consoleOutput');
            if (consoleEl) consoleEl.innerHTML = ''; // Clear once before batch
            data.logs.forEach(log => addConsoleLog(log));
        }
    });

    socket.on('console_cleared', () => {
        document.getElementById('consoleOutput').innerHTML = '<p class="text-muted">Console cleared</p>';
    });

    socket.on('price_update', (data) => {
    });

    socket.on('success', (data) => {
        showNotification(data.message, 'success');
    });

    socket.on('error', (data) => {
        showNotification(data.message, 'error');
        // Re-enable start/stop button if it was disabled during an attempt
        const btn = document.getElementById('startStopBtn');
        if (btn) btn.disabled = false;
        isTransitioning = false; // Stop the transition block
        loadStatus(); // Re-sync to current real state
    });

    socket.on('connect', () => {
        console.log('WebSocket connected');
        loadStatus();
    });

    socket.on('disconnect', () => {
        console.log('WebSocket disconnected');
    });
}

function updateBotStatus(running) {
    // If we are currently transitioning (Starting/Stopping), don't let
    // early status polls from background threads flicker the UI back.
    // Only accept the update if it matches the EXPECTED transition outcome.
    if (isTransitioning) {
        if (running === isBotRunning) {
            // This is likely an old state being echoed back (e.g. from a poll while it was starting)
            // Keep the "Starting..." or "Stopping..." UI visible.
            return;
        }
        // If we get here, the state has actually CHANGED as requested (e.g. false -> true)
        isTransitioning = false;
    }

    isBotRunning = running;
    const statusBadge = document.getElementById('botStatus');
    const startStopBtn = document.getElementById('startStopBtn');

    if (running) {
        statusBadge.textContent = 'Running';
        statusBadge.className = 'badge status-badge running';
        startStopBtn.className = 'btn btn-danger w-100';
        // Rebuild content: Icon + Text
        startStopBtn.innerHTML = '<i class="bi bi-stop-fill"></i> <span id="btnText">Stop</span>';
    } else {
        statusBadge.textContent = 'Stopped';
        statusBadge.className = 'badge status-badge stopped';
        startStopBtn.className = 'btn btn-success w-100';
        // Rebuild content: Icon + Text
        startStopBtn.innerHTML = '<i class="bi bi-play-fill"></i> <span id="btnText">Start</span>';
    }
    startStopBtn.disabled = false; // Re-enable the button
}

function updateAccountMetrics(data) {
    if (!data) return;

    // Safety: ignore packets with missing critical fields to prevent flickering to $0.00
    if (data.total_capital === undefined && data.total_balance === undefined) {
        return;
    }

    const safeSetText = (id, text) => {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    };

    // --- HIGH PRIORITY: Need Add Display ---
    const needAddTgt = Number(data.need_add_usdt) || 0;
    const needAddAboveZero = Number(data.need_add_above_zero) || 0;
    const rawNeedAddTgt = Number(data.raw_need_add_usdt) || 0;
    const rawNeedAddAboveZero = Number(data.raw_need_add_above_zero) || 0;

    const tgtEl = document.getElementById('needAddProfitTargetDisplay');
    const zeroEl = document.getElementById('needAddAboveZeroDisplay');

    if (tgtEl) {
        tgtEl.textContent = `$${safeFix(needAddTgt)} ($${safeFix(rawNeedAddTgt)})`;
        if (needAddTgt > 0) {
            tgtEl.parentElement.classList.add('bg-warning-subtle');
            tgtEl.classList.add('text-warning');
        } else {
            tgtEl.parentElement.classList.remove('bg-warning-subtle');
            tgtEl.classList.remove('text-warning');
        }
    }
    if (zeroEl) {
        zeroEl.textContent = `$${safeFix(needAddAboveZero)} ($${safeFix(rawNeedAddAboveZero)})`;
        if (needAddAboveZero > 0) {
            zeroEl.parentElement.classList.add('bg-warning-subtle');
            zeroEl.classList.add('text-warning');
        } else {
            zeroEl.parentElement.classList.remove('bg-warning-subtle');
            zeroEl.classList.remove('text-warning');
        }
    }
    // ----------------------------------------

    if (data.total_capital !== undefined) {
        safeSetText('totalCapital', `$${safeFix(data.total_capital)}`);
    }
    if (data.total_capital_2nd !== undefined) {
        safeSetText('totalCapital2nd', `$${safeFix(data.total_capital_2nd)}`);
    }
    if (data.max_allowed_used_display !== undefined) {
        safeSetText('maxAllowedUsedDisplay', `$${safeFix(data.max_allowed_used_display)}`);
    }
    if (data.max_amount_display !== undefined) {
        safeSetText('maxAmountDisplay', `$${safeFix(data.max_amount_display)}`);
    }
    if (data.used_amount !== undefined) {
        safeSetText('usedAmount', `$${safeFix(data.used_amount)}`);
    }
    const remaining = data.remaining_amount !== undefined ? Number(data.remaining_amount) : 0;
    const minOrder = currentConfig?.min_order_amount || 0;
    const remainingEl = document.getElementById('remainingAmount');
    if (remainingEl) {
        if (!isNaN(remaining) && remaining < minOrder && minOrder > 0) {
            remainingEl.textContent = 'Loop budget exhausted';
            remainingEl.classList.add('text-danger', 'small');
            remainingEl.style.fontSize = '0.75rem';
        } else {
            remainingEl.textContent = `$${safeFix(remaining)}`;
            remainingEl.classList.remove('text-danger', 'small');
            remainingEl.style.fontSize = '';
        }
    }
    if (data.available_balance !== undefined) {
        safeSetText('balance', `$${safeFix(data.available_balance)}`);
    }

    // Update Auto-Cal Add Header based on position side
    const headerEl = document.getElementById('autoAddPosHeader');
    if (headerEl) {
        let side = '';
        if (data.in_position && data.in_position.short) {
            side = 'Short';
        } else if (data.in_position && data.in_position.long) {
            side = 'Long';
        }

        if (side) {
            headerEl.textContent = `Auto-Cal Add ${side} Position`;
        } else {
            // Fallback to configured direction if no active position
            // Use currentConfig (global) or check if it's in data? Usually data doesn't include config.
            const configDirection = currentConfig?.direction;
            if (configDirection === 'short') {
                headerEl.textContent = 'Auto-Cal Add Short Position';
            } else if (configDirection === 'long') {
                headerEl.textContent = 'Auto-Cal Add Long Position';
            } else {
                headerEl.textContent = 'Auto-Cal Add Position';
            }
        }
    }

    const netProfitElement = document.getElementById('netProfit');
    const netProfitValue = data.net_profit !== undefined ? Number(data.net_profit) : 0.00;
    if (netProfitElement) {
        netProfitElement.textContent = `$${netProfitValue.toFixed(2)}`;

        // Color coding for Net Profit
        if (netProfitValue > 0) {
            netProfitElement.classList.remove('text-danger');
            netProfitElement.classList.add('text-success');
        } else if (netProfitValue < 0) {
            netProfitElement.classList.remove('text-success');
            netProfitElement.classList.add('text-danger');
        } else {
            netProfitElement.classList.remove('text-success', 'text-danger');
        }
    }

    // New Advanced Profit Analytics
    safeSetText('totalTradeProfit', `$${safeFix(data.total_trade_profit)}`);
    safeSetText('totalTradeLoss', `$${safeFix(data.total_trade_loss)}`);
    safeSetText('netTradeProfit', `$${safeFix(data.net_trade_profit)}`);

    safeSetText('totalTrades', data.total_trades !== undefined ? String(data.total_trades) : '0');

    // Update daily report if present
    if (data.daily_reports) {
        updateDailyReport(data.daily_reports);
    }

    // Use Backend-provided fee metrics (Centralized Logic)
    const tradeFees = data.trade_fees || 0;
    const usedFee = data.used_fees || 0;
    const remainingFee = (data.remaining_amount || 0) * ((currentConfig?.trade_fee_percentage || 0.07) / 100); // Remaining is still estimate
    const sizeFee = data.size_fees || 0;
    const feeRate = (currentConfig?.trade_fee_percentage || 0.07);

    safeSetText('tradeFees', `$${safeFix(tradeFees)}`);
    safeSetText('usedFee', `$${safeFix(usedFee)}`);
    safeSetText('remainingFee', `$${safeFix(remainingFee)}`);
    safeSetText('feeRateDisplay', `${safeFix(feeRate, 3)}%`);
    safeSetText('sizeAmountDisplay', `$${safeFix(data.size_amount)}`);
    safeSetText('sizeFeeDisplay', `$${safeFix(sizeFee)}`);

    lastUsedFee = usedFee;
    lastSizeFee = sizeFee;


    updateAutoCalDisplay();
    updatePositionDisplay(data);
}

function updateDailyReport(reports) {
    const tableBody = document.getElementById('dailyReportTableBody');
    if (!tableBody) return;

    if (!reports || reports.length === 0) {
        tableBody.innerHTML = '<tr><td colspan="4" class="text-center text-muted py-4">No report data available yet.</td></tr>';
        return;
    }

    // Sort reports by date descending
    const sortedReports = [...reports].sort((a, b) => b.date.localeCompare(a.date));

    tableBody.innerHTML = sortedReports.map(report => `
        <tr>
            <td>${report.date}</td>
            <td>$${(report.total_capital || 0).toFixed(2)}</td>
            <td class="${report.net_trade_profit >= 0 ? 'text-success' : 'text-danger'}">
                $${(report.net_trade_profit || 0).toFixed(2)}
            </td>
            <td>
                <span class="badge ${report.compound_interest >= 1 ? 'bg-success' : 'bg-danger'}">
                    ${((report.compound_interest - 1) * 100).toFixed(2)}%
                </span>
                <small class="text-muted ms-1">(${(report.compound_interest || 0).toFixed(4)})</small>
            </td>
        </tr>
    `).join('');
}

function updateAutoCalDisplay() {
    const safeSetVal = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.value = val;
    };
    const safeGetVal = (id) => {
        const el = document.getElementById(id);
        return el ? parseFloat(el.value) || 0 : 0;
    };

    // Profit
    const profitTimes = safeGetVal('pnlAutoCalTimes');
    const autoProfitValue = lastUsedFee * profitTimes;
    safeSetVal('pnlAutoCalDisplay', autoProfitValue.toFixed(2));

    // Loss
    const lossTimes = safeGetVal('pnlAutoCalLossTimes');
    const autoLossValue = -(lastUsedFee * lossTimes);
    safeSetVal('pnlAutoCalLossDisplay', autoLossValue.toFixed(2));

    // Auto-Cal Size (Profit) (NEW)
    const sizeProfitTimes = safeGetVal('sizeAutoCalTimes');
    // Uses Size Fee basis as requested
    const autoSizeProfitValue = lastSizeFee * sizeProfitTimes;
    safeSetVal('sizeAutoCalDisplay', autoSizeProfitValue.toFixed(2));

    // Auto-Cal Size Loss (NEW)
    const sizeLossTimes = safeGetVal('sizeAutoCalLossTimes');
    const autoSizeLossValue = -(lastSizeFee * sizeLossTimes);
    safeSetVal('sizeAutoCalLossDisplay', autoSizeLossValue.toFixed(2));
}

function updatePositionDisplay(positionData) {
    const mlResultsContainer = document.getElementById('mlStrategyResults');

    if (!positionData) {
        mlResultsContainer.innerHTML = '<p class="text-muted">No active position.</p>';
        return;
    }

    let positionsToRender = [];

    if (positionData.positions) {
        if (positionData.positions.long.in) {
            positionsToRender.push({ side: 'LONG', ...positionData.positions.long });
        }
        if (positionData.positions.short.in) {
            positionsToRender.push({ side: 'SHORT', ...positionData.positions.short });
        }
    } else if (positionData.in_position && typeof positionData.in_position === 'object') {
        // Handle dictionary format: {'long': True, 'short': False}
        ['long', 'short'].forEach(side => {
            if (positionData.in_position[side]) {
                positionsToRender.push({
                    side: side.toUpperCase(),
                    price: positionData.position_entry_price ? positionData.position_entry_price[side] : 0,
                    qty: positionData.position_qty ? positionData.position_qty[side] : 0,
                    upl: positionData.position_upl ? positionData.position_upl[side] : 0,
                    net_pnl: positionData.position_net_pnl ? positionData.position_net_pnl[side] : 0,
                    tp: positionData.current_take_profit ? positionData.current_take_profit[side] : 0,
                    sl: positionData.current_stop_loss ? positionData.current_stop_loss[side] : 0,
                    liq: positionData.position_liq ? positionData.position_liq[side] : 0
                });
            }
        });
    }

    if (positionsToRender.length === 0) {
        mlResultsContainer.innerHTML = '<p class="text-muted">No active position.</p>';
        return;
    }

    let positionHtml = '';
    positionsToRender.forEach(pos => {
        const safeFix4 = (v) => safeFix(v, 4);
        positionHtml += `
            <div class="position-card mb-2 p-2 border rounded ${pos.side.toLowerCase()}-bg">
                <div class="d-flex justify-content-between align-items-center mb-1">
                    <h6 class="mb-0 text-${pos.side === 'LONG' ? 'success' : 'danger'} font-weight-bold">${pos.side} POSITION</h6>
                    <span class="badge bg-${pos.side === 'LONG' ? 'success' : 'danger'}">Active</span>
                </div>
                <div class="row g-0">
                    <div class="col-6 small text-white-50">Entry Price:</div>
                    <div class="col-6 small text-end">${safeFix4(pos.price)}</div>
                    <div class="col-6 small text-white-50">Quantity:</div>
                    <div class="col-6 small text-end">${safeFix4(pos.qty)}</div>
                    <div class="col-6 small text-white-50">Unrealized PnL:</div>
                    <div class="col-6 small text-end ${pos.upl >= 0 ? 'text-success' : 'text-danger'}">$${pos.upl.toFixed(2)}</div>
                    <div class="col-6 small text-white-50">Net PnL (w/ Fees):</div>
                    <div class="col-6 small text-end ${pos.net_pnl >= 0 ? 'text-success' : 'text-danger'}">$${(pos.net_pnl ?? pos.upl).toFixed(2)}</div>
                    <div class="col-6 small text-white-50">Current TP:</div>
                    <div class="col-6 small text-end text-success">${safeFix4(pos.tp)}</div>
                    <div class="col-6 small text-white-50">Current SL:</div>
                    <div class="col-6 small text-end text-danger">${safeFix4(pos.sl)}</div>
                </div>
            </div>
        `;
    });
    mlResultsContainer.innerHTML = positionHtml;

    // Update Liq Gap Display
    const liqGapDisplay = document.getElementById('liqGapDisplay');
    if (liqGapDisplay) {
        let minGap = Infinity;
        positionsToRender.forEach(pos => {
            const liqp = parseFloat(pos.liq || 0);
            const sl = parseFloat(pos.sl || (positionData && positionData.current_stop_loss) || 0);
            if (liqp > 0 && sl > 0) {
                const gap = Math.abs(sl - liqp);
                if (gap < minGap) minGap = gap;
            }
        });

        if (minGap === Infinity) {
            liqGapDisplay.textContent = '$0.00';
            liqGapDisplay.className = 'badge bg-dark';
        } else {
            liqGapDisplay.textContent = `$${minGap.toFixed(2)}`;
            // Color code based on danger (e.g. if gap < autoMarginOffset or just generic warning)
            const offset = parseFloat(document.getElementById('autoMarginOffset').value) || 30;
            if (minGap < offset) {
                liqGapDisplay.className = 'badge bg-danger';
            } else if (minGap < offset * 2) {
                liqGapDisplay.className = 'badge bg-warning text-dark';
            } else {
                liqGapDisplay.className = 'badge bg-success';
            }
        }
    }
}

function updateParametersDisplay() {
    const paramsContainer = document.getElementById('currentParams');
    if (currentConfig) {
        let configHtml = `
           <div class="param-item">
               <span class="param-label">Symbol:</span>
               <span class="param-value">${currentConfig.symbol}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Direction:</span>
               <span class="param-value">${currentConfig.direction}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Mode:</span>
               <span class="param-value">${currentConfig.mode}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Leverage:</span>
               <span class="param-value">${currentConfig.leverage}x</span>
           </div>
           <div class="param-item">
               <span class="param-label">Max Allowed Used (USDT):</span>
               <span class="param-value">${currentConfig.max_allowed_used}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Target Order Amount:</span>
               <span class="param-value">${currentConfig.target_order_amount}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Min Order Amount:</span>
               <span class="param-value">${currentConfig.min_order_amount}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Entry Price Offset:</span>
               <span class="param-value">${currentConfig.entry_price_offset}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Batch Offset:</span>
               <span class="param-value">${currentConfig.batch_offset}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Batch Size Per Loop:</span>
               <span class="param-value">${currentConfig.batch_size_per_loop}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Loop Time:</span>
               <span class="param-value">${currentConfig.loop_time_seconds}s</span>
           </div>
           <div class="param-item">
               <span class="param-label">Rate Divisor:</span>
               <span class="param-value">${currentConfig.rate_divisor}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Short Safety Line Price:</span>
               <span class="param-value">${currentConfig.short_safety_line_price}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Long Safety Line Price:</span>
               <span class="param-value">${currentConfig.long_safety_line_price}</span>
           </div>
           <div class="param-item">
               <span class="param-label">TP Price Offset:</span>
               <span class="param-value">${currentConfig.tp_price_offset}</span>
           </div>
           <div class="param-item">
               <span class="param-label">SL Price Offset:</span>
               <span class="param-value">${currentConfig.sl_price_offset}</span>
           </div>
           <div class="param-item">
               <span class="param-label">TP Amount (%):</span>
               <span class="param-value">${currentConfig.tp_amount}</span>
           </div>
           <div class="param-item">
               <span class="param-label">SL Amount (%):</span>
               <span class="param-value">${currentConfig.sl_amount}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Trigger Price:</span>
               <span class="param-value">${currentConfig.trigger_price}</span>
           </div>
           <div class="param-item">
               <span class="param-label">TP Mode:</span>
               <span class="param-value">${currentConfig.tp_mode}</span>
           </div>
           <div class="param-item">
               <span class="param-label">TP Type:</span>
               <span class="param-value">${currentConfig.tp_type}</span>
           </div>
            <div class="param-item">
                <span class="param-label">Trade Fee %:</span>
                <span class="param-value">${currentConfig.trade_fee_percentage}%</span>
            </div>
            <div class="param-item">
                <span class="param-label">Cancel Unfilled (s):</span>
                <span class="param-value">${currentConfig.cancel_unfilled_seconds}</span>
            </div>
            <div class="param-item">
                <span class="param-label">Cancel if TP below market:</span>
                <span class="param-value">${currentConfig.cancel_on_tp_price_below_market ? 'Yes' : 'No'}</span>
            </div>
            <div class="param-item">
                <span class="param-label">Cancel if TP above market:</span>
                <span class="param-value">${currentConfig.cancel_on_tp_price_above_market ? 'Yes' : 'No'}</span>
            </div>
            <div class="param-item">
                <span class="param-label">Cancel if Entry below market:</span>
                <span class="param-value">${currentConfig.cancel_on_entry_price_below_market ? 'Yes' : 'No'}</span>
            </div>
            <div class="param-item">
                <span class="param-label">Cancel if Entry above market:</span>
                <span class="param-value">${currentConfig.cancel_on_entry_price_above_market ? 'Yes' : 'No'}</span>
            </div>
           <div class="param-item">
               <span class="param-label">Use Candlestick Conditions:</span>
               <span class="param-value">${currentConfig.use_candlestick_conditions ? 'Yes' : 'No'}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Candlestick Timeframe:</span>
               <span class="param-value">${currentConfig.candlestick_timeframe}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Use Chg Open/Close:</span>
               <span class="param-value">${currentConfig.use_chg_open_close ? 'Yes' : 'No'}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Min Chg Open/Close:</span>
               <span class="param-value">${currentConfig.min_chg_open_close}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Max Chg Open/Close:</span>
               <span class="param-value">${currentConfig.max_chg_open_close}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Use Chg High/Low:</span>
               <span class="param-value">${currentConfig.use_chg_high_low ? 'Yes' : 'No'}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Min Chg High/Low:</span>
               <span class="param-value">${currentConfig.min_chg_high_low}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Max Chg High/Low:</span>
               <span class="param-value">${currentConfig.max_chg_high_low}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Use Chg High/Close:</span>
               <span class="param-value">${currentConfig.use_chg_high_close ? 'Yes' : 'No'}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Min Chg High/Close:</span>
               <span class="param-value">${currentConfig.min_chg_high_close}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Max Chg High/Close:</span>
               <span class="param-value">${currentConfig.max_chg_high_close}</span>
           </div>
       `;
        paramsContainer.innerHTML = configHtml;
    } else {
        paramsContainer.innerHTML = '<p class="text-muted">No parameters loaded yet.</p>';
    }
}

function updateOpenTrades(trades) {
    const tradesContainer = document.getElementById('openTrades');

    if (!trades || trades.length === 0) {
        tradesContainer.innerHTML = '<p class="text-muted">No open orders</p>';
        return;
    }

    tradesContainer.innerHTML = trades.map(trade => {
        // Cache the expiration target timestamp to avoid server stutter
        if (trade.time_left !== null) {
            orderExpirationCache[trade.id] = Date.now() + (trade.time_left * 1000);
        } else {
            delete orderExpirationCache[trade.id];
        }

        return `
        <div class="trade-card ${trade.type.toLowerCase()}">
            <div class="trade-header">
                <span class="trade-type ${trade.type.toLowerCase()}">${trade.type}</span>
                <span class="trade-id">ID: ${trade.id} <span class="badge bg-warning text-dark ms-1 timer-badge" data-order-id="${trade.id}">${trade.time_left !== null ? trade.time_left + 's' : ''}</span></span>
            </div>
            <div class="trade-details">
                <div class="trade-detail-item">
                    <span class="trade-detail-label">Entry:</span>
                    <span class="trade-detail-value">${trade.entry_spot_price !== null ? Number(trade.entry_spot_price).toFixed(4) : 'N/A'}</span>
                </div>
                <div class="trade-detail-item">
                    <span class="param-label">Target Order:</span>
                    <span class="param-value">$${trade.stake !== null ? Number(trade.stake).toFixed(2) : 'N/A'}</span>
                </div>
                <div class="trade-detail-item">
                    <span class="trade-detail-label">TP:</span>
                    <span class="trade-detail-value text-success">${trade.tp_price !== null ? Number(trade.tp_price).toFixed(4) : 'N/A'}</span>
                </div>
                <div class="trade-detail-item">
                    <span class="trade-detail-label">SL:</span>
                    <span class="trade-detail-value text-danger">${trade.sl_price !== null ? Number(trade.sl_price).toFixed(4) : 'N/A'}</span>
                </div>
            </div>
        </div>
    `;
    }).join('');
}

function startUITimers() {
    // Precise local countdown timer
    setInterval(() => {
        const now = Date.now();
        const badges = document.querySelectorAll('.timer-badge');
        badges.forEach(badge => {
            const orderId = badge.getAttribute('data-order-id');
            const targetTime = orderExpirationCache[orderId];

            if (targetTime) {
                const remaining = Math.max(0, Math.floor((targetTime - now) / 1000));
                badge.textContent = remaining + 's';

                // Cleanup cache if reached 0
                if (remaining <= 0) delete orderExpirationCache[orderId];
            }
        });
    }, 1000);
}

function addConsoleLog(log) {
    const consoleOutput = document.getElementById('consoleOutput');

    if (consoleOutput.querySelector('.text-muted')) {
        consoleOutput.innerHTML = '';
    }

    const logLine = document.createElement('div');
    logLine.className = `console-line ${log.level}`;
    logLine.innerHTML = `
        <span class="console-timestamp">[${log.timestamp}]</span>
        <span class="console-message">${escapeHtml(log.message)}</span>
    `;

    // Check if user is near the bottom
    const isAtBottom = consoleOutput.scrollHeight - consoleOutput.clientHeight <= consoleOutput.scrollTop + 50;

    consoleOutput.appendChild(logLine);

    if (isAtBottom) {
        consoleOutput.scrollTop = consoleOutput.scrollHeight;
    }

    if (consoleOutput.children.length > 500) {
        consoleOutput.removeChild(consoleOutput.firstChild);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function loadConfig() {
    try {
        const response = await fetch('/api/config');
        currentConfig = await response.json();

        // Sync PnL Auto-Cancel UI (New Dual Mode)
        // Auto-Manual
        const useAutoManual = currentConfig.use_pnl_auto_manual ?? false;
        const manualThreshold = currentConfig.pnl_auto_manual_threshold ?? 100.0;

        const elUseManual = document.getElementById('usePnlAutoManual');
        if (elUseManual) elUseManual.checked = useAutoManual;

        const elManualThresh = document.getElementById('pnlAutoManualThreshold');
        if (elManualThresh) elManualThresh.value = manualThreshold;

        // Auto-Cal
        const useAutoCal = currentConfig.use_pnl_auto_cal ?? false;
        const calTimes = currentConfig.pnl_auto_cal_times ?? 4.0;

        const elUseCal = document.getElementById('usePnlAutoCal');
        if (elUseCal) elUseCal.checked = useAutoCal;

        const elCalTimes = document.getElementById('pnlAutoCalTimes');
        if (elCalTimes) elCalTimes.value = calTimes;

        // Auto-Cal Loss
        const useAutoCalLoss = currentConfig.use_pnl_auto_cal_loss ?? false;
        const calLossTimes = currentConfig.pnl_auto_cal_loss_times ?? 1.5;

        const elUseCalLoss = document.getElementById('usePnlAutoCalLoss');
        if (elUseCalLoss) elUseCalLoss.checked = useAutoCalLoss;

        const elCalLossTimes = document.getElementById('pnlAutoCalLossTimes');
        if (elCalLossTimes) elCalLossTimes.value = calLossTimes;

        // Auto-Cal Size (Profit)
        const useSizeAutoCal = currentConfig.use_size_auto_cal ?? false;
        const sizeCalTimes = currentConfig.size_auto_cal_times ?? 2.0;

        const elUseSizeCal = document.getElementById('useSizeAutoCal');
        if (elUseSizeCal) elUseSizeCal.checked = useSizeAutoCal;

        const elSizeCalTimes = document.getElementById('sizeAutoCalTimes');
        if (elSizeCalTimes) elSizeCalTimes.value = sizeCalTimes;

        // Auto-Cal Size Loss
        const useSizeAutoCalLoss = currentConfig.use_size_auto_cal_loss ?? false;
        const sizeCalLossTimes = currentConfig.size_auto_cal_loss_times ?? 1.5;

        const elUseSizeCalLoss = document.getElementById('useSizeAutoCalLoss');
        if (elUseSizeCalLoss) elUseSizeCalLoss.checked = useSizeAutoCalLoss;

        const elSizeCalLossTimes = document.getElementById('sizeAutoCalLossTimes');
        if (elSizeCalLossTimes) elSizeCalLossTimes.value = sizeCalLossTimes;

        // Auto-Add Margin
        const useAutoMargin = currentConfig.use_auto_margin ?? false;
        const autoMarginOffset = currentConfig.auto_margin_offset ?? 30.0;

        const elUseAutoMargin = document.getElementById('useAutoMargin');
        if (elUseAutoMargin) elUseAutoMargin.checked = useAutoMargin;

        const elAutoMarginOffset = document.getElementById('autoMarginOffset');
        if (elAutoMarginOffset) elAutoMarginOffset.value = autoMarginOffset;

        // Auto-Cal Add Position
        const useAddPosAboveZero = currentConfig.use_add_pos_above_zero ?? false;
        const useAddPosProfitTarget = currentConfig.use_add_pos_profit_target ?? currentConfig.use_add_pos_auto_cal ?? false;

        const elAboveZero = document.getElementById('useAddPosAboveZero');
        if (elAboveZero) elAboveZero.checked = useAddPosAboveZero;
        const elAboveZeroMain = document.getElementById('useAddPosAboveZeroMain');
        if (elAboveZeroMain) elAboveZeroMain.checked = useAddPosAboveZero;

        const elProfitTarget = document.getElementById('useAddPosProfitTarget');
        if (elProfitTarget) elProfitTarget.checked = useAddPosProfitTarget;
        const elProfitTargetMain = document.getElementById('useAddPosProfitTargetMain');
        if (elProfitTargetMain) elProfitTargetMain.checked = useAddPosProfitTarget;

        const addPosProfitMult = currentConfig.add_pos_profit_multiplier ?? 1.5;
        const elAddPosProfitMult = document.getElementById('addPosProfitMultiplier');
        if (elAddPosProfitMult) elAddPosProfitMult.value = addPosProfitMult;
        const elAddPosProfitMultMain = document.getElementById('addPosProfitMultiplierMain');
        if (elAddPosProfitMultMain) elAddPosProfitMultMain.value = addPosProfitMult;

        const addPosGap = currentConfig.add_pos_gap_threshold ?? 5.0;
        const elAddPosGap = document.getElementById('addPosGapThreshold');
        if (elAddPosGap) elAddPosGap.value = addPosGap;
        const elAddPosGapMain = document.getElementById('addPosGapThresholdMain');
        if (elAddPosGapMain) elAddPosGapMain.value = addPosGap;

        const step2Offset = currentConfig.add_pos_step2_offset ?? 0.0;
        const elStep2Offset = document.getElementById('addPosStep2Offset');
        if (elStep2Offset) elStep2Offset.value = step2Offset;

        // New Percentage Based Martingale Fields
        const addPosSizePct = currentConfig.add_pos_size_pct ?? 30.0;
        const elSizePct = document.getElementById('addPosSizePct');
        if (elSizePct) elSizePct.value = addPosSizePct;
        const elSizePctMain = document.getElementById('addPosSizePctMain');
        if (elSizePctMain) elSizePctMain.value = addPosSizePct;

        const addPosMaxCount = currentConfig.add_pos_max_count ?? 10;
        const elMaxCount = document.getElementById('addPosMaxCount');
        if (elMaxCount) elMaxCount.value = addPosMaxCount;
        const elMaxCountMain = document.getElementById('addPosMaxCountMain');
        if (elMaxCountMain) elMaxCountMain.value = addPosMaxCount;

        const addPosGapOffset = currentConfig.add_pos_gap_offset ?? 0.0;
        const elGapOffset = document.getElementById('addPosGapOffset');
        if (elGapOffset) elGapOffset.value = addPosGapOffset;
        const elGapOffsetMain = document.getElementById('addPosGapOffsetMain');
        if (elGapOffsetMain) elGapOffsetMain.value = addPosGapOffset;

        const addPosSizePctOffset = currentConfig.add_pos_size_pct_offset ?? 0.0;
        const elSizePctOffset = document.getElementById('addPosSizePctOffset');
        if (elSizePctOffset) elSizePctOffset.value = addPosSizePctOffset;
        const elSizePctOffsetMain = document.getElementById('addPosSizePctOffsetMain');
        if (elSizePctOffsetMain) elSizePctOffsetMain.value = addPosSizePctOffset;

        const addPosOrderType = currentConfig.add_pos_order_type ?? 'Market';
        const elOrderType = document.getElementById('addPosOrderType');
        if (elOrderType) elOrderType.value = addPosOrderType;
        const elOrderTypeMain = document.getElementById('addPosOrderTypeMain');
        if (elOrderTypeMain) elOrderTypeMain.value = addPosOrderType;

        const addPosTimes2 = currentConfig.add_pos_times2 ?? 1.1;
        const elTimes2 = document.getElementById('addPosTimes2');
        if (elTimes2) elTimes2.value = addPosTimes2;
        const elTimes2Main = document.getElementById('addPosTimes2Main');
        if (elTimes2Main) elTimes2Main.value = addPosTimes2;

        const addPosTpOffset2 = currentConfig.add_pos_tp_offset2 ?? 0.0;
        const elTpOffset2 = document.getElementById('addPosTpOffset2');
        if (elTpOffset2) elTpOffset2.value = addPosTpOffset2;
        const elTpOffset2Main = document.getElementById('addPosTpOffset2Main');
        if (elTpOffset2Main) elTpOffset2Main.value = addPosTpOffset2;

        // Add live listeners if not already added
        const fieldsToWatch = [
            'useAddPosAboveZero', 'useAddPosProfitTarget',
            'addPosProfitMultiplier', 'addPosGapThreshold', 'addPosSizePct', 'addPosMaxCount',
            'addPosGapOffset', 'addPosSizePctOffset', 'addPosOrderType', 'addPosTimes2', 'addPosTpOffset2'
        ];

        fieldsToWatch.forEach(fieldId => {
            // Find both modal and main dashboard inputs (id might be same or specialized)
            const input = document.getElementById(fieldId.replace(/_([a-z])/g, (g) => g[1].toUpperCase())); // camelCase
            if (input && !input.dataset.listener) {
                input.addEventListener('change', saveLiveConfigs);
                input.dataset.listener = 'true';
            }
        });

        // Legacy fallback
        if (currentConfig.use_pnl_auto_cancel !== undefined && currentConfig.use_pnl_auto_manual === undefined) {
            if (elUseManual) elUseManual.checked = currentConfig.use_pnl_auto_cancel;
            if (elManualThresh) elManualThresh.value = currentConfig.pnl_auto_cancel_threshold;
        }

        // Initial dashboard trade fee % sync
        const feeInput = document.getElementById('tradeFeePercentage');
        if (feeInput) {
            feeInput.value = currentConfig.trade_fee_percentage !== undefined ? currentConfig.trade_fee_percentage : 0.07;
        }
        updateAutoCalDisplay();
    } catch (error) {
        console.error('Error loading config:', error);
        showNotification('Failed to load configuration', 'error');
    }
}

async function loadStatus() {
    try {
        const response = await fetch('/api/status');
        const status = await response.json();

        updateBotStatus(status.running);
        updateAccountMetrics(status);
        updateOpenTrades(status.open_trades);
        updatePositionDisplay(status);
        updateParametersDisplay(); // Call the new function to populate parameters tab
    } catch (error) {
        console.error('Error loading status:', error);
    }
}

function loadConfigToModal() {
    if (!currentConfig) return;

    const safeSetVal = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.value = (val !== undefined && val !== null) ? val : '';
    };
    const safeSetChecked = (id, checked) => {
        const el = document.getElementById(id);
        if (el) el.checked = !!checked;
    };

    safeSetVal('okxApiKey', currentConfig.okx_api_key);
    safeSetVal('okxApiSecret', currentConfig.okx_api_secret);
    safeSetVal('okxPassphrase', currentConfig.okx_passphrase);
    safeSetVal('okxDemoApiKey', currentConfig.okx_demo_api_key);
    safeSetVal('okxDemoApiSecret', currentConfig.okx_demo_api_secret);
    safeSetVal('okxDemoApiPassphrase', currentConfig.okx_demo_api_passphrase);
    safeSetVal('devApiKey', currentConfig.dev_api_key);
    safeSetVal('devApiSecret', currentConfig.dev_api_secret);
    safeSetVal('devPassphrase', currentConfig.dev_passphrase);
    safeSetVal('devDemoApiKey', currentConfig.dev_demo_api_key);
    safeSetVal('devDemoApiSecret', currentConfig.dev_demo_api_secret);
    safeSetVal('devDemoApiPassphrase', currentConfig.dev_demo_api_passphrase);
    safeSetChecked('useTestnet', currentConfig.use_testnet);
    safeSetChecked('useDeveloperApi', currentConfig.use_developer_api);
    safeSetVal('symbol', currentConfig.symbol);
    safeSetVal('shortSafetyLinePrice', currentConfig.short_safety_line_price);
    safeSetVal('longSafetyLinePrice', currentConfig.long_safety_line_price);
    safeSetVal('leverage', currentConfig.leverage);
    safeSetVal('maxAllowedUsed', currentConfig.max_allowed_used);
    safeSetVal('entryPriceOffset', currentConfig.entry_price_offset);
    safeSetVal('batchOffset', currentConfig.batch_offset);
    safeSetVal('tpPriceOffset', currentConfig.tp_price_offset);
    safeSetVal('slPriceOffset', currentConfig.sl_price_offset);
    safeSetVal('loopTimeSeconds', currentConfig.loop_time_seconds);
    safeSetVal('rateDivisor', currentConfig.rate_divisor);
    safeSetVal('batchSizePerLoop', currentConfig.batch_size_per_loop);
    safeSetVal('minOrderAmount', currentConfig.min_order_amount);
    safeSetVal('targetOrderAmount', currentConfig.target_order_amount);
    safeSetVal('cancelUnfilledSeconds', currentConfig.cancel_unfilled_seconds);
    safeSetChecked('cancelOnTpPriceBelowMarket', currentConfig.cancel_on_tp_price_below_market);
    safeSetChecked('cancelOnTpPriceAboveMarket', currentConfig.cancel_on_tp_price_above_market);
    safeSetChecked('cancelOnEntryPriceBelowMarket', currentConfig.cancel_on_entry_price_below_market);
    safeSetChecked('cancelOnEntryPriceAboveMarket', currentConfig.cancel_on_entry_price_above_market);
    safeSetVal('tradeFeePercentage', currentConfig.trade_fee_percentage ?? 0.07);

    // New fields
    safeSetVal('direction', currentConfig.direction);
    safeSetVal('mode', currentConfig.mode);
    safeSetVal('tpAmount', currentConfig.tp_amount);
    safeSetVal('slAmount', currentConfig.sl_amount);
    safeSetVal('triggerPrice', currentConfig.trigger_price);
    safeSetVal('tpMode', currentConfig.tp_mode);
    safeSetVal('tpType', currentConfig.tp_type);
    safeSetChecked('useCandlestickConditions', currentConfig.use_candlestick_conditions);

    // Candlestick conditions
    safeSetChecked('useChgOpenClose', currentConfig.use_chg_open_close);
    safeSetVal('minChgOpenClose', currentConfig.min_chg_open_close);
    safeSetVal('maxChgOpenClose', currentConfig.max_chg_open_close);
    safeSetChecked('useChgHighLow', currentConfig.use_chg_high_low);
    safeSetVal('minChgHighLow', currentConfig.min_chg_high_low);
    safeSetVal('maxChgHighLow', currentConfig.max_chg_high_low);
    safeSetChecked('useChgHighClose', currentConfig.use_chg_high_close);
    safeSetVal('minChgHighClose', currentConfig.min_chg_high_close);
    safeSetVal('maxChgHighClose', currentConfig.max_chg_high_close);
    safeSetVal('candlestickTimeframe', currentConfig.candlestick_timeframe);
    safeSetVal('okxPosMode', currentConfig.okx_pos_mode ?? 'net_mode');

    safeSetChecked('tpCloseLimit', currentConfig.tp_close_limit);
    safeSetChecked('tpCloseSameAsTrigger', currentConfig.tp_close_same_as_trigger);
    safeSetChecked('slCloseLimit', currentConfig.sl_close_limit);
    safeSetChecked('slCloseSameAsTrigger', currentConfig.sl_close_same_as_trigger);
    safeSetVal('tpClosePrice', currentConfig.tp_close_price ?? 0);
    safeSetVal('slClosePrice', currentConfig.sl_close_price ?? 0);

    toggleClosePrice('tpCloseLimit', 'tpCloseSameAsTrigger', 'tpClosePriceDiv');
    toggleClosePrice('slCloseLimit', 'slCloseSameAsTrigger', 'slClosePriceDiv');

    // PnL Auto-Cancel (Modal Sync -> Maps to Auto-Manual Profit)
    safeSetChecked('usePnlAutoCancelModal', currentConfig.use_pnl_auto_manual ?? false);
    safeSetVal('pnlAutoCancelThresholdModal', currentConfig.pnl_auto_manual_threshold ?? 100.0);

    // Populate Add Pos fields in modal explicitly
    safeSetVal('addPosProfitMultiplier', currentConfig.add_pos_profit_multiplier ?? 1.5);
    safeSetVal('addPosGapThreshold', currentConfig.add_pos_gap_threshold ?? 5.0);
    safeSetVal('addPosSizePct', currentConfig.add_pos_size_pct ?? 30.0);
    safeSetVal('addPosMaxCount', currentConfig.add_pos_max_count ?? 10);
    safeSetVal('addPosGapOffset', currentConfig.add_pos_gap_offset ?? 0.0);
    safeSetVal('addPosSizePctOffset', currentConfig.add_pos_size_pct_offset ?? 0.0);
    safeSetVal('addPosOrderType', currentConfig.add_pos_order_type ?? 'Market');
    safeSetVal('addPosTimes2', currentConfig.add_pos_times2 ?? 1.1);
    safeSetVal('addPosTpOffset2', currentConfig.add_pos_tp_offset2 ?? 0.0);
}

// Helper to keep dashboard and modal in sync - Removed old PnL sync listeners as modal update is pending
// TODO: Update modal with new fields if needed.

async function saveConfig() {
    const getVal = (id) => document.getElementById(id)?.value;
    const getNum = (id, def = 0) => {
        const val = parseFloat(getVal(id));
        return isNaN(val) ? def : val;
    };
    const getInt = (id, def = 0) => {
        const val = parseInt(getVal(id));
        return isNaN(val) ? def : val;
    };
    const getCheck = (id) => !!document.getElementById(id)?.checked;

    const newConfig = {
        okx_api_key: getVal('okxApiKey'),
        okx_api_secret: getVal('okxApiSecret'),
        okx_passphrase: getVal('okxPassphrase'),
        okx_demo_api_key: getVal('okxDemoApiKey'),
        okx_demo_api_secret: getVal('okxDemoApiSecret'),
        okx_demo_api_passphrase: getVal('okxDemoApiPassphrase'),
        dev_api_key: getVal('devApiKey'),
        dev_api_secret: getVal('devApiSecret'),
        dev_passphrase: getVal('devPassphrase'),
        dev_demo_api_key: getVal('devDemoApiKey'),
        dev_demo_api_secret: getVal('devDemoApiSecret'),
        dev_demo_api_passphrase: getVal('devDemoApiPassphrase'),
        use_developer_api: getCheck('useDeveloperApi'),
        use_testnet: getCheck('useTestnet'),
        symbol: getVal('symbol'),
        short_safety_line_price: getNum('shortSafetyLinePrice'),
        long_safety_line_price: getNum('longSafetyLinePrice'),
        leverage: getInt('leverage'),
        max_allowed_used: getNum('maxAllowedUsed'),
        entry_price_offset: getNum('entryPriceOffset'),
        batch_offset: getNum('batchOffset'),
        tp_price_offset: getNum('tpPriceOffset'),
        sl_price_offset: getNum('slPriceOffset'),
        loop_time_seconds: getInt('loopTimeSeconds'),
        rate_divisor: getInt('rateDivisor'),
        batch_size_per_loop: getInt('batchSizePerLoop'),
        min_order_amount: getNum('minOrderAmount'),
        target_order_amount: getNum('targetOrderAmount'),
        cancel_unfilled_seconds: getInt('cancelUnfilledSeconds'),
        cancel_on_tp_price_below_market: getCheck('cancelOnTpPriceBelowMarket'),
        cancel_on_tp_price_above_market: getCheck('cancelOnTpPriceAboveMarket'),
        cancel_on_entry_price_below_market: getCheck('cancelOnEntryPriceBelowMarket'),
        cancel_on_entry_price_above_market: getCheck('cancelOnEntryPriceAboveMarket'),
        trade_fee_percentage: getNum('tradeFeePercentage', 0.07),

        // New fields
        direction: getVal('direction'),
        mode: getVal('mode'),
        tp_amount: getNum('tpAmount'),
        sl_amount: getNum('slAmount'),
        trigger_price: getVal('triggerPrice'),
        tp_mode: getVal('tpMode'),
        tp_type: getVal('tpType'),
        use_candlestick_conditions: getCheck('useCandlestickConditions'),

        // Candlestick conditions
        use_chg_open_close: getCheck('useChgOpenClose'),
        min_chg_open_close: getNum('minChgOpenClose'),
        max_chg_open_close: getNum('maxChgOpenClose'),
        use_chg_high_low: getCheck('useChgHighLow'),
        min_chg_high_low: getNum('minChgHighLow'),
        max_chg_high_low: getNum('maxChgHighLow'),
        use_chg_high_close: getCheck('useChgHighClose'),
        min_chg_high_close: getNum('minChgHighClose'),
        max_chg_high_close: getNum('maxChgHighClose'),
        add_pos_gap_threshold: getNum('addPosGapThreshold', 5.0),
        add_pos_profit_multiplier: getNum('addPosProfitMultiplier', 1.5),
        add_pos_step2_offset: getNum('addPosStep2Offset', 0.0),
        add_pos_size_pct: getNum('addPosSizePct', 30.0),
        add_pos_max_count: getInt('addPosMaxCount', 10),
        add_pos_gap_offset: getNum('addPosGapOffset', 0.0),
        add_pos_size_pct_offset: getNum('addPosSizePctOffset', 0.0),
        use_add_pos_profit_target: getCheck('useAddPosProfitTarget'),

        candlestick_timeframe: getVal('candlestickTimeframe'),
        okx_pos_mode: getVal('okxPosMode'),

        tp_close_limit: getCheck('tpCloseLimit'),
        tp_close_same_as_trigger: getCheck('tpCloseSameAsTrigger'),
        sl_close_limit: getCheck('slCloseLimit'),
        sl_close_same_as_trigger: getCheck('slCloseSameAsTrigger'),
        tp_close_price: getNum('tpClosePrice', 0),
        sl_close_price: getNum('slClosePrice', 0),

        add_pos_order_type: getVal('addPosOrderType') || 'Market',
        add_pos_times2: getNum('addPosTimes2', 1.1),
        add_pos_tp_offset2: getNum('addPosTpOffset2', 0.0),

        // PnL Auto-Cancel (New Dual Mode - Unified with Modal)
        // If modal inputs exist, use them. Otherwise use dashboard/current config.
        use_pnl_auto_manual: document.getElementById('usePnlAutoCancelModal')?.checked ?? document.getElementById('usePnlAutoManual')?.checked ?? currentConfig.use_pnl_auto_manual,
        pnl_auto_manual_threshold: parseFloat(document.getElementById('pnlAutoCancelThresholdModal')?.value ?? document.getElementById('pnlAutoManualThreshold')?.value ?? currentConfig.pnl_auto_manual_threshold),
        use_pnl_auto_cal: document.getElementById('usePnlAutoCal')?.checked ?? currentConfig.use_pnl_auto_cal ?? false,
        pnl_auto_cal_times: parseFloat(document.getElementById('pnlAutoCalTimes')?.value ?? currentConfig.pnl_auto_cal_times ?? 4.0),
        use_pnl_auto_cal_loss: document.getElementById('usePnlAutoCalLoss')?.checked ?? currentConfig.use_pnl_auto_cal_loss ?? false,
        pnl_auto_cal_loss_times: parseFloat(document.getElementById('pnlAutoCalLossTimes')?.value ?? currentConfig.pnl_auto_cal_loss_times ?? 1.5),

        // Auto-Cal Size (New)
        use_size_auto_cal: document.getElementById('useSizeAutoCal')?.checked ?? currentConfig.use_size_auto_cal ?? false,
        size_auto_cal_times: parseFloat(document.getElementById('sizeAutoCalTimes')?.value ?? currentConfig.size_auto_cal_times ?? 2.0),
        use_size_auto_cal_loss: document.getElementById('useSizeAutoCalLoss')?.checked ?? currentConfig.use_size_auto_cal_loss ?? false,
        size_auto_cal_loss_times: parseFloat(document.getElementById('sizeAutoCalLossTimes')?.value ?? currentConfig.size_auto_cal_loss_times ?? 1.5),

        // Auto-Cal Add Position (Split Mode)
        use_add_pos_above_zero: document.getElementById('useAddPosAboveZero')?.checked ?? currentConfig.use_add_pos_above_zero ?? false,
        use_add_pos_profit_target: document.getElementById('useAddPosProfitTarget')?.checked ?? currentConfig.use_add_pos_profit_target ?? false,
        add_pos_profit_multiplier: parseFloat(document.getElementById('addPosProfitMultiplier')?.value ?? currentConfig.add_pos_profit_multiplier ?? 1.5),
        add_pos_order_type: document.getElementById('addPosOrderType')?.value ?? currentConfig.add_pos_order_type ?? 'Market',
        add_pos_times2: parseFloat(document.getElementById('addPosTimes2')?.value ?? currentConfig.add_pos_times2 ?? 1.1),
        add_pos_tp_offset2: parseFloat(document.getElementById('addPosTpOffset2')?.value ?? currentConfig.add_pos_tp_offset2 ?? 0.0)
    };

    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(newConfig),
        });

        const result = await response.json();

        if (result.success) {
            currentConfig = newConfig;
            configModal.hide();
            showNotification(result.message || 'Configuration saved successfully', 'success');
            updateParametersDisplay(); // Refresh the parameters display
        } else {
            showNotification(result.message, 'error');
        }
    } catch (error) {
        console.error('Error saving config:', error);
        showNotification('Failed to save configuration', 'error');
    }
}

// Function to save specific configs without closing modal (live updates)
async function saveLiveConfigs() {
    if (!currentConfig) return;

    const getVal = (id) => document.getElementById(id)?.value;
    const getNum = (id, def = 0) => {
        const val = parseFloat(getVal(id));
        return isNaN(val) ? def : val;
    };
    const getInt = (id, def = 0) => {
        const val = parseInt(getVal(id));
        return isNaN(val) ? def : val;
    };
    const getCheck = (id) => !!document.getElementById(id)?.checked;

    const liveConfig = {
        use_pnl_auto_manual: getCheck('usePnlAutoManual'),
        pnl_auto_manual_threshold: getNum('pnlAutoManualThreshold', 100.0),
        use_pnl_auto_cal: getCheck('usePnlAutoCal'),
        pnl_auto_cal_times: getNum('pnlAutoCalTimes', 4.0),
        use_pnl_auto_cal_loss: getCheck('usePnlAutoCalLoss'),
        pnl_auto_cal_loss_times: getNum('pnlAutoCalLossTimes', 1.5),

        // Auto-Cal Size (New)
        use_size_auto_cal: getCheck('useSizeAutoCal'),
        size_auto_cal_times: getNum('sizeAutoCalTimes', 2.0),
        use_size_auto_cal_loss: getCheck('useSizeAutoCalLoss'),
        size_auto_cal_loss_times: getNum('sizeAutoCalLossTimes', 1.5),

        trade_fee_percentage: getNum('tradeFeePercentage', 0.07),

        // Safety Lines (Add to live updates)
        short_safety_line_price: getNum('shortSafetyLinePrice'),
        long_safety_line_price: getNum('longSafetyLinePrice'),

        // Auto-Add Margin
        use_auto_margin: getCheck('useAutoMargin'),
        auto_margin_offset: getNum('autoMarginOffset', 30.0),

        // Auto-Cal Add Position (Split Mode)
        use_add_pos_above_zero: getCheck('useAddPosAboveZero'),
        use_add_pos_profit_target: getCheck('useAddPosProfitTarget'),
        add_pos_profit_multiplier: getNum('addPosProfitMultiplier', 1.5),
        add_pos_gap_threshold: getNum('addPosGapThreshold', 5.0),
        add_pos_size_pct: getNum('addPosSizePct', 30.0),
        add_pos_max_count: getInt('addPosMaxCount', 10),
        add_pos_step2_offset: getNum('addPosStep2Offset', 0.0),
        add_pos_gap_offset: getNum('addPosGapOffset', 0.0),
        add_pos_size_pct_offset: getNum('addPosSizePctOffset', 0.0),
        add_pos_order_type: getVal('addPosOrderType') || 'Market',
        add_pos_times2: getNum('addPosTimes2', 1.1),
        add_pos_tp_offset2: getNum('addPosTpOffset2', 0.0)
    };

    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(liveConfig)
        });
        const data = await response.json();
        if (data.success) {
            showNotification('Auto-Exit settings saved', 'success');
            // Update local currentConfig but don't reload everything
            Object.assign(currentConfig, liveConfig);
        } else {
            // Revert UI on error (e.g. bot running error)
            document.getElementById('usePnlAutoManual').checked = currentConfig.use_pnl_auto_manual;
            document.getElementById('pnlAutoManualThreshold').value = currentConfig.pnl_auto_manual_threshold;
            document.getElementById('usePnlAutoCal').checked = currentConfig.use_pnl_auto_cal;
            document.getElementById('pnlAutoCalTimes').value = currentConfig.pnl_auto_cal_times;
            document.getElementById('usePnlAutoCalLoss').checked = currentConfig.use_pnl_auto_cal_loss;
            document.getElementById('pnlAutoCalLossTimes').value = currentConfig.pnl_auto_cal_loss_times;
            document.getElementById('tradeFeePercentage').value = currentConfig.trade_fee_percentage;
            showNotification(data.message, 'error');
        }
    } catch (error) {
        console.error('Error saving live config:', error);
    }
}

function showNotification(message, type) {
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type === 'success' ? 'success' : 'danger'} alert-dismissible fade show position-fixed top-0 start-50 translate-middle-x mt-3`;
    alertDiv.style.zIndex = '10000'; // Increased z-index to ensure visibility
    alertDiv.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    `;

    document.body.appendChild(alertDiv);

    setTimeout(() => {
        alertDiv.remove();
    }, 5000);
}
