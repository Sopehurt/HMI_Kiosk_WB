import serial
import time

print("--- UART FLOOD PING START ---")
try:
    ser = serial.Serial("/dev/serial0", 115200, timeout=1)
    print("Port /dev/serial0 opened.")
    for i in range(20):
        ser.write(b"[Action] Ping\n")
        print(f"Sent Ping {i+1}")
        line = ser.readline()
        if line:
            print(f"Received: {line}")
            break
        time.sleep(0.1)
    ser.close()
except Exception as e:
    print(f"Error: {e}")
print("--- UART FLOOD PING END ---")
