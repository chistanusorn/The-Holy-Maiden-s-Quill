import json
import logging
import os
import sys
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from hashlib import sha256
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from threading import Thread
from unicodedata import category

try:
    from pythainlp.tokenize import word_tokenize
    HAS_PYTHAINLP = True
except Exception:
    HAS_PYTHAINLP = False

import mss
import mss.tools
import qdarktheme
from pynput import keyboard
from PyQt5.QtCore import QObject, QRect, Qt, QTimer, pyqtSignal, QPoint
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPen, QImage
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "The Holy Maiden's Quill"
GOOGLE_CREDENTIALS_ENV = "GOOGLE_APPLICATION_CREDENTIALS"
LOCAL_CREDENTIALS_NAME = "google_credentials.json"
LOCAL_CREDENTIALS_PATTERNS = (LOCAL_CREDENTIALS_NAME, "translate-*.json")
OVERLAY_MARGIN = 8
OVERLAY_PADDING = 12
MIN_OVERLAY_WIDTH = 220
MAX_OVERLAY_WIDTH = 520
MIN_OVERLAY_HEIGHT = 64
MAX_OVERLAY_HEIGHT = 620
CONTINUOUS_INTERVAL_MS = 1500
SCENE_HISTORY_LIMIT = 20
TRANSLATION_CACHE_LIMIT = 100
 
LANG_MAP = {
    "TH": {"code": "th", "name": "Thai"},
    "EN": {"code": "en", "name": "English"},
    "JP": {"code": "ja", "name": "Japanese"},
    "KR": {"code": "ko", "name": "Korean"},
    "CH": {"code": "zh", "name": "Chinese"},
}


@dataclass
class TextRegion:
    original: str
    source_rect: QRect
    translated: str = ""
    overlay_rect: QRect = field(default_factory=QRect)


class TranslatedRegionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.regions = {}
        self.current_region = None

    def handle_starttag(self, tag, attrs):
        if tag != "p":
            return

        attributes = dict(attrs)
        region_index = attributes.get("data-region")
        if region_index is not None:
            self.current_region = int(region_index)
            self.regions[self.current_region] = ""

    def handle_endtag(self, tag):
        if tag == "p":
            self.current_region = None

    def handle_data(self, data):
        if self.current_region is not None:
            self.regions[self.current_region] += data


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(name):
    base_path = Path(getattr(sys, "_MEIPASS", app_dir()))
    return base_path / name


def find_local_credentials():
    for pattern in LOCAL_CREDENTIALS_PATTERNS:
        matches = sorted(app_dir().glob(pattern))
        if matches:
            return matches[0]
    return None


