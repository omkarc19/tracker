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


def start(app):
    _scheduler.add_job(check_meeting_reminders, "interval", minutes=5, args=[app], id="reminders")
    _scheduler.add_job(check_task_reminders, "interval", hours=1, args=[app], id="task_reminders")
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
