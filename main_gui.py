import sys
import io
import os
import re
import time
import random
import signal
import threading
from pathlib import Path
from collections import OrderedDict, deque

# --- RAM: пулы BLAS-потоков numpy аудио-конвейеру не нужны -------------
# Замер по модулям (scripts/ram_sources.py): реальный источник веса
# простоя — сам стек зависимостей (PySide6 + numpy + httpx + libsndfile),
# а при игре добавляется scipy (~+77 МБ, грузится движком лениво — как
# было в g3). Отдельно: OpenBLAS/MKL поднимают пул потоков по числу
# ядер и держат буферы под каждого — на многоядерных Windows-машинах
# это десятки МБ, которые никогда не используются (фильтры sosfilt и
# компрессор — блоковая обработка маленьких массивов, БЛАС-матмуль там
# нет). Лимит ставится ДО импорта numpy, на качество/скорость звука не
# влияет (замер: обработка 30x realtime и на одном потоке).
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import numpy as np
import sounddevice as sd
import soundfile as sf

from PySide6.QtCore import (QByteArray, Qt, QSize, QRectF, QRect, QPoint,
                            QPointF, QEvent, Signal, Slot, QThread, QTimer,
                            QModelIndex, QAbstractListModel)
from PySide6.QtGui import (QColor, QFont, QIcon, QLinearGradient, QPainter,
                           QPainterPath, QPixmap, QMouseEvent, QImage,
                           QPen, QPolygonF)
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView,
    QStyledItemDelegate, QStyle, QStyleOptionSlider, QButtonGroup,
    QPushButton, QScrollBar, QSlider, QSizePolicy, QVBoxLayout, QWidget,
    QStackedWidget, QFileDialog
)

try:
    from PySide6.QtSvg import QSvgRenderer
except Exception:  # pragma: no cover — запасной путь: текстовые глифы
    QSvgRenderer = None

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.database import Database
from core.vk_client import VKClient

COVER_ROLE = Qt.UserRole + 2
TITLE_ROLE = Qt.UserRole + 3
ARTIST_ROLE = Qt.UserRole + 4
PLAYING_ROLE = Qt.UserRole + 5
DL_ROLE = Qt.UserRole + 6      # прогресс скачивания: None | 0..1 | -1 (сбой)

RESIZE_MARGIN = 6  # ширина кромки для ресайза, общая для окна и титлбара



MIN_WINDOW_W = 780
MIN_WINDOW_H = 600


DB = Database()
VKC = VKClient()
# клиент один на все потоки, поэтому сетевые вызовы сериализуем
VK_LOCK = threading.Lock()


class LRUCache:
    def __init__(self, max_size=100):
        self._cache = OrderedDict()
        self._max_size = max_size

    def get(self, key):
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, key, value):
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)
        self._cache[key] = value

    def __contains__(self, key):
        return key in self._cache


class AudioEngine:
    def __init__(self):
        self._stream = None
        self._data = None
        self._samplerate = 44100
        self._position = 0.0
        self._playing = False
        self._finished = True
        self._volume = 1.0
        self._speed = 1.0
        self._lock = threading.Lock()
        self._device_index = None

        self._proc = None
        try:
            # 10-полосный параллельный EQ + мультибэнд-динамика
            # (Linkwitz-Riley-сплит, точная плоская сумма на unity)
            try:
                from core.effects import MultibandProcessor
            except Exception:
                from effects import MultibandProcessor
            self._proc = MultibandProcessor()
        except Exception as ex:
            print("Effects unavailable (EQ отключён):", ex)

    def _get_extra_settings(self):
        try:
            info = sd.query_devices(kind='output')
            api = sd.query_hostapis(info['hostapi'])['name']
            if "WASAPI" in api:
                return sd.WasapiSettings(exclusive=True)
        except Exception:
            pass
        return None

    @property
    def loaded(self):
        return self._data is not None

    @property
    def playing(self):
        return self._playing

    @property
    def finished(self):
        return self._finished

    def _open_stream(self) -> bool:
        sr = self._samplerate
        ch = self._data.shape[1]

        try:
            extra = self._get_extra_settings()
            # latency='low' + мелкий блок: смена Speed/громкости слышна
            # за ~25-50 мс, а не за сотни миллисекунд как с 'high'
            self._stream = sd.OutputStream(
                samplerate=sr, channels=ch, dtype='float32',
                blocksize=1024, latency='low',
                extra_settings=extra, callback=self._callback,
            )
            self._stream.start()
        except Exception as ex:
            print("Stream error:", ex)
            try:
                # эксклюзивный WASAPI не завёлся — пробуем shared
                self._stream = sd.OutputStream(
                    samplerate=sr, channels=ch, dtype='float32',
                    blocksize=1024, latency='low',
                    callback=self._callback,
                )
                self._stream.start()
            except Exception as ex2:
                print("Fallback error:", ex2)
                try:
                    # последний шанс: консервативный профиль
                    self._stream = sd.OutputStream(
                        samplerate=sr, channels=ch, dtype='float32',
                        blocksize=2048, latency='high',
                        callback=self._callback,
                    )
                    self._stream.start()
                except Exception as ex3:
                    print("Conservative fallback error:", ex3)
                    self._stream = None
                    return False

        try:
            self._device_index = sd.query_devices(kind='output')['index']
        except Exception:
            self._device_index = None
        return True

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def check_output_device(self):

        try:
            idx = sd.query_devices(kind='output')['index']
        except Exception:
            return

        if idx == self._device_index:
            return

        self._device_index = idx

        if self._data is None:
            return

        self._close_stream()
        self._open_stream()

    def load(self, data: bytes) -> bool:
        self.stop()
        try:
            audio, sr = sf.read(io.BytesIO(data), always_2d=True, dtype='float32')
            del data
        except Exception as ex:
            print("Decode error:", ex)
            return False

        with self._lock:
            self._data = np.ascontiguousarray(audio)
            self._samplerate = sr
            self._position = 0.0
            self._finished = False
            self._playing = False
            if self._proc is not None:
                try:


                    self._proc.prepare(sr, audio.shape[1])
                except Exception as ex:
                    print("Effects prepare failed:", ex)
                    self._proc = None

        if not self._open_stream():
            self.stop()
            return False
        return True

    def stop(self):
        self._close_stream()

        with self._lock:
            self._data = None
            self._playing = False
            self._finished = True
            self._position = 0.0

    def play(self):
        with self._lock:
            if self._data is not None:
                self._playing = True

    def pause(self):
        with self._lock:
            self._playing = False

    def set_rate(self, rate: float):
        with self._lock:
            self._speed = max(0.0, min(3.0, rate))

    def set_volume(self, volume: float):
        # шкала громкости 0..200%: 1.0 = без изменения (слайдер 100),
        # 2.0 = +6 дБ запаса. Громкость применяется после EQ/компрессора,
        # так что на 200% вместе с большим бустом возможно клиппирование —
        # это честное поведение «громкости больше максимума»
        with self._lock:
            self._volume = max(0.0, min(2.0, volume))

    def set_eq_band(self, band: int, gain: int):
        if self._proc is not None:
            try:
                self._proc.set_eq_band(band, gain)
            except Exception:
                pass

    def set_dynamics_mode(self, mode: str):

        if self._proc is not None:
            try:
                self._proc.set_dynamics_mode(mode)
            except Exception:
                pass

    def position_ms(self):
        with self._lock:
            if self._data is None:
                return 0, 0
            pos = self._position
            total = len(self._data)
            sr = self._samplerate
        return int(pos / sr * 1000), int(total / sr * 1000)

    def seek_ms(self, ms: int):
        with self._lock:
            if self._data is None:
                return
            frame = ms / 1000.0 * self._samplerate
            frame = max(0.0, min(frame, float(len(self._data) - 1)))
            self._position = frame
            self._finished = False

    def _callback(self, outdata, frames, time_info, status):

        with self._lock:
            data = self._data
            playing = self._playing
            volume = self._volume
            speed = self._speed
            position = self._position

        if not playing or data is None:
            outdata.fill(0)
            return

        # скорость 0.00 — «заморозка» трека: тишина без продвижения
        # позиции (повтор одного сэмпла давал бы постоянное DC-смещение
        # на выходе — щелчки и нагрузка на динамик)
        if speed <= 0.0:
            outdata.fill(0)
            return

        max_idx = len(data) - 2
        indices = position + np.arange(frames) * speed
        finished = False

        if indices[0] > max_idx:
            outdata.fill(0)
            finished = True
        else:
            valid_indices = indices[indices <= max_idx]

            if len(valid_indices) == 0:
                outdata.fill(0)
                finished = True
            else:
                idx_floor = valid_indices.astype(np.int32)
                alpha = (valid_indices - idx_floor)[:, np.newaxis]

                interpolated = (data[idx_floor] * (1.0 - alpha)
                                + data[idx_floor + 1] * alpha)

                out_len = len(interpolated)
                outdata[:out_len] = interpolated
                if out_len < frames:
                    outdata[out_len:].fill(0)
                    finished = True

                if self._proc is not None:




                    try:
                        outdata[:] = self._proc.process(outdata)
                    except Exception as ex:

                        print("Effects process error:", ex)
                        self._proc = None



                outdata *= volume

        with self._lock:
            # пока играли блок, позицию могли поменять снаружи (seek) — не трогаем
            if self._position == position:
                self._position = position + frames * speed
                if finished:
                    self._playing = False
                    self._finished = True


def _app_dpr() -> float:
    app = QApplication.instance()
    if app is not None:
        screen = app.primaryScreen()
        if screen is not None:
            return float(screen.devicePixelRatio())
    return 1.0


def make_cover(size, c1, c2, radius=None, text=None):
    dpr = _app_dpr()
    px = max(1, round(size * dpr))

    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    g = QLinearGradient(0, 0, px, px)
    g.setColorAt(0.0, QColor(c1))
    g.setColorAt(1.0, QColor(c2))

    p.setBrush(g)
    p.setPen(Qt.NoPen)

    r = radius if radius is not None else int(px * 0.18)
    p.drawRoundedRect(0, 0, px, px, r, r)

    if text:
        p.setPen(QColor("white"))
        f = QFont()
        f.setFamilies(["Segoe UI", "Tahoma"])
        f.setPixelSize(int(px * 0.5))
        f.setWeight(QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignCenter, text)

    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


# заглушки кэшируются по (размер, dpr) — нужен живой QApplication
_PH = {}


def placeholder_cover(size=120):
    key = (size, round(_app_dpr(), 2))
    pm = _PH.get(key)
    if pm is None:
        pm = make_cover(size, "#23272e", "#23272e",
                        radius=max(3.0, size * 0.083), text="♪")
        _PH[key] = pm
    return pm


