# admin_routes.py (Part 1/2)
import os
import datetime
import calendar
import psycopg2
import werkzeug
import random
import string
from psycopg2.extras import RealDictCursor
from zoneinfo import ZoneInfo
from flask import Blueprint, render_template, request, redirect, url_for, jsonify
from linebot import LineBotApi
from linebot.models import TextSendMessage, FlexSendMessage

admin_bp = Blueprint('admin', __name__)
TH_TZ = ZoneInfo('Asia/Bangkok')

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_ACCESS_TOKEN')
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)

def generate_short_contract_number(cursor):
    """สร้างเลขสัญญาแบบสั้น: ตัวอักษร 3 ตัว + ตัวเลข 4 ตัว (เช่น ABC1234)"""
    for _ in range(1000):
        letters = ''.join(random.choices(string.ascii_uppercase, k=3))
        digits = ''.join(random.choices(string.digits, k=4))
        candidate = f"{letters}{digits}"
        cursor.execute("SELECT id FROM contracts WHERE contract_number = %s", (candidate,))
        if not cursor.fetchone():
            return candidate
    return f"CNT{random.randint(1000, 9999)}"

def build_flex_message_ui(title, body_text, contract_number="", product_name="", color="#0d6efd"):
    """ UI กล่องข้อความแบบเดียวกับเวลาส่งให้ลูกค้าตอนพิมพ์สัญญา (ใช้ UI เดียวกันในทุกๆ ข้อความ) """
    contents = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": color,
            "paddingAll": "xl",
            "contents": [
                {
                    "type": "text",
                    "text": title,
                    "weight": "bold",
                    "color": "#ffffff",
                    "size": "lg",
                    "wrap": True
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "paddingAll": "xl",
            "contents": []
        }
    }
    
    if contract_number or product_name:
        header_sub = []
        if contract_number:
            header_sub.append({"type": "text", "text": f"เลขที่สัญญา: {contract_number}", "weight": "bold", "color": "#ffffff", "size": "md", "margin": "xs"})
        if product_name:
            header_sub.append({"type": "text", "text": f"สินค้า: {product_name}", "color": "#e0e0e0", "size": "xs", "margin": "xs", "wrap": True})
        contents["header"]["contents"].extend(header_sub)

    contents["body"]["contents"].append({
        "type": "box",
        "layout": "vertical",
        "backgroundColor": "#f8f9fa",
        "cornerRadius": "md",
        "paddingAll": "md",
        "contents": [
            {
                "type": "text",
                "text": body_text,
                "size": "sm",
                "color": "#212529",
                "wrap": True
            }
        ]
    })
    return FlexSendMessage(alt_text=title, contents=contents)

