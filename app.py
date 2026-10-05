import os
import io
import json
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
    MessageEvent, TextMessage, ImageMessage, TextSendMessage, FlexSendMessage, ImageSendMessage,
    QuickReply, QuickReplyButton, MessageAction, PostbackAction, PostbackEvent, FollowEvent,
    RichMenuRequest, RichMenuArea, RichMenuBound, RichMenuSize
)
from promptpay import qrcode
import qrcode as qrcode_lib

from admin_routes import (
    admin_bp, process_contracts_api, process_contract_detail_api, 
    process_payments_by_contract_api, pay_contract_installment_api, unpay_contract_installment_api,
    build_flex_message_ui, send_custom_message_api,
    process_slips_api, process_slip_review_api, get_slip_image
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

# ============================================================================
# ระบบป้องกันบอท + โปรโตคอลคำสั่งจากปุ่มเมนู (Quick Reply / Flex Button / Rich Menu)
# ============================================================================
# บอทจะตอบกลับ "เฉพาะเมื่อลูกค้ากดปุ่มบนเมนู" เท่านั้น
# ข้อความที่ลูกค้าพิมพ์เองด้วยมือจะถูกบล็อกทิ้ง (ไม่ตอบอัตโนมัติ) กันการถูกบอทหรือสแปมยิงคำสั่ง
CMD_PREFIX = "#BOTMENU#"

CMD_STATUS      = "status"         # เช็คยอดค่างวด
CMD_CONTRACT    = "contract"       # ดูรายละเอียดสัญญา
CMD_PAY         = "pay"            # ชำระงวดที่ N
CMD_PAY_NEXT    = "pay_next"       # ชำระงวดถัดไป (งวดค้างชำระล่าสุด)
CMD_EARLY_CLOSE = "early_close"    # ปิดยอดก่อนกำหนด (ส่วนลด 15%)
CMD_UPLOAD_SLIP = "upload_slip"    # เปิดสถานะรอรับสลิป (ต้องกดปุ่มนี้ก่อน ถึงจะรับรูปได้)
CMD_CANCEL      = "cancel"         # ยกเลิกคำสั่งที่ค้างไว้
CMD_MENU        = "menu"           # เมนูหลัก

# เมนูหลัก (ใช้ทั้งใน Quick Reply และ Rich Menu)
MAIN_MENU_BUTTONS = [
    ("เช็คยอดค่างวด", CMD_STATUS),
    ("ชำระงวดถัดไป", CMD_PAY_NEXT),
    ("ปิดยอดก่อนกำหนด", CMD_EARLY_CLOSE),
    ("ข้อมูลสัญญา", CMD_CONTRACT),
]

# สถานะค้างของลูกค้า: รอส่งรูปสลิป (ต้องกดปุ่มอัปโหลดสลิปมาก่อนเท่านั้น)
STATE_AWAIT_SLIP = 'await_slip'
SLIP_STATE_TTL_MINUTES = int(os.environ.get('SLIP_STATE_TTL_MINUTES', '30'))
RICH_MENU_ID = os.environ.get('LINE_RICHMENU_ID', '')

# โหมดสำรอง: ถ้าใครต้องการให้ลูกค้าพิมพ์ข้อความเองได้ ให้ตั้งค่านี้เป็น true
# ค่าเริ่มต้น = false (ปิดไว้ตามหลักป้องกันบอท)
ALLOW_LEGACY_TEXT_COMMANDS = os.environ.get('ALLOW_LEGACY_TEXT_COMMANDS', 'false').strip().lower() in ('1', 'true', 'yes', 'on')


def build_cmd(action, *args):
    """ สร้างข้อความคำสั่งที่ฝังไว้ในปุ่มเมนู เพื่อให้ระบบแยกได้ว่าลูกค้ากดปุ่ม (ไม่ใช่พิมพ์เอง) """
    parts = [str(a) for a in args if a is not None and str(a) != '']
    return CMD_PREFIX + str(action) + ('|' + '|'.join(parts) if parts else '')


def parse_cmd(text):
    """ แยกคำสั่งจากปุ่มเมนู -> (action, args) หรือ None ถ้าไม่ใช่คำสั่งจากเมนู """
    if not text:
        return None
    raw = str(text).strip()
    if not raw.startswith(CMD_PREFIX):
        return None
    segments = raw[len(CMD_PREFIX):].split('|')
    action = segments[0].strip()
    if not action:
        return None
    return action, segments[1:]


def build_quick_reply(items):
    """ สร้าง Quick Reply จากรายการ (label, action, *args)
        ใช้ PostbackAction เพื่อไม่ให้ข้อความคำสั่งไปโผล่ในห้องแชทของลูกค้า """
    buttons = []
    for item in items or []:
        label = str(item[0])
        action = item[1]
        args = item[2:]
        # LINE กำหนดให้ label ของ Quick Reply ไม่เกิน 20 ตัวอักษร
        buttons.append(QuickReplyButton(action=PostbackAction(label=label[:20], data=build_cmd(action, *args))))
    return QuickReply(items=buttons) if buttons else None


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
            requested_early_close BOOLEAN DEFAULT FALSE,
            early_close_requested_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS due_day INTEGER DEFAULT 5;')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS evidence_file TEXT;')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS imei VARCHAR(100);')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS serial_number VARCHAR(100);')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS color VARCHAR(50);')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS capacity VARCHAR(50);')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS requested_early_close BOOLEAN DEFAULT FALSE;')
    cursor.execute('ALTER TABLE contracts ADD COLUMN IF NOT EXISTS early_close_requested_at TIMESTAMP;')
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

    # ตารางสลิปการโอนเงินที่ลูกค้าส่งรูปเข้ามาทาง LINE (ใช้แจ้งเตือนหลังบ้าน)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payment_slips (
            id SERIAL PRIMARY KEY,
            contract_id INTEGER REFERENCES contracts(id) ON DELETE CASCADE,
            line_user_id VARCHAR(100),
            message_id VARCHAR(255),
            slip_image VARCHAR(255),
            slip_amount NUMERIC(12, 2),
            installment_no INTEGER,
            status VARCHAR(50) DEFAULT 'pending',
            admin_note TEXT,
            reviewed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    cursor.execute('ALTER TABLE payment_slips ADD COLUMN IF NOT EXISTS slip_amount NUMERIC(12, 2);')
    cursor.execute('ALTER TABLE payment_slips ADD COLUMN IF NOT EXISTS installment_no INTEGER;')
    cursor.execute('ALTER TABLE payment_slips ADD COLUMN IF NOT EXISTS admin_note TEXT;')
    cursor.execute('ALTER TABLE payment_slips ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP;')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payment_slips_status ON payment_slips (status);")

    # ตารางเก็บสถานะค้างของลูกค้า (เช่น รอส่งรูปสลิป) ใช้ควบคุมว่าจะรับรูป/คำสั่งได้หรือไม่
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS line_user_states (
            line_user_id VARCHAR(100) PRIMARY KEY,
            state VARCHAR(50),
            payload TEXT,
            expires_at TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_line_user_states_state ON line_user_states (state);")
    conn.commit()
    cursor.close()
    conn.close()

try:
    init_db()
except Exception as e:
    print(f"Database initialization error: {e}")

# ============================================================================
# จัดการสถานะค้างของลูกค้า (ใช้ควบคุมการรับรูปสลิป / คำสั่งจากเมนู)
# หมายเหตุ: ใช้ NOW() ของฐานข้อมูลทั้งหมด เพื่อเลี่ยงปัญหา timezone ระหว่าง Python กับ PostgreSQL
# ============================================================================
def set_user_state(user_id, state, payload=None, ttl_minutes=None):
    """ บันทึกสถานะค้างของลูกค้า (เช่น กำลังรอรับรูปสลิป) ลงฐานข้อมูล """
    if not user_id or not state:
        return False

    ttl = SLIP_STATE_TTL_MINUTES if ttl_minutes is None else ttl_minutes

    try:
        conn = get_db()
    except Exception as e:
        print(f"DB Error on set_user_state: {e}")
        return False

    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO line_user_states (line_user_id, state, payload, expires_at, updated_at)
            VALUES (%s, %s, %s, NOW() + (%s * INTERVAL '1 minute'), NOW())
            ON CONFLICT (line_user_id) DO UPDATE
            SET state = EXCLUDED.state,
                payload = EXCLUDED.payload,
                expires_at = EXCLUDED.expires_at,
                updated_at = NOW()
        """, (user_id, state, payload, int(ttl)))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Error setting user state: {e}")
        return False
    finally:
        cursor.close()
        conn.close()


def get_user_state(user_id):
    """ คืนสถานะค้างของลูกค้าเป็น dict ({'state':..., 'payload':...}) หรือ None ถ้าไม่มี/หมดอายุ """
    if not user_id:
        return None

    try:
        conn = get_db()
    except Exception as e:
        print(f"DB Error on get_user_state: {e}")
        return None

    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT state, payload FROM line_user_states
            WHERE line_user_id = %s AND (expires_at IS NULL OR expires_at > NOW())
            """,
            (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            # ไม่มีสถานะ (หรือหมดอายุ) -> ล้างข้อมูลเก่าทิ้ง
            cursor.execute("DELETE FROM line_user_states WHERE line_user_id = %s", (user_id,))
            conn.commit()
            return None
        return {'state': row['state'], 'payload': row['payload']}
    except Exception as e:
        print(f"Error reading user state: {e}")
        return None
    finally:
        cursor.close()
        conn.close()


def clear_user_state(user_id):
    """ ล้างสถานะค้างของลูกค้า """
    if not user_id:
        return False

    try:
        conn = get_db()
    except Exception as e:
        print(f"DB Error on clear_user_state: {e}")
        return False

    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM line_user_states WHERE line_user_id = %s", (user_id,))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Error clearing user state: {e}")
        return False
    finally:
        cursor.close()
        conn.close()


def get_state_installment_no(user_state, default=None):
    """ ดึงเลขงวดที่ลูกค้ากดปุ่มอัปโหลดสลิปไว้ (ถ้าไม่มีใช้ค่า default) """
    if not user_state:
        return default
    raw = user_state.get('payload')
    if not raw:
        return default
    try:
        return json.loads(raw).get('installment_no') or default
    except Exception:
        return default


def get_active_contract(cursor, user_id, statuses=None):
    """ ดึงสัญญาที่กำลังใช้งานอยู่ของลูกค้า """
    statuses = statuses or ('active', 'overdue_1', 'overdue_2')
    placeholders = ', '.join(['%s'] * len(statuses))
    cursor.execute(
        f"SELECT * FROM contracts WHERE line_user_id = %s AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (user_id, *statuses)
    )
    return cursor.fetchone()


def get_installment_due_date(created_at, due_day, installment_no):
    if isinstance(created_at, str):
        try:
            created_at = datetime.datetime.strptime(created_at[:19], '%Y-%m-%d %H:%M:%S')
        except Exception:
            created_at = datetime.datetime.now(TH_TZ)

    start_year = created_at.year
    start_month = created_at.month
    
    # แก้ไข: คำนวณงวดที่ 1 ให้เป็นเดือนถัดไปจากเดือนที่ทำสัญญาจริงทันที และตรงกับหลังบ้าน
    target_month_index = (start_month - 1) + installment_no
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
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"Error on root delete contract: {e}")
            
    return process_contract_detail_api(contract_id)

