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

THAI_MONTHS = [
    "", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"
]

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
    
    # แก้ไข: งวดแรก (installment_no = 1) ให้เป็นเดือนถัดไปทันที
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
                cancel_msg = (
                    f"⚠️ แจ้งเตือนสถานะสัญญาของคุณ\n"
                    f"-------------------------------\n"
                    f"📦 สินค้า: {contract['product_name']}\n"
                    f"เลขที่สัญญา: {contract['contract_number']}\n\n"
                    f"สัญญาเช่าซื้อของคุณได้ถูก **ยกเลิก / ลบออกจากระบบจากร้าน** เรียบร้อยแล้วครับ"
                )
                send_simple_push_notification(contract['line_user_id'], cancel_msg)
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
                cancel_msg = (
                    f"⚠️ แจ้งเตือนสถานะสัญญาของคุณ\n"
                    f"-------------------------------\n"
                    f"📦 สินค้า: {contract['product_name']}\n"
                    f"เลขที่สัญญา: {contract['contract_number']}\n\n"
                    f"สัญญาเช่าซื้อของคุณได้ถูก **ยกเลิก** จากทางระบบเรียบร้อยแล้วครับ 🙏"
                )
                send_simple_push_notification(contract['line_user_id'], cancel_msg)

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
            thank_msg = (
                f"🎉 ขอขอบพระคุณเป็นอย่างยิ่งครับ!\n"
                f"-------------------------------\n"
                f"📦 สินค้า: {contract['product_name']}\n"
                f"เลขที่สัญญา: {contract['contract_number']}\n\n"
                f"ทางร้านได้ทำการบันทึกยืนยันการปิดยอดสัญญาเรียบร้อยแล้ว ขอบคุณที่ไว้วางใจใช้บริการของเราครับ 🙏✨"
            )
            send_simple_push_notification(contract['line_user_id'], thank_msg)

        return jsonify({'message': 'บันทึกการปิดยอดสัญญาสำเร็จ'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/payments/<contract_identifier>', methods=['GET'])
def root_api_payments(contract_identifier):
    return process_payments_by_contract_api(contract_identifier)

@app.route('/api/contracts/<int:contract_id>/pay', methods=['POST', 'PUT'])
def root_api_pay(contract_id):
    return pay_contract_installment_api(contract_id)

@app.route('/api/contracts/<int:contract_id>/unpay', methods=['POST', 'PUT'])
def root_api_unpay(contract_id):
    return unpay_contract_installment_api(contract_id)

@app.route('/api/cron/check-due-payments', methods=['GET', 'POST'])
def trigger_due_notifications():
    result = check_due_notifications()
    return jsonify(result)

def check_due_notifications():
    try:
        conn = get_db()
    except Exception as e:
        return {"success": False, "error": str(e)}

    today = datetime.datetime.now(TH_TZ).date()
    notified_count = 0

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE status IN ('active', 'overdue_1', 'overdue_2')")
        contracts = cursor.fetchall()

        for c in contracts:
            line_user_id = c.get('line_user_id')
            if not line_user_id:
                continue

            due_day = c.get('due_day') or 5
            created_at = c.get('created_at')

            cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (c['id'],))
            payments = cursor.fetchall()

            unpaid_payments = [p for p in payments if p['status'] != 'paid']
            if not unpaid_payments:
                continue

            current_p = unpaid_payments[0]
            inst_no = current_p['installment_no']
            due_date = get_installment_due_date(created_at, due_day, inst_no)
            days_overdue = (today - due_date).days
            days_until_due = (due_date - today).days

            year_th = due_date.year + 543
            month_th = THAI_MONTHS[due_date.month]
            day_th = due_date.day

            base_amount = float(c['installment_amount'])

            if 0 <= days_until_due <= 3:
                msg = (
                    f"⏰ แจ้งเตือนค่างวดผ่อนชำระ (ใกล้ถึงวันกำหนดชำระ)\n"
                    f"-------------------------------\n"
                    f"📦 สินค้า: {c['product_name']}\n"
                    f"🔢 งวดที่ต้องชำระ: งวดที่ {inst_no}/{c['total_installments']}\n"
                    f"📅 กำหนดชำระ: วันที่ {day_th} เดือน {month_th} พ.ศ. {year_th}\n"
                    f"💰 ยอดชำระ: {base_amount:,.2f} บาท\n\n"
                    f"กรุณาชำระเงินตามกำหนด ขอบคุณครับ"
                )
                send_line_push_notification(line_user_id, msg, inst_no, base_amount)
                notified_count += 1

            elif 1 <= days_overdue <= 2:
                msg = (
                    f"🔔 แจ้งเตือนระยะที่ 1: เลยกำหนดชำระ (อนุโลม 2 วัน)\n"
                    f"-------------------------------\n"
                    f"📦 สินค้า: {c['product_name']}\n"
                    f"🔢 งวดที่ต้องชำระ: งวดที่ {inst_no}/{c['total_installments']}\n"
                    f"📅 กำหนดชำระเดิม: วันที่ {day_th} เดือน {month_th} พ.ศ. {year_th}\n"
                    f"💰 ยอดชำระ: {base_amount:,.2f} บาท (ไม่มีค่าปรับ)\n\n"
                    f"ขณะนี้เลยกำหนดชำระมาแล้ว {days_overdue} วัน กรุณาดำเนินการชำระเพื่อป้องกันการเกิดค่าปรับครับ"
                )
                send_line_push_notification(line_user_id, msg, inst_no, base_amount)
                notified_count += 1

            elif 3 <= days_overdue <= 7:
                fine = days_overdue * 100
                total_with_fine = base_amount + fine
                msg = (
                    f"⚠️ แจ้งเตือนระยะที่ 2: เลยกำหนดชำระ {days_overdue} วัน\n"
                    f"-------------------------------\n"
                    f"📦 สินค้า: {c['product_name']}\n"
                    f"🔢 งวดที่ต้องชำระ: งวดที่ {inst_no}/{c['total_installments']}\n"
                    f"📅 กำหนดชำระเดิม: วันที่ {day_th} เดือน {month_th} พ.ศ. {year_th}\n"
                    f"💰 ค่างวด: {base_amount:,.2f} บาท\n"
                    f"💸 ค่าปรับ (100 บาท/วัน): {fine:,.2f} บาท\n"
                    f"💵 ยอดรวมที่ต้องชำระ: {total_with_fine:,.2f} บาท\n\n"
                    f"ขณะนี้อยู่ระหว่างค้างชำระ กรุณาชำระเงินโดยเร็วครับ"
                )
                send_line_push_notification(line_user_id, msg, inst_no, total_with_fine)
                notified_count += 1

            elif days_overdue > 7:
                total_installments = c['total_installments']
                
                cursor.execute("""
                    SELECT * FROM payments 
                    WHERE contract_id = %s AND installment_no = %s AND status = 'overdue_shifted'
                """, (c['id'], inst_no))
                already_shifted = cursor.fetchone()

                if not already_shifted:
                    fine = days_overdue * 100
                    cursor.execute("""
                        UPDATE payments 
                        SET status = 'overdue_shifted', amount = %s 
                        WHERE id = %s
                    """, (base_amount + fine, current_p['id']))

                    cursor.execute("""
                        INSERT INTO payments (contract_id, installment_no, amount, status)
                        VALUES (%s, %s, %s, 'pending')
                    """, (c['id'], total_installments + 1, base_amount + fine))

                    cursor.execute("""
                        UPDATE contracts 
                        SET total_installments = total_installments + 1 
                        WHERE id = %s
                    """, (c['id'],))
                    conn.commit()

                cursor.execute("SELECT amount FROM payments WHERE id = %s", (current_p['id'],))
                updated_p = cursor.fetchone()
                fine_amount = float(updated_p['amount']) - base_amount
                total_amount_shifted = float(updated_p['amount'])

                msg = (
                    f"🚨 แจ้งเตือนระยะที่ 3: เกินกำหนดชำระเกิน 7 วัน (สถานะ: ค้างชำระ)\n"
                    f"-------------------------------\n"
                    f"📦 สินค้า: {c['product_name']}\n"
                    f"🔢 งวดที่ค้างชำระ: งวดที่ {inst_no}\n"
                    f"📅 กำหนดชำระเดิม: วันที่ {day_th} เดือน {month_th} พ.ศ. {year_th}\n"
                    f"💸 ค่าปรับบวกเพิ่ม: {fine_amount:,.2f} บาท\n\n"
                    f"ขณะนี้เลยกำหนดชำระแล้ว ระบบได้ข้ามเดือนที่ค้างชำระไปยังงวดถัดไป และนำงวดที่ค้างชำระพร้อมค่าปรับรวม {total_amount_shifted:,.2f} บาท ไปยกยอดเป็นงวดสุดท้ายเรียบร้อยแล้วครับ"
                )
                send_simple_push_notification(line_user_id, msg)
                notified_count += 1

        return {"success": True, "notified_count": notified_count}
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        cursor.close()
        conn.close()

def send_simple_push_notification(user_id, text_msg):
    try:
        line_bot_api.push_message(user_id, TextSendMessage(text=text_msg))
    except Exception as e:
        print(f"Error sending Simple Push Message to {user_id}: {e}")

def send_line_push_notification(user_id, text_msg, installment_no, amount):
    try:
        quick_reply = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label=f"ชำระงวดที่ {installment_no}", text=f"ชำระงวดที่ {installment_no}")),
            QuickReplyButton(action=MessageAction(label="เช็คยอดค่างวด", text="เช็คยอด"))
        ])
        line_bot_api.push_message(user_id, TextSendMessage(text=text_msg, quick_reply=quick_reply))
    except Exception as e:
        print(f"Error sending Push Message to {user_id}: {e}")

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

    if user_text in ["เช็คยอด", "เช็คค่างวด", "เมนู"]:
        send_contract_status(user_id, event.reply_token)
    elif user_text == "สัญญา":
        send_contract_only(user_id, event.reply_token)
    elif user_text.startswith("ชำระงวดที่"):
        try:
            installment_no = int(user_text.replace("ชำระงวดที่", "").strip())
            send_payment_qr(user_id, installment_no, event.reply_token)
        except ValueError:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="รูปแบบคำสั่งไม่ถูกต้อง"))
    elif user_text in ["ปิดยอดก่อนกำหนด", "ปิดยอด"]:
        send_early_close_qr(user_id, event.reply_token)
    else:
        search_contract_and_reply(user_id, user_text, event.reply_token)

