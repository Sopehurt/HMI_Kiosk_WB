import sys
import os
import json
import time
import serial
import threading
import subprocess
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VIDEO_PATH = os.path.join(BASE_DIR, "static", "videos")

app = FastAPI()
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
app.mount("/video", StaticFiles(directory=os.path.join(BASE_DIR, "video")), name="video")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# --- Global State ---
shared_state = {
    "balance": 0,
    "last_action": "",
    "last_action_event": None,
    "last_state_event": None,
    "last_schedule_event": None,
    "countdown": 0,
    "current_page": "home",
    "mqtt_online": False,
    "mqtt_status": "offline",
    "pending_config": None,
    "ozone_val": 0, "uv_val": 0, "temp_val": 0, "humi_val": 0, "dust_val": 0, "flow_val": 0,
}

_action_seq = 0
_state_seq = 0
_partial_line_buffer = ""
ser_lock = threading.Lock()
ser_obj = None

# --- 1. Helper Functions ---
def get_machine_system():
    try:
        with open(os.path.join(BASE_DIR, "Global_data.json"), "r") as f:
            data = json.load(f)
        return data.get("machine_system", "CATCARWASH")
    except: return "CATCARWASH"

def get_active_functions():
    try:
        with open(os.path.join(BASE_DIR, "Global_data.json"), "r") as f:
            data = json.load(f)
        names = data.get("fnNames", [""]*8)
        en_names = ["dust", "bact", "uv", "ozone", "dry", "perfume", "fn7", "fn8"]
        active = []
        for i in range(8):
            if names[i] and names[i].lower() != "nan":
                active.append({"index": i, "name": names[i], "en_name": en_names[i]})
        return active
    except: return []

def _save_to_pi(config: dict):
    try:
        with open(os.path.join(BASE_DIR, "Global_data.json"), "w") as f:
            json.dump(config, f, indent=2)
        print("UART: Successfully saved config to Global_data.json")
    except Exception as e:
        print(f"PI SAVE ERROR: {e}")

def _parse_line(line: str):
    global _action_seq, _state_seq, _partial_line_buffer
    if not line: return
    print(f"UART READ: {repr(line)}")

    # 1. State/Balance/MQTT
    if line.startswith("[State]"):
        name = line.replace("[State]", "").strip()
        _state_seq += 1
        shared_state["last_state_event"] = {"name": name, "seq": _state_seq}
    
    elif line.startswith("[Balance]"):
        try:
            val = int(line.replace("[Balance]", "").strip())
            shared_state["balance"] = val
        except: pass

    elif "[MQTT]" in line:
        shared_state["mqtt_online"] = ("online" in line.lower())
        shared_state["mqtt_status"] = "online" if shared_state["mqtt_online"] else "offline"

    elif line.startswith("[Action]"):
        txt = line.replace("[Action]", "").strip()
        _action_seq += 1
        shared_state["last_action_event"] = {"name": txt, "val": "", "seq": time.time()}
        if "Balance =" in txt:
            try: shared_state["balance"] = int(txt.split("=")[1].strip())
            except: pass
        if "MQTT" in txt.upper() and "ONLINE" in txt.upper():
            shared_state["mqtt_online"] = True

    # 2. Config & JSON
    elif line.startswith("[Config]"):
        val_str = line.split("[Config]")[1].strip()
        if "|" in val_str: val_str = val_str.split("|", 1)[1]
        try:
            config_data = json.loads(val_str)
            _save_to_pi(config_data)
        except: pass

    elif line.startswith("{") and line.endswith("}"):
        try:
            config_data = json.loads(line)
            _save_to_pi(config_data)
        except: pass

    # 3. Fragments
    elif line.startswith("{") and not line.endswith("}"):
        _partial_line_buffer = line
    elif _partial_line_buffer and line.endswith("}"):
        _partial_line_buffer += line
        try:
            config_data = json.loads(_partial_line_buffer)
            _save_to_pi(config_data)
        except: pass
        _partial_line_buffer = ""
    elif _partial_line_buffer:
        _partial_line_buffer += line

    # 4. QR/Status
    elif ":" in line and any(x in line for x in ["QR_", "PAYMENT_", "WAITING_"]):
        parts = line.split(":", 1)
        shared_state["last_action_event"] = {"name": parts[0].strip(), "val": parts[1].strip(), "seq": time.time()}

    elif line.startswith("[Count down]"):
        shared_state["countdown"] = line.split("[Count down]")[1].strip()

    elif line.startswith("[Schedule]"):
        raw = line.replace("[Schedule]", "").strip()
        shared_state["last_schedule_event"] = {"raw": raw, "seq": time.time()}

