from flask import Blueprint, render_template, request, jsonify
import sqlite3
import datetime

admin_bp = Blueprint('admin', __name__)

def get_db_connection():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn

@admin_bp.route('/admin')
def admin_page():
    return render_template('admin.html')

@admin_bp.route('/api/contracts', methods=['GET'])
def get_contracts():
    conn = get_db_connection()
    contracts = conn.execute('SELECT * FROM contracts ORDER BY id DESC').fetchall()
    conn.close()
    return jsonify([dict(row) for row in contracts])

@admin_bp.route('/api/contracts', methods=['POST'])
def add_contract():
    data = request.json
    conn = get_db_connection()
    try:
        conn.execute('''
            INSERT INTO contracts (contract_number, customer_name, phone_number, phone_model, total_amount, monthly_payment, total_installments, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'Active')
        ''', (
            data['contract_number'],
            data['customer_name'],
            data['phone_number'],
            data['phone_model'],
            float(data['total_amount']),
            float(data['monthly_payment']),
            int(data['total_installments'])
        ))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': 'เพิ่มสัญญาเรียบร้อย'})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'status': 'error', 'message': 'เลขที่สัญญานี้มีในระบบแล้ว'}), 400

@admin_bp.route('/api/contracts/<int:contract_id>', methods=['GET'])
def get_single_contract(contract_id):
    conn = get_db_connection()
    contract = conn.execute('SELECT * FROM contracts WHERE id = ?', (contract_id,)).fetchone()
    conn.close()
    if contract:
        return jsonify(dict(contract))
    return jsonify({'status': 'error', 'message': 'ไม่พบสัญญา'}), 404

@admin_bp.route('/api/contracts/<int:contract_id>', methods=['PUT'])
def update_contract(contract_id):
    data = request.json
    conn = get_db_connection()
    conn.execute('''
        UPDATE contracts 
        SET contract_number = ?, customer_name = ?, phone_number = ?, phone_model = ?, 
            total_amount = ?, monthly_payment = ?, total_installments = ?
        WHERE id = ?
    ''', (
        data['contract_number'],
        data['customer_name'],
        data['phone_number'],
        data['phone_model'],
        float(data['total_amount']),
        float(data['monthly_payment']),
        int(data['total_installments']),
        contract_id
    ))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'message': 'อัปเดตสัญญาเรียบร้อย'})

@admin_bp.route('/api/contracts/<int:contract_id>/pay', methods=['POST'])
def update_installment(contract_id):
    action = request.json.get('action') # 'add' or 'subtract'
    conn = get_db_connection()
    contract = conn.execute('SELECT * FROM contracts WHERE id = ?', (contract_id,)).fetchone()
    
    if not contract:
        conn.close()
        return jsonify({'status': 'error', 'message': 'ไม่พบสัญญา'}), 404
        
    current_paid = contract['paid_installments']
    
    if action == 'add':
        if current_paid >= contract['total_installments']:
            conn.close()
            return jsonify({'status': 'error', 'message': 'ชำระครบทุกงวดแล้ว'}), 400
        new_paid = current_paid + 1
        # บันทึกประวัติ
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute('''
            INSERT INTO payment_history (contract_number, installment_no, amount, paid_at)
            VALUES (?, ?, ?, ?)
        ''', (contract['contract_number'], new_paid, contract['monthly_payment'], now))
    elif action == 'subtract':
        if current_paid <= 0:
            conn.close()
            return jsonify({'status': 'error', 'message': 'จำนวนงวดชำระเป็น 0 อยู่แล้ว'}), 400
        new_paid = current_paid - 1
        # ลบประวัติด้านบนสุด 1 รายการ
        conn.execute('''
            DELETE FROM payment_history 
            WHERE id = (SELECT id FROM payment_history WHERE contract_number = ? ORDER BY id DESC LIMIT 1)
        ''', (contract['contract_number'],))
    else:
        conn.close()
        return jsonify({'status': 'error', 'message': 'คำสั่งไม่ถูกต้อง'}), 400

    conn.execute('UPDATE contracts SET paid_installments = ? WHERE id = ?', (new_paid, contract_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'paid_installments': new_paid})

@admin_bp.route('/api/contracts/<int:contract_id>/status', methods=['POST'])
def change_status(contract_id):
    new_status = request.json.get('status')
    conn = get_db_connection()
    conn.execute('UPDATE contracts SET status = ? WHERE id = ?', (new_status, contract_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'message': 'เปลี่ยนสถานะเรียบร้อย'})

@admin_bp.route('/api/contracts/<int:contract_id>', methods=['DELETE'])
def delete_contract(contract_id):
    conn = get_db_connection()
    contract = conn.execute('SELECT contract_number FROM contracts WHERE id = ?', (contract_id,)).fetchone()
    if contract:
        conn.execute('DELETE FROM payment_history WHERE contract_number = ?', (contract['contract_number'],))
        conn.execute('DELETE FROM contracts WHERE id = ?', (contract_id,))
        conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'message': 'ลบสัญญาเรียบร้อย'})

@admin_bp.route('/api/payments/<contract_number>', methods=['GET'])
def get_payment_history(contract_number):
    conn = get_db_connection()
    history = conn.execute('SELECT * FROM payment_history WHERE contract_number = ? ORDER BY id DESC', (contract_number,)).fetchall()
    conn.close()
    return jsonify([dict(row) for row in history])