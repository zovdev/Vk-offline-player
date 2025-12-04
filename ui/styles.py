DARK_THEME = """
QMainWindow {
    background-color: #1e1e1e;
    color: #ffffff;
}

QWidget {
    background-color: #1e1e1e;
    color: #ffffff;
    font-family: 'Segoe UI', sans-serif;
    font-size: 14px;
}

QListWidget {
    background-color: #252526;
    border: none;
    outline: none;
}

QListWidget::item {
    padding: 10px;
    border-bottom: 1px solid #333;
}

QListWidget::item:selected {
    background-color: #37373d;
    color: #ffffff;
}

QListWidget::item:hover {
    background-color: #2a2d2e;
}

QPushButton {
    background-color: #007acc;
    color: white;
    border: none;
    padding: 8px 16px;
    border-radius: 4px;
}

QPushButton:hover {
    background-color: #0098ff;
}

QPushButton:pressed {
    background-color: #005c99;
}

QSlider::groove:horizontal {
    border: 1px solid #333;
    height: 6px;
    background: #333;
    margin: 2px 0;
    border-radius: 3px;
}

QSlider::handle:horizontal {
    background: #007acc;
    border: 1px solid #007acc;
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}

QSlider::groove:vertical {
    border: 1px solid #333;
    width: 6px;
    background: #333;
    margin: 0 2px;
    border-radius: 3px;
}

QSlider::handle:vertical {
    background: #007acc;
    border: 1px solid #007acc;
    height: 14px;
    width: 14px;
    margin: 0 -5px;
    border-radius: 7px;
}

QLineEdit {
    background-color: #3c3c3c;
    border: 1px solid #3c3c3c;
    color: #cccccc;
    padding: 5px;
    border-radius: 2px;
}

QLineEdit:focus {
    border: 1px solid #007acc;
}

QLabel {
    color: #cccccc;
}

QGroupBox {
    border: 1px solid #333;
    border-radius: 5px;
    margin-top: 10px;
    padding-top: 10px;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 3px;
    color: #007acc;
}
"""