@app.route('/api/contracts/<int:contract_id>/send_custom_message', methods=['POST'])
def root_api_send_custom_message(contract_id):
    return send_custom_message_api(contract_id)

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

        cursor.execute("UPDATE contracts SET status = 'closed_early', requested_early_close = FALSE WHERE id = %s", (contract_id,))
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

@app.route('/api/payments/<contract_identifier>', methods=['GET'])
def root_api_payments(contract_identifier):
    return process_payments_by_contract_api(contract_identifier)

@app.route('/api/contracts/<int:contract_id>/pay', methods=['POST', 'PUT'])
def root_api_pay(contract_id):
    return pay_contract_installment_api(contract_id)

@app.route('/api/contracts/<int:contract_id>/unpay', methods=['POST', 'PUT'])
def root_api_unpay(contract_id):
    return unpay_contract_installment_api(contract_id)

@app.route('/api/slips', methods=['GET'])
def root_api_slips():
    return process_slips_api()

@app.route('/api/slips/<int:slip_id>/review', methods=['POST', 'PUT'])
def root_api_review_slip(slip_id):
    return process_slip_review_api(slip_id)

@app.route('/slip/<int:slip_id>/image', methods=['GET'])
def root_slip_image(slip_id):
    return get_slip_image(slip_id)

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
                    f"🔢 งวดที่ต้องชำระ: งวดที่ {inst_no}/{c['total_installments']}\n"
                    f"📅 กำหนดชำระ: วันที่ {day_th} เดือน {month_th} พ.ศ. {year_th}\n"
                    f"💰 ยอดชำระ: {base_amount:,.2f} บาท\n\n"
                    f"กรุณาชำระเงินตามกำหนด ขอบคุณครับ"
                )
                send_line_push_notification(line_user_id, msg, inst_no, base_amount, title="⏰ แจ้งเตือนค่างวดผ่อนชำระ (ใกล้ถึงวันกำหนดชำระ)", contract_number=c.get('contract_number'), product_name=c.get('product_name'))
                notified_count += 1

            elif 1 <= days_overdue <= 2:
                msg = (
                    f"🔢 งวดที่ต้องชำระ: งวดที่ {inst_no}/{c['total_installments']}\n"
                    f"📅 กำหนดชำระเดิม: วันที่ {day_th} เดือน {month_th} พ.ศ. {year_th}\n"
                    f"💰 ยอดชำระ: {base_amount:,.2f} บาท (ไม่มีค่าปรับ)\n\n"
                    f"ขณะนี้เลยกำหนดชำระมาแล้ว {days_overdue} วัน กรุณาดำเนินการชำระเพื่อป้องกันการเกิดค่าปรับครับ"
                )
                send_line_push_notification(line_user_id, msg, inst_no, base_amount, title="🔔 แจ้งเตือนระยะที่ 1: เลยกำหนดชำระ (อนุโลม 2 วัน)", contract_number=c.get('contract_number'), product_name=c.get('product_name'))
                notified_count += 1

            elif 3 <= days_overdue <= 7:
                fine = days_overdue * 100
                total_with_fine = base_amount + fine
                msg = (
                    f"🔢 งวดที่ต้องชำระ: งวดที่ {inst_no}/{c['total_installments']}\n"
                    f"📅 กำหนดชำระเดิม: วันที่ {day_th} เดือน {month_th} พ.ศ. {year_th}\n"
                    f"💰 ค่างวด: {base_amount:,.2f} บาท\n"
                    f"💸 ค่าปรับ (100 บาท/วัน): {fine:,.2f} บาท\n"
                    f"💵 ยอดรวมที่ต้องชำระ: {total_with_fine:,.2f} บาท\n\n"
                    f"ขณะนี้อยู่ระหว่างค้างชำระ กรุณาชำระเงินโดยเร็วครับ"
                )
                send_line_push_notification(line_user_id, msg, inst_no, total_with_fine, title=f"⚠️ แจ้งเตือนระยะที่ 2: เลยกำหนดชำระ {days_overdue} วัน", contract_number=c.get('contract_number'), product_name=c.get('product_name'), color="#ffc107")
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
                    f"🔢 งวดที่ค้างชำระ: งวดที่ {inst_no}\n"
                    f"📅 กำหนดชำระเดิม: วันที่ {day_th} เดือน {month_th} พ.ศ. {year_th}\n"
                    f"💸 ค่าปรับบวกเพิ่ม: {fine_amount:,.2f} บาท\n\n"
                    f"ขณะนี้เลยกำหนดชำระแล้ว ระบบได้ข้ามเดือนที่ค้างชำระไปยังงวดถัดไป และนำงวดที่ค้างชำระพร้อมค่าปรับรวม {total_amount_shifted:,.2f} บาท ไปยกยอดเป็นงวดสุดท้ายเรียบร้อยแล้วครับ"
                )
                send_simple_push_notification(line_user_id, msg, title="🚨 แจ้งเตือนระยะที่ 3: เกินกำหนดชำระเกิน 7 วัน", contract_number=c.get('contract_number'), product_name=c.get('product_name'), color="#dc3545")
                notified_count += 1

        return {"success": True, "notified_count": notified_count}
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        cursor.close()
        conn.close()

