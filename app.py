import os
import io
import datetime
import calendar
import urllib.parse
import psycopg2
import werkzeug
from psycopg2.extras import RealDictCursor
from zoneinfo import ZoneInfo
from flask import Flask, request, abort, render_template, jsonify, send_file
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage, ImageSendMessage, QuickReply, QuickReplyButton, MessageAction
)
from promptpay import qrcode
import qrcode as qrcode_lib

from admin_routes import (
    admin_bp, process_contracts_api, process_contract_detail_api, 
    process_payments_by_contract_api, pay_contract_installment_api, unpay_contract_installment_api,
    build_flex_message_ui
)

app = Flask(__name__)
app.register_blueprint(admin_bp, url_prefix='/admin')

TH_TZ = ZoneInfo('Asia/Bangkok')

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', 'YOUR_SECRET')
PROMPTPAY_ID = os.environ.get('PROMPTPAY_ID', '0800000000')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

THAI_MONTHS = [
    "", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"
]

def send_simple_push_notification(user_id, body_text, title="📢 แจ้งเตือนจากระบบ", contract_number="", product_name="", color="#0d6efd"):
    """ ฟังก์ชั่นสำหรับส่ง Push Notification หาผู้ใช้งานฝั่งลูกค้า """
    if not user_id:
        return
    try:
        flex_msg = build_flex_message_ui(title, body_text, contract_number, product_name, color)
        line_bot_api.push_message(user_id, flex_msg)
    except Exception as e:
        print(f"Error sending push notification to {user_id}: {e}")

def get_db():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise ValueError("DATABASE_URL environment variable is missing")
    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    return conn

