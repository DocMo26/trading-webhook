
import os
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
 
 
def is_regular_market_hours():
    """Returns True if it's currently 9:30am-4:00pm ET on a weekday (regular session)."""
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    if now_ny.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    minutes_since_midnight = now_ny.hour * 60 + now_ny.minute
    market_open = 9 * 60 + 30   # 9:30am
    market_close = 16 * 60      # 4:00pm
    return market_open <= minutes_since_midnight < market_close
 
 
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
        "extended_hours": not is_regular_market_hours(),
    }
 
    response = requests.post(
        f"{ALPACA_BASE_URL}/v2/orders",
        json=order,
        headers=HEADERS,
    )
 
    print("Alpaca response:", response.status_code, response.text)
 
    return jsonify({
        "sent_order": order,
        "alpaca_status": response.status_code,
        "alpaca_response": response.json() if response.text else {},
    }), response.status_code
 
 
@app.route("/", methods=["GET"])
def home():
    return "Webhook server is running."
 
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