def send_simple_push_notification(user_id, text_msg, title="📢 แจ้งเตือน", contract_number="", product_name="", color="#0d6efd"):
    try:
        flex_msg = build_flex_message_ui(title, text_msg, contract_number, product_name, color)
        line_bot_api.push_message(user_id, flex_msg)
    except Exception as e:
        print(f"Error sending Simple Push Message to {user_id}: {e}")

def send_line_push_notification(user_id, text_msg, installment_no, amount, title="📢 แจ้งเตือน", contract_number="", product_name="", color="#0d6efd"):
    try:
        flex_msg = build_flex_message_ui(title, text_msg, contract_number, product_name, color)
        quick_reply = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label=f"ชำระงวดที่ {installment_no}", text=f"ชำระงวดที่ {installment_no}")),
            QuickReplyButton(action=MessageAction(label="เช็คยอดค่างวด", text="เช็คยอด"))
        ])
        flex_msg.quick_reply = quick_reply
        line_bot_api.push_message(user_id, flex_msg)
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
            flex_msg = build_flex_message_ui("⚠️ แจ้งเตือน", "รูปแบบคำสั่งไม่ถูกต้องครับ", color="#dc3545")
            line_bot_api.reply_message(event.reply_token, flex_msg)
    elif user_text in ["ปิดยอดก่อนกำหนด", "ปิดยอด", "ปิดยอดทั้งหมด"]:
        notify_admin_early_close(user_id, user_text)
        send_early_close_qr(user_id, event.reply_token)
    else:
        search_contract_and_reply(user_id, user_text, event.reply_token)

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    """ ลูกค้าส่งรูปภาพสลิปการโอนเงินเข้ามา -> บันทึกสลิป + แจ้งเตือนหลังบ้าน """
    user_id = event.source.user_id
    message_id = event.message.id

    try:
        image_content = line_bot_api.get_message_content(message_id)
    except Exception as e:
        print(f"Error downloading LINE image content: {e}")
        flex_msg = build_flex_message_ui("⚠️ ไม่สามารถอ่านรูปได้", "ไม่สามารถดาวน์โหลดรูปที่คุณส่งมาได้ กรุณาส่งรูปสลิปอีกครั้งครับ", color="#dc3545")
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    try:
        conn = get_db()
    except Exception as e:
        print(f"DB Error on handle_image_message: {e}")
        flex_msg = build_flex_message_ui("⚠️ ข้อผิดพลาด", "เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล", color="#dc3545")
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT * FROM contracts WHERE line_user_id = %s AND status IN ('active', 'overdue_1', 'overdue_2') ORDER BY id DESC LIMIT 1",
            (user_id,)
        )
        contract = cursor.fetchone()

        if not contract:
            body_text = (
                "ได้รับรูปที่คุณส่งมาแล้วครับ แต่ยังไม่พบสัญญาที่ผูกกับบัญชี LINE นี้\n\n"
                "💡 กรุณาพิมพ์ 'เบอร์โทรศัพท์', 'เลขบัตรประชาชน' หรือ 'ชื่อ-นามสกุล' "
                "ที่ใช้ทำสัญญา เพื่อให้เจ้าหน้าที่ตรวจสอบสลิปให้คุณได้อย่างรวดเร็วครับ"
            )
            flex_msg = build_flex_message_ui("ℹ️ ยังไม่พบข้อมูลสัญญา", body_text, color="#6c757d")
            flex_msg.quick_reply = QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="เช็คยอดค่างวด", text="เช็คยอด"))
            ])
            line_bot_api.reply_message(event.reply_token, flex_msg)
            return

        # บันทึกรูปสลิปลง static/uploads
        now_time = datetime.datetime.now(TH_TZ)
        ext = 'jpg'
        content_type = getattr(image_content, 'content_type', '') or ''
        if 'png' in content_type:
            ext = 'png'

        upload_folder = os.path.join('static', 'uploads')
        os.makedirs(upload_folder, exist_ok=True)
        slip_filename = f"slip_{now_time.strftime('%Y%m%d%H%M%S')}_{user_id[-6:]}.{ext}"
        with open(os.path.join(upload_folder, slip_filename), 'wb') as f:
            f.write(image_content.content)

        # หางวดที่ค้างชำระอยู่ เพื่ออ้างอิงให้ผู้ดูแลตรวจสอบ
        cursor.execute(
            "SELECT installment_no, amount FROM payments WHERE contract_id = %s AND status != 'paid' ORDER BY installment_no ASC LIMIT 1",
            (contract['id'],)
        )
        next_payment = cursor.fetchone()

        slip_amount = float(next_payment['amount']) if next_payment and next_payment.get('amount') else None
        installment_no = next_payment['installment_no'] if next_payment else None

        cursor.execute("""
            INSERT INTO payment_slips (contract_id, line_user_id, message_id, slip_image, slip_amount, installment_no, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            RETURNING id
        """, (contract['id'], user_id, message_id, slip_filename, slip_amount, installment_no))
        slip_row = cursor.fetchone()
        slip_id = slip_row['id']
        conn.commit()

        # ตอบกลับลูกค้าว่าได้รับสลิปแล้ว
        amount_text = f"{slip_amount:,.2f} บาท" if slip_amount else "-"
        reply_body = (
            "ได้รับรูปสลิปการโอนเงินของคุณเรียบร้อยแล้วครับ ✅\n\n"
            f"💰 ยอดที่ระบุ: {amount_text}\n"
            f"🔢 งวดที่: งวดที่ {installment_no if installment_no else '-'}\n\n"
            "เจ้าหน้าที่จะตรวจสอบยอดเงินและแจ้งกลับคุณโดยเร็วที่สุดครับ 🙏"
        )
        flex_reply = build_flex_message_ui(
            "🧾 ได้รับสลิปการโอนเงินแล้ว",
            reply_body,
            contract_number=contract.get('contract_number'),
            product_name=contract.get('product_name'),
            color="#198754"
        )
        flex_reply.quick_reply = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="เช็คยอดค่างวด", text="เช็คยอด"))
        ])
        line_bot_api.reply_message(event.reply_token, flex_reply)

        # แจ้งเตือนผู้ดูแลระบบทาง LINE (ใช้รูปแบบเดียวกับการแจ้งเตือนปิดยอดก่อนกำหนด)
        print(
            f"[ADMIN NOTIFICATION] 🧾 ลูกค้าส่งสลิปการโอนเงิน! สัญญาเลขที่: {contract['contract_number']} | "
            f"ลูกค้า: {contract['customer_name']} ({contract['phone']}) | งวดที่: {installment_no} | "
            f"ยอด: {amount_text} | รหัสสลิป: {slip_id}"
        )
        notify_admin_new_slip(contract, slip_id, installment_no, slip_amount, now_time)
    except Exception as e:
        conn.rollback()
        print(f"Error handling slip image: {e}")
        flex_msg = build_flex_message_ui("⚠️ ข้อผิดพลาด", "เกิดข้อผิดพลาดในการบันทึกสลิป กรุณาส่งรูปใหม่อีกครั้งหรือติดต่อทางร้านครับ", color="#dc3545")
        try:
            line_bot_api.reply_message(event.reply_token, flex_msg)
        except Exception as reply_err:
            print(f"Error replying slip error message: {reply_err}")
    finally:
        cursor.close()
        conn.close()