logging.basicConfig(
    filename=app_dir() / "translator.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def show_error(title, message):
    logging.error("%s: %s", title, message)
    if QApplication.instance():
        QMessageBox.critical(None, title, message)
    else:
        print(f"{title}: {message}", file=sys.stderr)


class GoogleCloudService:
    def __init__(self):
        self._vision = None
        self._translate = None
        self._vision_client = None
        self._translate_client = None

    def _ensure_credentials(self):
        credentials_path = os.environ.get(GOOGLE_CREDENTIALS_ENV)
        local_credentials = find_local_credentials()

        if not credentials_path and local_credentials:
            credentials_path = str(local_credentials)
            os.environ[GOOGLE_CREDENTIALS_ENV] = credentials_path

        if credentials_path and not Path(credentials_path).exists():
            raise RuntimeError(f"Credential file not found: {credentials_path}")

    def _ensure_clients(self):
        if self._vision_client and self._translate_client:
            return

        self._ensure_credentials()
        try:
            from google.cloud import translate_v2 as translate
            from google.cloud import vision
        except ImportError as exc:
            raise RuntimeError(
                "Missing Google Cloud libraries. Install google-cloud-vision and "
                "google-cloud-translate."
            ) from exc

        try:
            self._vision = vision
            self._translate = translate
            self._vision_client = vision.ImageAnnotatorClient()
            self._translate_client = translate.Client()
        except Exception as exc:
            raise RuntimeError(f"Cannot connect to Google Cloud: {exc}") from exc

    def get_text_regions(self, image_bytes, scale):
        self._ensure_clients()
        image = self._vision.Image(content=image_bytes)
        response = self._vision_client.text_detection(
            image=image,
            image_context={"language_hints": ["ja"]},
        )
        if response.error.message:
            raise RuntimeError(response.error.message)

        regions = []
        annotation = response.full_text_annotation
        for page in annotation.pages:
            for block in page.blocks:
                for paragraph in block.paragraphs:
                    text = paragraph_text(paragraph)
                    rect = bounds_to_rect(paragraph.bounding_box, scale)
                    if text and rect.isValid():
                        regions.append(TextRegion(text, rect))

        if regions:
            return merge_nearby_regions(regions)

        texts = response.text_annotations
        if not texts:
            return []

        main_block = texts[0]
        return [
            TextRegion(
                main_block.description.replace("\n", " ").strip(),
                bounds_to_rect(main_block.bounding_poly, scale),
            )
        ]

    def translate_text(self, text, target_language="th"):
        if not text:
            return ""

        self._ensure_clients()
        result = self._translate_client.translate(text, target_language=target_language)
        return unescape(result["translatedText"])

    def translate_batch(self, texts, target_language="th"):
        if not texts:
            return []

        self._ensure_clients()
        try:
            contextual_text = "".join(
                f'<p data-region="{index}">{escape(text)}</p>'
                for index, text in enumerate(texts)
            )
            result = self._translate_client.translate(
                contextual_text,
                target_language=target_language,
                format_="html",
            )
            parser = TranslatedRegionParser()
            parser.feed(result["translatedText"])
            if len(parser.regions) != len(texts):
                raise ValueError("Contextual translation did not preserve every region.")
            return [parser.regions[index].strip() for index in range(len(texts))]
        except Exception:
            logging.exception(
                "Contextual translation failed. Falling back to independent batch."
            )

        try:
            results = self._translate_client.translate(
                texts,
                target_language=target_language,
            )
        except Exception:
            logging.exception("Independent batch failed. Falling back to single requests.")
            return [self.translate_text(text, target_language) for text in texts]

        if isinstance(results, dict):
            results = [results]

        return [unescape(result["translatedText"]) for result in results]


def virtual_screen_geometry():
    screens = QApplication.screens()
    if not screens:
        return QApplication.desktop().screenGeometry()

    geometry = QRect(screens[0].geometry())
    for screen in screens[1:]:
        geometry = geometry.united(screen.geometry())
    return geometry


def screen_for_rect(rect):
    screen = QApplication.screenAt(rect.center())
    if screen:
        return screen
    return QApplication.primaryScreen()


def capture_screen_region(rect):
    screen = screen_for_rect(rect)
    screens = QApplication.screens()
    screen_index = screens.index(screen) if screen in screens else 0
    screen_geometry = screen.geometry()
    scale = screen.devicePixelRatio()

    with mss.mss() as sct:
        mss_monitor = (
            sct.monitors[screen_index + 1]
            if screen_index + 1 < len(sct.monitors)
            else sct.monitors[0]
        )
        monitor = {
            "top": mss_monitor["top"] + round((rect.top() - screen_geometry.top()) * scale),
            "left": mss_monitor["left"] + round((rect.left() - screen_geometry.left()) * scale),
            "width": max(1, round(rect.width() * scale)),
            "height": max(1, round(rect.height() * scale)),
        }
        image = sct.grab(monitor)
        return mss.tools.to_png(image.rgb, image.size), scale


def paragraph_text(paragraph):
    parts = []
    for word in paragraph.words:
        for symbol in word.symbols:
            parts.append(symbol.text)
            detected_break = getattr(getattr(symbol, "property", None), "detected_break", None)
            break_type = getattr(detected_break, "type_", 0)
            if break_type in (1, 2, 3, 5):
                parts.append(" ")

    return " ".join("".join(parts).split())


def bounds_to_rect(bounds, scale):
    vertices = list(bounds.vertices)
    if not vertices:
        return QRect()

    xs = [getattr(vertex, "x", 0) for vertex in vertices]
    ys = [getattr(vertex, "y", 0) for vertex in vertices]
    left = round(min(xs) / scale)
    top = round(min(ys) / scale)
    right = round(max(xs) / scale)
    bottom = round(max(ys) / scale)
    return QRect(left, top, max(1, right - left), max(1, bottom - top))


def horizontal_overlap_ratio(first, second):
    overlap = max(0, min(first.right(), second.right()) - max(first.left(), second.left()))
    return overlap / max(1, min(first.width(), second.width()))


def vertical_overlap_ratio(first, second):
    overlap = max(0, min(first.bottom(), second.bottom()) - max(first.top(), second.top()))
    return overlap / max(1, min(first.height(), second.height()))


def rect_gap(first, second):
    horizontal = max(0, max(first.left(), second.left()) - min(first.right(), second.right()))
    vertical = max(0, max(first.top(), second.top()) - min(first.bottom(), second.bottom()))
    return horizontal, vertical


def should_merge_regions(first, second):
    first_rect = first.source_rect
    second_rect = second.source_rect
    horizontal_gap, vertical_gap = rect_gap(first_rect, second_rect)

    same_column = (
        horizontal_overlap_ratio(first_rect, second_rect) >= 0.25
        and vertical_gap <= max(18, min(first_rect.height(), second_rect.height()))
    )
    same_line = (
        vertical_overlap_ratio(first_rect, second_rect) >= 0.45
        and horizontal_gap <= max(16, min(first_rect.width(), second_rect.width()) // 3)
    )
    return same_column or same_line


def merge_nearby_regions(regions):
    merged = []
    for region in sorted(regions, key=lambda item: (item.source_rect.top(), item.source_rect.left())):
        target = None
        for candidate in merged:
            if should_merge_regions(candidate, region):
                target = candidate
                break

        if target:
            target.original = f"{target.original} {region.original}".strip()
            target.source_rect = target.source_rect.united(region.source_rect)
        else:
            merged.append(TextRegion(region.original, QRect(region.source_rect)))

    return merged


def keep_rect_visible(rect):
    screen_geometry = virtual_screen_geometry()
    rect = QRect(rect)

    if rect.right() > screen_geometry.right():
        rect.moveRight(screen_geometry.right())
    if rect.bottom() > screen_geometry.bottom():
        rect.moveBottom(screen_geometry.bottom())
    if rect.left() < screen_geometry.left():
        rect.moveLeft(screen_geometry.left())
    if rect.top() < screen_geometry.top():
        rect.moveTop(screen_geometry.top())

    return rect


def contains_japanese(text):
    # Modified to allow any language (checks if the text contains any letters/alphabets)
    for char in text:
        if category(char).startswith("L"):
            return True
    return False


def text_units(text):
    units = []
    current = ""
    for char in text:
        if not current or category(char).startswith("M"):
            current += char
        else:
            units.append(current)
            current = char
    if current:
        units.append(current)
    return units


def split_long_token(token, metrics, max_width):
    lines = []
    current = ""
    for unit in text_units(token):
        candidate = current + unit
        if not current or metrics.horizontalAdvance(candidate) <= max_width:
            current = candidate
            continue

        lines.append(current)
        current = unit

    if current:
        lines.append(current)
    return lines


def wrap_text_lines(text, font, max_width):
    metrics = QFontMetrics(font)
    wrap_limit = max(10, max_width - 8)  # 8px safety margin to avoid QPainter accidental wraps
    lines = []

    for paragraph in text.splitlines() or [text]:
        # Tokenize paragraph using pythainlp if available
        use_pythainlp = HAS_PYTHAINLP
        words = []
        if use_pythainlp:
            try:
                words = word_tokenize(paragraph)
            except Exception:
                use_pythainlp = False

        if not use_pythainlp:
            words = paragraph.split()

        if not words:
            lines.append("")
            continue

        current = ""
        for word in words:
            if use_pythainlp:
                candidate = current + word
            else:
                candidate = f"{current} {word}" if current else word

            # Measure width of candidate (strip for measurement to ignore trailing whitespace impact)
            measured_candidate = candidate.strip()
            if not measured_candidate or metrics.horizontalAdvance(measured_candidate) <= wrap_limit:
                current = candidate
                continue

            # If it doesn't fit, commit current line
            if current:
                stripped_current = current.strip()
                if stripped_current:
                    lines.append(stripped_current)
                current = ""

            # Ignore leading spaces on the new line
            if word.strip() == "":
                continue

            # Measure word itself
            if metrics.horizontalAdvance(word.strip()) <= wrap_limit:
                current = word
            else:
                # If a single word is too long to fit, split it character-by-character
                broken = split_long_token(word, metrics, wrap_limit)
                for part in broken[:-1]:
                    stripped_part = part.strip()
                    if stripped_part:
                        lines.append(stripped_part)
                current = broken[-1] if broken else ""

        if current:
            stripped_current = current.strip()
            if stripped_current:
                lines.append(stripped_current)

    return lines


def wrapped_text(text, font, max_width):
    return "\n".join(wrap_text_lines(text, font, max_width))


def text_box_size(source_rect, text, font):
    metrics = QFontMetrics(font)
    natural_width = max(
        metrics.horizontalAdvance(line)
        for line in (text.splitlines() or [text])
    ) + (OVERLAY_PADDING * 2)
    width = max(
        MIN_OVERLAY_WIDTH,
        min(MAX_OVERLAY_WIDTH, max(source_rect.width() + 120, natural_width)),
    )
    if len(text) > 45:
        width = min(MAX_OVERLAY_WIDTH, max(width, 300))
    if len(text) > 90:
        width = MAX_OVERLAY_WIDTH

    text_width = width - (OVERLAY_PADDING * 2)
    lines = wrap_text_lines(text, font, text_width)
    line_height = metrics.lineSpacing()
    height = max(MIN_OVERLAY_HEIGHT, (line_height * max(1, len(lines))) + (OVERLAY_PADDING * 2))
    return width, min(height, MAX_OVERLAY_HEIGHT)


def padded_intersects(rect, others, padding=6):
    padded = QRect(rect).adjusted(-padding, -padding, padding, padding)
    return any(padded.intersects(other) for other in others)


def candidate_overlay_rects(source_rect, width, height):
    margin = OVERLAY_MARGIN
    center_x = source_rect.center().x() - (width // 2)
    return [
        QRect(source_rect.right() + margin, source_rect.top(), width, height),
        QRect(source_rect.left() - width - margin, source_rect.top(), width, height),
        QRect(center_x, source_rect.bottom() + margin, width, height),
        QRect(center_x, source_rect.top() - height - margin, width, height),
        QRect(source_rect.right() + margin, source_rect.bottom() + margin, width, height),
        QRect(source_rect.left() - width - margin, source_rect.bottom() + margin, width, height),
        QRect(source_rect.right() + margin, source_rect.top() - height - margin, width, height),
        QRect(source_rect.left() - width - margin, source_rect.top() - height - margin, width, height),
    ]


def place_overlay_regions(regions):
    font = QFont("Segoe UI", 14, QFont.Bold)
    source_rects = [region.source_rect for region in regions]
    placed_rects = []

    for region in sorted(regions, key=lambda item: (item.source_rect.top(), item.source_rect.left())):
        text = region.translated or region.original
        width, height = text_box_size(region.source_rect, text, font)
        chosen = None

        for candidate in candidate_overlay_rects(region.source_rect, width, height):
            candidate = keep_rect_visible(candidate)
            if padded_intersects(candidate, placed_rects):
                continue
            if padded_intersects(candidate, source_rects):
                continue
            chosen = candidate
            break

        if chosen is None:
            chosen = keep_rect_visible(
                QRect(
                    region.source_rect.left(),
                    region.source_rect.top(),
                    width,
                    height,
                )
            )

        region.overlay_rect = chosen
        placed_rects.append(chosen)

    return regions


class Communicate(QObject):
    crop_requested = pyqtSignal()
    hide_requested = pyqtSignal()
    job_ready = pyqtSignal(object)
    job_failed = pyqtSignal(str)
    job_finished = pyqtSignal()
    translation_ready = pyqtSignal(object)


class ControlPanelWindow(QWidget):
    def __init__(self):
        self.is_loading = True
        super().__init__()
        self.setWindowTitle(APP_NAME)
        icon_path = resource_path("icon.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setMinimumSize(450, 600)

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

        self.continuous_checkbox = QCheckBox("Continuous mode: watch the latest area")
        self.continuous_checkbox.setFont(QFont("Segoe UI", 10))
        self.layout.addWidget(self.continuous_checkbox)

        # Settings Widget for Translation Engine & API Key
        self.settings_widget = QWidget()
        self.settings_layout = QFormLayout(self.settings_widget)
        self.settings_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_layout.setSpacing(10)

        self.engine_combo = QComboBox()
        self.engine_combo.addItems([
            "Google Translate (เดิม)",
            "9arm API (Qwen3.6)",
            "9arm API (Gemma)",
            "Gemini API (Google)",
            "API Gateway (Local)"
        ])
        self.engine_combo.setFont(QFont("Segoe UI", 10))
        self.settings_layout.addRow("ระบบแปลภาษา:", self.engine_combo)

        self.lang_combo = QComboBox()
        self.lang_combo.addItems(["TH", "EN", "JP", "KR", "CH"])
        self.lang_combo.setFont(QFont("Segoe UI", 10))
        self.settings_layout.addRow("ภาษาปลายทาง:", self.lang_combo)

        self.api_key_input = QLineEdit()
        self.api_key_input.setPlaceholderText("ใส่ API Key...")
        self.api_key_input.setFont(QFont("Segoe UI", 10))
        self.api_key_input.setEchoMode(QLineEdit.Password)

        self.show_key_checkbox = QCheckBox("แสดงคีย์")
        self.show_key_checkbox.setFont(QFont("Segoe UI", 9))
        self.show_key_checkbox.toggled.connect(
            lambda checked: self.api_key_input.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
        )

        key_layout = QVBoxLayout()
        key_layout.addWidget(self.api_key_input)
        key_layout.addWidget(self.show_key_checkbox)
        self.settings_layout.addRow("API Key:", key_layout)

        self.layout.addWidget(self.settings_widget)

        self.status_label = QLabel("Ready")
        self.status_label.setFont(QFont("Segoe UI", 9))
        self.status_label.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(self.status_label)

        self.title_label = QLabel("บันทึกคำแปลล่าสุด")
        self.title_label.setFont(QFont("Segoe UI", 12, QFont.Bold))
        self.layout.addWidget(self.title_label)

        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setFont(QFont("Segoe UI", 10))
        self.layout.addWidget(self.log_display)

        # Connect settings events and load existing config
        self.engine_combo.currentIndexChanged.connect(self.toggle_api_key_visibility)
        self.engine_combo.currentIndexChanged.connect(self.save_settings)
        self.lang_combo.currentIndexChanged.connect(self.save_settings)
        self.api_key_input.textChanged.connect(self.save_settings)
        self.load_settings()
        self.is_loading = False

    def load_settings(self):
        config_path = app_dir() / "config.json"
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    engine = data.get("engine", "google")
                    api_key = data.get("gemini_api_key", "")
                    target_lang = data.get("target_lang", "TH")
                    
                    if engine == "9arm_qwen":
                        self.engine_combo.setCurrentIndex(1)
                    elif engine == "9arm_gemma":
                        self.engine_combo.setCurrentIndex(2)
                    elif engine == "gemini":
                        self.engine_combo.setCurrentIndex(3)
                    elif engine == "api_gateway":
                        self.engine_combo.setCurrentIndex(4)
                    else:
                        self.engine_combo.setCurrentIndex(0)
                    self.api_key_input.setText(api_key)
                    
                    lang_index = self.lang_combo.findText(target_lang)
                    if lang_index >= 0:
                        self.lang_combo.setCurrentIndex(lang_index)
            except Exception as e:
                logging.error(f"Failed to load settings: {e}")
        self.toggle_api_key_visibility()

    def save_settings(self):
        config_path = app_dir() / "config.json"
        engine_idx = self.engine_combo.currentIndex()
        if engine_idx == 1:
            engine = "9arm_qwen"
            engine_name = "9arm API (Qwen3.6)"
        elif engine_idx == 2:
            engine = "9arm_gemma"
            engine_name = "9arm API (Gemma)"
        elif engine_idx == 3:
            engine = "gemini"
            engine_name = "Gemini API (Google)"
        elif engine_idx == 4:
            engine = "api_gateway"
            engine_name = "API Gateway (Local)"
        else:
            engine = "google"
            engine_name = "Google Translate (เดิม)"
            
        api_key = self.api_key_input.text().strip()
        target_lang = self.lang_combo.currentText()
        data = {
            "engine": engine,
            "gemini_api_key": api_key,
            "target_lang": target_lang
        }
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            if not getattr(self, "is_loading", False):
                self.set_status(f"เปลี่ยนระบบแปลภาษาเป็น: {engine_name}")
                logging.info(f"Engine changed to: {engine_name}")
        except Exception as e:
            logging.error(f"Failed to save settings: {e}")

    def toggle_api_key_visibility(self):
        needs_key = self.engine_combo.currentIndex() in (1, 2, 3)
        self.api_key_input.setEnabled(needs_key)
        self.show_key_checkbox.setEnabled(needs_key)

    def add_page_to_log(self, regions):
        if not regions:
            return

        engine_name = self.engine_combo.currentText()
        lines = [f"แปลทั้งหน้า [ใช้ระบบ: {engine_name}]:"]
        for index, region in enumerate(regions, 1):
            lines.append(f"{index}. ต้นฉบับ: {region.original}")
            lines.append(f"   คำแปล: {region.translated}")
        lines.append("-" * 50)
        log_entry = "\n".join(lines)
        self.log_display.append(log_entry)

    def set_status(self, message):
        self.status_label.setText(message)


class TextBubbleOverlay(QWidget):
    def __init__(self, region, parent=None):
        super().__init__(parent)
        self.region = region
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setMinimumSize(100, 45)
        
        self.drag_position = None
        self.resize_zone = 0
        self.initial_geometry = None
        self.overlay_font = QFont("Segoe UI", 14, QFont.Bold)
        self.setGeometry(region.overlay_rect)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(self.overlay_font)

        box = self.rect()
        text_rect = box.adjusted(
            OVERLAY_PADDING,
            OVERLAY_PADDING,
            -OVERLAY_PADDING,
            -OVERLAY_PADDING,
        )
        painter.setBrush(QColor(0, 0, 0, 225))
        painter.setPen(QPen(QColor(255, 255, 255, 100), 1))
        painter.drawRoundedRect(box, 8, 8)
        
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(
            text_rect,
            Qt.AlignCenter,
            wrapped_text(self.region.translated, self.overlay_font, text_rect.width()),
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.pos()
            rect = self.rect()
            border = 8
            
            self.resize_zone = 0
            if pos.x() < border:
                self.resize_zone |= 1  # LEFT
            elif pos.x() > rect.width() - border:
                self.resize_zone |= 2  # RIGHT
                
            if pos.y() < border:
                self.resize_zone |= 4  # TOP
            elif pos.y() > rect.height() - border:
                self.resize_zone |= 8  # BOTTOM
                
            self.drag_position = event.globalPos()
            self.initial_geometry = self.geometry()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton:
            if self.drag_position is None:
                return
                
            delta = event.globalPos() - self.drag_position
            new_geom = QRect(self.initial_geometry)
            
            if self.resize_zone == 0:
                # Dragging mode (move window)
                new_geom.translate(delta)
                self.setGeometry(new_geom)
                self.region.overlay_rect = self.geometry()
            else:
                # Resizing mode
                min_w = 100
                min_h = 45
                
                # Left
                if self.resize_zone & 1:
                    new_left = self.initial_geometry.left() + delta.x()
                    if new_left > self.initial_geometry.right() - min_w:
                        new_left = self.initial_geometry.right() - min_w
                    new_geom.setLeft(new_left)
                # Right
                elif self.resize_zone & 2:
                    new_right = self.initial_geometry.right() + delta.x()
                    if new_right < self.initial_geometry.left() + min_w:
                        new_right = self.initial_geometry.left() + min_w
                    new_geom.setRight(new_right)
                    
                # Top
                if self.resize_zone & 4:
                    new_top = self.initial_geometry.top() + delta.y()
                    if new_top > self.initial_geometry.bottom() - min_h:
                        new_top = self.initial_geometry.bottom() - min_h
                    new_geom.setTop(new_top)
                # Bottom
                elif self.resize_zone & 8:
                    new_bottom = self.initial_geometry.bottom() + delta.y()
                    if new_bottom < self.initial_geometry.top() + min_h:
                        new_bottom = self.initial_geometry.top() + min_h
                    new_geom.setBottom(new_bottom)
                    
                self.setGeometry(new_geom)
                self.region.overlay_rect = self.geometry()
            event.accept()
        else:
            # Change cursor shape on hover
            pos = event.pos()
            rect = self.rect()
            border = 8
            
            zone = 0
            if pos.x() < border:
                zone |= 1
            elif pos.x() > rect.width() - border:
                zone |= 2
                
            if pos.y() < border:
                zone |= 4
            elif pos.y() > rect.height() - border:
                zone |= 8
                
            if zone == 5 or zone == 10:  # TOP|LEFT or BOTTOM|RIGHT
                self.setCursor(Qt.SizeFDiagCursor)
            elif zone == 6 or zone == 9:  # TOP|RIGHT or BOTTOM|LEFT
                self.setCursor(Qt.SizeBDiagCursor)
            elif zone & 3:  # LEFT or RIGHT
                self.setCursor(Qt.SizeHorCursor)
            elif zone & 12:  # TOP or BOTTOM
                self.setCursor(Qt.SizeVerCursor)
            else:
                self.setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        self.drag_position = None
        self.resize_zone = 0


class OverlayWindow(QObject):
    def __init__(self):
        super().__init__()
        self.bubbles = []

    def update_translations(self, regions):
        # We want to map each new region to a previous bubble to reuse its geometry
        assigned_bubbles = set()
        new_geometries = {} # map of id(region) -> QRect
        
        # 1. Match by source_rect overlap
        for region in regions:
            best_bubble = None
            best_overlap = 0.0
            for bubble in self.bubbles:
                if bubble in assigned_bubbles or not bubble.isVisible():
                    continue
                intersection = region.source_rect.intersected(bubble.region.source_rect)
                if not intersection.isEmpty():
                    area_int = intersection.width() * intersection.height()
                    area_new = region.source_rect.width() * region.source_rect.height()
                    area_old = bubble.region.source_rect.width() * bubble.region.source_rect.height()
                    overlap = area_int / max(1, min(area_new, area_old))
                    if overlap > best_overlap and overlap > 0.3:
                        best_overlap = overlap
                        best_bubble = bubble
            if best_bubble:
                new_geometries[id(region)] = best_bubble.geometry()
                assigned_bubbles.add(best_bubble)
                
        # 2. Match remaining by distance
        for region in regions:
            if id(region) in new_geometries:
                continue
            best_bubble = None
            best_dist = 100000.0
            for bubble in self.bubbles:
                if bubble in assigned_bubbles or not bubble.isVisible():
                    continue
                dist = (region.source_rect.center() - bubble.region.source_rect.center()).manhattanLength()
                if dist < best_dist and dist < 150:
                    best_dist = dist
                    best_bubble = bubble
            if best_bubble:
                new_geometries[id(region)] = best_bubble.geometry()
                assigned_bubbles.add(best_bubble)

        self.hide_overlay()
        
        if not regions:
            return

        for region in regions:
            if id(region) in new_geometries:
                region.overlay_rect = new_geometries[id(region)]
            
            bubble = TextBubbleOverlay(region)
            bubble.show()
            self.bubbles.append(bubble)

    def hide_overlay(self):
        for bubble in self.bubbles:
            bubble.close()
        self.bubbles.clear()


class CropWindow(QWidget):
    crop_completed = pyqtSignal(QRect)

    def __init__(self):
        super().__init__()
        self.start_pos = None
        self.end_pos = None

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setCursor(Qt.CrossCursor)
        self.setGeometry(virtual_screen_geometry())
        self.setWindowOpacity(0.3)

    def paintEvent(self, event):
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
        self.crop_completed.emit(crop_rect.translated(self.geometry().topLeft()))
        self.close()


class Controller:
    def __init__(self, overlay_window, control_panel_window, google_service):
        self.overlay = overlay_window
        self.control_panel = control_panel_window
        self.google_service = google_service
        self.crop_window = None
        self.comm = Communicate()
        self.active_crop_rect = None
        self.last_capture_digest = None
        self.translation_in_progress = False
        self.pending_scan = False
        self.scene_history = deque(maxlen=SCENE_HISTORY_LIMIT)
        self.translation_cache = OrderedDict()
        self.continuous_timer = QTimer()
        self.continuous_timer.setInterval(CONTINUOUS_INTERVAL_MS)

        self.comm.job_ready.connect(self.handle_job_ready)
        self.comm.job_failed.connect(self.handle_job_failed)
        self.comm.job_finished.connect(self.handle_job_finished)
        self.comm.translation_ready.connect(self.overlay.update_translations)
        self.comm.translation_ready.connect(self.control_panel.add_page_to_log)
        self.comm.crop_requested.connect(self.trigger_cropping)
        self.comm.hide_requested.connect(self.overlay.hide_overlay)
        self.control_panel.continuous_checkbox.toggled.connect(
            self.set_continuous_mode
        )
        self.continuous_timer.timeout.connect(self.scan_continuous_area)

    def trigger_cropping(self):
        if self.crop_window and self.crop_window.isVisible():
            return
        self.crop_window = CropWindow()
        self.crop_window.crop_completed.connect(self.set_translation_area)
        self.crop_window.show()

    def request_crop_from_hotkey(self):
        self.comm.crop_requested.emit()

    def request_hide_from_hotkey(self):
        self.comm.hide_requested.emit()

    def set_continuous_mode(self, enabled):
        if enabled:
            self.continuous_timer.start()
            if self.active_crop_rect:
                self.control_panel.set_status("Continuous mode is watching the selected area")
            else:
                self.control_panel.set_status("Continuous mode needs a selected area")
        else:
            self.continuous_timer.stop()
            self.control_panel.set_status("Continuous mode stopped")

    def set_translation_area(self, crop_rect):
        logging.info("Area selected: %s", crop_rect)
        if crop_rect.width() < 5 or crop_rect.height() < 5:
            logging.info("Selected area is too small.")
            return

        self.active_crop_rect = QRect(crop_rect)
        self.last_capture_digest = None
        self._last_pixels = None
        if self.translation_in_progress:
            self.pending_scan = True
            self.control_panel.set_status("New area queued")
            return

        self.scan_active_area(force=True)

    def scan_continuous_area(self):
        self.scan_active_area()

    def scan_active_area(self, force=False):
        if not self.active_crop_rect or self.translation_in_progress:
            return

        try:
            crop_rect = QRect(self.active_crop_rect)
            image_bytes, capture_scale = capture_screen_region(crop_rect)
            
            # Perceptual similarity check using downscaled grayscale image
            qimg = QImage.fromData(image_bytes)
            small_img = qimg.scaled(16, 16, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            
            current_pixels = []
            for y in range(16):
                for x in range(16):
                    color = QColor(small_img.pixel(x, y))
                    gray = int(0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue())
                    current_pixels.append(gray)
            
            if not force and hasattr(self, "_last_pixels") and self._last_pixels:
                diff = sum(abs(a - b) for a, b in zip(current_pixels, self._last_pixels)) / 256.0
                if diff < 3.0:  # Threshold: less than ~1.2% average pixel change
                    return
            
            self._last_pixels = current_pixels
            self.start_translation_job(crop_rect, image_bytes, capture_scale)
        except Exception as exc:
            self.handle_job_failed(str(exc))

    def start_translation_job(self, crop_rect, image_bytes, capture_scale):
        self.translation_in_progress = True
        self.control_panel.set_status("Reading and translating...")

        worker = Thread(
            target=self.run_translation_job,
            args=(crop_rect, image_bytes, capture_scale),
            daemon=True,
        )
        worker.start()

    def run_translation_job(self, crop_rect, image_bytes, capture_scale):
        try:
            regions = self.google_service.get_text_regions(image_bytes, capture_scale)
            if not regions:
                logging.info("No text found in selected area.")
                self.comm.job_ready.emit([])
                return

            for region in regions:
                region.source_rect = region.source_rect.translated(crop_rect.topLeft())

            scene_key = tuple(region.original for region in regions)
            translations = self.get_cached_translations(scene_key)
            if translations is None:
                # Identify which texts actually need translation (contain Japanese)
                translated_map = {}
                to_translate = []
                for text in scene_key:
                    if contains_japanese(text):
                        to_translate.append(text)
                    else:
                        translated_map[text] = text  # Keep non-Japanese text as-is
                
                if to_translate:
                    engine_idx = self.control_panel.engine_combo.currentIndex()
                    target_lang_text = self.control_panel.lang_combo.currentText()
                    lang_info = LANG_MAP.get(target_lang_text, {"code": "th", "name": "Thai"})
                    target_code = lang_info["code"]
                    target_name = lang_info["name"]
                    
                    if engine_idx in (1, 2, 3, 4):
                        if engine_idx in (1, 2, 3):
                            api_key = self.control_panel.api_key_input.text().strip()
                            if not api_key:
                                raise ValueError("กรุณากรอก API Key ในช่องตั้งค่าการแปลภาษา")
                            
                            if engine_idx == 1:
                                gemini_results = self.translate_with_custom_api(to_translate, api_key, "qwen3.6-35b-a3b", target_name)
                            elif engine_idx == 2:
                                gemini_results = self.translate_with_custom_api(to_translate, api_key, "diffusiongemma-26b-a4b", target_name)
                            elif engine_idx == 3:
                                gemini_results = self.translate_with_gemini_api(to_translate, api_key, target_name)
                        elif engine_idx == 4:
                            gemini_results = self.translate_with_api_gateway(to_translate, service="aistudio")
                    else:
                        gemini_results = self.google_service.translate_batch(to_translate, target_code)
                    
                    for text, trans in zip(to_translate, gemini_results):
                        translated_map[text] = trans
                
                # Reconstruct translations list in original order
                translations = [translated_map[text] for text in scene_key]
                self.cache_translations(scene_key, translations)

            for region, translated_text in zip(regions, translations):
                region.translated = translated_text

            self.comm.job_ready.emit(regions)
        except Exception as exc:
            self.comm.job_failed.emit(str(exc))
        finally:
            self.comm.job_finished.emit()

    def translate_with_custom_api(self, texts, api_key, model_name="qwen3.6-35b-a3b", target_name="Thai"):
        import urllib.request
        import urllib.error
        
        prompt = (
            f"You are a professional game translator. Translate the following game text into natural, flowing {target_name}.\n"
            "The translation should match the style of a visual novel/light novel, choosing appropriate pronouns for characters.\n"
            "Maintain the context of the game. Translate the input list and return ONLY a JSON array of strings in the same order.\n"
            f"Input:\n{json.dumps(texts, ensure_ascii=False)}"
        )
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        url = "https://gateway.9arm.co/v1/chat/completions"
        
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}]
        }
        
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=15) as response:
                response_data = response.read().decode('utf-8')
                response_json = json.loads(response_data)
                content = response_json["choices"][0]["message"]["content"]
            
            # Find json array in the content (some models wrap it in markdown block)
            start_idx = content.find('[')
            end_idx = content.rfind(']') + 1
            if start_idx != -1 and end_idx != -1:
                content = content[start_idx:end_idx]
                
            translated_list = json.loads(content)
            if isinstance(translated_list, list) and len(translated_list) == len(texts):
                return [str(t) for t in translated_list]
            else:
                logging.error(f"API returned invalid structure: {content}")
        except Exception as e:
            logging.error(f"Failed to parse API response: {e}")

        # Fallback to single translate
        fallback_translations = []
        for text in texts:
            try:
                single_prompt = (
                    f"Translate the following text into natural {target_name} for a game:\n"
                    f"{text}"
                )
                single_payload = {
                    "model": model_name,
                    "messages": [{"role": "user", "content": single_prompt}]
                }
                req = urllib.request.Request(url, data=json.dumps(single_payload).encode('utf-8'), headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=10) as response:
                    single_response_data = response.read().decode('utf-8')
                    single_response_json = json.loads(single_response_data)
                    single_content = single_response_json["choices"][0]["message"]["content"]
                fallback_translations.append(single_content.strip())
            except Exception as e:
                logging.error(f"API fallback failed for '{text}': {e}")
                fallback_translations.append(text)
        return fallback_translations

    def translate_with_gemini_api(self, texts, api_key, target_name="Thai"):
        import urllib.request
        import urllib.error
        
        prompt = (
            f"You are a professional game translator. Translate the following game text into natural, flowing {target_name}.\n"
            "The translation should match the style of a visual novel/light novel, choosing appropriate pronouns for characters.\n"
            "Maintain the context of the game. Translate the input list and return ONLY a JSON array of strings in the same order.\n"
            f"Input:\n{json.dumps(texts, ensure_ascii=False)}"
        )
        
        headers = {
            "Content-Type": "application/json"
        }
        
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
        
        payload = {
            "contents": [{
                "parts": [{
                    "text": prompt
                }]
            }]
        }
        
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=15) as response:
                response_data = response.read().decode('utf-8')
                response_json = json.loads(response_data)
                content = response_json["candidates"][0]["content"]["parts"][0]["text"]
            
            # Find json array in the content (some models wrap it in markdown block)
            start_idx = content.find('[')
            end_idx = content.rfind(']') + 1
            if start_idx != -1 and end_idx != -1:
                content = content[start_idx:end_idx]
                
            translated_list = json.loads(content)
            if isinstance(translated_list, list) and len(translated_list) == len(texts):
                return [str(t) for t in translated_list]
            else:
                logging.error(f"Gemini API returned invalid structure: {content}")
        except Exception as e:
            logging.error(f"Failed to parse Gemini API response: {e}")

        # Fallback to single translate
        fallback_translations = []
        for text in texts:
            try:
                single_prompt = (
                    f"Translate the following text into natural {target_name} for a game:\n"
                    f"{text}"
                )
                single_payload = {
                    "contents": [{
                        "parts": [{
                            "text": single_prompt
                        }]
                    }]
                }
                req = urllib.request.Request(url, data=json.dumps(single_payload).encode('utf-8'), headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=10) as response:
                    single_response_data = response.read().decode('utf-8')
                    single_response_json = json.loads(single_response_data)
                    single_content = single_response_json["candidates"][0]["content"]["parts"][0]["text"]
                fallback_translations.append(single_content.strip())
            except Exception as e:
                logging.error(f"Gemini API fallback failed for '{text}': {e}")
                fallback_translations.append(text)
        return fallback_translations

    def translate_with_api_gateway(self, texts, service="aistudio"):
        import urllib.request
        import urllib.error
        
        url = "http://localhost:3000/api/v1/translations/"
        headers = {
            "Content-Type": "application/json"
        }
        
        results = []
        for text in texts:
            payload = {
                "service": service,
                "text": text
            }
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=15) as response:
                    response_data = response.read().decode('utf-8')
                    response_json = json.loads(response_data)
                    translated = response_json.get("data", {}).get("result", "")
                    if translated:
                        results.append(translated)
                    else:
                        logging.error(f"API Gateway returned no result for '{text}': {response_data}")
                        results.append(text)
            except Exception as e:
                logging.error(f"API Gateway failed for '{text}': {e}")
                results.append(text)
        return results

    def get_cached_translations(self, scene_key):
        translations = self.translation_cache.get(scene_key)
        if translations is not None:
            self.translation_cache.move_to_end(scene_key)
            logging.info("Translation cache hit.")
        return translations

    def cache_translations(self, scene_key, translations):
        self.translation_cache[scene_key] = tuple(translations)
        self.translation_cache.move_to_end(scene_key)
        while len(self.translation_cache) > TRANSLATION_CACHE_LIMIT:
            self.translation_cache.popitem(last=False)

    def handle_job_ready(self, regions):
        if regions:
            regions = place_overlay_regions(regions)
            scene_key = tuple(region.original for region in regions)
            if not self.scene_history or self.scene_history[-1] != scene_key:
                self.scene_history.append(scene_key)
            self.control_panel.set_status(
                f"Translated | scene history: {len(self.scene_history)}/{SCENE_HISTORY_LIMIT}"
            )
        else:
            self.control_panel.set_status("No text found")

        self.comm.translation_ready.emit(regions)

    def handle_job_failed(self, message):
        self.last_capture_digest = None
        self._last_pixels = None
        show_error("Translation failed", message)
        self.control_panel.set_status("Translation failed")

    def handle_job_finished(self):
        self.translation_in_progress = False
        if self.pending_scan:
            self.pending_scan = False
            self.scan_active_area(force=True)


def start_hotkey_listener(controller):
    def on_crop_activate():
        controller.request_crop_from_hotkey()

    def on_close_activate():
        controller.request_hide_from_hotkey()

    hotkeys = keyboard.GlobalHotKeys(
        {
            "<f9>": on_crop_activate,
            "<esc>": on_close_activate,
        }
    )
    hotkeys.run()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(qdarktheme.load_stylesheet("dark"))

    control_panel = ControlPanelWindow()
    overlay = OverlayWindow()
    google_service = GoogleCloudService()
    controller = Controller(overlay, control_panel, google_service)

    control_panel.crop_button.clicked.connect(controller.trigger_cropping)

    listener_thread = Thread(target=start_hotkey_listener, args=(controller,), daemon=True)
    listener_thread.start()

    logging.info("%s is ready.", APP_NAME)
    control_panel.show()
    sys.exit(app.exec_())
