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
    "last_action_event": None, # {name: str, seq: int}
    "last_state_event": None,  # {name: str, seq: int}
    "last_schedule_event": None,
    "countdown": 0,
    "current_page": "home",
    "mqtt_status": "offline",
    "pending_config": None,
    # Test values
    "ozone_val": 0,
    "uv_val": 0,
    "temp_val": 0,
    "humi_val": 0,
    "dust_val": 0,
    "flow_val": 0,
}

_action_seq = 0
_state_seq = 0
_PI_ONLY_KEYS = {"lastedUpdate", "lastedMaintenance"}

# --- 1. Helper Functions ---
def get_machine_system():
    try:
        with open(os.path.join(BASE_DIR, "Global_data.json"), "r") as f:
            data = json.load(f)
        return data.get("machine_system", "CATCARWASH")
    except:
        return "CATCARWASH"

def get_delay_time():
    try:
        with open(os.path.join(BASE_DIR, "Global_data.json"), "r") as f:
            data = json.load(f)
            return data.get("delay_time", [10]*8)
    except:
        return [10]*8

def _merge_pending(current: dict, pending: dict) -> dict:
    """Merge pending ESP config into current.
    Pi-only keys are only updated if the incoming value is non-empty."""
    safe = dict(pending)

    # --- 1. Comprehensive Field Mapping ---
    mapping = {
        "device_id": "deviceId",
        "machine_system": "machineSystem",
        "fn_time": "fnTime",
        "fn_enable": "fnEnable",
        "delay_time": "delayTime",
        "machine_active": "machineActive",
        "multi_mode": "multiMode",
        "bank_accept": "bankAccept",
        "coin_accept": "coinAccept",
        "qr_accept": "qrAccept",
        "money_mem_active": "moneyMemActive",
        "start_prices": "startPrices",
        "start_timeout": "startTimeout",
        "heartbeat_inv": "heartbeatInv",
        "pro_mo": "proMo",
        "debug": "debugMode",
        "HMI": "HMI",
        "ver": "ver",
        "ssid": "ssid",
        "wfpwd": "wfpwd"
    }
    
    for old_k, new_k in mapping.items():
        val = safe.get(old_k) or safe.get(new_k)
        if val is not None:
            # Type conversion for booleans if sent as 0/1
            if isinstance(val, (int, float)) and "accept" in old_k.lower():
                val = bool(val)
            if (old_k == "fn_enable" or new_k == "fnEnable") and isinstance(val, list):
                val = [bool(x) for x in val]
            
            safe[old_k] = val
            safe[new_k] = val

    # --- 2. Handle Pi-only protected keys ---
    for key in _PI_ONLY_KEYS:
        if key in safe:
            incoming = safe[key]
            if not incoming:
                del safe[key]

    # --- 3. CATPAW Config Nesting ---
    timeline_keys = [
        't_total', 'bact_s', 'bact_e', 'dust_s', 'dust_e', 
        'uv_s', 'uv_e', 'ozone_s', 'ozone_e', 'dry_s', 'dry_e', 
        'perfume_s', 'perfume_e'
    ]
    if any(k in safe for k in timeline_keys):
        if 'catpaw_config' not in current:
            current['catpaw_config'] = {}
        for k in timeline_keys:
            if k in safe:
                current['catpaw_config'][k] = safe[k]
    
    current.update(safe)
    return current

def _save_to_pi(config_data: dict):
    """Utility to safely write config to Global_data.json"""
    try:
        path = os.path.join(BASE_DIR, "Global_data.json")
        with open(path, "r") as f:
            current = json.load(f)
        
        updated = _merge_pending(current, config_data)
        
        with open(path, "w") as f:
            json.dump(updated, f, indent=2)
        print(f"UART CONFIG: Atomic update success. System: {updated.get('machine_system')}")
    except Exception as e:
        print(f"UART CONFIG ERROR (Save): {e}")

# --- 2. Hardware Loop (Real UART) ---
_partial_line_buffer = ""