def init_db():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        print("DATABASE_URL is not set. Skipping DB initialization.")
        return
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS contracts (
            id SERIAL PRIMARY KEY,
            contract_number VARCHAR(100) UNIQUE,
            line_user_id VARCHAR(100),
            customer_name VARCHAR(255),
            id_card VARCHAR(50),
            phone VARCHAR(50),
            product_name VARCHAR(255),
            imei VARCHAR(100),
            serial_number VARCHAR(100),
            color VARCHAR(50),
            capacity VARCHAR(50),
            total_amount NUMERIC(12, 2),
            total_installments INTEGER,
            installment_amount NUMERIC(12, 2),
            due_day INTEGER DEFAULT 5,
            status VARCHAR(50) DEFAULT 'active',
            evidence_file TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS due_day INTEGER DEFAULT 5;')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS evidence_file TEXT;')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS imei VARCHAR(100);')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS serial_number VARCHAR(100);')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS color VARCHAR(50);')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS capacity VARCHAR(50);')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            contract_id INTEGER REFERENCES contracts(id) ON DELETE CASCADE,
            installment_no INTEGER,
            amount NUMERIC(12, 2),
            status VARCHAR(50) DEFAULT 'pending',
            paid_at VARCHAR(100),
            receipt_no VARCHAR(100)
        );
    ''')
    conn.commit()
    cursor.close()
    conn.close()

try:
    init_db()
except Exception as e:
    print(f"Database initialization error: {e}")

def get_installment_due_date(created_at, due_day, installment_no):
    if isinstance(created_at, str):
        try:
            created_at = datetime.datetime.strptime(created_at[:19], '%Y-%m-%d %H:%M:%S')
        except Exception:
            created_at = datetime.datetime.now(TH_TZ)

    start_year = created_at.year
    start_month = created_at.month
    
    # งวดแรก (installment_no = 1) ให้เป็นเดือนถัดไปทันที
    target_month_index = start_month + installment_no
    target_year = start_year + (target_month_index // 12)
    target_month = (target_month_index % 12) + 1

    last_day_of_month = calendar.monthrange(target_year, target_month)[1]
    actual_day = min(due_day, last_day_of_month)

    return datetime.date(target_year, target_month, actual_day)

@app.route("/")
def home():
    return "LINE Installment Bot is Running with PostgreSQL"

@app.route('/qr-code/<promptpay>/<float:amount>')
def generate_qr_code_image(promptpay, amount):
    try:
        payload = qrcode.generate_payload(promptpay, amount)
        img = qrcode_lib.make(payload)
        img_io = io.BytesIO()
        img.save(img_io, 'PNG')
        img_io.seek(0)
        return send_file(img_io, mimetype='image/png')
    except Exception as e:
        return f"QR Generation Error: {str(e)}", 500

@app.route('/api/contracts', methods=['GET', 'POST', 'PUT', 'DELETE'])
def root_api_contracts():
    return process_contracts_api()

@app.route('/api/contracts/<int:contract_id>', methods=['GET', 'PUT', 'POST', 'DELETE'])
def root_api_contract_detail(contract_id):
    if request.method == 'DELETE':
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM contracts WHERE id = %s", (contract_id,))
            contract = cursor.fetchone()
            if contract and contract.get('line_user_id') and contract.get('status') not in ['closed', 'closed_early']:
                cancel_msg = "สัญญาเช่าซื้อของคุณได้ถูก ยกเลิก / ลบออกจากระบบ เรียบร้อยแล้วครับ 🙏"
                send_simple_push_notification(contract['line_user_id'], cancel_msg, title="⚠️ แจ้งเตือนสถานะสัญญา", contract_number=contract.get('contract_number'), product_name=contract.get('product_name'), color="#dc3545")
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"Error sending push on root delete contract: {e}")
            
    return process_contract_detail_api(contract_id)

@app.route('/api/contracts/<int:contract_id>/status', methods=['PUT', 'POST'])
def root_api_update_contract_status(contract_id):
    data = request.get_json() or {}
    new_status = data.get('status')
    
    if not new_status:
        return jsonify({"success": False, "message": "Missing 'status' parameter"}), 400

    try:
        conn = get_db()
    except Exception as e:
        return jsonify({"success": False, "message": f"Database Connection Error: {str(e)}"}), 500

    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE contracts SET status = %s WHERE id = %s", (new_status, contract_id))
        conn.commit()

        if new_status.lower() == 'cancelled':
            cursor.execute("SELECT * FROM contracts WHERE id = %s", (contract_id,))
            contract = cursor.fetchone()
            if contract and contract.get('line_user_id'):
                cancel_msg = "สัญญาเช่าซื้อของคุณได้ถูก ยกเลิก จากทางระบบเรียบร้อยแล้วครับ 🙏"
                send_simple_push_notification(contract['line_user_id'], cancel_msg, title="⚠️ แจ้งเตือนสถานะสัญญา", contract_number=contract.get('contract_number'), product_name=contract.get('product_name'), color="#dc3545")

        return jsonify({"success": True, "message": f"Contract status updated to '{new_status}' successfully", "status": new_status})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/contracts/<int:contract_id>/close_early', methods=['POST', 'PUT'])
def root_api_close_contract_early(contract_id):
    try:
        conn = get_db()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    now_str = datetime.datetime.now(TH_TZ).strftime('%Y-%m-%d %H:%M:%S')
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE id = %s", (contract_id,))
        contract = cursor.fetchone()

        if not contract:
            return jsonify({'error': 'ไม่พบข้อมูลสัญญา'}), 404

        cursor.execute("SELECT * FROM payments WHERE contract_id = %s AND status != 'paid'", (contract_id,))
        unpaid_payments = cursor.fetchall()

        for p in unpaid_payments:
            receipt_no = f"REC-EARLY-{contract_id}-{p['installment_no']}-{datetime.datetime.now(TH_TZ).strftime('%M%S')}"
            cursor.execute("""
                UPDATE payments 
                SET status = 'paid', paid_at = %s, receipt_no = %s
                WHERE id = %s
            """, (now_str, receipt_no, p['id']))

        cursor.execute("UPDATE contracts SET status = 'closed_early' WHERE id = %s", (contract_id,))
        conn.commit()

        if contract.get('line_user_id'):
            thank_msg = "ทางร้านได้ทำการบันทึกยืนยันการปิดยอดสัญญาเรียบร้อยแล้ว ขอบคุณที่ไว้วางใจใช้บริการของเราครับ 🙏✨"
            send_simple_push_notification(contract['line_user_id'], thank_msg, title="🎉 ขอบคุณที่ใช้บริการ", contract_number=contract.get('contract_number'), product_name=contract.get('product_name'), color="#198754")

        return jsonify({'message': 'บันทึกการปิดยอดสัญญาสำเร็จ'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
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
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id

    if text in ['เช็คสัญญา', 'สัญญาของฉัน', 'ผ่อน', 'ดูสัญญา', 'สถานะสัญญา']:
        send_contract_info(event.reply_token, user_id)
    else:
        quick_reply = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="📋 ตรวจสอบสัญญา", text="เช็คสัญญา"))
        ])
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="สวัสดีครับ สามารถกดปุ่ม 'ตรวจสอบสัญญา' ด้านล่างนี้เพื่อดูข้อมูลการผ่อนชำระได้เลยครับ", quick_reply=quick_reply)
        )

def send_contract_info(reply_token, user_id):
    try:
        conn = get_db()
    except Exception as e:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล"))
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="ไม่พบข้อมูลสัญญาที่เชื่อมโยงกับบัญชี LINE นี้ กรุณาติดต่อผู้ดูแลระบบ"))
            return

        cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (contract['id'],))
        payments = cursor.fetchall()

        paid_count = sum(1 for p in payments if p['status'] == 'paid')
        total_installments = contract['total_installments']
        unpaid_payments = [p for p in payments if p['status'] != 'paid']

        status_text = "ปกติ"
        header_color = "#0d6efd"

        if contract['status'] == 'closed' or contract['status'] == 'closed_early':
            status_text = "ปิดยอดชำระครบแล้ว 🎉"
            header_color = "#198754"
        elif contract['status'] == 'cancelled':
            status_text = "ยกเลิกสัญญาแล้ว ❌"
            header_color = "#6c757d"
        elif contract['status'] == 'reclaim':
            status_text = "เรียกคืนเครื่อง (ค้างเกินกำหนด)"
            header_color = "#dc3545"
        elif contract['status'] == 'overdue_2':
            status_text = "ค้างชำระ 2 งวด ⚠️"
            header_color = "#ffc107"
        elif contract['status'] == 'overdue_1':
            status_text = "ค้างชำระ 1 งวด ⚠️"
            header_color = "#ffc107"

        # ปรับปรุง Flex Message ฝั่งลูกค้า: ตัดปุ่มดูเอกสารสัญญาออกตามโจทย์
        flex_contents = {
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": header_color,
                "paddingAll": "xl",
                "contents": [
                    {
                        "type": "text",
                        "text": f"📱 สัญญาผ่อนสินค้า",
                        "weight": "bold",
                        "color": "#ffffff",
                        "size": "lg"
                    },
                    {
                        "type": "text",
                        "text": f"เลขที่: {contract['contract_number']}",
                        "color": "#ffffff",
                        "size": "xs",
                        "margin": "xs"
                    }
                ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": contract['product_name'],
                        "weight": "bold",
                        "size": "xl",
                        "color": "#111111"
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "backgroundColor": "#f8f9fa",
                        "cornerRadius": "md",
                        "paddingAll": "md",
                        "spacing": "sm",
                        "contents": [
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": "ผู้เช่าซื้อ:", "size": "sm", "color": "#aaaaaa", "flex": 3},
                                    {"type": "text", "text": contract['customer_name'], "size": "sm", "color": "#111111", "flex": 5, "align": "end", "weight": "bold"}
                                ]
                            },
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": "สถานะสัญญา:", "size": "sm", "color": "#aaaaaa", "flex": 3},
                                    {"type": "text", "text": status_text, "size": "sm", "color": header_color, "flex": 5, "align": "end", "weight": "bold"}
                                ]
                            },
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": "ความคืบหน้า:", "size": "sm", "color": "#aaaaaa", "flex": 3},
                                    {"type": "text", "text": f"ชำระแล้ว {paid_count}/{total_installments} งวด", "size": "sm", "color": "#111111", "flex": 5, "align": "end"}
                                ]
                            }
                        ]
                    }
                ]
            }
        }

        if unpaid_payments and contract['status'] not in ['closed', 'closed_early', 'cancelled']:
            next_payment = unpaid_payments[0]
            due_date = get_installment_due_date(contract.get('created_at'), contract.get('due_day', 5), next_payment['installment_no'])
            due_date_str = due_date.strftime('%d/%m/%Y')

            unpaid_box = {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#fff3cd",
                "cornerRadius": "md",
                "paddingAll": "md",
                "spacing": "xs",
                "contents": [
                    {
                        "type": "text",
                        "text": f"📌 งวดถัดไป (งวดที่ {next_payment['installment_no']})",
                        "weight": "bold",
                        "size": "sm",
                        "color": "#664d03"
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "margin": "xs",
                        "contents": [
                            {"type": "text", "text": "ยอดที่ต้องชำระ:", "size": "xs", "color": "#664d03"},
                            {"type": "text", "text": f"฿{float(next_payment['amount']):,.2f}", "size": "sm", "color": "#842029", "weight": "bold", "align": "end"}
                        ]
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "contents": [
                            {"type": "text", "text": "กำหนดชำระภายใน:", "size": "xs", "color": "#664d03"},
                            {"type": "text", "text": due_date_str, "size": "xs", "color": "#664d03", "align": "end", "weight": "bold"}
                        ]
                    }
                ]
            }
            flex_contents["body"]["contents"].append(unpaid_box)

        line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="ข้อมูลสัญญาผ่อนชำระ", contents=flex_contents))

    except Exception as e:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"เกิดข้อผิดพลาดในการดึงข้อมูล: {str(e)}"))
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))