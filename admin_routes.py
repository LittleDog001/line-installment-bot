import os
import datetime
import calendar
import psycopg2
import werkzeug
import random
from psycopg2.extras import RealDictCursor
from zoneinfo import ZoneInfo
from flask import Blueprint, render_template, request, redirect, url_for, jsonify
from linebot import LineBotApi
from linebot.models import TextSendMessage

admin_bp = Blueprint('admin', __name__)
TH_TZ = ZoneInfo('Asia/Bangkok')

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_ACCESS_TOKEN')
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)

def generate_short_contract_number(cursor):
    """สร้างเลขสัญญาแบบสั้น เช่น C-260928-101"""
    now = datetime.datetime.now(TH_TZ)
    prefix = f"C-{now.strftime('%y%m%d')}"
    for _ in range(100):
        rand_num = random.randint(100, 999)
        candidate = f"{prefix}-{rand_num}"
        cursor.execute("SELECT id FROM contracts WHERE contract_number = %s", (candidate,))
        if not cursor.fetchone():
            return candidate
    return f"{prefix}-{now.strftime('%H%M%S')}"

def get_installment_due_date(created_at, due_day, installment_no):
    if isinstance(created_at, str):
        try:
            created_at = datetime.datetime.strptime(created_at[:19], '%Y-%m-%d %H:%M:%S')
        except Exception:
            created_at = datetime.datetime.now(TH_TZ)

    start_year = created_at.year
    start_month = created_at.month
    
    # คำนวณงวดที่ 1 ให้เป็นเดือนถัดไปจากวันที่ทำสัญญา
    target_month_index = start_month + installment_no - 1
    target_year = start_year + (target_month_index // 12)
    target_month = (target_month_index % 12) + 1

    last_day_of_month = calendar.monthrange(target_year, target_month)[1]
    actual_day = min(due_day, last_day_of_month)

    return datetime.date(target_year, target_month, actual_day)

def send_push_thank_you(user_id, contract_number, product_name):
    if not user_id:
        return
    try:
        msg = (
            f"🎉 ขอขอบพระคุณเป็นอย่างยิ่งครับ!\n"
            f"-------------------------------\n"
            f"📦 สินค้า: {product_name}\n"
            f"เลขที่สัญญา: {contract_number}\n\n"
            f"ท่านได้ดำเนินการชำระเงินและปิดยอดสัญญาครบถ้วนเรียบร้อยแล้ว ขอบคุณที่ไว้วางใจใช้บริการของเราครับ 🙏✨"
        )
        line_bot_api.push_message(user_id, TextSendMessage(text=msg))
    except Exception as e:
        print(f"Error sending thank you message to {user_id}: {e}")

def send_push_cancellation(user_id, contract_number, product_name, contract_status="active"):
    if not user_id:
        return
    
    if contract_status in ['closed', 'closed_early']:
        print(f"Contract {contract_number} is already closed/completed. Skipping cancellation notification.")
        return

    try:
        msg = (
            f"⚠️ แจ้งเตือนสถานะสัญญาของคุณ\n"
            f"-------------------------------\n"
            f"📦 สินค้า: {product_name}\n"
            f"เลขที่สัญญา: {contract_number}\n\n"
            f"สัญญาเช่าซื้อของคุณได้ถูก **ยกเลิก / ลบออกจากระบบ** เรียบร้อยแล้วครับ หากมีข้อสงสัยประการใดกรุณาติดต่อเจ้าหน้าที่ 🙏"
        )
        line_bot_api.push_message(user_id, TextSendMessage(text=msg))
    except Exception as e:
        print(f"Error sending cancellation message to {user_id}: {e}")

def send_push_contract_updated(user_id, contract_number, product_name, updated_fields_text):
    if not user_id:
        return
    try:
        msg = (
            f"📝 แจ้งการอัปเดตข้อมูลสัญญาของคุณ\n"
            f"-------------------------------\n"
            f"📦 สินค้า: {product_name}\n"
            f"เลขที่สัญญา: {contract_number}\n\n"
            f"ทางร้านได้ทำการแก้ไขข้อมูลสัญญาของคุณ มีรายละเอียดดังนี้:\n"
            f"{updated_fields_text}\n\n"
            f"หากมีข้อสงสัยประการใดสามารถติดต่อสอบถามเจ้าหน้าที่ได้เลยครับ 🙏"
        )
        line_bot_api.push_message(user_id, TextSendMessage(text=msg))
    except Exception as e:
        print(f"Error sending contract update message to {user_id}: {e}")

def get_db():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise ValueError("DATABASE_URL is not set")
    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    return conn

def calculate_contract_overdue_status(cursor, c_dict):
    today = datetime.datetime.now(TH_TZ).date()
    cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (c_dict['id'],))
    payments = [dict(p) for p in cursor.fetchall()]

    unpaid_payments = [p for p in payments if p['status'] != 'paid']
    
    overdue_count = 0
    overdue_7d_flag = False
    overdue_installments_list = []

    for p in unpaid_payments:
        due_date = get_installment_due_date(c_dict.get('created_at'), c_dict.get('due_day', 5), p['installment_no'])
        if today > due_date:
            overdue_count += 1
            days_overdue = (today - due_date).days
            overdue_installments_list.append(f"งวดที่ {p['installment_no']} (เกิน {days_overdue} วัน)")
            if days_overdue >= 7:
                overdue_7d_flag = True

    # ปรับสถานะสัญญาตามจำนวนการค้างชำระ
    new_status = c_dict.get('status', 'active')
    if c_dict.get('status') not in ['closed', 'closed_early', 'cancelled']:
        if overdue_count >= 3:
            new_status = 'reclaim' # เรียกคืนเครื่อง
        elif overdue_count == 2:
            new_status = 'overdue_2' # ค้างชำระครั้งที่ 2
        elif overdue_count == 1:
            new_status = 'overdue_1' # ค้างชำระครั้งที่ 1
        else:
            new_status = 'active'
            
        if new_status != c_dict.get('status'):
            cursor.execute("UPDATE contracts SET status = %s WHERE id = %s", (new_status, c_dict['id']))

    c_dict['status'] = new_status
    c_dict['overdue_count'] = overdue_count
    c_dict['overdue_7d_flag'] = overdue_7d_flag
    c_dict['overdue_installments_text'] = ", ".join(overdue_installments_list) if overdue_installments_list else "-"
    c_dict['payments'] = payments
    return c_dict

