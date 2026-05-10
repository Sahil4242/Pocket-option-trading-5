import asyncio
import json
import time
import os
import requests
import websockets
from flask import Flask, request, jsonify
from datetime import datetime

# ============================================
# CONFIG — reads from environment variables
# ============================================
POCKET_OPTION_EMAIL    = os.environ.get("POCKET_OPTION_EMAIL", "")
POCKET_OPTION_PASSWORD = os.environ.get("POCKET_OPTION_PASSWORD", "")
TELEGRAM_BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET         = os.environ.get("WEBHOOK_SECRET", "")
TRADE_AMOUNT           = 10       # ₹10 INR per trade
TRADE_CURRENCY         = "INR"    # Currency
TRADE_EXPIRY           = 60       # 1 minute
ASSET                  = "EURUSD_otc"
# ============================================

app = Flask(__name__)

# ── Telegram helper ──────────────────────────
def send_telegram(chat_id, msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Telegram send error: {e}")

# ── Pocket Option Trader ─────────────────────
class PocketOptionTrader:
    def __init__(self):
        self.ws_url = "wss://api-l.po.market/socket.io/?EIO=4&transport=websocket"
        self.session_token = None

    def get_session(self):
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
                print("✅ Login successful.")
                return True
            else:
                print(f"❌ Login failed: {data}")
                return False
        except Exception as e:
            print(f"❌ Login error: {e}")
            return False

    async def place_trade(self, direction):
        if not self.session_token:
            if not self.get_session():
                return False, "Login failed — check credentials"
        try:
            async with websockets.connect(
                self.ws_url,
                extra_headers={"Authorization": f"Bearer {self.session_token}"},
                ping_interval=20
            ) as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)
                auth_msg = json.dumps({"action": "auth", "token": self.session_token})
                await ws.send(f"42{auth_msg}")
                await asyncio.sleep(1)

                trade_direction = 1 if direction.upper() == "CALL" else 0
                trade_payload = json.dumps([
                    "openOrder",
                    {
                        "asset": ASSET,
                        "amount": TRADE_AMOUNT,
                        "action": trade_direction,
                        "expiration": TRADE_EXPIRY,
                        "time": int(time.time()),
                        "currency": TRADE_CURRENCY
                    }
                ])
                await ws.send(f"42{trade_payload}")
                print(f"📤 Trade sent: {direction} ₹{TRADE_AMOUNT}")

                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=10)
                    return True, response
                except asyncio.TimeoutError:
                    return True, "Trade sent successfully"

        except Exception as e:
            print(f"❌ Trade error: {e}")
            return False, str(e)


trader = PocketOptionTrader()


# ── Health check ─────────────────────────────
@app.route("/", methods=["GET"])
@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "status": "running",
        "asset": ASSET,
        "amount": f"₹{TRADE_AMOUNT}",
        "currency": TRADE_CURRENCY,
        "expiry": TRADE_EXPIRY,
        "logged_in": trader.session_token is not None
    })


# ── n8n webhook endpoint ──────────────────────
@app.route("/trade", methods=["POST"])
def trade():
    secret = request.headers.get("X-Secret")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data received"}), 400

    direction = data.get("signal", "").upper()
    source = data.get("source", "n8n")

    if direction not in ["CALL", "PUT"]:
        return jsonify({"error": f"Invalid signal: {direction}"}), 400

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
            f"Amount: ₹{TRADE_AMOUNT}\n"
            f"Expiry: {TRADE_EXPIRY}s\n"
            f"Time: {now}\n"
            f"Source: {source}"
        )
        send_telegram(TELEGRAM_CHAT_ID, msg)
        return jsonify({"status": "success", "direction": direction})
    else:
        send_telegram(TELEGRAM_CHAT_ID, f"❌ Trade FAILED: {direction}\nError: {result}")
        return jsonify({"status": "failed", "error": result}), 500


# ── Telegram command handler ──────────────────
@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({}), 200

        msg = data.get("message") or data.get("edited_message")
        if not msg:
            return jsonify({}), 200

        chat_id = str(msg.get("chat", {}).get("id", ""))
        text_raw = msg.get("text", "")
        if not text_raw:
            return jsonify({}), 200

        text = text_raw.split("@")[0].strip().lower()
        first_name = msg.get("chat", {}).get("first_name", "Trader")

        print(f"📩 Command: {text} from {chat_id}")

        if text == "/start":
            send_telegram(chat_id,
                f"👋 Welcome <b>{first_name}</b>!\n\n"
                f"🤖 Pocket Option Auto Trader is active.\n\n"
                f"Type /help to see all commands."
            )

        elif text == "/help":
            send_telegram(chat_id,
                "📋 <b>Available Commands</b>\n\n"
                "/call — Place CALL trade 📈\n"
                "/put — Place PUT trade 📉\n"
                "/status — Check bot status\n"
                "/help — Show this menu\n\n"
                f"Asset: <b>{ASSET}</b>\n"
                f"Amount: <b>₹{TRADE_AMOUNT}</b>\n"
                f"Expiry: <b>{TRADE_EXPIRY}s</b>"
            )

        elif text == "/status":
            logged = "✅ Connected" if trader.session_token else "❌ Not logged in"
            send_telegram(chat_id,
                f"🤖 <b>Bot Status</b>\n\n"
                f"Server: ✅ Running\n"
                f"Pocket Option: {logged}\n"
                f"Asset: {ASSET}\n"
                f"Amount: ₹{TRADE_AMOUNT}\n"
                f"Expiry: {TRADE_EXPIRY}s"
            )

        elif text in ["/call", "/put"]:
            direction = "CALL" if text == "/call" else "PUT"
            send_telegram(chat_id, f"⏳ Placing <b>{direction}</b> trade of ₹{TRADE_AMOUNT}...")

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            success, result = loop.run_until_complete(trader.place_trade(direction))
            loop.close()

            now = datetime.now().strftime("%H:%M:%S")
            if success:
                send_telegram(chat_id,
                    f"{'📈' if direction == 'CALL' else '📉'} <b>TRADE EXECUTED</b>\n\n"
                    f"Direction: <b>{direction}</b>\n"
                    f"Asset: {ASSET}\n"
                    f"Amount: ₹{TRADE_AMOUNT}\n"
                    f"Expiry: {TRADE_EXPIRY}s\n"
                    f"Time: {now}"
                )
            else:
                send_telegram(chat_id, f"❌ Trade failed!\nReason: {result}")

        else:
            send_telegram(chat_id, "❓ Unknown command. Type /help to see all commands.")

    except Exception as e:
        print(f"Webhook error: {e}")

    return jsonify({}), 200


if __name__ == "__main__":
    print("🚀 Pocket Option Auto Trader starting...")
    print(f"🎯 Asset: {ASSET} | Amount: ₹{TRADE_AMOUNT} INR | Expiry: {TRADE_EXPIRY}s")
    trader.get_session()
    app.run(host="0.0.0.0", port=5000, debug=False)
        else:
        send_telegram(TELEGRAM_CHAT_ID, f"❌ Trade FAILED: {direction}\nError: {result}")
        return jsonify({"status": "failed", "error": result}), 500
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
