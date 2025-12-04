import os
import numpy as np
import soundfile as sf
from pydub import AudioSegment
from pedalboard import Pedalboard

class Exporter:
    def __init__(self, output_dir="exports"):
        self.output_dir = output_dir
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

    def export_track(self, audio_data, sample_rate, output_filename, speed, eq_board, limiter_board):
        try:
            data = audio_data
            sr = sample_rate
            
            if abs(speed - 1.0) > 0.01:
                new_len = int(len(data) / speed)
                indices = np.linspace(0, len(data) - 1, new_len)
                resampled_data = np.zeros((new_len, data.shape[1]), dtype=np.float32)
                for i in range(data.shape[1]):
                    resampled_data[:, i] = np.interp(indices, np.arange(len(data)), data[:, i])
                data = resampled_data

            data_T = data.T
            data_T = eq_board.process(data_T, sample_rate=sr)
            data_T = limiter_board.process(data_T, sample_rate=sr)
            
            temp_wav = os.path.join(self.output_dir, "temp_export.wav")
            sf.write(temp_wav, data_T.T, sr)
            
            output_path = os.path.join(self.output_dir, output_filename)
            audio = AudioSegment.from_wav(temp_wav)
            audio.export(output_path, format="mp3", bitrate="320k")
            
            if os.path.exists(temp_wav):
                os.remove(temp_wav)
            
            return output_path
            
        except Exception as e:
            print(f"Export error: {e}")
            return None