def _parse_line(line: str):
    global _partial_line_buffer
    
    if not line:
        return

    # Debug: See every single line clearly
    print(f"UART RAW LINE: {repr(line)}")
    """Parse one UART line and update shared_state."""
    if line:
        print(f"\n<<< ESP32 SENDING TO PI <<< : {line}\n")

    global _action_seq, _state_seq
    
    # 1. State Event
    if line.startswith("[State]"):
        state_name = line.replace("[State]", "").strip()
        _state_seq += 1
        shared_state["last_state_event"] = {"name": state_name, "seq": _state_seq}
        print(f"UART READ: State = {state_name} (seq:{_state_seq})")
    
    # 2. Action Event
    elif line.startswith("[Action]"):
        action_text = line.replace("[Action]", "").strip()
        _action_seq += 1
        shared_state["last_action_event"] = {"text": action_text, "seq": _action_seq}
        shared_state["last_action"] = action_text
        print(f"UART READ: Action = {action_text} (seq:{_action_seq})")
        
        # Specific sub-parsers
        if "Balance =" in action_text:
            try:
                shared_state["balance"] = int(action_text.split("=")[1].strip())
                print(f"UART READ: Balance = {shared_state['balance']}")
            except: pass
        elif "Countdown =" in action_text:
            try:
                shared_state["countdown"] = int(action_text.split("=")[1].strip())
            except: pass
        elif "MQTT Status =" in action_text:
            shared_state["mqtt_status"] = action_text.split("=")[1].strip()
        elif "Bank =" in action_text or "Coin =" in action_text:
            # Maybe update balance or just log
            pass

    # 3. Test values
    elif line.startswith("[Test]"):
        try:
            val_str = line.replace("[Test]", "").strip()
            if "|" in val_str:
                parts = val_str.split("|")
                for p in parts:
                    if ":" in p:
                        k, v = p.split(":")
                        k = k.lower().strip()
                        if k == "ozone": shared_state["ozone_val"] = int(v)
                        elif k == "uv": shared_state["uv_val"] = int(v)
                        elif k == "temp": shared_state["temp_val"] = float(v)
                        elif k == "humi": shared_state["humi_val"] = float(v)
                        elif k == "dust": shared_state["dust_val"] = int(v)
                        elif k == "flow": shared_state["flow_val"] = int(v)
        except: pass

    # 4. JSON Config (Support both [Config] prefix and raw JSON)
    elif line.startswith("[Config]"):
        val_str = line.split("[Config]")[1].strip()
        if "|" in val_str:
            val_str = val_str.split("|", 1)[1]
        
        if not val_str.startswith("{"):
            print(f"UART READ (UNRECOGNIZED CONFIG): {val_str}")
            return
        try:
            config_data = json.loads(val_str)
            shared_state["pending_config"] = config_data
            _save_to_pi(config_data) # Auto-save to Pi
        except json.JSONDecodeError:
            print("UART READ: Config JSON Decode Error")

    # --- NEW: Handle Raw JSON lines (no prefix) ---
    elif line.startswith("{") and line.endswith("}"):
        try:
            config_data = json.loads(line)
            shared_state["pending_config"] = config_data
            _save_to_pi(config_data) # Auto-save to Pi
            print("UART READ: Raw JSON config detected & saved to Pi.")
        except json.JSONDecodeError as e:
            print(f"UART READ: Raw JSON Decode Error: {e}")
    
    # --- NEW: Handle Fragmented JSON ---
    elif line.startswith("{") and not line.endswith("}"):
        _partial_line_buffer = line
        print("UART READ: Detected partial JSON start...")
    
    elif _partial_line_buffer and line.endswith("}"):
        _partial_line_buffer += line
        try:
            config_data = json.loads(_partial_line_buffer)
            shared_state["pending_config"] = config_data
            _save_to_pi(config_data) # Auto-save to Pi
            print("UART READ: Reconstructed & saved fragmented JSON.")
            _partial_line_buffer = ""
        except json.JSONDecodeError as e:
            print(f"UART READ: Fragmented JSON Decode Error: {e}")
            _partial_line_buffer = ""
    
    elif _partial_line_buffer:
        _partial_line_buffer += line
    
    else:
        # Debug: Print unrecognized lines
        if line:
            print(f"UART READ (UNRECOGNIZED): {line}")

def uart_thread():
    # Use real serial
    try:
        ser = serial.Serial('/dev/ttyAMA0', 115200, timeout=1)
        print("UART READ: Connected to /dev/ttyAMA0")
    except Exception as e:
        print(f"UART READ: Failed to open serial: {e}")
        return

    while True:
        if ser.in_waiting > 0:
            try:
                raw = ser.readline()
                line = raw.decode('utf-8', errors='ignore').strip()
                if line:
                    _parse_line(line)
            except Exception as e:
                print(f"UART READ ERROR: {e}")
        time.sleep(0.01)

