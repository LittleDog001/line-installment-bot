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

from admin_routes import admin_bp

app = Flask(__name__)
app.register_blueprint(admin_bp, url_prefix='/admin')

TH_TZ = ZoneInfo('Asia/Bangkok')

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', 'YOUR_SECRET')
PROMPTPAY_ID = os.environ.get('PROMPTPAY_ID', '0800000000')
DATABASE_URL = os.environ.get('DATABASE_URL')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
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

# API ดึงข้อมูลและสร้างสัญญา รองรับทั้ง GET, POST, PUT, DELETE และ Path ทั้งแบบมี/ไม่มี /admin
@app.route('/api/contracts', methods=['GET', 'POST', 'PUT', 'DELETE'])
@app.route('/admin/api/contracts', methods=['GET', 'POST', 'PUT', 'DELETE'])
def global_get_contracts_api():
    if not DATABASE_URL:
        return jsonify({'error': 'DATABASE_URL is not set'}), 500

    conn = get_db()
    cursor = conn.cursor()
    try:
        if request.method == 'POST':
            data = request.get_json() if request.is_json else request.form
            line_user_id = (data.get('line_user_id') or '').strip()
            customer_name = (data.get('customer_name') or '').strip()
            id_card = (data.get('id_card') or '').strip()
            phone = (data.get('phone') or '').strip()
            product_name = (data.get('product_name') or '').strip()
            
            total_amount = float(data.get('total_amount', 0))
            total_installments = int(data.get('total_installments', 0))

            if total_installments <= 0 or total_amount <= 0:
                return jsonify({'error': 'กรุณากรอกข้อมูลยอดเงินและจำนวนงวดให้ถูกต้อง'}), 400

            installment_amount = total_amount / total_installments
            contract_number = f"CTR-{datetime.datetime.now(TH_TZ).strftime('%Y%m%d%H%M%S')}"

            cursor.execute("""
                INSERT INTO contracts (
                    contract_number, line_user_id, customer_name, id_card, phone, 
                    product_name, total_amount, total_installments, installment_amount, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
                RETURNING id
            """, (
                contract_number, line_user_id, customer_name, id_card, phone, 
                product_name, total_amount, total_installments, installment_amount
            ))
            
            contract_row = cursor.fetchone()
            contract_id = contract_row['id']

            for i in range(1, total_installments + 1):
                cursor.execute("""
                    INSERT INTO payments (contract_id, installment_no, amount, status)
                    VALUES (%s, %s, %s, 'pending')
                """, (contract_id, i, installment_amount))

            conn.commit()
            return jsonify({'message': 'บันทึกสัญญาสำเร็จ', 'contract_id': contract_id}), 201

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
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

# API จัดการสัญญาเป็นรายรายการ (ดึงข้อมูล/แก้ไข/ลบ)
@app.route('/api/contracts/<int:contract_id>', methods=['GET', 'PUT', 'POST', 'DELETE'])
@app.route('/admin/api/contracts/<int:contract_id>', methods=['GET', 'PUT', 'POST', 'DELETE'])
def global_get_contract_detail_api(contract_id):
    if not DATABASE_URL:
        return jsonify({'error': 'DATABASE_URL is not set'}), 500

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE id = %s", (contract_id,))
        contract = cursor.fetchone()

        if not contract:
            return jsonify({'error': 'ไม่พบข้อมูลสัญญา'}), 404

        if request.method in ['PUT', 'POST']:
            data = request.get_json() if request.is_json else request.form
            line_user_id = (data.get('line_user_id') or '').strip()
            customer_name = (data.get('customer_name') or '').strip()
            id_card = (data.get('id_card') or '').strip()
            phone = (data.get('phone') or '').strip()
            product_name = (data.get('product_name') or '').strip()

            cursor.execute("""
                UPDATE contracts 
                SET line_user_id = %s, customer_name = %s, id_card = %s, phone = %s, product_name = %s
                WHERE id = %s
            """, (line_user_id, customer_name, id_card, phone, product_name, contract_id))

            conn.commit()
            return jsonify({'message': 'แก้ไขสัญญาสำเร็จ', 'contract_id': contract_id})

        elif request.method == 'DELETE':
            cursor.execute("DELETE FROM contracts WHERE id = %s", (contract_id,))
            conn.commit()
            return jsonify({'message': 'ลบสัญญาสำเร็จ', 'contract_id': contract_id})

        c_dict = dict(contract)
        cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (contract_id,))
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

        return jsonify(c_dict)
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
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
    if not DATABASE_URL:
        return

    conn = get_db()
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
    if not DATABASE_URL:
        return

    conn = get_db()
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
    if not DATABASE_URL:
        return

    conn = get_db()
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
    if not DATABASE_URL:
        return

    now_str = datetime.datetime.now(TH_TZ).strftime('%Y-%m-%d %H:%M:%S')
    
    conn = get_db()
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

@app.route('/bill/<int:payment_id>')
def print_bill_main(payment_id):
    if not DATABASE_URL:
        return "DATABASE_URL is not set", 500

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT p.*, c.customer_name, c.id_card, c.phone, c.product_name, c.installment_amount
            FROM payments p
            JOIN contracts c ON p.contract_id = c.id
            WHERE p.id = %s
        """, (payment_id,))
        payment = cursor.fetchone()

        if not payment:
            return "ไม่พบข้อมูลบิลนี้", 404

        paid_at = payment['paid_at'] if payment['paid_at'] else datetime.datetime.now(TH_TZ).strftime('%Y-%m-%d %H:%M:%S')

        return render_template('bill.html', payment=payment, paid_at=paid_at)
    finally:
        cursor.close()
        conn.close()

@app.route('/contract/doc/<contract_number>')
def print_contract_doc(contract_number):
    if not DATABASE_URL:
        return "DATABASE_URL is not set", 500

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE contract_number = %s", (contract_number,))
        contract = cursor.fetchone()

        if not contract:
            return "ไม่พบข้อมูลสัญญา", 404

        return render_template('contract_document.html', contract=contract)
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)