def get_installment_due_date(created_at, due_day, installment_no):
    if isinstance(created_at, str):
        try:
            created_at = datetime.datetime.strptime(created_at[:19], '%Y-%m-%d %H:%M:%S')
        except Exception:
            created_at = datetime.datetime.now(TH_TZ)

    start_year = created_at.year
    start_month = created_at.month
    
    # แก้ไข: คำนวณงวดที่ 1 ให้เป็นเดือนถัดไปจากเดือนที่ทำสัญญาจริงทันที (งวดแรกเป็นเดือนถัดไป)
    target_month_index = (start_month - 1) + installment_no
    target_year = start_year + (target_month_index // 12)
    target_month = (target_month_index % 12) + 1

    last_day_of_month = calendar.monthrange(target_year, target_month)[1]
    actual_day = min(due_day, last_day_of_month)

    return datetime.date(target_year, target_month, actual_day)

def send_push_thank_you(user_id, contract_number, product_name):
    if not user_id:
        return
    try:
        body_text = "ท่านได้ดำเนินการชำระเงินและปิดยอดสัญญาครบถ้วนเรียบร้อยแล้ว ขอบคุณที่ไว้วางใจใช้บริการของเราครับ 🙏✨"
        flex_msg = build_flex_message_ui("🎉 ขอบคุณที่ใช้บริการ", body_text, contract_number, product_name, color="#198754")
        line_bot_api.push_message(user_id, flex_msg)
    except Exception as e:
        print(f"Error sending thank you message to {user_id}: {e}")

def send_push_cancellation(user_id, contract_number, product_name, contract_status="active"):
    if not user_id:
        return
    try:
        body_text = f"สัญญาเลขที่ {contract_number} ({product_name}) ของท่านได้รับการยกเลิก/ลบออกจากระบบหลังบ้านเรียบร้อยแล้ว หากมีข้อสงสัยเพิ่มเติมโปรดติดต่อเจ้าหน้าที่ครับ 🙏"
        flex_msg = build_flex_message_ui("🗑️ แจ้งเตือนการลบสัญญา", body_text, contract_number, product_name, color="#dc3545")
        line_bot_api.push_message(user_id, flex_msg)
    except Exception as e:
        print(f"Error sending cancellation message to {user_id}: {e}")

def send_push_contract_updated(user_id, contract_number, product_name, updated_fields_text):
    if not user_id:
        return
    try:
        body_text = f"ทางร้านได้ทำการแก้ไขข้อมูลสัญญาของคุณ มีรายละเอียดดังนี้:\n\n{updated_fields_text}\n\nหากมีข้อสงสัยประการใดสามารถติดต่อสอบถามเจ้าหน้าที่ได้เลยครับ 🙏"
        flex_msg = build_flex_message_ui("📝 แจ้งการอัปเดตข้อมูลสัญญา", body_text, contract_number, product_name, color="#0d6efd")
        line_bot_api.push_message(user_id, flex_msg)
    except Exception as e:
        print(f"Error sending contract update message to {user_id}: {e}")

def send_push_payment_notification(user_id, contract_number, product_name, installment_no, amount, paid_at):
    """ แจ้งเตือนเวลาลูกค้าชำระเงินเป็นรายงวดเข้า LINE ของลูกค้า """
    if not user_id:
        return
    try:
        body_text = f"ได้รับการชำระเงินค่างวดเรียบร้อยแล้วครับ\n\n🔢 งวดที่: {installment_no}\n💰 จำนวนเงิน: {amount:,.2f} บาท\n📅 วันที่ชำระ: {paid_at}\n\nขอบคุณที่ใช้บริการครับ 🙏✨"
        flex_msg = build_flex_message_ui("✅ แจ้งการชำระเงินค่างวดสำเร็จ", body_text, contract_number, product_name, color="#198754")
        line_bot_api.push_message(user_id, flex_msg)
    except Exception as e:
        print(f"Error sending payment notification to {user_id}: {e}")

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

    new_status = c_dict.get('status', 'active')
    if c_dict.get('status') not in ['closed', 'closed_early', 'cancelled']:
        if overdue_count >= 3:
            new_status = 'reclaim'
        elif overdue_count == 2:
            new_status = 'overdue_2'
        elif overdue_count == 1:
            new_status = 'overdue_1'
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
    # admin_routes.py (Part 2/2)

@admin_bp.route('/contract/<int:contract_id>/close-early', methods=['POST'])
@admin_bp.route('/api/contract/<int:contract_id>/close-early', methods=['POST'])
def close_contract_early(contract_id):
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

        now_time = datetime.datetime.now(TH_TZ)
        cursor.execute("""
            UPDATE payments 
            SET status = 'paid', paid_at = %s 
            WHERE contract_id = %s AND status = 'pending'
        """, (now_time, contract_id))

        cursor.execute("""
            UPDATE contracts 
            SET status = 'closed_early' 
            WHERE id = %s
        """, (contract_id,))

        conn.commit()

        send_push_thank_you(
            user_id=contract.get('line_user_id'),
            contract_number=contract.get('contract_number', ''),
            product_name=contract.get('product_name', '')
        )

        if request.path.startswith('/api/'):
            return jsonify({'message': 'ปิดสัญญาสดสำเร็จ'})
        return redirect(url_for('admin.index'))

    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@admin_bp.route('/contract/<int:contract_id>/delete', methods=['POST'])
@admin_bp.route('/api/contract/<int:contract_id>/delete', methods=['POST'])
def delete_contract(contract_id):
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

        cursor.execute("DELETE FROM payments WHERE contract_id = %s", (contract_id,))
        cursor.execute("DELETE FROM contracts WHERE id = %s", (contract_id,))

        conn.commit()

        send_push_cancellation(
            user_id=contract.get('line_user_id'),
            contract_number=contract.get('contract_number', ''),
            product_name=contract.get('product_name', ''),
            contract_status=contract.get('status', 'active')
        )

        if request.path.startswith('/api/'):
            return jsonify({'message': 'ลบสัญญาสำเร็จ'})
        return redirect(url_for('admin.index'))

    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@admin_bp.route('/contract/<int:contract_id>/edit', methods=['POST'])
@admin_bp.route('/api/contract/<int:contract_id>/edit', methods=['POST'])
def edit_contract(contract_id):
    try:
        conn = get_db()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE id = %s", (contract_id,))
        old_contract = cursor.fetchone()
        if not old_contract:
            return jsonify({'error': 'ไม่พบสัญญาที่ต้องการแก้ไข'}), 404

        data = request.form if request.form else (request.get_json(silent=True) or {})

        customer_name = (data.get('customer_name') or '').strip()
        id_card = (data.get('id_card') or '').strip()
        phone = (data.get('phone') or '').strip()
        product_name = (data.get('product_name') or '').strip()
        imei = (data.get('imei') or '').strip()
        serial_number = (data.get('serial_number') or '').strip()
        color = (data.get('color') or '').strip()
        capacity = (data.get('capacity') or '').strip()
        line_user_id = (data.get('line_user_id') or '').strip()

        raw_total = data.get('total_amount')
        raw_installments = data.get('total_installments')
        raw_installment_amount = data.get('installment_amount')

        total_installments = int(raw_installments) if raw_installments else old_contract['total_installments']
        
        if raw_installment_amount and float(raw_installment_amount) > 0:
            installment_amount = float(raw_installment_amount)
            total_amount = installment_amount * total_installments
        elif raw_total and float(raw_total) > 0:
            total_amount = float(raw_total)
            installment_amount = total_amount / total_installments if total_installments > 0 else 0.0
        else:
            total_amount = float(old_contract['total_amount'])
            installment_amount = float(old_contract['installment_amount'])

        changes = []
        if customer_name != old_contract['customer_name']:
            changes.append(f"• ชื่อ-นามสกุล: {old_contract['customer_name']} ➔ {customer_name}")
        if id_card != old_contract['id_card']:
            changes.append(f"• เลขบัตรประชาชน: {old_contract['id_card']} ➔ {id_card}")
        if phone != old_contract['phone']:
            changes.append(f"• เบอร์โทรศัพท์: {old_contract['phone']} ➔ {phone}")
        if product_name != old_contract['product_name']:
            changes.append(f"• สินค้า: {old_contract['product_name']} ➔ {product_name}")
        if imei != old_contract.get('imei'):
            changes.append(f"• IMEI: {old_contract.get('imei') or '-'} ➔ {imei}")
        if serial_number != old_contract.get('serial_number'):
            changes.append(f"• Serial Number: {old_contract.get('serial_number') or '-'} ➔ {serial_number}")
        if color != old_contract.get('color'):
            changes.append(f"• สี: {old_contract.get('color') or '-'} ➔ {color}")
        if capacity != old_contract.get('capacity'):
            changes.append(f"• ความจุ: {old_contract.get('capacity') or '-'} ➔ {capacity}")
        if float(total_amount) != float(old_contract['total_amount']):
            changes.append(f"• ยอดรวมสัญญา: {float(old_contract['total_amount']):,.2f} ➔ {float(total_amount):,.2f} บาท")
        if total_installments != old_contract['total_installments']:
            changes.append(f"• จำนวนงวด: {old_contract['total_installments']} ➔ {total_installments} งวด")
        if float(installment_amount) != float(old_contract['installment_amount']):
            changes.append(f"• ยอดผ่อนต่องวด: {float(old_contract['installment_amount']):,.2f} ➔ {float(installment_amount):,.2f} บาท")

        evidence_filename = old_contract.get('evidence_file')
        evidence_file = request.files.get('evidence_file') if request.files else None
        if evidence_file and evidence_file.filename:
            now_time = datetime.datetime.now(TH_TZ)
            filename = werkzeug.utils.secure_filename(evidence_file.filename)
            timestamp_str = now_time.strftime('%Y%m%d%H%M%S')
            evidence_filename = f"ev_{timestamp_str}_{filename}"
            upload_folder = os.path.join('static', 'uploads')
            os.makedirs(upload_folder, exist_ok=True)
            evidence_file.save(os.path.join(upload_folder, evidence_filename))
            changes.append("• หลักฐานประกอบสัญญา: อัปเดตไฟล์ใหม่เรียบร้อย")

        cursor.execute("""
            UPDATE contracts SET
                customer_name = %s,
                id_card = %s,
                phone = %s,
                product_name = %s,
                imei = %s,
                serial_number = %s,
                color = %s,
                capacity = %s,
                line_user_id = %s,
                total_amount = %s,
                total_installments = %s,
                installment_amount = %s,
                evidence_file = %s
            WHERE id = %s
        """, (
            customer_name, id_card, phone, product_name, imei, serial_number, color, capacity,
            line_user_id, total_amount, total_installments, installment_amount, evidence_filename, contract_id
        ))

        # หากจำนวนงวดมีการเปลี่ยนแปลง ปรับตารางผ่อนชำระ
        if total_installments != old_contract['total_installments'] or float(installment_amount) != float(old_contract['installment_amount']):
            cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (contract_id,))
            existing_payments = cursor.fetchall()
            existing_dict = {p['installment_no']: p for p in existing_payments}

            # อัปเดตค่างวดที่มีอยู่เดิม
            for i in range(1, min(total_installments, old_contract['total_installments']) + 1):
                if existing_dict.get(i) and existing_dict[i]['status'] == 'pending':
                    cursor.execute("""
                        UPDATE payments SET amount = %s WHERE contract_id = %s AND installment_no = %s
                    """, (installment_amount, contract_id, i))

            # หากเพิ่มจำนวนงวด
            if total_installments > old_contract['total_installments']:
                for i in range(old_contract['total_installments'] + 1, total_installments + 1):
                    cursor.execute("""
                        INSERT INTO payments (contract_id, installment_no, amount, status)
                        VALUES (%s, %s, %s, 'pending')
                    """, (contract_id, i, installment_amount))

            # หากลดจำนวนงวด (ลบงวดที่เกินเฉพาะที่ยังไม่ได้จ่าย)
            elif total_installments < old_contract['total_installments']:
                cursor.execute("""
                    DELETE FROM payments 
                    WHERE contract_id = %s AND installment_no > %s AND status = 'pending'
                """, (contract_id, total_installments))

        conn.commit()

        if changes and (line_user_id or old_contract.get('line_user_id')):
            updated_text = "\n".join(changes)
            target_user_id = line_user_id or old_contract.get('line_user_id')
            send_push_contract_updated(
                user_id=target_user_id,
                contract_number=old_contract.get('contract_number', ''),
                product_name=product_name,
                updated_fields_text=updated_text
            )

        if request.path.startswith('/api/'):
            return jsonify({'message': 'แก้ไขสัญญาเรียบร้อยแล้ว'})
        return redirect(url_for('admin.index'))

    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@admin_bp.route('/contract/<int:contract_id>/pay-installment', methods=['POST'])
