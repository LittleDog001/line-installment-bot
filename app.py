import os
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from zoneinfo import ZoneInfo
from flask import Flask, request, abort, render_template, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage, QuickReply, QuickReplyButton, MessageAction
)
from promptpay import qrcode

from admin_routes import (
    admin_bp, process_contracts_api, process_contract_detail_api, 
    process_payments_by_contract_api, pay_contract_installment_api, unpay_contract_installment_api
)

app = Flask(__name__)
app.register_blueprint(admin_bp, url_prefix='/admin')

TH_TZ = ZoneInfo('Asia/Bangkok')

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', 'YOUR_SECRET')
PROMPTPAY_ID = os.environ.get('PROMPTPAY_ID', '0800000000')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

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
            total_amount NUMERIC(12, 2),
            total_installments INTEGER,
            installment_amount NUMERIC(12, 2),
            status VARCHAR(50) DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
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

@app.route("/")
def home():
    return "LINE Installment Bot is Running with PostgreSQL"

# --- Global API Routes สำหรับ JavaScript หน้าบ้านเรียกใช้งานตรงๆ (/api/...) ---
@app.route('/api/contracts', methods=['GET', 'POST', 'PUT', 'DELETE'])
def root_api_contracts():
    return process_contracts_api()

@app.route('/api/contracts/<int:contract_id>', methods=['GET', 'PUT', 'POST', 'DELETE'])
def root_api_contract_detail(contract_id):
    return process_contract_detail_api(contract_id)

@app.route('/api/payments/<contract_identifier>', methods=['GET'])
def root_api_payments(contract_identifier):
    return process_payments_by_contract_api(contract_identifier)

@app.route('/api/contracts/<int:contract_id>/pay', methods=['POST', 'PUT'])
def root_api_pay(contract_id):
    return pay_contract_installment_api(contract_id)

@app.route('/api/contracts/<int:contract_id>/unpay', methods=['POST', 'PUT'])
def root_api_unpay(contract_id):
    return unpay_contract_installment_api(contract_id)

