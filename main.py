import sys
import os
import json
import time
import serial
import threading
import subprocess
import queue
import re
from datetime import datetime
from fastapi import FastAPI, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = FastAPI()
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
app.mount("/video", StaticFiles(directory=os.path.join(BASE_DIR, "video")), name="video")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["is_dev"] = sys.platform == "darwin"
VIDEO_DIR = os.path.join(BASE_DIR, "video")

# --- Global State ---
shared_state = {
    "balance": 0,
    "test_coin": 0,
    "test_bank": 0,
    "last_qr_event": None,
    "last_action_event": None,
    "action_history": [], # Buffer for multiple fast events
    "last_state_event": None,
    "last_schedule_event": None,
    "countdown": 0,
    "current_page": "home",
    "mqtt_online": False,
}

send_queue = queue.Queue()

# --- Helper Functions ---
def get_machine_system():
    try:
        with open(os.path.join(BASE_DIR, "Global_data.json"), "r") as f:
            data = json.load(f)
        return data.get("machine_system") or data.get("machineSystem") or "CATCARWASH"
    except: return "CATCARWASH"

def get_active_functions():
    try:
        with open(os.path.join(BASE_DIR, "Global_data.json"), "r") as f:
            data = json.load(f)
        names = data.get("fnNames") or data.get("fn_names") or [""]*8
        en = data.get("fnEnable") or data.get("fn_enable") or [True]*8
        order = data.get("fnOrder") or data.get("fn_order") or list(range(8))
        
        en_names = ["dust", "bact", "uv", "ozone", "dry", "perfume", "fn7", "fn8"]
        active = []
        
        # We follow the order specified in fnOrder
        for pos in range(len(order)):
            idx = order[pos]
            if idx < len(names) and en[idx]:
                # Priority: 1. Name from config, 2. Fallback to Function N
                display_name = names[idx] if (names[idx] and names[idx].lower() != "nan") else f"Function {idx+1}"
                active.append({
                    "index": idx,
                    "name": display_name,
                    "en_name": f"fn{idx}"
                })
        return active
    except Exception as e:
        print(f"GET ACTIVE FN ERROR: {e}")
        return []

def handle_video_download(url):
    try:
        os.makedirs(VIDEO_DIR, exist_ok=True)
        filename = os.path.basename(url.split('?')[0])
        if not filename or len(filename) < 5: 
            filename = f"video_{int(time.time())}.mp4"
        dest = os.path.join(VIDEO_DIR, filename)
        # Use wget to download to the target destination
        cmd = f"wget -q -O \"{dest}\" \"{url}\""
        res = subprocess.call(cmd, shell=True)
        if res == 0:
            send_queue.put("[Download Video] Success")
        else:
            send_queue.put("[Download Video] Error")
    except Exception as e:
        print(f"Download Error: {e}")
        send_queue.put("[Download Video] Error")

def _save_to_pi(new_config: dict):
    try:
        data = {}
        path = os.path.join(BASE_DIR, "Global_data.json")
        if os.path.exists(path):
            with open(path, "r") as f:
                data = json.load(f)
        
        # KEY MAPPING: Normalize snake_case from ESP to camelCase for Pi where needed
        mapping = {
            "start_prices": "startPrices",
            "start_timeout": "startTimeout",
            "machine_active": "machineActive",
            "money_mem_active": "moneyMemActive",
            "pro_mo": "proMo",
            "fn_enable": "fnEnable",
            "fn_order": "fnOrder",
            "fn_names": "fnNames",
            "fn_time": "fnTime",
            "delay_time": "delayTime"
        }
        # CATPAW MAPPING: These keys should be nested inside catpaw_config
        catpaw_keys = ["t_total", "ozone_s", "ozone_e", "uv_s", "uv_e", "dust_s", "dust_e", "bact_s", "bact_e", "dry_s", "dry_e", "perfume_s", "perfume_e"]
        if "catpaw_config" not in data: data["catpaw_config"] = {}

        for k, v in new_config.items():
            if k in catpaw_keys:
                data["catpaw_config"][k] = v
                continue
                
            final_k = mapping.get(k, k)
            data[final_k] = v
            # If it's machine_system, ensure both versions are updated to be safe
            if final_k in ["machine_system", "machineSystem"]:
                data["machine_system"] = v
                data["machineSystem"] = v
            # If it's fnEnable, also ensure fn_enable is sync'd and vice versa
            if final_k in ["fnEnable", "fn_enable"]:
                data["fnEnable"] = v
                data["fn_enable"] = v

        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"PI SAVED: {list(new_config.keys())}")
    except Exception as e:
        print(f"PI SAVE ERROR: {e}")

