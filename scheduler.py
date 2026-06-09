from datetime import datetime, timedelta, date
import logging

from apscheduler.schedulers.background import BackgroundScheduler

import config
import notifications

logger = logging.getLogger(__name__)
_scheduler = BackgroundScheduler(timezone="Asia/Kolkata")


def check_meeting_reminders(app):
    with app.app_context():
        from models import Activity
        window_start = datetime.utcnow() + timedelta(minutes=config.MEETING_REMINDER_MINUTES - 2)
        window_end = datetime.utcnow() + timedelta(minutes=config.MEETING_REMINDER_MINUTES + 2)
        upcoming = Activity.query.filter(
            Activity.scheduled_at >= window_start,
            Activity.scheduled_at <= window_end,
            Activity.reminder_sent == False,
            Activity.type == "meeting",
        ).all()
        for activity in upcoming:
            notifications.notify_meeting_reminder(activity)
            activity.reminder_sent = True
        from models import db
        db.session.commit()


def send_daily_digest(app):
    with app.app_context():
        from models import Deal, Activity
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)
        tomorrow_start = today_end
        tomorrow_end = tomorrow_start + timedelta(days=1)

        deals_won_today = Deal.query.filter(
            Deal.stage == "won",
            Deal.updated_at >= today_start,
            Deal.updated_at < today_end,
        ).all()

        upcoming_meetings = Activity.query.filter(
            Activity.type == "meeting",
            Activity.scheduled_at >= tomorrow_start,
            Activity.scheduled_at < tomorrow_end,
        ).all()

        # Deals with no activity in 5+ days that aren't closed
        stale_cutoff = datetime.utcnow() - timedelta(days=5)
        stale_deals = [
            d for d in Deal.query.filter(
                Deal.stage.notin_(["won", "lost"]),
                Deal.updated_at <= stale_cutoff,
            ).all()
        ]

        notifications.send_daily_digest(deals_won_today, upcoming_meetings, stale_deals)


def send_weekly_digest(app):
    with app.app_context():
        from models import Deal, Activity
        week_start = datetime.utcnow() - timedelta(days=7)

        won = Deal.query.filter(Deal.stage == "won", Deal.updated_at >= week_start).all()
        lost = Deal.query.filter(Deal.stage == "lost", Deal.updated_at >= week_start).all()
        new = Deal.query.filter(Deal.created_at >= week_start).all()
        meetings = Activity.query.filter(
            Activity.type == "meeting",
            Activity.scheduled_at >= week_start,
            Activity.scheduled_at <= datetime.utcnow(),
        ).count()
        pipeline = Deal.query.filter(Deal.stage.notin_(["won", "lost"])).all()

        notifications.send_weekly_digest({
            "won_count": len(won),
            "won_value": sum(d.value for d in won),
            "lost_count": len(lost),
            "new_count": len(new),
            "meetings_count": meetings,
            "pipeline_value": sum(d.value for d in pipeline),
        })


def check_task_reminders(app):
    with app.app_context():
        from models import Task, db
        today = date.today()
        tomorrow = today + timedelta(days=1)

        # Due tomorrow reminders
        due_tomorrow = Task.query.filter(
            Task.due_date == tomorrow,
            Task.status != "done",
            Task.due_reminder_sent == False,
        ).all()
        for task in due_tomorrow:
            notifications.notify_task_due_tomorrow(task)
            task.due_reminder_sent = True

        # Overdue reminders (send once per task)
        overdue = Task.query.filter(
            Task.due_date < today,
            Task.status != "done",
            Task.overdue_reminder_sent == False,
        ).all()
        for task in overdue:
            notifications.notify_task_overdue(task)
            task.overdue_reminder_sent = True

        db.session.commit()