def fit_cover(source, logical, radius=8):
    dpr = _app_dpr()
    target = max(1, round(logical * dpr))

    img = QImage()
    if isinstance(source, (bytes, bytearray)):
        img.loadFromData(source)
    elif isinstance(source, str) and source:
        img = QImage(source)

    if img.isNull():
        return placeholder_cover(logical)

    # центр-кроп в квадрат на ОРИГИНАЛЬНОМ разрешении
    w, h = img.width(), img.height()
    if w != h:
        side = min(w, h)
        img = img.copy((w - side) // 2, (h - side) // 2, side, side)
        w = h = side

    if w > target:
        # каскадное уменьшение вдвое — без алиасинга при большом факторе
        while img.width() >= 2 * target:
            half = max(target, img.width() // 2)
            img = img.scaled(half, half, Qt.IgnoreAspectRatio,
                             Qt.SmoothTransformation)
        if img.width() != target:
            img = img.scaled(target, target, Qt.IgnoreAspectRatio,
                             Qt.SmoothTransformation)

    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)

    if radius and radius > 0:
        r = radius * dpr
        out = QPixmap(pm.width(), pm.height())
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(0, 0, out.width(), out.height(), r, r)
        p.setClipPath(path)
        p.drawPixmap(0, 0, pm)
        p.end()
        out.setDevicePixelRatio(dpr)
        return out

    return pm


# --- вшитые SVG-иконки кнопок управления ---------------------------------
# Текстовые глифы «◀ ▶ ⇄» рисуются системным шрифтом мелко (12px) и
# по-разному на разных машинах. Собственная SVG-графика крупнее, всегда
# одинаково выглядит и остаётся чёткой на любом DPI. {COLOR} подставляется
# при рендере; если модуль QtSvg вдруг недоступен — кнопки остаются с
# прежними текстовыми глифами (фолбэк).

_SVG_PREV = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<path d="M3.6 3.2v9.6" fill="none" stroke="{COLOR}" stroke-width="1.7" '
    'stroke-linecap="round"/>'
    '<path d="M12.4 3.8v8.4L5.4 8z" fill="{COLOR}"/>'
    '</svg>'
)

_SVG_NEXT = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<path d="M12.4 3.2v9.6" fill="none" stroke="{COLOR}" stroke-width="1.7" '
    'stroke-linecap="round"/>'
    '<path d="M3.6 3.8v8.4L10.6 8z" fill="{COLOR}"/>'
    '</svg>'
)

_SVG_SHUFFLE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" '
    'stroke="{COLOR}" stroke-width="1.5" stroke-linecap="round" '
    'stroke-linejoin="round">'
    '<path d="M1.8 4.6h1.6c1.6 0 3 .9 3.8 2.3l1.6 2.2c.8 1.4 2.2 2.3 3.8 2.3h1.6"/>'
    '<path d="M1.8 11.4h1.6c1.6 0 3-.9 3.8-2.3l1.6-2.2c.8-1.4 2.2-2.3 3.8-2.3h1.6"/>'
    '<path d="M12.2 2.8l2 1.8-2 1.8"/>'
    '<path d="M12.2 9.6l2 1.8-2 1.8"/>'
    '</svg>'
)

_SVG_PLAY = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<path d="M5 3.3c0-.6.7-1 1.2-.6l7 4.7c.4.3.4 1 0 1.2l-7 4.7c-.5.4-1.2 0-1.2-.6z" '
    'fill="{COLOR}"/>'
    '</svg>'
)

_SVG_PAUSE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="{COLOR}">'
    '<rect x="4.2" y="3.2" width="2.7" height="9.6" rx="1.3"/>'
    '<rect x="9.1" y="3.2" width="2.7" height="9.6" rx="1.3"/>'
    '</svg>'
)



_SVG_MINIMIZE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<path d="M3.5 8h9" fill="none" stroke="{COLOR}" stroke-width="1.6" '
    'stroke-linecap="round"/>'
    '</svg>'
)

_SVG_MAXIMIZE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" '
    'stroke="{COLOR}" stroke-width="1.5">'
    '<rect x="4" y="4" width="8" height="8" rx="1.2"/>'
    '</svg>'
)

_SVG_RESTORE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" '
    'stroke="{COLOR}" stroke-width="1.5" stroke-linejoin="round">'
    '<path d="M6.2 3.5h5.3c.5 0 .9.4.9.9v5.3"/>'
    '<rect x="3.6" y="6.2" width="6.2" height="6.2" rx="1.2"/>'
    '</svg>'
)

_SVG_CLOSE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<path d="M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6" fill="none" '
    'stroke="{COLOR}" stroke-width="1.6" stroke-linecap="round"/>'
    '</svg>'
)


def svg_icon(svg: str, color: str, logical: int = 17):
    if QSvgRenderer is None:
        return None

    data = QByteArray(svg.replace("{COLOR}", color).encode("utf-8"))
    renderer = QSvgRenderer(data)
    if not renderer.isValid():
        return None

    dpr = _app_dpr()
    px = max(1, round(logical * dpr))
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return QIcon(pm)


def apply_button_icon(btn, svg: str, color: str, fallback_text: str,
                      size: int = 17):

    ic = svg_icon(svg, color, size)
    if ic is not None:
        btn.setIcon(ic)
        btn.setIconSize(QSize(size, size))
        btn.setText("")
    else:
        btn.setText(fallback_text)
    return ic


class TitleBtn(QPushButton):
    def __init__(self, svg, fallback_text, normal, hovered):
        super().__init__(fallback_text)
        self._fallback = fallback_text
        self.set_svg(svg, normal, hovered)

    def set_svg(self, svg, normal, hovered):

        self._normal = svg_icon(svg, normal, 15)
        self._hover = svg_icon(svg, hovered, 15)
        if self._normal is not None:
            self.setText("")
            self.setIcon(self._normal)
            self.setIconSize(QSize(15, 15))
        else:

            self.setIcon(QIcon())
            self.setText(self._fallback)

    def enterEvent(self, event):
        if self._hover is not None:
            self.setIcon(self._hover)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._normal is not None:
            self.setIcon(self._normal)
        super().leaveEvent(event)


class TitleBar(QWidget):
    def __init__(self):
        super().__init__()

        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("titleBar")
        self.setFixedHeight(40)
        self.setMouseTracking(True)

        self._drag_pos = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 6, 0)
        lay.setSpacing(8)

        icon = QLabel()
        icon.setFixedSize(22, 22)
        icon.setPixmap(make_cover(22, "#4c8dff", "#4c8dff", 6, "♪"))

        title = QLabel("VK Offline Player")
        title.setObjectName("titleText")

        lay.addWidget(icon)
        lay.addWidget(title)
        lay.addStretch(1)


        self.btn_min = TitleBtn(_SVG_MINIMIZE, "—", "#8b929e", "#dfe3ea")
        self.btn_max = TitleBtn(_SVG_MAXIMIZE, "□", "#8b929e", "#dfe3ea")
        self.btn_close = TitleBtn(_SVG_CLOSE, "✕", "#8b929e", "#ffffff")

        for b in (self.btn_min, self.btn_max):
            b.setObjectName("titleBtn")
            b.setFixedSize(34, 26)

        self.btn_close.setObjectName("closeBtn")
        self.btn_close.setFixedSize(34, 26)

        self.btn_min.clicked.connect(lambda: self.window().showMinimized())
        self.btn_max.clicked.connect(self._toggle_max)
        self.btn_close.clicked.connect(lambda: self.window().close())

        lay.addWidget(self.btn_min)
        lay.addWidget(self.btn_max)
        lay.addWidget(self.btn_close)

    def set_maximized(self, on: bool):

        self.btn_max.set_svg(_SVG_RESTORE if on else _SVG_MAXIMIZE,
                             "#8b929e", "#dfe3ea")

    def _toggle_max(self):
        w = self.window()
        if w.isMaximized():
            w.showNormal()
        else:
            w.showMaximized()

    def mousePressEvent(self, event):

        if event.button() == Qt.LeftButton and event.position().y() > RESIZE_MARGIN:
            self._drag_pos = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self._drag_pos is not None:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):



        if self._drag_pos is not None:
            self._drag_pos = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, e):

        if e.position().y() <= RESIZE_MARGIN:
            e.ignore()
            return
        self._toggle_max()


