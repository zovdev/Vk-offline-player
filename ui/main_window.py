import sys
import os
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QListWidget, QLineEdit, QPushButton, 
                             QMessageBox, QLabel, QMenu, QListWidgetItem, QSizePolicy)
from PySide6.QtGui import QPixmap, QIcon, QPainter, QBrush, QColor, QPainterPath, QResizeEvent
from PySide6.QtCore import QTimer, Qt, QSize, QThread, Signal

from core.audio_engine import AudioEngine
from core.vk_client import VKClient
from core.database import Database
from core.exporter import Exporter
from .styles import DARK_THEME
from .player_controls import PlayerControls
from .player_controls import PlayerControls
from .effects_panel import EffectsPanel

class DownloadWorker(QThread):
    finished = Signal(object)
    
    def __init__(self, func, *args):
        super().__init__()
        self.func = func
        self.args = args
        
    def run(self):
        result = self.func(*self.args)
        self.finished.emit(result)

class TrackLoaderWorker(QThread):
    finished = Signal(object)
    
    def __init__(self, vk_client, token):
        super().__init__()
        self.vk_client = vk_client
        self.token = token
        
    def run(self):
        # We need to authenticate in the thread context or just use the client if it's thread-safe enough for requests
        # VKClient just does requests, so it should be fine.
        # But we need to auth first.
        success = self.vk_client.authenticate(self.token)
        if success:
            tracks = self.vk_client.get_audio()
            self.finished.emit(tracks)
        else:
            self.finished.emit(None)

