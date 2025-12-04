from ui.main_window import MainWindow, QApplication
import sys
import os

if __name__ == "__main__":
    __file__ = sys.argv[0]
    os.chdir(__file__[:__file__.replace("\\", "/").rindex("/")])

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
