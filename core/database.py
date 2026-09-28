import sqlite3
import threading


class Database:
    def __init__(self):
        self.db = sqlite3.connect("player_data.db", check_same_thread=False)
        self._lock = threading.RLock()

        with self._lock:
            self.db.execute("""CREATE TABLE IF NOT EXISTS settings(
                auth_type INT,
                access_token TEXT,
                login TEXT,
                password TEXT,
                refresh_token TEXT,
                app_id TEXT,
                device_id TEXT
            )""")

            # старая база обновляется на месте: колонки токен-рефреша
            # добавляются к уже существующей таблице settings
            for col in ("refresh_token", "app_id", "device_id"):
                try:
                    self.db.execute(f"ALTER TABLE settings ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass  # колонка уже есть

            self.db.execute("""CREATE TABLE IF NOT EXISTS tracks(
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INT UNIQUE,
                artist TEXT,
                title TEXT,
                audio_url TEXT,
                image_url TEXT,
                audio_blob BLOB,
                image_blob BLOB
            )""")

            # скрытое состояние интерфейса: ползунки EQ, скорость, режим
            # динамики, громкость, рандомизация — key/value, переживает
            # перезапуск плеера
            self.db.execute("""CREATE TABLE IF NOT EXISTS ui_state(
                key TEXT PRIMARY KEY,
                value TEXT
            )""")

            self.db.execute("""INSERT INTO settings (auth_type, access_token, login, password) SELECT ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM settings)""", (0, "", "", ""))

            self.db.row_factory = sqlite3.Row

            self.db.commit()

    def get_settings(self):
        with self._lock:
            cur = self.db.execute("SELECT * FROM settings")
            return cur.fetchone()

    def set_settings(self, key, value):
        with self._lock:
            self.db.execute(f"UPDATE settings SET {key} = ?", (value,))
            self.db.commit()

    def get_ui_state(self):

        with self._lock:
            cur = self.db.execute("SELECT key, value FROM ui_state")
            return {r["key"]: r["value"] for r in cur.fetchall()}

    def set_ui_state(self, key, value):

        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO ui_state(key, value) VALUES(?, ?)",
                (key, str(value)))
            self.db.commit()

    def get_audio(self, count=100, offset=0):
        with self._lock:
            cur = self.db.execute("SELECT track_id, artist, title, audio_url, image_url FROM tracks ORDER BY row_id DESC LIMIT ? OFFSET ?", (count, offset))
            return cur.fetchall()

    def get_audio_by_id(self, track_id):
        with self._lock:
            cur = self.db.execute("SELECT * FROM tracks WHERE track_id = ?", (track_id,))
            return cur.fetchone()

    def save_audio(self, value):
        with self._lock:
            self.db.executemany("""
                INSERT OR IGNORE INTO tracks(track_id, artist, title, audio_url, image_url)
                VALUES (:track_id, :artist, :title, :audio_url, :image_url)
            """, value)
            self.db.commit()

    def save_audio_data(self, track_id, value):
        with self._lock:
            self.db.execute("UPDATE tracks SET audio_blob = ? WHERE track_id = ?", (value, track_id))
            self.db.commit()

    def save_audio_image(self, track_id, value):
        with self._lock:
            self.db.execute("UPDATE tracks SET image_blob = ? WHERE track_id = ?", (value, track_id))
            self.db.commit()
