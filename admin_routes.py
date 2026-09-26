import os
import json
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, render_template
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from admin_routes import admin_bp

app = Flask(__name__)
app.register_blueprint(admin_bp, url_prefix='/admin')

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DATABASE_URL = os.environ.get('DATABASE_URL')
TH_TZ = ZoneInfo('Asia/Bangkok')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    if not DATABASE_URL:
        print("DATABASE_URL is not set. Skipping DB initialization.")
        return
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS contracts (
            id SERIAL PRIMARY KEY,
            contract_number TEXT UNIQUE NOT NULL,
            line_user_id TEXT,
            customer_name TEXT NOT NULL,
            id_card TEXT,
            phone TEXT,
            product_name TEXT NOT NULL,
            total_amount NUMERIC(12, 2) NOT NULL,
            total_installments INT NOT NULL,
            installment_amount NUMERIC(12, 2) NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            contract_id INT REFERENCES contracts(id) ON DELETE CASCADE,
            installment_no INT NOT NULL,
            amount NUMERIC(12, 2) NOT NULL,
            status TEXT DEFAULT 'pending',
            paid_at TEXT,
            receipt_no TEXT,
            slip_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    cursor.close()
    conn.close()

try:
    init_db()
except Exception as e:
    print(f"Database initialization error: {e}")

@app.route('/')
def home():
    return "LINE Installment Bot is Running with PostgreSQL"

# API รองรับ JavaScript จาก admin.html ดึงข้อมูลสัญญา
@app.route('/api/contracts', methods=['GET'])
@app.route('/admin/api/contracts', methods=['GET'])
def global_get_contracts_api():
    if not DATABASE_URL:
        return jsonify({'error': 'DATABASE_URL is not set'}), 500

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts ORDER BY id DESC")
        contracts = cursor.fetchall()
        
        contract_list = []
        for c in contracts:
            c_dict = dict(c)
            cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (c['id'],))
            payments = [dict(p) for p in cursor.fetchall()]
            
            c_dict['total_amount'] = float(c_dict['total_amount']) if c_dict['total_amount'] is not None else 0.0
            c_dict['installment_amount'] = float(c_dict['installment_amount']) if c_dict['installment_amount'] is not None else 0.0

            paid_count = sum(1 for p in payments if p['status'] == 'paid')
            remaining_count = c_dict['total_installments'] - paid_count
            remaining_amount = float(remaining_count * c_dict['installment_amount'])
            close_with_discount = remaining_amount * 0.85
            
            c_dict['payments'] = payments
            c_dict['paid_count'] = paid_count
            c_dict['remaining_count'] = remaining_count
            c_dict['remaining_amount'] = remaining_amount
            c_dict['close_with_discount'] = close_with_discount
            contract_list.append(c_dict)

        return jsonify(contract_list)
    finally:
        cursor.close()
        conn.close()

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return 'Invalid signature. Please check your channel access token/secret.', 400
    except Exception as e:
        print(f"Callback error: {e}")
        return 'Internal Server Error', 500

    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()

    if user_text in ["เช็คค่างวด", "ผ่อน", "ชำระเงิน"]:
        reply_user_contracts(event, user_id)

def reply_user_contracts(event, user_id):
    if not DATABASE_URL:
        return

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status = 'active'", (user_id,))
        contracts = cursor.fetchall()

        if not contracts:
            msg = "ไม่พบข้อมูลสัญญาผ่อนชำระของคุณในระบบค่ะ"
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=msg)]
                    )
                )
            return

        for contract in contracts:
            c_dict = dict(contract)
            cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (c_dict['id'],))
            payments = cursor.fetchall()

            paid_count = sum(1 for p in payments if p['status'] == 'paid')
            next_payment = next((p for p in payments if p['status'] != 'paid'), None)

            msg = f"📄 สัญญาเลขที่: {c_dict['contract_number']}\n"
            msg += f"📦 สินค้า: {c_dict['product_name']}\n"
            msg += f"📊 ผ่อนไปแล้ว: {paid_count}/{c_dict['total_installments']} งวด\n"
            
            if next_payment:
                msg += f"🔹 งวดถัดไป: งวดที่ {next_payment['installment_no']}\n"
                msg += f"💰 ยอดที่ต้องชำระ: {float(next_payment['amount']):,.2f} บาท"
            else:
                msg += "🎉 คุณชำระครบทุกงวดแล้ว ขอบคุณค่ะ!"

            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=msg)]
                    )
                )

    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    app.run(port=5000, debug=True)