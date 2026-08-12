
import os
import requests
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
        "extended_hours": True,  # required for orders placed outside 9:30-16:00 ET regular session
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