class TrackModel(QAbstractListModel):
    def __init__(self):
        super().__init__()

        self._rows = []
        self._index_by_id = {}
        # обложки хранятся в ФИЗИЧЕСКОМ разрешении блока (DPR-aware):
        # мелкие 24px для списка + большие 120px для «сейчас играет»
        self._covers = LRUCache(max_size=300)
        self._big_covers = LRUCache(max_size=8)
        self._pending = set()
        self._missing = set()
        self._playing_id = None                # трек, который сейчас играет
        self._downloads = {}                   # track_id -> прогресс 0..1 (или -1 — сбой)

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = list(rows)
        self._index_by_id = {r["track_id"]: i for i, r in enumerate(self._rows)}

        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return len(self._rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        r = self._rows[index.row()]

        if role == Qt.DisplayRole:
            return f"{r['artist']} - {r['title']}"

        if role == TITLE_ROLE:
            return r["title"]

        if role == ARTIST_ROLE:
            return r["artist"]

        if role == PLAYING_ROLE:
            return r["track_id"] == self._playing_id

        if role == DL_ROLE:
            return self._downloads.get(r["track_id"])

        if role == COVER_ROLE:
            return self._covers.get(r["track_id"])

        return None

    def set_playing(self, track_id):

        old = self._playing_id
        if old == track_id:
            return
        self._playing_id = track_id
        for tid in (old, track_id):
            if tid is None:
                continue
            i = self._index_by_id.get(tid)
            if i is not None:
                idx = self.index(i, 0)
                self.dataChanged.emit(idx, idx, [PLAYING_ROLE])

    def _touch(self, track_id, role):
        i = self._index_by_id.get(track_id)
        if i is not None:
            idx = self.index(i, 0)
            self.dataChanged.emit(idx, idx, [role])

    def set_download(self, track_id, frac):

        if frac is None:
            self.clear_download(track_id)
            return
        self._downloads[track_id] = float(frac)
        self._touch(track_id, DL_ROLE)

    def clear_download(self, track_id):

        if track_id in self._downloads:
            self._downloads.pop(track_id, None)
            self._touch(track_id, DL_ROLE)

    def row_at(self, row):
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def row_by_id(self, track_id):
        return self._index_by_id.get(track_id, -1)

    def big_cover_for(self, track_id):

        return self._big_covers.get(track_id)

    def needs_cover(self, track_id):
        return (track_id not in self._covers
                and track_id not in self._pending
                and track_id not in self._missing)

    def mark_pending(self, track_id):
        self._pending.add(track_id)

    def unmark_pending(self, track_id):
        self._pending.discard(track_id)

    def mark_missing(self, track_id):
        self._missing.add(track_id)
        self._pending.discard(track_id)

    def set_cover(self, track_id, pm_list, pm_big=None):

        self._covers.put(track_id, pm_list)
        if pm_big is not None:
            self._big_covers.put(track_id, pm_big)
        self._pending.discard(track_id)
        self._missing.discard(track_id)

        i = self._index_by_id.get(track_id)
        if i is not None:
            idx = self.index(i, 0)
            self.dataChanged.emit(idx, idx, [COVER_ROLE])


class TrackDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._ph = placeholder_cover(24)

    def sizeHint(self, option, index):
        return QSize(0, 50)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        rect = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        playing = bool(index.data(PLAYING_ROLE))
        dl = index.data(DL_ROLE)      # None | 0..1 | -1 (не скачался)



        if selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(76, 141, 255, 26))
            painter.drawRoundedRect(rect, 7, 7)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 12))
            painter.drawRoundedRect(rect, 7, 7)

        crect = QRect(rect.x() + 6, rect.y() + 11, 24, 24)

        # обложка уже в физическом разрешении блока (DPR-aware):
        # рисуем в логический размер — 1:1 физических пикселей, без ресемпла
        pm = index.data(COVER_ROLE)
        pm = pm if pm is not None else self._ph
        dpr = pm.devicePixelRatio() or 1.0
        lw = pm.width() / dpr
        lh = pm.height() / dpr
        painter.drawPixmap(
            QPoint(crect.x() + int((crect.width() - lw) / 2),
                   crect.y() + int((crect.height() - lh) / 2)),
            pm)

        title = index.data(TITLE_ROLE) or ""
        artist = index.data(ARTIST_ROLE) or ""


        glyph_w = 14 if playing else (40 if dl is not None else 0)
        trect = QRect(rect.x() + 38, rect.y(),
                      rect.width() - 38 - 6 - glyph_w, rect.height())


        f1 = painter.font()
        f1.setPixelSize(13)
        f1.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f1)
        painter.setPen(QColor("#6fa5ff") if playing else QColor("#d9dee6"))
        r1 = QRect(trect.x(), rect.y() + 5, trect.width(), 18)
        painter.drawText(
            r1, Qt.AlignVCenter | Qt.AlignLeft,
            painter.fontMetrics().elidedText(title, Qt.ElideRight, r1.width())
        )


        f2 = painter.font()
        f2.setPixelSize(13)
        f2.setWeight(QFont.Weight.Normal)
        painter.setFont(f2)
        painter.setPen(QColor("#868d99"))
        r2 = QRect(trect.x(), rect.y() + 26, trect.width(), 16)
        painter.drawText(
            r2, Qt.AlignVCenter | Qt.AlignLeft,
            painter.fontMetrics().elidedText(artist, Qt.ElideRight, r2.width())
        )


        if playing:
            gx = rect.right() - 12
            cy = rect.y() + rect.height() / 2
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#4c8dff"))
            for dx, h in ((-4, 6), (0, 12), (4, 8)):
                painter.drawRoundedRect(
                    QRectF(gx + dx - 1, cy - h / 2, 2, h), 1, 1)


        elif dl is not None:
            self._paint_download(painter, rect, dl)

        painter.restore()

    def _paint_download(self, painter, rect, frac):

        frac = float(frac)
        d = 13.0
        cx = rect.right() - 12.0
        cy = rect.y() + rect.height() / 2.0
        arc = QRectF(cx - d / 2, cy - d / 2, d, d)


        pen = QPen(QColor(255, 255, 255, 38))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawArc(arc, 0, 360 * 16)

        if frac < 0:

            pen = QPen(QColor("#e0685f"))
            pen.setWidthF(1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            k = 2.6
            painter.drawLine(QPointF(cx - k, cy - k), QPointF(cx + k, cy + k))
            painter.drawLine(QPointF(cx - k, cy + k), QPointF(cx + k, cy - k))
            return

        frac = max(0.0, min(1.0, frac))


        pen = QPen(QColor("#4c8dff"))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(arc, 90 * 16, -int(360 * frac * 16))


        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#6fa5ff"))
        painter.drawPolygon(QPolygonF([
            QPointF(cx - 2.4, cy - 2.3),
            QPointF(cx + 2.4, cy - 2.3),
            QPointF(cx, cy + 2.3),
        ]))


        f = painter.font()
        f.setPixelSize(9)
        f.setWeight(QFont.Weight.Normal)
        painter.setFont(f)
        painter.setPen(QColor("#868d99"))
        prect = QRectF(rect.x() + 4, rect.y(),
                       max(1.0, cx - d / 2 - 6 - (rect.x() + 4)), rect.height())
        painter.drawText(prect, Qt.AlignVCenter | Qt.AlignRight,
                         f"{int(frac * 100)}%")


class LibraryPanel(QFrame):
    need_covers = Signal(list)

    def __init__(self):
        super().__init__()

        self.setObjectName("panel")
        self.setMinimumWidth(0)

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)

        self.model = TrackModel()

        self.list = QListView()
        self.list.setObjectName("trackList")
        self.list.setModel(self.model)
        self.list.setItemDelegate(TrackDelegate(self.list))
        self.list.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setEditTriggers(QListView.EditTrigger.NoEditTriggers)


        self.list.setVerticalScrollBar(RoundedScrollBar(Qt.Vertical))

        v.addWidget(self.list, 1)

        self._last_scroll_check = 0.0
        self.list.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.list.verticalScrollBar().rangeChanged.connect(lambda *_: self._request_visible_covers())

    def showEvent(self, event):
        super().showEvent(event)

        QTimer.singleShot(0, self._request_visible_covers)

    def set_tracks(self, rows):
        self.model.set_rows(rows)

        QTimer.singleShot(0, self._request_visible_covers)

    def update_cover(self, track_id, pm_list, pm_big=None):
        self.model.set_cover(track_id, pm_list, pm_big)

    def _on_scroll(self):
        now = time.time()
        if now - self._last_scroll_check < 0.3:
            return
        self._last_scroll_check = now
        self._request_visible_covers()

    def _request_visible_covers(self):
        ids_to_load = []

        viewport = self.list.viewport()
        top_idx = self.list.indexAt(QPoint(0, 0))

        if not top_idx.isValid():
            return

        bottom_idx = self.list.indexAt(QPoint(0, viewport.height()))

        start_row = top_idx.row()
        end_row = bottom_idx.row() if bottom_idx.isValid() else self.model.rowCount() - 1

        start_row = max(0, start_row - 5)
        end_row = min(self.model.rowCount() - 1, end_row + 5)

        for row in range(start_row, end_row + 1):
            r = self.model.row_at(row)
            if r is None:
                continue
            tid = r["track_id"]
            if self.model.needs_cover(tid):
                ids_to_load.append(tid)
                self.model.mark_pending(tid)

        if ids_to_load:
            self.need_covers.emit(ids_to_load)


class ElidedLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._full_text = text
        self.setWordWrap(False)

    def setText(self, text):
        self._full_text = text or ""
        self._apply_elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self):
        fm = self.fontMetrics()
        shown = fm.elidedText(self._full_text, Qt.ElideRight,
                              max(1, self.width() - 2))
        if shown != self.text():
            super().setText(shown)


class NowPlayingPanel(QFrame):
    def __init__(self):
        super().__init__()

        self.setObjectName("panel")




        self.setMaximumWidth(380)

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(2)

        v.addStretch(1)

        self.cover = QLabel()
        self.cover.setFixedSize(120, 120)
        self.cover.setPixmap(placeholder_cover())



        self.title = ElidedLabel("—")
        self.title.setObjectName("npTitle")
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setMinimumHeight(22)

        self.artist = ElidedLabel("—")
        self.artist.setObjectName("npArtist")
        self.artist.setAlignment(Qt.AlignCenter)
        self.artist.setMinimumHeight(17)

        self.export_btn = QPushButton("Export Track")
        self.export_btn.setObjectName("ghostBtn")
        self.export_btn.setFixedHeight(30)
        self.export_btn.setCursor(Qt.PointingHandCursor)

        v.addWidget(self.cover, 0, Qt.AlignHCenter)
        v.addSpacing(6)
        v.addWidget(self.title)
        v.addWidget(self.artist)
        v.addSpacing(8)
        v.addWidget(self.export_btn)
        v.addStretch(1)

    def update_track(self, row, cover=None):
        if row is None:
            self.title.setText("—")
            self.artist.setText("—")
            self.cover.setPixmap(placeholder_cover())
            return

        self.title.setText(row["title"])
        self.artist.setText(row["artist"])
        self.cover.setPixmap(cover if cover is not None else placeholder_cover())


class EqualizerPanel(QFrame):
    FREQS = ["32", "64", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]

    def __init__(self):
        super().__init__()

        self.setObjectName("panel")

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)

        head = QLabel("Equalizer")
        head.setObjectName("panelTitle")
        v.addWidget(head)

        self.sliders: list[RoundedSlider] = []

        row = QHBoxLayout()
        row.setSpacing(0)

        for f in self.FREQS:
            col = QVBoxLayout()
            col.setSpacing(4)

            s = RoundedSlider(Qt.Vertical)
            # полная шкала полос: ±20 дБ (было ±12)
            s.setRange(-20, 20)
            s.setValue(0)
            s.setFixedHeight(110)
            s.setFocusPolicy(Qt.NoFocus)

            lab = QLabel(f)
            lab.setObjectName("eqLabel")
            lab.setAlignment(Qt.AlignCenter)

            col.addWidget(s, 1, Qt.AlignHCenter)
            col.addWidget(lab)

            row.addLayout(col, 1)
            self.sliders.append(s)

        v.addLayout(row, 1)


class DynamicsPanel(QFrame):
    DYN_MODES = (("multiband", "Multiband"), ("output", "Output"), ("off", "Off"))

    sig_dynamics_mode = Signal(str)

    def __init__(self):
        super().__init__()

        self.setObjectName("panel")

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 12)
        v.setSpacing(8)

        t = QLabel("Dynamic Compressor")
        t.setObjectName("panelTitle")
        v.addWidget(t)

        v.addStretch(1)

        row = QHBoxLayout()
        row.setSpacing(4)

        self.dyn_buttons: dict[str, QPushButton] = {}

        self._dyn_group = QButtonGroup(self)
        self._dyn_group.setExclusive(True)

        for mode, label in self.DYN_MODES:
            b = QPushButton(label)
            b.setObjectName("segBtn")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setChecked(mode == "multiband")
            b.clicked.connect(lambda _=False, m=mode: self.sig_dynamics_mode.emit(m))

            self._dyn_group.addButton(b)


            row.addWidget(b, 1)
            self.dyn_buttons[mode] = b

        v.addLayout(row)

        v.addStretch(1)

    def set_mode(self, mode: str):

        b = self.dyn_buttons.get(mode)
        if b is not None and not b.isChecked():
            b.setChecked(True)


class SpeedPanel(QFrame):
    def __init__(self):
        super().__init__()

        self.setObjectName("panel")

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 12)
        v.setSpacing(8)

        t = QLabel("Speed")
        t.setObjectName("panelTitle")
        v.addWidget(t)

        row = QHBoxLayout()
        row.setSpacing(8)

        self.lbl = QLabel("1.00x")
        self.lbl.setObjectName("muted")

        self.slider = ClickSlider(Qt.Horizontal)
        # полная шкала скоростей: 0.00x..3.00x с шагом 0.01
        self.slider.setRange(0, 300)
        self.slider.setValue(100)
        self.slider.setMinimumHeight(28)

        row.addWidget(self.lbl)
        row.addWidget(self.slider, 1)

        v.addStretch(1)
        v.addLayout(row)
        v.addStretch(1)

    def set_label(self, value_100: int):
        self.lbl.setText(f"{value_100 / 100:.2f}x")


class RoundedSlider(QSlider):
    GROOVE = 5.0
    KNOB = 14.0

    _GROOVE_BG = QColor(255, 255, 255, 26)
    _FILL = QColor("#4c8dff")
    _KNOB = QColor("#dfe3ea")
    _KNOB_HOVER = QColor("#ffffff")
    _KNOB_DIM = QColor("#4a515c")

    def paintEvent(self, event):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderHandle, self)

        horizontal = self.orientation() == Qt.Horizontal
        half = self.GROOVE / 2.0


        if horizontal:
            cy = groove.center().y()
            gr = QRectF(groove.x(), cy - half, groove.width(), self.GROOVE)
        else:
            cx = groove.center().x()
            gr = QRectF(cx - half, groove.y(), self.GROOVE, groove.height())

        path = QPainterPath()
        path.addRoundedRect(gr, half, half)

        p.setPen(Qt.NoPen)
        p.setBrush(self._GROOVE_BG)
        p.drawPath(path)


        if horizontal:
            fill = min(max(0.0, handle.center().x() - gr.x()), gr.width())
            fr = QRectF(gr.x(), gr.y(), fill, gr.height())
        else:
            fill = min(max(0.0, gr.bottom() - handle.center().y()), gr.height())
            fr = QRectF(gr.x(), gr.bottom() - fill, gr.width(), fill)

        if fr.width() > 0.01 and fr.height() > 0.01:
            fpath = QPainterPath()
            fpath.addRoundedRect(fr, half, half)
            p.save()
            p.setClipPath(path)
            p.setBrush(self._FILL)
            p.drawPath(fpath)
            p.restore()


        disabled = not (opt.state & QStyle.StateFlag.State_Enabled)
        hover = opt.state & QStyle.StateFlag.State_MouseOver
        color = (self._KNOB_DIM if disabled
                 else self._KNOB_HOVER if hover else self._KNOB)
        r = self.KNOB / 2.0 - 0.5
        p.setBrush(color)
        p.drawEllipse(QPointF(handle.center()), r, r)

        p.end()


