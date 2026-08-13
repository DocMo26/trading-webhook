import os
import time
import threading
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
 
app = Flask(__name__)
 
# These come from Render's environment variables (we'll set them up there, not in the code)
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_PASSWORD = os.environ.get("WEBHOOK_PASSWORD")  # simple protection so random people can't trigger trades
 
HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}
 
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
 
 
def send_alert(message):
    """Sends a Telegram message. Never raises — a failed alert shouldn't crash the server."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured, skipping alert:", message)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception as e:
        print("Failed to send Telegram alert:", e)
 
 
def is_regular_market_hours():
    """Returns True if it's currently 9:30am-4:00pm ET on a weekday (regular session)."""
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    if now_ny.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    minutes_since_midnight = now_ny.hour * 60 + now_ny.minute
    market_open = 9 * 60 + 30   # 9:30am
    market_close = 16 * 60      # 4:00pm
    return market_open <= minutes_since_midnight < market_close
 
 
STOP_LOSS_PERCENT = 0.015  # 1.5%
 
 
def cancel_open_orders_for_symbol(symbol):
    """Cancels any resting orders (e.g. a pending stop-loss leg) that are holding
    shares of this symbol, so a manual sell we submit right after isn't rejected
    for 'insufficient qty available'."""
    try:
        resp = requests.get(
            f"{ALPACA_BASE_URL}/v2/orders",
            params={"status": "open", "symbols": symbol},
            headers=HEADERS,
        )
        if resp.status_code != 200:
            print(f"cancel_open_orders_for_symbol: failed to list orders for {symbol}", resp.text)
            return
        for o in resp.json():
            cancel_resp = requests.delete(f"{ALPACA_BASE_URL}/v2/orders/{o['id']}", headers=HEADERS)
            print(f"Cancelled open order {o['id']} for {symbol}: {cancel_resp.status_code}")
    except Exception as e:
        print(f"cancel_open_orders_for_symbol error for {symbol}:", e)
 
 
