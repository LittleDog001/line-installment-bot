from flask import Blueprint, render_template, request, jsonify
import sqlite3
import datetime

admin_bp = Blueprint('admin', __name__)

DATABASE = "database.db"

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

@admin_bp.route('/admin')
def admin_page():
    return render_template('admin.html')

@admin_bp.route('/api/contracts', methods=['GET'])
def get_contracts():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM contracts ORDER BY id DESC')
        contracts = cursor.fetchall()
    return jsonify([dict(row) for row in contracts])

@admin_bp.route('/api/contracts', methods=['POST'])
def add_contract():
    data = request.json
    try:
        phone_val = data.get('phone_number') or data.get('phone')
        product_val = data.get('phone_model') or data.get('product_name')
        monthly_val = data.get('monthly_payment') or data.get('monthly_amount')

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO contracts (contract_number, customer_name, phone, product_name, total_amount, monthly_amount, total_installments, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
            ''', (
                data['contract_number'],
                data['customer_name'],
                phone_val,
                product_val,
                float(data['total_amount']),
                float(monthly_val),
                int(data['total_installments'])
            ))
            conn.commit()
        return jsonify({'status': 'success', 'message': 'เพิ่มสัญญาเรียบร้อย'})
    except sqlite3.IntegrityError:
        return jsonify({'status': 'error', 'message': 'เลขที่สัญญานี้มีในระบบแล้ว'}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@admin_bp.route('/api/contracts/<int:contract_id>', methods=['GET'])
def get_single_contract(contract_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM contracts WHERE id = ?', (contract_id,))
        contract = cursor.fetchone()
    if contract:
        return jsonify(dict(contract))
    return jsonify({'status': 'error', 'message': 'ไม่พบสัญญา'}), 404

@admin_bp.route('/api/contracts/<int:contract_id>', methods=['PUT'])
def update_contract(contract_id):
    data = request.json
    phone_val = data.get('phone_number') or data.get('phone')
    product_val = data.get('phone_model') or data.get('product_name')
    monthly_val = data.get('monthly_payment') or data.get('monthly_amount')

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE contracts 
            SET contract_number = ?, customer_name = ?, phone = ?, product_name = ?, 
                total_amount = ?, monthly_amount = ?, total_installments = ?
            WHERE id = ?
        ''', (
            data['contract_number'],
            data['customer_name'],
            phone_val,
            product_val,
            float(data['total_amount']),
            float(monthly_val),
            int(data['total_installments']),
            contract_id
        ))
        conn.commit()
    return jsonify({'status': 'success', 'message': 'อัปเดตสัญญาเรียบร้อย'})

@admin_bp.route('/api/contracts/<int:contract_id>/pay', methods=['POST'])
def update_installment(contract_id):
    action = request.json.get('action') # 'add' or 'subtract'
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM contracts WHERE id = ?', (contract_id,))
        contract = cursor.fetchone()
        
        if not contract:
            return jsonify({'status': 'error', 'message': 'ไม่พบสัญญา'}), 404
            
        current_paid = contract['paid_installments']
        
        if action == 'add':
            if current_paid >= contract['total_installments']:
                return jsonify({'status': 'error', 'message': 'ชำระครบทุกงวดแล้ว'}), 400
            new_paid = current_paid + 1
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('''
                INSERT INTO payments (contract_number, installment_no, amount, paid_at)
                VALUES (?, ?, ?, ?)
            ''', (contract['contract_number'], new_paid, contract['monthly_amount'], now))
        elif action == 'subtract':
            if current_paid <= 0:
                return jsonify({'status': 'error', 'message': 'จำนวนงวดชำระเป็น 0 อยู่แล้ว'}), 400
            new_paid = current_paid - 1
            cursor.execute('''
                DELETE FROM payments 
                WHERE id = (SELECT id FROM payments WHERE contract_number = ? ORDER BY id DESC LIMIT 1)
            ''', (contract['contract_number'],))
        else:
            return jsonify({'status': 'error', 'message': 'คำสั่งไม่ถูกต้อง'}), 400

        cursor.execute('UPDATE contracts SET paid_installments = ? WHERE id = ?', (new_paid, contract_id))
        conn.commit()
    return jsonify({'status': 'success', 'paid_installments': new_paid})

@admin_bp.route('/api/contracts/<int:contract_id>/status', methods=['POST'])
def change_status(contract_id):
    new_status = request.json.get('status')
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE contracts SET status = ? WHERE id = ?', (new_status, contract_id))
        conn.commit()
    return jsonify({'status': 'success', 'message': 'เปลี่ยนสถานะเรียบร้อย'})

@admin_bp.route('/api/contracts/<int:contract_id>', methods=['DELETE'])
def delete_contract(contract_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT contract_number FROM contracts WHERE id = ?', (contract_id,))
        contract = cursor.fetchone()
        if contract:
            cursor.execute('DELETE FROM payments WHERE contract_number = ?', (contract['contract_number'],))
            cursor.execute('DELETE FROM contracts WHERE id = ?', (contract_id,))
            conn.commit()
    return jsonify({'status': 'success', 'message': 'ลบสัญญาเรียบร้อย'})

@admin_bp.route('/api/payments/<contract_number>', methods=['GET'])
def get_payment_history(contract_number):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM payments WHERE contract_number = ? ORDER BY id DESC', (contract_number,))
        history = cursor.fetchall()
    return jsonify([dict(row) for row in history])