class ClickSlider(RoundedSlider):
    def _style_option(self):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return opt

    def _value_from_x(self, x):

        opt = self._style_option()

        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderHandle, self)

        span = groove.width() - handle.width()
        if span <= 0:
            return self.value()

        pos = x - groove.x() - handle.width() / 2.0
        pos = max(0.0, min(pos, float(span)))

        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(),
            int(round(pos)), span, opt.upsideDown)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # 1) прыжок ползунка в точку клика
            self.setSliderPosition(self._value_from_x(event.position().x()))

            # 2) синтетическое нажатие ровно по новому центру ползунка:
            #    QSlider считает, что его схватили, и дальше работает
            #    обычный drag со всеми сигналами Pressed/Moved/Released
            opt = self._style_option()
            handle = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider, opt,
                QStyle.SubControl.SC_SliderHandle, self)

            synth = QMouseEvent(
                event.type(),
                QPointF(handle.center()),
                QPointF(handle.center()),
                event.globalPosition(),
                event.button(), event.buttons(), event.modifiers(),
            )
            super().mousePressEvent(synth)

            # 3) визуальное значение применяется сразу (механика клика
            #    не изменилась); перемотка ЗВУКА — один раз на отпускание
            #    (см. _wire_events), чтобы не было двойного звука
            event.accept()
            return

        super().mousePressEvent(event)


class RoundedScrollBar(QScrollBar):
    _HANDLE = QColor(255, 255, 255, 36)
    _HANDLE_HOVER = QColor(255, 255, 255, 56)

    def __init__(self, orientation=Qt.Vertical, parent=None):
        super().__init__(orientation, parent)
        # hover-состояние в QStyleOption — без tracking не приходит
        self.setMouseTracking(True)

    def paintEvent(self, event):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        slider = self.style().subControlRect(
            QStyle.ComplexControl.CC_ScrollBar, opt,
            QStyle.SubControl.SC_ScrollBarSlider, self)

        if slider.width() > 0 and slider.height() > 0:
            r = min(slider.width(), slider.height()) / 2.0
            path = QPainterPath()
            path.addRoundedRect(QRectF(slider), r, r)

            hover = bool(opt.state & QStyle.StateFlag.State_MouseOver)
            p.setPen(Qt.NoPen)
            p.setBrush(self._HANDLE_HOVER if hover else self._HANDLE)
            p.drawPath(path)

        p.end()


class PlayerBar(QWidget):
    def __init__(self):
        super().__init__()

        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("playerBar")

        h = QHBoxLayout(self)
        h.setContentsMargins(12, 8, 12, 8)
        h.setSpacing(8)

        self.prev = QPushButton("◀")
        self.shuffle = QPushButton("⇄")
        self.shuffle.setCheckable(True)
        self.play = QPushButton("Play")
        self.next = QPushButton("▶")

        for b in (self.prev, self.shuffle, self.next):
            b.setObjectName("roundBtn")
            b.setFixedSize(34, 34)

        self.play.setObjectName("playBtn")
        self.play.setFixedHeight(34)


        apply_button_icon(self.prev, _SVG_PREV, "#c6ccd5", "◀")
        apply_button_icon(self.next, _SVG_NEXT, "#c6ccd5", "▶")


        self._ic_shuffle = svg_icon(_SVG_SHUFFLE, "#c6ccd5", 17)
        self._ic_shuffle_on = svg_icon(_SVG_SHUFFLE, "#dbe7ff", 17)
        if self._ic_shuffle is not None:
            self.shuffle.setIcon(self._ic_shuffle)
            self.shuffle.setIconSize(QSize(17, 17))
            self.shuffle.setText("")
            self.shuffle.toggled.connect(self._on_shuffle_toggled)



        self._ic_play = svg_icon(_SVG_PLAY, "#10151b", 17)
        self._ic_pause = svg_icon(_SVG_PAUSE, "#10151b", 17)
        if self._ic_play is not None and self._ic_pause is not None:
            self.play.setIcon(self._ic_play)
            self.play.setIconSize(QSize(17, 17))
            self.play.setText("")
            self.play.setFixedWidth(40)
        else:

            self.play.setFixedWidth(78)

        self.progress = ClickSlider(Qt.Horizontal)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setMinimumHeight(28)

        vol_lbl = QLabel("Vol")
        vol_lbl.setObjectName("muted")

        self.volume = ClickSlider(Qt.Horizontal)
        # громкость 0..200%: 100 = единичная громкость (прежние «100»),
        # 200 = +6 дБ запас. Значение сохраняется как раньше — старые
        # базы с 0..100 читаются без изменений
        self.volume.setRange(0, 200)
        self.volume.setFixedWidth(150)
        self.volume.setMinimumHeight(28)
        self.volume.setValue(100)

        h.addWidget(self.prev)
        h.addWidget(self.shuffle)
        h.addWidget(self.play)
        h.addWidget(self.next)
        h.addSpacing(6)
        h.addWidget(self.progress, 1)
        h.addSpacing(6)
        h.addWidget(vol_lbl)
        h.addWidget(self.volume)

    def set_play_icon(self, playing: bool):

        if self._ic_play is not None and self._ic_pause is not None:
            self.play.setIcon(self._ic_pause if playing else self._ic_play)
        else:
            self.play.setText("Pause" if playing else "Play")

    def _on_shuffle_toggled(self, checked: bool):
        if self._ic_shuffle is not None and self._ic_shuffle_on is not None:
            self.shuffle.setIcon(self._ic_shuffle_on if checked
                                 else self._ic_shuffle)


class AuthOverlay(QFrame):
    # (тип входа, токен, логин, пароль, код 2FA, ответ на капчу)
    submitted = Signal(int, str, str, str, str, str)
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self.hide()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        panel = QFrame(self)
        panel.setObjectName("panel")
        panel.setFixedWidth(320)

        v = QVBoxLayout(panel)
        v.setContentsMargins(14, 12, 14, 14)
        v.setSpacing(10)

        header = QHBoxLayout()

        title = QLabel("Авторизация")
        title.setObjectName("panelTitle")

        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("closeBtn")
        self.close_btn.setFixedSize(24, 24)

        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.close_btn)

        tabs = QFrame()
        tabs.setObjectName("authTabs")

        th = QHBoxLayout(tabs)
        th.setContentsMargins(2, 2, 2, 2)
        th.setSpacing(2)

        self.tab_token = QPushButton("Token")
        self.tab_login = QPushButton("Login")

        for btn in (self.tab_token, self.tab_login):
            btn.setObjectName("authTab")
            btn.setFixedHeight(28)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            btn.setProperty("active", False)

        th.addWidget(self.tab_token)
        th.addWidget(self.tab_login)

        self.pages = QStackedWidget()

        token_page = QWidget()
        tl = QVBoxLayout(token_page)
        tl.setContentsMargins(0, 0, 0, 0)

        self.token_edit = QLineEdit()
        self.token_edit.setObjectName("search")
        self.token_edit.setPlaceholderText("Токен или ссылка с токеном")
        self.token_edit.setFixedHeight(32)

        tl.addWidget(self.token_edit)

        login_page = QWidget()
        ll = QVBoxLayout(login_page)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)

        self.login_edit = QLineEdit()
        self.login_edit.setObjectName("search")
        self.login_edit.setPlaceholderText("Login")
        self.login_edit.setFixedHeight(32)

        self.password_edit = QLineEdit()
        self.password_edit.setObjectName("search")
        self.password_edit.setPlaceholderText("Password")
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setFixedHeight(32)

        # код двухфакторки: при включённой 2FA VK отвечает need_validation
        # («sms sent, use code param») — вводим код сюда и жмём
        # «Подключить» снова, запрос уйдёт с параметром code
        self.twofa_edit = QLineEdit()
        self.twofa_edit.setObjectName("search")
        self.twofa_edit.setPlaceholderText("Код 2FA (если включена)")
        self.twofa_edit.setFixedHeight(32)

        # капча (редкий случай): картинка + поле ответа, до надобности скрыты
        self.captcha_img = QLabel()
        self.captcha_img.setAlignment(Qt.AlignHCenter)
        self.captcha_img.setVisible(False)

        self.captcha_edit = QLineEdit()
        self.captcha_edit.setObjectName("search")
        self.captcha_edit.setPlaceholderText("Код с картинки")
        self.captcha_edit.setFixedHeight(32)
        self.captcha_edit.setVisible(False)

        ll.addWidget(self.login_edit)
        ll.addWidget(self.password_edit)
        ll.addWidget(self.twofa_edit)
        ll.addWidget(self.captcha_img)
        ll.addWidget(self.captcha_edit)

        self.pages.addWidget(token_page)
        self.pages.addWidget(login_page)

        self.connect_btn = QPushButton("Подключить")
        self.connect_btn.setObjectName("primaryBtn")
        self.connect_btn.setFixedHeight(32)

        # статус входа: куда VK отправил код 2FA, текст ошибки VK
        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("muted")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setVisible(False)

        v.addLayout(header)
        v.addWidget(tabs)
        v.addWidget(self.pages)
        v.addWidget(self.status_lbl)
        v.addWidget(self.connect_btn)

        outer.addWidget(panel, 0, Qt.AlignCenter)

        self.close_btn.clicked.connect(self._on_close)
        self.tab_token.clicked.connect(lambda: self._set_page(0))
        self.tab_login.clicked.connect(lambda: self._set_page(1))
        self.connect_btn.clicked.connect(self._on_submit)

        self._set_page(0)

    def _on_close(self):
        self.hide()
        self.closed.emit()

    def _set_page(self, index: int):
        self.pages.setCurrentIndex(index)

        self.tab_token.setProperty("active", index == 0)
        self.tab_login.setProperty("active", index == 1)

        for btn in (self.tab_token, self.tab_login):
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _on_submit(self):
        auth_type = self.pages.currentIndex()

        self.submitted.emit(
            auth_type,
            self.token_edit.text().strip(),
            self.login_edit.text().strip(),
            self.password_edit.text().strip(),
            self.twofa_edit.text().strip(),
            self.captcha_edit.text().strip(),
        )

    def set_loading(self, loading: bool):
        self.connect_btn.setEnabled(not loading)
        self.connect_btn.setText("Подключение..." if loading else "Подключить")

        self.token_edit.setEnabled(not loading)
        self.login_edit.setEnabled(not loading)
        self.password_edit.setEnabled(not loading)
        self.twofa_edit.setEnabled(not loading)
        self.captcha_edit.setEnabled(not loading)

        self.tab_token.setEnabled(not loading)
        self.tab_login.setEnabled(not loading)

    def show_auth(self, auth_type=0, token="", login="", password="", status=""):
        self.set_loading(False)

        self._set_page(1 if int(auth_type or 0) == 1 else 0)

        self.token_edit.setText(token or "")
        self.login_edit.setText(login or "")
        self.password_edit.setText(password or "")


        self.twofa_edit.clear()
        self.captcha_edit.clear()
        self.captcha_img.setVisible(False)
        self.captcha_edit.setVisible(False)
        self._show_status(status or "")

        self.show()
        self.raise_()

    def show_2fa(self, phone_mask: str):

        self.set_loading(False)
        self._set_page(1)

        where = f" на {phone_mask}" if phone_mask else ""
        self._show_status(f"VK отправил код подтверждения{where} — "
                          f"введите его и нажмите «Подключить»")

        self.show()
        self.raise_()
        self.twofa_edit.setFocus()

    def show_captcha(self, image_bytes):

        self.set_loading(False)
        self._set_page(1)

        if image_bytes:
            pm = QPixmap()
            if pm.loadFromData(bytes(image_bytes)):
                self.captcha_img.setPixmap(
                    pm.scaledToHeight(
                        50, Qt.TransformationMode.SmoothTransformation))
        self.captcha_img.setVisible(True)
        self.captcha_edit.setVisible(True)
        self._show_status("VK просит подтвердить, что вы не робот — "
                          f"введите код с картинки")

        self.show()
        self.raise_()
        self.captcha_edit.setFocus()

    def _show_status(self, text: str):
        self.status_lbl.setText(text)
        self.status_lbl.setVisible(bool(text))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(8, 10, 14, 205))
        p.end()


