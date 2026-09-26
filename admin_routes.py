import os
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from zoneinfo import ZoneInfo
from flask import Blueprint, render_template, request, redirect, url_for, jsonify

admin_bp = Blueprint('admin', __name__)
DATABASE_URL = os.environ.get('DATABASE_URL')
TH_TZ = ZoneInfo('Asia/Bangkok')

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

@admin_bp.route('/')
def index():
    if not DATABASE_URL:
        return "DATABASE_URL is not set", 500

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts ORDER BY id DESC")
        contracts = cursor.fetchall()
        
        contract_list = []
        for c in contracts:
            c_dict = dict(c)
            cursor.execute("SELECT * FROM payments WHERE contract_id = %s ORDER BY installment_no ASC", (c['id'],))
            payments = cursor.fetchall()
            
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

        return render_template('admin.html', contracts=contract_list)
    finally:
        cursor.close()
        conn.close()

@admin_bp.route('/contract/create', methods=['POST'])
def create_contract():
    if not DATABASE_URL:
        return "DATABASE_URL is not set", 500

    conn = get_db()
    cursor = conn.cursor()
    try:
        line_user_id = (request.form.get('line_user_id') or '').strip()
        customer_name = (request.form.get('customer_name') or '').strip()
        id_card = (request.form.get('id_card') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        product_name = (request.form.get('product_name') or '').strip()
        
        raw_total = request.form.get('total_amount', 0)
        raw_installments = request.form.get('total_installments', 0)

        total_amount = float(raw_total) if raw_total else 0.0
        total_installments = int(raw_installments) if raw_installments else 0

        if total_installments <= 0 or total_amount <= 0:
            return "กรุณากรอกยอดเงินรวมและจำนวนงวดให้ถูกต้อง (ต้องมากกว่า 0)", 400

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
        return redirect(url_for('admin.index'))

    except Exception as e:
        conn.rollback()
        return f"เกิดข้อผิดพลาดในการบันทึกสัญญา: {str(e)}", 500
    finally:
        cursor.close()
        conn.close()

@admin_bp.route('/payment/<int:payment_id>/confirm', methods=['POST'])
def confirm_payment(payment_id):
    now_str = datetime.datetime.now(TH_TZ).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM payments WHERE id = %s", (payment_id,))
        payment = cursor.fetchone()
        
        if payment:
            receipt_no = f"REC-{payment['contract_id']}-{payment['installment_no']}-{datetime.datetime.now(TH_TZ).strftime('%M%S')}"
            cursor.execute("""
                UPDATE payments 
                SET status = 'paid', paid_at = %s, receipt_no = %s
                WHERE id = %s
            """, (now_str, receipt_no, payment_id))
            conn.commit()

        return redirect(url_for('admin.index'))
    finally:
        cursor.close()
        conn.close()

@admin_bp.route('/contract/<int:contract_id>/close_early', methods=['POST'])
def close_contract_early(contract_id):
    now_str = datetime.datetime.now(TH_TZ).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM contracts WHERE id = %s", (contract_id,))
        contract = cursor.fetchone()
        
        if contract:
            cursor.execute("SELECT * FROM payments WHERE contract_id = %s", (contract_id,))
            payments = cursor.fetchall()
            unpaid_payments = [p for p in payments if p['status'] != 'paid']
            
            for p in unpaid_payments:
                receipt_no = f"REC-EARLY-{contract_id}-{p['installment_no']}-{datetime.datetime.now(TH_TZ).strftime('%M%S')}"
                cursor.execute("""
                    UPDATE payments 
                    SET status = 'paid', paid_at = %s, receipt_no = %s
                    WHERE id = %s
                """, (now_str, receipt_no, p['id']))
            
            cursor.execute("UPDATE contracts SET status = 'closed_early' WHERE id = %s", (contract_id,))
            conn.commit()

        return redirect(url_for('admin.index'))
    finally:
        cursor.close()
        conn.close()

@admin_bp.route('/api/contracts', methods=['GET', 'POST', 'PUT', 'DELETE'])
def get_contracts_api():
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

@admin_bp.route('/contracts', methods=['GET', 'POST', 'PUT', 'DELETE'])
def get_contracts_api_alt():
    return get_contracts_api()