# candlestick_patterns.py
"""
Complete Candlestick pattern module converted from provided Amibroker AFL.
Includes a wide set of bullish/bearish/continuation/gap patterns and buy/sell lists.

Usage:
    import pandas as pd
    from candlestick_patterns import CandlePatterns

    df = pd.read_csv("ohlcv.csv")  # must have Open, High, Low, Close, Volume
    cp = CandlePatterns(df)
    df_out = cp.build_all()
"""

from typing import Optional
import pandas as pd
import numpy as np


class CandlePatterns:
    def __init__(self, df: pd.DataFrame):
        df = df.copy()
        cols = {c.lower(): c for c in df.columns}
        lower_map = {k: v for k, v in cols.items()}
        required = ['open', 'high', 'low', 'close', 'volume']
        for r in required:
            if r not in lower_map:
                raise ValueError(f"DataFrame must contain column '{r}' (case-insensitive). Found: {list(df.columns)}")
        self.df = df.rename(columns={lower_map['open']: 'Open',
                                     lower_map['high']: 'High',
                                     lower_map['low']: 'Low',
                                     lower_map['close']: 'Close',
                                     lower_map['volume']: 'Volume'})

        self.O = self.df['Open'].astype(float)
        self.H = self.df['High'].astype(float)
        self.L = self.df['Low'].astype(float)
        self.C = self.df['Close'].astype(float)
        self.V = self.df['Volume'].astype(float)

        # previous values shortcuts
        self.O1 = self.O.shift(1); self.O2 = self.O.shift(2); self.O3 = self.O.shift(3); self.O4 = self.O.shift(4)
        self.H1 = self.H.shift(1); self.H2 = self.H.shift(2); self.H3 = self.H.shift(3); self.H4 = self.H.shift(4)
        self.L1 = self.L.shift(1); self.L2 = self.L.shift(2); self.L3 = self.L.shift(3); self.L4 = self.L.shift(4)
        self.C1 = self.C.shift(1); self.C2 = self.C.shift(2); self.C3 = self.C.shift(3); self.C4 = self.C.shift(4)

        self.smallBodyMaximum = 0.0005  # 0.05% - more appropriate for this data
        self.LargeBodyMinimum = 0.002   # 0.2% - more appropriate for this data

        self._cache = {}

    # helpers
    def hh_max(self, s: pd.Series, window: int) -> pd.Series:
        return s.rolling(window, min_periods=1).max()

    def ll_min(self, s: pd.Series, window: int) -> pd.Series:
        return s.rolling(window, min_periods=1).min()

    # basic building blocks
    def white_body(self) -> pd.Series:
        return (self.C >= self.O)

    def black_body(self) -> pd.Series:
        return (self.O > self.C)

    def real_body_size(self) -> pd.Series:
        return (self.C - self.O).abs()

    def small_body(self) -> pd.Series:
        sb = self.smallBodyMaximum
        return (((self.O >= self.C * (1 - sb)) & self.white_body()) |
                ((self.C >= self.O * (1 - sb)) & self.black_body()))

    def large_body(self) -> pd.Series:
        lb = self.LargeBodyMinimum
        return (((self.C >= self.O * (1 + lb)) & self.white_body()) |
                ((self.C <= self.O * (1 - lb)) & self.black_body()))

    def medium_body(self) -> pd.Series:
        return ~(self.large_body() | self.small_body())

    def identical_bodies(self) -> pd.Series:
        prev_real = (self.O1 - self.C1).abs()
        cur_real = (self.O - self.C).abs()
        return ((prev_real - cur_real).abs() < cur_real * self.smallBodyMaximum)

    def small_upper_shadow(self) -> pd.Series:
        sb = self.smallBodyMaximum
        return ((self.white_body() & (self.H <= self.C * (1 + sb))) |
                (self.black_body() & (self.H <= self.O * (1 + sb))))

    def small_lower_shadow(self) -> pd.Series:
        sb = self.smallBodyMaximum
        return ((self.white_body() & (self.L >= self.O * (1 - sb))) |
                (self.black_body() & (self.L >= self.C * (1 - sb))))

    def large_upper_shadow(self) -> pd.Series:
        lb = self.LargeBodyMinimum
        return ((self.white_body() & (self.H >= self.C * (1 + lb))) |
                (self.black_body() & (self.H >= self.O * (1 + lb))))

    def large_lower_shadow(self) -> pd.Series:
        lb = self.LargeBodyMinimum
        return ((self.white_body() & (self.L <= self.O * (1 - lb))) |
                (self.black_body() & (self.L <= self.C * (1 - lb))))

    def doji(self) -> pd.Series:
        sb = self.smallBodyMaximum
        return ((self.C - self.O).abs() <= (self.C * sb)) | ((self.O - self.C).abs() <= ((self.H - self.L) * 0.1))

    def near_doji(self) -> pd.Series:
        return ((self.O - self.C).abs() <= ((self.H - self.L) * 0.1))

    def MHT(self) -> pd.Series:
        return (self.hh_max(self.H, 5) == self.H)

    def MHY(self) -> pd.Series:
        return (self.hh_max(self.H, 5).shift(1) == self.H1)

    def MLT(self) -> pd.Series:
        return (self.ll_min(self.L, 5) == self.L)

    def MLY(self) -> pd.Series:
        return (self.ll_min(self.L, 5).shift(1) == self.L1)

    # gap definitions (from AFL logic)
    def GapUp(self) -> pd.Series:
        prev_black = (self.O1 > self.C1)
        prev_white = (self.C1 >= self.O1)
        cond1 = (prev_black & self.white_body() & (self.O > self.O1))
        cond2 = (prev_black & self.black_body() & (self.C > self.O1))
        cond3 = (prev_white & self.white_body() & (self.O > self.C1))
        cond4 = (prev_white & self.black_body() & (self.C > self.C1))
        return cond1 | cond2 | cond3 | cond4

    def GapDown(self) -> pd.Series:
        prev_black = (self.O1 > self.C1)
        prev_white = (self.C1 >= self.O1)
        cond1 = (prev_black & self.white_body() & (self.C < self.C1))
        cond2 = (prev_black & self.black_body() & (self.O < self.C1))
        cond3 = (prev_white & self.white_body() & (self.C < self.O1))
        cond4 = (prev_white & self.black_body() & (self.O < self.O1))
        return cond1 | cond2 | cond3 | cond4

    def big_gap_up(self) -> pd.Series:
        return self.L > 1.01 * self.H1

    def big_gap_down(self) -> pd.Series:
        return self.H < 0.99 * self.L1

    def huge_gap_up(self) -> pd.Series:
        return self.L > 1.02 * self.H1

    def huge_gap_down(self) -> pd.Series:
        return self.H < 0.98 * self.L1

    def double_gap_up(self) -> pd.Series:
        gu = self.GapUp()
        return gu & gu.shift(1)

    def double_gap_down(self) -> pd.Series:
        gd = self.GapDown()
        return gd & gd.shift(1)

    # Simple named patterns
    def hammer(self) -> pd.Series:
        return ((self.H - self.L) > 3 * (self.O - self.C).abs()) & \
               ((self.C - self.L) / (0.001 + (self.H - self.L)) > 0.6) & \
               ((self.O - self.L) / (0.001 + (self.H - self.L)) > 0.6)

    def hanging_man(self) -> pd.Series:
        return ((self.H - self.L) > 4 * (self.O - self.C).abs()) & \
               ((self.C - self.L) / (0.001 + (self.H - self.L)) >= 0.75) & \
               ((self.O - self.L) / (0.001 + (self.H - self.L)) >= 0.75)

    def inverted_hammer(self) -> pd.Series:
        return ((self.H - self.L) > 3 * (self.O - self.C).abs()) & \
               ((self.H - self.C) / (0.001 + (self.H - self.L)) > 0.6) & \
               ((self.H - self.O) / (0.001 + (self.H - self.L)) > 0.6)

    def shooting_star(self) -> pd.Series:
        return ((self.H - self.L) > 4 * (self.O - self.C).abs()) & \
               ((self.H - self.C) / (0.001 + (self.H - self.L)) >= 0.75) & \
               ((self.H - self.O) / (0.001 + (self.H - self.L)) >= 0.75)

    def white_spinning_top(self) -> pd.Series:
        return (self.C > self.O) & ((self.H - self.L) > (3 * (self.C - self.O))) & \
               (((self.H - self.C) / (0.001 + (self.H - self.L))) < 0.4) & \
               (((self.O - self.L) / (0.001 + (self.H - self.L))) < 0.4)

    def black_spinning_top(self) -> pd.Series:
        return (self.O > self.C) & ((self.H - self.L) > (3 * (self.O - self.C))) & \
               (((self.H - self.O) / (0.001 + (self.H - self.L))) < 0.4) & \
               (((self.C - self.L) / (0.001 + (self.H - self.L))) < 0.4)

    # Engulfing patterns
    def bullish_engulfing(self) -> pd.Series:
        O1, C1 = self.O1, self.C1
        return (O1 > C1) & (self.C > self.O) & (self.C >= O1) & (C1 >= self.O) & ((self.C - self.O) > (O1 - C1))

    def bearish_engulfing(self) -> pd.Series:
        O1, C1 = self.O1, self.C1
        return (C1 > O1) & (self.O > self.C) & (self.O >= C1) & (O1 >= self.C) & ((self.O - self.C) > (C1 - O1))

    def piercing_line(self) -> pd.Series:
        O1, C1 = self.O1, self.C1
        return (C1 < O1) & (((O1 + C1) / 2) < self.C) & (self.O < self.C) & (self.O < O1) & (self.C < O1) & ((self.C - self.O) / (0.001 + (self.H - self.L)) > 0.6)

    def three_white_soldiers(self) -> pd.Series:
        return (self.C > self.O * 1.01) & (self.C1 > self.O1 * 1.01) & (self.C2 > self.O2 * 1.01) & \
               (self.C > self.C1) & (self.C1 > self.C2) & (self.O < self.C1) & (self.O > self.O1) & \
               (self.O1 < self.C2) & (self.O1 > self.O2) & (((self.H - self.C) / (self.H - self.L)) < 0.2) & \
               (((self.H1 - self.C1) / (self.H1 - self.L1)) < 0.2) & (((self.H2 - self.C2) / (self.H2 - self.L2)) < 0.2)

    def three_black_crows(self) -> pd.Series:
        return (self.O > self.C * 1.01) & (self.O1 > self.C1 * 1.01) & (self.O2 > self.C2 * 1.01) & \
               (self.C < self.C1) & (self.C1 < self.C2) & (self.O > self.C1) & (self.O < self.O1) & \
               (self.O1 > self.C2) & (self.O1 < self.O2) & (((self.C - self.L) / (self.H - self.L)) < 0.2) & \
               (((self.C1 - self.L1) / (self.H1 - self.L1)) < 0.2) & (((self.C2 - self.L2) / (self.H2 - self.L2)) < 0.2)

    # doji star pattern
    def doji_star(self) -> pd.Series:
        return self.doji() & (self.GapUp() | self.GapDown()) & self.large_body().shift(1)

    # morning/evening star
    def morning_star(self) -> pd.Series:
        return (self.large_body().shift(2) & (self.O.shift(2) > self.C.shift(2)) & self.GapDown().shift(1) & self.white_body() & self.large_body() & (self.C > self.C.shift(2)) & self.MLY())

    def evening_star(self) -> pd.Series:
        return (self.large_body().shift(2) & (self.C.shift(2) >= self.O.shift(2)) & self.GapUp().shift(1) & (~self.large_body().shift(1).fillna(False)) & self.black_body() & (~self.small_body().fillna(False)) & (self.MHT() | self.MHY()))

    def dark_cloud_cover(self) -> pd.Series:
        cond1 = (self.C1 > self.O1) & (((self.C1 + self.O1) / 2) > self.C) & (self.O > self.C) & (self.O > self.C1) & (self.C > self.O1)
        return cond1

    # Tweezer patterns
    def tweezer_top(self) -> pd.Series:
        return ((self.H - self.H1).abs() <= self.H * 0.0005) & (self.O > self.C) & (self.C1 > self.O1)

    def tweezer_bottom(self) -> pd.Series:
        return (((self.L - self.L1).abs() / self.L) < 0.0005) & (self.O < self.C) & (self.O1 > self.C1)

    def match_low(self) -> pd.Series:
        llv8 = self.ll_min(self.L, 8)
        llv2 = self.ll_min(self.L, 2)
        return (llv8 == llv2) & (self.C1 <= self.O1 * 0.99) & ((self.C - self.C1).abs() <= self.C * 0.0005) & (self.O > self.C1) & (self.O <= (self.H - ((self.H - self.L) * 0.5)))

    # many multi-day patterns (closely translated)
    def abandoned_baby_bullish(self) -> pd.Series:
        return (self.large_body().shift(2) & (self.O.shift(2) > self.C.shift(2)) & self.GapDown().shift(1) & self.white_body() & self.large_body() & self.GapUp())

    def abandoned_baby_bearish(self) -> pd.Series:
        return (self.large_body().shift(2) & self.white_body().shift(2) & self.small_body().shift(1) & self.GapUp().shift(1) & self.GapDown() & (~self.small_body()) & self.black_body() & self.MHY())

    def belt_hold_bullish(self) -> pd.Series:
        return self.large_body() & self.small_lower_shadow() & self.white_body() & self.MLT()

    def belt_hold_bearish(self) -> pd.Series:
        return self.large_body() & self.black_body() & self.small_upper_shadow() & self.MHT()

    def breakaway_bullish(self) -> pd.Series:
        return (self.large_body().shift(4) & (self.O.shift(4) > self.C.shift(4)) & (self.O.shift(3) < self.C.shift(4)) & self.small_body().shift(2) & (self.C.shift(2) < self.C.shift(3)) & (self.C.shift(1) < self.C.shift(2)) & self.large_body() & self.white_body() & (self.C > self.O.shift(3)) & (self.C < self.C.shift(4)))

    def breakaway_bearish(self) -> pd.Series:
        return (self.large_body().shift(4) & self.white_body().shift(4) & self.GapUp().shift(3) & self.white_body().shift(3) & self.small_body().shift(2) & self.small_body().shift(1) & self.black_body() & (self.O > self.O.shift(3)) & (self.C < self.C.shift(4)))

    def concealing_baby_swallow(self) -> pd.Series:
        # approximated: 4-day complex swallow: marabuzu/black series, down gap then engulfing on last day
        marabuzu_prev3 = self.large_body().shift(3) & (self.H.shift(3) == self.O.shift(3)) & (self.C.shift(3) == self.L.shift(3))
        cond = marabuzu_prev3 & (self.black_body().shift(3)) & (self.large_body().shift(2)) & (self.black_body().shift(2)) & (self.black_body().shift(1)) & self.GapDown().shift(1) & (self.H.shift(1) > self.C.shift(2)) & self.black_body().shift(1) & self.black_body() & self.bullish_engulfing()
        return cond

    def doji_star_bullish(self) -> pd.Series:
        return (self.doji().shift(1) & (self.MLT() | self.MLY())) | (self.doji() & ((self.C < self.C1) | (self.O < self.C1)) & self.black_body().shift(1) & self.large_body().shift(1))

    def doji_star_bearish(self) -> pd.Series:
        return (self.doji().shift(1) & (self.MHT() | self.MHY())) | (self.doji() & ((self.C > self.C1) | (self.O > self.C1)) & self.white_body().shift(1) & self.large_body().shift(1))

    def engulfing_bullish(self) -> pd.Series:
        return self.bullish_engulfing() & self.large_body() & self.white_body() & ((self.black_body().shift(1)) | (self.doji().shift(1))) & self.MLT()

    def engulfing_bearish(self) -> pd.Series:
        return self.bearish_engulfing() & self.large_body() & self.black_body() & ((self.white_body().shift(1)) | (self.doji().shift(1))) & (self.MHT() | self.MHY())

    def harami(self) -> pd.Series:
        # harami: current small body contained within previous body
        prev_high = self.H1; prev_low = self.L1
        cur_high = self.H; cur_low = self.L
        return (cur_high <= prev_high) & (cur_low >= prev_low)

    def harami_bullish(self) -> pd.Series:
        return self.harami() & self.large_body().shift(1) & self.black_body().shift(1) & (~self.large_body()) & self.white_body()

    def harami_bearish(self) -> pd.Series:
        return self.harami() & self.large_body().shift(1) & self.white_body().shift(1) & self.black_body() & (self.MHY() | self.MHT())

    def harami_cross(self) -> pd.Series:
        return self.harami() & self.large_body().shift(1) & self.black_body().shift(1) & self.doji()

    def harami_cross_bearish(self) -> pd.Series:
        return self.harami() & self.doji() & self.white_body().shift(1) & self.large_body().shift(1)

    def homing_pigeon(self) -> pd.Series:
        return self.large_body().shift(1) & self.black_body().shift(1) & (self.H <= self.O1) & (self.L >= self.C1) & (self.C < self.O) & self.MLY()

    def inverted_hammer_pattern(self) -> pd.Series:
        return self.shooting_star() & (self.MLT() | self.MLY())

    def meeting_lines_bullish(self) -> pd.Series:
        return self.large_body().shift(1) & self.black_body().shift(1) & self.large_body() & self.white_body() & (self.C > self.C1 * 0.9975) & (self.C < self.C1 * 1.0025)

    def meeting_lines_bearish(self) -> pd.Series:
        return (self.large_body().shift(1) & self.white_body().shift(1) & (self.hh_max(self.C, 8).shift(1) == self.C1) & self.large_body() & self.black_body() & ((self.C - self.C1).abs() < self.C * 0.0005))

    def morning_doji_star(self) -> pd.Series:
        return self.large_body().shift(2) & self.black_body().shift(2) & self.doji().shift(1) & (self.O.shift(1) < self.C.shift(2)) & self.white_body() & (self.C > self.C.shift(2)) & self.MLY()

    def pierce_line(self) -> pd.Series:
        return self.piercing_line()

    def stick_sandwich(self) -> pd.Series:
        return self.large_body().shift(2) & self.black_body().shift(2) & self.large_body().shift(1) & self.white_body().shift(1) & (self.O.shift(1) >= self.C.shift(2)) & (self.O >= self.C.shift(1)) & ((self.C - self.C.shift(2)).abs() <= self.C * 0.0005)

    def three_inside_up(self) -> pd.Series:
        return self.harami_bullish().shift(1) & self.white_body() & self.large_body() & (self.C > self.C1)

    def three_outside_up(self) -> pd.Series:
        return self.engulfing_bullish().shift(1) & self.white_body() & (self.C > self.C1)

    def three_stars_in_the_south(self) -> pd.Series:
        return self.large_body().shift(2) & self.black_body().shift(2) & self.large_lower_shadow().shift(2) & self.black_body().shift(1) & self.large_lower_shadow().shift(1) & (self.L.shift(1) > self.L.shift(2)) & self.black_body() & self.small_upper_shadow() & self.small_lower_shadow() & (self.L > self.L.shift(1)) & (self.H < self.H.shift(1))

    def tri_star_bullish(self) -> pd.Series:
        return self.doji().shift(2) & self.doji().shift(1) & self.doji() & self.MLY() & (self.GapDown().shift(1)) & self.GapUp()

    def tri_star_bearish(self) -> pd.Series:
        return self.doji().shift(2) & self.doji().shift(1) & self.doji() & self.MHY() & self.GapUp().shift(1) & self.GapDown()

    def threeriver_bottom(self) -> pd.Series:
        # approximate
        return self.large_body().shift(2) & self.black_body().shift(2) & self.black_body().shift(1) & self.large_lower_shadow().shift(1) & (self.O.shift(1) < self.O.shift(2)) & (self.C.shift(1) > self.C.shift(2)) & self.white_body() & (self.C < self.C.shift(1)) & self.MLY()

    def mat_hold_bullish(self) -> pd.Series:
        return self.large_body().shift(4) & self.white_body().shift(4) & self.black_body().shift(3) & self.GapUp().shift(3) & (~self.large_body().shift(3).fillna(False)) & (~self.large_body().shift(2).fillna(False)) & (self.C.shift(2) < self.C.shift(3)) & (self.O.shift(2) < self.O.shift(3)) & (self.C.shift(2) > self.O.shift(4)) & (~self.large_body().shift(1).fillna(False)) & (self.C.shift(1) < self.C.shift(2)) & self.large_body() & self.white_body() & (self.C > self.C.shift(4))

    def rising_three_methods(self) -> pd.Series:
        return self.large_body().shift(4) & self.white_body().shift(4) & (~self.large_body().shift(3).fillna(False)) & (~self.large_body().shift(2).fillna(False)) & (~self.large_body().shift(1).fillna(False)) & (self.C.shift(3) < self.C.shift(4)) & (self.C.shift(2) < self.C.shift(3)) & (self.C.shift(1) < self.C.shift(2)) & self.large_body() & self.white_body() & (self.C > self.C.shift(4))

    def upside_gap_three_methods(self) -> pd.Series:
        return self.large_body().shift(2) & self.white_body().shift(2) & self.large_body().shift(1) & self.white_body().shift(1) & self.GapUp().shift(1) & self.black_body() & (self.O > self.O.shift(1)) & (self.C < self.C.shift(2)) & (self.C > self.O.shift(2)) & self.MHY()

    def three_line_strike(self) -> pd.Series:
        return (~self.small_body().shift(3).fillna(False)) & (~self.small_body().shift(2).fillna(False)) & (~self.small_body().shift(1).fillna(False)) & self.white_body().shift(3) & self.white_body().shift(2) & self.white_body().shift(1) & (self.C.shift(1) > self.C.shift(2)) & (self.C.shift(2) > self.C.shift(3)) & self.black_body() & (self.O > self.C.shift(1)) & (self.C < self.O.shift(3))

    def upside_tasuki_gap(self) -> pd.Series:
        return self.large_body().shift(2) & self.large_body().shift(1) & self.white_body().shift(2) & self.white_body().shift(1) & self.GapUp().shift(1) & self.black_body() & (self.O > self.O.shift(1)) & (self.C < self.O.shift(1)) & (self.C > self.C.shift(2)) & self.identical_bodies() & (self.O < self.C.shift(1))

    # Bearish counterparts and continuation patterns
    def downside_gap_three_methods(self) -> pd.Series:
        return (self.large_body().shift(2) & self.black_body().shift(2) & self.GapDown().shift(2) & self.large_body().shift(1) & self.black_body().shift(1) & self.white_body() & (self.O < self.O.shift(1)) & (self.C > self.C.shift(2)) & (self.ll_min(self.L, 8) == self.L.shift(1)))

    def downside_tasuki_gap(self) -> pd.Series:
        return (self.black_body().shift(2) & self.black_body().shift(1) & self.GapDown().shift(1) & self.white_body() & (self.O < self.O.shift(1)) & (self.O > self.C.shift(1)) & (self.C > self.O.shift(1)) & (self.C < self.C.shift(2)) & self.identical_bodies().shift(1) & (self.ll_min(self.L, 15) == self.L.shift(1)))

    def falling_three_methods(self) -> pd.Series:
        return self.large_body().shift(4) & self.black_body().shift(4) & (self.C.shift(1) > self.C.shift(2)) & (self.C.shift(2) > self.C.shift(3)) & self.large_body() & self.black_body() & (self.O > self.C.shift(4)) & (self.O < self.O.shift(4)) & (self.C < self.O.shift(4)) & (self.C < self.C.shift(4))

    def in_neck_bearish(self) -> pd.Series:
        return self.large_body().shift(1) & self.black_body().shift(1) & self.white_body() & (self.O < self.L.shift(1)) & (self.C < self.C.shift(1) * 1.0005) & (self.C >= self.C.shift(1))

    def on_neck_bearish(self) -> pd.Series:
        return self.large_body().shift(1) & self.black_body().shift(1) & self.white_body() & (self.O < self.L.shift(1)) & (self.C < self.L.shift(1) * 1.0025) & (self.C >= self.L.shift(1) * 0.9975)

    def separating_lines_bullish(self) -> pd.Series:
        return (self.black_body().shift(1) & self.white_body() & self.large_body() & self.small_lower_shadow() & self.MHT() & ((self.O - self.O1).abs() <= self.O * 0.0001))

    def separating_lines_bearish(self) -> pd.Series:
        return self.large_body().shift(1) & self.white_body().shift(1) & self.black_body() & (self.O > self.O1 * 0.9975) & (self.O <= self.O1 * 1.0025)

    def side_by_side_white_lines(self) -> pd.Series:
        return (~self.small_body().shift(2).fillna(False)) & self.white_body().shift(2) & self.GapUp().shift(1) & self.white_body().shift(1) & self.white_body() & self.identical_bodies() & ((self.O - self.O1).abs() < self.O * 0.0005)

    def side_by_side_white_lines_bearish(self) -> pd.Series:
        return (~self.small_body().shift(2).fillna(False)) & self.black_body().shift(2) & self.white_body().shift(1) & self.white_body() & self.GapDown().shift(1) & self.identical_bodies() & ((self.C - self.C.shift(1)).abs() < self.C * 0.0005)

    def two_crows(self) -> pd.Series:
        return self.white_body().shift(2) & self.large_body().shift(2) & self.black_body().shift(1) & self.GapUp().shift(1) & self.black_body() & (self.O < self.O.shift(1)) & (self.O > self.C.shift(1)) & (self.C < self.C.shift(2)) & (self.C > self.O.shift(2)) & self.MHY()

    def upside_gap_two_crows(self) -> pd.Series:
        return self.white_body().shift(2) & self.large_body().shift(2) & self.GapUp().shift(1) & self.black_body().shift(1) & self.black_body() & (self.O > self.O.shift(1)) & (self.C < self.C.shift(1)) & (self.C > self.C.shift(2))

    def tri_star_bearish_alt(self) -> pd.Series:
        return self.tri_star_bearish()

    # -- Combined buy/sell signals --
    def buy_signal(self) -> pd.Series:
        idx = self.df.index
        candidates = [
            self.abandoned_baby_bullish(),           # abandonedBabybullish
            self.belt_hold_bullish(),                # beltHoldBullish
            self.breakaway_bullish(),                # breakAwayBullish
            self.concealing_baby_swallow(),          # ConcealingBabySwallow
            self.bullish_engulfing_no_doji(),        # bullishEngulfing (without doji condition)
            self.hammer_bullish(),                   # hammerBullish
            self.harami_bullish(),                   # haramiBullish
            self.homing_pigeon(),                    # homingPigeon
            self.inverted_hammer(),                  # invertedHammer
            self.meeting_lines_bullish(),            # meetingLinesbullish
            self.morning_star(),                     # morningStar
            self.pierce_line(),                      # piercingLine
            self.stick_sandwich(),                   # stickSandwich
            self.three_inside_up(),                  # threeInsideUp
            self.three_outside_up(),                 # threeOutsideUp
            self.three_stars_in_the_south(),         # threeStarsInTheSouth
            self.threeriver_bottom(),                # threeriverBottom
            self.mat_hold_bullish(),                 # MAtHoldBullish
            self.rising_three_methods(),             # risingThreeMethods
            self.separating_lines_bullish(),         # separatingLinesBullish
            self.side_by_side_white_lines(),         # sideBySideWhiteLines
            self.three_white_soldiers(),             # threeWhiteSoldiers
            self.upside_gap_three_methods(),         # upsideGapThreeMethods
            self.three_line_strike(),                # threeLineStrike
            self.tweezer_bottom(),                   # tweezerBottom
            self.upside_tasuki_gap()                 # upsideTasukiGap
        ]
        combined = pd.Series(False, index=idx)
        for s in candidates:
            combined |= s.fillna(False)
        return combined.fillna(False)

    def sell_signal(self) -> pd.Series:
        idx = self.df.index
        candidates = [
            self.abandoned_baby_bearish(),           # AbandonedBabyBearish
            self.advance_block_bearish(),            # advanceBlockBearish
            self.belt_hold_bearish(),                # beltHoldBearish
            self.breakaway_bearish(),                # breakAwayBearish
            self.dark_cloud_cover(),                 # darkCloudCover
            self.deliberation_bearish(),             # deliberationBearish
            self.counter_attack_bearish(),           # CounterAttackBearish
            self.bearish_engulfing_no_doji(),        # bearishEngulfing (without doji condition)
            self.evening_star(),                     # eveningStar
            self.HangingMan(),                       # HangingMan
            self.hammer_bearish(),                   # HammerBearish
            self.harami_bearish(),                   # HaramiBearish
            self.identical_three_black_crows(),      # idendicalThreeBlackCrows
            self.kicking_bearish(),                  # kickingBearish
            self.meeting_lines_bearish(),            # MeetingLinesBearish
            self.shooting_star_gap(),                # shootingStarGap
            self.three_inside_down_bearish(),        # threeInsideDownBearish
            self.three_outside_down_bearish(),       # threeoutsideDownBearish
            self.two_crows(),                        # twoCrows
            self.upside_gap_two_crows(),             # upsideGapTwoCrows
            self.downside_gap_three_methods(),       # downsideGapThreeMethods
            self.downside_tasuki_gap(),              # downsideTasukiGap
            self.falling_three_methods(),            # fallingThreeMethods
            self.in_neck_bearish(),                  # inNeckBearish
            self.on_neck_bearish(),                  # OnNeckBearish
            self.separating_lines_bearish(),         # separatingLinesBearish
            self.side_by_side_white_lines_bearish(), # sideBySideWhiteLinesBearish
            self.three_black_crows(),                # threeBlackCrows
            self.three_line_strike(),                # threeLineStrike
            self.thrusting_bearish(),                # thrustingBearish
            self.tweezer_top()                       # tweezerTop
        ]
        combined = pd.Series(False, index=idx)
        for s in candidates:
            combined |= s.fillna(False)
        return combined.fillna(False)

    # helper wrappers to avoid name errors in the sell list
    def HangingMan(self) -> pd.Series:
        return self.hanging_man()

    def three_inside_down(self) -> pd.Series:
        return self.three_inside_up().shift(0) & (self.black_body() & (self.C < self.C1))  # approximate

    def three_outside_down(self) -> pd.Series:
        return self.three_outside_up().shift(0) & (self.black_body() & (self.C < self.C1))  # approximate

    def thrusting_bearish(self) -> pd.Series:
        # thrustingBearish=Ref(blackBody,-1) AND Ref(LargeBody,-1) AND LargeBody AND whitebody AND O<Ref(L,-1) AND C<(Ref(O,-1)+Ref(C,-1))/2 AND C>Ref(C,-1);
        return (self.black_body().shift(1) & self.large_body().shift(1) & self.large_body() & self.white_body() & (self.O < self.L1) & (self.C < ((self.O1 + self.C1) / 2)) & (self.C > self.C1))

    # Additional missing patterns for the specified criteria
    def advance_block_bearish(self) -> pd.Series:
        # Advance block bearish: three consecutive white candles with decreasing body sizes
        return (self.white_body().shift(2) & self.white_body().shift(1) & self.white_body() & 
                (self.C.shift(2) > self.C.shift(1)) & (self.C.shift(1) > self.C) &
                (self.real_body_size().shift(2) > self.real_body_size().shift(1)) & 
                (self.real_body_size().shift(1) > self.real_body_size()))

    def deliberation_bearish(self) -> pd.Series:
        # Deliberation bearish: two large white bodies followed by a small body
        return (self.large_body().shift(2) & self.white_body().shift(2) & 
                self.large_body().shift(1) & self.white_body().shift(1) & 
                self.small_body() & self.white_body())

    def counter_attack_bearish(self) -> pd.Series:
        # Counter attack bearish: two candles with same close prices, one white one black
        return (self.white_body().shift(1) & self.black_body() & 
                ((self.C - self.C1).abs() <= self.C * 0.0005))

    def evening_doji_star(self) -> pd.Series:
        # Evening doji star: large white body, doji, then black body
        return (self.large_body().shift(2) & self.white_body().shift(2) & 
                self.doji().shift(1) & self.black_body() & self.large_body())

    def dragonfly_doji_bearish(self) -> pd.Series:
        # Dragonfly doji bearish: doji with very large lower shadow at high
        return (self.doji() & self.large_lower_shadow() & self.small_upper_shadow() & 
                (self.MHT() | self.MHY()))

    def hammer_bullish(self) -> pd.Series:
        # Hammer bullish: hammer pattern at low
        return (self.hammer() & (self.MLT() | self.MLY()))

    def hammer_bearish(self) -> pd.Series:
        # Hammer bearish: hammer pattern at high
        return (self.hammer() & (self.hh_max(self.H, 8) == self.H))

    def identical_three_black_crows(self) -> pd.Series:
        # Identical three black crows: three consecutive black candles with similar closes
        return (self.black_body().shift(2) & self.black_body().shift(1) & self.black_body() &
                (self.C.shift(2) > self.C.shift(1)) & (self.C.shift(1) > self.C) &
                ((self.C.shift(2) - self.C.shift(1)).abs() <= self.C.shift(2) * 0.002) &
                ((self.C.shift(1) - self.C).abs() <= self.C.shift(1) * 0.002))

    def kicking_bearish(self) -> pd.Series:
        # Kicking bearish: marubozu white followed by marubozu black with gap
        return (self.large_body().shift(1) & self.white_body().shift(1) & 
                (self.H.shift(1) == self.C.shift(1)) & (self.L.shift(1) == self.O.shift(1)) &
                self.large_body() & self.black_body() & 
                (self.H == self.O) & (self.L == self.C) & self.GapDown())

    def shooting_star_gap(self) -> pd.Series:
        # Shooting star with gap up
        return (self.shooting_star() & self.GapUp())

    def gravestone_doji(self) -> pd.Series:
        # Gravestone doji: doji with very large upper shadow
        return (self.doji() & self.large_upper_shadow() & self.small_lower_shadow() & 
                self.GapUp() & (self.MHT() | self.MHY()))

    def three_inside_down_bearish(self) -> pd.Series:
        # Three inside down bearish: harami bearish followed by black body
        return (self.harami_bearish().shift(1) & self.black_body() & 
                (self.C < self.C1))

    def three_outside_down_bearish(self) -> pd.Series:
        # Three outside down bearish: engulfing bearish followed by black body
        return (self.engulfing_bearish().shift(1) & self.black_body() & 
                (self.C < self.C1))

    def dragonfly_doji(self) -> pd.Series:
        # Dragonfly doji: doji with very large lower shadow at low
        return (self.doji() & self.large_lower_shadow() & self.small_upper_shadow() & 
                (self.MLT() | self.MLY()))

    # Non-doji versions of engulfing patterns for buy/sell signals
    def bullish_engulfing_no_doji(self) -> pd.Series:
        # Bullish engulfing without doji condition
        return self.bullish_engulfing() & self.large_body() & self.white_body() & self.black_body().shift(1) & self.MLT()

    def bearish_engulfing_no_doji(self) -> pd.Series:
        # Bearish engulfing without doji condition
        return self.bearish_engulfing() & self.large_body() & self.black_body() & self.white_body().shift(1) & (self.MHT() | self.MHY())

    # commentary mapping (simplified)
    def c_status(self) -> pd.Series:
        idx = self.df.index
        out = pd.Series("Zilch", index=idx)
        # Priority mapping (partial, but covering main signals)
        out = out.mask(self.abandoned_baby_bullish(), "Abandoned Baby Bullish")
        out = out.mask(self.belt_hold_bullish(), "Belt Hold Bullish")
        out = out.mask(self.breakaway_bullish(), "Break Away Bullish")
        out = out.mask(self.concealing_baby_swallow(), "Concealing Baby Swallow")
        out = out.mask(self.doji_star_bullish(), "Bullish Doji Star")
        out = out.mask(self.engulfing_bullish(), "Bullish Engulfing")
        out = out.mask(self.hammer(), "Hammer")
        out = out.mask(self.piercing_line(), "Piercing Line")
        out = out.mask(self.morning_star(), "Morning Star")
        out = out.mask(self.three_white_soldiers(), "3 White Soldiers")
        out = out.mask(self.shooting_star(), "Shooting Star")
        out = out.mask(self.bearish_engulfing(), "Bearish Engulfing")
        out = out.mask(self.evening_star(), "Evening Star")
        out = out.mask(self.three_black_crows(), "3 Black Crows")
        out = out.mask(self.doji(), "Doji")
        return out.fillna("Zilch")

    def p_status(self) -> pd.Series:
        idx = self.df.index
        out = pd.Series("Zilch", index=idx)
        out = out.mask(self.GapUp(), "Gap Up")
        out = out.mask(self.GapDown(), "Gap Down")
        out = out.mask(self.big_gap_up(), "Big Gap Up")
        out = out.mask(self.big_gap_down(), "Big Gap Down")
        out = out.mask(self.huge_gap_up(), "Huge Gap Up")
        out = out.mask(self.huge_gap_down(), "Huge Gap Down")
        out = out.mask(self.double_gap_up(), "Double Gap Up")
        out = out.mask(self.double_gap_down(), "Double Gap Down")
        return out.fillna("Zilch")

    def build_all(self) -> pd.DataFrame:
        df = self.df.copy()

        # Basic blocks
        df['WhiteBody'] = self.white_body()
        df['BlackBody'] = self.black_body()
        df['SmallBody'] = self.small_body()
        df['LargeBody'] = self.large_body()
        df['MediumBody'] = self.medium_body()
        df['IdenticalBodies'] = self.identical_bodies()
        df['RealBodySize'] = self.real_body_size()
        df['SmallUpperShadow'] = self.small_upper_shadow()
        df['SmallLowerShadow'] = self.small_lower_shadow()
        df['LargeUpperShadow'] = self.large_upper_shadow()
        df['LargeLowerShadow'] = self.large_lower_shadow()
        df['Doji'] = self.doji()
        df['NearDoji'] = self.near_doji()

        # Gaps
        df['GapUp'] = self.GapUp()
        df['GapDown'] = self.GapDown()
        df['BigGapUp'] = self.big_gap_up()
        df['BigGapDown'] = self.big_gap_down()
        df['HugeGapUp'] = self.huge_gap_up()
        df['HugeGapDown'] = self.huge_gap_down()
        df['DoubleGapUp'] = self.double_gap_up()
        df['DoubleGapDown'] = self.double_gap_down()

        # Single-day patterns
        df['Hammer'] = self.hammer()
        df['HangingMan'] = self.hanging_man()
        df['InvertedHammer'] = self.inverted_hammer()
        df['ShootingStar'] = self.shooting_star()
        df['WhiteSpinningTop'] = self.white_spinning_top()
        df['BlackSpinningTop'] = self.black_spinning_top()
        df['DojiStar'] = self.doji_star()

        # Engulfing / Piercing / Marubozu-like
        df['BullishEngulfing'] = self.bullish_engulfing()
        df['BearishEngulfing'] = self.bearish_engulfing()
        df['PiercingLine'] = self.piercing_line()
        df['DarkCloudCover'] = self.dark_cloud_cover()
        df['TweezerTop'] = self.tweezer_top()
        df['TweezerBottom'] = self.tweezer_bottom()
        df['MATCHLOW'] = self.match_low()

        # Multi-day patterns (many)
        df['AbandonedBabyBullish'] = self.abandoned_baby_bullish()
        df['AbandonedBabyBearish'] = self.abandoned_baby_bearish()
        df['BeltHoldBullish'] = self.belt_hold_bullish()
        df['BeltHoldBearish'] = self.belt_hold_bearish()
        df['BreakAwayBullish'] = self.breakaway_bullish()
        df['BreakAwayBearish'] = self.breakaway_bearish()
        df['ConcealingBabySwallow'] = self.concealing_baby_swallow()
        df['DojiStarBullish'] = self.doji_star_bullish()
        df['DojiStarBearish'] = self.doji_star_bearish()
        df['EngulfingBullish'] = self.engulfing_bullish()
        df['EngulfingBearish'] = self.engulfing_bearish()
        df['Harami'] = self.harami()
        df['HaramiBullish'] = self.harami_bullish()
        df['HaramiBearish'] = self.harami_bearish()
        df['HaramiCross'] = self.harami_cross()
        df['HaramiCrossBearish'] = self.harami_cross_bearish()
        df['HomingPigeon'] = self.homing_pigeon()
        df['MeetingLinesBullish'] = self.meeting_lines_bullish()
        df['MeetingLinesBearish'] = self.meeting_lines_bearish()
        df['MorningDojiStar'] = self.morning_doji_star()
        df['MorningStar'] = self.morning_star()
        df['StickSandwich'] = self.stick_sandwich()
        df['ThreeInsideUp'] = self.three_inside_up()
        df['ThreeOutsideUp'] = self.three_outside_up()
        df['ThreeStarsInTheSouth'] = self.three_stars_in_the_south()
        df['TriStarBullish'] = self.tri_star_bullish()
        df['ThreeRiverBottom'] = self.threeriver_bottom()
        df['MatHoldBullish'] = self.mat_hold_bullish()
        df['RisingThreeMethods'] = self.rising_three_methods()
        df['SeparatingLinesBullish'] = self.separating_lines_bullish()
        df['SideBySideWhiteLines'] = self.side_by_side_white_lines()
        df['ThreeWhiteSoldiers'] = self.three_white_soldiers()
        df['UpsideGapThreeMethods'] = self.upside_gap_three_methods()
        df['ThreeLineStrike'] = self.three_line_strike()
        df['TweezerBottom'] = self.tweezer_bottom()
        df['UpsideTasukiGap'] = self.upside_tasuki_gap()

        # Bearish / continuation patterns
        df['DownsideGapThreeMethods'] = self.downside_gap_three_methods()
        df['DownsideTasukiGap'] = self.downside_tasuki_gap()
        df['FallingThreeMethods'] = self.falling_three_methods()
        df['InNeckBearish'] = self.in_neck_bearish()
        df['OnNeckBearish'] = self.on_neck_bearish()
        df['SeparatingLinesBearish'] = self.separating_lines_bearish()
        df['SideBySideWhiteLinesBearish'] = self.side_by_side_white_lines_bearish()
        df['TwoCrows'] = self.two_crows()
        df['UpsideGapTwoCrows'] = self.upside_gap_two_crows()
        df['TriStarBearish'] = self.tri_star_bearish()
        df['ThreeBlackCrows'] = self.three_black_crows()
        df['ThreeLineStrikeBearish'] = self.three_line_strike()
        df['ThrustingBearish'] = self.thrusting_bearish()
        df['TweezerTop'] = self.tweezer_top()

        # Combined signals
        df['BuySignal'] = self.buy_signal()
        df['SellSignal'] = self.sell_signal()

        # commentary/status labels
        df['C_Status'] = self.c_status()
        df['P_Status'] = self.p_status()

        return df


# quick sanity test if run directly
if __name__ == "__main__":
    df_test = pd.read_csv('strategies/sample data.csv')
    # Sort the time column(timestamp)
    df_test['timestamp'] = pd.to_datetime(df_test['time'])
    df_test['date'] = df_test['timestamp'].dt.date    
    df_test = df_test.sort_values(by='timestamp')
    df_test = df_test.set_index('timestamp')
    cp = CandlePatterns(df_test)
    out = cp.build_all()
    out.to_csv('strategies/sample data_out.csv', index=False)
    # show a few columns
    #print(out[['BuySignal','SellSignal','C_Status','P_Status']].tail(10))