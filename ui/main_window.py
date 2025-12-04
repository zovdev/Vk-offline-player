import sys
import os
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QListWidget, QLineEdit, QPushButton, 
                             QMessageBox, QLabel, QMenu, QListWidgetItem)
from PySide6.QtGui import QPixmap, QIcon, QPainter, QBrush, QColor, QPainterPath
from PySide6.QtCore import QTimer, Qt, QSize

from core.audio_engine import AudioEngine
from core.vk_client import VKClient
from core.database import Database
from core.exporter import Exporter
from .styles import DARK_THEME
from .player_controls import PlayerControls
from .effects_panel import EffectsPanel

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VK Offline Player")
        self.resize(1000, 700)
        
        self.db = Database()
        self.audio_engine = AudioEngine()
        self.vk_client = VKClient()
        self.exporter = Exporter()
        self.tracks = []
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.main_layout = QVBoxLayout(central_widget)
        
        self.auth_layout = QHBoxLayout()
        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText("Enter VK Access Token or User Link (requires token for API)")
        self.auth_btn = QPushButton("Load Tracks")
        self.auth_btn.clicked.connect(self.authenticate)
        
        self.auth_layout.addWidget(self.token_input)
        self.auth_layout.addWidget(self.auth_btn)
        
        self.content_layout = QHBoxLayout()
        
        self.playlist = QListWidget()
        self.playlist.itemDoubleClicked.connect(self.play_track)
        self.playlist.setContextMenuPolicy(Qt.CustomContextMenu)
        self.playlist.customContextMenuRequested.connect(self.show_playlist_context_menu)
        
        self.info_layout = QVBoxLayout()
        
        self.art_label = QLabel()
        self.art_label.setFixedSize(300, 300)
        self.art_label.setStyleSheet("background-color: #222; border-radius: 10px;")
        self.art_label.setAlignment(Qt.AlignCenter)
        
        self.info_label = QLabel("Select a track to play")
        self.info_label.setAlignment(Qt.AlignCenter)
        self.info_label.setStyleSheet("font-size: 18px; color: #888; font-weight: bold;")
        self.info_label.setWordWrap(True)
        
        self.export_btn = QPushButton("Export Processed Track")
        self.export_btn.clicked.connect(self.export_current_track)
        self.export_btn.setEnabled(False)
        
        self.info_layout.addWidget(self.art_label, alignment=Qt.AlignCenter)
        self.info_layout.addWidget(self.info_label)
        self.info_layout.addWidget(self.export_btn)
        self.info_layout.addStretch()
        
        self.content_layout.addWidget(self.playlist, stretch=1)
        self.content_layout.addLayout(self.info_layout, stretch=1)
        
        self.effects_panel = EffectsPanel()
        self.effects_panel.eq_changed.connect(self.update_eq)
        self.effects_panel.speed_changed.connect(self.update_speed)

        self.controls = PlayerControls()
        self.controls.play_clicked.connect(self.audio_engine.play)
        self.controls.pause_clicked.connect(self.audio_engine.pause)
        self.controls.seek_changed.connect(self.seek)
        self.controls.volume_changed.connect(self.audio_engine.set_volume)
        
        self.main_layout.addLayout(self.auth_layout)
        self.main_layout.addLayout(self.content_layout, stretch=1)
        self.main_layout.addWidget(self.effects_panel)
        self.main_layout.addWidget(self.controls)
        
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_ui)
        self.timer.start(100)
        
        self.setStyleSheet(DARK_THEME)
        
        self.load_state()

    def load_state(self):
        token = self.db.get_setting("access_token")
        if token:
            self.token_input.setText(token)
            self.vk_client.access_token = token
            
        saved_tracks = self.db.get_tracks()
        if saved_tracks:
            self.tracks = saved_tracks
            self.refresh_playlist()

    def authenticate(self):
        token = self.token_input.text().strip()
        if not token:
            QMessageBox.warning(self, "Error", "Please enter a token")
            return
            
        if self.vk_client.authenticate(token):
            self.db.set_setting("access_token", token)
            self.load_tracks_from_api()
        else:
            QMessageBox.critical(self, "Error", "Authentication failed")

    def load_tracks_from_api(self):
        new_tracks = self.vk_client.get_audio()
        if new_tracks:
            filtered_tracks = []
            for t in new_tracks:
                if not self.db.is_track_deleted(t['id']):
                    filtered_tracks.append(t)
            
            self.tracks = filtered_tracks
            self.db.save_tracks(self.tracks)
            self.refresh_playlist()
        else:
            QMessageBox.information(self, "Info", "No tracks found or access denied")

    def get_rounded_pixmap(self, pixmap, size=48, radius=24):
        scaled = pixmap.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        
        if scaled.width() > size or scaled.height() > size:
            x = (scaled.width() - size) // 2
            y = (scaled.height() - size) // 2
            scaled = scaled.copy(x, y, size, size)
            
        rounded = QPixmap(size, size)
        rounded.fill(Qt.transparent)
        
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.Antialiasing)
        
        path = QPainterPath()
        path.addRoundedRect(0, 0, size, size, radius, radius)
        
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, scaled)
        painter.end()
        
        return rounded

    def get_placeholder_pixmap(self, size=48):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        painter.setBrush(QBrush(QColor("#444")))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(0, 0, size, size)
        
        painter.setPen(QColor("#888"))
        painter.setBrush(Qt.NoBrush)
        font = painter.font()
        font.setPixelSize(int(size * 0.6))
        painter.setFont(font)
        painter.drawText(0, 0, size, size, Qt.AlignCenter, "♪")
        
        painter.end()
        return pixmap

    def refresh_playlist(self):
        self.playlist.clear()
        self.playlist.setIconSize(QSize(48, 48))
        
        for track in self.tracks:
            title = f"{track['artist']} - {track['title']}"
            item = QListWidgetItem(title)
            
            image_data = self.db.get_track_image(track['id'])
            if image_data:
                pixmap = QPixmap()
                pixmap.loadFromData(image_data)
                icon_pixmap = self.get_rounded_pixmap(pixmap)
                item.setIcon(QIcon(icon_pixmap))
            else:
                item.setIcon(QIcon(self.get_placeholder_pixmap()))
                
            self.playlist.addItem(item)

    def show_playlist_context_menu(self, position):
        menu = QMenu()
        delete_action = menu.addAction("Delete")
        action = menu.exec(self.playlist.mapToGlobal(position))
        if action == delete_action:
            self.delete_selected_track()

    def delete_selected_track(self):
        items = self.playlist.selectedItems()
        if not items:
            return
        
        row = self.playlist.row(items[0])
        track = self.tracks[row]
        
        confirm = QMessageBox.question(self, "Delete Track", 
                                     f"Are you sure you want to delete '{track['title']}'?\nIt will not appear again.",
                                     QMessageBox.Yes | QMessageBox.No)
        
        if confirm == QMessageBox.Yes:
            self.db.mark_track_deleted(track['id'])
            self.tracks.pop(row)
            self.refresh_playlist()

    def play_track(self, item):
        idx = self.playlist.row(item)
        track = self.tracks[idx]
        
        audio_data = self.db.get_track_audio(track['id'])
        
        if not audio_data:
            self.info_label.setText(f"Downloading {track['title']}...")
            QApplication.processEvents()
            
            audio_data = self.vk_client.download_track(track['url'])
            if audio_data:
                self.db.save_track_audio(track['id'], audio_data)
            else:
                QMessageBox.warning(self, "Error", "Failed to download track")
                return

        image_data = self.db.get_track_image(track['id'])
        if not image_data and track.get('image_url'):
            image_data = self.vk_client.download_image(track['image_url'])
            if image_data:
                self.db.save_track_image(track['id'], image_data)
                self.refresh_playlist()
        
        if image_data:
            pixmap = QPixmap()
            pixmap.loadFromData(image_data)
            self.art_label.setPixmap(pixmap.scaled(300, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.art_label.clear()
            self.art_label.setText("No Art")

        if audio_data and self.audio_engine.load_track(file_data=audio_data):
            self.audio_engine.play()
            self.controls.set_playing(True)
            self.info_label.setText(f"{track['artist']}\n{track['title']}")
            self.export_btn.setEnabled(True)
        else:
            QMessageBox.warning(self, "Error", "Failed to load track audio")

    def seek(self, position_percent):
        if self.audio_engine.data is not None:
            total_seconds = len(self.audio_engine.data) / self.audio_engine.samplerate
            seek_seconds = position_percent * total_seconds
            self.audio_engine.seek(seek_seconds)

    def update_ui(self):
        if self.audio_engine.playing and self.audio_engine.data is not None:
            pos = self.audio_engine.position / self.audio_engine.samplerate
            duration = len(self.audio_engine.data) / self.audio_engine.samplerate
            if duration > 0:
                self.controls.update_seek(pos / duration)
        elif not self.audio_engine.playing and self.controls.is_playing:
             self.controls.set_playing(False)

    def update_eq(self, band, gain):
        self.audio_engine.eq.set_gain(band, gain)

    def update_speed(self, factor):
        self.audio_engine.set_speed(factor)

    def export_current_track(self):
        if self.audio_engine.data is None:
            return
            
        items = self.playlist.selectedItems()
        if not items:
            QMessageBox.warning(self, "Export", "No track selected")
            return
            
        idx = self.playlist.row(items[0])
        track = self.tracks[idx]
        
        safe_title = f"{track['artist']} - {track['title']}".replace("/", "_").replace("\\", "_")
        output_filename = f"{safe_title}_processed.mp3"
        
        self.export_btn.setText("Exporting...")
        self.export_btn.setEnabled(False)
        QApplication.processEvents()
        
        out_path = self.exporter.export_track(
            self.audio_engine.data,
            self.audio_engine.samplerate,
            output_filename,
            self.audio_engine.speed,
            self.audio_engine.eq.board,
            self.audio_engine.limiter.board
        )
        
        self.export_btn.setText("Export Processed Track")
        self.export_btn.setEnabled(True)
        
        if out_path:
            QMessageBox.information(self, "Export", f"Exported to:\n{out_path}")
        else:
            QMessageBox.critical(self, "Export", "Export failed")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