class LoadWorker(QThread):
    ok = Signal(list)
    auth = Signal()
    failed = Signal()
    need_2fa = Signal(str)         # маска телефона (куда ушёл код)
    need_captcha = Signal(object, str)  # bytes картинки, captcha_sid

    def __init__(self, auth_type: int, token: str, login: str, password: str,
                 refresh_token: str = "", app_id: str = "", device_id: str = "",
                 twofa_code: str = "", captcha_sid: str = "", captcha_key: str = ""):
        super().__init__()

        self.auth_type = auth_type
        self.token = token
        self.login = login
        self.password = password
        self.refresh_token = refresh_token
        self.app_id = app_id
        self.device_id = device_id
        # дозапросы прямой авторизации: код 2FA и ответы на капчу
        self.twofa_code = twofa_code
        self.captcha_sid = captcha_sid
        self.captcha_key = captcha_key

    def run(self):
        try:
            new_refresh = None

            try:
                with VK_LOCK:
                    result = VKC.authenticate(
                        auth_type=self.auth_type,
                        access_token=self.token,
                        login=self.login,
                        password=self.password,
                        refresh_token=self.refresh_token,
                        app_id=self.app_id or None,
                        device_id=self.device_id or None,
                        twofa_code=self.twofa_code,
                        captcha_sid=self.captcha_sid,
                        captcha_key=self.captcha_key,
                    )




                    if result is False and self.auth_type == 1:
                        if VKC.auth_need == "2fa":
                            self.need_2fa.emit(VKC.auth_hint or "")
                            return
                        if VKC.auth_need == "captcha":
                            sid = VKC.auth_captcha_sid
                            img = None
                            if VKC.auth_captcha_img:
                                try:
                                    img = VKC.get_audio_image(VKC.auth_captcha_img)
                                except Exception as ex:
                                    print(ex)
                            self.need_captcha.emit(img, sid)
                            return

                    # токен не приняли (код 5 = невалиден/истёк сутки) —
                    # прежде чем просить пользователя логиниться заново,
                    # пробуем обменять refresh-креденциал на новую пару
                    # токенов: сначала refresh_token, а без него — сам
                    # протухший access_token («по основному токену»,
                    # VK ID принимает его для сессий моста id.vk.ru/auth)
                    if (result is False and self.auth_type == 0
                            and VKC.auth_error == 5
                            and (VKC.refresh_token or VKC.access_token)):
                        if VKC.refresh_access_token():
                            new_refresh = VKC.refresh_token

                            result = VKC.auth_from_token()
            except Exception as ex:
                print(ex)
                self.auth.emit()
                return

            if result is False:
                print("Авторизация не успешна")
                self.auth.emit()
                return

            try:
                DB.set_settings("auth_type", VKC.auth_type)
                DB.set_settings("access_token", VKC.access_token or "")

                if self.auth_type == 1:
                    DB.set_settings("login", self.login or "")
                    DB.set_settings("password", self.password or "")

                # токены от refresh — сохраняем немедленно: обмен
                # одноразовый, старый refresh_token уже невалиден
                if new_refresh:
                    DB.set_settings("refresh_token", new_refresh or "")
                    if VKC.device_id:
                        DB.set_settings("device_id", VKC.device_id)
                    print("[auth] токен обновлён и сохранён")
            except Exception as ex:
                print(ex)

            try:
                local_audio = DB.get_audio()

                with VK_LOCK:
                    if not local_audio:
                        cloud_audio = VKC.get_audio()
                    else:
                        cloud_audio = VKC.get_audio(count=100, rexit=True)

                DB.save_audio(cloud_audio)

                rows = DB.get_audio(2147483647)

                # Свежие ссылки из ответа API подставляем в строки списка
                # (только в память, база не трогается — INSERT OR IGNORE как
                # было). Причина: INSERT OR IGNORE не обновляет url уже
                # существующих треков, а VK со временем ротирует подписи
                # ссылок — протухшая ссылка из базы кончается 403, ffmpeg
                # возвращает пустые данные и трек «не скачивается», хотя
                # в API лежит свежая. Трека нет в ответе API — остаётся его
                # url из базы, как раньше.
                if cloud_audio:
                    fresh = {r["track_id"]: r for r in cloud_audio}
                    merged = []
                    for row in rows:
                        row = {k: row[k] for k in row.keys()}
                        fr = fresh.get(row["track_id"])
                        if fr is not None:
                            if fr["audio_url"]:
                                row["audio_url"] = fr["audio_url"]
                            if fr["image_url"]:
                                row["image_url"] = fr["image_url"]
                        merged.append(row)
                    rows = merged

                self.ok.emit(rows)

            except Exception as ex:
                print(ex)
                self.failed.emit()

        except Exception as ex:
            print(ex)
            self.failed.emit()


class CoverLoader(QThread):
    loaded = Signal(int, object)   # track_id, bytes
    missing = Signal(int)          # track_id, обложки в БД нет

    def __init__(self):
        super().__init__()
        self._queue = deque()
        self._cv = threading.Condition()
        self._stopped = False

    def enqueue(self, ids):
        with self._cv:
            self._queue.extend(ids)
            self._cv.notify_all()

    def stop(self):
        with self._cv:
            self._stopped = True
            self._cv.notify_all()

    def run(self):
        while True:
            with self._cv:
                while not self._queue and not self._stopped:
                    self._cv.wait()
                if self._stopped:
                    return
                tid = self._queue.popleft()

            try:
                row = DB.get_audio_by_id(tid)
                blob = row["image_blob"] if row else None

                if blob is not None:
                    self.loaded.emit(tid, blob)
                else:
                    self.missing.emit(tid)
            except Exception as ex:
                print(ex)
                self.missing.emit(tid)


def parse_token_input(text: str) -> dict:
    text = (text or "").strip()
    out = {"access_token": "", "refresh_token": "",
           "app_id": "", "device_id": ""}

    def grab(key):
        m = re.search(rf"(?:^|[?#&\s]){key}=([^&\s#]+)", text)
        return m.group(1) if m else ""

    at = grab("access_token")
    out["refresh_token"] = grab("refresh_token")
    out["app_id"] = grab("app_id") or grab("client_id")
    out["device_id"] = grab("device_id")

    # вставили ссылку — берём токен из неё; просто токен — как есть
    out["access_token"] = at or text
    return out


def fetch_audio(track_id: int, audio_url: str) -> bytes:
    row = DB.get_audio_by_id(track_id)
    data = row["audio_blob"] if row else None

    if not data:
        with VK_LOCK:
            data = VKC.get_audio_data(audio_url)

        if not data:
            raise Exception("Пустые аудио данные")

        DB.save_audio_data(track_id, data)

        # контроль записи: скачанное обязано лежать в базе. Это лечит
        # «кольцо дошло до конца, а трек не играет / в базе NULL»:
        # запись проверяется чтением, если не встала — сохраняем ещё раз,
        # не вышло и второй раз — громкий raise с диагнозом вместо
        # молчаливого NULL (пайплайн скачивания при этом не трогается)
        chk = DB.get_audio_by_id(track_id)
        if (chk is None or chk["audio_blob"] is None
                or len(chk["audio_blob"]) != len(data)):
            print(f"[fetch_audio] аудио track_id={track_id} не записалось в базу — повторное сохранение")
            DB.save_audio_data(track_id, data)
            chk = DB.get_audio_by_id(track_id)
            if (chk is None or chk["audio_blob"] is None
                    or len(chk["audio_blob"]) != len(data)):
                raise Exception(f"Аудио не сохранилось в базу (track_id={track_id})")

    return data


class CoverWorker(QThread):
    ok = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, track_id: int, image_url: str):
        super().__init__()

        self.track_id = track_id
        self.image_url = image_url

    def run(self):
        try:
            row = DB.get_audio_by_id(self.track_id)
            blob = row["image_blob"] if row else None

            if blob is None and self.image_url:
                with VK_LOCK:
                    data = VKC.get_audio_image(self.image_url)

                if data:
                    DB.save_audio_image(self.track_id, data)
                    blob = data

            self.ok.emit(self.track_id, blob)

        except Exception as ex:
            print(ex)
            self.failed.emit(self.track_id, str(ex))


class AudioWorker(QThread):
    ok = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, track_id: int, audio_url: str):
        super().__init__()

        self.track_id = track_id
        self.audio_url = audio_url

    def run(self):
        try:
            self.ok.emit(self.track_id, fetch_audio(self.track_id, self.audio_url))
        except Exception as ex:
            print(ex)
            self.failed.emit(self.track_id, str(ex))


class ExportWorker(QThread):
    done = Signal(str, bool)

    def __init__(self, track_id: int, audio_url: str, path: str):
        super().__init__()

        self.track_id = track_id
        self.audio_url = audio_url
        self.path = path

    def run(self):
        ok = False
        try:
            data = fetch_audio(self.track_id, self.audio_url)
            with open(self.path, "wb") as f:
                f.write(data)
            ok = True
        except Exception as ex:
            print(ex)
        self.done.emit(self.path, ok)