@admin_bp.route('/')
def index():
    try:
        conn = get_db()
    except Exception as e:
        return f"Database error: {str(e)}", 500

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts ORDER BY id DESC")
        contracts = cursor.fetchall()
        
        contract_list = []
        for c in contracts:
            c_dict = dict(c)
            c_dict = calculate_contract_overdue_status(cursor, c_dict)
            payments = c_dict['payments']
            
            c_dict['total_amount'] = float(c_dict['total_amount']) if c_dict['total_amount'] is not None else 0.0
            c_dict['installment_amount'] = float(c_dict['installment_amount']) if c_dict['installment_amount'] is not None else 0.0

            c_dict['due_day'] = c_dict.get('due_day', 5) or 5
            if c_dict.get('created_at'):
                c_dict['created_at_formatted'] = c_dict['created_at'].strftime('%d/%m/%Y %H:%M')
                c_dict['created_at'] = str(c_dict['created_at'])
            else:
                c_dict['created_at_formatted'] = '-'

            paid_count = sum(1 for p in payments if p['status'] == 'paid')
            remaining_count = c_dict['total_installments'] - paid_count
            remaining_amount = float(remaining_count * c_dict['installment_amount'])
            close_with_discount = remaining_amount * 0.85
            
            c_dict['paid_count'] = paid_count
            c_dict['remaining_count'] = remaining_count
            c_dict['remaining_amount'] = remaining_amount
            c_dict['close_with_discount'] = close_with_discount
            contract_list.append(c_dict)

        conn.commit()
        return render_template('admin.html', contracts=contract_list)
    except Exception as e:
        conn.rollback()
        return f"Error loading admin page: {str(e)}", 500
    finally:
        cursor.close()
        conn.close()

