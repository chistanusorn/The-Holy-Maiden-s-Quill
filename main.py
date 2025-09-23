import sys
import os
import mss
import mss.tools
from threading import Thread
from pynput import keyboard
import qdarktheme
from PyQt5.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout, QTextEdit, QPushButton
from PyQt5.QtGui import QPainter, QColor, QFont, QIcon, QPen
from PyQt5.QtCore import Qt, pyqtSignal, QObject, QRect

# --- ส่วนของการนำเข้าและตั้งค่า Google Cloud ---
try:
    from google.cloud import vision
    from google.cloud import translate_v2 as translate
except ImportError:
    print("เกิดข้อผิดพลาด: กรุณาติดตั้งไลบรารีของ Google Cloud ก่อน:\npip install google-cloud-vision google-cloud-translate")
    sys.exit()

try:
    # !!!!!! จุดสำคัญ: กรุณาเปลี่ยน PATH/TO/YOUR/KEY.json เป็นที่อยู่ไฟล์ key ของคุณ !!!!!!
    SERVICE_ACCOUNT_FILE = r"C:\Users\ASUS\Documents\maejo\3\MIS\The Holy Maiden's Quill\translate-471715-b037641fbe19.json"
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = SERVICE_ACCOUNT_FILE
    vision_client = vision.ImageAnnotatorClient()
    translate_client = translate.Client()
except Exception as e:
    print(f"เกิดข้อผิดพลาดในการเชื่อมต่อ Google Cloud: {e}")
    sys.exit()

# --- ส่วนประมวลผล (Helper Functions) ---
def get_text_and_bounds(image_bytes):
    try:
        image = vision.Image(content=image_bytes)
        response = vision_client.text_detection(image=image, image_context={"language_hints": ["ja"]})
        texts = response.text_annotations
        if texts:
            main_block = texts[0]
            description = main_block.description.replace('\n', ' ')
            return description, main_block.bounding_poly
        return "", None
    except Exception as e:
        print(f"เกิดข้อผิดพลาดระหว่างการทำ OCR: {e}")
        return None, None

def translate_text(text, target_language='th'):
    if not text: return ""
    try:
        result = translate_client.translate(text, target_language=target_language)
        return result['translatedText']
    except Exception as e:
        print(f"เกิดข้อผิดพลาดระหว่างการแปลภาษา: {e}")
        return "[ไม่สามารถแปลภาษาได้]"

# --- ส่วนจัดการการสื่อสารระหว่าง Thread (Signals & Slots) ---
class Communicate(QObject):
    crop_requested = pyqtSignal() 
    translation_ready = pyqtSignal(str, str, QRect)

# --- หน้าต่างควบคุม (Control Panel) ---
class ControlPanelWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("The Holy Maiden's Quill")
        self.setWindowIcon(QIcon("icon.ico"))
        self.setMinimumSize(450, 500)

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(20, 20, 20, 20)
        self.layout.setSpacing(15)

        self.info_label = QLabel("กด 'F9' เพื่อเลือกพื้นที่ | กด 'Esc' เพื่อปิดคำแปล")
        self.info_label.setFont(QFont("Segoe UI", 11))
        self.info_label.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(self.info_label)

        self.crop_button = QPushButton("เลือกพื้นที่เพื่อแปล (F9)")
        self.crop_button.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.crop_button.setMinimumHeight(40)
        self.layout.addWidget(self.crop_button)

        self.title_label = QLabel("📖 บันทึกคำแปลล่าสุด")
        self.title_label.setFont(QFont("Segoe UI", 12, QFont.Bold))
        self.layout.addWidget(self.title_label)

        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setFont(QFont("Segoe UI", 10))
        self.layout.addWidget(self.log_display)
    
    def add_to_log(self, original_text, translated_text, rect):
        log_entry = f"ต้นฉบับ: {original_text}\nคำแปล: {translated_text}\n{'-'*50}"
        self.log_display.append(log_entry)


# --- หน้าต่าง Overlay ---
class OverlayWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.translated_text = ""
        self.text_rect = QRect(0, 0, 0, 0)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setGeometry(QApplication.desktop().screenGeometry())
        self.hide()

    def update_translation(self, original_text, translated_text, rect):
        self.translated_text = translated_text
        self.text_rect = rect
        self.update()
        self.show()

    def hide_overlay(self):
        self.hide()

    def paintEvent(self, event):
        if not self.translated_text:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        bg_color = QColor(0, 0, 0, 180)
        painter.setBrush(bg_color)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.text_rect.adjusted(-15, -10, 15, 20), 5, 5)
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Segoe UI", 18))
        painter.drawText(self.text_rect, Qt.AlignCenter | Qt.TextWordWrap, self.translated_text)