threading.Thread(target=uart_thread, daemon=True).start()

def send_uart(msg: str):
    print(f"\n>>> PI SENDING TO ESP32 >>> : {msg}\n")
    try:
        ser = serial.Serial('/dev/ttyAMA0', 115200, timeout=1)
        ser.write((msg + "\n").encode('utf-8'))
        ser.close()
    except:
        pass

# --- 3. Routes ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/page/home")
async def page_home(request: Request):
    shared_state["current_page"] = "home"
    send_uart("[State] home")
    
    # Check current config for delays
    video_delay = 10000
    config_delay = 2000
    try:
        with open(os.path.join(BASE_DIR, "Global_data.json"), "r") as f:
            data = json.load(f)
            video_delay = data.get("startTimeout", 10) * 1000
            config_delay = 2000 # Default apply config after 2s idle
    except: pass

    # Get video list
    try:
        videos = [f for f in os.listdir(VIDEO_PATH) if f.lower().endswith((".mp4", ".mov", ".avi"))]
    except:
        videos = []

    return templates.TemplateResponse(
        request=request, name="pages/home.html", context={"video_delay": video_delay, "config_delay": config_delay, "videos": videos}
    )

@app.get("/page/payment")
async def page_payment(request: Request):
    shared_state["current_page"] = "payment"
    send_uart("[State] payment")
    mode = request.query_params.get("mode")
    machine_system = get_machine_system()
    delay_time = get_delay_time()
    return templates.TemplateResponse(
        request=request, name="pages/payment.html", context={"mode": mode, "machine_system": machine_system, "delay_time": delay_time}
    )

@app.get("/page/cash")
async def page_cash(request: Request):
    # apply pending UART config ก่อนอ่านค่า
    if shared_state.get("pending_config"):
        try:
            with open(os.path.join(BASE_DIR, "Global_data.json"), "r") as f:
                current_data = json.load(f)
            _merge_pending(current_data, shared_state["pending_config"])
            with open(os.path.join(BASE_DIR, "Global_data.json"), "w") as f:
                json.dump(current_data, f, indent=2)
            shared_state["pending_config"] = None
        except:
            pass

    shared_state["current_page"] = "cash"
    send_uart("[State] cash")
    mode = request.query_params.get("mode")
    
    with open(os.path.join(BASE_DIR, "Global_data.json"), "r") as f:
        data = json.load(f)
        
    prices = [0]*8
    if mode == "1": # Shoe
        cfg = data.get("catpaw_config", {})
        prices = [
            cfg.get("dust_p", 10), cfg.get("bact_p", 10), cfg.get("uv_p", 10),
            cfg.get("ozone_p", 10), cfg.get("dry_p", 10), cfg.get("perfume_p", 10),
            10, 10
        ]
    else: # Carwash / General
        prices = data.get("fn_price", [10]*8)

    return templates.TemplateResponse(
        request=request, name="pages/cash.html", context={"mode": mode, "prices": prices}
    )

@app.get("/page/qr")
async def page_qr(request: Request):
    shared_state["current_page"] = "qr"
    send_uart("[State] qr")
    mode = request.query_params.get("mode")
    return templates.TemplateResponse(
        request=request, name="pages/qr.html", context={"mode": mode}
    )

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
            return JSONResponse(content=json.load(f))
    except:
        return JSONResponse(content={})

@app.post("/api/settings")
async def post_settings(request: Request):
    new_settings = await request.json()
    try:
        with open(os.path.join(BASE_DIR, "Global_data.json"), "r") as f:
            data = json.load(f)
        
        # Merge logic
        data.update(new_settings)
        
        with open(os.path.join(BASE_DIR, "Global_data.json"), "w") as f:
            json.dump(data, f, indent=2)
        
        # Forward to ESP if needed
        # send_uart("[Config] ...")
        
        return JSONResponse(content={"ok": True})
    except:
        return JSONResponse(content={"ok": False}, status_code=500)

@app.post("/api/maintenance/submit")
async def maint_submit():
    # Trigger ESP to save current preference
    send_uart("[Maint] Submit")
    return {"ok": True}

@app.get("/api/action_state")
async def api_action_state():
    return JSONResponse(content={
        "action": shared_state.get("last_action", ""),
        "action_event": shared_state.get("last_action_event"),
        "state_event": shared_state.get("last_state_event"),
        "schedule_event": shared_state.get("last_schedule_event"),
        "mqtt_status": shared_state.get("mqtt_status", "offline"),
        "countdown": shared_state.get("countdown", "--"),
        "balance": shared_state.get("balance", 0)
    })

