import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import indian_holidays, recurring, reminders, reports, summaries
from .models import (
    Client, EmailFailure, ExtensionRequest, Holiday, Leave, LoginAttempt, Notification, RecurringTask, Subtask, Task,
    TaskComment, TaskRequest, User, is_office_closed, next_working_day, on_leave_ids,
)


class FlowTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pass12345!", role=User.Role.ADMIN, first_name="Boss")
        self.admin2 = User.objects.create_user("boss2", password="pass12345!", role=User.Role.ADMIN)
        self.yashi = User.objects.create_user("yashi", password="pass12345!", first_name="Jain", last_name="Yashi")
        self.niyati = User.objects.create_user("niyati", password="pass12345!")
        self.today = timezone.localdate()

    def login(self, user):
        self.client.cookies.clear()  # a new person, a new browser: no leftover messages
        self.client.force_login(user)

    def assign(self, *users, title="Diwali post"):
        self.login(self.admin)
        return self.client.post(reverse("assign_task"), {
            "title": title, "priority": "high", "due_date": self.today.isoformat(),
            "assignees": [u.id for u in users], "details": "",
        })

    def act(self, user, task, action, note=""):
        self.login(user)
        return self.client.post(reverse("task_action", args=[task.pk, action]), {"note": note, "next": "/"})

    def test_full_workflow_with_change_request(self):
        self.assign(self.yashi)
        task = Task.objects.get()
        self.act(self.yashi, task, "start")
        self.act(self.yashi, task, "submit", "link here")
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.REVIEW)

        self.act(self.admin2, task, "changes", "bigger logo")  # a second admin can act too
        task.refresh_from_db()
        self.assertEqual((task.status, task.change_count, task.admin_note), (Task.Status.CHANGE, 1, "bigger logo"))

        self.act(self.yashi, task, "submit")
        self.act(self.admin, task, "approve")
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.DONE)
        self.assertEqual(task.approved_by, self.admin)

    def test_assign_to_multiple_people(self):
        self.assign(self.yashi, self.niyati)
        self.assertEqual(Task.objects.count(), 2)

    def test_member_cannot_touch_others_or_admin_actions(self):
        self.assign(self.yashi)
        task = Task.objects.get()
        self.assertEqual(self.act(self.niyati, task, "start").status_code, 403)
        self.act(self.yashi, task, "submit")
        self.assertEqual(self.act(self.yashi, task, "approve").status_code, 403)
        self.login(self.niyati)
        # Members are sent back to their own tasks rather than seeing admin pages.
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("my_tasks"))
        self.assertRedirects(self.client.get(reverse("team")), reverse("my_tasks"))

    def test_member_sees_only_own_tasks(self):
        self.assign(self.yashi, title="For Yashi")
        self.assign(self.niyati, title="For Niyati")
        self.login(self.niyati)
        html = self.client.get(reverse("my_tasks")).content.decode()
        self.assertIn("For Niyati", html)
        self.assertNotIn("For Yashi", html)
        # Report is forced to their own data even if they ask for another member
        html = self.client.get(reverse("report"), {"member": self.yashi.id}).content.decode()
        self.assertNotIn("For Yashi", html)

    def test_reports_and_csv(self):
        self.assign(self.yashi)
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("report")).status_code, 200)
        resp = self.client.get(reverse("report_csv"))
        self.assertIn("Diwali post", resp.content.decode("utf-8-sig"))

    def test_overdue_counts_as_late(self):
        Task.objects.create(title="Old", due_date=self.today - timedelta(days=2), assignee=self.yashi)
        self.assertTrue(Task.objects.get().is_late)

    def test_admin_can_add_people_and_cannot_demote_self(self):
        self.login(self.admin)
        self.client.post(reverse("user_add"), {
            "first_name": "New", "last_name": "Lead", "username": "newlead", "email": "lead@example.com",
            "designation": "Manager", "role": "manager",
        })
        self.assertTrue(User.objects.get(username="newlead").is_lead)
        self.client.post(reverse("user_edit", args=[self.admin.pk]), {
            "first_name": "Boss", "last_name": "", "email": "boss@example.com", "designation": "", "role": "member", "is_active": "on",
        })
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_admin)

    def test_pages_render(self):
        self.assign(self.yashi)
        for user, names in [(self.admin, ["dashboard", "my_tasks", "report", "team", "user_add", "clients", "calendar", "archive", "recurring", "search"]), (self.yashi, ["my_tasks", "report", "search"])]:
            self.login(user)
            for name in names:
                self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)


class CorporateTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser("owner", "owner@example.com", "pass12345!")
        self.admin = User.objects.create_user("boss", "boss@example.com", "pass12345!", role=User.Role.ADMIN)
        self.admin2 = User.objects.create_user("boss2", "boss2@example.com", "pass12345!", role=User.Role.ADMIN)
        self.lead = User.objects.create_user("lead", "lead@example.com", "pass12345!", role=User.Role.MANAGER)
        self.yashi = User.objects.create_user("yashi", "yashi@example.com", "pass12345!", manager=self.lead)
        self.niyati = User.objects.create_user("niyati", "niyati@example.com", "pass12345!")
        self.today = timezone.localdate()
        self.now = timezone.now()

    def task(self, **kw):
        kw.setdefault("title", "Post")
        kw.setdefault("due_date", self.today)
        kw.setdefault("assignee", self.yashi)
        return Task.objects.create(**kw)

    def post(self, user, name, *args, data=None, **kw):
        self.client.force_login(user)
        return self.client.post(reverse(name, args=args), data or {}, **kw)

    def act(self, user, task, action, **data):
        return self.post(user, "task_action", task.pk, action, data={"next": "/", **data})

    # ---- the numbers

    def test_on_time_uses_submission_not_approval(self):
        t = self.task(due_date=self.today - timedelta(days=2), status="review", submitted_at=self.now - timedelta(days=3))
        self.act(self.admin, t, "approve")
        t.refresh_from_db()
        self.assertEqual(t.status, "done")
        self.assertFalse(t.finished_late)
        self.assertEqual(reports.summarize([t]).on_time, 1)

    def test_late_submission_is_late(self):
        t = self.task(due_date=self.today - timedelta(days=2), status="done", submitted_at=self.now, done_at=self.now)
        self.assertTrue(t.finished_late)

    def test_moving_a_passed_deadline_keeps_the_lateness(self):
        old = self.today - timedelta(days=5)
        t = self.task(due_date=old)
        self.post(self.admin, "task_edit", t.pk, data={
            "title": t.title, "priority": t.priority, "due_date": self.today.isoformat(), "assignee": t.assignee_id, "details": "", "next": "/",
        })
        t.refresh_from_db()
        self.assertEqual((t.due_date, t.score_due_date, t.original_due_date), (self.today, old, old))
        t.status, t.submitted_at, t.done_at = "done", self.now, self.now
        self.assertTrue(t.finished_late)

    def test_rescheduling_before_the_deadline_is_a_new_plan(self):
        t = self.task(due_date=self.today + timedelta(days=2))
        new = self.today + timedelta(days=5)
        self.post(self.admin, "task_edit", t.pk, data={
            "title": t.title, "priority": t.priority, "due_date": new.isoformat(), "assignee": t.assignee_id, "details": "", "next": "/",
        })
        t.refresh_from_db()
        self.assertEqual(t.score_due_date, new)
        self.assertEqual(t.original_due_date, self.today + timedelta(days=2))

    def test_waiting_for_approval_or_on_hold_is_not_overdue_on_any_page(self):
        past = self.today - timedelta(days=1)
        a = self.task(due_date=past, status="review", submitted_at=self.now - timedelta(days=3))
        b = self.task(due_date=past, status="hold", title="Held")
        for t in (a, b):
            self.assertFalse(t.is_late)
        self.assertEqual(reports.summarize([a, b]).overdue_now, 0)
        self.client.force_login(self.admin)
        rows = {r["user"].pk: r for r in self.client.get(reverse("team")).context["rows"]}
        self.assertEqual(rows[self.yashi.pk]["overdue"], 0)
        self.assertEqual(self.client.get(reverse("dashboard")).context["focus"]["overdue"], 0)

    # ---- missed days

    def test_recurring_catches_up_after_the_site_slept(self):
        monday = self.today - timedelta(days=self.today.weekday() + 7)
        rule = RecurringTask.objects.create(title="R", frequency="weekdays", start_date=monday - timedelta(days=30), created_by=self.admin)
        RecurringTask.objects.filter(pk=rule.pk).update(created_at=self.now - timedelta(days=30), last_run_on=monday)
        rule.refresh_from_db()
        rule.assignees.add(self.yashi)
        made = recurring.create_recurring_tasks(self.today)
        expected = sum(1 for i in range(1, (self.today - monday).days + 1) if (monday + timedelta(days=i)).weekday() < 5)
        self.assertEqual(made, expected)
        self.assertEqual(recurring.create_recurring_tasks(self.today), 0)
        for t in Task.objects.filter(recurring=rule):
            self.assertGreaterEqual(t.due_date, self.today)  # nothing is born overdue

    def test_resuming_a_paused_schedule_does_not_backfill(self):
        rule = RecurringTask.objects.create(title="R", frequency="weekly", weekdays=str((self.today - timedelta(days=2)).weekday()), start_date=self.today - timedelta(days=30), is_active=False, created_by=self.admin)
        RecurringTask.objects.filter(pk=rule.pk).update(created_at=self.now - timedelta(days=30), last_run_on=self.today - timedelta(days=20))
        rule.assignees.add(self.yashi)
        self.post(self.admin, "recurring_toggle", rule.pk)
        self.assertEqual(recurring.create_recurring_tasks(self.today), 0)

    def test_every_working_day_skips_closed_days_and_leave(self):
        sunday = self.today + timedelta(days=(6 - self.today.weekday()) or 7)
        rule = RecurringTask.objects.create(title="R", frequency="daily", start_date=self.today - timedelta(days=1))
        self.assertFalse(rule.runs_on(sunday))
        monday = sunday + timedelta(days=1)
        Holiday.objects.create(date=monday, name="Holiday")
        self.assertFalse(rule.runs_on(monday))
        self.assertEqual(next_working_day(sunday), monday + timedelta(days=1))
        rule2 = RecurringTask.objects.create(title="L", frequency="weekly", weekdays=str(self.today.weekday()), start_date=self.today, created_by=self.admin)
        RecurringTask.objects.filter(pk=rule2.pk).update(created_at=self.now)
        rule2.refresh_from_db()
        rule2.assignees.add(self.yashi, self.niyati)
        Leave.objects.create(user=self.yashi, start_date=self.today, end_date=self.today)
        recurring.create_recurring_tasks(self.today, rules=[rule2])
        self.assertEqual([t.assignee for t in Task.objects.filter(recurring=rule2)], [self.niyati])
        self.assertTrue(Notification.objects.filter(recipient=self.admin, message__contains="on leave").exists())

    def test_week_reminder_has_a_window_but_only_for_old_tasks(self):
        old = self.task(due_date=self.today + timedelta(days=5), title="Old")
        Task.objects.filter(pk=old.pk).update(created_at=self.now - timedelta(days=10))
        self.task(due_date=self.today + timedelta(days=5), title="New")
        self.assertEqual(reminders.send_due_reminders(self.today), 1)
        self.assertEqual(reminders.send_due_reminders(self.today), 0)
        self.assertIn("in 5 days", mail.outbox[-1].subject)

    # ---- accountability

    def test_archive_keeps_everything_and_restores(self):
        t = self.task()
        TaskComment.objects.create(task=t, author=self.yashi, body="hello")
        self.act(self.admin, t, "delete")
        self.assertFalse(Task.objects.filter(pk=t.pk).exists())
        archived = Task.all_objects.get(pk=t.pk)
        self.assertEqual(archived.comments.count(), 1)
        self.assertTrue(archived.activity.filter(action="archived").exists())
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("archive")), t.title)
        self.post(self.admin, "archive_restore", t.pk)
        self.assertTrue(Task.objects.filter(pk=t.pk).exists())

    def test_member_cannot_archive_or_restore(self):
        t = self.task()
        self.assertEqual(self.act(self.yashi, t, "delete").status_code, 403)
        self.act(self.admin, t, "delete")
        self.post(self.yashi, "archive_restore", t.pk)
        self.assertFalse(Task.objects.filter(pk=t.pk).exists())

    def test_deleted_comment_leaves_a_trace(self):
        t = self.task()
        c = TaskComment.objects.create(task=t, author=self.yashi, body="secret words")
        self.post(self.yashi, "task_comment_delete", c.pk)
        c.refresh_from_db()
        self.assertIsNotNone(c.deleted_at)
        self.assertEqual(c.body, "secret words")
        self.assertTrue(t.activity.filter(action="comment_del", note__contains="secret words").exists())
        self.client.force_login(self.yashi)
        self.assertNotContains(self.client.get(reverse("task_detail", args=[t.pk])), "secret words")
        self.assertEqual(self.client.get(reverse("search"), {"q": "secret"}).context["results"], [])
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("task_detail", args=[t.pk])), "secret words")

    def test_no_self_approval(self):
        t = self.task(assignee=self.admin, status="review", submitted_at=self.now)
        self.act(self.admin, t, "approve")
        t.refresh_from_db()
        self.assertEqual(t.status, "review")
        self.act(self.admin2, t, "approve")
        t.refresh_from_db()
        self.assertEqual(t.status, "done")

    def test_only_admin_can_self_approve_when_nobody_else_can(self):
        User.objects.exclude(pk=self.admin.pk).update(is_active=False)
        t = self.task(assignee=self.admin, status="review", submitted_at=self.now)
        self.assertTrue(t.can_approve(self.admin))

    def test_admins_cannot_touch_other_admins(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("user_password", args=[self.admin2.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("user_edit", args=[self.owner.pk])).status_code, 403)
        self.assertEqual(self.post(self.admin, "user_toggle_active", self.admin2.pk).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("user_password", args=[self.admin2.pk])).status_code, 200)

    def test_only_owner_can_make_admins(self):
        self.post(self.admin, "user_add", data={"first_name": "A", "last_name": "B", "username": "x1", "email": "x1@example.com", "role": "admin"})
        self.assertFalse(User.objects.filter(username="x1").exists())
        self.post(self.owner, "user_add", data={"first_name": "A", "last_name": "B", "username": "x2", "email": "x2@example.com", "role": "admin"})
        self.assertTrue(User.objects.get(username="x2").role == "admin")

    def test_team_lead_sees_and_manages_only_their_team(self):
        mine = self.task(title="Team work", status="review", submitted_at=self.now)
        other = self.task(title="Other work", assignee=self.niyati, status="review", submitted_at=self.now)
        self.client.force_login(self.lead)
        html = self.client.get(reverse("dashboard"), {"show": "open"}).content.decode()
        self.assertIn("Team work", html)
        self.assertNotIn("Other work", html)
        self.assertEqual(self.client.get(reverse("task_detail", args=[other.pk])).status_code, 403)
        self.assertEqual(self.act(self.lead, other, "approve").status_code, 403)
        self.act(self.lead, mine, "approve")
        mine.refresh_from_db()
        self.assertEqual(mine.status, "done")
        self.client.force_login(self.lead)
        self.assertNotIn("Other work", self.client.get(reverse("report"), {"member": self.niyati.pk}).content.decode())
        self.assertRedirects(self.client.get(reverse("team")), reverse("my_tasks"))

    def test_team_lead_is_told_about_their_team(self):
        t = self.task()
        self.act(self.yashi, t, "submit", note="done")
        self.assertTrue(Notification.objects.filter(recipient=self.lead, verb="submitted").exists())
        self.assertFalse(Notification.objects.filter(recipient=self.niyati).exists())

    def test_handover_moves_everything(self):
        t = self.task()
        rule = RecurringTask.objects.create(title="R", frequency="daily")
        rule.assignees.add(self.yashi)
        self.client.force_login(self.admin)
        resp = self.client.post(reverse("user_toggle_active", args=[self.yashi.pk]))
        self.assertRedirects(resp, reverse("handover", args=[self.yashi.pk]))
        self.assertEqual(len(self.client.get(reverse("dashboard")).context["orphaned"]), 1)
        self.post(self.admin, "handover", self.yashi.pk, data={"to": self.niyati.pk})
        t.refresh_from_db()
        self.assertEqual(t.assignee, self.niyati)
        self.assertEqual(list(rule.assignees.all()), [self.niyati])
        self.assertTrue(t.activity.filter(note__contains="Handed over").exists())

    def test_overdue_alert_still_fires_for_someone_who_left(self):
        self.task(due_date=self.today - timedelta(days=1))
        User.objects.filter(pk=self.yashi.pk).update(is_active=False)
        self.assertEqual(reminders.send_overdue_alerts(self.today), 1)

    # ---- agency work

    def test_client_filter_and_report(self):
        c = Client.objects.create(name="Peanutji")
        self.task(title="For client", client=c)
        self.task(title="Internal")
        self.client.force_login(self.admin)
        html = self.client.get(reverse("report"), {"client": c.pk}).content.decode()
        self.assertIn("For client", html)
        self.assertNotIn("Internal", html)
        self.assertEqual(self.client.get(reverse("clients")).status_code, 200)
        csv_text = self.client.get(reverse("report_csv"), {"client": c.pk}).content.decode("utf-8-sig")
        self.assertIn("Peanutji", csv_text)
        self.assertContains(self.client.get(reverse("search"), {"q": "Peanutji"}), "For client")

    def test_member_uploads_finished_work(self):
        t = self.task()
        f = SimpleUploadedFile("final.pdf", b"pdf")
        with tempfile.TemporaryDirectory() as d, self.settings(MEDIA_ROOT=d):
            self.post(self.yashi, "task_upload", t.pk, data={"files": f, "next": "/"})
            att = t.attachments.get()
            self.assertEqual((att.kind, att.original_name), ("work", "final.pdf"))
            self.assertEqual(self.client.get(reverse("attachment", args=[att.pk])).status_code, 200)
            self.client.force_login(self.niyati)
            self.assertEqual(self.client.get(reverse("attachment", args=[att.pk])).status_code, 403)
        self.client.force_login(self.niyati)
        self.assertEqual(self.client.post(reverse("task_upload", args=[t.pk]), {}).status_code, 403)

    def test_hold_stops_the_overdue_clock_and_needs_a_reason(self):
        t = self.task(due_date=self.today - timedelta(days=1))
        self.act(self.yashi, t, "hold", hold_kind="client")
        t.refresh_from_db()
        self.assertTrue(t.is_late)  # no reason given, nothing changed
        self.act(self.yashi, t, "hold", hold_kind="client", note="Waiting for logo")
        t.refresh_from_db()
        self.assertEqual(t.status, "hold")
        self.assertFalse(t.is_late)
        self.assertEqual(reminders.send_overdue_alerts(self.today), 0)
        self.act(self.yashi, t, "resume")
        t.refresh_from_db()
        self.assertEqual(t.status, "progress")
        self.assertTrue(t.is_late)  # the deadline only moves with an approved extension

    def test_extension_flow(self):
        t = self.task(due_date=self.today - timedelta(days=1))
        new = self.today + timedelta(days=3)
        self.post(self.yashi, "extension_request", t.pk, data={"requested_date": new.isoformat(), "reason": "Client late", "next": "/"})
        ext = t.extensions.get()
        t.refresh_from_db()
        self.assertEqual(t.due_date, self.today - timedelta(days=1))
        self.assertEqual(self.post(self.yashi, "extension_decide", ext.pk, "approve").status_code, 403)
        self.client.force_login(self.lead)
        self.assertContains(self.client.get(reverse("dashboard")), "Asking for more time")
        self.post(self.lead, "extension_decide", ext.pk, "approve")
        t.refresh_from_db()
        self.assertEqual((t.due_date, t.score_due_date), (new, new))
        self.assertEqual(t.original_due_date, self.today - timedelta(days=1))
        self.assertFalse(t.is_late)

    def test_extension_decline_keeps_date(self):
        t = self.task()
        self.post(self.yashi, "extension_request", t.pk, data={"requested_date": (self.today + timedelta(days=1)).isoformat(), "reason": "x"})
        self.post(self.admin, "extension_decide", t.extensions.get().pk, "decline", data={"note": "no"})
        t.refresh_from_db()
        self.assertEqual(t.due_date, self.today)
        self.assertEqual(t.extensions.get().status, "declined")

    def test_shared_task(self):
        self.post(self.admin, "assign_task", data={
            "title": "Shoot", "priority": "high", "due_date": self.today.isoformat(),
            "assignees": [self.yashi.pk, self.niyati.pk], "details": "", "shared": "on", "lead": self.niyati.pk,
        })
        t = Task.objects.get()
        self.assertEqual(t.assignee, self.niyati)
        self.assertEqual(list(t.collaborators.all()), [self.yashi])
        self.client.force_login(self.yashi)
        self.assertContains(self.client.get(reverse("my_tasks")), "Shoot")
        self.act(self.yashi, t, "start")
        t.refresh_from_db()
        self.assertEqual(t.status, "progress")

    def test_subtasks(self):
        t = self.task()
        self.post(self.yashi, "subtask_add", t.pk, data={"title": "Draft", "next": "/"})
        s = t.subtasks.get()
        self.post(self.yashi, "subtask_toggle", s.pk, data={"next": "/"})
        s.refresh_from_db()
        self.assertTrue(s.is_done)
        self.assertEqual(self.post(self.niyati, "subtask_toggle", s.pk).status_code, 403)
        self.assertEqual(self.post(self.niyati, "subtask_add", t.pk, data={"title": "x"}).status_code, 403)

    def test_estimates_add_up_in_workload(self):
        self.task(estimated_hours=Decimal("4.5"))
        self.task(estimated_hours=Decimal("2"))
        self.client.force_login(self.admin)
        rows = self.client.get(reverse("dashboard")).context["workload"]
        self.assertEqual(next(w for w in rows if w["user"] == self.yashi)["hours"], Decimal("6.5"))

    def test_team_member_can_create_their_own_task(self):
        self.client.force_login(self.yashi)
        self.assertContains(self.client.get(reverse("my_tasks")), "Add my own task")
        self.assertNotContains(self.client.get(reverse("my_tasks")), "Ask for this to be assigned")
        self.post(self.yashi, "own_task_add", data={"title": "Mine", "priority": "low", "due_date": self.today.isoformat()})
        t = Task.objects.get()
        self.assertEqual((t.assignee, t.assigned_by, t.status), (self.yashi, self.yashi, "todo"))
        self.assertTrue(t.activity.filter(note__contains="Added by the person").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.lead, task=t, message__contains="their own task").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.admin, task=t).exists())
        self.assertContains(self.client.get(reverse("my_tasks")), "Mine")
        # They still can't approve it themselves.
        self.act(self.yashi, t, "submit")
        self.assertEqual(self.act(self.yashi, t, "approve").status_code, 403)
        self.act(self.lead, t, "approve")
        t.refresh_from_db()
        self.assertEqual(t.status, "done")

    def test_own_task_cannot_be_backdated_or_given_to_someone_else(self):
        self.post(self.yashi, "own_task_add", data={"title": "Old", "priority": "low", "due_date": (self.today - timedelta(days=1)).isoformat()})
        self.assertFalse(Task.objects.exists())
        self.post(self.yashi, "own_task_add", data={"title": "Theirs", "priority": "low", "due_date": self.today.isoformat(), "assignee": self.niyati.pk})
        self.assertEqual(Task.objects.get().assignee, self.yashi)

    def test_requests_already_sent_can_still_be_decided(self):
        req = TaskRequest.objects.create(title="Please", requested_by=self.yashi)
        self.post(self.lead, "request_decide", req.pk, "accept", data={"assignee": self.yashi.pk, "due_date": self.today.isoformat()})
        req.refresh_from_db()
        self.assertEqual(req.status, "accepted")

    # ---- security

    def login_form(self, password, username="yashi"):
        self.client.logout()
        return self.client.post(reverse("login"), {"username": username, "password": password})

    def test_login_limit(self):
        for _ in range(settings.LOGIN_FAIL_LIMIT):
            self.assertEqual(self.login_form("wrong").status_code, 200)
        resp = self.login_form("pass12345!")  # even the right password waits
        self.assertEqual(resp.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)
        LoginAttempt.objects.all().delete()
        self.assertEqual(self.login_form("pass12345!").status_code, 302)

    def test_two_step_login_is_gone(self):
        self.assertEqual(self.login_form("pass12345!").status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get("/security/").status_code, 404)
        self.assertEqual(self.client.get("/login/verify/").status_code, 404)
        self.assertNotContains(self.client.get(reverse("my_tasks")), "Two-step")

    def test_admin_set_password_must_be_changed(self):
        self.post(self.admin, "user_password", self.yashi.pk, data={"password": "Temp-pass-9931"})
        self.yashi.refresh_from_db()
        self.assertTrue(self.yashi.must_change_password)
        self.client.force_login(self.yashi)
        self.assertRedirects(self.client.get(reverse("my_tasks")), reverse("password_change"))
        self.client.post(reverse("password_change"), {"old_password": "Temp-pass-9931", "new_password1": "Own-pass-77123", "new_password2": "Own-pass-77123"})
        self.yashi.refresh_from_db()
        self.assertFalse(self.yashi.must_change_password)
        self.assertEqual(self.client.get(reverse("my_tasks")).status_code, 200)

    def test_tagging_rules(self):
        t = self.task()
        # Someone working on it can give access by tagging.
        self.post(self.yashi, "task_comment", t.pk, data={"body": "see this @niyati"})
        self.assertTrue(t.can_view(self.niyati))
        self.assertTrue(mail.outbox and any("tagged you" in m.subject for m in mail.outbox))
        # Someone who was only tagged can't pass access on.
        other = User.objects.create_user("other", "other@example.com", "pass12345!")
        self.post(self.niyati, "task_comment", t.pk, data={"body": "and @other"})
        self.assertFalse(t.can_view(other))
        # A manager can remove access.
        self.post(self.admin, "watcher_remove", t.pk, self.niyati.pk, data={"next": "/"})
        self.assertFalse(t.can_view(self.niyati))

    def test_confidential_task_ignores_tags_from_workers(self):
        t = self.task(is_confidential=True)
        self.post(self.yashi, "task_comment", t.pk, data={"body": "look @niyati"})
        self.assertFalse(t.can_view(self.niyati))
        self.post(self.admin, "task_comment", t.pk, data={"body": "look @niyati"})
        self.assertTrue(t.can_view(self.niyati))

    def test_making_a_task_confidential_removes_tagged_people(self):
        t = self.task()
        t.watchers.add(self.niyati)
        self.post(self.admin, "task_edit", t.pk, data={
            "title": t.title, "priority": t.priority, "due_date": t.due_date.isoformat(), "assignee": t.assignee_id, "details": "", "is_confidential": "on", "next": "/",
        })
        self.assertFalse(t.can_view(self.niyati))

    def test_ensure_admin_leaves_password_alone(self):
        env = {"DJANGO_SUPERUSER_USERNAME": "owner", "DJANGO_SUPERUSER_PASSWORD": "from-env-99123"}
        with mock.patch.dict(os.environ, env):
            call_command("ensure_admin", verbosity=0)
            self.owner.refresh_from_db()
            self.assertTrue(self.owner.check_password("pass12345!"))
            with mock.patch.dict(os.environ, {"DJANGO_SUPERUSER_FORCE_RESET": "1"}):
                call_command("ensure_admin", verbosity=0)
            self.owner.refresh_from_db()
            self.assertTrue(self.owner.check_password("from-env-99123"))
            self.assertTrue(self.owner.must_change_password)

    def test_failed_email_is_reported_truthfully(self):
        with mock.patch("django.core.mail.message.EmailMessage.send", side_effect=RuntimeError("Brevo down")):
            self.post(self.admin, "user_add", data={"first_name": "N", "last_name": "P", "username": "np", "email": "np@example.com", "role": "member"})
            self.client.force_login(self.admin)
            resp = self.client.get(reverse("team"))
        self.assertEqual(EmailFailure.objects.get().recipient, "np@example.com")
        self.assertContains(resp, "couldn't be sent")
        self.assertContains(resp, "Couldn&#x27;t email the invitation")

    def test_every_page_renders_with_real_data(self):
        c = Client.objects.create(name="Peanutji")
        statuses = ["todo", "progress", "change", "hold", "review", "done"]
        tasks = [self.task(title=f"T {s}", status=s, client=c, submitted_at=self.now if s in ("review", "done") else None, done_at=self.now if s == "done" else None, hold_kind="client" if s == "hold" else "", estimated_hours=2) for s in statuses]
        tasks[0].collaborators.add(self.niyati)
        Subtask.objects.create(task=tasks[0], title="Step")
        ExtensionRequest.objects.create(task=tasks[1], requested_by=self.yashi, previous_date=self.today, requested_date=self.today + timedelta(days=1), reason="r")
        TaskRequest.objects.create(title="Ask", requested_by=self.yashi)
        Leave.objects.create(user=self.yashi, start_date=self.today, end_date=self.today)
        for user in (self.owner, self.admin, self.lead, self.yashi, self.niyati):
            self.client.force_login(user)
            for name in ["home", "dashboard", "my_tasks", "report", "team", "clients", "calendar", "archive", "recurring", "notification_list", "my_leave", "leave_approvals", "schedule"]:
                resp = self.client.get(reverse(name), follow=True)
                self.assertEqual(resp.status_code, 200, (user.username, name))
            for t in tasks:
                self.assertIn(self.client.get(reverse("task_detail", args=[t.pk])).status_code, (200, 403))
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("handover", args=[self.yashi.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("task_edit", args=[tasks[0].pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("dashboard"), {"member": "abc", "client": "x"}).status_code, 200)

    # ---- applying for leave

    def apply(self, user, days=(1, 2), **extra):
        return self.post(user, "my_leave", data={
            "kind": "casual", "start_date": (self.today + timedelta(days=days[0])).isoformat(),
            "end_date": (self.today + timedelta(days=days[1])).isoformat(), "note": "Family function",
            "approver": (self.admin2 if user == self.admin else self.admin).pk, **extra,
        })

    def test_team_member_can_see_page_and_apply(self):
        Holiday.objects.create(date=self.today + timedelta(days=5), name="Bhai Dooj")
        self.client.force_login(self.yashi)
        resp = self.client.get(reverse("my_leave"))
        self.assertContains(resp, "Apply for leave")
        self.assertContains(resp, "Bhai Dooj")
        self.assertContains(self.client.get(reverse("my_tasks")), reverse("my_leave"))
        self.apply(self.yashi, cover=self.niyati.pk)
        leave = Leave.objects.get()
        self.assertEqual((leave.user, leave.status, leave.cover), (self.yashi, "pending", self.niyati))
        self.assertEqual(leave.approver, self.admin)
        self.assertTrue(Notification.objects.filter(recipient=self.lead, verb="leave").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.admin, verb="leave").exists())
        self.assertFalse(Notification.objects.filter(recipient=self.admin2, verb="leave").exists())
        sent_to = {m.to[0] for m in mail.outbox if "Leave request" in m.subject}
        self.assertEqual(sent_to, {self.admin.email, self.lead.email})  # the person picked, and the reporting manager

    def test_pending_leave_changes_nothing_until_approved(self):
        self.post(self.yashi, "my_leave", data={"kind": "casual", "start_date": self.today.isoformat(), "end_date": self.today.isoformat(), "note": "x", "approver": self.admin.pk})
        leave = Leave.objects.get()
        self.assertNotIn(self.yashi.pk, on_leave_ids(self.today))
        self.post(self.lead, "leave_decide", leave.pk, "approve", data={"note": "Enjoy"})
        leave.refresh_from_db()
        self.assertEqual((leave.status, leave.decided_by), ("approved", self.lead))
        self.assertIn(self.yashi.pk, on_leave_ids(self.today))
        self.assertTrue(Notification.objects.filter(recipient=self.yashi, verb="leave", message__contains="approved").exists())
        self.assertTrue(any("approved" in m.subject for m in mail.outbox))

    def test_decline_keeps_them_working(self):
        self.apply(self.yashi, days=(0, 0))
        self.post(self.admin, "leave_decide", Leave.objects.get().pk, "decline", data={"note": "Busy week"})
        self.assertEqual(Leave.objects.get().status, "declined")
        self.assertNotIn(self.yashi.pk, on_leave_ids(self.today))

    def test_who_can_decide_leave(self):
        self.apply(self.yashi)
        leave = Leave.objects.get()
        self.assertEqual(self.post(self.yashi, "leave_decide", leave.pk, "approve").status_code, 302)  # sent away
        other_lead = User.objects.create_user("lead2", "l2@example.com", "pass12345!", role=User.Role.MANAGER)
        self.assertEqual(self.post(other_lead, "leave_decide", leave.pk, "approve").status_code, 403)
        leave.refresh_from_db()
        self.assertEqual(leave.status, "pending")
        self.apply(self.admin)
        own = Leave.objects.get(user=self.admin)
        self.assertEqual(self.post(self.admin, "leave_decide", own.pk, "approve").status_code, 403)
        self.post(self.admin2, "leave_decide", own.pk, "approve")
        own.refresh_from_db()
        self.assertEqual(own.status, "approved")

    def test_reason_is_private_to_approvers(self):
        self.apply(self.yashi)
        self.client.force_login(self.niyati)
        self.assertNotContains(self.client.get(reverse("my_leave")), "Family function")
        self.assertRedirects(self.client.get(reverse("leave_approvals")), reverse("my_tasks"))
        self.client.force_login(self.lead)
        self.assertContains(self.client.get(reverse("leave_approvals")), "Family function")
        lead2 = User.objects.create_user("lead2", "l2@example.com", "pass12345!", role=User.Role.MANAGER)
        self.client.force_login(lead2)
        self.assertNotContains(self.client.get(reverse("leave_approvals")), "Family function")

    def test_cannot_cancel_someone_elses_leave(self):
        self.apply(self.yashi)
        self.assertEqual(self.post(self.niyati, "leave_cancel", Leave.objects.get().pk).status_code, 404)

    def test_cancel_pending_quietly_and_approved_with_notice(self):
        self.apply(self.yashi)
        leave = Leave.objects.get()
        Notification.objects.all().delete()
        self.post(self.yashi, "leave_cancel", leave.pk)
        leave.refresh_from_db()
        self.assertEqual(leave.status, "cancelled")
        self.assertFalse(Notification.objects.exists())
        self.apply(self.yashi)  # the cancelled dates are free again
        leave = Leave.objects.exclude(status="cancelled").get()
        self.post(self.lead, "leave_decide", leave.pk, "approve")
        Notification.objects.all().delete()
        self.post(self.yashi, "leave_cancel", leave.pk)
        self.assertTrue(Notification.objects.filter(recipient=self.lead, message__contains="cancelled").exists())

    def test_overlap_and_bad_dates_are_refused(self):
        self.apply(self.yashi, days=(1, 3))
        self.apply(self.yashi, days=(3, 5))
        self.assertEqual(Leave.objects.count(), 1)
        self.apply(self.yashi, days=(9, 8))
        self.apply(self.yashi, days=(-40, -39))
        self.assertEqual(Leave.objects.count(), 1)

    def test_approver_sees_affected_tasks_and_can_reassign(self):
        due = self.today + timedelta(days=1)
        t = self.task(due_date=due, title="Due in leave")
        self.client.force_login(self.yashi)
        check = self.client.get(reverse("leave_check"), {"start": due.isoformat(), "end": due.isoformat()}).json()
        self.assertEqual(check["tasks"][0]["title"], "Due in leave")
        self.apply(self.yashi)
        leave = Leave.objects.get()
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("leave_approvals")), "Due in leave")
        self.post(self.admin, "leave_decide", leave.pk, "approve")
        self.post(self.admin, "leave_reassign", leave.pk, data={"to": self.niyati.pk})
        t.refresh_from_db()
        self.assertEqual(t.assignee, self.niyati)

    def test_approver_must_be_picked_and_must_be_an_admin_or_lead(self):
        self.apply(self.yashi, approver="")
        self.assertFalse(Leave.objects.exists())
        self.apply(self.yashi, approver=self.niyati.pk)
        self.assertFalse(Leave.objects.exists())
        self.apply(self.yashi, approver=self.yashi.pk)
        self.assertFalse(Leave.objects.exists())

    def test_reporting_manager_is_suggested_first(self):
        self.client.force_login(self.yashi)
        form = self.client.get(reverse("my_leave")).context["form"]
        self.assertEqual(form.fields["approver"].initial, self.lead.pk)

    def test_a_team_lead_picked_by_someone_else_can_decide(self):
        lead2 = User.objects.create_user("lead2", "l2@example.com", "pass12345!", role=User.Role.MANAGER)
        self.apply(self.yashi, approver=lead2.pk)
        leave = Leave.objects.get()
        self.client.force_login(lead2)
        self.assertContains(self.client.get(reverse("leave_approvals")), "Family function")
        self.post(lead2, "leave_decide", leave.pk, "approve")
        leave.refresh_from_db()
        self.assertEqual((leave.status, leave.decided_by), ("approved", lead2))

    def test_badge_says_admin_not_owner(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('<span class="role-badge">Admin</span>', html)
        self.assertNotIn("Owner", html)

    def test_sole_admin_is_not_stuck(self):
        User.objects.exclude(pk=self.owner.pk).update(is_active=False)
        self.apply(self.owner, approver="")
        self.assertEqual(Leave.objects.get().status, "approved")

    def test_leave_notification_links_to_the_right_page(self):
        self.apply(self.yashi)
        n = Notification.objects.filter(recipient=self.lead).first()
        self.client.force_login(self.lead)
        self.assertRedirects(self.client.get(reverse("notification_open", args=[n.pk])), reverse("leave_approvals"))

    # ---- Indian holiday calendar

    def test_calendar_applies_nothing_until_an_admin_ticks_it(self):
        self.assertFalse(Holiday.objects.exists())
        self.client.force_login(self.admin)
        with mock.patch("django.utils.timezone.localdate", return_value=date(2026, 12, 1)):
            self.assertContains(self.client.get(reverse("calendar")), "Republic Day")
        self.assertFalse(Holiday.objects.exists())

    def test_admin_ticks_and_team_sees_only_those(self):
        day = date(2027, 1, 26)
        with mock.patch("django.utils.timezone.localdate", return_value=date(2026, 12, 1)):
            self.post(self.admin, "holiday_catalog_save", data={"holidays": [indian_holidays.key_for(day, "Republic Day")]})
            self.assertEqual(Holiday.objects.get().name, "Republic Day")
            self.client.force_login(self.yashi)
            html = self.client.get(reverse("my_leave")).content.decode()
            self.assertIn("Republic Day", html)
            self.assertNotIn("Good Friday", html)
            self.assertTrue(is_office_closed(day))
            self.post(self.admin, "holiday_catalog_save", data={})
            self.assertFalse(Holiday.objects.exists())

    def test_team_member_cannot_change_holidays(self):
        self.post(self.yashi, "holiday_catalog_save", data={"holidays": [indian_holidays.key_for(date(2027, 1, 26), "Republic Day")]})
        self.assertFalse(Holiday.objects.exists())
        self.client.force_login(self.yashi)
        self.assertRedirects(self.client.get(reverse("calendar")), reverse("my_tasks"))

    def test_several_holidays_on_one_day_and_custom_ones_are_kept(self):
        day = date(2027, 8, 15)
        Holiday.objects.create(date=day, name="Office picnic")
        Holiday.objects.create(date=date(2027, 2, 2), name="Annual day")
        keys = [indian_holidays.key_for(day, "Independence Day"), indian_holidays.key_for(day, "Parsi New Year")]
        with mock.patch("django.utils.timezone.localdate", return_value=date(2026, 12, 1)):
            self.post(self.admin, "holiday_catalog_save", data={"holidays": keys})
            self.assertEqual(Holiday.objects.get(date=day).name, "Office picnic / Independence Day / Parsi New Year")
            self.client.force_login(self.admin)
            rows = {r["name"]: r["checked"] for r in self.client.get(reverse("calendar")).context["catalog"] if r["date"] == day}
            self.assertEqual(rows, {"Independence Day": True, "Milad-un-Nabi": False, "Parsi New Year": True})
            self.post(self.admin, "holiday_catalog_save", data={})
        self.assertEqual(Holiday.objects.get(date=day).name, "Office picnic")
        self.assertEqual(Holiday.objects.get(date=date(2027, 2, 2)).name, "Annual day")

    def test_made_up_or_past_holidays_are_ignored(self):
        with mock.patch("django.utils.timezone.localdate", return_value=date(2027, 6, 1)):
            self.post(self.admin, "holiday_catalog_save", data={"holidays": ["2027-07-01|Fake day", indian_holidays.key_for(date(2027, 1, 26), "Republic Day")]})
        self.assertFalse(Holiday.objects.exists())

    def test_holiday_list_is_clean(self):
        seen = set()
        for day, name, kind in indian_holidays.HOLIDAYS:
            self.assertNotIn((day, name), seen)
            seen.add((day, name))
            self.assertNotIn(indian_holidays.SEPARATOR, name)
            self.assertIn(kind, indian_holidays.KIND_LABELS)
            self.assertLessEqual(day, indian_holidays.COVERS_UNTIL)

    # ---- monthly calendar

    def day_cell(self, resp, day):
        return next(d for w in resp.context["weeks"] for d in w if d["date"] == day)

    def test_calendar_shows_deadlines_holidays_and_only_approved_leave(self):
        day = self.today + timedelta(days=1)
        self.task(title="Deadline", due_date=day)
        Holiday.objects.create(date=day, name="Festival")
        Leave.objects.create(user=self.yashi, start_date=day, end_date=day)
        Leave.objects.create(user=self.niyati, start_date=day, end_date=day, status=Leave.Status.PENDING)
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("schedule"), {"month": f"{day:%Y-%m}"})
        cell = self.day_cell(resp, day)
        self.assertEqual([t.title for t in cell["tasks"]], ["Deadline"])
        self.assertEqual(cell["holiday"], "Festival")
        self.assertEqual(cell["away"], [self.yashi])
        self.assertEqual(cell["clashes"], 1)
        self.assertContains(resp, "Festival")

    def test_calendar_scope_by_role(self):
        day = self.today
        self.task(title="Yashi work", due_date=day)
        self.task(title="Niyati work", assignee=self.niyati, due_date=day)
        Leave.objects.create(user=self.niyati, start_date=day, end_date=day, note="Private reason")
        for user, sees, hidden in [
            (self.admin, ["Yashi work", "Niyati work"], []),
            (self.lead, ["Yashi work"], ["Niyati work"]),
            (self.yashi, ["Yashi work"], ["Niyati work"]),
            (self.niyati, ["Niyati work"], ["Yashi work"]),
        ]:
            self.client.force_login(user)
            resp = self.client.get(reverse("schedule"))
            html = resp.content.decode()
            for title in sees:
                self.assertIn(title, html, user)
            for title in hidden:
                self.assertNotIn(title, html, user)
            self.assertNotIn("Private reason", html)
        self.client.force_login(self.yashi)
        self.assertEqual(self.day_cell(self.client.get(reverse("schedule")), day)["away"], [])

    def test_calendar_filters_and_bad_input(self):
        c = Client.objects.create(name="Peanutji")
        self.task(title="Client one", client=c)
        self.task(title="Other one", assignee=self.niyati)
        self.client.force_login(self.admin)
        html = self.client.get(reverse("schedule"), {"client": c.pk}).content.decode()
        self.assertIn("Client one", html)
        self.assertNotIn("Other one", html)
        html = self.client.get(reverse("schedule"), {"member": self.niyati.pk}).content.decode()
        self.assertIn("Other one", html)
        self.assertNotIn("Client one", html)
        self.assertEqual(self.client.get(reverse("schedule"), {"month": "junk", "member": "x", "client": "y"}).status_code, 200)
        self.client.force_login(self.yashi)
        html = self.client.get(reverse("schedule"), {"member": self.niyati.pk}).content.decode()
        self.assertNotIn("Other one", html)

    def test_calendar_link_prefills_the_assign_date(self):
        day = self.today + timedelta(days=3)
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("dashboard"), {"due": day.isoformat()})
        self.assertEqual(resp.context["form"].initial["due_date"], day)

    def test_archived_tasks_stay_off_the_calendar(self):
        self.task(title="Gone", archived_at=self.now)
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(reverse("schedule")), "Gone")

    # ---- morning summary

    def working_day(self):
        day = self.today
        while day.weekday() == 6:
            day += timedelta(days=1)
        return day

    def test_summary_has_due_and_overdue_for_each_person(self):
        day = self.working_day()
        self.task(title="Late one", due_date=day - timedelta(days=2))
        self.task(title="Today one", due_date=day)
        self.task(title="Later one", due_date=day + timedelta(days=5))
        self.task(title="On hold one", due_date=day - timedelta(days=2), status="hold")
        mail.outbox.clear()
        self.assertEqual(summaries.send_morning_summaries(day), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, [self.yashi.email])
        self.assertIn("1 overdue", m.subject)
        self.assertIn("1 due today", m.subject)
        self.assertIn("Late one", m.body)
        self.assertIn("Today one", m.body)
        self.assertNotIn("Later one", m.body)
        self.assertNotIn("On hold one", m.body)

    def test_summary_shows_approvals_to_admins_and_team_leads(self):
        day = self.working_day()
        self.task(title="Yashi done", status="review", submitted_at=self.now, due_date=day)
        self.task(title="Niyati done", assignee=self.niyati, status="review", submitted_at=self.now, due_date=day)
        Leave.objects.create(user=self.yashi, start_date=day + timedelta(days=2), end_date=day + timedelta(days=2), status=Leave.Status.PENDING)
        mail.outbox.clear()
        summaries.send_morning_summaries(day)
        by_person = {m.to[0]: m for m in mail.outbox}
        self.assertIn("2 to approve", by_person[self.admin.email].subject)
        self.assertIn("1 leave request", by_person[self.admin.email].subject)
        self.assertIn("1 to approve", by_person[self.lead.email].subject)
        self.assertIn("Yashi done", by_person[self.lead.email].body)
        self.assertNotIn("Niyati done", by_person[self.lead.email].body)
        self.assertNotIn(self.yashi.email, by_person)  # nothing due, and waiting isn't their job

    def test_admin_is_not_asked_to_approve_their_own_work(self):
        day = self.working_day()
        self.task(title="Own work", assignee=self.admin, status="review", submitted_at=self.now, due_date=day)
        summary = summaries.build_summary(self.admin, day)
        self.assertEqual(summary["approvals"], [])
        self.assertEqual([t.title for t in summaries.build_summary(self.admin2, day)["approvals"]], ["Own work"])

    def test_summary_sends_once_a_day(self):
        day = self.working_day()
        self.task(due_date=day)
        self.assertEqual(summaries.send_morning_summaries(day), 1)
        self.assertEqual(summaries.send_morning_summaries(day), 0)

    def test_no_summary_on_closed_days_or_while_on_leave(self):
        sunday = self.today + timedelta(days=(6 - self.today.weekday()) or 7)
        self.task(due_date=sunday)
        self.assertEqual(summaries.send_morning_summaries(sunday), 0)
        day = sunday + timedelta(days=1)
        Holiday.objects.create(date=day, name="Festival")
        self.task(due_date=day)
        self.assertEqual(summaries.send_morning_summaries(day), 0)
        day2 = day + timedelta(days=1)
        self.task(due_date=day2)
        Leave.objects.create(user=self.yashi, start_date=day2, end_date=day2)
        self.assertEqual(summaries.send_morning_summaries(day2), 0)

    def test_nothing_to_report_means_no_email(self):
        mail.outbox.clear()
        self.assertEqual(summaries.send_morning_summaries(self.working_day()), 0)
        self.assertEqual(mail.outbox, [])

    def test_failed_summary_is_listed_for_admins(self):
        day = self.working_day()
        self.task(due_date=day)
        with mock.patch("django.core.mail.message.EmailMessage.send", side_effect=RuntimeError("down")):
            summaries.send_morning_summaries(day)
        self.assertEqual(EmailFailure.objects.get().recipient, self.yashi.email)

    def test_list_view_does_not_call_held_or_submitted_tasks_overdue(self):
        past = self.today - timedelta(days=2)
        self.task(title="Held", due_date=past, status="hold")
        self.task(title="Submitted", due_date=past, status="review", submitted_at=self.now)
        self.task(title="Really late", due_date=past)
        self.client.force_login(self.yashi)
        groups = {g["key"]: [t.title for t in g["tasks"]] for g in self.client.get(reverse("my_tasks")).context["agenda"]}
        self.assertEqual(groups["overdue"], ["Really late"])
        self.assertCountEqual(groups["waiting"], ["Held", "Submitted"])

    def test_cards_are_a_simple_summary_with_actions_in_the_popup(self):
        t = self.task(title="Card", details="Long secret brief")
        self.client.force_login(self.yashi)
        html = self.client.get(reverse("my_tasks")).content.decode()
        self.assertNotIn("Long secret brief", html)
        self.assertNotIn(reverse("task_action", args=[t.pk, "start"]), html)
        self.assertContains(self.client.get(reverse("task_detail", args=[t.pk])), reverse("task_action", args=[t.pk, "start"]))

    def test_theme_switch_is_on_login_and_app_pages(self):
        self.assertContains(self.client.get(reverse("login")), "data-theme-toggle")
        self.client.force_login(self.yashi)
        self.assertContains(self.client.get(reverse("my_tasks")), "data-theme-toggle")

    def test_health_check_is_public(self):
        self.assertEqual(self.client.get(reverse("healthz")).status_code, 200)
