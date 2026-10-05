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
check("อนุญาตข้อความสัญญา", app.TEXT_COMMANDS.get("สัญญา") == app.CMD_CONTRACT)
check("อนุญาตข้อความชำระค่างวด", app.TEXT_COMMANDS.get("ชำระค่างวด") == app.CMD_PAY_NEXT)

print("\n=== 3. คำสั่งจากปุ่มเมนู ต้องถูกมองเป็นคำสั่ง ===")
for a in [app.CMD_STATUS, app.CMD_CONTRACT, app.CMD_PAY_NEXT, app.CMD_EARLY_CLOSE, app.CMD_CANCEL, app.CMD_MENU]:
    check(f"รับรู้คำสั่ง {a}", app.parse_cmd(app.build_cmd(a)) is not None)

print("\n=== 4. build_quick_reply ใช้ PostbackAction ทั้งหมด ===")
qr = app.build_quick_reply([("เช็คยอดค่างวด", app.CMD_STATUS), ("ชำระงวดถัดไป", app.CMD_PAY_NEXT)])
check("สร้าง QuickReply ได้", qr is not None and len(qr.items) == 2)
for b in qr.items:
    check("เป็น PostbackAction", type(b.action).__name__ == "PostbackAction", type(b.action).__name__)
    check("data ขึ้นต้นด้วย prefix", b.action.data.startswith(app.CMD_PREFIX), b.action.data)
    check("label <= 20 ตัว", len(b.action.label) <= 20, f"{len(b.action.label)}:{b.action.label}")

qr2 = app.build_quick_reply([(f"📤 ส่งสลิปงวดที่ {i}", app.CMD_UPLOAD_SLIP, i) for i in (3, 12, 120)])
for b in qr2.items:
    check(f"label สลิปงวดที่ {b.action.data.split('|')[-1]} <= 20 ตัว", len(b.action.label) <= 20, f"{len(b.action.label)}:{b.action.label}")
check("build_quick_reply(None) -> None", app.build_quick_reply(None) is None)
check("build_quick_reply([]) -> None", app.build_quick_reply([]) is None)

print("\n=== 5. ลบระบบริชเมนู ===")
source = open("app.py", encoding="utf-8").read()
check("ไม่มีฟังก์ชัน Rich Menu", not hasattr(app, "build_rich_menu_payload"))
check("ไม่มี endpoint Rich Menu", not any("/api/richmenu" in str(rule) for rule in app.app.url_map.iter_rules()))
check("ไม่มีการผูก Rich Menu ตอน follow", "RICH_MENU_ID" not in source and "richmenu" not in source.lower())

print("\n=== 6. flex ปุ่มอัปโหลดสลิป ===")
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

print("\n=== 7. ตัวแปรค่าตั้ง ===")
check("STATE_AWAIT_SLIP", app.STATE_AWAIT_SLIP == "await_slip")
check("TTL = 30 นาที", app.SLIP_STATE_TTL_MINUTES == 30, str(app.SLIP_STATE_TTL_MINUTES))
check("ไม่มีโหมดรับข้อความพิมพ์เอง", not hasattr(app, "ALLOW_LEGACY_TEXT_COMMANDS"))
check("เมนูหลักมี 4 ปุ่ม", len(app.MAIN_MENU_BUTTONS) == 4)
check(
    "อ่านสถานะ upload ปิดยอด",
    app.get_state_payload({"payload": '{"payment_type":"early_close","amount":123.45}'})
    == {"payment_type": "early_close", "amount": 123.45}
)

print("\n=== 8. ฟังก์ชันสำคัญถูกต้องครบ ===")
required = ["set_user_state", "get_user_state", "clear_user_state", "get_active_contract",
            "get_state_installment_no", "run_command", "send_main_menu",
            "handle_upload_slip_request", "handle_cancel_pending", "send_payment_qr_next",
            "handle_postback_event", "handle_follow_event", "build_cmd", "parse_cmd"]
for fn in required:
    check(f"มีฟังก์ชัน {fn}", callable(getattr(app, fn, None)))

print("\n=== 9. ข้อความที่กำหนดเท่านั้นจึงเรียกคำสั่ง ===")
original_run_command = app.run_command
received_commands = []
app.run_command = lambda *args: received_commands.append(args)
for text, expected_action in [
    ("เช็คยอด", app.CMD_STATUS),
    ("สัญญา", app.CMD_CONTRACT),
    ("ชำระค่างวด", app.CMD_PAY_NEXT),
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
for text in ["สวัสดี", "เช็คยอดค่างวด", "#BOTMENU#status", "ชำระงวด"]:
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