def search_contract_and_reply(user_id, search_term, reply_token):
    try:
        conn = get_db()
    except Exception:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล"))
        return

    cursor = conn.cursor()
    try:
        clean_term = search_term.replace('-', '').replace(' ', '')

        cursor.execute("""
            SELECT * FROM contracts 
            WHERE REPLACE(phone, '-', '') = %s 
               OR REPLACE(id_card, '-', '') = %s 
               OR customer_name LIKE %s 
               OR contract_number = %s
            ORDER BY id DESC LIMIT 1
        """, (clean_term, clean_term, f"%{search_term}%", search_term))

        contract = cursor.fetchone()

        if contract:
            if not contract['line_user_id']:
                cursor.execute("UPDATE contracts SET line_user_id = %s WHERE id = %s", (user_id, contract['id']))
                conn.commit()

            render_flex_contract(contract, reply_token, show_buttons=True)
        else:
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(
                    text="ไม่พบข้อมูลสัญญาจากคำค้นหาของคุณ กรุณาพิมพ์ เบอร์โทรศัพท์, เลขบัตรประชาชน หรือ ชื่อ-นามสกุล ที่ใช้ลงทะเบียนสัญญาให้ถูกต้องครับ",
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=MessageAction(label="เช็คยอดค่างวด", text="เช็คยอด")),
                        QuickReplyButton(action=MessageAction(label="ปิดยอดก่อนกำหนด", text="ปิดยอดก่อนกำหนด"))
                    ])
                )
            )
    finally:
        cursor.close()
        conn.close()