class PlayerWindow(QWidget):
    # команды от системных медиа-кнопок (SMTC): приходят в RPC-потоке,
    # через сигнал доставляются в GUI-поток (queued connection)
    sig_media_action = Signal(str)

    _EDGE_CURSORS = {
        Qt.LeftEdge: Qt.SizeHorCursor,
        Qt.RightEdge: Qt.SizeHorCursor,
        Qt.TopEdge: Qt.SizeVerCursor,
        Qt.BottomEdge: Qt.SizeVerCursor,
        Qt.TopLeftCorner: Qt.SizeFDiagCursor,
        Qt.TopRightCorner: Qt.SizeBDiagCursor,
        Qt.BottomLeftCorner: Qt.SizeBDiagCursor,
        Qt.BottomRightCorner: Qt.SizeFDiagCursor,
    }

    def __init__(self):
        super().__init__()

        self.setObjectName("window")
        self.setWindowTitle("VK Offline Player")
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)


        self.setMinimumSize(MIN_WINDOW_W, MIN_WINDOW_H)
        self.setMouseTracking(True)



        self.resize(MIN_WINDOW_W, MIN_WINDOW_H)

        self._tracks = []
        self._all_rows = []
        self._playing = False
        self._shuffle = False
        self._seeking = False
        self._closing = False
        self._dev_tick = 0

        self._load_thread = None
        self._workers = []
        self._audio_pending = set()     # track_id с уже работающим AudioWorker

        self._current = None
        self._current_id = None
        self._loaded_id = None

        # капча входа: sid последнего запроса (ответ вводится в форму)
        self._captcha_sid = ""


        self._win_integrated = False
        self._media_keys = None         # SMTC (Windows) — медиа-кнопки

        # настройки звука, переживающие ленивое создание движка
        self._eq_gains = [0] * 10          # гейны полос EQ, дБ (целые)
        self._dyn_mode = "multiband"      # где работает компрессия

        self.engine = None

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(100)
        self._ui_timer.timeout.connect(self._poll)
        self._ui_timer.start()

        # поиск с дебаунсом: фильтрация не чаще 4 раз в секунду
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._apply_search_now)

        # скрытое автосохранение настроек интерфейса (EQ/скорость/динамика/
        # громкость/шафл) в БД: изменения копим и пишем пачкой через 400 мс
        self._ui_pending = {}
        self._ui_state_timer = QTimer(self)
        self._ui_state_timer.setSingleShot(True)
        self._ui_state_timer.setInterval(400)
        self._ui_state_timer.timeout.connect(self._flush_ui_state)

        self._resize_edge = None
        self._resize_origin = None
        self._resize_geo_origin = None
        self._resize_min_size = QSize(MIN_WINDOW_W, MIN_WINDOW_H)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.title_bar = TitleBar()
        v.addWidget(self.title_bar)

        self.content = QWidget()
        cv = QVBoxLayout(self.content)
        cv.setContentsMargins(12, 10, 12, 10)
        cv.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)

        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search tracks or artists...")
        self.search.setFixedHeight(32)

        self.load_btn = QPushButton("Load Tracks")
        self.load_btn.setObjectName("primaryBtn")
        self.load_btn.setFixedHeight(32)

        top.addWidget(self.search, 1)
        top.addWidget(self.load_btn)

        cv.addLayout(top)

        self.library = LibraryPanel()
        self.now_playing = NowPlayingPanel()




        mid = QHBoxLayout()
        mid.setSpacing(10)
        mid.addWidget(self.library, 5)
        mid.addWidget(self.now_playing, 4)




        cv.addLayout(mid, 1)

        self.eq_panel = EqualizerPanel()
        self.speed_panel = SpeedPanel()
        self.dyn_panel = DynamicsPanel()



        right = QWidget()
        right.setMaximumWidth(420)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(10)
        rl.addWidget(self.speed_panel)
        rl.addWidget(self.dyn_panel)

        low = QHBoxLayout()
        low.setSpacing(10)
        low.addWidget(self.eq_panel, 2)
        low.addWidget(right, 1)



        cv.addLayout(low, 0)

        v.addWidget(self.content, 1)



        self.content.setMouseTracking(True)

        self.player_bar = PlayerBar()
        self.player_bar.setMouseTracking(True)
        v.addWidget(self.player_bar)

        self.auth_overlay = AuthOverlay(self)
        self.auth_overlay.hide()
        self.auth_overlay.submitted.connect(self._on_auth_submitted)

        self.cover_loader = CoverLoader()
        self.cover_loader.loaded.connect(self._on_cover_loaded)
        self.cover_loader.missing.connect(self._on_cover_missing)
        self.cover_loader.start()

        self.library.need_covers.connect(self._on_need_covers)

        # медиа-кнопки: сигнал из RPC-потока -> слот в GUI-потоке
        self.sig_media_action.connect(self._on_media_action)

        self._wire_events()



        self._load_ui_state()

        self._init_local()

    def closeEvent(self, event):
        self._closing = True
        self._ui_timer.stop()
        self._search_timer.stop()


        self._ui_state_timer.stop()
        self._flush_ui_state()


        if self._media_keys is not None:
            self._media_keys.close()
            self._media_keys = None

        if self.engine is not None:
            self.engine.stop()

        self.cover_loader.stop()

        for w in list(self._workers):
            w.wait()

        if self._load_thread is not None:
            self._load_thread.wait()

        self.cover_loader.wait()

        super().closeEvent(event)

    def _poll(self):
        if self.engine is None or not self.engine.loaded:
            return

        # устройство вывода проверяем раз в ~1 с, позицию — каждый тик (10 Гц)
        self._dev_tick += 1
        if self._dev_tick >= 10:
            self._dev_tick = 0
            self.engine.check_output_device()

        pos_written = False




        if self._current_id is not None and self._current_id != self._loaded_id:
            self.set_position(0, 1000)
            pos_written = True

        if not pos_written:
            pos, dur = self.engine.position_ms()
            self.set_position(pos, max(1, dur))

        if self.engine.finished and self._playing:



            if self._current_id in self._audio_pending:
                return
            self._step(1)

    def _ensure_engine(self):
        if self.engine is not None:
            return True

        try:
            self.engine = AudioEngine()

            # движок ленивый: применяем настройки EQ, заданные ДО его создания
            for band, gain in enumerate(self._eq_gains):
                self.engine.set_eq_band(band, gain)
            self.engine.set_dynamics_mode(self._dyn_mode)
            return True
        except Exception as ex:
            print(ex)
            return False

    def _wire_events(self):
        pb = self.player_bar

        pb.play.clicked.connect(lambda _=False: self._on_play_clicked())
        pb.next.clicked.connect(lambda _=False: self._step(1))
        pb.prev.clicked.connect(lambda _=False: self._step(-1))
        pb.shuffle.clicked.connect(lambda _=False: self._toggle_shuffle())

        # перемотка применяется ОДИН раз — на отпускание кнопки. Раньше
        # seek висел и на sliderMoved: клик прыгал в точку, а дальше
        # «синтетический» drag тянул ползунок за мышью и перематывал на
        # каждый пиксель движения — отсюда двойной/дребезжащий звук при
        # клике по перемотке. Теперь: клик/драг двигает ползунок,
        # отпускание — единственный seek
        pb.progress.sliderPressed.connect(self._on_seek_pressed)
        pb.progress.sliderReleased.connect(self._on_seek_released)
        pb.volume.valueChanged.connect(self._on_volume)

        self.speed_panel.slider.valueChanged.connect(self._on_speed)

        for i, s in enumerate(self.eq_panel.sliders):
            s.valueChanged.connect(lambda val, band=i: self._on_eq(band, val))

        self.dyn_panel.sig_dynamics_mode.connect(self._on_dyn_mode)

        self.search.textChanged.connect(self._on_search_text)

        self.load_btn.clicked.connect(lambda _=False: self._on_load_requested())
        self.now_playing.export_btn.clicked.connect(lambda _=False: self._on_export())

        self.library.list.doubleClicked.connect(
            lambda idx: self._on_double_clicked(idx.row())
        )

    def _init_local(self):
        try:
            rows = DB.get_audio(2147483647)
        except Exception as ex:
            print(ex)
            rows = []

        self._set_rows(rows)

        if not rows:
            self._show_auth_from_settings()

    def _set_rows(self, rows):
        self._all_rows = list(rows)
        self._apply_search(self.search.text())

    def _on_search_text(self):
        self._search_timer.start()

    def _apply_search_now(self):
        self._apply_search(self.search.text())

    # -------- скрытое сохранение/восстановление настроек интерфейса -------

    def _queue_ui_save(self, key: str, value):

        self._ui_pending[key] = str(value)
        self._ui_state_timer.start()

    def _flush_ui_state(self):

        if not self._ui_pending:
            return

        pending = dict(self._ui_pending)
        self._ui_pending.clear()

        try:
            for key, value in pending.items():
                DB.set_ui_state(key, value)
        except Exception as ex:
            print(ex)

    def _load_ui_state(self):

        try:
            state = DB.get_ui_state()
        except Exception as ex:
            print(ex)
            return

        if not state:
            return

        def clamp_int(key, default, lo, hi):
            try:
                return max(lo, min(hi, int(state.get(key, default))))
            except (TypeError, ValueError):
                return default

        # --- ползунки эквалайзера (10 полос), шкала ±20 дБ
        for band, sl in enumerate(self.eq_panel.sliders):
            val = clamp_int(f"eq_{band}", 0, -20, 20)
            sl.blockSignals(True)
            sl.setValue(val)
            sl.blockSignals(False)
            self._eq_gains[band] = val

        # --- скорость 0.00x..3.00x
        speed = clamp_int("speed", 100, 0, 300)
        sl = self.speed_panel.slider
        sl.blockSignals(True)
        sl.setValue(speed)
        sl.blockSignals(False)
        self.speed_panel.set_label(speed)

        # --- режим динамики
        mode = state.get("dyn_mode", "multiband")
        if mode not in ("multiband", "output", "off"):
            mode = "multiband"
        self.dyn_panel.set_mode(mode)
        self._dyn_mode = mode

        # --- громкость (шкала 0..200, 100 = единичная)
        vol = clamp_int("volume", 100, 0, 200)
        v = self.player_bar.volume
        v.blockSignals(True)
        v.setValue(vol)
        v.blockSignals(False)

        # --- рандомизация
        self._shuffle = state.get("shuffle", "0") == "1"
        self.player_bar.shuffle.setChecked(self._shuffle)

    def _on_eq(self, band: int, val: int):
        self._eq_gains[band] = val
        self._queue_ui_save(f"eq_{band}", val)

        if self.engine is not None:
            self.engine.set_eq_band(band, val)

    def _on_dyn_mode(self, mode: str):
        self._dyn_mode = mode
        self._queue_ui_save("dyn_mode", mode)

        if self.engine is not None:
            self.engine.set_dynamics_mode(mode)

    def _on_need_covers(self, ids):
        if ids:
            self.cover_loader.enqueue(ids)

    def _on_cover_loaded(self, track_id: int, blob):
        self._apply_cover(track_id, blob)

    def _on_cover_missing(self, track_id: int):
        self.library.model.mark_missing(track_id)

    def _apply_cover(self, track_id: int, blob):
        if self._closing:
            return

        if not blob:
            self.library.model.mark_missing(track_id)
            return

        # два размера с ОДНОГО оригинала: мелкий — в список (24px),
        # большой — в «сейчас играет» (120px); оба в физическом разрешении
        pm_list = fit_cover(blob, 24, 5)
        pm_big = fit_cover(blob, 120, 10)


        self.library.update_cover(track_id, pm_list, pm_big)

        # hot swap в проигрывателе — только если панель «сейчас играет»
        # уже показывает этот трек (трек мог скачаться, но ещё не заиграть)
        if self._loaded_id == track_id:
            self.now_playing.cover.setPixmap(pm_big)

    def _apply_search(self, text: str = ""):
        text = (text or "").strip().lower()

        if not text:
            rows = self._all_rows
        else:
            rows = [
                r for r in self._all_rows
                if text in (r["artist"] or "").lower() or text in (r["title"] or "").lower()
            ]

        self.set_tracks(rows)

    def _show_auth_from_settings(self, status: str = ""):
        try:
            s = DB.get_settings()
        except Exception as ex:
            print(ex)
            s = None

        auth_type = int(s["auth_type"] or 0) if s else 0
        token = s["access_token"] if s else ""
        login = s["login"] if s else ""
        password = s["password"] if s else ""

        self.auth_overlay.show_auth(
            auth_type=auth_type,
            token=token or "",
            login=login or "",
            password=password or "",
            status=status or "",
        )

    def _on_load_requested(self):
        if self._load_thread is not None and self._load_thread.isRunning():
            return

        try:
            s = DB.get_settings()
        except Exception as ex:
            print(ex)
            self._show_auth_from_settings()
            return

        auth_type = int(s["auth_type"] or 0) if s else 0
        token = (s["access_token"] or "").strip() if s else ""
        login = (s["login"] or "").strip() if s else ""
        password = s["password"] or "" if s else ""
        # для автообновления протухшего токена
        refresh_token = (s["refresh_token"] or "").strip() if s else ""
        app_id = (s["app_id"] or "").strip() if s else ""
        device_id = (s["device_id"] or "").strip() if s else ""

        if auth_type == 0 and not token:
            self._show_auth_from_settings()
            return

        if auth_type == 1 and (not login or not password):
            self._show_auth_from_settings()
            return

        self._start_load(auth_type, token, login, password,
                         refresh_token, app_id, device_id)

    def _on_auth_submitted(self, auth_type: int, token: str, login: str,
                           password: str, twofa_code: str = "",
                           captcha_key: str = ""):
        token = token.strip()
        login = login.strip()
        password = password.strip()
        twofa_code = twofa_code.strip()
        captcha_key = captcha_key.strip()

        # вставили ссылку после авторизации — вынимаем из неё токены:
        # VK ID отдаёт access_token + refresh_token (+ user_id) в адресе
        refresh_token = app_id = device_id = ""
        if auth_type == 0 and token:
            parsed = parse_token_input(token)
            token = parsed["access_token"]
            refresh_token = parsed["refresh_token"]
            app_id = parsed["app_id"]
            device_id = parsed["device_id"]

        if auth_type == 0 and not token:
            print("Token is empty")
            return

        if auth_type == 1 and (not login or not password):
            print("Login/password are empty")
            return

        try:
            DB.set_settings("auth_type", auth_type)
            DB.set_settings("access_token", token)
            DB.set_settings("login", login)
            DB.set_settings("password", password)
            # refresh-цепочку обновляем только новым значением — иначе
            # затёрли бы сохранённый refresh_token голым access-токеном
            if refresh_token:
                DB.set_settings("refresh_token", refresh_token)
            if app_id:
                DB.set_settings("app_id", app_id)
            if device_id:
                DB.set_settings("device_id", device_id)
        except Exception as ex:
            print(ex)

        self._start_load(auth_type, token, login, password,
                         refresh_token, app_id, device_id,
                         twofa_code=twofa_code,
                         captcha_sid=self._captcha_sid,
                         captcha_key=captcha_key)

    def _start_load(self, auth_type: int, token: str, login: str, password: str,
                    refresh_token: str = "", app_id: str = "", device_id: str = "",
                    twofa_code: str = "", captcha_sid: str = "",
                    captcha_key: str = ""):
        if self._load_thread is not None and self._load_thread.isRunning():
            return

        self.load_btn.setEnabled(False)
        self.auth_overlay.set_loading(True)

        worker = LoadWorker(auth_type, token, login, password,
                            refresh_token, app_id, device_id,
                            twofa_code, captcha_sid, captcha_key)

        worker.ok.connect(self._on_loaded)
        worker.auth.connect(self._on_auth_needed)
        worker.failed.connect(self._on_load_failed)
        worker.need_2fa.connect(self._on_need_2fa)
        worker.need_captcha.connect(self._on_need_captcha)
        worker.finished.connect(lambda: self._on_thread_finished(worker))

        self._load_thread = worker
        worker.start()

    def _on_thread_finished(self, worker):
        if self._load_thread is worker:
            self._load_thread = None

        if worker in self._workers:
            self._workers.remove(worker)

        worker.deleteLater()

    def _on_loaded(self, rows):
        self._set_rows(rows)


        self._captcha_sid = ""

        self.load_btn.setEnabled(True)
        self.auth_overlay.set_loading(False)
        self.auth_overlay.hide()

    def _on_need_2fa(self, phone_mask: str):

        self.load_btn.setEnabled(True)
        self.auth_overlay.show_2fa(phone_mask or "")

    def _on_need_captcha(self, image_bytes, captcha_sid: str):

        self._captcha_sid = captcha_sid or ""
        self.load_btn.setEnabled(True)
        self.auth_overlay.show_captcha(image_bytes)

    def _on_auth_needed(self):
        self.load_btn.setEnabled(True)
        self.auth_overlay.set_loading(False)

        if not self.auth_overlay.isVisible():
            self._show_auth_from_settings(VKC.auth_hint or "")
        else:
            self.auth_overlay.raise_()

    def _on_load_failed(self):
        self.load_btn.setEnabled(True)
        self.auth_overlay.set_loading(False)

    def _current_row(self):
        return self.library.list.currentIndex().row()

    def _on_double_clicked(self, row: int):
        self.library.list.setCurrentIndex(self.library.model.index(row, 0))
        self._play_row(row)

    def _play_row(self, row: int):
        if row < 0 or row >= len(self._tracks):
            return

        if not self._ensure_engine():
            return

        r = self._tracks[row]






        self._current = r
        self._current_id = r["track_id"]


        pm = self.library.model.big_cover_for(self._current_id)
        if pm is None:
            self.library.model.mark_pending(self._current_id)

            cover_worker = CoverWorker(self._current_id, r["image_url"])
            cover_worker.ok.connect(self._on_cover_ready)
            cover_worker.failed.connect(
                lambda tid, _err: self.library.model.unmark_pending(tid)
            )
            cover_worker.finished.connect(lambda w=cover_worker: self._on_worker_done(w))

            self._workers.append(cover_worker)
            cover_worker.start()

        # аудио качается ТОЛЬКО по требованию (запуск трека). Если этот трек
        # уже грузится — дублей не плодим; данные проверяются в БД,
        # качается только при отсутствии (см. fetch_audio)
        if self._current_id not in self._audio_pending:
            self._audio_pending.add(self._current_id)
            self.library.model.set_download(self._current_id, 0.0)

            audio_worker = AudioWorker(self._current_id, r["audio_url"])
            audio_worker.ok.connect(self._on_audio_ready)
            audio_worker.failed.connect(self._on_audio_failed)
            audio_worker.finished.connect(
                lambda w=audio_worker: self._on_worker_done(w))

            self._workers.append(audio_worker)
            audio_worker.start()

    def _on_worker_done(self, worker):
        if isinstance(worker, AudioWorker):
            self._audio_pending.discard(worker.track_id)

        if worker in self._workers:
            self._workers.remove(worker)

        worker.deleteLater()

    def _on_cover_ready(self, track_id: int, data):
        self._apply_cover(track_id, data)

    def _on_audio_ready(self, track_id: int, data):
        if self._closing:
            return


        self.library.model.clear_download(track_id)

        if self._current_id != track_id:
            return

        if self.engine is None or not data:
            return

        if not self.engine.load(data):


            self._on_audio_failed(track_id, "decode error")
            return

        self._loaded_id = track_id

        self.engine.set_volume(self.player_bar.volume.value() / 100)
        self.engine.set_rate(self.speed_panel.slider.value() / 100)

        self.engine.play()
        self.set_playing(True)



        self.library.model.set_playing(track_id)
        self.now_playing.update_track(
            self._current, self.library.model.big_cover_for(track_id))

        # метаданные для системного оверлея медиа-кнопок (SMTC)
        if self._media_keys is not None and self._current is not None:
            self._media_keys.set_track(self._current["title"],
                                       self._current["artist"])

    def _on_audio_failed(self, track_id: int, _err: str):

        self._audio_pending.discard(track_id)

        if self._closing:
            return


        self.library.model.set_download(track_id, -1.0)

        def _clear(tid=track_id):
            if not self._closing:
                self.library.model.clear_download(tid)

        QTimer.singleShot(2000, _clear)

        if self._current_id != track_id:
            return

        if self._loaded_id is not None and self._loaded_id != track_id:


            self._current_id = self._loaded_id
            self._current = self._row_by_id(self._loaded_id)



            if self.engine is not None and self.engine.finished:
                self.set_playing(False)
            return


        self.set_playing(False)

    def _row_by_id(self, track_id):
        for r in self._all_rows:
            if r["track_id"] == track_id:
                return r
        return None

    def _on_play_clicked(self):
        if not self._ensure_engine():
            return

        if self._playing:
            self.engine.pause()
            self.set_playing(False)
            return

        if self.engine.loaded and not self.engine.finished:
            self.engine.play()
            self.set_playing(True)
            return

        if self.engine.loaded and self.engine.finished:
            self.engine.seek_ms(0)
            self.engine.play()
            self.set_playing(True)
            return

        self._play_row(self._current_row())

    def _step(self, d: int):
        if not self._tracks:
            return

        if self._shuffle and len(self._tracks) > 1:
            new = random.randrange(len(self._tracks))
        else:
            new = (self._current_row() + d) % len(self._tracks)

        self.library.list.setCurrentIndex(self.library.model.index(new, 0))
        self._play_row(new)

    def _toggle_shuffle(self):
        self._shuffle = not self._shuffle
        self.player_bar.shuffle.setChecked(self._shuffle)
        self._queue_ui_save("shuffle", int(self._shuffle))

    def _on_seek(self, value: int):
        if self.engine is not None:
            self.engine.seek_ms(value)

    def _on_seek_pressed(self):
        self._seeking = True

    def _on_seek_released(self):
        self._seeking = False
        self._on_seek(self.player_bar.progress.value())

    def _on_volume(self, value: int):
        # шкала 0..200, 100 = единичная громкость
        if self.engine is not None:
            self.engine.set_volume(value / 100)

        self._queue_ui_save("volume", value)

    def _on_speed(self, val: int):
        self.speed_panel.set_label(val)

        if self.engine is not None:
            self.engine.set_rate(val / 100)

        self._queue_ui_save("speed", val)

    def _on_export(self):
        row = self._current

        if not row:
            return

        default = f"{row['artist']} - {row['title']}.mp3"

        path, _ = QFileDialog.getSaveFileName(self, "Экспорт трека", default, "MP3 (*.mp3)")

        if not path:
            return

        if not path.lower().endswith(".mp3"):
            path += ".mp3"

        worker = ExportWorker(row["track_id"], row["audio_url"], path)
        worker.done.connect(self._on_export_done)
        worker.finished.connect(lambda w=worker: self._on_worker_done(w))

        self._workers.append(worker)
        worker.start()

    @Slot(str, bool)
    def _on_export_done(self, path: str, ok: bool):
        print(f"Сохранено: {path}" if ok else f"Не удалось сохранить: {path}")

    @Slot(list)
    def set_tracks(self, rows):
        self._tracks = list(rows)

        self.library.set_tracks(self._tracks)

        if not self._tracks:
            return


        if self._current_id is not None:
            row = self.library.model.row_by_id(self._current_id)
            if row >= 0:
                self.library.list.setCurrentIndex(self.library.model.index(row, 0))
                return

        if not self.library.list.currentIndex().isValid():
            self.library.list.setCurrentIndex(self.library.model.index(0, 0))

    @Slot(bool)
    def set_playing(self, playing: bool):
        self._playing = playing
        self.player_bar.set_play_icon(playing)

        # статус в системном оверлее медиа-кнопок (SMTC)
        if self._media_keys is not None:
            self._media_keys.set_status(playing)

    @Slot(int, int)
    def set_position(self, value: int, maximum: int):
        if self._seeking:
            return

        bar = self.player_bar.progress

        bar.blockSignals(True)
        bar.setRange(0, max(1, maximum))
        bar.setValue(value)
        bar.blockSignals(False)

    def _update_overlay_geometry(self):
        if not hasattr(self, "auth_overlay"):
            return

        top = self.title_bar.height() if hasattr(self, "title_bar") else 0

        self.auth_overlay.setGeometry(
            0,
            top,
            max(0, self.width()),
            max(0, self.height() - top)
        )

    def showEvent(self, event):
        super().showEvent(event)
        self._update_overlay_geometry()

        if not self._win_integrated:
            self._win_integrated = True
            self._integrate_with_windows()

    def changeEvent(self, event):
        super().changeEvent(event)


        if event.type() == QEvent.Type.WindowStateChange:
            self.title_bar.set_maximized(self.isMaximized())

    def _integrate_with_windows(self):

        if sys.platform != "win32":
            return

        try:
            import ctypes

            hwnd = int(self.winId())
            GWL_STYLE = -16
            WS_MAXIMIZEBOX = 0x00010000
            WS_MINIMIZEBOX = 0x00020000

            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            user32.SetWindowLongW(
                hwnd, GWL_STYLE,
                style | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
        except Exception as ex:
            print("Taskbar integration failed:", ex)

        try:
            try:
                from core.media_keys import MediaKeys
            except Exception:
                from media_keys import MediaKeys

            mk = MediaKeys()
            if not mk.attach(int(self.winId())):
                return

            self._media_keys = mk

            emit = self.sig_media_action.emit
            mk.on_command("play", lambda: emit("play"))
            mk.on_command("pause", lambda: emit("pause"))
            mk.on_command("stop", lambda: emit("pause"))
            mk.on_command("next", lambda: emit("next"))
            mk.on_command("previous", lambda: emit("previous"))
        except Exception as ex:
            print("Media keys integration failed:", ex)

    @Slot(str)
    def _on_media_action(self, action: str):

        if action == "play":
            if not self._playing:
                self._on_play_clicked()
        elif action == "pause":
            if self._playing:
                self._on_play_clicked()
        elif action == "next":
            self._step(1)
        elif action == "previous":
            self._step(-1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_overlay_geometry()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        p.setClipPath(path)


        p.fillRect(rect, QColor("#0f1216"))

        p.setBrush(Qt.NoBrush)
        p.setPen(QColor(255, 255, 255, 16))
        p.drawPath(path)

        p.end()

    def _get_resize_edge(self, pos: QPoint):
        if self.isMaximized():
            return None

        rect = self.rect()
        m = RESIZE_MARGIN

        left = pos.x() <= m
        right = pos.x() >= rect.width() - m
        top = pos.y() <= m
        bottom = pos.y() >= rect.height() - m

        if top and left:
            return Qt.TopLeftCorner
        if top and right:
            return Qt.TopRightCorner
        if bottom and left:
            return Qt.BottomLeftCorner
        if bottom and right:
            return Qt.BottomRightCorner
        if left:
            return Qt.LeftEdge
        if right:
            return Qt.RightEdge
        if top:
            return Qt.TopEdge
        if bottom:
            return Qt.BottomEdge

        return None

    def _cursor_for_edge(self, edge):
        return self._EDGE_CURSORS.get(edge, Qt.ArrowCursor)

    def leaveEvent(self, event):
        if self._resize_edge is None:
            self.unsetCursor()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            edge = self._get_resize_edge(event.position().toPoint())
            if edge is not None:
                self._resize_edge = edge
                self._resize_origin = event.globalPosition().toPoint()
                self._resize_geo_origin = QRect(self.geometry())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):


        if (self._resize_edge is not None
                and self._resize_origin is not None
                and event.buttons() & Qt.LeftButton):
            delta = event.globalPosition().toPoint() - self._resize_origin
            geo = QRect(self._resize_geo_origin)
            min_w = self._resize_min_size.width()
            min_h = self._resize_min_size.height()
            e = self._resize_edge





            if e in (Qt.LeftEdge, Qt.TopLeftCorner, Qt.BottomLeftCorner):
                new_left = min(geo.left() + delta.x(),
                               geo.right() - min_w + 1)
                geo.setLeft(new_left)

            if e in (Qt.RightEdge, Qt.TopRightCorner, Qt.BottomRightCorner):
                geo.setWidth(max(min_w, geo.width() + delta.x()))

            if e in (Qt.TopEdge, Qt.TopLeftCorner, Qt.TopRightCorner):
                new_top = min(geo.top() + delta.y(),
                              geo.bottom() - min_h + 1)
                geo.setTop(new_top)

            if e in (Qt.BottomEdge, Qt.BottomLeftCorner, Qt.BottomRightCorner):
                geo.setHeight(max(min_h, geo.height() + delta.y()))

            self.setGeometry(geo)
            event.accept()
            return

        edge = self._get_resize_edge(event.position().toPoint())
        if edge is not None:
            self.setCursor(self._cursor_for_edge(edge))
        else:
            self.unsetCursor()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resize_edge is not None:
            self._resize_edge = None
            self._resize_origin = None
            self._resize_geo_origin = None
            event.accept()
            return
        super().mouseReleaseEvent(event)


STYLE = """
/* ------- минималистичная тема: плоскость, один акцент, тонкие линии ------- */

QWidget#titleBar {
    background: #0b0e12;
    border-top-left-radius: 10px; border-top-right-radius: 10px;
}

QLabel#titleText { color:#dfe3ea; font-size:14px; font-weight:600; }

QPushButton#titleBtn, QPushButton#closeBtn {
    background:transparent; border:none; color:#8b929e; font-size:13px; border-radius:6px;
}
QPushButton#titleBtn:hover { background:rgba(255,255,255,0.06); color:#dfe3ea; }
QPushButton#closeBtn:hover { background:#e0483f; color:white; }

QLineEdit#search {
    background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.06);
    border-radius:7px; padding:0 12px; color:#dfe3ea; font-size:14px;
}
QLineEdit#search:focus { border:1px solid rgba(76,141,255,0.55); }

QPushButton#primaryBtn {
    background:#4c8dff; color:white; border:none; border-radius:7px;
    padding:0 16px; font-size:14px; font-weight:600;
}
QPushButton#primaryBtn:hover { background:#5e97ff; }
QPushButton#primaryBtn:pressed { background:#3d78d9; }
QPushButton#primaryBtn:disabled { background:#2b3340; color:#5c636e; }

QPushButton#ghostBtn {
    background:transparent; border:1px solid rgba(255,255,255,0.09); border-radius:7px;
    color:#9aa2ad; font-size:13px; font-weight:600;
}
QPushButton#ghostBtn:hover { background:rgba(255,255,255,0.05); color:#c6ccd5; }

QFrame#panel {
    background:rgba(255,255,255,0.025);
    border:1px solid rgba(255,255,255,0.06);
    border-radius:8px;
}

QLabel#panelTitle { color:#dfe3ea; font-size:13px; font-weight:600; }
QLabel#muted      { color:#8b929e; font-size:13px; }
QLabel#npTitle    { color:#e7eaee; font-size:15px; font-weight:600; }
QLabel#npArtist   { color:#98a0ab; font-size:14px; }
QLabel#eqLabel    { color:#878e99; font-size:12px; }

/* переключатель динамики: плоская «сегментная» кнопка */
QPushButton#segBtn {
    background:transparent; border:1px solid rgba(255,255,255,0.08);
    border-radius:5px; color:#8b929e; font-size:12px; font-weight:600;
    padding:3px 10px;
}
QPushButton#segBtn:hover { background:rgba(255,255,255,0.05); color:#c6ccd5; }
QPushButton#segBtn:checked {
    background:rgba(76,141,255,0.14); border:1px solid rgba(76,141,255,0.45);
    color:#dbe7ff;
}

QListView#trackList { background:transparent; border:none; outline:none; }
QListView#trackList::item { background:transparent; }

QScrollBar:vertical { width:10px; background:transparent; }
QScrollBar::handle:vertical { background:rgba(255,255,255,0.14); border-radius:5px; min-height:40px; }
QScrollBar::handle:vertical:hover { background:rgba(255,255,255,0.22); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }

QScrollBar:horizontal { height:10px; background:transparent; margin:2px; }
QScrollBar::handle:horizontal { background:rgba(255,255,255,0.14); border-radius:5px; min-width:40px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; }

/* ползунки: тонкая скруглённая полоса + ручка, единственный акцент */
QSlider::groove:horizontal { height:5px; background:rgba(255,255,255,0.10); border-radius:2.5px; }
QSlider::sub-page:horizontal { background:#4c8dff; border-radius:2.5px; }
QSlider::add-page:horizontal { background:rgba(255,255,255,0.10); border-radius:2.5px; }
QSlider::handle:horizontal {
    width:14px; height:14px; margin:-4.5px 0;
    background-color:#dfe3ea; border:none; border-radius:7px;
}
QSlider::handle:horizontal:hover { background-color:#ffffff; }
QSlider::handle:horizontal:disabled { background-color:#4a515c; }

QSlider::groove:vertical { width:5px; background:rgba(255,255,255,0.10); border-radius:2.5px; }
QSlider::handle:vertical {
    width:14px; height:14px; margin:0 -4.5px;
    background-color:#c9ced6; border:none; border-radius:7px;
}
QSlider::handle:vertical:hover { background-color:#e7eaee; }

QWidget#playerBar {
    background:#0b0e12;
    border-top:1px solid rgba(255,255,255,0.055);
    border-bottom-left-radius:10px; border-bottom-right-radius:10px;
}

QPushButton#roundBtn {
    background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.06);
    border-radius:16px; color:#c6ccd5; font-size:12px;
}
QPushButton#roundBtn:hover { background:rgba(255,255,255,0.10); }
QPushButton#roundBtn:checked {
    background:rgba(76,141,255,0.15); border:1px solid rgba(76,141,255,0.45); color:#dbe7ff;
}
QPushButton#roundBtn:disabled { color:#4a515c; background:rgba(255,255,255,0.03); }

QPushButton#playBtn {
    background:#e8ebf0; border:none; border-radius:17px; color:#10151b;
    padding:0; font-size:14px; font-weight:700;
}
QPushButton#playBtn:hover { background:#ffffff; }
QPushButton#playBtn:pressed { background:#d3d8e0; }
QPushButton#playBtn:disabled { background:#2b3340; color:#5c636e; }

QFrame#authTabs { background:rgba(255,255,255,0.04); border-radius:7px; }
QPushButton#authTab {
    background:transparent; border:none; color:#8b929e;
    font-size:14px; font-weight:600; border-radius:6px; padding:6px 0;
}
QPushButton#authTab:hover { color:#dfe3ea; }
QPushButton#authTab[active="true"] { background:#4c8dff; color:white; }

QStackedWidget { background:transparent; }
"""


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")




    font = QFont()
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Tahoma"])
    font.setPointSize(11)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)

    app.setStyleSheet(STYLE)

    w = PlayerWindow()
    w.show()

    # Ctrl+C в консоли: аккуратный выход через app.quit() вместо
    # KeyboardInterrupt посреди QTimer-колбэка (Traceback + зависание).
    # Обработчик срабатывает при первом же тике UI-таймера (<=100 мс).
    def _graceful_quit(*_):
        app.quit()

    signal.signal(signal.SIGINT, _graceful_quit)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _graceful_quit)

    try:
        code = app.exec()
    except KeyboardInterrupt:
        code = 0
    finally:
        # гарантируем closeEvent-очистку (движок, потоки, SMTC)
        w.close()

    sys.exit(code)


if __name__ == "__main__":
    main()
