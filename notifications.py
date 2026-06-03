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