def send_contract_status(user_id, reply_token):
    try:
        conn = get_db()
    except Exception:
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status IN ('active', 'closed', 'closed_early', 'cancelled', 'reclaim') ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            line_bot_api.reply_message(
                reply_token, 
                TextSendMessage(text="ไม่พบข้อมูลสัญญาผ่อนชำระที่ผูกกับ LINE นี้\n\n💡 ท่านสามารถพิมพ์ 'เบอร์โทรศัพท์', 'เลขบัตรประชาชน' หรือ 'ชื่อ-นามสกุล' เพื่อค้นหาสัญญาของคุณได้เลยครับ")
            )
            return

        render_flex_contract(contract, reply_token, show_buttons=True)
    finally:
        cursor.close()
        conn.close()

def send_contract_only(user_id, reply_token):
    try:
        conn = get_db()
    except Exception:
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status IN ('active', 'closed', 'closed_early', 'cancelled', 'reclaim') ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            line_bot_api.reply_message(
                reply_token, 
                TextSendMessage(text="ไม่พบข้อมูลสัญญาผ่อนชำระที่ผูกกับ LINE นี้")
            )
            return

        render_flex_contract(contract, reply_token, show_buttons=False)
    finally:
        cursor.close()
        conn.close()

def render_flex_contract(contract, reply_token, show_buttons=True):
    try:
        conn = get_db()
    except Exception:
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (contract['id'],))
        payments = cursor.fetchall()

        paid_count = sum(1 for p in payments if p['status'] == 'paid')
        
        created_at_str = str(contract['created_at'])[:10] if contract.get('created_at') else '-'

        unpaid_list = [p for p in payments if p['status'] != 'paid']
        next_p = unpaid_list[0] if unpaid_list else None

        if next_p:
            next_inst_no = next_p['installment_no']
            due_date = get_installment_due_date(contract.get('created_at'), contract.get('due_day', 5), next_inst_no)
            year_th = due_date.year + 543
            month_th = THAI_MONTHS[due_date.month]
            day_th = due_date.day
            due_date_str = f"งวดที่ {next_inst_no} : วันที่ {day_th} {month_th} {year_th}"
            pay_amount = float(next_p['amount']) if next_p.get('amount') else float(contract['installment_amount'])
        else:
            next_inst_no = paid_count
            due_date_str = "ชำระครบถ้วนแล้ว"
            pay_amount = float(contract['installment_amount'])

        footer_contents = []
        is_closed = contract['status'] in ['closed', 'closed_early'] or paid_count >= contract['total_installments']
        is_cancelled = contract['status'] == 'cancelled'
        is_reclaim = contract['status'] == 'reclaim'

        if not show_buttons:
            footer_contents.append({
                "type": "button",
                "style": "link",
                "action": {
                    "type": "uri",
                    "label": "📄 เปิดดูเอกสารสัญญาฉบับเต็ม",
                    "uri": f"{request.host_url.rstrip('/')}/contract/doc/{contract['id']}"
                }
            })
        else:
            if is_cancelled:
                footer_contents.append({
                    "type": "text",
                    "text": "❌ สัญญานี้ถูกยกเลิกแล้ว",
                    "align": "center",
                    "color": "#e74c3c",
                    "weight": "bold",
                    "wrap": True
                })
            elif is_reclaim:
                footer_contents.append({
                    "type": "text",
                    "text": "🚨 สถานะ: เรียกคืนเครื่อง (ค้างชำระเกินกำหนด)",
                    "align": "center",
                    "color": "#d63031",
                    "weight": "bold",
                    "wrap": True
                })
            elif not is_closed:
                footer_contents.append({
                    "type": "button",
                    "style": "primary",
                    "color": "#0d6efd",
                    "action": {
                        "type": "message",
                        "label": f"ชำระงวดที่ {next_inst_no} ({pay_amount:,.2f} บาท)",
                        "text": f"ชำระงวดที่ {next_inst_no}"
                    }
                })
                footer_contents.append({
                    "type": "button",
                    "style": "secondary",
                    "color": "#6c757d",
                    "action": {
                        "type": "message",
                        "label": "ปิดยอดก่อนกำหนด (ส่วนลด 15%)",
                        "text": "ปิดยอดก่อนกำหนด"
                    }
                })
            else:
                footer_contents.append({
                    "type": "text",
                    "text": "🔒 ปิดยอดเรียบร้อยแล้ว",
                    "align": "center",
                    "color": "#198754",
                    "weight": "bold",
                    "wrap": True
                })

        # ดีไซน์ Flex Message ในรูปแบบทันสมัยสไตล์การ์ดดิจิทัล
        flex_contents = {
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#c90404",
                "paddingAll": "xl",
                "contents": [
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "contents": [
                            {"type": "text", "text": "รายละเอียดสัญญาผ่อนชำระ", "weight": "bold", "color": "#ffffff", "size": "md", "flex": 1},
                            {"type": "text", "text": "ACTIVE" if not is_closed and not is_cancelled else ("CLOSED" if is_closed else "CANCELLED"), "weight": "bold", "color": "#ffffff", "size": "xs", "align": "end"}
                        ]
                    },
                    {"type": "text", "text": f"{contract['contract_number']}", "weight": "bold", "color": "#ffffff", "size": "xxl", "margin": "sm"},
                    {"type": "text", "text": f"สินค้า: {contract['product_name']}", "color": "#e0e0e0", "size": "xs", "margin": "xs", "wrap": True}
                ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "paddingAll": "xl",
                "contents": [
                    {
                        "type": "box",
                        "layout": "vertical",
                        "backgroundColor": "#f8f9fa",
                        "cornerRadius": "md",
                        "paddingAll": "md",
                        "contents": [
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": "ผู้เช่าซื้อ", "size": "xs", "color": "#6c757d"},
                                    {"type": "text", "text": f"{contract['customer_name']}", "size": "xs", "color": "#212529", "weight": "bold", "align": "end", "wrap": True}
                                ]
                            },
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "margin": "xs",
                                "contents": [
                                    {"type": "text", "text": "เบอร์โทรศัพท์", "size": "xs", "color": "#6c757d"},
                                    {"type": "text", "text": f"{contract['phone'] if contract['phone'] else '-'}", "size": "xs", "color": "#212529", "align": "end"}
                                ]
                            },
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "margin": "xs",
                                "contents": [
                                    {"type": "text", "text": "เลขบัตรประชาชน", "size": "xs", "color": "#6c757d"},
                                    {"type": "text", "text": f"{contract['id_card'] if contract['id_card'] else '-'}", "size": "xs", "color": "#212529", "align": "end"}
                                ]
                            }
                        ]
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "backgroundColor": "#f8f9fa",
                        "cornerRadius": "md",
                        "paddingAll": "md",
                        "contents": [
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": "IMEI / Serial", "size": "xs", "color": "#6c757d"},
                                    {"type": "text", "text": f"{contract['imei'] if contract['imei'] else (contract['serial_number'] if contract['serial_number'] else '-')}", "size": "xs", "color": "#212529", "align": "end", "wrap": True}
                                ]
                            },
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "margin": "xs",
                                "contents": [
                                    {"type": "text", "text": "สเปกเครื่อง", "size": "xs", "color": "#6c757d"},
                                    {"type": "text", "text": f"{contract['color'] if contract['color'] else '-'} / {contract['capacity'] if contract['capacity'] else '-'}", "size": "xs", "color": "#212529", "align": "end", "wrap": True}
                                ]
                            },
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "margin": "xs",
                                "contents": [
                                    {"type": "text", "text": "วันที่ทำสัญญา", "size": "xs", "color": "#6c757d"},
                                    {"type": "text", "text": f"{created_at_str}", "size": "xs", "color": "#212529", "align": "end"}
                                ]
                            }
                        ]
                    },
                    {"type": "separator"},
                    {
                        "type": "box",
                        "layout": "vertical",
                        "spacing": "xs",
                        "contents": [
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": "ยอดจัดผ่อนรวม", "size": "sm", "color": "#6c757d"},
                                    {"type": "text", "text": f"฿{float(contract['total_amount']):,.2f}", "size": "sm", "color": "#212529", "weight": "bold", "align": "end"}
                                ]
                            },
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": "ความคืบหน้าการผ่อน", "size": "sm", "color": "#6c757d"},
                                    {"type": "text", "text": f"{paid_count}/{contract['total_installments']} งวด", "size": "sm", "color": "#0d6efd", "weight": "bold", "align": "end"}
                                ]
                            },
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {"type": "text", "text": "ยอดผ่อนต่องวด", "size": "sm", "color": "#6c757d"},
                                    {"type": "text", "text": f"฿{float(contract['installment_amount']):,.2f}", "size": "sm", "color": "#212529", "weight": "bold", "align": "end"}
                                ]
                            }
                        ]
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "backgroundColor": "#e7f1ff" if not is_closed else "#d1e7dd",
                        "cornerRadius": "md",
                        "paddingAll": "md",
                        "margin": "xs",
                        "contents": [
                            {"type": "text", "text": "กำหนดชำระงวดถัดไป", "size": "xs", "color": "#0d6efd" if not is_closed else "#0f5132"},
                            {"type": "text", "text": f"{due_date_str}", "size": "sm", "weight": "bold", "color": "#0a58ca" if not is_closed else "#0f5132", "margin": "xs", "wrap": True}
                        ]
                    }
                ]
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "paddingAll": "md",
                "contents": footer_contents
            }
        }

        line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="ข้อมูลสัญญา", contents=flex_contents))
    finally:
        cursor.close()
        conn.close()

