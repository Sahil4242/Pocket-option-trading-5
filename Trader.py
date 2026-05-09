import asyncio
import json
import time
import hmac
import hashlib
import requests
import websockets
from flask import Flask, request, jsonify
from datetime import datetime

# ============================================
# CONFIG — fill these in
# ============================================
POCKET_OPTION_EMAIL    = "YOUR_EMAIL"
POCKET_OPTION_PASSWORD = "YOUR_PASSWORD"
TELEGRAM_BOT_TOKEN     = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID       = "YOUR_CHAT_ID"
TRADE_AMOUNT           = 1      # USD per trade (start small!)
TRADE_EXPIRY           = 60     # seconds (1 minute)
ASSET                  = "EURUSD_otc"
WEBHOOK_PORT           = 5000
WEBHOOK_SECRET         = "your_secret_key_123"
# ============================================

app = Flask(__name__)

# ── Telegram helper ──────────────────────────
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"})

# ── Pocket Option WebSocket trader ───────────
class PocketOptionTrader:
    def __init__(self):
        self.ws_url = "wss://api-l.po.market/socket.io/?EIO=4&transport=websocket"
        self.session_token = None
        self.connected = False

    def get_session(self):
        """Login and get session token"""
        try:
            resp = requests.post(
                "https://api-l.po.market/api/v1/cabinet/login",
                json={
                    "email": POCKET_OPTION_EMAIL,
                    "password": POCKET_OPTION_PASSWORD
                },
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            data = resp.json()
            if "token" in data:
                self.session_token = data["token"]
                print(f"✅ Login successful. Token: {self.session_token[:20]}...")
                return True
            else:
                print(f"❌ Login failed: {data}")
                return False
        except Exception as e:
            print(f"❌ Login error: {e}")
            return False

    async def place_trade(self, direction):
        """Place a trade via WebSocket"""
        if not self.session_token:
            if not self.get_session():
                return False, "Login failed"

        try:
            async with websockets.connect(
                self.ws_url,
                extra_headers={"Authorization": f"Bearer {self.session_token}"},
                ping_interval=20
            ) as ws:

                # Wait for connection ack
                await asyncio.wait_for(ws.recv(), timeout=5)

                # Send auth
                auth_msg = json.dumps({
                    "action": "auth",
                    "token": self.session_token
                })
                await ws.send(f"42{auth_msg}")
                await asyncio.sleep(1)

                # Build trade payload
                trade_direction = 1 if direction.upper() == "CALL" else 0
                trade_payload = json.dumps([
                    "openOrder",
                    {
                        "asset": ASSET,
                        "amount": TRADE_AMOUNT,
                        "action": trade_direction,
                        "expiration": TRADE_EXPIRY,
                        "time": int(time.time())
                    }
                ])

                await ws.send(f"42{trade_payload}")
                print(f"📤 Trade sent: {direction} ${TRADE_AMOUNT} on {ASSET}")

                # Wait for response
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=10)
                    print(f"📥 Response: {response}")
                    return True, response
                except asyncio.TimeoutError:
                    return True, "Trade sent (no confirmation received)"

        except Exception as e:
            print(f"❌ Trade error: {e}")
            return False, str(e)


trader = PocketOptionTrader()


# ── Webhook endpoint (called by n8n) ─────────
@app.route("/trade", methods=["POST"])
def trade():
    # Verify secret
    secret = request.headers.get("X-Secret")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    direction = data.get("signal", "").upper()
    source    = data.get("source", "n8n")

    if direction not in ["CALL", "PUT"]:
        return jsonify({"error": f"Invalid signal: {direction}"}), 400

    print(f"\n🔔 Trade request from {source}: {direction}")

    # Run async trade in sync context
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    success, result = loop.run_until_complete(trader.place_trade(direction))
    loop.close()

    now = datetime.now().strftime("%H:%M:%S")

    if success:
        msg = (
            f"{'📈' if direction == 'CALL' else '📉'} <b>TRADE EXECUTED</b>\n\n"
            f"Direction: <b>{direction}</b>\n"
            f"Asset: {ASSET}\n"
            f"Amount: ${TRADE_AMOUNT}\n"
            f"Expiry: {TRADE_EXPIRY}s\n"
            f"Time: {now}\n"
            f"Source: {source}"
        )
        send_telegram(msg)
        return jsonify({"status": "success", "direction": direction, "time": now})
    else:
        msg = f"❌ Trade FAILED: {direction}\nError: {result}"
        send_telegram(msg)
        return jsonify({"status": "failed", "error": result}), 500


# ── Status endpoint ───────────────────────────
@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "status": "running",
        "asset": ASSET,
        "amount": TRADE_AMOUNT,
        "expiry": TRADE_EXPIRY,
        "logged_in": trader.session_token is not None
    })


# ── Telegram command handler ──────────────────
@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json()
    try:
        msg  = data["message"]
        text = msg["text"].strip().lower()
        chat = str(msg["chat"]["id"])

        if chat != str(TELEGRAM_CHAT_ID):
            return jsonify({}), 200  # ignore other chats

        if text == "/call":
            direction = "CALL"
        elif text == "/put":
            direction = "PUT"
        elif text == "/status":
            send_telegram(
                f"🤖 <b>Bot Status</b>\n"
                f"Running: ✅\nAsset: {ASSET}\n"
                f"Amount: ${TRADE_AMOUNT}\nExpiry: {TRADE_EXPIRY}s"
            )
            return jsonify({}), 200
        elif text == "/help":
            send_telegram(
                "📋 <b>Commands</b>\n\n"
                "/call — Place CALL trade\n"
                "/put — Place PUT trade\n"
                "/status — Bot status\n"
                "/help — This menu"
            )
            return jsonify({}), 200
        else:
            return jsonify({}), 200

        # Execute trade
        send_telegram(f"⏳ Executing {direction} trade...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success, result = loop.run_until_complete(trader.place_trade(direction))
        loop.close()

        now = datetime.now().strftime("%H:%M:%S")
        if success:
            send_telegram(
                f"{'📈' if direction == 'CALL' else '📉'} <b>TRADE EXECUTED</b>\n"
                f"Direction: <b>{direction}</b>\n"
                f"Amount: ${TRADE_AMOUNT} | Expiry: {TRADE_EXPIRY}s\n"
                f"Time: {now}"
            )
        else:
            send_telegram(f"❌ Trade failed: {result}")

    except Exception as e:
        print(f"Telegram webhook error: {e}")

    return jsonify({}), 200


if __name__ == "__main__":
    print("🚀 Pocket Option Auto Trader starting...")
    print(f"📡 Webhook listening on port {WEBHOOK_PORT}")
    print(f"🎯 Asset: {ASSET} | Amount: ${TRADE_AMOUNT} | Expiry: {TRADE_EXPIRY}s")

    # Login on startup
    trader.get_session()

    app.run(host="0.0.0.0", port=WEBHOOK_PORT, debug=False)
