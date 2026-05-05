import sys
import os
from PyQt6.QtCore import QUrl, Qt
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWebEngineWidgets import QWebEngineView

class KioskBrowser(QWebEngineView):
    def keyPressEvent(self, event):
        # กดปุ่ม Esc เพื่อออกจากโปรแกรม
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            QApplication.quit()
        super().keyPressEvent(event)

def main():
    # แก้ปัญหาเรื่องการรันด้วย root (sudo)
    os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    sys.argv.append("--no-sandbox")
    
    # เพิ่ม Flags เพื่อรีดประสิทธิภาพและลดอาการภาพฉีกบน Pi Zero 2W
    sys.argv.append("--disable-gpu-compositing")
    sys.argv.append("--ignore-gpu-blocklist")
    sys.argv.append("--enable-gpu-rasterization")

    app = QApplication(sys.argv)
    
    # ซ่อนเคอร์เซอร์เมาส์ (เหมาะสำหรับงาน Kiosk หน้าจอสัมผัส)
    QApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)

    browser = KioskBrowser()
    
    # ชี้ไปที่ Backend (Localhost)
    browser.setUrl(QUrl("http://127.0.0.1:8000"))

    # --- ส่วนสำคัญเพื่อให้เต็มจอ 1024x600 ---
    browser.setFixedSize(1024, 600) 
    browser.showFullScreen()

    # ปิดแถบเลื่อน (Scrollbars)
    browser.page().settings().setAttribute(
        browser.page().settings().WebAttribute.ShowScrollBars, False
    )

    sys.exit(app.exec())

if __name__ == "__main__":
    main()