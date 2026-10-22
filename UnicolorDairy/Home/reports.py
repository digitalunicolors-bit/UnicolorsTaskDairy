"""Monthly, day-by-day report calculations."""
import calendar
import csv
from dataclasses import dataclass, field
from datetime import date

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
    late: int = 0
    changes: int = 0

    @property
    def pct(self):
        return round(self.done * 100 / self.assigned) if self.assigned else 0


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
        elif t.due_date < today:
            s.late += 1
    return s


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
        "Due date", "Member", "Task", "Priority", "Status", "Submitted", "Approved",
        "Approved by", "On time", "Change requests", "Member note", "Admin note",
    ])
    fmt = lambda dt: timezone.localtime(dt).strftime("%d-%m-%Y %H:%M") if dt else ""
    for t in tasks:
        if t.status == Task.Status.DONE:
            on_time = "Yes" if t.on_time else "No"
        else:
            on_time = "No (overdue)" if t.is_late else ""
        w.writerow([
            t.due_date.strftime("%d-%m-%Y"), t.assignee.display_name, t.title, t.get_priority_display(),
            t.get_status_display(), fmt(t.submitted_at), fmt(t.done_at),
            t.approved_by.display_name if t.approved_by else "", on_time, t.change_count,
            t.member_note, t.admin_note,
        ])
    return response