@admin_bp.route('/contract/create', methods=['POST'])
def create_contract():
    try:
        conn = get_db()
    except Exception as e:
        return f"Database error: {str(e)}", 500

    cursor = conn.cursor()
    try:
        line_user_id = (request.form.get('line_user_id') or '').strip()
        customer_name = (request.form.get('customer_name') or '').strip()
        id_card = (request.form.get('id_card') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        product_name = (request.form.get('product_name') or '').strip()
        imei = (request.form.get('imei') or '').strip()
        serial_number = (request.form.get('serial_number') or '').strip()
        color = (request.form.get('color') or '').strip()
        capacity = (request.form.get('capacity') or '').strip()
        
        raw_total = request.form.get('total_amount', 0)
        raw_installments = request.form.get('total_installments', 0)
        raw_installment_amount = request.form.get('installment_amount', 0)

        total_installments = int(raw_installments) if raw_installments else 0
        
        now_time = datetime.datetime.now(TH_TZ)
        due_day = now_time.day

        if raw_installment_amount and float(raw_installment_amount) > 0:
            installment_amount = float(raw_installment_amount)
            total_amount = installment_amount * total_installments
        else:
            total_amount = float(raw_total) if raw_total else 0.0
            installment_amount = total_amount / total_installments if total_installments > 0 else 0.0

        if total_installments <= 0 or total_amount <= 0 or installment_amount <= 0:
            return "กรุณากรอกยอดเงินรวม/ยอดต่องวด และจำนวนงวดให้ถูกต้อง", 400

        contract_number = generate_short_contract_number(cursor)

        evidence_filename = None
        evidence_file = request.files.get('evidence_file')
        if evidence_file and evidence_file.filename:
            filename = werkzeug.utils.secure_filename(evidence_file.filename)
            timestamp_str = now_time.strftime('%Y%m%d%H%M%S')
            evidence_filename = f"ev_{timestamp_str}_{filename}"
            upload_folder = os.path.join('static', 'uploads')
            os.makedirs(upload_folder, exist_ok=True)
            evidence_file.save(os.path.join(upload_folder, evidence_filename))

        cursor.execute("""
            INSERT INTO contracts (
                contract_number, line_user_id, customer_name, id_card, phone, 
                product_name, imei, serial_number, color, capacity, total_amount, total_installments, installment_amount, due_day, status, evidence_file
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
            RETURNING id
        """, (
            contract_number, line_user_id, customer_name, id_card, phone, 
            product_name, imei, serial_number, color, capacity, total_amount, total_installments, installment_amount, due_day, evidence_filename
        ))
        
        contract_row = cursor.fetchone()
        contract_id = contract_row['id']

        for i in range(1, total_installments + 1):
            cursor.execute("""
                INSERT INTO payments (contract_id, installment_no, amount, status)
                VALUES (%s, %s, %s, 'pending')
            """, (contract_id, i, installment_amount))

        conn.commit()
        return redirect(url_for('admin.index'))

    except Exception as e:
        conn.rollback()
        return f"เกิดข้อผิดพลาดในการบันทึกสัญญา: {str(e)}", 500
    finally:
        cursor.close()
        conn.close()

def process_contracts_api():
    try:
        conn = get_db()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    cursor = conn.cursor()
    try:
        if request.method == 'POST':
            data = request.form if request.form else (request.get_json(silent=True) or {})
            line_user_id = (data.get('line_user_id') or '').strip()
            customer_name = (data.get('customer_name') or '').strip()
            id_card = (data.get('id_card') or '').strip()
            phone = (data.get('phone') or '').strip()
            product_name = (data.get('product_name') or '').strip()
            imei = (data.get('imei') or '').strip()
            serial_number = (data.get('serial_number') or '').strip()
            color = (data.get('color') or '').strip()
            capacity = (data.get('capacity') or '').strip()
            
            raw_total = data.get('total_amount', 0)
            raw_installments = data.get('total_installments', 0)
            raw_installment_amount = data.get('installment_amount', 0)

            total_installments = int(raw_installments) if raw_installments else 0
            
            now_time = datetime.datetime.now(TH_TZ)
            due_day = now_time.day

            if raw_installment_amount and float(raw_installment_amount) > 0:
                installment_amount = float(raw_installment_amount)
                total_amount = installment_amount * total_installments
            else:
                total_amount = float(raw_total) if raw_total else 0.0
                installment_amount = total_amount / total_installments if total_installments > 0 else 0.0

            if total_installments <= 0 or total_amount <= 0 or installment_amount <= 0:
                return jsonify({'error': 'กรุณากรอกยอดเงินรวม/ยอดต่องวด และจำนวนงวดให้ถูกต้อง'}), 400

            contract_number = generate_short_contract_number(cursor)

            evidence_filename = None
            evidence_file = request.files.get('evidence_file') if request.files else None
            if evidence_file and evidence_file.filename:
                filename = werkzeug.utils.secure_filename(evidence_file.filename)
                timestamp_str = now_time.strftime('%Y%m%d%H%M%S')
                evidence_filename = f"ev_{timestamp_str}_{filename}"
                upload_folder = os.path.join('static', 'uploads')
                os.makedirs(upload_folder, exist_ok=True)
                evidence_file.save(os.path.join(upload_folder, evidence_filename))

            cursor.execute("""
                INSERT INTO contracts (
                    contract_number, line_user_id, customer_name, id_card, phone, 
                    product_name, imei, serial_number, color, capacity, total_amount, total_installments, installment_amount, due_day, status, evidence_file
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
                RETURNING id
            """, (
                contract_number, line_user_id, customer_name, id_card, phone, 
                product_name, imei, serial_number, color, capacity, total_amount, total_installments, installment_amount, due_day, evidence_filename
            ))
            
            contract_row = cursor.fetchone()
            contract_id = contract_row['id']

            for i in range(1, total_installments + 1):
                cursor.execute("""
                    INSERT INTO payments (contract_id, installment_no, amount, status)
                    VALUES (%s, %s, %s, 'pending')
                """, (contract_id, i, installment_amount))

            conn.commit()
            return jsonify({'message': 'บันทึกสัญญาสำเร็จ', 'contract_id': contract_id, 'contract_number': contract_number}), 201

        cursor.execute("SELECT * FROM contracts ORDER BY id DESC")
        contracts = cursor.fetchall()
        
        contract_list = []
        for c in contracts:
            c_dict = dict(c)
            c_dict = calculate_contract_overdue_status(cursor, c_dict)
            payments = c_dict['payments']

            c_dict['total_amount'] = float(c_dict['total_amount']) if c_dict['total_amount'] is not None else 0.0
            c_dict['installment_amount'] = float(c_dict['installment_amount']) if c_dict['installment_amount'] is not None else 0.0

            c_dict['due_day'] = c_dict.get('due_day', 5) or 5
            if c_dict.get('created_at'):
                c_dict['created_at_formatted'] = c_dict['created_at'].strftime('%d/%m/%Y %H:%M')
                c_dict['created_at'] = str(c_dict['created_at'])
            else:
                c_dict['created_at_formatted'] = '-'

            paid_count = sum(1 for p in payments if p['status'] == 'paid')
            remaining_count = c_dict['total_installments'] - paid_count
            remaining_amount = float(remaining_count * c_dict['installment_amount'])
            close_with_discount = remaining_amount * 0.85
            
            c_dict['paid_count'] = paid_count
            c_dict['remaining_count'] = remaining_count
            c_dict['remaining_amount'] = remaining_amount
            c_dict['close_with_discount'] = close_with_discount
            contract_list.append(c_dict)

        conn.commit()
        return jsonify(contract_list)
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@admin_bp.route('/api/contracts', methods=['GET', 'POST', 'PUT', 'DELETE'])
@admin_bp.route('/contracts', methods=['GET', 'POST', 'PUT', 'DELETE'])
def get_contracts_api():
    return process_contracts_api()

def process_contract_detail_api(contract_id):
    try:
        conn = get_db()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE id = %s", (contract_id,))
        contract = cursor.fetchone()

        if not contract:
            return jsonify({'error': 'ไม่พบข้อมูลสัญญา'}), 404

        if request.method in ['PUT', 'POST']:
            data = request.form if request.form else (request.get_json(silent=True) or {})
            line_user_id = (data.get('line_user_id') or '').strip()
            customer_name = (data.get('customer_name') or '').strip()
            id_card = (data.get('id_card') or '').strip()
            phone = (data.get('phone') or '').strip()
            product_name = (data.get('product_name') or '').strip()
            imei = (data.get('imei') or '').strip()
            serial_number = (data.get('serial_number') or '').strip()
            color = (data.get('color') or '').strip()
            capacity = (data.get('capacity') or '').strip()

            evidence_filename = contract.get('evidence_file')
            evidence_file = request.files.get('evidence_file') if request.files else None
            if evidence_file and evidence_file.filename:
                filename = werkzeug.utils.secure_filename(evidence_file.filename)
                timestamp_str = datetime.datetime.now(TH_TZ).strftime('%Y%m%d%H%M%S')
                evidence_filename = f"ev_{timestamp_str}_{filename}"
                upload_folder = os.path.join('static', 'uploads')
                os.makedirs(upload_folder, exist_ok=True)
                evidence_file.save(os.path.join(upload_folder, evidence_filename))

            changes = []
            if contract.get('customer_name') != customer_name:
                changes.append(f"- ชื่อ-นามสกุล: {contract.get('customer_name')} ➡️ {customer_name}")
            if contract.get('phone') != phone:
                changes.append(f"- เบอร์โทรศัพท์: {contract.get('phone')} ➡️ {phone}")
            if contract.get('id_card') != id_card:
                changes.append(f"- เลขบัตรประชาชน: {contract.get('id_card')} ➡️ {id_card}")
            if contract.get('product_name') != product_name:
                changes.append(f"- รุ่นสินค้า: {contract.get('product_name')} ➡️ {product_name}")
            if contract.get('imei') != imei:
                changes.append(f"- IMEI: {contract.get('imei')} ➡️ {imei}")
            if contract.get('serial_number') != serial_number:
                changes.append(f"- Serial Number: {contract.get('serial_number')} ➡️ {serial_number}")
            if contract.get('color') != color:
                changes.append(f"- สี: {contract.get('color')} ➡️ {color}")
            if contract.get('capacity') != capacity:
                changes.append(f"- ความจุ: {contract.get('capacity')} ➡️ {capacity}")
            if evidence_file and evidence_file.filename:
                changes.append(f"- อัปเดต/เพิ่มหลักฐานการทำสัญญาแล้ว")

            cursor.execute("""
                UPDATE contracts 
                SET line_user_id = %s, customer_name = %s, id_card = %s, phone = %s, product_name = %s, imei = %s, serial_number = %s, color = %s, capacity = %s, evidence_file = %s
                WHERE id = %s
            """, (line_user_id, customer_name, id_card, phone, product_name, imei, serial_number, color, capacity, evidence_filename, contract_id))

            conn.commit()

            target_line_user_id = line_user_id if line_user_id else contract.get('line_user_id')
            if target_line_user_id and changes:
                changes_text = "\n".join(changes)
                send_push_contract_updated(target_line_user_id, contract.get('contract_number'), product_name, changes_text)

            return jsonify({'message': 'แก้ไขสัญญาและจัดการหลักฐานสำเร็จ', 'contract_id': contract_id})

        elif request.method == 'DELETE':
            if contract.get('line_user_id'):
                send_push_cancellation(contract.get('line_user_id'), contract.get('contract_number'), contract.get('product_name'), contract.get('status'))

            cursor.execute("DELETE FROM contracts WHERE id = %s", (contract_id,))
            conn.commit()
            return jsonify({'message': 'ลบสัญญาสำเร็จและตรวจสอบเงื่อนไขแจ้งเตือนลูกค้าแล้ว', 'contract_id': contract_id})

        c_dict = dict(contract)
        c_dict = calculate_contract_overdue_status(cursor, c_dict)
        payments = c_dict['payments']

        c_dict['total_amount'] = float(c_dict['total_amount']) if c_dict['total_amount'] is not None else 0.0
        c_dict['installment_amount'] = float(c_dict['installment_amount']) if c_dict['installment_amount'] is not None else 0.0

        c_dict['due_day'] = c_dict.get('due_day', 5) or 5
        if c_dict.get('created_at'):
            c_dict['created_at_formatted'] = c_dict['created_at'].strftime('%d/%m/%Y %H:%M')
            c_dict['created_at'] = str(c_dict['created_at'])
        else:
            c_dict['created_at_formatted'] = '-'

        paid_count = sum(1 for p in payments if p['status'] == 'paid')
        remaining_count = c_dict['total_installments'] - paid_count
        remaining_amount = float(remaining_count * c_dict['installment_amount'])
        close_with_discount = remaining_amount * 0.85

        c_dict['paid_count'] = paid_count
        c_dict['remaining_count'] = remaining_count
        c_dict['remaining_amount'] = remaining_amount
        c_dict['close_with_discount'] = close_with_discount

        conn.commit()
        return jsonify(c_dict)
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@admin_bp.route('/api/contracts/<int:contract_id>', methods=['GET', 'PUT', 'POST', 'DELETE'])
@admin_bp.route('/contracts/<int:contract_id>', methods=['GET', 'PUT', 'POST', 'DELETE'])
def get_contract_detail_api(contract_id):
    return process_contract_detail_api(contract_id)

def process_payments_by_contract_api(contract_identifier):
    try:
        conn = get_db()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE contract_number = %s OR id::text = %s", (contract_identifier, contract_identifier))
        contract = cursor.fetchone()
        if not contract:
            return jsonify({'error': 'ไม่พบสัญญา'}), 404

        cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (contract['id'],))
        payments = [dict(p) for p in cursor.fetchall()]
        return jsonify(payments)
    finally:
        cursor.close()
        conn.close()

