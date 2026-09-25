import base64
import io
import os
import sqlite3
from flask import Flask, jsonify, render_template, request, send_file
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    FlexBubble,
    FlexContainer,
    FlexMessage,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent
import qrcode

app = Flask(__name__)

# ตั้งค่า LINE Access Token และ Secret (ใส่ค่าจริงในไฟล์ .env)
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "YOUR_CHANNEL_SECRET")
PROMPTPAY_ID = os.getenv("PROMPTPAY_ID", "0812345678")  # เบอร์พร้อมเพย์หรือเลขบัตรประชาชนของร้าน

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

DATABASE = "database.db"


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contracts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_no TEXT UNIQUE NOT NULL,
                customer_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                product_name TEXT NOT NULL,
                total_price REAL NOT NULL,
                price_per_month REAL NOT NULL,
                total_months INTEGER NOT NULL,
                paid_months INTEGER DEFAULT 0,
                status TEXT DEFAULT 'ACTIVE',
                line_user_id TEXT
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id INTEGER NOT NULL,
                month_no INTEGER NOT NULL,
                amount REAL NOT NULL,
                paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (contract_id) REFERENCES contracts (id)
            )
        """
        )
        conn.commit()


init_db()


# === ฟังก์ชันสร้าง PromptPay QR Code ===
def crc16(data: str) -> str:
    crc = 0xFFFF
    for char in data:
        crc ^= ord(char) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return f"{crc:04X}"


def generate_promptpay_payload(target: str, amount: float = 0.0) -> str:
    target = target.replace("-", "").strip()
    if len(target) == 10:  # Phone
        target_formatted = "0066" + target[1:]
        target_type = "0112"
    elif len(target) == 13:  # ID Card
        target_formatted = target
        target_type = "0213"
    else:
        raise ValueError("Invalid PromptPay ID")

    payload = "000201010212"
    merchant_info = f"0016A000000677010111{target_type}{target_formatted}"
    payload += f"29{len(merchant_info):02d}{merchant_info}"
    payload += "5303764"  # THB

    if amount > 0:
        amt_str = f"{amount:.2f}"
        payload += f"54{len(amt_str):02d}{amt_str}"

    payload += "5802TH"
    payload += "6304"
    payload += crc16(payload)
    return payload


# === LINE Webhook Route ===
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    return "OK", 200


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        with get_db() as conn:
            cursor = conn.cursor()

            # สแกน/ค้นหาด้วยเบอร์ หรือ เลขสัญญา
            cursor.execute(
                "SELECT * FROM contracts WHERE (phone = ? OR contract_no = ?) AND status = 'ACTIVE'",
                (text, text),
            )
            contract = cursor.fetchone()

            if contract:
                # ผูก line_user_id เข้ากับสัญญา
                cursor.execute(
                    "UPDATE contracts SET line_user_id = ? WHERE id = ?",
                    (user_id, contract["id"]),
                )
                conn.commit()

                reply_flex = create_contract_flex(contract)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token, messages=[reply_flex]
                    )
                )
                return

            # ค้นหาสัญญาที่ผูกไว้กับ user_id นี้
            cursor.execute(
                "SELECT * FROM contracts WHERE line_user_id = ? AND status = 'ACTIVE'",
                (user_id,),
            )
            my_contract = cursor.fetchone()

            if text in ["เช็คสัญญา", "ดูสัญญา", "สัญญาของฉัน"]:
                if my_contract:
                    reply_flex = create_contract_flex(my_contract)
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token, messages=[reply_flex]
                        )
                    )
                else:
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[
                                TextMessage(
                                    text="❌ ไม่พบสัญญาที่ผูกไว้ กรุณาพิมพ์ 'เบอร์โทรศัพท์' หรือ 'เลขที่สัญญา' เพื่อลงทะเบียนครับ"
                                )
                            ],
                        )
                    )

            elif text in ["ชำระเงิน", "จ่ายค่างวด"]:
                if my_contract:
                    amount = my_contract["price_per_month"]
                    qr_payload = generate_promptpay_payload(PROMPTPAY_ID, amount)

                    # สร้าง QR Code Image Base64
                    qr = qrcode.QRCode(box_size=10, border=2)
                    qr.add_data(qr_payload)
                    qr.make(fit=True)
                    img = qr.make_image(fill_color="black", back_color="white")

                    buffered = io.BytesIO()
                    img.save(buffered, format="PNG")
                    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")

                    msg = TextMessage(
                        text=f"📱 **QR Code ชำระค่างวด**\n"
                        f"สัญญา: {my_contract['contract_no']}\n"
                        f"งวดที่: {my_contract['paid_months'] + 1}/{my_contract['total_months']}\n"
                        f"ยอดชำระ: {amount:,.2f} บาท\n\n"
                        f"โอนแล้วส่งสลิปแจ้งแอดมินได้เลยครับ!"
                    )
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token, messages=[msg]
                        )
                    )
                else:
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[
                                TextMessage(
                                    text="❌ กรุณาพิมพ์ 'เบอร์โทร' เพื่อระบุสัญญาก่อนทำรายการครับ"
                                )
                            ],
                        )
                    )

            elif text in ["ปิดยอด", "ปิดสัญญา"]:
                if my_contract:
                    remaining_months = (
                        my_contract["total_months"] - my_contract["paid_months"]
                    )
                    payoff_amount = remaining_months * my_contract["price_per_month"]

                    msg = TextMessage(
                        text=f"💰 **รายละเอียดการปิดยอดสัญญา**\n"
                        f"สัญญา: {my_contract['contract_no']}\n"
                        f"คุณผ่อนไปแล้ว: {my_contract['paid_months']}/{my_contract['total_months']} งวด\n"
                        f"คงเหลืออีก: {remaining_months} งวด\n"
                        f"----------------------------------\n"
                        f"🔥 **ยอดปิดสัญญาชำระทั้งหมด: {payoff_amount:,.2f} บาท**\n\n"
                        f"หากต้องการปิดยอด กรุณาติดต่อแอดมินเพื่อยืนยันอีกครั้งครับ"
                    )
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token, messages=[msg]
                        )
                    )
                else:
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[
                                TextMessage(
                                    text="❌ กรุณาพิมพ์ 'เบอร์โทร' เพื่อระบุสัญญาก่อนครับ"
                                )
                            ],
                        )
                    )

            else:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[
                            TextMessage(
                                text="ยินดีต้อนรับครับ! พิมพ์ 'เบอร์โทร' หรือ 'เลขที่สัญญา' เพื่อเช็คข้อมูลสัญญาได้เลยครับ"
                            )
                        ],
                    )
                )


def create_contract_flex(c):
    flex_json = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0d6efd",
            "contents": [
                {
                    "type": "text",
                    "text": "📱 สัญญาผ่อนชำระโทรศัพท์",
                    "color": "#ffffff",
                    "weight": "bold",
                    "size": "lg",
                }
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": f"เลขที่สัญญา: {c['contract_no']}",
                    "weight": "bold",
                    "size": "md",
                },
                {"type": "text", "text": f"ชื่อลูกค้า: {c['customer_name']}"},
                {"type": "text", "text": f"สินค้า: {c['product_name']}"},
                {"type": "separator", "margin": "md"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "md",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"ผ่อนงวดละ: {c['price_per_month']:,.2f} บาท",
                            "color": "#198754",
                            "weight": "bold",
                        },
                        {
                            "type": "text",
                            "text": f"งวดปัจจุบัน: {c['paid_months']}/{c['total_months']} งวด",
                            "color": "#0d6efd",
                        },
                        {
                            "type": "text",
                            "text": f"ราคาทั้งหมด: {c['total_price']:,.2f} บาท",
                            "size": "sm",
                            "color": "#6c757d",
                        },
                    ],
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#198754",
                    "action": {
                        "type": "message",
                        "label": "ชำระค่างวด",
                        "text": "ชำระเงิน",
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "message",
                        "label": "คำนวณปิดยอด",
                        "text": "ปิดยอด",
                    },
                },
            ],
        },
    }
    return FlexMessage(alt_text="รายละเอียดสัญญาของคุณ", contents=FlexContainer.from_dict(flex_json))


# === Admin UI Dashboard Routes ===
@app.route("/admin")
def admin_page():
    return render_template("admin.html")


@app.route("/api/contracts", methods=["GET"])
def api_get_contracts():
    search = request.args.get("search", "")
    with get_db() as conn:
        cursor = conn.cursor()
        if search:
            q = f"%{search}%"
            cursor.execute(
                "SELECT * FROM contracts WHERE contract_no LIKE ? OR customer_name LIKE ? OR phone LIKE ?",
                (q, q, q),
            )
        else:
            cursor.execute("SELECT * FROM contracts ORDER BY id DESC")
        rows = [dict(row) for row in cursor.fetchall()]
    return jsonify(rows)


@app.route("/api/contracts", methods=["POST"])
def api_add_contract():
    data = request.json
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO contracts (contract_no, customer_name, phone, product_name, total_price, price_per_month, total_months)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            (
                data["contract_no"],
                data["customer_name"],
                data["phone"],
                data["product_name"],
                float(data["total_price"]),
                float(data["price_per_month"]),
                int(data["total_months"]),
            ),
        )
        conn.commit()
    return jsonify({"success": True})


@app.route("/api/contracts/<int:cid>/update_paid", methods=["POST"])
def api_update_paid(cid):
    data = request.json
    new_paid = int(data["paid_months"])
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE contracts SET paid_months = ? WHERE id = ?", (new_paid, cid)
        )
        conn.commit()
    return jsonify({"success": True})


@app.route("/api/contracts/<int:cid>/cancel", methods=["POST"])
def api_cancel_contract(cid):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE contracts SET status = 'CANCELLED' WHERE id = ?", (cid,)
        )
        conn.commit()
    return jsonify({"success": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)