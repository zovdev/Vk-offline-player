from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QSlider, QLabel
from PySide6.QtCore import Qt, Signal

class PlayerControls(QWidget):
    prev_clicked = Signal()
    next_clicked = Signal()
    play_clicked = Signal()
    pause_clicked = Signal()
    seek_changed = Signal(float)
    volume_changed = Signal(float)

    def __init__(self):
        super().__init__()
        self.layout = QHBoxLayout(self)
        
        self.prev_btn = QPushButton("<<")
        self.prev_btn.setFixedWidth(40)
        self.prev_btn.clicked.connect(self.prev_clicked.emit)
        
        self.play_btn = QPushButton("Play")
        self.play_btn.setFixedWidth(80)
        self.play_btn.clicked.connect(self.toggle_play)
        self.is_playing = False
        
        self.next_btn = QPushButton(">>")
        self.next_btn.setFixedWidth(40)
        self.next_btn.clicked.connect(self.next_clicked.emit)
        
        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.sliderReleased.connect(self.on_seek)
        
        self.seek_slider.sliderReleased.connect(self.on_seek)
        
        # Mini Mode Widgets
        self.mini_info_label = QLabel("")
        self.mini_info_label.setAlignment(Qt.AlignCenter)
        self.mini_info_label.setVisible(False)
        self.mini_info_label.setWordWrap(True) # Just in case, though 1 line pref
        
        self.mini_art_label = QLabel()
        self.mini_art_label.setFixedSize(40, 40)
        self.mini_art_label.setVisible(False)
        self.mini_art_label.setStyleSheet("background-color: #333; border-radius: 4px;")
        
        self.vol_label = QLabel("Vol")
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setFixedWidth(100)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(100)
        self.vol_slider.valueChanged.connect(self.on_volume)
        
        self.layout.addWidget(self.prev_btn)
        self.layout.addWidget(self.play_btn)
        self.layout.addWidget(self.next_btn)
        
        # Layout Order: [Prev] [Play] [Next] [Seek | Info] [Vol | Art]
        self.layout.addWidget(self.mini_info_label, 1) # Stretch info
        self.layout.addWidget(self.seek_slider, 1) # Stretch seek
        
        self.layout.addWidget(self.vol_label)
        self.layout.addWidget(self.vol_slider)
        self.layout.addWidget(self.mini_art_label)
        
    def toggle_play(self):
        if self.is_playing:
            self.pause_clicked.emit()
            self.play_btn.setText("Play")
        else:
            self.play_clicked.emit()
            self.play_btn.setText("Pause")
        self.is_playing = not self.is_playing

    def set_playing(self, playing):
        self.is_playing = playing
        self.play_btn.setText("Pause" if playing else "Play")

    def on_seek(self):
        val = self.seek_slider.value() / 1000.0
        self.seek_changed.emit(val)

    def on_volume(self):
        val = self.vol_slider.value() / 100.0
        self.volume_changed.emit(val)
        
    def update_seek(self, percent):
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(int(percent * 1000))

    def set_mini_mode(self, is_mini):
        # Toggle visibility sets
        
        # Standard Set
        self.prev_btn.setHidden(is_mini)
        self.seek_slider.setHidden(is_mini)
        self.vol_label.setHidden(is_mini)
        self.vol_slider.setHidden(is_mini)
        
        # Mini Set
        self.mini_info_label.setVisible(is_mini)
        self.mini_art_label.setVisible(is_mini)
        
        if is_mini:
            self.layout.setContentsMargins(5, 5, 5, 5) # Enough for bar
        else:
            self.layout.setContentsMargins(9, 9, 9, 9)

    def update_mini_info(self, title, artist, pixmap=None):
        text = f"{artist} - {title}" if title else ""
        self.mini_info_label.setText(text)
        
        if pixmap:
            # Scale to 40x40
            scaled = pixmap.scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.mini_art_label.setPixmap(scaled)
        else:
            self.mini_art_label.clear()
