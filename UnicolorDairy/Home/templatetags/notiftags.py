from django import template

register = template.Library()


@register.inclusion_tag("tasks/_notif_bell.html", takes_context=True)
def notif_bell(context):
    """The bell icon with the unread count and the latest notifications."""
    user = context.get("user")
    if not user or not user.is_authenticated:
        return {"show": False}
    qs = user.notifications.select_related("actor")
    return {
        "show": True,
        "unread": qs.filter(is_read=False).count(),
        "latest": list(qs[:8]),
        "request": context.get("request"),
        "csrf_token": context.get("csrf_token"),
    }