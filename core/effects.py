from pedalboard import Pedalboard, PeakFilter, LowShelfFilter, HighShelfFilter, Compressor

class Equalizer:
    def __init__(self, sample_rate=44100):
        self.sample_rate = sample_rate
        self.bands = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
        self.gains = [0.0] * len(self.bands)
        
        filters = []
        for i, freq in enumerate(self.bands):
            if i == 0:
                f = LowShelfFilter(cutoff_frequency_hz=freq, gain_db=0.0, q=0.707)
            elif i == len(self.bands) - 1:
                f = HighShelfFilter(cutoff_frequency_hz=freq, gain_db=0.0, q=0.707)
            else:
                f = PeakFilter(cutoff_frequency_hz=freq, gain_db=0.0, q=1.0)
            filters.append(f)
            
        self.board = Pedalboard(filters)

    def set_gain(self, band_index, gain_db):
        if 0 <= band_index < len(self.gains):
            self.gains[band_index] = gain_db
            self.board[band_index].gain_db = gain_db

    def process(self, data):
        input_data = data.T
        output_data = self.board.process(input_data, sample_rate=self.sample_rate, reset=False)
        return output_data.T

class MultiBandLimiter:
    def __init__(self, sample_rate=44100):
        self.sample_rate = sample_rate
        self.compressor = Compressor(threshold_db=-12.0, ratio=1.5, attack_ms=10, release_ms=1000)
        self.board = Pedalboard([self.compressor])

    def set_params(self, threshold_db, knee_width_db, ratio, attack_time, release_time):
        pass

    def process(self, data):
        input_data = data.T
        output_data = self.board.process(input_data, sample_rate=self.sample_rate, reset=False)
        return output_data.T
