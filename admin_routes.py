import sqlite3
import datetime
import pytz
from flask import Blueprint, render_template, request, redirect, url_for, jsonify

admin_bp = Blueprint('admin', __name__)
DATABASE = 'database.db'
TH_TZ = pytz.timezone('Asia/Bangkok')

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

@admin_bp.route('/')
def index():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contracts ORDER BY id DESC")
        contracts = cursor.fetchall()
        
        contract_list = []
        for c in contracts:
            c_dict = dict(c)
            cursor.execute("SELECT * FROM payments WHERE contract_id = ? ORDER BY installment_no ASC", (c['id'],))
            payments = cursor.fetchall()
            
            paid_count = sum(1 for p in payments if p['status'] == 'paid')
            remaining_count = c['total_installments'] - paid_count
            remaining_amount = remaining_count * c['installment_amount']
            close_with_discount = remaining_amount * 0.85
            
            c_dict['payments'] = payments
            c_dict['paid_count'] = paid_count
            c_dict['remaining_count'] = remaining_count
            c_dict['remaining_amount'] = remaining_amount
            c_dict['close_with_discount'] = close_with_discount
            contract_list.append(c_dict)

    return render_template('admin.html', contracts=contract_list)

@admin_bp.route('/contract/create', methods=['POST'])
def create_contract():
    line_user_id = request.form.get('line_user_id')
    customer_name = request.form.get('customer_name')
    id_card = request.form.get('id_card')
    phone = request.form.get('phone')
    product_name = request.form.get('product_name')
    total_amount = float(request.form.get('total_amount'))
    total_installments = int(request.form.get('total_installments'))
    installment_amount = total_amount / total_installments

    # เวลาปัจจุบันไทย
    contract_number = f"CTR-{datetime.datetime.now(TH_TZ).strftime('%Y%m%d%H%M%S')}"

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO contracts (contract_number, line_user_id, customer_name, id_card, phone, product_name, total_amount, total_installments, installment_amount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (contract_number, line_user_id, customer_name, id_card, phone, product_name, total_amount, total_installments, installment_amount))
        
        contract_id = cursor.lastrowid

        for i in range(1, total_installments + 1):
            cursor.execute("""
                INSERT INTO payments (contract_id, installment_no, amount, status)
                VALUES (?, ?, ?, 'pending')
            """, (contract_id, i, installment_amount))

        conn.commit()

    return redirect(url_for('admin.index'))

@admin_bp.route('/payment/<int:payment_id>/confirm', methods=['POST'])
def confirm_payment(payment_id):
    # บันทึกเวลาชำระเงินตรงกับเวลาจริงประเทศไทย
    now_str = datetime.datetime.now(TH_TZ).strftime('%Y-%m-%d %H:%M:%S')
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
        payment = cursor.fetchone()
        
        if payment:
            receipt_no = f"REC-{payment['contract_id']}-{payment['installment_no']}-{datetime.datetime.now(TH_TZ).strftime('%M%S')}"
            cursor.execute("""
                UPDATE payments 
                SET status = 'paid', paid_at = ?, receipt_no = ?
                WHERE id = ?
            """, (now_str, receipt_no, payment_id))
            conn.commit()

    return redirect(url_for('admin.index'))

@admin_bp.route('/contract/<int:contract_id>/close_early', methods=['POST'])
def close_contract_early(contract_id):
    # บันทึกเวลาชำระเงินตรงกับเวลาจริงประเทศไทย
    now_str = datetime.datetime.now(TH_TZ).strftime('%Y-%m-%d %H:%M:%S')
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,))
        contract = cursor.fetchone()
        
        if contract:
            cursor.execute("SELECT * FROM payments WHERE contract_id = ?", (contract_id,))
            payments = cursor.fetchall()
            unpaid_payments = [p for p in payments if p['status'] != 'paid']
            
            for p in unpaid_payments:
                receipt_no = f"REC-EARLY-{contract_id}-{p['installment_no']}-{datetime.datetime.now(TH_TZ).strftime('%M%S')}"
                cursor.execute("""
                    UPDATE payments 
                    SET status = 'paid', paid_at = ?, receipt_no = ?
                    WHERE id = ?
                """, (now_str, receipt_no, p['id']))
            
            cursor.execute("UPDATE contracts SET status = 'closed_early' WHERE id = ?", (contract_id,))
            conn.commit()

    return redirect(url_for('admin.index'))