def notify_admin_new_slip(contract, slip_id, installment_no, slip_amount, slip_time):
    """ แจ้งเตือนผู้ดูแลระบบเมื่อมีลูกค้าส่งสลิปการโอนเงินเข้ามาใหม่ """
    admin_line_id = os.environ.get('ADMIN_LINE_USER_ID')
    if not admin_line_id:
        return

    amount_text = f"{slip_amount:,.2f} บาท" if slip_amount else "ไม่ระบุ"
    admin_msg = (
        "🧾 ลูกค้าส่งสลิปการโอนเงินเข้ามา!\n"
        f"เลขที่สัญญา: {contract['contract_number']}\n"
        f"ชื่อลูกค้า: {contract['customer_name']}\n"
        f"เบอร์โทร: {contract['phone']}\n"
        f"สินค้า: {contract['product_name']}\n"
        f"งวดที่: งวดที่ {installment_no if installment_no else '-'}\n"
        f"ยอดที่ระบุ: {amount_text}\n"
        f"เวลาส่ง: {slip_time.strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        "กรุณาตรวจสอบรูปสลิปในระบบหลังบ้านครับ"
    )
    try:
        flex_admin = build_flex_message_ui(
            title="🧾 แจ้งเตือนลูกค้าส่งสลิปการโอนเงิน",
            body_text=admin_msg,
            contract_number=contract.get('contract_number'),
            product_name=contract.get('product_name'),
            color="#0dcaf0"
        )
        line_bot_api.push_message(admin_line_id, flex_admin)
    except Exception as push_err:
        print(f"Error pushing admin slip alert: {push_err}")

