"""Aggregations over already-decoded records. Pure, no I/O.

Every number produced here is DERIVED, and says how: the consumer is an LLM
that will restate it as fact, so the method travels with the value. Three
rules the calculations follow:

* Pace over a period is total distance over total moving time -- never the
  mean of per-workout paces, which over-weights short slow sessions.
* Heart rate over a period is weighted by moving time, for the same reason.
* Nothing is modelled. There is no home-grown training-load, recovery or
  fitness score here; Zepp's own figures are summed or averaged, and only
  where summing is meaningful (exercise load is additive; Training Effect is
  not, so it is averaged).
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Callable
from typing import Any

from .sports import DISTANCE_FAMILIES


def _family(workout: dict[str, Any]) -> str | None:
    return workout.get("sport_family")


def _paced(workout: dict[str, Any]) -> bool:
    """Pace means something for this workout's sport family."""
    return _family(workout) in DISTANCE_FAMILIES


def _get(workout: dict[str, Any], path: str) -> Any:
    node: Any = workout
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _date(workout: dict[str, Any]) -> dt.date | None:
    start = workout.get("start_local")
    try:
        return dt.date.fromisoformat(start[:10]) if start else None
    except ValueError:
        return None


def period_key(day: dt.date, group_by: str) -> str:
    if group_by == "week":
        year, week, _ = day.isocalendar()
        return f"{year}-W{week:02d}"
    if group_by == "month":
        return f"{day.year}-{day.month:02d}"
    if group_by == "year":
        return str(day.year)
    return "all"


def totals(workouts: list[dict[str, Any]]) -> dict[str, Any]:
    """Volume and intensity for a set of workouts."""
    distance = sum(_num(_get(w, "summary.distance_metres")) or 0 for w in workouts)
    moving = sum(
        _num(_get(w, "summary.moving_time_seconds"))
        or _num(w.get("elapsed_seconds"))
        or 0
        for w in workouts
    )
    calories = sum(_num(_get(w, "summary.calorie")) or 0 for w in workouts)
    load = [_num(_get(w, "summary.exercise_load")) for w in workouts]
    load = [v for v in load if v is not None]

    hr_weighted = [
        (
            _num(_get(w, "summary.avg_heart_rate")),
            _num(_get(w, "summary.moving_time_seconds"))
            or _num(w.get("elapsed_seconds")),
        )
        for w in workouts
    ]
    hr_weighted = [(hr, t) for hr, t in hr_weighted if hr and t]

    out: dict[str, Any] = {
        "workouts": len(workouts),
        "moving_time_hours": round(moving / 3600, 2),
        "calories_kcal": round(calories),
    }
    if distance:
        out["distance_km"] = round(distance / 1000, 2)
    if load:
        out["exercise_load_total"] = round(sum(load))
        out["exercise_load_sessions"] = len(load)
    if hr_weighted:
        weight = sum(t for _, t in hr_weighted)
        out["avg_heart_rate_bpm"] = round(sum(hr * t for hr, t in hr_weighted) / weight)
        out["avg_heart_rate_method"] = (
            "derived: mean of per-workout avg HR weighted by moving time"
        )
    climb = sum(
        _num(_get(w, "foot.elevation_gain_metres"))
        or _num(_get(w, "hike.elevation_gain_metres"))
        or _num(_get(w, "ride.elevation_gain_metres"))
        or 0
        for w in workouts
    )
    if climb:
        out["elevation_gain_metres"] = round(climb)

    paced = [
        w
        for w in workouts
        if _paced(w)
        and _num(_get(w, "summary.distance_metres"))
        and _num(_get(w, "summary.moving_time_seconds"))
    ]
    # One family only: running and trail running share a pace, a run and a
    # swim do not.
    families = {_family(w) for w in paced}
    if paced and len(families) == 1:
        d = sum(_num(_get(w, "summary.distance_metres")) or 0 for w in paced)
        t = sum(_num(_get(w, "summary.moving_time_seconds")) or 0 for w in paced)
        if next(iter(families)) == "swimming":
            per = t / d * 100
            out["avg_pace"] = f"{int(per) // 60}:{int(per) % 60:02d}/100m"
            out["avg_pace_sec_per_100m"] = round(per, 1)
        else:
            per = t / d * 1000
            out["avg_pace"] = f"{int(per) // 60}:{int(per) % 60:02d}/km"
            out["avg_pace_sec_per_km"] = round(per, 1)
        out["avg_pace_method"] = "derived: total distance / total moving time"
    return out