def _parse_line(line: str):
    if not line: return
    # print(f"UART READ: {repr(line)}")
    
    # 1. Balance
    if "[Balance]" in line:
        try: shared_state["balance"] = int(line.split("[Balance]")[1].strip())
        except: pass
    elif "Balance =" in line:
        try: shared_state["balance"] = int(line.split("Balance =")[1].strip().split()[0])
        except: pass

    # 2. State & MQTT
    lower_line = line.lower()
    if lower_line.startswith("[state]"):
        import re
        state_name = re.sub(r'(?i)\[state\]', '', line).strip()
        shared_state["last_state_event"] = {"name": state_name, "seq": time.time()}
    elif "[MQTT]" in line:
        shared_state["mqtt_online"] = ("online" in line.lower())
    elif "[Coin]" in line:
        try: shared_state["test_coin"] = int(line.split("[Coin]")[1].strip())
        except: pass
    elif "[Bank]" in line:
        try: shared_state["test_bank"] = int(line.split("[Bank]")[1].strip())
        except: pass
    
    # 3. Actions & Config
    elif line.startswith("[Action]"):
        txt = line.replace("[Action]", "").strip()
        evt = {"name": txt, "val": "", "seq": time.time()}
        shared_state["last_action_event"] = evt
        shared_state["action_history"].append(evt)
        if len(shared_state["action_history"]) > 20: shared_state["action_history"].pop(0)
    
    elif line.startswith("[Video] Clear"):
        # Delete all files in video directory
        if os.path.exists(VIDEO_DIR):
            for f in os.listdir(VIDEO_DIR):
                try: os.remove(os.path.join(VIDEO_DIR, f))
                except: pass
    elif line.startswith("[Download Video]"):
        url = line.replace("[Download Video]", "").strip()
        if url:
            threading.Thread(target=handle_video_download, args=(url,), daemon=True).start()
    elif line.startswith("[Maint] Submit"):
        shared_state["test_coin"] = 0
        shared_state["test_bank"] = 0

    line = line.strip()
    if line.startswith("QR_PAYLOAD:"):
        shared_state["last_qr_event"] = {"name": "QR_PAYLOAD", "val": line.replace("QR_PAYLOAD:", "").strip(), "seq": time.time()}
    elif line.startswith("QR_URL:"):
        shared_state["last_qr_event"] = {"name": "QR_URL", "val": line.replace("QR_URL:", "").strip(), "seq": time.time()}
    elif line.startswith("WAITING_QR:"):
        shared_state["last_qr_event"] = {"name": "WAITING_QR", "val": line.replace("WAITING_QR:", "").strip(), "seq": time.time()}
    elif line.startswith("PAYMENT_OK:"):
        shared_state["last_action_event"] = {"name": "PAYMENT_OK", "val": line.replace("PAYMENT_OK:", "").strip(), "seq": time.time()}
    elif line.startswith("PAYMENT_ERROR:"):
        shared_state["last_action_event"] = {"name": "PAYMENT_ERROR", "val": line.replace("PAYMENT_ERROR:", "").strip(), "seq": time.time()}
    elif line.startswith("PAYMENT_TIMEOUT"):
        shared_state["last_action_event"] = {"name": "PAYMENT_TIMEOUT", "val": "Timeout", "seq": time.time()}
    elif line.startswith("[Action]"):
        shared_state["last_action_event"] = {"name": "Action", "val": line.replace("[Action]", "").strip(), "seq": time.time()}
    elif line.startswith("[Config]"):
        try:
            val_str = line.split("[Config]")[1].strip()
            if "|" in val_str: val_str = val_str.split("|", 1)[1]
            _save_to_pi(json.loads(val_str))
        except: pass
    elif "{" in line and "}" in line:
        # Robust JSON detection: find the outermost { }
        try:
            start = line.find("{")
            end = line.rfind("}") + 1
            json_str = line[start:end]
            _save_to_pi(json.loads(json_str))
        except: pass
    elif line.startswith("[Count down]"):
        shared_state["countdown"] = line.split("[Count down]")[1].strip()
    elif line.startswith("[Schedule]"):
        shared_state["last_schedule_event"] = {"raw": line.replace("[Schedule]", "").strip(), "seq": time.time()}

