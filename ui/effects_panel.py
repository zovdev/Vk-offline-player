from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QSlider, QLabel, QGroupBox
from PySide6.QtCore import Qt, Signal

class EffectsPanel(QWidget):
    eq_changed = Signal(int, float)
    speed_changed = Signal(float)

    def __init__(self):
        super().__init__()
        self.layout = QHBoxLayout(self)
        
        self.eq_group = QGroupBox("Equalizer (10 Band)")
        self.eq_layout = QHBoxLayout()
        self.eq_sliders = []
        
        bands = ["32", "64", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]

        for i, band in enumerate(bands):
            v_layout = QVBoxLayout()
            slider = QSlider(Qt.Vertical)
            slider.valueChanged.connect(lambda val, idx=i: self.eq_changed.emit(idx, val))
            if band == "32":
                slider.setRange(-12, 12)
            else:
                slider.setRange(-15, 15)
            slider.setValue(0)
            slider.setTickPosition(QSlider.TicksBothSides)

            label = QLabel(band)
            label.setAlignment(Qt.AlignCenter)
            
            v_layout.addWidget(slider)
            v_layout.addWidget(label)
            self.eq_layout.addLayout(v_layout)
            self.eq_sliders.append(slider)
            
        self.eq_group.setLayout(self.eq_layout)
        
        self.dyn_group = QGroupBox("Dynamics & Speed")
        self.dyn_layout = QVBoxLayout()
        
        self.speed_layout = QHBoxLayout()
        self.speed_label = QLabel("Speed: 1.0x")
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(0, 300)
        self.speed_slider.setValue(100)
        self.speed_slider.valueChanged.connect(self.on_speed)
        self.speed_layout.addWidget(self.speed_label)
        self.speed_layout.addWidget(self.speed_slider)
        
        self.dyn_layout.addLayout(self.speed_layout)
        self.dyn_group.setLayout(self.dyn_layout)
        
        self.layout.addWidget(self.eq_group, stretch=2)
        self.layout.addWidget(self.dyn_group, stretch=1)

    def on_speed(self):
        val = self.speed_slider.value() / 100.0
        self.speed_label.setText(f"Speed: {val:.2f}x")
        self.speed_changed.emit(val)