def get_real_position_qty(symbol):
    """Returns the actual open share quantity for this symbol on Alpaca (0 if none)."""
    try:
        resp = requests.get(f"{ALPACA_BASE_URL}/v2/positions/{symbol}", headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            return float(resp.json().get("qty", 0))
        return 0  # 404 means no position — that's expected, not an error
    except Exception as e:
        print(f"get_real_position_qty error for {symbol}:", e)
        return None  # None = "couldn't check", different from 0 = "confirmed no position"
 
 
def process_order(data):
    """Does the actual Alpaca order submission. Runs in a background thread so the
    /webhook route can respond to TradingView instantly, since TradingView times out
    webhook deliveries after just a few seconds."""
    ticker = data.get("ticker")
    action = data.get("action")
    quantity = data.get("quantity")
    limit_price = data.get("price")
 
    regular_hours = is_regular_market_hours()
    limit_price_float = float(limit_price)
 
    # TradingView's own simulated strategy position can drift from the real
    # Alpaca account (e.g. after a failed order, a manual close, or unrealistic
    # position sizing). Check the real position before acting, instead of
    # blindly trusting the signal.
    real_qty = get_real_position_qty(ticker)
 
    if action == "sell":
        if real_qty == 0:
            print(f"Ignoring phantom sell signal for {ticker} — no real position held")
            send_alert(f"ℹ️ Ignored sell signal for {ticker} — no real position exists on Alpaca (phantom signal).")
            return
        if real_qty is not None:
            quantity = real_qty  # sell exactly what we actually hold, not what the signal assumed
        cancel_open_orders_for_symbol(ticker)  # release any resting stop-loss leg first
 
    if action == "buy" and real_qty and real_qty > 0:
        print(f"Ignoring buy signal for {ticker} — already holding {real_qty} shares")
        send_alert(f"ℹ️ Ignored buy signal for {ticker} — already holding {real_qty} shares (avoiding duplicate entry).")
        return
 
    order = {
        "symbol": ticker,
        "qty": str(quantity),
        "side": action,
        "type": "limit",
        "limit_price": str(round(limit_price_float, 2)),
        "time_in_force": "day",
        "extended_hours": not regular_hours,
    }
 
    stop_loss_attached = False
 
    if action == "buy" and regular_hours:
        stop_price = round(limit_price_float * (1 - STOP_LOSS_PERCENT), 2)
        order["order_class"] = "oto"  # one-triggers-other: allows stop_loss alone, unlike "bracket" which requires take_profit too
        order["stop_loss"] = {"stop_price": str(stop_price)}
        stop_loss_attached = True
 
    try:
        response = requests.post(f"{ALPACA_BASE_URL}/v2/orders", json=order, headers=HEADERS, timeout=20)
        print("Alpaca response:", response.status_code, response.text)
 
        if response.status_code not in (200, 201):
            send_alert(
                f"⚠️ ORDER FAILED\nSymbol: {ticker}\nAction: {action}\nQty: {quantity}\n"
                f"Price: {limit_price}\nAlpaca status: {response.status_code}\nResponse: {response.text}"
            )
        elif stop_loss_attached:
            send_alert(f"✅ BUY FILLED with stop loss\nSymbol: {ticker}\nQty: {quantity}\nLimit price: {limit_price}")
    except Exception as e:
        print("process_order error:", e)
        send_alert(f"🚨 ORDER SUBMISSION ERROR\nSymbol: {ticker}\nAction: {action}\n{e}")
 
 
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    print("Received signal:", data)
 
    if WEBHOOK_PASSWORD and data.get("password") != WEBHOOK_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
 
    ticker = data.get("ticker")
    action = data.get("action")
    quantity = data.get("quantity")
    limit_price = data.get("price")
 
    if not all([ticker, action, quantity, limit_price]):
        return jsonify({"error": "missing fields", "data": data}), 400
 
    # hand off the actual order submission to a background thread and respond
    # immediately — TradingView only waits a few seconds for a webhook reply
    threading.Thread(target=process_order, args=(data,), daemon=True).start()
 
    return jsonify({"received": True, "ticker": ticker, "action": action}), 200
 
 
@app.route("/", methods=["GET"])
def home():
    return "Webhook server is running."
 
 
# --- SOFTWARE STOP LOSS MONITOR (for extended-hours positions) ---
selling_in_progress = set()  # symbols we've already triggered a protective sell for
 
 
def check_positions_and_protect():
    try:
        positions_resp = requests.get(f"{ALPACA_BASE_URL}/v2/positions", headers=HEADERS)
        if positions_resp.status_code != 200:
            print("Monitor: failed to fetch positions", positions_resp.text)
            return
 
        current_symbols = set()
        for position in positions_resp.json():
            symbol = position["symbol"]
            current_symbols.add(symbol)
            side = position["side"]
            qty = position["qty"]
            avg_entry_price = float(position["avg_entry_price"])
            current_price = float(position["current_price"])
 
            if side != "long":
                continue
            if symbol in selling_in_progress:
                continue
 
            drop_percent = (avg_entry_price - current_price) / avg_entry_price
 
            if drop_percent >= STOP_LOSS_PERCENT:
                print(f"Monitor: {symbol} down {drop_percent:.2%} from entry — selling {qty} shares")
                cancel_open_orders_for_symbol(symbol)
                sell_order = {
                    "symbol": symbol,
                    "qty": qty,
                    "side": "sell",
                    "type": "limit",
                    "limit_price": str(round(current_price, 2)),
                    "time_in_force": "day",
                    "extended_hours": not is_regular_market_hours(),
                }
                resp = requests.post(f"{ALPACA_BASE_URL}/v2/orders", json=sell_order, headers=HEADERS)
                print("Monitor: protective sell response", resp.status_code, resp.text)
                if resp.status_code in (200, 201):
                    selling_in_progress.add(symbol)
                    send_alert(
                        f"🛑 STOP LOSS TRIGGERED\nSymbol: {symbol}\nDropped {drop_percent:.2%} from entry "
                        f"(entry: {avg_entry_price}, current: {current_price})\nSell order submitted for {qty} shares."
                    )
                else:
                    send_alert(
                        f"🚨 STOP LOSS SELL FAILED\nSymbol: {symbol}\nDropped {drop_percent:.2%} from entry, "
                        f"but the protective sell order was REJECTED.\nStatus: {resp.status_code}\nResponse: {resp.text}\n"
                        f"Please check this position manually."
                    )
 
        selling_in_progress.intersection_update(current_symbols)
    except Exception as e:
        print("Monitor error:", e)
        send_alert(f"🚨 MONITOR ERROR\nThe stop-loss monitor hit an error and may not be protecting positions:\n{e}")
 
 
def is_force_close_time():
    """Returns True from 17:55 ET onward for the rest of the day — keeps retrying
    if an earlier close attempt failed, instead of giving up after one minute."""
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    minutes_since_midnight = now_ny.hour * 60 + now_ny.minute
    return minutes_since_midnight >= (17 * 60 + 55)
 
 
force_closed_today = set()
last_force_close_date = None
 
 
def force_close_all_positions():
    try:
        positions_resp = requests.get(f"{ALPACA_BASE_URL}/v2/positions", headers=HEADERS)
        if positions_resp.status_code != 200:
            print("Force-close: failed to fetch positions", positions_resp.text)
            return
 
        for position in positions_resp.json():
            symbol = position["symbol"]
            side = position["side"]
            qty = position["qty"]
            current_price = float(position["current_price"])
 
            if side != "long":
                continue
            if symbol in force_closed_today:
                continue
 
            print(f"Force-close: closing {symbol} ({qty} shares) — 17:55 NY reached")
            cancel_open_orders_for_symbol(symbol)
            sell_order = {
                "symbol": symbol,
                "qty": qty,
                "side": "sell",
                "type": "limit",
                "limit_price": str(round(current_price, 2)),
                "time_in_force": "day",
                "extended_hours": not is_regular_market_hours(),
            }
            resp = requests.post(f"{ALPACA_BASE_URL}/v2/orders", json=sell_order, headers=HEADERS)
            print("Force-close: sell response", resp.status_code, resp.text)
            if resp.status_code in (200, 201):
                force_closed_today.add(symbol)
                send_alert(f"🔔 FORCE CLOSE (17:55 NY)\nSymbol: {symbol}\nSell order submitted for {qty} shares.")
            else:
                send_alert(
                    f"🚨 FORCE CLOSE FAILED\nSymbol: {symbol}\nCouldn't close position at 17:55 NY.\n"
                    f"Status: {resp.status_code}\nResponse: {resp.text}\nPlease check this position manually."
                )
    except Exception as e:
        print("Force-close error:", e)
        send_alert(f"🚨 FORCE CLOSE ERROR\n{e}")
 
 
def monitor_loop():
    global last_force_close_date
    while True:
        check_positions_and_protect()
        now_ny = datetime.now(ZoneInfo("America/New_York"))
        if is_force_close_time():
            force_close_all_positions()
        elif last_force_close_date != now_ny.date():
            force_closed_today.clear()  # new day — ready to force-close again this evening
            last_force_close_date = now_ny.date()
        time.sleep(30)  # check every 30 seconds
 
 
threading.Thread(target=monitor_loop, daemon=True).start()
 
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
