import os
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from zoneinfo import ZoneInfo
from flask import Blueprint, render_template, request, redirect, url_for, jsonify
from linebot import LineBotApi
from linebot.models import TextSendMessage

admin_bp = Blueprint('admin', __name__)
TH_TZ = ZoneInfo('Asia/Bangkok')

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_ACCESS_TOKEN')
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)

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

def get_db():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise ValueError("DATABASE_URL is not set")
    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    return conn

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
            cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (c['id'],))
            payments = [dict(p) for p in cursor.fetchall()]
            
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
            
            c_dict['payments'] = payments
            c_dict['paid_count'] = paid_count
            c_dict['remaining_count'] = remaining_count
            c_dict['remaining_amount'] = remaining_amount
            c_dict['close_with_discount'] = close_with_discount
            contract_list.append(c_dict)

        return render_template('admin.html', contracts=contract_list)
    except Exception as e:
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
        
        raw_total = request.form.get('total_amount', 0)
        raw_installments = request.form.get('total_installments', 0)
        raw_installment_amount = request.form.get('installment_amount', 0)
        raw_due_day = request.form.get('due_day', 5)

        total_installments = int(raw_installments) if raw_installments else 0
        due_day = int(raw_due_day) if raw_due_day else 5

        if raw_installment_amount and float(raw_installment_amount) > 0:
            installment_amount = float(raw_installment_amount)
            total_amount = installment_amount * total_installments
        else:
            total_amount = float(raw_total) if raw_total else 0.0
            installment_amount = total_amount / total_installments if total_installments > 0 else 0.0

        if total_installments <= 0 or total_amount <= 0 or installment_amount <= 0:
            return "กรุณากรอกยอดเงินรวม/ยอดต่องวด และจำนวนงวดให้ถูกต้อง", 400

        contract_number = f"CTR-{datetime.datetime.now(TH_TZ).strftime('%Y%m%d%H%M%S')}"

        cursor.execute("""
            INSERT INTO contracts (
                contract_number, line_user_id, customer_name, id_card, phone, 
                product_name, total_amount, total_installments, installment_amount, due_day, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
            RETURNING id
        """, (
            contract_number, line_user_id, customer_name, id_card, phone, 
            product_name, total_amount, total_installments, installment_amount, due_day
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
            data = request.get_json(silent=True) or request.form
            line_user_id = (data.get('line_user_id') or '').strip()
            customer_name = (data.get('customer_name') or '').strip()
            id_card = (data.get('id_card') or '').strip()
            phone = (data.get('phone') or '').strip()
            product_name = (data.get('product_name') or '').strip()
            
            raw_total = data.get('total_amount', 0)
            raw_installments = data.get('total_installments', 0)
            raw_installment_amount = data.get('installment_amount', 0)
            raw_due_day = data.get('due_day', 5)

            total_installments = int(raw_installments) if raw_installments else 0
            due_day = int(raw_due_day) if raw_due_day else 5

            if raw_installment_amount and float(raw_installment_amount) > 0:
                installment_amount = float(raw_installment_amount)
                total_amount = installment_amount * total_installments
            else:
                total_amount = float(raw_total) if raw_total else 0.0
                installment_amount = total_amount / total_installments if total_installments > 0 else 0.0

            if total_installments <= 0 or total_amount <= 0 or installment_amount <= 0:
                return jsonify({'error': 'กรุณากรอกยอดเงินรวม/ยอดต่องวด และจำนวนงวดให้ถูกต้อง'}), 400

            contract_number = f"CTR-{datetime.datetime.now(TH_TZ).strftime('%Y%m%d%H%M%S')}"

            cursor.execute("""
                INSERT INTO contracts (
                    contract_number, line_user_id, customer_name, id_card, phone, 
                    product_name, total_amount, total_installments, installment_amount, due_day, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
                RETURNING id
            """, (
                contract_number, line_user_id, customer_name, id_card, phone, 
                product_name, total_amount, total_installments, installment_amount, due_day
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
            data = request.get_json(silent=True) or request.form
            line_user_id = (data.get('line_user_id') or '').strip()
            customer_name = (data.get('customer_name') or '').strip()
            id_card = (data.get('id_card') or '').strip()
            phone = (data.get('phone') or '').strip()
            product_name = (data.get('product_name') or '').strip()
            due_day = int(data.get('due_day', 5))

            cursor.execute("""
                UPDATE contracts 
                SET line_user_id = %s, customer_name = %s, id_card = %s, phone = %s, product_name = %s, due_day = %s
                WHERE id = %s
            """, (line_user_id, customer_name, id_card, phone, product_name, due_day, contract_id))

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
        contract = cursor.fetchone()
        cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (contract_id,))
        all_payments = cursor.fetchall()

        paid_count = sum(1 for p in all_payments if p['status'] == 'paid')
        remaining_count = contract['total_installments'] - paid_count
        remaining_amount = float(remaining_count * contract['installment_amount'])

        if remaining_count == 0:
            cursor.execute("UPDATE contracts SET status = 'closed' WHERE id = %s", (contract_id,))
            conn.commit()
            # ส่งข้อความขอบคุณเนื่องจากจ่ายครบทุกงวดตามสัญญา
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

        cursor.execute("UPDATE contracts SET status = 'active' WHERE id = %s AND status IN ('closed_early', 'closed')", (contract_id,))

        conn.commit()

        cursor.execute("SELECT * FROM contracts WHERE id = %s", (contract_id,))
        contract = cursor.fetchone()
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

        # ส่งข้อความขอบคุณไปยัง LINE ของลูกค้า
        send_push_thank_you(contract.get('line_user_id'), contract.get('contract_number'), contract.get('product_name'))

        return jsonify({'message': 'บันทึกการปิดยอดสัญญาสำเร็จ'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()