from django import template
from django.utils.html import escape, urlize
from django.utils.safestring import mark_safe

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


@register.filter(needs_autoescape=True)
def mentionize(text, autoescape=True):
    """Make links clickable and show @username tags as highlighted names."""
    from django.db.models import Q

    from ..models import MENTION_RE, MENTION_TRIM, User

    text = text or ""
    names = {m.group(1).rstrip(MENTION_TRIM).lower() for m in MENTION_RE.finditer(text)}
    names.discard("")
    users = {}
    if names:
        q = Q()
        for n in names:
            q |= Q(username__iexact=n)
        users = {u.username.lower(): u for u in User.objects.filter(q)}
    found = []

    def mark(m):
        raw = m.group(1)
        name = raw.rstrip(MENTION_TRIM)
        u = users.get(name.lower())
        if not u:
            return m.group(0)
        found.append(u)
        return f"\ue000{len(found) - 1}\ue001{raw[len(name):]}"

    html = urlize(MENTION_RE.sub(mark, text), autoescape=autoescape)
    for i, u in enumerate(found):
        chip = f'<span class="mention" title="@{escape(u.username)}">@{escape(u.display_name)}</span>'
        html = html.replace(f"\ue000{i}\ue001", chip)
    return mark_safe(html)