
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
 
 
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    print("Received signal:", data)
 
    # simple password check
    if WEBHOOK_PASSWORD and data.get("password") != WEBHOOK_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
 
    ticker = data.get("ticker")
    action = data.get("action")  # "buy" or "sell"
    quantity = data.get("quantity")
    limit_price = data.get("price")  # comes from Pine script, e.g. {{strategy.order.price}} or close
 
    if not all([ticker, action, quantity, limit_price]):
        return jsonify({"error": "missing fields", "data": data}), 400
 
    regular_hours = is_regular_market_hours()
    limit_price_float = float(limit_price)
 
    order = {
        "symbol": ticker,
        "qty": str(quantity),
        "side": action,
        "type": "limit",
        "limit_price": str(limit_price),
        "time_in_force": "day",
        # only flag as extended_hours when we're actually outside the regular
        # 9:30-16:00 ET session — during regular hours this routes orders through
        # a slower venue, which caused real delays we saw in testing
        "extended_hours": not regular_hours,
    }
 
    stop_loss_attached = False
 
    # Attach a stop loss automatically for buy orders — but only during regular
    # market hours, since Alpaca bracket orders (entry + stop loss together)
    # are not supported for extended_hours orders.
    if action == "buy" and regular_hours:
        stop_price = round(limit_price_float * (1 - STOP_LOSS_PERCENT), 2)
        order["order_class"] = "oto"  # one-triggers-other: allows stop_loss alone, unlike "bracket" which requires take_profit too
        order["stop_loss"] = {"stop_price": str(stop_price)}
        stop_loss_attached = True
 
    response = requests.post(
        f"{ALPACA_BASE_URL}/v2/orders",
        json=order,
        headers=HEADERS,
    )
 
    print("Alpaca response:", response.status_code, response.text)
 
    if response.status_code not in (200, 201):
        send_alert(
            f"⚠️ ORDER FAILED\nSymbol: {ticker}\nAction: {action}\nQty: {quantity}\n"
            f"Price: {limit_price}\nAlpaca status: {response.status_code}\nResponse: {response.text}"
        )
 
    return jsonify({
        "sent_order": order,
        "stop_loss_attached": stop_loss_attached,
        "alpaca_status": response.status_code,
        "alpaca_response": response.json() if response.text else {},
    }), response.status_code
 
 
@app.route("/", methods=["GET"])
def home():
    return "Webhook server is running."
 
 
# --- SOFTWARE STOP LOSS MONITOR (for extended-hours positions) ---
# Regular-hours buys already get a real broker-side stop loss attached (bracket
# order above). Positions opened outside regular hours can't have a bracket
# stop attached, so this background loop watches them itself: if the price
# drops more than STOP_LOSS_PERCENT below the average entry price, it submits
# a sell order for that position.
 
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
                sell_order = {
                    "symbol": symbol,
                    "qty": qty,
                    "side": "sell",
                    "type": "limit",
                    "limit_price": str(current_price),
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
 
        # a symbol no longer in open positions means it fully closed out —
        # clear it so a future re-entry gets monitored again
        selling_in_progress.intersection_update(current_symbols)
    except Exception as e:
        print("Monitor error:", e)
        send_alert(f"🚨 MONITOR ERROR\nThe stop-loss monitor hit an error and may not be protecting positions:\n{e}")
 
 
def is_force_close_time():
    """Returns True during the 17:55-17:56 ET minute window (force-close all open trades)."""
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    return now_ny.hour == 17 and now_ny.minute == 55
 
 
force_closed_today = set()  # symbols already force-closed today, to avoid repeat orders in the same minute
 
 
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
            sell_order = {
                "symbol": symbol,
                "qty": qty,
                "side": "sell",
                "type": "limit",
                "limit_price": str(current_price),
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
    while True:
        check_positions_and_protect()
        if is_force_close_time():
            force_close_all_positions()
        else:
            force_closed_today.clear()  # reset once we're past the 17:55 minute, ready for tomorrow
        time.sleep(30)  # check every 30 seconds
 
 
threading.Thread(target=monitor_loop, daemon=True).start()
 
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