@admin_bp.route('/api/contract/<int:contract_id>/pay-installment', methods=['POST'])
def pay_installment(contract_id):
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

        data = request.form if request.form else (request.get_json(silent=True) or {})
        installment_no = data.get('installment_no')

        if not installment_no:
            cursor.execute("""
                SELECT * FROM payments 
                WHERE contract_id = %s AND status = 'pending' 
                ORDER BY installment_no ASC LIMIT 1
            """, (contract_id,))
            payment = cursor.fetchone()
        else:
            cursor.execute("""
                SELECT * FROM payments 
                WHERE contract_id = %s AND installment_no = %s
            """, (contract_id, int(installment_no)))
            payment = cursor.fetchone()

        if not payment:
            return jsonify({'error': 'ไม่พบรายการงวดชำระที่รอดำเนินการ'}), 404

        now_time = datetime.datetime.now(TH_TZ)
        cursor.execute("""
            UPDATE payments 
            SET status = 'paid', paid_at = %s 
            WHERE id = %s
        """, (now_time, payment['id']))

        # ตรวจสอบว่าชำระครบทุกงวดหรือยัง
        cursor.execute("SELECT COUNT(*) as unpaid FROM payments WHERE contract_id = %s AND status = 'pending'", (contract_id,))
        unpaid_row = cursor.fetchone()
        
        if unpaid_row['unpaid'] == 0:
            cursor.execute("UPDATE contracts SET status = 'closed' WHERE id = %s", (contract_id,))
            send_push_thank_you(
                user_id=contract.get('line_user_id'),
                contract_number=contract.get('contract_number', ''),
                product_name=contract.get('product_name', '')
            )
        else:
            paid_at_str = now_time.strftime('%d/%m/%Y %H:%M น.')
            send_push_payment_notification(
                user_id=contract.get('line_user_id'),
                contract_number=contract.get('contract_number', ''),
                product_name=contract.get('product_name', ''),
                installment_no=payment['installment_no'],
                amount=float(payment['amount']),
                paid_at=paid_at_str
            )

        conn.commit()

        if request.path.startswith('/api/'):
            return jsonify({'message': f"บันทึกการชำระเงินงวดที่ {payment['installment_no']} สำเร็จ"})
        return redirect(url_for('admin.index'))

    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()