import logging
from logging.handlers import RotatingFileHandler

# Setup local file logging for UART
uart_logger = logging.getLogger('uart_debug')
uart_logger.setLevel(logging.DEBUG)

if sys.platform != "darwin":
    # Limit to 5MB, keep up to 3 backups
    fh = RotatingFileHandler(os.path.join(BASE_DIR, 'uart.log'), maxBytes=5*1024*1024, backupCount=3)
    fh.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
    uart_logger.addHandler(fh)
else:
    uart_logger.addHandler(logging.NullHandler())

def uart_worker():
    uart_logger.info("UART Worker Thread Started")
    print("UART Worker Thread Started", flush=True)
    while True:
        try:
            # Use serial0 (primary UART)
            ser = serial.Serial('/dev/serial0', 115200, timeout=1.0)
            msg = "UART Connected successfully on /dev/serial0"
            print(msg, flush=True)
            uart_logger.info(msg)
            
            ser.flushInput()
            ser.flushOutput()
            
            while True:
                if ser.in_waiting > 0:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        print(f"UART RX: {line}", flush=True)
                        uart_logger.info(f"RX: {line}")
                        _parse_line(line)
                
                # Send messages from queue
                while not send_queue.empty():
                    msg = send_queue.get()
                    ser.write((msg + "\n").encode('utf-8'))
                    uart_logger.info(f"TX: {msg}")
                
                time.sleep(0.05)
        except Exception as e:
            err_msg = f"UART Error: {e}. Retrying in 2s..."
            print(err_msg, flush=True)
            uart_logger.error(err_msg)
            time.sleep(2)

if sys.platform != "darwin":
    threading.Thread(target=uart_worker, daemon=True).start()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request): return templates.TemplateResponse(request=request, name="index.html")

@app.get("/page/home")
async def page_home(request: Request):
    shared_state["current_page"] = "home"
    send_queue.put("[State] Home")
    return templates.TemplateResponse(request=request, name="pages/home.html", context={"videos": []})

@app.get("/page/payment")
async def page_payment(request: Request):
    shared_state["current_page"] = "payment"
    send_queue.put("[State] payment")
    return templates.TemplateResponse(request=request, name="pages/payment.html", context={"machine_system": get_machine_system()})

@app.get("/page/cash")
async def page_cash(request: Request):
    shared_state["current_page"] = "cash"
    send_queue.put("[State] cash")
    return templates.TemplateResponse(request=request, name="pages/cash.html")

@app.get("/page/qr")
async def page_qr(request: Request):
    shared_state["current_page"] = "qr"
    send_queue.put("[State] qr")
    return templates.TemplateResponse(request=request, name="pages/qr.html")

@app.get("/page/operation_CATCARWASH")
async def page_op_carwash(request: Request):
    shared_state["current_page"] = "operation"
    send_queue.put("[State] operation")
    return templates.TemplateResponse(request=request, name="pages/operation_catcarwash.html", context={"active_functions": get_active_functions()})

@app.get("/page/operation_CATPAW")
@app.get("/page/operation_CATPAW-SHOE")
@app.get("/page/operation_CATPAW-HELMET")
async def page_op_catpaw(request: Request):
    shared_state["current_page"] = "operation"
    send_queue.put("[State] operation")
    mode = "helmet" if "HELMET" in request.url.path else "shoe"
    return templates.TemplateResponse(request=request, name="pages/operation_catpaw.html", context={"mode": mode})

@app.get("/page/maintenance")
async def page_maint(request: Request):
    send_queue.put("[State] maintenance")
    return templates.TemplateResponse(request=request, name="pages/maintenance.html")

@app.post("/api/dev/adjust_balance")
async def api_dev_adjust_balance(data: dict = Body(...)):
    if sys.platform != "darwin":
        return JSONResponse(content={"ok": False, "msg": "Only allowed in dev environment"}, status_code=403)
    amount = data.get("amount", 0)
    shared_state["balance"] = max(0, shared_state["balance"] + amount)
    return {"ok": True, "balance": shared_state["balance"]}

