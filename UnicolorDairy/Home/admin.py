from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Task, TaskActivity, TaskAttachment, User


@admin.register(User)
class UnicolorsUserAdmin(UserAdmin):
    list_display = ("username", "first_name", "last_name", "role", "designation", "is_active")
    list_filter = ("role", "is_active")
    fieldsets = UserAdmin.fieldsets + (("Unicolors", {"fields": ("role", "designation")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("Unicolors", {"fields": ("first_name", "last_name", "role", "designation")}),)


class ActivityInline(admin.TabularInline):
    model = TaskActivity
    extra = 0
    readonly_fields = ("actor", "action", "note", "created_at")
    can_delete = False


class AttachmentInline(admin.TabularInline):
    model = TaskAttachment
    extra = 0
    readonly_fields = ("kind", "original_name", "size", "uploaded_by", "created_at")
    fields = ("kind", "original_name", "size", "uploaded_by", "created_at")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "assignee", "status", "priority", "due_date", "change_count")
    list_filter = ("status", "priority", "assignee")
    search_fields = ("title", "details")
    date_hierarchy = "due_date"
    inlines = [AttachmentInline, ActivityInline]
