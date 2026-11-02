"""The Indian holiday calendar that admins choose office holidays from.

Nothing here applies by itself: a date only becomes an office holiday when an admin ticks it.

Sources:
- Central Government (DoPT) gazetted and restricted holiday lists for 2026 and 2027.
- Gujarat dates: the Gujarat Government list for 2026; for 2027, published holiday calendars,
  as the state's own 2027 list wasn't available when this was added.

Moon-based dates (Id, Muharram, Milad-un-Nabi) can shift when the moon is sighted.
Add each new year's list here when the government publishes it.
"""
from datetime import date

NATIONAL = "national"
CENTRAL = "central"
OPTIONAL = "optional"
GUJARAT = "gujarat"

KIND_LABELS = {
    NATIONAL: "National",
    CENTRAL: "Central govt",
    OPTIONAL: "Optional",
    GUJARAT: "Gujarat",
}

COVERS_UNTIL = date(2027, 12, 31)

HOLIDAYS = [
    # ---- 2026 (from November)
    (date(2026, 11, 8), "Diwali (Deepavali)", CENTRAL),
    (date(2026, 11, 10), "Bestu Varas (Gujarati New Year)", GUJARAT),
    (date(2026, 11, 11), "Bhai Bij", GUJARAT),
    (date(2026, 11, 24), "Guru Nanak's Birthday", CENTRAL),
    (date(2026, 12, 25), "Christmas Day", CENTRAL),
    # ---- 2027
    (date(2027, 1, 1), "New Year's Day", OPTIONAL),
    (date(2027, 1, 14), "Makar Sankranti (Uttarayan)", OPTIONAL),
    (date(2027, 1, 15), "Pongal, Guru Gobind Singh's Birthday", OPTIONAL),
    (date(2027, 1, 26), "Republic Day", NATIONAL),
    (date(2027, 2, 11), "Basant Panchami", OPTIONAL),
    (date(2027, 2, 19), "Shivaji Jayanti", OPTIONAL),
    (date(2027, 2, 20), "Guru Ravidas's Birthday", OPTIONAL),
    (date(2027, 3, 2), "Swami Dayananda Saraswati's Birthday", OPTIONAL),
    (date(2027, 3, 5), "Jamat-ul-Vida", OPTIONAL),
    (date(2027, 3, 6), "Maha Shivratri", OPTIONAL),
    (date(2027, 3, 10), "Id-ul-Fitr", CENTRAL),
    (date(2027, 3, 22), "Holika Dahan", OPTIONAL),
    (date(2027, 3, 23), "Holi (Dhuleti)", CENTRAL),
    (date(2027, 3, 26), "Good Friday", CENTRAL),
    (date(2027, 3, 28), "Easter Sunday", OPTIONAL),
    (date(2027, 4, 7), "Chaitra Sukladi, Gudi Padwa, Ugadi, Cheti Chand", OPTIONAL),
    (date(2027, 4, 14), "Dr. Ambedkar Jayanti", GUJARAT),
    (date(2027, 4, 14), "Vaisakhi, Tamil New Year", OPTIONAL),
    (date(2027, 4, 15), "Ram Navami", CENTRAL),
    (date(2027, 4, 19), "Mahavir Jayanti", CENTRAL),
    (date(2027, 5, 9), "Rabindranath Tagore's Birthday", OPTIONAL),
    (date(2027, 5, 17), "Id-ul-Zuha (Bakrid)", CENTRAL),
    (date(2027, 5, 20), "Buddha Purnima", CENTRAL),
    (date(2027, 6, 16), "Muharram", CENTRAL),
    (date(2027, 7, 5), "Rath Yatra", OPTIONAL),
    (date(2027, 8, 15), "Independence Day", NATIONAL),
    (date(2027, 8, 15), "Milad-un-Nabi", CENTRAL),
    (date(2027, 8, 15), "Parsi New Year", OPTIONAL),
    (date(2027, 8, 17), "Raksha Bandhan", OPTIONAL),
    (date(2027, 8, 25), "Janmashtami", CENTRAL),
    (date(2027, 9, 4), "Ganesh Chaturthi", OPTIONAL),
    (date(2027, 9, 4), "Samvatsari", GUJARAT),
    (date(2027, 9, 12), "Onam", OPTIONAL),
    (date(2027, 10, 2), "Mahatma Gandhi's Birthday", NATIONAL),
    (date(2027, 10, 6), "Dussehra (Saptami)", OPTIONAL),
    (date(2027, 10, 7), "Dussehra (Mahashtami)", OPTIONAL),
    (date(2027, 10, 8), "Dussehra (Mahanavmi)", OPTIONAL),
    (date(2027, 10, 9), "Dussehra (Vijaya Dashmi)", CENTRAL),
    (date(2027, 10, 15), "Maharishi Valmiki's Birthday", OPTIONAL),
    (date(2027, 10, 18), "Karwa Chauth", OPTIONAL),
    (date(2027, 10, 28), "Naraka Chaturdasi (Kali Chaudas)", OPTIONAL),
    (date(2027, 10, 29), "Diwali (Deepavali)", CENTRAL),
    (date(2027, 10, 30), "Bestu Varas (Gujarati New Year)", GUJARAT),
    (date(2027, 10, 30), "Govardhan Puja", OPTIONAL),
    (date(2027, 10, 31), "Bhai Bij", OPTIONAL),
    (date(2027, 10, 31), "Sardar Patel Jayanti", GUJARAT),
    (date(2027, 11, 4), "Chhath Puja", OPTIONAL),
    (date(2027, 11, 14), "Guru Nanak's Birthday", CENTRAL),
    (date(2027, 11, 24), "Guru Teg Bahadur's Martyrdom Day", OPTIONAL),
    (date(2027, 12, 12), "Hazarat Ali's Birthday", OPTIONAL),
    (date(2027, 12, 24), "Christmas Eve", OPTIONAL),
    (date(2027, 12, 25), "Christmas Day", CENTRAL),
]

SEPARATOR = " / "


def key_for(day, name):
    return f"{day.isoformat()}|{name}"


def upcoming(today):
    return [h for h in HOLIDAYS if h[0] >= today]
