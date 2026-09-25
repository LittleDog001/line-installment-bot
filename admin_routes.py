import sqlite3
from flask import Blueprint, request, render_template, jsonify

admin_bp = Blueprint('admin', __name__)
DATABASE = "database.db"

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

# --- Admin Page UI ---
@admin_bp.route("/admin")
def admin_page():
    return render_template("admin.html")

# --- Admin APIs ---
@admin_bp.route("/api/contracts", methods=["GET"])
def get_contracts():
    search = request.args.get("search", "")
    with get_db() as conn:
        cursor = conn.cursor()
        if search:
            query = "%" + search + "%"
            cursor.execute("""
                SELECT * FROM contracts 
                WHERE contract_number LIKE ? OR customer_name LIKE ? OR phone LIKE ? 
                ORDER BY id DESC
            """, (query, query, query))
        else:
            cursor.execute("SELECT * FROM contracts ORDER BY id DESC")
        rows = cursor.fetchall()
        return jsonify([dict(r) for r in rows])

@admin_bp.route("/api/contracts", methods=["POST"])
def add_contract():
    data = request.json
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO contracts 
                (contract_number, customer_name, phone, product_name, total_amount, monthly_amount, total_installments, paid_installments)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data["contract_number"],
                data["customer_name"],
                data["phone"],
                data["product_name"],
                float(data["total_amount"]),
                float(data["monthly_amount"]),
                int(data["total_installments"]),
                int(data.get("paid_installments", 0))
            ))
            conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@admin_bp.route("/api/contracts/<int:contract_id>/pay", methods=["POST"])
def admin_pay_installment(contract_id):
    data = request.json or {}
    delta = data.get("delta", 1) # 1 หรือ -1
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,))
        c = cursor.fetchone()
        if not c:
            return jsonify({"success": False, "error": "Not found"}), 404
            
        new_paid = max(0, min(c["total_installments"], c["paid_installments"] + delta))
        cursor.execute("UPDATE contracts SET paid_installments = ? WHERE id = ?", (new_paid, contract_id))
        
        # บันทึกประวัติถ้าเป็นการเพิ่มงวด
        if delta > 0:
            cursor.execute("""
                INSERT INTO payments (contract_number, amount, installment_no)
                VALUES (?, ?, ?)
            """, (c["contract_number"], c["monthly_amount"], new_paid))
            
        conn.commit()
    return jsonify({"success": True, "new_paid": new_paid})

@admin_bp.route("/api/contracts/<int:contract_id>/status", methods=["POST"])
def update_contract_status(contract_id):
    data = request.json
    status = data.get("status", "ACTIVE")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE contracts SET status = ? WHERE id = ?", (status, contract_id))
        conn.commit()
    return jsonify({"success": True})

@admin_bp.route("/api/contracts/<int:contract_id>", methods=["DELETE"])
def delete_contract(contract_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM contracts WHERE id = ?", (contract_id,))
        conn.commit()
    return jsonify({"success": True})

@admin_bp.route("/api/payments/<contract_number>", methods=["GET"])
def get_payment_history(contract_number):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM payments WHERE contract_number = ? ORDER BY paid_at DESC", (contract_number,))
        rows = cursor.fetchall()
        return jsonify([dict(r) for r in rows])