@app.get("/api/test_values")
async def get_test_values():
    return JSONResponse(content={
        "ozone": shared_state["ozone_val"],
        "uv": shared_state["uv_val"],
        "temp": shared_state["temp_val"],
        "humi": shared_state["humi_val"],
        "dust": shared_state["dust_val"],
        "flow": shared_state["flow_val"],
        # Mock coin/bank based on flow for testing
        "coin": shared_state["flow_val"],
        "bank": shared_state["dust_val"]
    })

@app.get("/api/uart_send")
async def api_uart_send(msg: str):
    send_uart(msg)
    return {"ok": True}

@app.post("/api/apply_pending_config")
async def api_apply_pending():
    if shared_state.get("pending_config"):
        try:
            with open(os.path.join(BASE_DIR, "Global_data.json"), "r") as f:
                current_data = json.load(f)
            
            _merge_pending(current_data, shared_state["pending_config"])
            
            with open(os.path.join(BASE_DIR, "Global_data.json"), "w") as f:
                json.dump(current_data, f, indent=2)
            
            shared_state["pending_config"] = None
            print("UART CONFIG: Successfully updated Global_data.json from pending config")
            return {"status": "success"}
        except Exception as e:
            print(f"UART CONFIG ERROR: {e}")
            return {"status": "error", "message": str(e)}
    return {"status": "no_pending_config"}

# --- WiFi API ---
@app.get("/api/wifi/status")
async def wifi_status():
    try:
        # Get SSID
        ssid = subprocess.check_output("iwgetid -r", shell=True).decode().strip()
        # Get IP
        ip = subprocess.check_output("hostname -I", shell=True).decode().split()[0]
        return {"ssid": ssid, "ip": ip}
    except:
        return {"ssid": None, "ip": None}

@app.get("/api/wifi/scan")
async def wifi_scan():
    try:
        # Try to trigger a rescan (needs sudo to be reliable)
        try:
            subprocess.run("sudo nmcli dev wifi rescan", shell=True, capture_output=True, timeout=5)
            time.sleep(2) 
        except:
            pass

        output = subprocess.check_output("sudo nmcli -t -f SSID,SIGNAL dev wifi", shell=True).decode()
        networks = []
        for line in output.split('\n'):
            line = line.strip()
            if not line:
                continue
            
            if ':' in line:
                parts = line.rsplit(':', 1)
                ssid = parts[0]
                signal = parts[1]
                if ssid: 
                    networks.append({"ssid": ssid, "signal": signal})
        
        try:
            networks.sort(key=lambda x: int(x["signal"]), reverse=True)
        except:
            pass

        seen = set()
        unique = []
        for n in networks:
            if n["ssid"] not in seen:
                seen.add(n["ssid"])
                unique.append(n)
        
        return {"networks": unique[:50]}
    except Exception as e:
        print(f"WIFI SCAN ERROR: {e}")
        return {"networks": []}

@app.post("/api/wifi/connect")
async def wifi_connect(request: Request):
    data = await request.json()
    ssid = data.get("ssid")
    password = data.get("password")
    try:
        # 1. Delete existing connection to avoid conflicts
        subprocess.run(f'sudo nmcli connection delete "{ssid}"', shell=True, capture_output=True)
        
        # 2. Add connection manually with explicit security (fixes 'property missing' error)
        add_cmd = (
            f'sudo nmcli connection add type wifi ifname wlan0 con-name "{ssid}" ssid "{ssid}" '
            f'-- wifi-sec.key-mgmt wpa-psk wifi-sec.psk "{password}"'
        )
        subprocess.run(add_cmd, shell=True, check=True, capture_output=True)
        
        # 3. Bring up the connection
        subprocess.run(f'sudo nmcli connection up "{ssid}"', shell=True, check=True, capture_output=True)
        
        return {"message": "Connected successfully"}
    except Exception as e:
        print(f"WIFI CONNECT ERROR: {e}")
        return {"message": f"Connection failed: {str(e)}"}, 400

@app.post("/api/wifi/forget")
async def wifi_forget(request: Request):
    data = await request.json()
    ssid = data.get("ssid")
    try:
        subprocess.run(f'sudo nmcli connection delete "{ssid}"', shell=True, check=True)
        return {"message": "Network forgotten"}
    except:
        return {"message": "Failed to forget network"}, 400

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)