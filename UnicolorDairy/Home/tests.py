from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Task, User


class FlowTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pass12345!", role=User.Role.ADMIN, first_name="Boss")
        self.admin2 = User.objects.create_user("boss2", password="pass12345!", role=User.Role.ADMIN)
        self.yashi = User.objects.create_user("yashi", password="pass12345!", first_name="Jain", last_name="Yashi")
        self.niyati = User.objects.create_user("niyati", password="pass12345!")
        self.today = timezone.localdate()

    def login(self, user):
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
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 403)
        self.assertEqual(self.client.get(reverse("team")).status_code, 403)

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
            "first_name": "New", "last_name": "Admin", "username": "newadmin", "email": "",
            "designation": "Manager", "role": "admin", "password": "Str0ng-pass-99",
        })
        self.assertTrue(User.objects.get(username="newadmin").is_admin)
        self.client.post(reverse("user_edit", args=[self.admin.pk]), {
            "first_name": "Boss", "last_name": "", "email": "", "designation": "", "role": "member", "is_active": "on",
        })
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_admin)

    def test_pages_render(self):
        self.assign(self.yashi)
        for user, names in [(self.admin, ["dashboard", "my_tasks", "report", "team", "user_add"]), (self.yashi, ["my_tasks", "report"])]:
            self.login(user)
            for name in names:
                self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
