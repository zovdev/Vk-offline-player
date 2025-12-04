import sounddevice as sd
import soundfile as sf
import numpy as np
import threading
import queue
import os
from .effects import Equalizer, MultiBandLimiter

class AudioEngine:
    def __init__(self):
        self.stream = None
        self.data = None
        self.samplerate = 44100
        self.position = 0.0
        self.playing = False
        self.volume = 1.0
        
        self.eq = Equalizer()
        self.limiter = MultiBandLimiter()
        
        self.speed = 1.0
        
        self.lock = threading.Lock()
        
    def load_track(self, file_path=None, file_data=None):
        with self.lock:
            self.stop()
            print(f"AudioEngine loading track...")
            
            try:
                if file_data:
                    import io
                    if isinstance(file_data, bytes):
                        file_data = io.BytesIO(file_data)
                    data, fs = sf.read(file_data, always_2d=True)
                elif file_path:
                    if not os.path.exists(file_path):
                        print(f"Error: File does not exist at {file_path}")
                        return False
                    data, fs = sf.read(file_path, always_2d=True)
                else:
                    return False
                    
                self.data = data.astype(np.float32)
                self.samplerate = fs
                self.position = 0.0
                self.eq.sample_rate = fs
                self.limiter.sample_rate = fs
                return True
            except Exception as e:
                print(f"Error loading track: {e}")
                return False

    def play(self):
        if self.data is None:
            return
        
        if self.stream is None:
            self.stream = sd.OutputStream(
                samplerate=self.samplerate,
                channels=self.data.shape[1],
                callback=self._callback,
                blocksize=2048
            )
            self.stream.start()
        
        self.playing = True

    def pause(self):
        self.playing = False

    def stop(self):
        self.playing = False
        self.position = 0.0
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def seek(self, position_seconds):
        if self.data is not None:
            sample_pos = position_seconds * self.samplerate
            self.position = max(0.0, min(sample_pos, float(len(self.data))))

    def set_volume(self, volume):
        self.volume = max(0.0, min(1.0, volume))

    def set_speed(self, speed):
        if speed >= 0.0:
            self.speed = speed

    def _callback(self, outdata, frames, time, status):
        if status:
            print(status)
            
        if not self.playing or self.data is None:
            outdata.fill(0)
            return

        indices = self.position + np.arange(frames) * self.speed
        
        max_idx = len(self.data) - 2
        
        if indices[0] > max_idx:
            outdata.fill(0)
            self.playing = False
            return
            
        valid_mask = indices <= max_idx
        valid_indices = indices[valid_mask]
        
        if len(valid_indices) == 0:
            outdata.fill(0)
            self.playing = False
            return

        idx_floor = valid_indices.astype(int)
        idx_ceil = idx_floor + 1
        alpha = (valid_indices - idx_floor)[:, np.newaxis]
        
        sample0 = self.data[idx_floor]
        sample1 = self.data[idx_ceil]
        
        interpolated = sample0 * (1.0 - alpha) + sample1 * alpha
        
        out_len = len(interpolated)
        outdata[:out_len] = interpolated
        if out_len < frames:
            outdata[out_len:] = 0
            self.playing = False
            
        self.position += frames * self.speed
        
        outdata[:] = self.eq.process(outdata)
        
        outdata[:] = self.limiter.process(outdata)
        
        outdata *= self.volume
