from __future__ import annotations

import math

import numpy as np

__all__ = ["DynamicsCompressor"]

_DB_FLOOR_LIN = 1e-6          # 10^(-120/20): нижняя граница дБ-домена
_MAX_CHANNELS_HEURISTIC = 8   # эвристика распознавания layout'а 2-D массивов


def _db_to_lin(db: float) -> float:
    return 10.0 ** (db / 20.0)


def _peak_db(x: np.ndarray) -> np.ndarray:
    env = np.max(np.abs(x), axis=0)
    np.maximum(env, _DB_FLOOR_LIN, out=env)
    return 20.0 * np.log10(env)


def _soft_knee_target_db(level_db: np.ndarray,
                         threshold_db: float,
                         knee_db: float,
                         ratio: float) -> np.ndarray:

    x = level_db - threshold_db
    k = 0.5 * knee_db
    if k < 1e-9:  # жёсткое колено
        y = np.where(x > 0.0, x / ratio, x)
    else:
        y = np.empty_like(x)
        below = x <= -k
        above = x >= k
        mid = ~(below | above)
        y[below] = x[below]
        y[above] = x[above] / ratio
        u = x[mid] + k
        y[mid] = -k + u + u * u * (1.0 / ratio - 1.0) / (4.0 * k)
    return y - x  # усиление (дБ): output_dB - input_dB


def _peak_env_follow(env_abs: np.ndarray, state: float,
                    decay_coef: float):

    lst = env_abs.tolist()
    out = []
    ap = out.append
    e = state
    c = decay_coef
    for v in lst:
        ed = e * c
        e = v if v > ed else ed
        ap(e)
    return np.asarray(out, dtype=np.float64), e


def _ballistics(target_db: np.ndarray, g_state: float,
                attack_coef: float, release_coef: float):

    tl = target_db.tolist()
    out = []
    ap = out.append
    g = g_state
    ca = attack_coef
    cr = release_coef
    for t in tl:
        if t < g:      # нужно БОЛЬШЕ ослабления — ветка атаки
            g = t + (g - t) * ca
        else:          # ослабление можно отпускать — ветка восстановления
            g = t + (g - t) * cr
        ap(g)
    return np.asarray(out, dtype=np.float64), g


