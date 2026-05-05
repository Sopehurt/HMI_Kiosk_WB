#!/bin/bash

# ==========================================
# Raspberry Pi Kiosk Auto-Setup Script
# ==========================================
# Run this script with: sudo ./setup_kiosk.sh
# ==========================================

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo ./setup_kiosk.sh)"
  exit 1
fi

# Get the actual user who ran the sudo command
if [ -n "$SUDO_USER" ]; then
  USER_NAME="$SUDO_USER"
else
  USER_NAME=$(whoami)
fi
APP_DIR="/home/$USER_NAME/HMI_Kiosk_WB"

echo "=== 1. Updating System & Installing Dependencies ==="
apt-get update
# Install X11, Python tools, and Watchdog
DEBIAN_FRONTEND=noninteractive apt-get install -y xserver-xorg xinit python3-venv python3-pyqt6 python3-pyqt6.qtwebengine watchdog git

echo "=== 2. System Configurations (Desktop, raspi-connect) ==="
# Set boot to CLI (Disable Desktop GUI login)
systemctl set-default multi-user.target
# Disable Raspi Connect if exists
systemctl disable rpi-connect 2>/dev/null
systemctl mask rpi-connect 2>/dev/null

echo "=== 3. Configuring Boot Options (UART, HDMI, Watchdog) ==="
CONFIG_FILE="/boot/firmware/config.txt"
if [ ! -f "$CONFIG_FILE" ]; then
    CONFIG_FILE="/boot/config.txt"
fi

# Enable UART
grep -q "^enable_uart=1" $CONFIG_FILE || echo "enable_uart=1" >> $CONFIG_FILE
# Disable Bluetooth to map UART0/ttyAMA0 cleanly
grep -q "^dtoverlay=disable-bt" $CONFIG_FILE || echo "dtoverlay=disable-bt" >> $CONFIG_FILE
# HDMI Settings for 1024x600 Kiosk Display
grep -q "^hdmi_ignore_cec=1" $CONFIG_FILE || echo "hdmi_ignore_cec=1" >> $CONFIG_FILE
grep -q "^hdmi_force_hotplug=1" $CONFIG_FILE || echo "hdmi_force_hotplug=1" >> $CONFIG_FILE
grep -q "^hdmi_drive=2" $CONFIG_FILE || echo "hdmi_drive=2" >> $CONFIG_FILE
grep -q "^disable_overscan=1" $CONFIG_FILE || echo "disable_overscan=1" >> $CONFIG_FILE
# Enable Hardware Watchdog
grep -q "^dtparam=watchdog=on" $CONFIG_FILE || echo "dtparam=watchdog=on" >> $CONFIG_FILE

echo "=== 4. Freeing up UART Console in cmdline.txt ==="
CMDLINE_FILE="/boot/firmware/cmdline.txt"
if [ ! -f "$CMDLINE_FILE" ]; then
    CMDLINE_FILE="/boot/cmdline.txt"
fi
# Remove console=serial0,115200 to allow Python to use UART
sed -i 's/console=serial0,115200 //g' $CMDLINE_FILE

echo "=== 5. Setting up Watchdog Service ==="
sed -i "s/#watchdog-device/watchdog-device/" /etc/watchdog.conf
sed -i "s/#watchdog-timeout/watchdog-timeout/" /etc/watchdog.conf
sed -i "s/#max-load-1 = 24/max-load-1 = 24/" /etc/watchdog.conf
systemctl enable watchdog
systemctl restart watchdog

echo "=== 6. Setting up Crontab for Daily Reboot (3 AM) ==="
(crontab -l 2>/dev/null | grep -v "/sbin/reboot"; echo "0 3 * * * /sbin/reboot") | crontab -

echo "=== 7. Setting up Sudoers for Passwordless Commands ==="
# Allow backend to run nmcli, iwlist, and reboot without sudo password, regardless of username
cat << EOF > /etc/sudoers.d/kiosk_nopasswd
ALL ALL=(ALL) NOPASSWD: /usr/bin/nmcli, /sbin/iwlist, /sbin/reboot
EOF
chmod 0440 /etc/sudoers.d/kiosk_nopasswd

echo "=== 8. Setting up Python Environment ==="
# Add user to dialout for UART access
usermod -a -G dialout,tty $USER_NAME

if [ ! -d "$APP_DIR" ]; then
    echo "Warning: Application directory $APP_DIR not found."
    echo "Please clone the repository to $APP_DIR before starting the services."
else
    # Create VENV and install requirements
    sudo -u $USER_NAME python3 -m venv $APP_DIR/venv
    sudo -u $USER_NAME $APP_DIR/venv/bin/pip install uvicorn fastapi pyserial
    if [ -f "$APP_DIR/requirements.txt" ]; then
        sudo -u $USER_NAME $APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt
    fi
fi

echo "=== 9. Creating Systemd Services ==="

# HMI Backend Service
cat << EOF > /etc/systemd/system/hmi_backend.service
[Unit]
Description=HMI Backend Uvicorn Service
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# HMI GUI Service (MemoryMax Added)
cat << EOF > /etc/systemd/system/hmi_gui.service
[Unit]
Description=HMI Frontend Kiosk
After=hmi_backend.service
Wants=hmi_backend.service

[Service]
Environment=DISPLAY=:0
ExecStartPre=-/usr/bin/pkill -9 Xorg
ExecStartPre=-/usr/bin/rm /tmp/.X0-lock
ExecStart=/usr/bin/xinit /usr/bin/env QT_QUICK_BACKEND=software QT_WEBENGINE_DISABLE_GPU=1 /usr/bin/python3 $APP_DIR/gui.py -- :0 -nocursor -s 0 -dpms
Restart=always
RestartSec=5
MemoryMax=300M
User=root

[Install]
WantedBy=multi-user.target
EOF

echo "=== 10. Enabling Services ==="
systemctl daemon-reload
systemctl enable hmi_backend.service
systemctl enable hmi_gui.service

echo "=========================================="
echo "Setup Complete! "
echo "Please reboot the Raspberry Pi to apply all changes (UART, Display, Services)."
echo "Run: sudo reboot"
echo "=========================================="
