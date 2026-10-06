""" สคริปต์ทดสอบตรรกะระบบป้องกันบอท + ปุ่มอัปโหลดสลิป (ไม่ต้องใช้ฐานข้อมูลจริง) """
import sys, os
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO
sys.path.insert(0, os.path.join(os.environ['TEMP'], 'stub'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app

fails = []
def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)

print("=== 1. build_cmd / parse_cmd ===")
check("build_cmd ไม่มี args", app.build_cmd("status") == "#BOTMENU#status", app.build_cmd("status"))
check("build_cmd มี args", app.build_cmd("pay", 3) == "#BOTMENU#pay|3", app.build_cmd("pay", 3))
check("parse_cmd คืน action+args", app.parse_cmd("#BOTMENU#pay|3") == ("pay", ["3"]))
check("parse_cmd ไม่มี args", app.parse_cmd("#BOTMENU#status") == ("status", []))
check("parse_cmd กัน None", app.parse_cmd(None) is None)
check("parse_cmd กันค่าว่าง", app.parse_cmd("") is None)

print("\n=== 2. ระบบป้องกันบอท: รับเฉพาะข้อความที่อนุญาต ===")
typed = ["เช็คค่างวด", "เมนู", "ชำระงวดที่ 3", "ปิดยอดก่อนกำหนด",
         "0812345678", "hello", "เช็ค", "  ", "#BOTMENU", "BOTMENU#pay|1", "x#BOTMENU#pay|1"]
for t in typed:
    check(f"บล็อกข้อความพิมพ์เอง: {t!r}", app.parse_cmd(t) is None)
check("อนุญาตข้อความเช็คยอด", app.TEXT_COMMANDS.get("เช็คยอด") == app.CMD_STATUS)
check("อนุญาตคำสั่งเช็คยอดจาก rich menu OA", app.TEXT_COMMANDS.get("เช็คยอดค่างวด") == app.CMD_STATUS)
check("อนุญาตข้อความสัญญา", app.TEXT_COMMANDS.get("สัญญา") == app.CMD_CONTRACT)
check("อนุญาตคำสั่งข้อมูลสัญญาจาก rich menu OA", app.TEXT_COMMANDS.get("ข้อมูลสัญญา") == app.CMD_CONTRACT)
check("อนุญาตข้อความชำระค่างวด", app.TEXT_COMMANDS.get("ชำระค่างวด") == app.CMD_PAY_NEXT)

print("\n=== 3. คำสั่งจาก postback ของปุ่มขั้นตอน ===")
for a in [app.CMD_STATUS, app.CMD_CONTRACT, app.CMD_PAY_NEXT, app.CMD_EARLY_CLOSE,
          app.CMD_CANCEL, app.CMD_CONFIRM_SLIP]:
    check(f"รับรู้คำสั่ง {a}", app.parse_cmd(app.build_cmd(a)) is not None)

print("\n=== 4. ไม่มี Quick Reply จำลอง ===")
source = open("app.py", encoding="utf-8").read()
check("ไม่มีการสร้าง Quick Reply", not hasattr(app, "build_quick_reply") and ".quick_reply" not in source)
check("คำสั่งพิมพ์ที่อนุญาตระบุชัดเจน", set(app.TEXT_COMMANDS) == {
    "เช็คยอด", "เช็คยอดค่างวด", "สัญญา", "ข้อมูลสัญญา",
    "ชำระค่างวด", "ชำระงวดถัดไป", "ปิดยอดก่อนกำหนด",
    "ส่งสลิป", "ยืนยันสลิป", "ยืนยันว่าเป็นสลิป",
    "ยกเลิกส่งสลิป", "ยกเลิกการส่งสลิป"
})

print("\n=== 5. Flex actions สำหรับส่งและยืนยันสลิป ===")
flex = app.build_upload_slip_prompt_flex(3, 2500.0)
flex_json = flex.contents.as_json_dict()
btns = flex_json["footer"]["contents"]
check("มีปุ่มอัปโหลดสลิป + ยกเลิก", len(btns) == 2)
check("ปุ่มแรกเป็น postback", btns[0]["action"]["type"] == "postback")
check("ปุ่มแรกเรียก upload_slip", app.parse_cmd(btns[0]["action"]["data"]) == ("upload_slip", ["3"]))
check("ปุ่มที่สองเรียก cancel", app.parse_cmd(btns[1]["action"]["data"]) == ("cancel", []))
check("alt_text มีค่า", bool(flex.alt_text))
close_flex = app.build_upload_slip_prompt_flex(None, 10000.0, is_early_close=True)
close_button = close_flex.contents.as_json_dict()["footer"]["contents"][0]
check("ปุ่มสลิปปิดยอดถูกแสดง", close_button["action"]["label"] == "📤 ส่งสลิปปิดยอด")
check("ปุ่มสลิปปิดยอดส่งคำสั่งเฉพาะ", app.parse_cmd(close_button["action"]["data"]) == ("upload_slip", ["early_close"]))
confirm_flex = app.build_slip_confirmation_flex().contents.as_json_dict()
confirm_buttons = confirm_flex["footer"]["contents"]
check("ยืนยันสลิปต้องกดปุ่มยืนยันชัดเจน",
      app.parse_cmd(confirm_buttons[0]["action"]["data"]) == ("confirm_slip", []))
check("ยกเลิกรูปที่รอยืนยันได้",
      app.parse_cmd(confirm_buttons[1]["action"]["data"]) == ("cancel", []))

print("\n=== 6. ตัวแปรค่าตั้ง ===")
check("STATE_AWAIT_SLIP", app.STATE_AWAIT_SLIP == "await_slip")
check("STATE_CONFIRM_SLIP", app.STATE_CONFIRM_SLIP == "confirm_slip")
check("TTL = 30 นาที", app.SLIP_STATE_TTL_MINUTES == 30, str(app.SLIP_STATE_TTL_MINUTES))
check("ไม่มีโหมดรับข้อความพิมพ์เอง", not hasattr(app, "ALLOW_LEGACY_TEXT_COMMANDS"))
check(
    "อ่านสถานะ upload ปิดยอด",
    app.get_state_payload({"payload": '{"payment_type":"early_close","amount":123.45}'})
    == {"payment_type": "early_close", "amount": 123.45}
)

print("\n=== 7. ฟังก์ชันสำคัญถูกต้องครบ ===")
required = ["set_user_state", "get_user_state", "clear_user_state", "get_active_contract",
            "transition_user_state", "get_state_installment_no", "run_command",
            "handle_upload_slip_request", "handle_cancel_pending", "handle_confirm_slip_request",
            "process_confirmed_slip_image", "send_payment_qr_next",
            "handle_postback_event", "handle_follow_event", "build_cmd", "parse_cmd"]
for fn in required:
    check(f"มีฟังก์ชัน {fn}", callable(getattr(app, fn, None)))

print("\n=== 8. รูปภาพต้องอยู่ในขั้นตอนและยืนยันก่อนบันทึก ===")
original_get_user_state = app.get_user_state
original_transition_user_state = app.transition_user_state
original_reply_message = app.line_bot_api.reply_message
original_get_message_content = app.line_bot_api.get_message_content
saved_state = {}
replies = []
download_attempts = []
app.get_user_state = lambda user_id: {"state": app.STATE_AWAIT_SLIP, "payload": '{"installment_no":2,"contract_id":7}'}
def capture_state(user_id, expected_state, state, payload=None, ttl_minutes=None):
    saved_state.update({
        "previous_state": expected_state,
        "state": state,
        "payload": payload,
        "ttl_minutes": ttl_minutes
    })
    return True
app.transition_user_state = capture_state
app.line_bot_api.reply_message = lambda reply_token, message: replies.append((reply_token, message))
app.line_bot_api.get_message_content = lambda message_id: download_attempts.append(message_id)
image_event = SimpleNamespace(
    source=SimpleNamespace(user_id="U-test"),
    message=SimpleNamespace(id="image-test"),
    reply_token="image-reply"
)
app.handle_image_message(image_event)
check("เมื่อได้รับรูปให้เปลี่ยนสถานะเป็นรอยืนยันเท่านั้น",
      saved_state.get("previous_state") == app.STATE_AWAIT_SLIP
      and saved_state.get("state") == app.STATE_CONFIRM_SLIP)
check("อัปเดตสถานะแบบมีเงื่อนไขเพื่อกันอีเวนต์ซ้ำ",
      saved_state.get("previous_state") == app.STATE_AWAIT_SLIP)
check("ผูก message ID รูปไว้กับสถานะยืนยัน",
      app.get_state_payload({"payload": saved_state.get("payload")}).get("image_message_id") == "image-test")
check("รูปยังไม่ถูกดาวน์โหลด/บันทึกก่อนยืนยัน", not download_attempts)
check("ส่ง Flex ให้ลูกค้ายืนยันรูป", len(replies) == 1 and type(replies[0][1]).__name__ == "FlexSendMessage")

saved_state.clear()
replies.clear()
app.get_user_state = lambda user_id: None
app.handle_image_message(image_event)
check("รูปที่ไม่มีคำสั่งเริ่มส่งสลิปไม่เปลี่ยนสถานะ", not saved_state)
check("รูปที่ไม่มีคำสั่งไม่ถูกดาวน์โหลด", not download_attempts)
check("รูปที่ไม่ได้อยู่ในขั้นตอนส่งสลิปไม่ส่งข้อความแจ้งเตือน", not replies)

saved_state.clear()
replies.clear()
app.get_user_state = lambda user_id: {
    "state": app.STATE_CONFIRM_SLIP,
    "payload": '{"installment_no":2,"contract_id":7,"image_message_id":"first-image"}'
}
app.handle_image_message(image_event)
check("รูปใหม่ไม่แทนที่รูปที่กำลังรอยืนยัน", not saved_state and not download_attempts)
check("รูปซ้ำระหว่างรอยืนยันถูกเพิกเฉย", not replies)

saved_state.clear()
replies.clear()
app.get_user_state = lambda user_id: {
    "state": app.STATE_CONFIRM_SLIP,
    "payload": '{"installment_no":2,"contract_id":7,"image_message_id":"confirmed-image"}'
}
processed_images = []
original_process_confirmed_slip_image = app.process_confirmed_slip_image
app.process_confirmed_slip_image = lambda event, state: processed_images.append((event, state))
app.handle_confirm_slip_request("U-test", "confirm-reply")
check("ปุ่มยืนยันใช้ transition แบบ atomic เข้าสถานะกำลังบันทึก",
      saved_state.get("previous_state") == app.STATE_CONFIRM_SLIP
      and saved_state.get("state") == "processing_slip")
check("ประมวลผลเฉพาะ message ID ที่รอยืนยัน",
      len(processed_images) == 1 and processed_images[0][0].message.id == "confirmed-image")
check("เก็บ message ID ไว้ระหว่างประมวลผลเพื่อรองรับการกู้คืน",
      app.get_state_payload(processed_images[0][1]).get("image_message_id") == "confirmed-image")

saved_state.clear()
replies.clear()
app.transition_user_state = lambda *args, **kwargs: False
app.handle_confirm_slip_request("U-test", "duplicate-confirm")
check("ป้องกันการยืนยันซ้ำด้วย state transition แบบ atomic",
      len(processed_images) == 1 and not saved_state and len(replies) == 1)
app.process_confirmed_slip_image = original_process_confirmed_slip_image

app.get_user_state = original_get_user_state
app.transition_user_state = original_transition_user_state
app.line_bot_api.reply_message = original_reply_message
app.line_bot_api.get_message_content = original_get_message_content

print("\n=== 9. ข้อความที่กำหนดเท่านั้นจึงเรียกคำสั่ง ===")
original_run_command = app.run_command
received_commands = []
app.run_command = lambda *args: received_commands.append(args)
for text, expected_action in [
    ("เช็คยอด", app.CMD_STATUS),
    ("เช็คยอดค่างวด", app.CMD_STATUS),
    ("สัญญา", app.CMD_CONTRACT),
    ("ข้อมูลสัญญา", app.CMD_CONTRACT),
    ("ชำระค่างวด", app.CMD_PAY_NEXT),
    ("ชำระงวดถัดไป", app.CMD_PAY_NEXT),
    ("ปิดยอดก่อนกำหนด", app.CMD_EARLY_CLOSE),
    ("ส่งสลิป", app.CMD_UPLOAD_SLIP),
    ("ยืนยันสลิป", app.CMD_CONFIRM_SLIP),
    ("ยืนยันว่าเป็นสลิป", app.CMD_CONFIRM_SLIP),
    ("ยกเลิกส่งสลิป", app.CMD_CANCEL),
    ("ยกเลิกการส่งสลิป", app.CMD_CANCEL),
]:
    event = SimpleNamespace(
        message=SimpleNamespace(text=text),
        source=SimpleNamespace(user_id="U-test"),
        reply_token="reply-token"
    )
    app.handle_message(event)
    check(
        f"เรียกคำสั่ง {text}",
        received_commands[-1] == (expected_action, [], "U-test", "reply-token"),
        str(received_commands[-1])
    )

received_commands.clear()
for text in ["สวัสดี", "#BOTMENU#status", "ชำระงวด", "ปิดยอด", "ส่งสลิปด่วน"]:
    event = SimpleNamespace(
        message=SimpleNamespace(text=text),
        source=SimpleNamespace(user_id="U-test"),
        reply_token="reply-token"
    )
    with redirect_stdout(StringIO()):
        app.handle_message(event)
    check(f"ไม่เรียกคำสั่งสำหรับ: {text!r}", not received_commands)
app.run_command = original_run_command

print("\n" + "=" * 50)
print(f"ผ่าน {len(fails) == 0 and 'ทั้งหมด' or 'มีข้อผิดพลาด: ' + ', '.join(fails)}")
print("=" * 50)
sys.exit(1 if fails else 0)
