"""
ガントチャート上でのタスク編集（docs/roadmap.md §9）のうち、Qtに依存しない部分。

- 計算結果の文字列ID（"JOB_012" / "T_034" / "TEAM_003"）とDBの整数IDの相互変換
- 営業日の計算（WorkDayCalendar）。ドラッグでの移動・伸縮を営業日単位で吸着させ、
  「+10営業日」のようにずれを数えるために使う

営業日の定義はスケジューラ（project_scheduler._WorkCalendar）と同じにする:
土日・日本の祝日・全チーム共通の休業日・そのチームの休業日を除いた日。
日付はすべて datetime.date で扱い、終了日は exclusive（その日を含まない。
スケジューリング結果の End_Date と同じ）。
"""

from datetime import date, timedelta

# 休業日が延々と続くデータ（誤入力等）でも無限ループしないための上限（日数）。
_MAX_SCAN_DAYS = 3660


def format_entity_id(prefix, entity_id, width=3):
    """12 → "JOB_012"。計算結果の文字列IDの形（gantt_generator が使う）。"""
    return f"{prefix}_{entity_id:0{width}d}"


def parse_entity_id(key):
    """"JOB_012" → 12。計算結果の文字列IDは format_entity_id が作る
    "<接頭辞>_<整数ID>" の形なので、最後の "_" 以降を整数として読む。"""
    return int(str(key).rsplit("_", 1)[1])


class WorkDayCalendar:
    def __init__(self, common_holidays=(), holidays_by_team=None, jp_holidays=(),
                 exclude_weekends=True):
        self._common = set(common_holidays) | set(jp_holidays)
        self._by_team = {k: set(v) for k, v in (holidays_by_team or {}).items()}
        self._exclude_weekends = exclude_weekends

    @classmethod
    def from_display(cls, display):
        """gantt_generator.build_display() の戻り値から作る。"""
        return cls(
            common_holidays=display.get("common_holiday_dates") or (),
            holidays_by_team=display.get("holidays_by_team") or {},
            jp_holidays=display.get("jp_holiday_dates") or (),
        )

    def is_working(self, d, team_key=None):
        if self._exclude_weekends and d.weekday() >= 5:
            return False
        if d in self._common:
            return False
        return d not in self._by_team.get(team_key, ())

    def next_working(self, d, team_key=None):
        """d が稼働日なら d、そうでなければその後の最初の稼働日。"""
        for _ in range(_MAX_SCAN_DAYS):
            if self.is_working(d, team_key):
                return d
            d += timedelta(days=1)
        return d

    def shift(self, d, n, team_key=None):
        """d（稼働日に寄せてから）から営業日で n 日ずらした稼働日。n は負でもよい。"""
        d = self.next_working(d, team_key)
        step = timedelta(days=1 if n >= 0 else -1)
        remaining = abs(n)
        for _ in range(_MAX_SCAN_DAYS):
            if remaining == 0:
                return d
            d += step
            if self.is_working(d, team_key):
                remaining -= 1
        return d

    def diff(self, a, b, team_key=None):
        """a から b まで営業日で何日ずれているか（b が後なら正）。どちらも稼働日に
        寄せてから数える。shift(a, diff(a, b)) == next_working(b) になる。"""
        a = self.next_working(a, team_key)
        b = self.next_working(b, team_key)
        if a == b:
            return 0
        sign = 1 if b > a else -1
        lo, hi = (a, b) if sign > 0 else (b, a)
        count = 0
        d = lo
        for _ in range(_MAX_SCAN_DAYS):
            if d >= hi:
                break
            d += timedelta(days=1)
            if self.is_working(d, team_key):
                count += 1
        return sign * count

    def count(self, start, end_exclusive, team_key=None):
        """[start, end_exclusive) に含まれる稼働日の数。"""
        n = 0
        d = start
        for _ in range(_MAX_SCAN_DAYS):
            if d >= end_exclusive:
                break
            if self.is_working(d, team_key):
                n += 1
            d += timedelta(days=1)
        return n

    def end_exclusive(self, start, days, team_key=None):
        """start（稼働日に寄せてから）から営業日で days 日かかるタスクの終了日
        （exclusive＝最後の稼働日の翌日）。days は1以上。"""
        last = self.shift(start, max(days, 1) - 1, team_key)
        return last + timedelta(days=1)


def to_date(value):
    """pd.Timestamp / datetime / date を date にそろえる。"""
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    return value.date()