# --- Routes สำหรับแสดงผลบิลและใบสัญญา ---
@app.route('/bill/<int:payment_id>')
@app.route('/admin/bill/<int:payment_id>')
def print_bill_main(payment_id):
    try:
        conn = get_db()
    except Exception as e:
        return f"Database Connection Error: {str(e)}", 500

    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT p.*, c.customer_name, c.id_card, c.phone, c.product_name, c.installment_amount, c.contract_number, c.total_installments
            FROM payments p
            JOIN contracts c ON p.contract_id = c.id
            WHERE p.id = %s
        """, (payment_id,))
        payment_row = cursor.fetchone()

        if not payment_row:
            return "ไม่พบข้อมูลบิลนี้", 404

        payment = dict(payment_row)
        payment['amount'] = float(payment['amount']) if payment['amount'] is not None else 0.0
        payment['installment_amount'] = float(payment['installment_amount']) if payment['installment_amount'] is not None else 0.0

        paid_at = payment['paid_at'] if payment['paid_at'] else datetime.datetime.now(TH_TZ).strftime('%Y-%m-%d %H:%M:%S')

        return render_template('bill.html', p=payment, payment=payment, paid_at=paid_at)
    except Exception as e:
        return f"Error loading bill: {str(e)}", 500
    finally:
        cursor.close()
        conn.close()

@app.route('/contract/doc/<contract_identifier>')
@app.route('/admin/contract/doc/<contract_identifier>')
def print_contract_doc(contract_identifier):
    try:
        conn = get_db()
    except Exception as e:
        return f"Database Connection Error: {str(e)}", 500

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE contract_number = %s OR id::text = %s", (contract_identifier, contract_identifier))
        contract_row = cursor.fetchone()

        if not contract_row:
            return "ไม่พบข้อมูลสัญญา", 404

        contract = dict(contract_row)
        contract['total_amount'] = float(contract['total_amount']) if contract['total_amount'] is not None else 0.0
        contract['installment_amount'] = float(contract['installment_amount']) if contract['installment_amount'] is not None else 0.0
        
        # เพิ่ม monthly_amount และส่งตัวแปร c ให้ตรงตาม contract_document.html
        contract['monthly_amount'] = contract['installment_amount']
        if contract.get('created_at'):
            contract['created_at'] = str(contract['created_at'])

        return render_template('contract_document.html', c=contract, contract=contract)
    except Exception as e:
        return f"Error loading contract doc: {str(e)}", 500
    finally:
        cursor.close()
        conn.close()

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text.strip()
    user_id = event.source.user_id

    if user_text in ["เช็คยอด", "เช็คค่างวด", "เมนู", "สัญญา"]:
        send_contract_status(user_id, event.reply_token)
    elif user_text.startswith("ชำระงวดที่"):
        try:
            installment_no = int(user_text.replace("ชำระงวดที่", "").strip())
            send_payment_qr(user_id, installment_no, event.reply_token)
        except ValueError:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="รูปแบบคำสั่งไม่ถูกต้อง"))
    elif user_text in ["ปิดยอดก่อนกำหนด", "ปิดยอด"]:
        send_early_close_qr(user_id, event.reply_token)
    elif user_text == "ยืนยันปิดยอดชำระแล้ว":
        process_user_close_early(user_id, event.reply_token)
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="พิมพ์ 'เช็คยอด' เพื่อดูรายละเอียด หรือกดเมนูด้านล่างครับ",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="เช็คยอดค่างวด", text="เช็คยอด")),
                    QuickReplyButton(action=MessageAction(label="ปิดยอดก่อนกำหนด (ลด 15%)", text="ปิดยอดก่อนกำหนด"))
                ])
            )
        )

def send_contract_status(user_id, reply_token):
    try:
        conn = get_db()
    except Exception:
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status = 'active' ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="ไม่พบข้อมูลสัญญาผ่อนชำระของคุณในระบบ"))
            return

        cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (contract['id'],))
        payments = cursor.fetchall()

        paid_count = sum(1 for p in payments if p['status'] == 'paid')
        remaining_count = contract['total_installments'] - paid_count
        remaining_balance = float(remaining_count * contract['installment_amount'])
        discounted_close_amount = remaining_balance * 0.85 if remaining_balance > 0 else 0

        footer_contents = []
        if remaining_count > 0:
            footer_contents.append({
                "type": "button",
                "style": "primary",
                "color": "#1DB446",
                "action": {
                    "type": "message",
                    "label": f"จ่ายงวดที่ {paid_count + 1} ({float(contract['installment_amount']):,.2f} บ.)",
                    "text": f"ชำระงวดที่ {paid_count + 1}"
                }
            })
            footer_contents.append({
                "type": "button",
                "style": "secondary",
                "color": "#e67e22",
                "action": {
                    "type": "message",
                    "label": "ปิดยอดทั้งหมด (ส่วนลด 15%)",
                    "text": "ปิดยอดก่อนกำหนด"
                }
            })
        else:
            footer_contents.append({
                "type": "text",
                "text": "ชำระครบถ้วนเรียบร้อยแล้ว",
                "align": "center",
                "color": "#27ae60",
                "weight": "bold"
            })

        flex_contents = {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "รายการผ่อนชำระของคุณ", "weight": "bold", "size": "xl", "color": "#1DB446"},
                    {"type": "text", "text": f"สินค้า: {contract['product_name']}", "size": "md", "margin": "md", "weight": "bold"},
                    {"type": "separator", "margin": "md"},
                    {"type": "box", "layout": "vertical", "margin": "md", "spacing": "sm", "contents": [
                        {"type": "text", "text": f"ผู้กู้/ผู้ผ่อน: {contract['customer_name']}", "size": "sm"},
                        {"type": "text", "text": f"เลขบัตรประชาชน: {contract['id_card'] if contract['id_card'] else '-'}", "size": "sm"},
                        {"type": "text", "text": f"งวดทั้งหมด: {contract['total_installments']} งวด", "size": "sm"},
                        {"type": "text", "text": f"ชำระแล้ว: {paid_count} งวด", "size": "sm", "color": "#27ae60"},
                        {"type": "text", "text": f"คงเหลือ: {remaining_count} งวด ({remaining_balance:,.2f} บาท)", "size": "sm", "color": "#e74c3c"},
                        {"type": "text", "text": f"🔥 ยอดปิดบัญชีทันที (ลด 15%): {discounted_close_amount:,.2f} บาท", "size": "sm", "weight": "bold", "color": "#d35400"}
                    ]}
                ]
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": footer_contents
            }
        }

        line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="สถานะสัญญาชำระเงิน", contents=flex_contents))
    finally:
        cursor.close()
        conn.close()

def send_payment_qr(user_id, installment_no, reply_token):
    try:
        conn = get_db()
    except Exception:
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status = 'active' ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="ไม่พบข้อมูลสัญญา"))
            return

        amount = float(contract['installment_amount'])
        qr_payload = qrcode.generate_payload(PROMPTPAY_ID, amount)
        qr_image_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={qr_payload}"

        line_bot_api.reply_message(
            reply_token,
            [
                TextSendMessage(text=f"สแกนเพื่อชำระค่างวดที่ {installment_no}\nยอดชำระ: {amount:,.2f} บาท\nพร้อมเพย์: {PROMPTPAY_ID}"),
                FlexSendMessage(
                    alt_text="QR Code ชำระเงิน",
                    contents={
                        "type": "bubble",
                        "body": {
                            "type": "box",
                            "layout": "vertical",
                            "contents": [
                                {"type": "text", "text": f"QR Code งวดที่ {installment_no}", "weight": "bold", "align": "center"},
                                {"type": "image", "url": qr_image_url, "size": "5l", "aspectRatio": "1:1"}
                            ]
                        }
                    }
                )
            ]
        )
    finally:
        cursor.close()
        conn.close()

def send_early_close_qr(user_id, reply_token):
    try:
        conn = get_db()
    except Exception:
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status = 'active' ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="ไม่พบข้อมูลสัญญาที่กำลังผ่อนชำระ"))
            return

        cursor.execute("SELECT * FROM payments WHERE contract_id = %s AND status = 'paid'", (contract['id'],))
        paid_payments = cursor.fetchall()

        paid_count = len(paid_payments)
        remaining_count = contract['total_installments'] - paid_count

        if remaining_count <= 0:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="คุณได้ชำระค่างวดครบถ้วนแล้ว ไม่มียอดคงเหลือ"))
            return

        remaining_balance = float(remaining_count * contract['installment_amount'])
        discount_amount = remaining_balance * 0.15
        final_pay_amount = remaining_balance - discount_amount

        qr_payload = qrcode.generate_payload(PROMPTPAY_ID, final_pay_amount)
        qr_image_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={qr_payload}"

        msg = (
            f"🎉 ข้อเสนอพิเศษปิดยอดก่อนกำหนด\n"
            f"• ยอดคงเหลือคงค้าง ({remaining_count} งวด): {remaining_balance:,.2f} บาท\n"
            f"• ส่วนลดพิเศษ (15%): -{discount_amount:,.2f} บาท\n"
            f"-------------------------------\n"
            f"💰 ยอดสุทธิที่ต้องชำระปิดบัญชี: {final_pay_amount:,.2f} บาท"
        )

        line_bot_api.reply_message(
            reply_token,
            [
                TextSendMessage(text=msg),
                FlexSendMessage(
                    alt_text="QR Code ปิดยอดก่อนกำหนด",
                    contents={
                        "type": "bubble",
                        "body": {
                            "type": "box",
                            "layout": "vertical",
                            "contents": [
                                {"type": "text", "text": "สแกนชำระปิดยอด (ลด 15%)", "weight": "bold", "align": "center", "color": "#e67e22"},
                                {"type": "image", "url": qr_image_url, "size": "5l", "aspectRatio": "1:1"}
                            ]
                        },
                        "footer": {
                            "type": "box",
                            "layout": "vertical",
                            "contents": [
                                {
                                    "type": "button",
                                    "style": "primary",
                                    "color": "#e67e22",
                                    "action": {
                                        "type": "message",
                                        "label": "ยืนยันปิดยอด (หลังชำระเงิน)",
                                        "text": "ยืนยันปิดยอดชำระแล้ว"
                                    }
                                }
                            ]
                        }
                    }
                )
            ]
        )
    finally:
        cursor.close()
        conn.close()

def process_user_close_early(user_id, reply_token):
    try:
        conn = get_db()
    except Exception:
        return

    now_str = datetime.datetime.now(TH_TZ).strftime('%Y-%m-%d %H:%M:%S')
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status = 'active' ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="ไม่พบข้อมูลสัญญาที่เปิดอยู่"))
            return

        contract_id = contract['id']
        cursor.execute("SELECT * FROM payments WHERE contract_id = %s AND status != 'paid'", (contract_id,))
        unpaid_payments = cursor.fetchall()

        if not unpaid_payments:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="สัญญาของคุณได้รับการปิดยอดเรียบร้อยแล้ว"))
            return

        for p in unpaid_payments:
            receipt_no = f"REC-EARLY-{contract_id}-{p['installment_no']}-{datetime.datetime.now(TH_TZ).strftime('%M%S')}"
            cursor.execute("""
                UPDATE payments 
                SET status = 'paid', paid_at = %s, receipt_no = %s
                WHERE id = %s
            """, (now_str, receipt_no, p['id']))

        cursor.execute("UPDATE contracts SET status = 'closed_early' WHERE id = %s", (contract_id,))
        conn.commit()

        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=f"✅ ระบบได้รับข้อมูลการปิดยอดก่อนกำหนดเรียบร้อยแล้วเมื่อ {now_str}\nสัญญาเลขที่ {contract['contract_number']} ได้ทำการปิดบัญชี (ส่วนลด 15%) สมบูรณ์แล้ว ขอบคุณครับ!")
        )
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)