class DynamicsCompressor:
    def __init__(self,
                 sample_rate: int = 44100,
                 channels: int = 2,
                 *,
                 threshold_db: float = -24.0,
                 knee_db: float = 30.0,
                 ratio: float = 12.0,
                 attack_ms: float = 3.0,
                 release_ms: float = 250.0,
                 lookahead_ms: float = 6.0,
                 detector_release_ms: float = 150.0,
                 makeup_db: float | None = None,
                 limiter_ceiling_db: float | None = None,
                 limiter_lookahead_ms: float = 3.0,
                 limiter_attack_ms: float = 0.5,
                 limiter_release_ms: float = 60.0,
                 limiter_detector_release_ms: float = 25.0) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate должен быть > 0")
        if ratio < 1.0:
            raise ValueError("ratio должен быть >= 1")

        sr = float(sample_rate)
        self._sr = sr
        self._default_channels = int(channels)
        self._threshold = float(threshold_db)
        self._knee = max(0.0, float(knee_db))
        self._ratio = max(1.0, float(ratio))

        self._attack_coef = math.exp(-1.0 / (max(1e-6, attack_ms / 1000.0) * sr))
        self._release_coef = math.exp(-1.0 / (max(1e-6, release_ms / 1000.0) * sr))
        self._det_decay_coef = math.exp(
            -1.0 / (max(1e-6, detector_release_ms / 1000.0) * sr))
        self._L1 = max(0, int(round(lookahead_ms / 1000.0 * sr)))

        if makeup_db is None:
            self._makeup_db = 0.5 * (-self._threshold) * (1.0 - 1.0 / self._ratio)
        else:
            self._makeup_db = float(makeup_db)
        self._makeup_lin = _db_to_lin(self._makeup_db)

        self._limiter_on = limiter_ceiling_db is not None
        self._ceiling_db = float(limiter_ceiling_db) if self._limiter_on else 0.0
        self._ceiling_lin = _db_to_lin(self._ceiling_db) if self._limiter_on else 1.0
        self._lim_attack_coef = math.exp(
            -1.0 / (max(1e-6, limiter_attack_ms / 1000.0) * sr))
        self._lim_release_coef = math.exp(
            -1.0 / (max(1e-6, limiter_release_ms / 1000.0) * sr))
        self._lim_det_decay_coef = math.exp(
            -1.0 / (max(1e-6, limiter_detector_release_ms / 1000.0) * sr))
        self._L2 = max(0, int(round(limiter_lookahead_ms / 1000.0 * sr))) \
            if self._limiter_on else 0

        self.reset()



    @property
    def latency_samples(self) -> int:

        return self._L1 + self._L2

    @property
    def latency_ms(self) -> float:
        return 1000.0 * self.latency_samples / self._sr

    @property
    def makeup_db(self) -> float:
        return self._makeup_db

    def reset(self) -> None:

        self._d1 = np.zeros((0, self._L1), dtype=np.float64)   # line задержки №1
        self._g1 = 0.0                                          # гейн компрессора, дБ
        self._det1 = 0.0                                        # огибающая детектора №1
        self._d2 = np.zeros((0, self._L2), dtype=np.float64)   # line задержки №2
        self._g2 = 0.0                                          # гейн лимитера, дБ
        self._det2 = 0.0                                        # огибающая детектора №2
        self._seen_channels = 0

    def process(self, block: np.ndarray) -> np.ndarray:

        arr = np.asarray(block)
        if arr.ndim not in (1, 2):
            raise ValueError("Ожидался 1-D (моно) или 2-D (каналы x сэмплы) блок")
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
        if n == 0:
            return block

        ch = x.shape[0]
        if ch != self._seen_channels or self._d1.shape[0] != ch:
            self._d1 = np.zeros((ch, self._L1), dtype=np.float64)
            self._d2 = np.zeros((ch, self._L2), dtype=np.float64)
            self._seen_channels = ch

        # ---------- стадия 1: компрессор (детектор на реальном времени) ----
        cat1 = np.concatenate((self._d1, x), axis=1)      # (ch, L1 + n)
        delayed1 = cat1[:, :n].copy()                       # звук из прошлого
        if self._L1:
            self._d1 = cat1[:, n:n + self._L1].copy()       # хвост для след. чанка

        mono1 = np.max(np.abs(x), axis=0)                   # пик по каналам
        env1, self._det1 = _peak_env_follow(mono1, self._det1,
                                            self._det_decay_coef)
        np.maximum(env1, _DB_FLOOR_LIN, out=env1)
        env_db = 20.0 * np.log10(env1)
        target_db = _soft_knee_target_db(env_db, self._threshold,
                                         self._knee, self._ratio)
        g1_db, self._g1 = _ballistics(target_db, self._g1,
                                      self._attack_coef, self._release_coef)
        stage1 = delayed1 * (10.0 ** (g1_db / 20.0)) * self._makeup_lin

        # ---------- стадия 2: ОПЦИОНАЛЬНЫЙ look-ahead лимитер --------------
        # По умолчанию ВЫКЛЮЧЕН: DynamicsCompressorNode (Firefox) не содержит
        # лимитера — выход может превышать ±1.0 и ограничивается устройством
        # вывода, как в браузере.
        if self._limiter_on:
            cat2 = np.concatenate((self._d2, stage1), axis=1)
            delayed2 = cat2[:, :n].copy()
            if self._L2:
                self._d2 = cat2[:, n:n + self._L2].copy()

            mono2 = np.max(np.abs(stage1), axis=0)
            env2, self._det2 = _peak_env_follow(mono2, self._det2,
                                                self._lim_det_decay_coef)
            np.maximum(env2, _DB_FLOOR_LIN, out=env2)
            env2_db = 20.0 * np.log10(env2)
            target2_db = np.minimum(0.0, self._ceiling_db - env2_db)
            g2_db, self._g2 = _ballistics(target2_db, self._g2,
                                          self._lim_attack_coef,
                                          self._lim_release_coef)
            out = delayed2 * (10.0 ** (g2_db / 20.0))
            # Жёсткий потолок — только если лимитер включён явно.
            np.clip(out, -self._ceiling_lin, self._ceiling_lin, out=out)
        else:
            out = stage1

        # ---------- обратно в исходный формат -------------------------------
        if transposed:
            out = out.T  # возвращаем исходную ориентацию (n, channels)
        if int_info is not None:
            scale = float(1 << (8 * int_info.itemsize - 1))
            out = np.clip(out * scale, -scale, scale - 1.0).astype(int_info)
        if was_1d:
            out = out.reshape(-1)
        return out



    def _orient_2d(self, x: np.ndarray):

        r, c = x.shape
        dc = self._default_channels
        if r == dc and c == dc:
            return x, False                                   # квадрат — считаем строками
        if r == dc:
            return x, False
        if c == dc:
            return x.T, True
        if r <= _MAX_CHANNELS_HEURISTIC < c:
            return x, False
        if c <= _MAX_CHANNELS_HEURISTIC < r:
            return x.T, True
        return x, False  # неоднозначно — считаем строками (каналы x сэмплы)
