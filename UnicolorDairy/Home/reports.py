"""Report calculations: periods, performance numbers, day-by-day grid, task log and CSV."""
import calendar
import csv
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.utils import timezone

from .models import Task


def parse_month(value):
    """'2026-10' -> date(2026, 10, 1); falls back to the current month."""
    try:
        year, month = (int(x) for x in (value or "").split("-"))
        return date(year, month, 1)
    except (ValueError, TypeError):
        today = timezone.localdate()
        return today.replace(day=1)


def month_days(first_day):
    n = calendar.monthrange(first_day.year, first_day.month)[1]
    return [first_day.replace(day=d) for d in range(1, n + 1)]


@dataclass
class Summary:
    assigned: int = 0
    done: int = 0
    on_time: int = 0
    late: int = 0          # finished late + still open past due date
    changes: int = 0
    first_pass: int = 0    # done with no change requests
    open: int = 0
    overdue_now: int = 0   # not done and past due date
    turnaround_hours: list = field(default_factory=list)  # assigned -> approved

    @property
    def pct(self):
        return round(self.done * 100 / self.assigned) if self.assigned else 0

    @property
    def on_time_pct(self):
        return round(self.on_time * 100 / self.done) if self.done else None

    @property
    def first_pass_pct(self):
        return round(self.first_pass * 100 / self.done) if self.done else None

    @property
    def avg_turnaround(self):
        h = self.turnaround_hours
        return sum(h) / len(h) if h else None

    @property
    def turnaround_label(self):
        h = self.avg_turnaround
        if h is None:
            return None
        if h < 1:
            return "< 1h"
        if h < 24:
            return f"{h:.0f}h"
        return f"{h / 24:.1f} days"


def summarize(tasks):
    s = Summary()
    today = timezone.localdate()
    for t in tasks:
        s.assigned += 1
        s.changes += t.change_count
        if t.status == Task.Status.DONE:
            s.done += 1
            if t.finished_late:
                s.late += 1
            else:
                s.on_time += 1
            if not t.change_count:
                s.first_pass += 1
            if t.done_at and t.created_at:
                s.turnaround_hours.append((t.done_at - t.created_at).total_seconds() / 3600)
        else:
            s.open += 1
            if t.due_date < today:
                s.late += 1
                s.overdue_now += 1
    return s


# ---------------------------------------------------------------- periods

MAX_GRID_DAYS = 62
MAX_RANGE_DAYS = 366