# --- หน้าต่างสำหรับการเลือกพื้นที่ (Crop) ---
class CropWindow(QWidget):
    crop_completed = pyqtSignal(QRect)

    def __init__(self):
        super().__init__()
        self.start_pos = None
        self.end_pos = None
        
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setCursor(Qt.CrossCursor)
        self.setGeometry(QApplication.desktop().screenGeometry())
        
        # ใช้วิธีทำให้หน้าต่างทั้งบานโปร่งแสง ซึ่งเข้ากันได้กับทุกระบบ
        self.setWindowOpacity(0.3) 

    def paintEvent(self, event):
        # วาดแค่เส้นขอบของพื้นที่ที่เลือกเท่านั้น
        if self.start_pos and self.end_pos:
            painter = QPainter(self)
            crop_rect = QRect(self.start_pos, self.end_pos).normalized()
            painter.setPen(QPen(QColor(255, 255, 255), 2, Qt.SolidLine))
            painter.drawRect(crop_rect)

    def mousePressEvent(self, event):
        self.start_pos = event.pos()
        self.end_pos = event.pos()
        self.update()

    def mouseMoveEvent(self, event):
        self.end_pos = event.pos()
        self.update()

    def mouseReleaseEvent(self, event):
        self.hide()
        crop_rect = QRect(self.start_pos, self.end_pos).normalized()
        self.crop_completed.emit(crop_rect)
        self.close()

# --- ส่วนควบคุมหลัก ---
class Controller:
    def __init__(self, overlay_window, control_panel_window):
        self.overlay = overlay_window
        self.control_panel = control_panel_window
        self.crop_window = None
        self.comm = Communicate()
        
        self.comm.translation_ready.connect(self.overlay.update_translation)
        self.comm.translation_ready.connect(self.control_panel.add_to_log)
        self.comm.crop_requested.connect(self.trigger_cropping)

    def trigger_cropping(self):
        if self.crop_window and self.crop_window.isVisible():
            return
        self.crop_window = CropWindow()
        self.crop_window.show()
        self.crop_window.crop_completed.connect(self.perform_translation)
        
    def request_crop_from_hotkey(self):
        self.comm.crop_requested.emit()

    def perform_translation(self, crop_rect):
        print(f"Area selected: {crop_rect}. Capturing...")
        if crop_rect.width() < 5 or crop_rect.height() < 5:
            print("Selected area is too small.")
            return

        monitor = {"top": crop_rect.top(), "left": crop_rect.left(), "width": crop_rect.width(), "height": crop_rect.height()}
        with mss.mss() as sct:
            sct_img = sct.grab(monitor)
            img_bytes = mss.tools.to_png(sct_img.rgb, sct_img.size)

        japanese_text, bounds = get_text_and_bounds(img_bytes)
        if japanese_text and bounds:
            print("Text found. Translating...")
            thai_text = translate_text(japanese_text)
            
            base_position = crop_rect.topLeft()
            v = bounds.vertices
            text_rect_in_crop = QRect(v[0].x, v[0].y, v[1].x - v[0].x, v[2].y - v[0].y)
            final_text_rect = text_rect_in_crop.translated(base_position)
            final_text_rect.setHeight(final_text_rect.height() + 20)
            vertical_offset = 20 
            final_text_rect.translate(0, vertical_offset)
            self.comm.translation_ready.emit(japanese_text, thai_text, final_text_rect)
        else:
            print("No text found in selected area.")

def start_hotkey_listener(controller):
    
    def on_crop_activate():
        print("Crop hotkey '<f9>' activated!")
        controller.request_crop_from_hotkey()

    def on_close_activate():
        print("Close hotkey '<esc>' activated!")
        controller.overlay.hide_overlay()

    hotkeys = keyboard.GlobalHotKeys({
        '<f9>': on_crop_activate,
        '<esc>': on_close_activate
    })
    
    print("Hotkey listener started using GlobalHotKeys...")
    hotkeys.run()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(qdarktheme.load_stylesheet('dark'))

    control_panel = ControlPanelWindow()
    overlay = OverlayWindow()
    controller = Controller(overlay, control_panel)

    control_panel.crop_button.clicked.connect(controller.trigger_cropping)
    
    # --- เริ่มการทำงานของ Hotkey Listener ใน Thread แยก ---
    listener_thread = Thread(target=start_hotkey_listener, args=(controller,), daemon=True)
    listener_thread.start()
    # ----------------------------------------
    
    print("="*40)
    print("Philia Translator is ready.")
    print("Press 'f9' to start cropping.")
    print("Press 'Esc' to hide the overlay.")
    print("="*40)
    
    control_panel.show()
    sys.exit(app.exec_())