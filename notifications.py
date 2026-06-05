import asyncio
import logging
from datetime import datetime

import requests

import config

logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"


def _send(text: str) -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping notification")
        return
    try:
        resp = requests.post(
            TELEGRAM_API,
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error("Telegram send failed: %s", e)


def notify_deal_created(deal) -> None:
    msg = (
        f"🆕 <b>New Deal Added</b>\n"
        f"Client: {deal.client.name}"
        + (f" ({deal.client.company})" if deal.client.company else "")
        + f"\nDeal: {deal.title}\n"
        f"Value: ₹{deal.value:,.0f}\n"
        f"Stage: {deal.stage.replace('_', ' ').title()}"
    )
    _send(msg)


def notify_stage_changed(deal, old_stage: str) -> None:
    stage_emoji = {
        "new": "🔵", "contacted": "📞", "pitched": "🎯",
        "follow_up": "🔄", "negotiation": "🤝", "won": "🏆", "lost": "❌"
    }
    new_stage = deal.stage
    emoji = stage_emoji.get(new_stage, "📌")
    msg = (
        f"{emoji} <b>Deal Stage Updated</b>\n"
        f"Client: {deal.client.name}\n"
        f"Deal: {deal.title}\n"
        f"{old_stage.replace('_', ' ').title()} → <b>{new_stage.replace('_', ' ').title()}</b>\n"
        f"Value: ₹{deal.value:,.0f}"
    )
    _send(msg)


def notify_deal_won(deal) -> None:
    msg = (
        f"🏆 <b>Deal Won!</b>\n"
        f"Client: {deal.client.name}"
        + (f" ({deal.client.company})" if deal.client.company else "")
        + f"\nDeal: {deal.title}\n"
        f"💰 Value: ₹{deal.value:,.0f}"
    )
    _send(msg)


def notify_meeting_reminder(activity) -> None:
    deal = activity.deal
    time_str = activity.scheduled_at.strftime("%I:%M %p") if activity.scheduled_at else "—"
    location = activity.location or activity.meeting_link or "—"
    msg = (
        f"⏰ <b>Meeting Reminder</b>\n"
        f"In {config.MEETING_REMINDER_MINUTES} minutes\n"
        f"Client: {deal.client.name}"
        + (f" ({deal.client.company})" if deal.client.company else "")
        + f"\nDeal: {deal.title}\n"
        f"Time: {time_str}\n"
        f"Where: {location}"
    )
    _send(msg)


def send_daily_digest(deals_won_today, upcoming_meetings, stale_deals) -> None:
    total_value = sum(d.value for d in deals_won_today)
    date_str = datetime.now().strftime("%d %b %Y")

    msg = f"📊 <b>Daily Digest — {date_str}</b>\n\n"

    if deals_won_today:
        msg += f"✅ <b>Deals Closed Today ({len(deals_won_today)})</b>\n"
        for d in deals_won_today:
            msg += f"  • {d.client.name} — ₹{d.value:,.0f}\n"
        msg += f"  Total: ₹{total_value:,.0f}\n\n"
    else:
        msg += "No deals closed today.\n\n"

    if upcoming_meetings:
        msg += f"📅 <b>Upcoming Meetings Tomorrow ({len(upcoming_meetings)})</b>\n"
        for a in upcoming_meetings:
            t = a.scheduled_at.strftime("%I:%M %p") if a.scheduled_at else "—"
            loc = a.location or a.meeting_link or "—"
            msg += f"  • {a.deal.client.name} at {t} — {loc}\n"
        msg += "\n"

    if stale_deals:
        msg += f"⚠️ <b>Follow-up Needed ({len(stale_deals)})</b>\n"
        for d in stale_deals:
            msg += f"  • {d.client.name} — {d.days_in_stage}d in {d.stage.replace('_', ' ').title()}\n"

    _send(msg)


def send_weekly_digest(stats: dict) -> None:
    msg = (
        f"📈 <b>Weekly Digest</b>\n\n"
        f"Deals Won: {stats['won_count']} (₹{stats['won_value']:,.0f})\n"
        f"Deals Lost: {stats['lost_count']}\n"
        f"New Deals Added: {stats['new_count']}\n"
        f"Meetings Held: {stats['meetings_count']}\n"
        f"Active Pipeline: ₹{stats['pipeline_value']:,.0f}"
    )
    _send(msg)


# ── Task notifications ────────────────────────────────────────────────────────

def notify_task_assigned(task) -> None:
    priority_emoji = {"low": "🟢", "medium": "🟡", "high": "🟠", "urgent": "🔴"}
    due = task.due_date.strftime("%d %b %Y") if task.due_date else "No due date"
    msg = (
        f"📌 <b>Task Assigned</b>\n"
        f"Title: {task.title}\n"
        f"Assigned to: {task.assigned_to or '—'}\n"
        f"Priority: {priority_emoji.get(task.priority, '')} {task.priority.title()}\n"
        f"Due: {due}"
        + (f"\nDeal: {task.deal.title} ({task.deal.client.name})" if task.deal else "")
    )
    _send(msg)


def notify_task_due_tomorrow(task) -> None:
    msg = (
        f"⏰ <b>Task Due Tomorrow</b>\n"
        f"Title: {task.title}\n"
        f"Assigned to: {task.assigned_to or '—'}\n"
        f"Due: {task.due_date.strftime('%d %b %Y')}"
        + (f"\nDeal: {task.deal.title}" if task.deal else "")
    )
    _send(msg)


def notify_task_overdue(task) -> None:
    from datetime import date
    days_late = (date.today() - task.due_date).days
    msg = (
        f"🔴 <b>Task Overdue</b>\n"
        f"Title: {task.title}\n"
        f"Assigned to: {task.assigned_to or '—'}\n"
        f"Was due: {task.due_date.strftime('%d %b %Y')} ({days_late}d ago)"
        + (f"\nDeal: {task.deal.title}" if task.deal else "")
    )
    _send(msg)


def notify_task_done(task, completed_by: str) -> None:
    msg = (
        f"✅ <b>Task Completed</b>\n"
        f"Title: {task.title}\n"
        f"Completed by: {completed_by}"
        + (f"\nDeal: {task.deal.title}" if task.deal else "")
    )
    _send(msg)


# ── Calendar notifications ────────────────────────────────────────────────────

def notify_calendar_event_created(event) -> None:
    type_emoji = {"meeting": "📅", "call": "📞", "demo": "🎯", "internal": "🏢", "other": "📌"}
    emoji = type_emoji.get(event.event_type, "📅")
    time_part = f" at {event.start_time}" if event.start_time else ""
    attendee_part = f"\nAttendees: {event.attendees}" if event.attendees else ""
    where_part = f"\nWhere: {event.location}" if event.location else (f"\nLink: {event.meeting_link}" if event.meeting_link else "")
    msg = (
        f"{emoji} <b>New Event Scheduled</b>\n"
        f"{event.title}\n"
        f"Date: {event.date.strftime('%d %b %Y')}{time_part}"
        f"{attendee_part}"
        f"{where_part}"
        + (f"\nBy: {event.created_by}" if event.created_by and event.created_by != "system" else "")
    )
    _send(msg)


# ── Interview notifications ───────────────────────────────────────────────────

def notify_interview_added(app_obj) -> None:
    ctc_part = f"\nExpected: ₹{app_obj.expected_ctc:.1f} LPA" if app_obj.expected_ctc else ""
    msg = (
        f"🆕 <b>New Application Added</b>\n"
        f"Company: {app_obj.company}\n"
        f"Role: {app_obj.role}\n"
        f"Source: {app_obj.source.title()}"
        f"{ctc_part}"
    )
    _send(msg)


def notify_interview_status_changed(app_obj, old_status: str) -> None:
    status_emoji = {
        "applied": "📝", "shortlisted": "⭐", "in_progress": "🔄",
        "offer_received": "🎉", "accepted": "🏆", "rejected": "❌", "withdrawn": "🚫",
    }
    new_status = app_obj.status
    emoji = status_emoji.get(new_status, "📌")
    old_label = old_status.replace("_", " ").title()
    new_label = new_status.replace("_", " ").title()
    msg = (
        f"{emoji} <b>Application Status Updated</b>\n"
        f"Company: {app_obj.company} — {app_obj.role}\n"
        f"{old_label} → <b>{new_label}</b>"
        + (f"\nOffered CTC: ₹{app_obj.offered_ctc:.1f} LPA" if new_status == "offer_received" and app_obj.offered_ctc else "")
    )
    _send(msg)


def notify_interview_round_scheduled(app_obj, rnd) -> None:
    type_label = {
        "hr_screening": "HR Screening", "technical": "Technical", "managerial": "Managerial",
        "hr_final": "HR Final", "salary_discussion": "Salary Discussion",
        "assignment": "Assignment", "other": "Other",
    }
    mode_emoji = {"phone": "📞", "video": "💻", "in_person": "🏢"}
    label = type_label.get(rnd.round_type, rnd.round_type)
    mode_icon = mode_emoji.get(rnd.mode, "")
    time_part = rnd.scheduled_at.strftime("%d %b %Y, %I:%M %p") if rnd.scheduled_at else "TBD"
    msg = (
        f"📋 <b>Interview Round Scheduled — R{rnd.round_number}</b>\n"
        f"Company: {app_obj.company}\n"
        f"Round: {label}\n"
        f"When: {time_part}\n"
        f"Mode: {mode_icon} {rnd.mode.replace('_', ' ').title()}"
        + (f"\nInterviewer: {rnd.interviewer}" if rnd.interviewer else "")
        + (f"\nLink: {rnd.meeting_link}" if rnd.meeting_link else "")
    )
    _send(msg)


def notify_interview_round_result(app_obj, rnd) -> None:
    result_emoji = {"cleared": "✅", "not_cleared": "❌", "cancelled": "🚫", "waiting": "⏳"}
    emoji = result_emoji.get(rnd.result, "📌")
    type_label = {
        "hr_screening": "HR Screening", "technical": "Technical", "managerial": "Managerial",
        "hr_final": "HR Final", "salary_discussion": "Salary Discussion",
        "assignment": "Assignment", "other": "Other",
    }
    label = type_label.get(rnd.round_type, rnd.round_type)
    msg = (
        f"{emoji} <b>Round Result Updated</b>\n"
        f"Company: {app_obj.company}\n"
        f"Round {rnd.round_number}: {label}\n"
        f"Result: <b>{rnd.result.replace('_', ' ').title()}</b>"
        + (f"\nFeedback: {rnd.feedback}" if rnd.feedback else "")
    )
    _send(msg)
