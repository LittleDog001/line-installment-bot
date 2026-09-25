from flask import Blueprint, request, jsonify
import sqlite3
import os

admin_bp = Blueprint('admin_bp', __name__)
DATABASE = os.path.join(os.path.dirname(__file__), 'database.db')

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

# --- API ดึงรายการสัญญาทั้งหมด ---
@admin_bp.route('/api/contracts', methods=['GET'])
def get_contracts():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contracts ORDER BY id DESC")
        contracts = [dict(row) for row in cursor.fetchall()]
    return jsonify(contracts)

# --- API ดึงสัญญาตาม ID ---
@admin_bp.route('/api/contracts/<int:contract_id>', methods=['GET'])
def get_contract_by_id(contract_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,))
        contract = cursor.fetchone()
    if not contract:
        return jsonify({"message": "ไม่พบสัญญา"}), 404
    return jsonify(dict(contract))

# --- API เพิ่มสัญญาใหม่ ---
@admin_bp.route('/api/contracts', methods=['POST'])
def add_contract():
    data = request.json
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO contracts (contract_number, customer_name, id_card, phone, product_name, total_amount, monthly_amount, total_installments)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                data['contract_number'],
                data['customer_name'],
                data.get('id_card', ''),
                data['phone_number'],
                data['phone_model'],
                float(data['total_amount']),
                float(data['monthly_payment']),
                int(data['total_installments'])
            ))
            conn.commit()
        return jsonify({"message": "เพิ่มสัญญาสำเร็จ"}), 201
    except sqlite3.IntegrityError:
        return jsonify({"message": "เลขที่สัญญานี้มีอยู่ในระบบแล้ว"}), 400
    except Exception as e:
        return jsonify({"message": str(e)}), 500

# --- API แก้ไขสัญญา ---
@admin_bp.route('/api/contracts/<int:contract_id>', methods=['PUT'])
def update_contract(contract_id):
    data = request.json
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE contracts 
                SET contract_number = ?, customer_name = ?, id_card = ?, phone = ?, product_name = ?, total_amount = ?, monthly_amount = ?, total_installments = ?
                WHERE id = ?
            ''', (
                data['contract_number'],
                data['customer_name'],
                data.get('id_card', ''),
                data['phone_number'],
                data['phone_model'],
                float(data['total_amount']),
                float(data['monthly_payment']),
                int(data['total_installments']),
                contract_id
            ))
            conn.commit()
        return jsonify({"message": "แก้ไขสัญญาสำเร็จ"})
    except Exception as e:
        return jsonify({"message": str(e)}), 500

# --- API อัปเดตงวดชำระ (+1 / -1) ---
@admin_bp.route('/api/contracts/<int:contract_id>/pay', methods=['POST'])
def update_payment(contract_id):
    action = request.json.get('action')
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,))
        contract = cursor.fetchone()
        
        if not contract:
            return jsonify({"message": "ไม่พบสัญญา"}), 404
            
        current_paid = contract['paid_installments']
        total = contract['total_installments']
        c_num = contract['contract_number']
        monthly = contract['monthly_amount']
        
        if action == 'add':
            if current_paid >= total:
                return jsonify({"message": "ผ่อนชำระครบแล้ว"}), 400
            new_paid = current_paid + 1
            cursor.execute("UPDATE contracts SET paid_installments = ? WHERE id = ?", (new_paid, contract_id))
            cursor.execute("INSERT INTO payments (contract_number, amount, installment_no) VALUES (?, ?, ?)", (c_num, monthly, new_paid))
            
        elif action == 'subtract':
            if current_paid <= 0:
                return jsonify({"message": "จำนวนงวดน้อยที่สุดแล้ว"}), 400
            new_paid = current_paid - 1
            cursor.execute("UPDATE contracts SET paid_installments = ? WHERE id = ?", (new_paid, contract_id))
            cursor.execute("DELETE FROM payments WHERE id = (SELECT MAX(id) FROM payments WHERE contract_number = ?)", (c_num,))
            
        conn.commit()
    return jsonify({"message": "อัปเดตงวดสำเร็จ"})

# --- API เปลี่ยนสถานะสัญญา ---
@admin_bp.route('/api/contracts/<int:contract_id>/status', methods=['POST'])
def update_status(contract_id):
    status = request.json.get('status')
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE contracts SET status = ? WHERE id = ?", (status, contract_id))
        conn.commit()
    return jsonify({"message": "อัปเดตสถานะสำเร็จ"})

# --- API ลบสัญญา ---
@admin_bp.route('/api/contracts/<int:contract_id>', methods=['DELETE'])
def delete_contract(contract_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT contract_number FROM contracts WHERE id = ?", (contract_id,))
        contract = cursor.fetchone()
        if contract:
            c_num = contract['contract_number']
            cursor.execute("DELETE FROM payments WHERE contract_number = ?", (c_num,))
            cursor.execute("DELETE FROM contracts WHERE id = ?", (contract_id,))
            conn.commit()
    return jsonify({"message": "ลบสัญญาเรียบร้อย"})

# --- API ดึงประวัติชำระเงิน ---
@admin_bp.route('/api/payments/<contract_number>', methods=['GET'])
def get_payments(contract_number):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM payments WHERE contract_number = ? ORDER BY id DESC", (contract_number,))
        payments = [dict(row) for row in cursor.fetchall()]
    return jsonify(payments)