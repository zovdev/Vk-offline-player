from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QSlider, QLabel
from PySide6.QtCore import Qt, Signal

class PlayerControls(QWidget):
    play_clicked = Signal()
    pause_clicked = Signal()
    seek_changed = Signal(float)
    volume_changed = Signal(float)

    def __init__(self):
        super().__init__()
        self.layout = QHBoxLayout(self)
        
        self.play_btn = QPushButton("Play")
        self.play_btn.setFixedWidth(80)
        self.play_btn.clicked.connect(self.toggle_play)
        self.is_playing = False
        
        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.sliderReleased.connect(self.on_seek)
        
        self.vol_label = QLabel("Vol")
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setFixedWidth(100)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(100)
        self.vol_slider.valueChanged.connect(self.on_volume)
        
        self.layout.addWidget(self.play_btn)
        self.layout.addWidget(self.seek_slider)
        self.layout.addWidget(self.vol_label)
        self.layout.addWidget(self.vol_slider)
        
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