def send_payment_qr(user_id, installment_no, reply_token):
    try:
        conn = get_db()
    except Exception as err:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล"))
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status IN ('active', 'overdue_1', 'overdue_2') ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="ไม่พบสัญญาผ่อนชำระที่กำลังใช้งานอยู่"))
            return

        cursor.execute("SELECT * FROM payments WHERE contract_id = %s AND installment_no = %s", (contract['id'], installment_no))
        p_row = cursor.fetchone()

        if p_row and p_row.get('amount'):
            amount = float(p_row['amount'])
        else:
            amount = float(contract['installment_amount'])

        due_date = get_installment_due_date(contract.get('created_at'), contract.get('due_day', 5), installment_no)
        year_th = due_date.year + 543
        month_th = THAI_MONTHS[due_date.month]
        day_th = due_date.day

        qr_url = f"{request.host_url.rstrip('/')}/qr-code/{PROMPTPAY_ID}/{amount:.2f}"

        msg_text = (
            f"📱 QR Code สำหรับชำระเงินค่างวด\n"
            f"-------------------------------\n"
            f"📦 สินค้า: {contract['product_name']}\n"
            f"🔢 งวดที่: {installment_no}\n"
            f"📅 กำหนดชำระ: วันที่ {day_th} เดือน {month_th} พ.ศ. {year_th}\n"
            f"💰 ยอดชำระ: {amount:,.2f} บาท\n\n"
            f"สแกน QR Code ด้านล่างเพื่อชำระเงินได้ทันทีครับ"
        )

        line_bot_api.reply_message(
            reply_token,
            [
                TextSendMessage(text=msg_text),
                ImageSendMessage(original_content_url=qr_url, preview_image_url=qr_url)
            ]
        )
    except Exception as err:
        print(f"Error in send_payment_qr: {err}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="เกิดข้อผิดพลาดในการสร้าง QR Code ชำระเงิน"))
    finally:
        cursor.close()
        conn.close()

def send_early_close_qr(user_id, reply_token):
    try:
        conn = get_db()
    except Exception as err:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล"))
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status IN ('active', 'overdue_1', 'overdue_2') ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="ไม่พบสัญญาผ่อนชำระที่กำลังใช้งานอยู่"))
            return

        cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (contract['id'],))
        payments = cursor.fetchall()

        paid_count = sum(1 for p in payments if p['status'] == 'paid')
        remaining_count = contract['total_installments'] - paid_count

        if remaining_count <= 0:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="สัญญาของคุณปิดยอดชำระเรียบร้อยแล้วครับ"))
            return

        remaining_balance = float(remaining_count * contract['installment_amount'])
        discounted_close_amount = remaining_balance * 0.85
        qr_url = f"{request.host_url.rstrip('/')}/qr-code/{PROMPTPAY_ID}/{discounted_close_amount:.2f}"

        msg_text = (
            f"🔥 QR Code ปิดยอดสัญญาผ่อนชำระก่อนกำหนด (รับส่วนลด 15%)\n"
            f"-------------------------------\n"
            f"📦 สินค้า: {contract['product_name']}\n"
            f"🔢 ยอดคงเหลือ: {remaining_count} งวด ({remaining_balance:,.2f} บาท)\n"
            f"💰 ยอดสุทธิหลังหักส่วนลด: {discounted_close_amount:,.2f} บาท\n\n"
            f"สแกน QR Code ด้านล่างเพื่อชำระปิดยอดได้ทันทีครับ"
        )

        line_bot_api.reply_message(
            reply_token,
            [
                TextSendMessage(text=msg_text),
                ImageSendMessage(original_content_url=qr_url, preview_image_url=qr_url)
            ]
        )
    except Exception as err:
        print(f"Error in send_early_close_qr: {err}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="เกิดข้อผิดพลาดในการสร้าง QR Code ปิดยอด"))
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)