def grouped(workouts: list[dict[str, Any]], group_by: str) -> list[dict[str, Any]]:
    """Totals per period (week/month/year) or per sport."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for workout in workouts:
        if group_by == "sport":
            key = workout.get("sport") or "unknown"
        elif group_by == "sport_family":
            key = _family(workout) or "unknown"
        else:
            day = _date(workout)
            if day is None:
                continue
            key = period_key(day, group_by)
        buckets.setdefault(key, []).append(workout)
    out = []
    for key in sorted(buckets):
        entry = {"group": key, **totals(buckets[key])}
        if group_by not in ("sport", "sport_family"):
            entry["by_sport"] = {
                sport: len([w for w in buckets[key] if w.get("sport") == sport])
                for sport in sorted({w.get("sport") for w in buckets[key]})
            }
        out.append(entry)
    return out


# Metrics a caller can trend per workout: name -> (path, unit, lower_is_better)
TREND_METRICS: dict[str, tuple[str, str, bool]] = {
    "distance": ("summary.distance_metres", "metres", False),
    "moving_time": ("summary.moving_time_seconds", "seconds", False),
    "pace": ("derived.pace_sec_per_km", "seconds_per_km", True),
    "swim_pace": ("derived.pace_sec_per_100m", "seconds_per_100m", True),
    "avg_heart_rate": ("summary.avg_heart_rate", "bpm", False),
    "max_heart_rate": ("summary.max_heart_rate", "bpm", False),
    "cadence": ("summary.avg_cadence_per_minute", "per_minute", False),
    "exercise_load": ("summary.exercise_load", "zepp_load_units", False),
    "aerobic_training_effect": ("summary.aerobic_training_effect", "0-5", False),
    "vo2_max": ("summary.VO2_max", "ml/kg/min", False),
    "calories": ("summary.calorie", "kcal", False),
    "swolf": ("swim.swolf", "swolf", True),
    "strokes": ("swim.total_strokes", "strokes", False),
    "distance_per_stroke": ("swim.avg_distance_per_stroke", "metres", False),
    "stride_length": ("foot.avg_stride_length", "cm", False),
    "ground_contact_time": (
        "foot.running_dynamics.avg_ground_contact_time_ms",
        "ms",
        True,
    ),
    "vertical_oscillation": (
        "foot.running_dynamics.avg_vertical_oscillation_mm",
        "mm",
        True,
    ),
    "power": ("summary.avg_power_watts", "watts", False),
    "elevation_gain": ("hike.elevation_gain_metres", "metres", False),
    "total_reps": ("strength.sets.total_reps", "reps", False),
}


def linear_trend(points: list[tuple[dt.date, float]]) -> dict[str, Any] | None:
    """Least-squares slope per 30 days over dated values. DERIVED."""
    if len(points) < 3:
        return None
    origin = min(d for d, _ in points)
    xs = [(d - origin).days for d, _ in points]
    ys = [v for _, v in points]
    if len(set(xs)) < 2:
        return None
    slope = statistics.linear_regression(xs, ys).slope
    return {
        "change_per_30_days": round(slope * 30, 3),
        "first_value": ys[0],
        "last_value": ys[-1],
        "points": len(points),
        "method": "derived: least-squares linear fit over the dated values",
    }


def series(workouts: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    path, unit, lower_better = TREND_METRICS[metric]
    rows = []
    for workout in sorted(workouts, key=lambda w: w.get("start_local") or ""):
        value = _num(_get(workout, path))
        day = _date(workout)
        if value is None or day is None:
            continue
        rows.append(
            {
                "date": day.isoformat(),
                "track_id": workout.get("track_id"),
                "sport": workout.get("sport"),
                "value": value,
            }
        )
    out: dict[str, Any] = {
        "metric": metric,
        "unit": unit,
        "lower_is_better": lower_better,
        "values": rows,
    }
    trend = linear_trend([(dt.date.fromisoformat(r["date"]), r["value"]) for r in rows])
    if trend:
        out["trend"] = trend
    if rows:
        best = (min if lower_better else max)(rows, key=lambda r: r["value"])
        out["best"] = best
        out["mean"] = round(statistics.fmean(r["value"] for r in rows), 2)
    return out


def _record(
    workouts: list[dict[str, Any]],
    path: str,
    better: Callable[[float, float], bool],
    minimum: Callable[[dict[str, Any]], bool] = lambda w: True,
) -> dict[str, Any] | None:
    best: tuple[float, dict[str, Any]] | None = None
    for workout in workouts:
        value = _num(_get(workout, path))
        if value is None or not minimum(workout):
            continue
        if best is None or better(value, best[0]):
            best = (value, workout)
    if best is None:
        return None
    return {
        "value": best[0],
        "date": (best[1].get("start_local") or "")[:10],
        "track_id": best[1].get("track_id"),
        "sport": best[1].get("sport"),
    }


def records(workouts: list[dict[str, Any]]) -> dict[str, Any]:
    """Bests within the queried range, per sport, computed from the rows,
    plus the personal bests Zepp itself flagged on individual workouts."""
    higher = lambda a, b: a > b  # noqa: E731
    lower = lambda a, b: a < b  # noqa: E731
    out: dict[str, Any] = {}
    for sport in sorted({w.get("sport") for w in workouts if w.get("sport")}):
        subset = [w for w in workouts if w.get("sport") == sport]
        entry: dict[str, Any] = {}
        for name, path, cmp in (
            ("longest_moving_time_seconds", "summary.moving_time_seconds", higher),
            ("longest_distance_metres", "summary.distance_metres", higher),
            ("highest_exercise_load", "summary.exercise_load", higher),
            ("highest_avg_heart_rate_bpm", "summary.avg_heart_rate", higher),
            ("most_calories_kcal", "summary.calorie", higher),
            ("best_swolf", "swim.swolf", lower),
            ("most_reps", "strength.sets.total_reps", higher),
            ("most_jumps", "rope_skipping.total_jumps", higher),
        ):
            found = _record(subset, path, cmp)
            if found:
                entry[name] = found
        if _paced(subset[0]):
            # Fastest pace only counts sessions of at least 1 km (100 m for a
            # swim), so a 200 m warm-up jog does not become the "record".
            swim = _family(subset[0]) == "swimming"
            path = "derived.pace_sec_per_100m" if swim else "derived.pace_sec_per_km"
            floor = 100 if swim else 1000
            found = _record(
                subset,
                path,
                lower,
                lambda w, floor=floor: (
                    (_num(_get(w, "summary.distance_metres")) or 0) >= floor
                ),
            )
            if found:
                entry[
                    "fastest_avg_pace_" + ("sec_per_100m" if swim else "sec_per_km")
                ] = found
        if entry:
            out[sport] = entry

    flagged: dict[str, dict[str, Any]] = {}
    for workout in workouts:
        for key, value in (workout.get("personal_bests") or {}).items():
            number = _num(value)
            if number is None:
                continue
            # Time-for-a-distance keys (run_3_km) are better when lower;
            # longest/furthest/most/highest/maximum keys when higher.
            lower_better = not any(
                w in key
                for w in ("longest", "furthest", "most", "highest", "maximum", "max")
            )
            current = flagged.get(key)
            if current is None or (
                number < current["value"] if lower_better else number > current["value"]
            ):
                flagged[key] = {
                    "value": number,
                    "date": (workout.get("start_local") or "")[:10],
                    "track_id": workout.get("track_id"),
                }
    if flagged:
        out["zepp_flagged_personal_bests"] = flagged
    return {
        "within_range": out,
        "note": "Computed from workouts in the queried range only -- an "
        "all-time record needs an all-time range. "
        "zepp_flagged_personal_bests are the pb values Zepp attached "
        "to individual workouts; keys like run_3_km are seconds for "
        "that distance.",
    }


def daily_trend(days: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    points = []
    for day in days:
        value = _num(day.get(key))
        try:
            when = dt.date.fromisoformat(str(day.get("date"))[:10])
        except ValueError:
            continue
        if value is not None:
            points.append((when, value))
    return linear_trend(points)


def mean_of(rows: list[dict[str, Any]], key: str, digits: int = 1) -> float | None:
    values = [_num(r.get(key)) for r in rows]
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), digits) if values else None