def uart_thread():
    global ser_obj
    while True:
        try:
            with ser_lock:
                if ser_obj is None or not ser_obj.is_open:
                    ser_obj = serial.Serial('/dev/ttyAMA0', 115200, timeout=0.1)
                    print("UART: Opened /dev/ttyAMA0")
            
            while True:
                line = ""
                with ser_lock:
                    if ser_obj.in_waiting > 0:
                        line = ser_obj.readline().decode('utf-8', errors='ignore').strip()
                if line: _parse_line(line)
                else: time.sleep(0.05)
        except Exception as e:
            print(f"UART ERROR: {e}")
            time.sleep(2)

def send_uart(msg: str):
    global ser_obj
    print(f"UART SEND: {msg}")
    try:
        with ser_lock:
            if ser_obj and ser_obj.is_open:
                ser_obj.write((msg + "\n").encode('utf-8'))
            else:
                # Fallback if thread not started
                tmp = serial.Serial('/dev/ttyAMA0', 115200, timeout=1)
                tmp.write((msg + "\n").encode('utf-8'))
                tmp.close()
    except Exception as e:
        print(f"UART SEND ERROR: {e}")

threading.Thread(target=uart_thread, daemon=True).start()

# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/page/home")
async def page_home(request: Request):
    shared_state["current_page"] = "home"
    send_uart("[State] Home")
    return templates.TemplateResponse(request=request, name="pages/home.html", context={"videos": []})

@app.get("/page/payment")
async def page_payment(request: Request):
    shared_state["current_page"] = "payment"
    send_uart("[State] payment")
    return templates.TemplateResponse(request=request, name="pages/payment.html", context={"machine_system": get_machine_system()})

@app.get("/page/cash")
async def page_cash(request: Request):
    shared_state["current_page"] = "cash"
    send_uart("[State] cash")
    return templates.TemplateResponse(request=request, name="pages/cash.html")

@app.get("/page/qr")
async def page_qr(request: Request):
    shared_state["current_page"] = "qr"
    send_uart("[State] qr")
    return templates.TemplateResponse(request=request, name="pages/qr.html")

@app.get("/page/operation_CATCARWASH")
async def page_op_carwash(request: Request):
    shared_state["current_page"] = "operation"
    send_uart("[State] operation")
    return templates.TemplateResponse(request=request, name="pages/operation_catcarwash.html", context={"active_functions": get_active_functions()})

@app.get("/page/operation_CATPAW")
@app.get("/page/operation_CATPAW-SHOE")
@app.get("/page/operation_CATPAW-HELMET")
async def page_op_catpaw(request: Request):
    shared_state["current_page"] = "operation"
    send_uart("[State] operation")
    mode = "helmet" if "HELMET" in request.url.path else "shoe"
    return templates.TemplateResponse(request=request, name="pages/operation_catpaw.html", context={"mode": mode})

@app.get("/page/maintenance")
async def page_maint(request: Request):
    send_uart("[State] maintenance")
    return templates.TemplateResponse(request=request, name="pages/maintenance.html")

# --- API ---
@app.get("/api/balance_html")
async def get_balance_html():
    return HTMLResponse(content=str(shared_state["balance"]))

@app.get("/api/settings")
async def get_settings():
    try:
        with open(os.path.join(BASE_DIR, "Global_data.json"), "r") as f:
            data = json.load(f)
        return JSONResponse(content=data)
    except: return JSONResponse(content={}, status_code=500)

@app.get("/api/action_state")
async def api_action_state():
    return JSONResponse(content={
        "action_event": shared_state.get("last_action_event"),
        "mqtt_online": shared_state.get("mqtt_online", False),
        "countdown": shared_state.get("countdown", "--"),
        "balance": shared_state.get("balance", 0)
    })

@app.get("/api/uart_send")
async def api_uart_send(msg: str):
    send_uart(msg)
    return {"ok": True}