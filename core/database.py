import sqlite3
import os
import json

class Database:
    def __init__(self, db_path="player_data.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS settings
                     (key TEXT PRIMARY KEY, value TEXT)''')
                     
        c.execute("PRAGMA table_info(tracks)")
        columns = [info[1] for info in c.fetchall()]
        
        needs_migration = False
        if 'owner_id' in columns or 'duration' in columns or 'local_path' in columns:
            needs_migration = True
            
        if needs_migration:
            c.execute('''CREATE TABLE IF NOT EXISTS tracks_new
                         (id TEXT PRIMARY KEY, 
                          artist TEXT, 
                          title TEXT, 
                          url TEXT, 
                          audio_blob BLOB,
                          image_blob BLOB)''')
            
            has_blobs = 'audio_blob' in columns
            
            if has_blobs:
                c.execute('''INSERT INTO tracks_new (id, artist, title, url, audio_blob, image_blob)
                             SELECT id, artist, title, url, audio_blob, image_blob FROM tracks''')
            else:
                c.execute('''INSERT INTO tracks_new (id, artist, title, url)
                             SELECT id, artist, title, url FROM tracks''')
            
            c.execute("DROP TABLE tracks")
            c.execute("ALTER TABLE tracks_new RENAME TO tracks")
            
        else:
            c.execute('''CREATE TABLE IF NOT EXISTS tracks
                         (id TEXT PRIMARY KEY, 
                          artist TEXT, 
                          title TEXT, 
                          url TEXT, 
                          audio_blob BLOB,
                          image_blob BLOB)''')

        c.execute('''CREATE TABLE IF NOT EXISTS deleted_tracks (id TEXT PRIMARY KEY)''')
        conn.commit()
        conn.close()

    def set_setting(self, key, value):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        conn.close()

    def get_setting(self, key, default=None):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else default

    def save_tracks(self, tracks):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        for t in tracks:
            c.execute("SELECT id FROM tracks WHERE id=?", (str(t['id']),))
            exists = c.fetchone()
            
            if exists:
                c.execute('''UPDATE tracks SET 
                             artist=?, title=?, url=?
                             WHERE id=?''',
                          (t['artist'], t['title'], t['url'], str(t['id'])))
            else:
                c.execute('''INSERT INTO tracks 
                             (id, artist, title, url) 
                             VALUES (?, ?, ?, ?)''',
                          (str(t['id']), t['artist'], t['title'], t['url']))
        
        conn.commit()
        conn.close()

    def get_tracks(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT id, artist, title, url FROM tracks")
        rows = c.fetchall()
        conn.close()
        
        tracks = []
        for row in rows:
            tracks.append(dict(row))
        return tracks

    def save_track_audio(self, track_id, audio_data):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("UPDATE tracks SET audio_blob=? WHERE id=?", (audio_data, str(track_id)))
        conn.commit()
        conn.close()

    def get_track_audio(self, track_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT audio_blob FROM tracks WHERE id=?", (str(track_id),))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    def save_track_image(self, track_id, image_data):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("UPDATE tracks SET image_blob=? WHERE id=?", (image_data, str(track_id)))
        conn.commit()
        conn.close()

    def get_track_image(self, track_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT image_blob FROM tracks WHERE id=?", (str(track_id),))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    def mark_track_deleted(self, track_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO deleted_tracks (id) VALUES (?)", (str(track_id),))
        c.execute("DELETE FROM tracks WHERE id=?", (str(track_id),))
        conn.commit()
        conn.close()

    def is_track_deleted(self, track_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT 1 FROM deleted_tracks WHERE id=?", (str(track_id),))
        row = c.fetchone()
        conn.close()
        return row is not None