class ImageLoaderWorker(QThread):
    progress = Signal(str, bytes) # track_id, image_data
    
    def __init__(self, tracks, db, vk_client):
        super().__init__()
        self.tracks = tracks
        self.db = db
        self.vk_client = vk_client
        self.running = True
        
    def run(self):
        # Create a local DB instance for thread safety if needed, 
        # but our DB class creates connection per call, so it's SAFE.
        for track in self.tracks:
            if not self.running: break
            
            track_id = track['id']
            # 1. Check DB
            image_data = self.db.get_track_image(track_id)
            
            # 2. If not, download
            if not image_data and track.get('image_url'):
                image_data = self.vk_client.download_image(track['image_url'])
                if image_data:
                    self.db.save_track_image(track_id, image_data)
                    
            if image_data:
                self.progress.emit(str(track_id), image_data)
                
    def stop(self):
        self.running = False
        self.wait()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VK Offline Player")
        self.resize(800, 600)
        
        self.db = Database()
        self.audio_engine = AudioEngine()
        self.vk_client = VKClient()
        self.exporter = Exporter()
        self.tracks = []
        
        self.exporter = Exporter()
        self.tracks = []
        
        self.image_loader = None
        self.current_art_pixmap = None
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.main_layout = QVBoxLayout(central_widget)
        self.main_layout.setContentsMargins(5, 5, 5, 5)
        self.main_layout.setSpacing(5)
        
        self.auth_layout = QHBoxLayout()
        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText("Enter VK Access Token or User Link")
        self.auth_btn = QPushButton("Load Tracks")
        self.auth_btn.clicked.connect(self.start_load_tracks)
        
        self.auth_layout.addWidget(self.token_input)
        self.auth_layout.addWidget(self.auth_btn)
        
        self.content_layout = QHBoxLayout()
        
        self.playlist = QListWidget()
        self.playlist.itemDoubleClicked.connect(self.play_track)
        self.playlist.setContextMenuPolicy(Qt.CustomContextMenu)
        self.playlist.customContextMenuRequested.connect(self.show_playlist_context_menu)
        
        self.info_layout = QVBoxLayout()
        
        self.info_layout = QVBoxLayout()
        
        self.art_label = QLabel()
        self.art_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.art_label.setMinimumSize(100, 100)
        self.art_label.setStyleSheet("background-color: #222; border-radius: 8px;")
        self.art_label.setAlignment(Qt.AlignCenter)
        
        self.info_label = QLabel("Select a track to play")
        self.info_label.setAlignment(Qt.AlignCenter)
        self.info_label.setStyleSheet("font-size: 14px; color: #888; font-weight: bold;")
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
        
        # Container for main content (Playlist + Big Art + Info)
        self.content_container = QWidget()
        self.content_container.setLayout(self.content_layout)
        
        self.effects_panel = EffectsPanel()
        self.effects_panel.eq_changed.connect(self.update_eq)
        self.effects_panel.speed_changed.connect(self.update_speed)

        self.controls = PlayerControls()
        self.controls.play_clicked.connect(self.audio_engine.play)
        self.controls.pause_clicked.connect(self.audio_engine.pause)
        self.controls.seek_changed.connect(self.seek)
        self.controls.volume_changed.connect(self.audio_engine.set_volume)
        self.controls.prev_clicked.connect(self.play_previous_track)
        self.controls.next_clicked.connect(self.play_next_track)
        
        self.main_layout.addLayout(self.auth_layout)
        self.main_layout.addWidget(self.content_container, stretch=1)
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


            
    def start_load_tracks(self):
        token = self.token_input.text().strip()
        if not token:
            QMessageBox.warning(self, "Error", "Please enter a token")
            return
            
        self.auth_btn.setEnabled(False)
        self.auth_btn.setText("Loading...")
        self.info_label.setText("Loading tracks from VK...")
        
        self.track_loader = TrackLoaderWorker(self.vk_client, token)
        self.track_loader.finished.connect(self.on_tracks_loaded)
        self.track_loader.start()

    def on_tracks_loaded(self, new_tracks):
        self.auth_btn.setEnabled(True)
        self.auth_btn.setText("Load Tracks")
        self.info_label.setText("Select a track to play")
        
        if new_tracks is not None:
            token = self.token_input.text().strip()
            self.db.set_setting("access_token", token)
            
            filtered_tracks = []
            for t in new_tracks:
                if not self.db.is_track_deleted(t['id']):
                    filtered_tracks.append(t)
            
            self.tracks = filtered_tracks
            self.db.save_tracks(self.tracks)
            self.refresh_playlist()
        else:
             QMessageBox.critical(self, "Error", "Authentication or network error")

    # Removed old authenticate/load_tracks_from_api methods in favor of threaded one

    def get_rounded_pixmap(self, pixmap, size=32, radius=4):
        # Reduced radius for list view as requested
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
        
    def get_main_art_pixmap(self, pixmap, size=200):
        # Round the main art dynamically
        radius = 12 
        
        # Scale keeping aspect ratio
        scaled = pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        
        # Create transparent canvas of target size (or scaled size)
        # We want it to be square? No, fit available space.
        # But for the label, we usually want it centered.
        
        # Let's make the result pixmap exactly 'size' x 'size' for consistency, or just the size of scaled?
        # If we use keepAspectRatio, one dim might be smaller.
        
        final_w = scaled.width()
        final_h = scaled.height()
        
        rounded = QPixmap(final_w, final_h)
        rounded.fill(Qt.transparent)
        
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.Antialiasing)
        
        path = QPainterPath()
        path.addRoundedRect(0, 0, final_w, final_h, radius, radius)
        
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, scaled)
        painter.end()
        
        return rounded

    def get_placeholder_pixmap(self, size=32):
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
        self.playlist.setIconSize(QSize(32, 32))
        
        # Optimization: Don't load images here to prevent slow startup
        # We use a placeholder and could load images lazily if needed
        placeholder = QIcon(self.get_placeholder_pixmap())
        
        for track in self.tracks:
            title = f"{track['artist']} - {track['title']}"
            item = QListWidgetItem(title)
            item.setIcon(placeholder)
            # Store ID in user role form easy access
            item.setData(Qt.UserRole, track['id'])
            self.playlist.addItem(item)
            
        # Start background image loading
        if self.image_loader:
            self.image_loader.stop()
            
        self.image_loader = ImageLoaderWorker(self.tracks, self.db, self.vk_client)
        self.image_loader.progress.connect(self.on_image_loaded)
        self.image_loader.start()

    def on_image_loaded(self, track_id, image_data):
        # Find item with this track_id and update icon
        # This is O(N) but happens gradually. For efficient O(1), we'd need a map.
        # Given list size < 2000 usually, it's okay'ish.
        # But we can assume order matches self.tracks? Not always if filtered.
        # Let's iterate visual items.
        
        # Optimization: cache items by ID map?
        # For now, linear search on QListWidget (limit iteration count if needed)
        # Actually, let's just do finds.
        
        for i in range(self.playlist.count()):
            item = self.playlist.item(i)
            # data is stored as string/int, verify type
            tid = item.data(Qt.UserRole)
            if str(tid) == str(track_id):
                pixmap = QPixmap()
                pixmap.loadFromData(image_data)
                item.setIcon(QIcon(self.get_rounded_pixmap(pixmap)))
                # If this is the currently playing track, update main art too if needed?
                # No, play_track handles that logic.
                break

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
        
        # Audio
        audio_data = self.db.get_track_audio(track['id'])
        if audio_data:
            self._finish_play_track(track, audio_data, idx)
        else:
            self.info_label.setText(f"Downloading {track['title']}...")
            self.playlist.setEnabled(False) # Prevent clicks while downloading
            
            self.download_worker = DownloadWorker(self.vk_client.download_track, track['url'])
            self.download_worker.finished.connect(lambda data: self._handle_download_finished(data, track, idx))
            self.download_worker.start()

    def _handle_download_finished(self, audio_data, track, idx):
        self.playlist.setEnabled(True)
        if audio_data:
            self.db.save_track_audio(track['id'], audio_data)
            self._finish_play_track(track, audio_data, idx)
        else:
            self.info_label.setText("Select a track to play")
            QMessageBox.warning(self, "Error", "Failed to download track")

    def _finish_play_track(self, track, audio_data, idx):
         # Image
        image_data = self.db.get_track_image(track['id'])
        if not image_data and track.get('image_url'):
             # We can do this async too, but images are smaller/faster usually. 
             # For now, let's keep it sync or use the worker if it blocks.
             # Given user complaints, let's use worker for image too if it's missing.
             pass
             
        # Update UI without clearing list (Fixes scrolling issue)
        if not image_data and track.get('image_url'):
             # Try download image sync for now (usually fast), or skip to avoid complex chaining
             # User reported freezing on "downloading audio" mostly.
             try:
                 image_data = self.vk_client.download_image(track['image_url'])
                 if image_data:
                     self.db.save_track_image(track['id'], image_data)
                     # Update specific item icon
                     item = self.playlist.item(idx)
                     pixmap = QPixmap()
                     pixmap.loadFromData(image_data)
                     icon_pixmap = self.get_rounded_pixmap(pixmap)
                     item.setIcon(QIcon(icon_pixmap))
             except: pass

        if image_data:
            pixmap = QPixmap()
            pixmap.loadFromData(image_data)
            self.current_art_pixmap = pixmap
            self.update_art_display()
        else:
            self.current_art_pixmap = None
            self.art_label.clear()
            self.art_label.setText("No Art")

        if self.audio_engine.load_track(file_data=audio_data):
            self.audio_engine.play()
            self.controls.set_playing(True)
            self.audio_engine.finished = False
            self.info_label.setText(f"{track['artist']}\n{track['title']}")
            # Also update mini info
            self.controls.update_mini_info(track['title'], track['artist'], self.current_art_pixmap)
            self.export_btn.setEnabled(True)
        else:
            QMessageBox.warning(self, "Error", "Failed to load track audio")

    def seek(self, position_percent):
        if self.audio_engine.data is not None:
            total_seconds = len(self.audio_engine.data) / self.audio_engine.samplerate
            seek_seconds = position_percent * total_seconds
            self.audio_engine.seek(seek_seconds)

    def update_ui(self):
        # Auto-play next track
        if self.audio_engine.finished and self.controls.is_playing:
             self.audio_engine.finished = False # Reset flag
             self.play_next_track()
             return

        if self.audio_engine.playing and self.audio_engine.data is not None:
            pos = self.audio_engine.position / self.audio_engine.samplerate
            duration = len(self.audio_engine.data) / self.audio_engine.samplerate
            if duration > 0:
                self.controls.update_seek(pos / duration)
        elif not self.audio_engine.playing and self.controls.is_playing:
             # Just pause update, but check if it was external stop (which we handled above)
             pass

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

    def play_next_track(self):
        if not self.tracks: return
        
        current_row = self.playlist.currentRow()
        next_row = current_row + 1
        if next_row >= len(self.tracks):
            next_row = 0 # Loop to start
            
        self.playlist.setCurrentRow(next_row)
        self.play_track(self.playlist.item(next_row))

    def play_previous_track(self):
        if not self.tracks: return
        
        current_row = self.playlist.currentRow()
        prev_row = current_row - 1
        if prev_row < 0:
            prev_row = len(self.tracks) - 1 # Loop to end
            
        self.playlist.setCurrentRow(prev_row)
        self.play_track(self.playlist.item(prev_row))

    def resizeEvent(self, event: QResizeEvent):
        width = event.size().width()
        is_mini = width < 500
        
        # In Single-Bar mode:
        # Hide Main Content (Playlist, Art, Info)
        # Hide Effects
        # Hide Auth Bar
        
        self.content_container.setHidden(is_mini)
        self.effects_panel.setHidden(is_mini)
        
        # Hide auth layout items
        for i in range(self.auth_layout.count()):
            w = self.auth_layout.itemAt(i).widget()
            if w: w.setHidden(is_mini)
            
        self.controls.set_mini_mode(is_mini)
        if is_mini:
             # Sync info
             # Need current track title/artist
             track = None
             # Try to get from playing index
             # Or if not playing, selected?
             # Let's rely on stored self.current_track_info if we had one, or derive from playlist.
             
             # Better: when play_track happens, store current metadata text.
             # Or pull from label? Info label has artist\ntitle.
             
             # Let's fetch from selected or playing item
             # This is a bit hacky, but robust enough for resize.
             txt = self.info_label.text().split('\n')
             artist, title = "", ""
             if len(txt) >= 2:
                 artist, title = txt[0], txt[1]
             elif len(txt) == 1:
                 title = txt[0]
                 
             self.controls.update_mini_info(title, artist, self.current_art_pixmap)
        
        # Update art size (only if visible)
        if not is_mini:
            self.update_art_display()
        
        super().resizeEvent(event)

    def update_art_display(self):
        if not self.current_art_pixmap:
            return
            
        # Calculate available size. 
        # If mini mode, use most of width?
        # Or just use the label's size.
        
        # We need to force update geometry sometimes
        size = min(self.art_label.width(), self.art_label.height())
        if size < 64: size = 64 # Min limit
        
        # Actually in layouts, the label might be 0 if hidden?
        # If we rely on label.width() during resize, it might lag.
        # Let's estimate based on window width in mini mode.
        
        if self.playlist.isHidden():
             # Mini mode
             target_size = min(self.width() - 20, self.height() - 100) # Subtract controls height approximate
        else:
             # Normal mode - layout constraints apply, but we want it to look good.
             # The label is in info_layout.
             target_size = 200 # Fallback or dynamic
             if self.art_label.width() > 50:
                 target_size = min(self.art_label.width(), self.art_label.height())

        pixmap = self.get_main_art_pixmap(self.current_art_pixmap, target_size)
        self.art_label.setPixmap(pixmap)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
