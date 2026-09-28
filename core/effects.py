from __future__ import annotations

import math

import numpy as np

try:
    from scipy.signal import butter, sosfilt
except ImportError as _ex:  # pragma: no cover
    raise ImportError(
        "Для core/effects.py требуется scipy (pip install scipy); "
        "без него плеер продолжит работать без эквалайзера"
    ) from _ex

try:  # пакетный импорт (core.effects)
    from .web_audio_compressor import DynamicsCompressor
except ImportError:  # pragma: no cover
    try:
        from core.web_audio_compressor import DynamicsCompressor
    except ImportError:
        from web_audio_compressor import DynamicsCompressor

__all__ = ["MultibandProcessor", "FREQS", "DYNAMICS_MODES"]

#: центры октавных полос ISO (Гц) — порядок как у ползунков EqualizerPanel
FREQS = (31.5, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0)

DYNAMICS_MODES = ("multiband", "output", "off")

#: баллистика пер-банд ограничителей: (attack_ms, release_ms).
#: Атака быстрая (1-3 мс): пик ловится в момент перехода — вместе с
#: look-ahead 6 мс транзиент сглаживается ДО того, как прозвучит.
#: Восстановление медленное (160-300 мс): между ударами баса гейн
#: плавно возвращается — без «дыхания» и пампинга.
_BAND_DYNAMICS = (
    (3.0, 300.0),   # 31.5
    (3.0, 300.0),   # 63
    (3.0, 280.0),   # 125
    (2.5, 250.0),   # 250
    (2.0, 220.0),   # 500
    (2.0, 220.0),   # 1k
    (2.0, 200.0),   # 2k
    (1.5, 180.0),   # 4k
    (1.0, 160.0),   # 8k
    (1.0, 160.0),   # 16k
)

#: Пер-банд динамика = мягкое пик-ограничение У ПОЛНОЙ ШКАЛЫ, как
#: ощущение от Web Audio в Firefox: буст EQ слышен целиком (тело сигнала
#: не трогается), сглаживаются только пики, которым грозил клип.
#: Колено шириной 12 дБ (от -9 до +3 дБ над порогом) — плавный вход
#: без изломов; выше колена ratio 12:1 — практически потолок.
_BAND_THRESHOLD_DB = -3.0    # у самой полной шкалы: ловим только клип
_BAND_KNEE_DB = 12.0         # мягкое колено (-9..+3 дБ): «сглаживание»
_BAND_RATIO = 12.0           # выше колена — почти потолок (лимитер)
_BAND_LOOKAHEAD_MS = 6.0     # look-ahead, как в спецификации Web Audio

_OUT_THRESHOLD_DB = -12.0    # режим "output": мягкий плеерный компрессор,
_OUT_KNEE_DB = 12.0          # ровно прежнее звучание плеера (Firefox-порт)
_OUT_RATIO = 4.0
_OUT_ATTACK_MS = 5.0
_OUT_RELEASE_MS = 300.0

#: Финальная страховка на СУММЕ полос: включается только у самой полной
#: шкалы и сглаживает ровно то, что иначе клипнулось бы на устройстве
#: вывода («превышение 1.0»). Пер-банд ограничители не видят межполосной
#: когерентности (полосы складываются в фазе) — а страховка видит. Всё
#: ниже -5 дБ проходит нетронутым: тело баса не меняется вовсе.
_SAFETY_THRESHOLD_DB = -2.0
_SAFETY_KNEE_DB = 6.0
_SAFETY_RATIO = 20.0
_SAFETY_ATTACK_MS = 1.0
_SAFETY_RELEASE_MS = 150.0
_SAFETY_LOOKAHEAD_MS = 6.0

_GAIN_SMOOTH_MS = 30.0       # сглаживание движения ползунков EQ

_MAX_CHANNELS_HEURISTIC = 8


def _band_edges(freqs=FREQS):
    return [math.sqrt(freqs[i] * freqs[i + 1]) for i in range(len(freqs) - 1)]


def _lr_sections(sr: float, fc: float):
    nyq = 0.5 * sr
    w = min(0.9999, max(1e-4, fc / nyq))
    lp = butter(2, w, btype="low", output="sos")[0]
    hp = butter(2, w, btype="high", output="sos")[0]
    ap = np.array([lp[5], lp[4], 1.0, 1.0, lp[4], lp[5]], dtype=np.float64)
    return lp, hp, ap


