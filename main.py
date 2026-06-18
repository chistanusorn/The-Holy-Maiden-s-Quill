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

import mss
import mss.tools
import qdarktheme
from pynput import keyboard
from PyQt5.QtCore import QObject, QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPen
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
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
    lines = []

    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue

        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if metrics.horizontalAdvance(candidate) <= max_width:
                current = candidate
                continue

            if current:
                lines.append(current)
                current = ""

            if metrics.horizontalAdvance(word) <= max_width:
                current = word
            else:
                broken = split_long_token(word, metrics, max_width)
                lines.extend(broken[:-1])
                current = broken[-1] if broken else ""

        if current:
            lines.append(current)

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
        super().__init__()
        self.setWindowTitle(APP_NAME)
        icon_path = resource_path("icon.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
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

        self.continuous_checkbox = QCheckBox("Continuous mode: watch the latest area")
        self.continuous_checkbox.setFont(QFont("Segoe UI", 10))
        self.layout.addWidget(self.continuous_checkbox)

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

    def add_page_to_log(self, regions):
        if not regions:
            return

        lines = ["แปลทั้งหน้า:"]
        for index, region in enumerate(regions, 1):
            lines.append(f"{index}. ต้นฉบับ: {region.original}")
            lines.append(f"   คำแปล: {region.translated}")
        lines.append("-" * 50)
        log_entry = "\n".join(lines)
        self.log_display.append(log_entry)

    def set_status(self, message):
        self.status_label.setText(message)


class OverlayWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.regions = []
        self.overlay_font = QFont("Segoe UI", 14, QFont.Bold)

        self.hide()

    def update_translations(self, regions):
        self.regions = list(regions)
        if not self.regions:
            self.hide()
            return

        self.setGeometry(virtual_screen_geometry())
        self.show()
        self.update()

    def hide_overlay(self):
        self.regions = []
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(self.overlay_font)
        painter.setPen(QColor(255, 255, 255))

        origin = self.geometry().topLeft()
        for region in self.regions:
            box = QRect(region.overlay_rect).translated(-origin)
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
                wrapped_text(region.translated, self.overlay_font, text_rect.width()),
            )


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
            capture_digest = sha256(image_bytes).digest()
            if not force and capture_digest == self.last_capture_digest:
                return

            self.last_capture_digest = capture_digest
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
                translations = self.google_service.translate_batch(list(scene_key))
                self.cache_translations(scene_key, translations)

            for region, translated_text in zip(regions, translations):
                region.translated = translated_text

            self.comm.job_ready.emit(regions)
        except Exception as exc:
            self.comm.job_failed.emit(str(exc))
        finally:
            self.comm.job_finished.emit()

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
