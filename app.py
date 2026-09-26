import os
import datetime
import urllib.parse
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
            due_day INTEGER DEFAULT 5,
            status VARCHAR(50) DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    cursor.execute('''
        ALTER TABLE contracts ADD COLUMN IF NOT EXISTS due_day INTEGER DEFAULT 5;
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
        return jsonify({"success": True, "message": f"Contract status updated to '{new_status}' successfully", "status": new_status})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
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

# --- Cron API สำหรับเช็กและแจ้งเตือนค่างวดผ่าน LINE ---
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
        cursor.execute("SELECT * FROM contracts WHERE status = 'active'")
        contracts = cursor.fetchall()

        for c in contracts:
            line_user_id = c.get('line_user_id')
            if not line_user_id:
                continue

            due_day = c.get('due_day') or 5
            
            try:
                due_date_this_month = datetime.date(today.year, today.month, due_day)
            except ValueError:
                import calendar
                last_day = calendar.monthrange(today.year, today.month)[1]
                due_date_this_month = datetime.date(today.year, today.month, last_day)

            days_diff = (due_date_this_month - today).days

            cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (c['id'],))
            payments = cursor.fetchall()

            paid_count = sum(1 for p in payments if p['status'] == 'paid')
            next_installment_no = paid_count + 1

            if next_installment_no > c['total_installments']:
                continue

            installment_amount = float(c['installment_amount'])

            if days_diff == 3:
                msg = (
                    f"⏰ แจ้งเตือนค่างวดผ่อนชำระ (ล่วงหน้า 3 วัน)\n"
                    f"-------------------------------\n"
                    f"📦 สินค้า: {c['product_name']}\n"
                    f"🔢 งวดที่: {next_installment_no}/{c['total_installments']}\n"
                    f"💰 ยอดชำระ: {installment_amount:,.2f} บาท\n"
                    f"📅 กำหนดชำระวันที่: {due_date_this_month.strftime('%d/%m/%Y')}\n\n"
                    f"พิมพ์ 'เช็คยอด' เพื่อดูรายละเอียดหรือกดชำระเงินได้เลยครับ"
                )
                send_line_push_notification(line_user_id, msg, next_installment_no, installment_amount)
                notified_count += 1
            elif days_diff == 0:
                msg = (
                    f"🔔 แจ้งเตือนครบกำหนดชำระค่างวดวันนี้!\n"
                    f"-------------------------------\n"
                    f"📦 สินค้า: {c['product_name']}\n"
                    f"🔢 งวดที่: {next_installment_no}/{c['total_installments']}\n"
                    f"💰 ยอดที่ต้องชำระ: {installment_amount:,.2f} บาท\n"
                    f"📅 กำหนดชำระ: วันนี้ ({due_date_this_month.strftime('%d/%m/%Y')})\n\n"
                    f"กรุณาชำระเงินภายในวันนี้เพื่อรักษาสิทธิ์ ขอบคุณครับ"
                )
                send_line_push_notification(line_user_id, msg, next_installment_no, installment_amount)
                notified_count += 1
            elif days_diff < 0 and days_diff >= -5:
                msg = (
                    f"⚠️ แจ้งเตือนเกินกำหนดชำระค่างวด ({abs(days_diff)} วัน)\n"
                    f"-------------------------------\n"
                    f"📦 สินค้า: {c['product_name']}\n"
                    f"🔢 งวดที่ค้างชำระ: งวดที่ {next_installment_no}\n"
                    f"💰 ยอดค้างชำระ: {installment_amount:,.2f} บาท\n\n"
                    f"กรุณาดำเนินการชำระค่างวดโดยเร็วครับ"
                )
                send_line_push_notification(line_user_id, msg, next_installment_no, installment_amount)
                notified_count += 1

        return {"success": True, "notified_count": notified_count}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        cursor.close()
        conn.close()

def send_line_push_notification(user_id, text_msg, installment_no, amount):
    try:
        quick_reply = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label=f"ชำระงวดที่ {installment_no}", text=f"ชำระงวดที่ {installment_no}")),
            QuickReplyButton(action=MessageAction(label="เช็คยอดค่างวด", text="เช็คยอด"))
        ])
        line_bot_api.push_message(user_id, TextSendMessage(text=text_msg, quick_reply=quick_reply))
    except Exception as e:
        print(f"Error sending Push Message to {user_id}: {e}")

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
        # ระบบค้นหาสัญญาด้วย เบอร์โทร บัตรประชาชน หรือ ชื่อ-สกุล
        search_contract_and_reply(user_id, user_text, event.reply_token)

def search_contract_and_reply(user_id, search_term, reply_token):
    try:
        conn = get_db()
    except Exception:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล"))
        return

    cursor = conn.cursor()
    try:
        # ลบขีดหรือช่องว่างกรณีค้นหาเบอร์หรือบัตรประชาชน
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
            # ทำการผูก line_user_id อัตโนมัติหากยังไม่ได้ผูก
            if not contract['line_user_id']:
                cursor.execute("UPDATE contracts SET line_user_id = %s WHERE id = %s", (user_id, contract['id']))
                conn.commit()

            render_flex_contract(contract, reply_token)
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
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status = 'active' ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            line_bot_api.reply_message(
                reply_token, 
                TextSendMessage(text="ไม่พบข้อมูลสัญญาผ่อนชำระที่ผูกกับ LINE นี้\n\n💡 ท่านสามารถพิมพ์ 'เบอร์โทรศัพท์', 'เลขบัตรประชาชน' หรือ 'ชื่อ-นามสกุล' เพื่อค้นหาสัญญาของคุณได้เลยครับ")
            )
            return

        render_flex_contract(contract, reply_token)
    finally:
        cursor.close()
        conn.close()

def render_flex_contract(contract, reply_token):
    try:
        conn = get_db()
    except Exception:
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (contract['id'],))
        payments = cursor.fetchall()

        paid_count = sum(1 for p in payments if p['status'] == 'paid')
        remaining_count = contract['total_installments'] - paid_count
        remaining_balance = float(remaining_count * contract['installment_amount'])
        discounted_close_amount = remaining_balance * 0.85 if remaining_balance > 0 else 0
        due_day = contract.get('due_day') or 5

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
                        {"type": "text", "text": f"เบอร์โทรศัพท์: {contract['phone'] if contract['phone'] else '-'}", "size": "sm"},
                        {"type": "text", "text": f"ค่างวด: {float(contract['installment_amount']):,.2f} บาท/งวด", "size": "sm", "weight": "bold", "color": "#2c3e50"},
                        {"type": "text", "text": f"งวดทั้งหมด: {contract['total_installments']} งวด", "size": "sm"},
                        {"type": "text", "text": f"📅 กำหนดชำระทุกวันที่: {due_day} ของเดือน", "size": "sm", "color": "#2980b9", "weight": "bold"},
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
    except Exception as e:
        print(f"Database error in send_payment_qr: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล กรุณาลองใหม่อีกครั้ง"))
        return

    cursor = conn.cursor()
    try:
        # ค้นหาจาก line_user_id หรือดึงสัญญาล่าสุดของลูกค้า
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status = 'active' ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            line_bot_api.reply_message(
                reply_token, 
                TextSendMessage(text="ไม่พบข้อมูลสัญญาของคุณ กรุณาพิมพ์ เบอร์โทรศัพท์ หรือ เลขบัตรประชาชน เพื่อค้นหาและผูกสัญญาบัญชีก่อนครับ")
            )
            return

        amount = float(contract['installment_amount'])
        promptpay_number = PROMPTPAY_ID.strip() if PROMPTPAY_ID else '0800000000'

        try:
            qr_payload = qrcode.generate_payload(promptpay_number, amount)
            encoded_payload = urllib.parse.quote(qr_payload)
            qr_image_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={encoded_payload}"

            line_bot_api.reply_message(
                reply_token,
                [
                    TextSendMessage(text=f"📌 สแกนเพื่อชำระค่างวดที่ {installment_no}\n📦 สินค้า: {contract['product_name']}\n💰 ยอดชำระ: {amount:,.2f} บาท\n📱 PromptPay: {promptpay_number}"),
                    FlexSendMessage(
                        alt_text=f"QR Code ชำระเงินงวดที่ {installment_no}",
                        contents={
                            "type": "bubble",
                            "body": {
                                "type": "box",
                                "layout": "vertical",
                                "contents": [
                                    {"type": "text", "text": f"QR Code ชำระเงินงวดที่ {installment_no}", "weight": "bold", "align": "center", "size": "md", "color": "#1DB446"},
                                    {"type": "text", "text": f"ยอดชำระ {amount:,.2f} บาท", "align": "center", "size": "sm", "color": "#555555", "margin": "xs"},
                                    {"type": "image", "url": qr_image_url, "size": "5l", "aspectRatio": "1:1"}
                                ]
                            }
                        }
                    )
                ]
            )
        except Exception as qr_err:
            print(f"Error generating QR Code: {qr_err}")
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text=f"📌 ข้อมูลการชำระค่างวดที่ {installment_no}\n📦 สินค้า: {contract['product_name']}\n💰 ยอดชำระ: {amount:,.2f} บาท\n📱 หมายเลขพร้อมเพย์: {promptpay_number}\n\nกรุณาโอนผ่านหมายเลขพร้อมเพย์ด้านบนและส่งสลิปได้เลยครับ")
            )
    except Exception as err:
        print(f"Error in send_payment_qr: {err}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="เกิดข้อผิดพลาดในการสร้างรายการชำระเงิน กรุณาลองใหม่อีกครั้งครับ"))
    finally:
        cursor.close()
        conn.close()

def send_early_close_qr(user_id, reply_token):
    try:
        conn = get_db()
    except Exception as e:
        print(f"Database error in send_early_close_qr: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล"))
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status = 'active' ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="ไม่พบข้อมูลสัญญาที่กำลังผ่อนชำระ กรุณาพิมพ์ เบอร์โทรศัพท์ เพื่อค้นหาสัญญาครับ"))
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
        promptpay_number = PROMPTPAY_ID.strip() if PROMPTPAY_ID else '0800000000'

        qr_payload = qrcode.generate_payload(promptpay_number, final_pay_amount)
        encoded_payload = urllib.parse.quote(qr_payload)
        qr_image_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={encoded_payload}"

        msg = (
            f"🎉 ข้อเสนอพิเศษปิดยอดก่อนกำหนด\n"
            f"• ยอดคงเหลือคงค้าง ({remaining_count} งวด): {remaining_balance:,.2f} บาท\n"
            f"• ส่วนลดพิเศษ (15%): -{discount_amount:,.2f} บาท\n"
            f"-------------------------------\n"
            f"💰 ยอดสุทธิที่ต้องชำระปิดบัญชี: {final_pay_amount:,.2f} บาท\n"
            f"📱 หมายเลขพร้อมเพย์: {promptpay_number}"
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
    except Exception as err:
        print(f"Error in send_early_close_qr: {err}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="เกิดข้อผิดพลาดในการสร้างรายการปิดยอดก่อนกำหนด"))
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