def sync_gcal_ics(app):
    with app.app_context():
        from models import AppSetting, CalendarEvent, db
        from icalendar import Calendar as iCalendar
        import requests as http_requests
        import re
        from datetime import timezone, timedelta

        setting = AppSetting.query.get("gcal_ics_url")
        if not setting or not setting.value:
            return

        try:
            resp = http_requests.get(
                setting.value, timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (compatible; CalendarSync/1.0)"},
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error("GCal ICS fetch failed: %s", e)
            return

        if "text/html" in resp.headers.get("Content-Type", ""):
            logger.warning("GCal ICS returned HTML — skipping sync")
            return

        try:
            cal = iCalendar.from_ical(resp.content)
        except Exception as e:
            logger.error("GCal ICS parse failed: %s", e)
            return

        IST = timezone(timedelta(hours=5, minutes=30))
        calendar_owner = setting.value.split("/ical/")[1].split("/")[0].replace("%40", "@") if "/ical/" in setting.value else ""
        imported = updated = 0

        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            uid = str(component.get("UID", ""))
            if not uid:
                continue
            summary = str(component.get("SUMMARY", "No Title"))
            if summary.strip().lower() == "busy":
                continue

            description = str(component.get("DESCRIPTION", "") or "")
            location_val = str(component.get("LOCATION", "") or "")

            dtstart = component.get("DTSTART")
            dtend   = component.get("DTEND")
            ev_date = ev_start = ev_end = None
            if dtstart:
                dt = dtstart.dt
                if hasattr(dt, "date"):
                    if dt.tzinfo is not None:
                        dt = dt.astimezone(IST)
                    ev_date  = dt.date()
                    ev_start = dt.strftime("%H:%M")
                else:
                    ev_date = dt
            if dtend:
                dt = dtend.dt
                if hasattr(dt, "hour"):
                    if dt.tzinfo is not None:
                        dt = dt.astimezone(IST)
                    ev_end = dt.strftime("%H:%M")
            if not ev_date:
                continue

            meet_url = None
            m = re.search(r"https://meet\.google\.com/[a-z0-9-]+", description)
            if m:
                meet_url = m.group(0)
            if not meet_url:
                url_field = str(component.get("URL", "") or "")
                if "meet.google.com" in url_field:
                    meet_url = url_field

            raw_attendees = component.get("ATTENDEE", [])
            if not isinstance(raw_attendees, list):
                raw_attendees = [raw_attendees] if raw_attendees else []
            attendee_names = []
            for a in raw_attendees:
                cn    = a.params.get("CN", "") if hasattr(a, "params") else ""
                email = str(a).replace("mailto:", "").strip()
                name  = cn if cn and cn != email else email
                if name and name != calendar_owner:
                    attendee_names.append(name)
            organizer = component.get("ORGANIZER")
            if organizer:
                org_cn    = organizer.params.get("CN", "") if hasattr(organizer, "params") else ""
                org_email = str(organizer).replace("mailto:", "").strip()
                org_name  = org_cn if org_cn and org_cn != org_email else org_email
                if org_name and "@" in org_name and "group.calendar.google.com" not in org_name:
                    if org_name not in attendee_names and org_name != calendar_owner:
                        attendee_names.append(org_name)
            attendees_str = ", ".join(attendee_names)

            existing = CalendarEvent.query.filter_by(external_id=uid).first()
            if existing:
                existing.title        = summary
                existing.date         = ev_date
                existing.start_time   = ev_start
                existing.end_time     = ev_end
                existing.location     = location_val or None
                existing.meeting_link = meet_url
                existing.description  = description or None
                existing.attendees    = attendees_str
                existing.updated_at   = datetime.utcnow()
                updated += 1
            else:
                db.session.add(CalendarEvent(
                    title        = summary,
                    date         = ev_date,
                    start_time   = ev_start,
                    end_time     = ev_end,
                    location     = location_val or None,
                    meeting_link = meet_url,
                    description  = description or None,
                    attendees    = attendees_str,
                    event_type   = "meeting",
                    created_by   = "google_calendar",
                    external_id  = uid,
                    gcal_synced  = True,
                ))
                imported += 1

        db.session.commit()
        if imported or updated:
            logger.info("GCal auto-sync: %d imported, %d updated", imported, updated)


def start(app):
    _scheduler.add_job(check_meeting_reminders, "interval", minutes=5, args=[app], id="reminders")
    _scheduler.add_job(check_task_reminders, "interval", hours=1, args=[app], id="task_reminders")
    _scheduler.add_job(sync_gcal_ics, "interval", minutes=5, args=[app], id="gcal_sync")
    _scheduler.add_job(
        send_daily_digest, "cron",
        hour=config.DAILY_DIGEST_HOUR, minute=0,
        args=[app], id="daily_digest"
    )
    _scheduler.add_job(
        send_weekly_digest, "cron",
        day_of_week=config.WEEKLY_DIGEST_DAY,
        hour=config.WEEKLY_DIGEST_HOUR, minute=0,
        args=[app], id="weekly_digest"
    )
    _scheduler.start()
    logger.info("Scheduler started")


def stop():
    _scheduler.shutdown(wait=False)