@admin_bp.route('/api/payments/<contract_identifier>', methods=['GET'])
@admin_bp.route('/payments/<contract_identifier>', methods=['GET'])
def get_payments_by_contract_api(contract_identifier):
    return process_payments_by_contract_api(contract_identifier)

def pay_contract_installment_api(contract_id):
    try:
        conn = get_db()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    now_str = datetime.datetime.now(TH_TZ).strftime('%Y-%m-%d %H:%M:%S')
    cursor = conn.cursor()
    try:
        data = request.get_json(silent=True) or request.form
        installment_no = data.get('installment_no') if data else None

        if installment_no:
            cursor.execute("SELECT * FROM payments WHERE contract_id = %s AND installment_no = %s", (contract_id, int(installment_no)))
        else:
            cursor.execute("SELECT * FROM payments WHERE contract_id = %s AND status != 'paid' ORDER BY installment_no ASC LIMIT 1", (contract_id,))

        payment = cursor.fetchone()

        if not payment:
            return jsonify({'error': 'ชำระเงินครบหมดแล้ว หรือไม่พบรายการงวด'}), 400

        receipt_no = f"REC-{contract_id}-{payment['installment_no']}-{datetime.datetime.now(TH_TZ).strftime('%M%S')}"
        cursor.execute("""
            UPDATE payments 
            SET status = 'paid', paid_at = %s, receipt_no = %s
            WHERE id = %s
        """, (now_str, receipt_no, payment['id']))

        conn.commit()

        cursor.execute("SELECT * FROM contracts WHERE id = %s", (contract_id,))
        contract = dict(cursor.fetchone())
        calculate_contract_overdue_status(cursor, contract)
        
        cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (contract_id,))
        all_payments = cursor.fetchall()

        paid_count = sum(1 for p in all_payments if p['status'] == 'paid')
        remaining_count = contract['total_installments'] - paid_count
        remaining_amount = float(remaining_count * contract['installment_amount'])

        if remaining_count == 0:
            cursor.execute("UPDATE contracts SET status = 'closed' WHERE id = %s", (contract_id,))
            conn.commit()
            send_push_thank_you(contract.get('line_user_id'), contract.get('contract_number'), contract.get('product_name'))

        return jsonify({
            'message': f'ชำระเงินงวดที่ {payment["installment_no"]} เรียบร้อยแล้ว',
            'payment_id': payment['id'],
            'paid_count': paid_count,
            'remaining_count': remaining_count,
            'remaining_amount': remaining_amount
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@admin_bp.route('/api/contracts/<int:contract_id>/pay', methods=['POST', 'PUT'])
@admin_bp.route('/contracts/<int:contract_id>/pay', methods=['POST', 'PUT'])
def handle_pay_contract_installment_api(contract_id):
    return pay_contract_installment_api(contract_id)

def unpay_contract_installment_api(contract_id):
    try:
        conn = get_db()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    cursor = conn.cursor()
    try:
        data = request.get_json(silent=True) or request.form
        installment_no = data.get('installment_no') if data else None

        if installment_no:
            cursor.execute("SELECT * FROM payments WHERE contract_id = %s AND installment_no = %s", (contract_id, int(installment_no)))
        else:
            cursor.execute("SELECT * FROM payments WHERE contract_id = %s AND status = 'paid' ORDER BY installment_no DESC LIMIT 1", (contract_id,))

        payment = cursor.fetchone()

        if not payment:
            return jsonify({'error': 'ยังไม่มีรายการที่ชำระเงิน หรือไม่พบงวดที่จะยกเลิก'}), 400

        cursor.execute("""
            UPDATE payments 
            SET status = 'pending', paid_at = NULL, receipt_no = NULL
            WHERE id = %s
        """, (payment['id'],))

        cursor.execute("UPDATE contracts SET status = 'active' WHERE id = %s AND status IN ('closed_early', 'closed', 'cancelled')", (contract_id,))

        conn.commit()

        cursor.execute("SELECT * FROM contracts WHERE id = %s", (contract_id,))
        contract = dict(cursor.fetchone())
        calculate_contract_overdue_status(cursor, contract)

        cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (contract_id,))
        all_payments = cursor.fetchall()

        paid_count = sum(1 for p in all_payments if p['status'] == 'paid')
        remaining_count = contract['total_installments'] - paid_count
        remaining_amount = float(remaining_count * contract['installment_amount'])

        return jsonify({
            'message': f'ยกเลิกการชำระงวดที่ {payment["installment_no"]} เรียบร้อยแล้ว',
            'paid_count': paid_count,
            'remaining_count': remaining_count,
            'remaining_amount': remaining_amount
        })
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@admin_bp.route('/api/contracts/<int:contract_id>/status', methods=['POST', 'PUT'])
@admin_bp.route('/contracts/<int:contract_id>/status', methods=['POST', 'PUT'])
def update_contract_status_api(contract_id):
    try:
        conn = get_db()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    cursor = conn.cursor()
    try:
        data = request.get_json(silent=True) or request.form
        status = data.get('status', 'active') if data else 'active'

        cursor.execute("UPDATE contracts SET status = %s WHERE id = %s", (status, contract_id))
        conn.commit()

        if status.lower() == 'cancelled':
            cursor.execute("SELECT * FROM contracts WHERE id = %s", (contract_id,))
            contract = cursor.fetchone()
            if contract:
                send_push_cancellation(contract.get('line_user_id'), contract.get('contract_number'), contract.get('product_name'), contract.get('status'))

        return jsonify({'message': 'อัปเดตสถานะสัญญาเรียบร้อยแล้ว', 'status': status})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@admin_bp.route('/api/contracts/<int:contract_id>/close_early', methods=['POST', 'PUT'])
@admin_bp.route('/contracts/<int:contract_id>/close_early', methods=['POST', 'PUT'])
def close_contract_early_api(contract_id):
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

        send_push_thank_you(contract.get('line_user_id'), contract.get('contract_number'), contract.get('product_name'))

        return jsonify({'message': 'บันทึกการปิดยอดสัญญาสำเร็จ'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()