def notify_admin_early_close(user_id, trigger_text):
    try:
        conn = get_db()
    except Exception as e:
        print(f"DB Error on notify_admin_early_close: {e}")
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status IN ('active', 'overdue_1', 'overdue_2') ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()
        if contract:
            now_time = datetime.datetime.now(TH_TZ)
            cursor.execute("""
                UPDATE contracts 
                SET requested_early_close = TRUE, early_close_requested_at = %s 
                WHERE id = %s
            """, (now_time, contract['id']))
            conn.commit()

            cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (contract['id'],))
            payments = cursor.fetchall()
            unpaid_list = [p for p in payments if p['status'] != 'paid']
            
            bill_info = f"งวดที่เหลือ {len(unpaid_list)} งวด"
            if unpaid_list:
                next_p = unpaid_list[0]
                bill_info += f" (งวดถัดไป: งวดที่ {next_p['installment_no']})"

            print(f"[ADMIN NOTIFICATION] 🚨 แจ้งเตือนลูกค้ากดปิดยอดทั้งหมด! สัญญาเลขที่: {contract['contract_number']} | ลูกค้า: {contract['customer_name']} ({contract['phone']}) | รายละเอียดบิล: {bill_info} | ข้อความที่ส่ง: '{trigger_text}'")
            
            admin_line_id = os.environ.get('ADMIN_LINE_USER_ID')
            if admin_line_id:
                admin_msg = (
                    f"🚨 แจ้งเตือนลูกค้าขอปิดยอดทั้งหมด!\n"
                    f"เลขที่สัญญา: {contract['contract_number']}\n"
                    f"ชื่อลูกค้า: {contract['customer_name']}\n"
                    f"เบอร์โทร: {contract['phone']}\n"
                    f"สินค้า: {contract['product_name']}\n"
                    f"สถานะบิล: {bill_info}\n\n"
                    f"กรุณาตรวจสอบระบบหลังบ้านครับ"
                )
                try:
                    flex_admin = build_flex_message_ui(
                        title="🔔 แจ้งเตือนลูกค้าปิดยอดทั้งหมด",
                        body_text=admin_msg,
                        contract_number=contract.get('contract_number'),
                        product_name=contract.get('product_name'),
                        color="#fd7e14"
                    )
                    line_bot_api.push_message(admin_line_id, flex_admin)
                except Exception as push_err:
                    print(f"Error pushing admin alert: {push_err}")
    except Exception as e:
        print(f"Error notifying admin for early close: {e}")
    finally:
        cursor.close()
        conn.close()