def _parse(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def parse_period(params):
    """Work out the report period from the page's filters.

    Returns a dict: key, start, end, label, month (first day, for the month picker), prev_start, prev_end.
    """
    today = timezone.localdate()
    key = params.get("period", "month")
    if key == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif key == "7d":
        start, end = today - timedelta(days=6), today
    elif key == "30d":
        start, end = today - timedelta(days=29), today
    elif key == "quarter":
        q_first_month = (today.month - 1) // 3 * 3 + 1
        start = today.replace(month=q_first_month, day=1)
        end = (start.replace(month=q_first_month + 2, day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    elif key == "custom" and _parse(params.get("start")) and _parse(params.get("end")):
        start, end = sorted([_parse(params.get("start")), _parse(params.get("end"))])
        if (end - start).days >= MAX_RANGE_DAYS:
            start = end - timedelta(days=MAX_RANGE_DAYS - 1)
    else:
        key = "month"
        start = parse_month(params.get("month"))
        end = month_days(start)[-1]

    length = (end - start).days + 1
    whole_month = start.day == 1 and end == month_days(start)[-1]
    if whole_month:
        prev_end = start - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
        label = f"{start:%B %Y}"
    else:
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=length - 1)
        label = f"{start:%d %b} – {end:%d %b %Y}" if start.year == end.year else f"{start:%d %b %Y} – {end:%d %b %Y}"
    return {
        "key": key, "start": start, "end": end, "label": label, "length": length,
        "month": start.replace(day=1), "prev_start": prev_start, "prev_end": prev_end,
    }


def days_between(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def delta(current, previous):
    """Change between two percentages, for the ▲ / ▼ indicator. None when either is missing."""
    if current is None or previous is None:
        return None
    d = current - previous
    return {"value": abs(d), "dir": "up" if d > 0 else "down" if d < 0 else "same"}


def status_breakdown(tasks):
    """Share of tasks in each status, for the stacked bar."""
    total = len(tasks) or 1
    out = []
    for value, label in Task.Status.choices:
        n = sum(1 for t in tasks if t.status == value)
        out.append({"status": value, "label": label, "count": n, "pct": round(n * 100 / total, 1)})
    return out


def daily_counts(tasks, days):
    """Per day: tasks due and how many of them are done (for the bar chart)."""
    by_day = {d: [0, 0] for d in days}
    for t in tasks:
        if t.due_date in by_day:
            by_day[t.due_date][0] += 1
            if t.status == Task.Status.DONE:
                by_day[t.due_date][1] += 1
    top = max([v[0] for v in by_day.values()] + [1])
    return [
        {"day": d, "due": due, "done": done,
         "h_due": round(due * 100 / top), "h_done": round(done * 100 / top)}
        for d, (due, done) in by_day.items()
    ]


@dataclass
class Cell:
    text: str = ""
    css: str = "none"
    style: str = ""
    title: str = ""


@dataclass
class GridRow:
    user: object
    cells: list = field(default_factory=list)
    summary: Summary = field(default_factory=Summary)


def cell_for(tasks, day):
    if not tasks:
        return Cell()
    total = len(tasks)
    done = sum(1 for t in tasks if t.status == Task.Status.DONE)
    pct = round(done * 100 / total)
    title = f"{day:%d %b}: {done} of {total} done"
    if done == total:
        return Cell(f"{done}/{total}", "all", "", title)
    if day < timezone.localdate():
        return Cell(f"{done}/{total}", "miss", f"--mix:{35 + pct * 3 // 10}%", title)
    return Cell(f"{done}/{total}", "part", f"--mix:{15 + pct * 6 // 10}%", title)


def build_grid(users, tasks, days):
    by_user_day = {}
    for t in tasks:
        by_user_day.setdefault((t.assignee_id, t.due_date), []).append(t)
    rows = []
    for u in users:
        user_tasks = [t for t in tasks if t.assignee_id == u.id]
        row = GridRow(user=u, summary=summarize(user_tasks))
        row.cells = [cell_for(by_user_day.get((u.id, d), []), d) for d in days]
        rows.append(row)
    return rows


def day_log(tasks, days):
    """Newest day first; only days that have tasks."""
    out = []
    for d in reversed(days):
        items = sorted(
            (t for t in tasks if t.due_date == d),
            key=lambda t: (Task.PRIORITY_ORDER.get(t.priority, 1), t.created_at),
        )
        if items:
            out.append({"day": d, "tasks": items, "summary": summarize(items)})
    return out


def write_csv(response, tasks):
    response.write("﻿")  # BOM so Excel opens UTF-8 correctly
    w = csv.writer(response)
    w.writerow([
        "Due date", "Member", "Task", "Priority", "Status", "Assigned on", "Submitted", "Approved",
        "Approved by", "On time", "Change requests", "Turnaround (hours)", "Member note", "Admin note",
    ])
    fmt = lambda dt: timezone.localtime(dt).strftime("%d-%m-%Y %H:%M") if dt else ""
    for t in tasks:
        if t.status == Task.Status.DONE:
            on_time = "Yes" if t.on_time else "No"
        else:
            on_time = "No (overdue)" if t.is_late else ""
        hours = round((t.done_at - t.created_at).total_seconds() / 3600, 1) if t.done_at and t.created_at else ""
        w.writerow([
            t.due_date.strftime("%d-%m-%Y"), t.assignee.display_name, t.title, t.get_priority_display(),
            t.get_status_display(), fmt(t.created_at), fmt(t.submitted_at), fmt(t.done_at),
            t.approved_by.display_name if t.approved_by else "", on_time, t.change_count, hours,
            t.member_note, t.admin_note,
        ])
    return response