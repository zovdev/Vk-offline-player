DARK_THEME = """
QMainWindow {
    background-color: #1e1e1e;
    color: #ffffff;
}

QWidget {
    background-color: #1e1e1e;
    color: #ffffff;
    font-family: 'Segoe UI', sans-serif;
    font-size: 12px;
}

QListWidget {
    background-color: #252526;
    border: none;
    outline: none;
}

QListWidget::item {
    padding: 3px 5px;
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
    padding: 4px 10px;
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
    height: 4px;
    background: #333;
    margin: 2px 0;
    border-radius: 2px;
}

QSlider::handle:horizontal {
    background: #007acc;
    border: 1px solid #007acc;
    width: 12px;
    height: 12px;
    margin: -5px 0;
    border-radius: 6px;
}

QSlider::groove:vertical {
    border: 1px solid #333;
    width: 4px;
    background: #333;
    margin: 0 2px;
    border-radius: 2px;
}

QSlider::handle:vertical {
    background: #007acc;
    border: 1px solid #007acc;
    height: 12px;
    width: 12px;
    margin: 0 -5px;
    border-radius: 6px;
}

QLineEdit {
    background-color: #3c3c3c;
    border: 1px solid #3c3c3c;
    color: #cccccc;
    padding: 3px;
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
    margin-top: 8px;
    padding-top: 8px;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 3px;
    color: #007acc;
}
"""