def search_contract_and_reply(user_id, search_term, reply_token):
    try:
        conn = get_db()
    except Exception:
        flex_msg = build_flex_message_ui("⚠️ ข้อผิดพลาด", "เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล", color="#dc3545")
        line_bot_api.reply_message(reply_token, flex_msg)
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
            body_text = "ไม่พบข้อมูลสัญญาจากคำค้นหาของคุณ กรุณาพิมพ์ เบอร์โทรศัพท์, เลขบัตรประชาชน หรือ ชื่อ-นามสกุล ที่ใช้ลงทะเบียนสัญญาให้ถูกต้องครับ"
            flex_msg = build_flex_message_ui("🔍 ค้นหาสัญญา", body_text, color="#6c757d")
            flex_msg.quick_reply = QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="เช็คยอดค่างวด", text="เช็คยอด")),
                QuickReplyButton(action=MessageAction(label="ปิดยอดก่อนกำหนด", text="ปิดยอดก่อนกำหนด"))
            ])
            line_bot_api.reply_message(reply_token, flex_msg)
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
            body_text = "ไม่พบข้อมูลสัญญาผ่อนชำระที่ผูกกับ LINE นี้\n\n💡 ท่านสามารถพิมพ์ 'เบอร์โทรศัพท์', 'เลขบัตรประชาชน' หรือ 'ชื่อ-นามสกุล' เพื่อค้นหาสัญญาของคุณได้เลยครับ"
            flex_msg = build_flex_message_ui("ℹ️ ไม่พบสัญญา", body_text, color="#6c757d")
            line_bot_api.reply_message(reply_token, flex_msg)
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
            body_text = "ไม่พบข้อมูลสัญญาผ่อนชำระที่ผูกกับ LINE นี้"
            flex_msg = build_flex_message_ui("ℹ️ ไม่พบสัญญา", body_text, color="#6c757d")
            line_bot_api.reply_message(reply_token, flex_msg)
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
            pass
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

        body_contents = [
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

        if contract.get('evidence_file'):
            evidence_url = f"{request.host_url.rstrip('/')}/static/uploads/{contract['evidence_file']}"
            body_contents.append({
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#f8f9fa",
                "cornerRadius": "md",
                "paddingAll": "md",
                "margin": "md",
                "contents": [
                    {"type": "text", "text": "📎 หลักฐานสัญญา", "size": "xs", "color": "#6c757d", "weight": "bold"},
                    {
                        "type": "button",
                        "style": "link",
                        "height": "sm",
                        "action": {
                            "type": "uri",
                            "label": "🖼️ คลิกเพื่อดูหลักฐานสัญญา",
                            "uri": evidence_url
                        }
                    }
                ]
            })

        flex_contents = {
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#0d6efd",
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
                "contents": body_contents
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
        flex_msg = build_flex_message_ui("⚠️ ข้อผิดพลาด", "เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล", color="#dc3545")
        line_bot_api.reply_message(reply_token, flex_msg)
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status IN ('active', 'overdue_1', 'overdue_2') ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            flex_msg = build_flex_message_ui("ℹ️ ไม่พบสัญญา", "ไม่พบสัญญาผ่อนชำระที่กำลังใช้งานอยู่", color="#6c757d")
            line_bot_api.reply_message(reply_token, flex_msg)
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
            f"🔢 งวดที่: {installment_no}\n"
            f"📅 กำหนดชำระ: วันที่ {day_th} เดือน {month_th} พ.ศ. {year_th}\n"
            f"💰 ยอดชำระ: {amount:,.2f} บาท\n\n"
            f"สแกน QR Code ด้านล่างเพื่อชำระเงินได้ทันทีครับ"
        )
        flex_msg = build_flex_message_ui("📱 QR Code สำหรับชำระเงินค่างวด", msg_text, contract_number=contract.get('contract_number'), product_name=contract.get('product_name'), color="#0d6efd")

        line_bot_api.reply_message(
            reply_token,
            [
                flex_msg,
                ImageSendMessage(original_content_url=qr_url, preview_image_url=qr_url)
            ]
        )
    except Exception as err:
        print(f"Error in send_payment_qr: {err}")
        flex_msg = build_flex_message_ui("⚠️ ข้อผิดพลาด", "เกิดข้อผิดพลาดในการสร้าง QR Code ชำระเงิน", color="#dc3545")
        line_bot_api.reply_message(reply_token, flex_msg)
    finally:
        cursor.close()
        conn.close()

def send_early_close_qr(user_id, reply_token):
    try:
        conn = get_db()
    except Exception as err:
        flex_msg = build_flex_message_ui("⚠️ ข้อผิดพลาด", "เกิดข้อผิดพลาดในการเชื่อมต่อฐานข้อมูล", color="#dc3545")
        line_bot_api.reply_message(reply_token, flex_msg)
        return

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE line_user_id = %s AND status IN ('active', 'overdue_1', 'overdue_2') ORDER BY id DESC LIMIT 1", (user_id,))
        contract = cursor.fetchone()

        if not contract:
            flex_msg = build_flex_message_ui("ℹ️ ไม่พบสัญญา", "ไม่พบสัญญาผ่อนชำระที่กำลังใช้งานอยู่", color="#6c757d")
            line_bot_api.reply_message(reply_token, flex_msg)
            return

        cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (contract['id'],))
        payments = cursor.fetchall()

        paid_count = sum(1 for p in payments if p['status'] == 'paid')
        remaining_count = contract['total_installments'] - paid_count

        if remaining_count <= 0:
            flex_msg = build_flex_message_ui("ℹ️ แจ้งเตือน", "สัญญาของคุณปิดยอดชำระเรียบร้อยแล้วครับ", color="#198754")
            line_bot_api.reply_message(reply_token, flex_msg)
            return

        remaining_balance = float(remaining_count * contract['installment_amount'])
        discounted_close_amount = remaining_balance * 0.85
        qr_url = f"{request.host_url.rstrip('/')}/qr-code/{PROMPTPAY_ID}/{discounted_close_amount:.2f}"

        msg_text = (
            f"🔢 ยอดคงเหลือ: {remaining_count} งวด ({remaining_balance:,.2f} บาท)\n"
            f"💰 ยอดสุทธิหลังหักส่วนลด: {discounted_close_amount:,.2f} บาท\n\n"
            f"สแกน QR Code ด้านล่างเพื่อชำระปิดยอดได้ทันทีครับ"
        )
        flex_msg = build_flex_message_ui("🔥 QR Code ปิดยอดสัญญา (รับส่วนลด 15%)", msg_text, contract_number=contract.get('contract_number'), product_name=contract.get('product_name'), color="#fd7e14")

        line_bot_api.reply_message(
            reply_token,
            [
                flex_msg,
                ImageSendMessage(original_content_url=qr_url, preview_image_url=qr_url)
            ]
        )
    except Exception as err:
        print(f"Error in send_early_close_qr: {err}")
        flex_msg = build_flex_message_ui("⚠️ ข้อผิดพลาด", "เกิดข้อผิดพลาดในการสร้าง QR Code ปิดยอด", color="#dc3545")
        line_bot_api.reply_message(reply_token, flex_msg)
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)