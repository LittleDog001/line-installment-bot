import os
import sqlite3
import io
from flask import Flask, request, abort, render_template, jsonify, send_file
import qrcode

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    ImageMessage,
    FlexMessage,
    FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from admin_routes import admin_bp

app = Flask(__name__)

app.register_blueprint(admin_bp)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "YOUR_CHANNEL_SECRET")
PROMPTPAY_ID = os.getenv("PROMPTPAY_ID", "0812345678")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

DATABASE = os.path.join(os.path.dirname(__file__), 'database.db')

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contracts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_number TEXT UNIQUE NOT NULL,
                customer_name TEXT NOT NULL,
                id_card TEXT,
                phone TEXT NOT NULL,
                line_user_id TEXT,
                product_name TEXT NOT NULL,
                total_amount REAL NOT NULL,
                monthly_amount REAL NOT NULL,
                total_installments INTEGER NOT NULL,
                paid_installments INTEGER DEFAULT 0,
                status TEXT DEFAULT 'ACTIVE',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # ตรวจสอบว่าคอลัมน์ id_card มีอยู่ในฐานข้อมูลเก่าหรือยัง ถ้ายังไม่มีให้เพิ่มอัตโนมัติ
        cursor.execute("PRAGMA table_info(contracts)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'id_card' not in columns:
            cursor.execute("ALTER TABLE contracts ADD COLUMN id_card TEXT")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_number TEXT NOT NULL,
                amount REAL NOT NULL,
                installment_no INTEGER NOT NULL,
                paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

init_db()

@app.route('/')
@app.route('/admin')
def admin_page():
    return render_template('admin.html')

# --- Route แสดงใบเสร็จรับเงิน (Bill) ---
@app.route('/bill/<int:payment_id>')
def view_bill(payment_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT p.*, c.customer_name, c.phone, c.product_name, c.total_installments 
            FROM payments p
            JOIN contracts c ON p.contract_number = c.contract_number
            WHERE p.id = ?
        ''', (payment_id,))
        payment = cursor.fetchone()

    if not payment:
        return "ไม่พบข้อมูลใบเสร็จ", 404

    return render_template('bill.html', p=payment)

# --- Route แสดงหนังสือสัญญา (Contract Document) ---
@app.route('/contract/doc/<contract_number>')
def view_contract_doc(contract_number):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contracts WHERE contract_number = ?", (contract_number,))
        contract = cursor.fetchone()

    if not contract:
        return "ไม่พบข้อมูลสัญญา", 404

    return render_template('contract_document.html', c=contract)

# --- PromptPay Payload Generator ---
def crc16(data: str) -> str:
    crc = 0xFFFF
    for char in data:
        crc ^= ord(char) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return f"{crc:04X}"

def generate_promptpay_payload(target: str, amount: float = 0.0) -> str:
    target = target.replace("-", "").strip()
    if len(target) == 10 and target.startswith("0"):
        target_formatted = "0066" + target[1:]
        target_tag = "01"
    else:
        target_formatted = target
        target_tag = "02"
    
    target_len = f"{len(target_formatted):02d}"
    merchant_info = f"0016A000000677010111{target_tag}{target_len}{target_formatted}"
    
    payload = "000201010212"
    payload += f"29{len(merchant_info):02d}{merchant_info}"
    payload += "5303764"
    
    if amount > 0:
        amt_str = f"{amount:.2f}"
        payload += f"54{len(amt_str):02d}{amt_str}"
        
    payload += "5802TH5907PAYMENT6007BANGKOK6304"
    payload += crc16(payload)
    return payload

@app.route("/qrcode/<contract_number>/<type_pay>")
def generate_qr_image(contract_number, type_pay):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contracts WHERE contract_number = ?", (contract_number,))
        contract = cursor.fetchone()
        
    if not contract:
        abort(404)
        
    paid = contract["paid_installments"]
    total = contract["total_installments"]
    monthly = contract["monthly_amount"]
    
    if type_pay == "full":
        remaining_installments = total - paid
        amount = remaining_installments * monthly
    else:
        amount = monthly
        
    payload = generate_promptpay_payload(PROMPTPAY_ID, amount)
    
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")

# --- LINE Bot Webhook ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        # Check binding by Phone
        if user_text.isdigit() and len(user_text) >= 9:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM contracts WHERE phone = ?", (user_text,))
                contracts = cursor.fetchall()
                
                if contracts:
                    cursor.execute("UPDATE contracts SET line_user_id = ? WHERE phone = ?", (user_id, user_text))
                    conn.commit()
                    msg = f"✅ ผูกบัญชีสำเร็จเรียบร้อยแล้วครับ!\nพบข้อมูลสัญญา {len(contracts)} รายการ\n\nพิมพ์ 'สัญญา' หรือ 'ใบเสร็จ' เพื่อดูรายละเอียดได้เลยครับ"
                else:
                    msg = f"❌ ไม่พบข้อมูลสัญญาที่ลงทะเบียนด้วยเบอร์ {user_text}\nกรุณาตรวจสอบเบอร์โทรศัพท์อีกครั้งครับ"
                    
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            ))
            return

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM contracts WHERE line_user_id = ? AND status = 'ACTIVE' ORDER BY id DESC", (user_id,))
            contract = cursor.fetchone()

        if not contract:
            reply_txt = "👋 ยินดีต้อนรับสู่ระบบบริการผ่อนชำระครับ\n\nกรุณาพิมพ์ **เบอร์โทรศัพท์** ที่ใช้ทำสัญญา เพื่อเริ่มต้นผูกบัญชีเข้ากับ LINE ครับ"
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_txt)]
            ))
            return

        base_url = request.host_url.rstrip('/')
        c_num = contract["contract_number"]
        paid = contract["paid_installments"]
        total = contract["total_installments"]
        monthly = contract["monthly_amount"]
        remain_amt = (total - paid) * monthly

        if user_text in ["สัญญา", "เช็คค่างวด", "ข้อมูลสัญญา", "ค่างวด"]:
            doc_url = f"{base_url}/contract/doc/{c_num}"
            flex_content = {
                "type": "bubble",
                "body": {
                    "type": "box", "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "📱 ข้อมูลสัญญาผ่อนชำระ", "weight": "bold", "size": "xl", "color": "#1DB446"},
                        {"type": "separator", "margin": "md"},
                        {"type": "box", "layout": "vertical", "margin": "lg", "spacing": "sm", "contents": [
                            {"type": "box", "layout": "baseline", "contents": [{"type": "text", "text": "เลขสัญญา", "color": "#aaaaaa", "size": "sm"}, {"type": "text", "text": c_num, "align": "end", "weight": "bold", "size": "sm"}]},
                            {"type": "box", "layout": "baseline", "contents": [{"type": "text", "text": "ผู้เช่าซื้อ", "color": "#aaaaaa", "size": "sm"}, {"type": "text", "text": contract["customer_name"], "align": "end", "size": "sm"}]},
                            {"type": "box", "layout": "baseline", "contents": [{"type": "text", "text": "สินค้า", "color": "#aaaaaa", "size": "sm"}, {"type": "text", "text": contract["product_name"], "align": "end", "weight": "bold", "size": "sm"}]},
                            {"type": "box", "layout": "baseline", "contents": [{"type": "text", "text": "ค่างวดต่อเดือน", "color": "#aaaaaa", "size": "sm"}, {"type": "text", "text": f"{monthly:,.2f} บาท", "align": "end", "color": "#1DB446", "weight": "bold", "size": "sm"}]},
                            {"type": "box", "layout": "baseline", "contents": [{"type": "text", "text": "งวดที่ชำระแล้ว", "color": "#aaaaaa", "size": "sm"}, {"type": "text", "text": f"{paid} / {total} งวด", "align": "end", "weight": "bold", "size": "sm"}]},
                            {"type": "box", "layout": "baseline", "contents": [{"type": "text", "text": "คงเหลือปิดบัญชี", "color": "#aaaaaa", "size": "sm"}, {"type": "text", "text": f"{remain_amt:,.2f} บาท", "align": "end", "color": "#de350b", "weight": "bold", "size": "sm"}]}
                        ]}
                    ]
                },
                "footer": {
                    "type": "box", "layout": "vertical", "spacing": "sm",
                    "contents": [
                        {"type": "button", "style": "primary", "color": "#1DB446", "action": {"type": "message", "label": "💳 จ่ายค่างวดเดือนนี้", "text": "จ่ายค่างวด"}},
                        {"type": "button", "style": "secondary", "action": {"type": "message", "label": "📄 ดูใบเสร็จล่าสุด", "text": "ใบเสร็จ"}},
                        {"type": "button", "style": "link", "action": {"type": "uri", "label": "📜 ดูหนังสือสัญญาฉบับเต็ม", "uri": doc_url}}
                    ]
                }
            }
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[FlexMessage(alt_text="ข้อมูลสัญญาผ่อนชำระ", contents=FlexContainer.from_dict(flex_content))]
            ))

        elif user_text in ["ใบเสร็จ", "ดูใบเสร็จ", "บิล"]:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM payments WHERE contract_number = ? ORDER BY id DESC LIMIT 1", (c_num,))
                last_pay = cursor.fetchone()

            if not last_pay:
                line_bot_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="📄 ยังไม่มีประวัติการชำระเงินในระบบครับ")]
                ))
            else:
                bill_url = f"{base_url}/bill/{last_pay['id']}"
                reply_txt = f"📄 **ใบเสร็จรับเงินล่าสุด**\nเลขที่สัญญา: {c_num}\nงวดที่: {last_pay['installment_no']}\nยอดชำระ: {last_pay['amount']:,.2f} บาท\nวันที่: {last_pay['paid_at']}\n\n🔗 คลิกดูใบเสร็จสมบูรณ์:\n{bill_url}"
                line_bot_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_txt)]
                ))

        elif user_text in ["จ่ายค่างวด", "ชำระเงิน", "ขอ QR", "จ่ายเงิน"]:
            if paid >= total:
                line_bot_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="🎉 สัญญานี้ผ่อนชำระครบทุกงวดเรียบร้อยแล้วครับ ขอบคุณครับ!")]
                ))
                return
                
            qr_url = f"{base_url}/qrcode/{c_num}/monthly"
            caption = f"ค่างวดประจำเดือน สัญญา {c_num}\nยอดชำระ: {monthly:,.2f} บาท\nสแกนผ่านแอปธนาคารได้ทันทีครับ"
            
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(text=caption),
                    ImageMessage(original_content_url=qr_url, preview_image_url=qr_url)
                ]
            ))

        elif user_text in ["ปิดบัญชี", "จ่ายทั้งหมด"]:
            if paid >= total:
                line_bot_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="🎉 สัญญานี้ผ่อนชำระครบทุกงวดเรียบร้อยแล้วครับ")]
                ))
                return
                
            qr_url = f"{base_url}/qrcode/{c_num}/full"
            caption = f"🔥 สแกนชำระปิดบัญชีทั้งหมด สัญญา {c_num}\nคงเหลือ {total - paid} งวด\nยอดชำระสุทธิ: {remain_amt:,.2f} บาท"
            
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(text=caption),
                    ImageMessage(original_content_url=qr_url, preview_image_url=qr_url)
                ]
            ))

        else:
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="สามารถพิมพ์คำสั่งต่อไปนี้ได้ครับ:\n- 'สัญญา' เพื่อดูรายละเอียด\n- 'ใบเสร็จ' เพื่อดูบิลล่าสุด\n- 'จ่ายค่างวด' เพื่อขอ QR Code")]
            ))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)