@app.get("/api/balance_html")
async def get_balance_html(): return HTMLResponse(content=str(shared_state["balance"]))

@app.get("/api/settings")
async def get_settings():
    try:
        with open(os.path.join(BASE_DIR, "Global_data.json"), "r") as f: return JSONResponse(content=json.load(f))
    except: return JSONResponse(content={}, status_code=500)

@app.post("/api/settings")
async def post_settings(request: Request):
    try:
        new_data = await request.json()
        _save_to_pi(new_data)
        return {"ok": True}
    except: return {"ok": False}

@app.get("/api/action_state")
async def api_action_state():
    return JSONResponse(content={
        "action_event": shared_state.get("last_action_event"),
        "action_history": shared_state.get("action_history", []),
        "qr_event": shared_state.get("last_qr_event"),
        "state_event": shared_state.get("last_state_event"),
        "schedule_event": shared_state.get("last_schedule_event"),
        "mqtt_online": shared_state.get("mqtt_online", False),
        "countdown": shared_state.get("countdown", "--"),
        "balance": shared_state.get("balance", 0),
        "test_coin": shared_state.get("test_coin", 0),
        "test_bank": shared_state.get("test_bank", 0)
    })

@app.get("/api/videos")
async def list_videos():
    try:
        if not os.path.exists(VIDEO_DIR): os.makedirs(VIDEO_DIR, exist_ok=True)
        files = [f for f in os.listdir(VIDEO_DIR) if f.lower().endswith(('.mp4', '.mov', '.avi', '.mkv'))]
        return {"videos": files}
    except: return {"videos": []}

@app.get("/api/uart_send")
async def api_uart_send(msg: str):
    send_queue.put(msg)
    return {"ok": True}

# --- WiFi API ---
@app.get("/api/wifi/status")
async def wifi_status():
    try:
        ssid = subprocess.check_output("iwgetid -r", shell=True).decode().strip()
        ip = subprocess.check_output("hostname -I", shell=True).decode().split()[0]
        return {"ssid": ssid, "ip": ip}
    except: return {"ssid": None, "ip": None}

@app.get("/api/wifi/scan")
async def wifi_scan():
    try:
        output = subprocess.check_output("sudo nmcli -t -f SSID,SIGNAL dev wifi", shell=True).decode()
        nets = []
        for line in output.split('\n'):
            if ':' in line:
                p = line.rsplit(':', 1)
                if p[0] and not any(n['ssid'] == p[0] for n in nets):
                    nets.append({"ssid": p[0], "signal": p[1]})
        return nets[:10]
    except: return []

@app.post("/api/wifi/connect")
async def api_wifi_connect(data: dict = Body(...)):
    ssid = data.get("ssid")
    password = data.get("password")
    if not ssid: return {"ok": False, "msg": "SSID required"}
    
    # Use a more robust nmcli command with Smart Fallback
    try:
        # 1. Clean up any existing profile with this SSID
        subprocess.run(f'sudo nmcli connection delete "{ssid}"', shell=True, capture_output=True)
        time.sleep(1)
        
        # 2. First attempt: Try with password (Standard for WPA2/Hotspot)
        cmd_with_pass = f'sudo nmcli device wifi connect "{ssid}" password "{password}"'
        process = subprocess.run(cmd_with_pass, shell=True, capture_output=True, text=True, timeout=45)
        
        if process.returncode == 0:
            return {"ok": True}
        
        # 3. Fallback: If failed (likely Open WiFi or Key-mgmt error), try without password
        # This is common for dormitory WiFi or public networks.
        cmd_open = f'sudo nmcli device wifi connect "{ssid}"'
        process_open = subprocess.run(cmd_open, shell=True, capture_output=True, text=True, timeout=45)
        
        if process_open.returncode == 0:
            return {"ok": True, "msg": "Connected as Open Network"}
            
        # 4. If both failed, return the most relevant error
        err = process_open.stderr.strip() or process.stdout.strip() or process.stderr.strip()
        return {"ok": False, "msg": err or "Failed to connect"}
        
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "Connection timeout (45s)"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.post("/api/wifi/forget")
async def wifi_forget(request: Request):
    try:
        d = await request.json()
        ssid = d.get("ssid")
        subprocess.run(f'sudo nmcli connection delete "{ssid}"', shell=True)
        return {"ok": True}
    except: return {"ok": False}