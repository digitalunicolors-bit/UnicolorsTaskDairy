from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("tasks/assign/", views.assign_task, name="assign_task"),
    path("task/<int:pk>/", views.task_detail, name="task_detail"),
    path("tasks/<int:pk>/<str:action>/", views.task_action, name="task_action"),
    path("my-tasks/", views.my_tasks, name="my_tasks"),
    path("files/<int:pk>/", views.attachment, name="attachment"),
    path("reports/", views.report, name="report"),
    path("reports/export.csv", views.report_csv, name="report_csv"),
    path("team/", views.team, name="team"),
    path("team/add/", views.user_add, name="user_add"),
    path("team/<int:pk>/edit/", views.user_edit, name="user_edit"),
    path("team/<int:pk>/password/", views.user_password, name="user_password"),
    path("invite/<uidb64>/<token>/", views.accept_invite, name="accept_invite"),
    path("team/<int:pk>/resend-invite/", views.resend_invite, name="resend_invite"),
    path("team/resend-invites/", views.resend_all_invites, name="resend_all_invites"),

    # notifications
    path("notifications/", views.notification_list, name="notification_list"),
    path("notifications/count/", views.notifications_count, name="notifications_count"),
    path("notifications/read-all/", views.notifications_read_all, name="notifications_read_all"),
    path("notifications/<int:pk>/open/", views.notification_open, name="notification_open"),
    path("notifications/<int:pk>/toggle/", views.notification_toggle, name="notification_toggle"),
]