class MultibandProcessor:
    def __init__(self, sample_rate: int = 44100, channels: int = 2,
                 mode: str = "multiband"):
        if mode not in DYNAMICS_MODES:
            raise ValueError(f"mode должен быть одним из {DYNAMICS_MODES}")

        self._freqs = FREQS
        self._edges = _band_edges(FREQS)
        self._nbands = len(FREQS)

        self._gains_target = [0.0] * self._nbands
        self._gains_cur = [0.0] * self._nbands

        self._mode = mode
        self._sr = 0
        self._ch = 0
        self._sos: list[np.ndarray] = []
        self._zi: list[np.ndarray] = []
        self._comps: list[DynamicsCompressor] = []
        self._comp_out: DynamicsCompressor | None = None
        self._comp_safety: DynamicsCompressor | None = None
        self._smooth_coef = 0.0

        self.prepare(sample_rate, channels)



    @property
    def sample_rate(self) -> int:
        return int(self._sr)

    @property
    def channels(self) -> int:
        return int(self._ch)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def latency_samples(self) -> int:

        if self._mode == "off":
            return 0                       # чистый EQ: только IIR, без задержки
        if self._mode == "output":
            comp = self._comp_out
            lat = comp.latency_samples if comp is not None else 0
        else:
            # multiband: пер-банд look-ahead, полосы обрабатываются параллельно
            comp0 = self._comps[0] if self._comps else None
            lat = comp0.latency_samples if comp0 is not None else 0
        # финальная страховка добавляет свой look-ahead
        if self._comp_safety is not None:
            lat += self._comp_safety.latency_samples
        return lat

    @property
    def latency_ms(self) -> float:
        return 1000.0 * self.latency_samples / max(1.0, self._sr)

    def prepare(self, sample_rate: int, channels: int) -> None:

        sr = int(sample_rate)
        ch = max(1, int(channels))
        if sr <= 0:
            raise ValueError("sample_rate должен быть > 0")

        self._sr = sr
        self._ch = ch

        # --- SOS-цепочка каждой полосы: HP нижних границ + LP своей + AP верхних
        sections = []
        for k in range(self._nbands):
            rows = []
            for j in range(k):                      # границы НИЖЕ полосы
                _, hp, _ = _lr_sections(sr, self._edges[j])
                rows.extend([hp, hp])               # LR4 = секция x2
            if k < self._nbands - 1:                # своя верхняя граница
                lp, _, _ = _lr_sections(sr, self._edges[k])
                rows.extend([lp, lp])
            for j in range(k + 1, self._nbands - 1):  # компенсация ВЕРХНИХ границ
                _, _, ap = _lr_sections(sr, self._edges[j])
                rows.append(ap)
            sections.append(np.asarray(rows, dtype=np.float64))

        self._sos = sections
        # scipy: zi = (n_sections, ...ведущие оси x..., 2) — состояние ПОСЛЕДНЕЙ осью
        self._zi = [np.zeros((sos.shape[0], ch, 2), dtype=np.float64)
                    for sos in sections]


        self._comps = [
            DynamicsCompressor(
                sr, ch,
                threshold_db=_BAND_THRESHOLD_DB,
                knee_db=_BAND_KNEE_DB,
                ratio=_BAND_RATIO,
                attack_ms=_BAND_DYNAMICS[k][0],
                release_ms=_BAND_DYNAMICS[k][1],
                lookahead_ms=_BAND_LOOKAHEAD_MS,
                makeup_db=0.0,            # громкость не меняем: буст ниже
                                          # порога проходит без изменений
                limiter_ceiling_db=None,  # чистый DynamicsCompressorNode
            )
            for k in range(self._nbands)
        ]

        self._comp_out = DynamicsCompressor(
            sr, ch,
            threshold_db=_OUT_THRESHOLD_DB,
            knee_db=_OUT_KNEE_DB,
            ratio=_OUT_RATIO,
            attack_ms=_OUT_ATTACK_MS,
            release_ms=_OUT_RELEASE_MS,
            limiter_ceiling_db=None,      # без лимитера — как Firefox
        )

        # финальная страховка у полной шкалы (режимы multiband/output)
        self._comp_safety = DynamicsCompressor(
            sr, ch,
            threshold_db=_SAFETY_THRESHOLD_DB,
            knee_db=_SAFETY_KNEE_DB,
            ratio=_SAFETY_RATIO,
            attack_ms=_SAFETY_ATTACK_MS,
            release_ms=_SAFETY_RELEASE_MS,
            lookahead_ms=_SAFETY_LOOKAHEAD_MS,
            makeup_db=0.0,                # ничего не подтягиваем: только
                                          # сглаживаем то, что клипнулось бы
            limiter_ceiling_db=None,
        )

        self._smooth_coef = math.exp(-1.0 / (_GAIN_SMOOTH_MS / 1000.0 * sr))
        self._gains_cur = list(self._gains_target)

    def reset(self) -> None:

        self._zi = [np.zeros_like(z) for z in self._zi]
        for comp in self._comps:
            comp.reset()
        if self._comp_out is not None:
            self._comp_out.reset()
        if self._comp_safety is not None:
            self._comp_safety.reset()
        self._gains_cur = list(self._gains_target)

    def set_eq_band(self, band: int, gain_db: float) -> None:

        if 0 <= band < self._nbands:
            self._gains_target[band] = float(gain_db)

    def set_dynamics_mode(self, mode: str) -> None:
        if mode not in DYNAMICS_MODES:
            raise ValueError(f"mode должен быть одним из {DYNAMICS_MODES}")
        self._mode = mode

    def process(self, block: np.ndarray) -> np.ndarray:

        arr = np.asarray(block)
        if arr.ndim not in (1, 2):
            raise ValueError("Ожидался 1-D (моно) или 2-D блок")
        if arr.size == 0:
            return block

        was_1d = arr.ndim == 1
        transposed = False
        int_info = None
        if np.issubdtype(arr.dtype, np.integer):
            int_info = arr.dtype
            scale = float(1 << (8 * arr.dtype.itemsize - 1))
            x = arr.astype(np.float64) / scale
        else:
            x = arr.astype(np.float64)

        if was_1d:
            x = x.reshape(1, -1)
        else:
            x, transposed = self._orient_2d(x)

        n = x.shape[1]
        ch = x.shape[0]
        if ch != self._ch:
            # каналов больше, чем ждали — пересобираемся на лету
            self.prepare(self._sr, ch)

        # ---------- 1. сплит на 10 полос (IIR, задержки нет) ---------------
        bands = []
        for k in range(self._nbands):
            y, self._zi[k] = sosfilt(self._sos[k], x, axis=-1, zi=self._zi[k])
            bands.append(y)

        # ---------- 2. гейны полос со сглаживанием (анти-зиппер) ----------
        lin = self._gain_curves(n)                 # (nbands, n)
        for k in range(self._nbands):
            bands[k] *= lin[k]

        # ---------- 3. динамика -------------------------------------------
        # Строго Firefox Web Audio API по инструменту: только компрессоры
        # DynamicsCompressorNode с мягким коленом — никаких жёстких клиперов.
        if self._mode == "multiband":
            for k in range(self._nbands):
                bands[k] = self._comps[k].process(bands[k])

        out = bands[0]
        for k in range(1, self._nbands):
            out = out + bands[k]

        if self._mode == "output" and self._comp_out is not None:
            out = self._comp_out.process(out)

        # финальная страховка у полной шкалы: пер-банд ограничители не видят,
        # что соседние полосы складываются в фазе — а она видит. Включается
        # только там, где иначе было бы «превышение 1.0»; тело сигнала
        # ниже -5 дБ не трогает. Режим off — чистый байпас без динамики.
        if self._mode != "off" and self._comp_safety is not None:
            out = self._comp_safety.process(out)

        # ---------- 4. обратно в исходный формат ---------------------------
        if transposed:
            out = out.T
        if int_info is not None:
            sc = float(1 << (8 * int_info.itemsize - 1))
            out = np.clip(out * sc, -sc, sc - 1.0).astype(int_info)
        elif arr.dtype == np.float32:
            out = out.astype(np.float32)
        if was_1d:
            out = out.reshape(-1)
        return out



    def _gain_curves(self, n: int) -> np.ndarray:

        tgt = self._gains_target
        cur = self._gains_cur
        if all(abs(t - c) < 1e-9 for t, c in zip(tgt, cur)):
            g = np.asarray(tgt, dtype=np.float64)[:, None] * np.ones((1, n))
        else:
            c = self._smooth_coef
            pw = c ** np.arange(1.0, n + 1.0)              # (n,)
            t_arr = np.asarray(tgt, dtype=np.float64)[:, None]
            c_arr = np.asarray(cur, dtype=np.float64)[:, None]
            g = t_arr + (c_arr - t_arr) * pw[None, :]      # (nbands, n)
            tail = c ** float(n)
            self._gains_cur = [t + (c0 - t) * tail
                               for t, c0 in zip(tgt, cur)]
        return np.power(10.0, g / 20.0)

    def _orient_2d(self, x: np.ndarray):

        r, c = x.shape
        dc = self._ch
        if r == dc and c == dc:
            return x, False
        if r == dc:
            return x, False
        if c == dc:
            return x.T.copy(), True
        if r <= _MAX_CHANNELS_HEURISTIC < c:
            return x, False
        if c <= _MAX_CHANNELS_HEURISTIC < r:
            return x.T.copy(), True
        return x, False


if __name__ == "__main__":  # pragma: no cover — маленькая самопроверка
    sr, ch = 44100, 2
    proc = MultibandProcessor(sr, ch)
    t = np.arange(sr) / sr
    x = np.stack([0.5 * np.sin(2 * np.pi * 997.0 * t)] * ch).T  # (n, ch)
    y = proc.process(x)
    print("in :", x.shape, x.dtype, "peak", float(np.max(np.abs(x))))
    print("out:", y.shape, y.dtype, "peak", float(np.max(np.abs(y))))
    print("latency:", proc.latency_samples, "samples =",
          round(proc.latency_ms, 2), "ms")
