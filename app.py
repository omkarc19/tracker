from datetime import datetime, timedelta, date
import base64
import csv
import io
import logging

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, Response

import config
import notifications
import scheduler
import re
import requests as http_requests
from icalendar import Calendar as iCalendar

from models import (db, Client, Deal, Activity, STAGES, ACTIVITY_TYPES,
                    Task, TaskComment, TaskSubtask, TaskActivity,
                    TASK_STATUSES, TASK_PRIORITIES, TASK_LABELS, RECUR_INTERVALS,
                    DEAL_LABELS, CalendarEvent, EVENT_TYPES, TeamMember,
                    Application, InterviewRound, AppDocument,
                    APPLICATION_STATUSES, APPLICATION_SOURCES, WORK_MODES,
                    ROUND_TYPES, ROUND_MODES, ROUND_RESULTS, AppSetting)

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)


# ── Basic Auth ────────────────────────────────────────────────────────────────

def _unauthorized():
    return Response(
        "Authentication required.",
        401,
        {"WWW-Authenticate": 'Basic realm="Sales Tracker"'},
    )


@app.before_request
def require_auth():
    # Skip auth for static files
    if request.path.startswith("/static/"):
        return
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return _unauthorized()
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return _unauthorized()
    if username != config.AUTH_USERNAME or password != config.AUTH_PASSWORD:
        return _unauthorized()
app.config["SQLALCHEMY_DATABASE_URI"] = config.DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = config.SECRET_KEY

db.init_app(app)

with app.app_context():
    db.create_all()
    # Add new columns to existing tables without dropping data
    with db.engine.connect() as conn:
        for sql in [
            "ALTER TABLE clients ADD COLUMN notes TEXT",
            "ALTER TABLE deals ADD COLUMN labels VARCHAR(300) DEFAULT ''",
            "ALTER TABLE calendar_events ADD COLUMN external_id VARCHAR(500)",
            "ALTER TABLE calendar_events ADD COLUMN gcal_synced BOOLEAN DEFAULT 0",
        ]:
            try:
                conn.execute(db.text(sql))
                conn.commit()
            except Exception:
                pass  # column already exists

scheduler.start(app)


# ── Search ───────────────────────────────────────────────────────────────────

@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    deals, tasks = [], []
    if q:
        like = f"%{q}%"
        deals = (Deal.query
                 .join(Client)
                 .filter(db.or_(
                     Client.name.ilike(like),
                     Client.company.ilike(like),
                     Deal.title.ilike(like),
                     Deal.salesperson.ilike(like),
                 ))
                 .order_by(Deal.updated_at.desc())
                 .all())
        tasks = (Task.query
                 .filter(db.or_(
                     Task.title.ilike(like),
                     Task.description.ilike(like),
                     Task.assigned_to.ilike(like),
                 ))
                 .order_by(Task.updated_at.desc())
                 .all())
    return render_template("search.html", q=q, deals=deals, tasks=tasks)


# ── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    today = date.today()
    tomorrow = today + timedelta(days=1)
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    day_after_start = today_start + timedelta(days=2)

    today_meetings = Activity.query.filter(
        Activity.type == "meeting",
        Activity.scheduled_at >= today_start,
        Activity.scheduled_at < tomorrow_start,
    ).order_by(Activity.scheduled_at).all()

    stale_cutoff = datetime.utcnow() - timedelta(days=5)
    stale_deals = Deal.query.filter(
        Deal.stage.notin_(["won", "lost"]),
        Deal.updated_at <= stale_cutoff,
    ).order_by(Deal.updated_at).limit(5).all()

    pipeline_by_stage = {}
    for stage in STAGES:
        deals = Deal.query.filter_by(stage=stage).all()
        pipeline_by_stage[stage] = {
            "count": len(deals),
            "value": sum(d.value for d in deals),
        }

    week_start = datetime.utcnow() - timedelta(days=7)
    won_this_week = Deal.query.filter(
        Deal.stage == "won",
        Deal.updated_at >= week_start,
    ).all()
    won_value = sum(d.value for d in won_this_week)

    tasks_today = Task.query.filter(
        Task.due_date == today,
        Task.status != "done",
    ).order_by(Task.priority.desc()).all()

    tasks_tomorrow = Task.query.filter(
        Task.due_date == tomorrow,
        Task.status != "done",
    ).order_by(Task.priority.desc()).all()

    overdue_tasks = Task.query.filter(
        Task.due_date < today,
        Task.status != "done",
    ).order_by(Task.due_date.asc()).limit(5).all()

    return render_template(
        "dashboard.html",
        today_meetings=today_meetings,
        stale_deals=stale_deals,
        pipeline_by_stage=pipeline_by_stage,
        stages=STAGES,
        won_this_week=len(won_this_week),
        won_value=won_value,
        tasks_today=tasks_today,
        tasks_tomorrow=tasks_tomorrow,
        overdue_tasks=overdue_tasks,
    )


# ── Pipeline ─────────────────────────────────────────────────────────────────

@app.route("/pipeline")
def pipeline():
    label_filter = request.args.get("label", "")
    salesperson_filter = request.args.get("salesperson", "")

    deals_by_stage = {}
    stage_values = {}
    for stage in STAGES:
        q = Deal.query.filter_by(stage=stage)
        if label_filter:
            q = q.filter(Deal.labels.contains(label_filter))
        if salesperson_filter:
            q = q.filter_by(salesperson=salesperson_filter)
        deals = q.order_by(Deal.updated_at.desc()).all()
        deals_by_stage[stage] = deals
        stage_values[stage] = sum(d.value for d in deals)

    total_pipeline_value = sum(
        stage_values[s] for s in STAGES if s not in ("won", "lost")
    )

    salespersons = sorted(set(
        d[0] for d in db.session.query(Deal.salesperson)
        .filter(Deal.salesperson != None).distinct().all()
        if d[0]
    ))

    return render_template(
        "pipeline.html",
        deals_by_stage=deals_by_stage,
        stages=STAGES,
        stage_values=stage_values,
        total_pipeline_value=total_pipeline_value,
        deal_labels=DEAL_LABELS,
        label_filter=label_filter,
        salespersons=salespersons,
        salesperson_filter=salesperson_filter,
    )


# ── Clients ───────────────────────────────────────────────────────────────────

@app.route("/clients/new", methods=["GET", "POST"])
def new_client():
    if request.method == "POST":
        client = Client(
            name=request.form["name"].strip(),
            company=request.form.get("company", "").strip() or None,
            phone=request.form.get("phone", "").strip() or None,
            email=request.form.get("email", "").strip() or None,
            source=request.form.get("source", "").strip() or None,
            notes=request.form.get("client_notes", "").strip() or None,
        )
        db.session.add(client)
        db.session.flush()

        label_vals = request.form.getlist("deal_labels")
        deal = Deal(
            client_id=client.id,
            title=request.form["deal_title"].strip(),
            value=float(request.form.get("deal_value") or 0),
            stage="new",
            salesperson=request.form.get("salesperson", "").strip() or None,
            notes=request.form.get("notes", "").strip() or None,
            labels=",".join(label_vals),
        )
        db.session.add(deal)
        db.session.commit()

        notifications.notify_deal_created(deal)
        flash("Deal added successfully.", "success")
        return redirect(url_for("deal_detail", deal_id=deal.id))

    return render_template("new_client.html", deal_labels=DEAL_LABELS)


# ── Deal detail ───────────────────────────────────────────────────────────────

@app.route("/deal/<int:deal_id>")
def deal_detail(deal_id):
    deal = Deal.query.get_or_404(deal_id)
    activities = Activity.query.filter_by(deal_id=deal_id).order_by(Activity.scheduled_at.desc()).all()
    return render_template(
        "deal.html",
        deal=deal,
        activities=activities,
        stages=STAGES,
        activity_types=ACTIVITY_TYPES,
    )


@app.route("/deal/<int:deal_id>/stage", methods=["POST"])
def update_stage(deal_id):
    deal = Deal.query.get_or_404(deal_id)
    old_stage = deal.stage
    new_stage = request.form["stage"]
    if new_stage in STAGES and new_stage != old_stage:
        deal.stage = new_stage
        deal.updated_at = datetime.utcnow()
        db.session.commit()
        if new_stage == "won":
            notifications.notify_deal_won(deal)
        else:
            notifications.notify_stage_changed(deal, old_stage)
        flash(f"Stage updated to {new_stage.replace('_', ' ').title()}.", "success")
    return redirect(url_for("deal_detail", deal_id=deal_id))


@app.route("/api/deal/<int:deal_id>/quick-log", methods=["POST"])
def api_quick_log(deal_id):
    Deal.query.get_or_404(deal_id)
    data = request.get_json()
    scheduled_raw = data.get("scheduled_at", "").strip()
    scheduled_at = None
    if scheduled_raw:
        try:
            scheduled_at = datetime.strptime(scheduled_raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            pass
    activity = Activity(
        deal_id=deal_id,
        type=data.get("type", "call"),
        scheduled_at=scheduled_at or datetime.utcnow(),
        notes=data.get("notes", "").strip() or None,
        outcome=data.get("outcome", "").strip() or None,
    )
    db.session.add(activity)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/deal/<int:deal_id>/stage", methods=["POST"])
def api_update_stage(deal_id):
    deal = Deal.query.get_or_404(deal_id)
    data = request.get_json()
    new_stage = data.get("stage")
    if not new_stage or new_stage not in STAGES:
        return jsonify({"error": "invalid stage"}), 400
    old_stage = deal.stage
    if new_stage != old_stage:
        deal.stage = new_stage
        deal.updated_at = datetime.utcnow()
        db.session.commit()
        if new_stage == "won":
            notifications.notify_deal_won(deal)
        else:
            notifications.notify_stage_changed(deal, old_stage)
    return jsonify({"ok": True, "stage": deal.stage})


@app.route("/deal/<int:deal_id>/edit", methods=["GET", "POST"])
def edit_deal(deal_id):
    deal = Deal.query.get_or_404(deal_id)
    if request.method == "POST":
        deal.title = request.form["title"].strip()
        deal.value = float(request.form.get("value") or 0)
        deal.salesperson = request.form.get("salesperson", "").strip() or None
        deal.notes = request.form.get("notes", "").strip() or None
        deal.labels = ",".join(request.form.getlist("deal_labels"))
        deal.client.name = request.form["client_name"].strip()
        deal.client.company = request.form.get("client_company", "").strip() or None
        deal.client.phone = request.form.get("client_phone", "").strip() or None
        deal.client.email = request.form.get("client_email", "").strip() or None
        deal.client.notes = request.form.get("client_notes", "").strip() or None
        deal.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Deal updated.", "success")
        return redirect(url_for("deal_detail", deal_id=deal_id))
    return render_template("edit_deal.html", deal=deal, deal_labels=DEAL_LABELS)


@app.route("/deal/<int:deal_id>/delete", methods=["POST"])
def delete_deal(deal_id):
    deal = Deal.query.get_or_404(deal_id)
    db.session.delete(deal)
    db.session.commit()
    flash("Deal deleted.", "info")
    return redirect(url_for("pipeline"))


# ── Activities ────────────────────────────────────────────────────────────────

@app.route("/deal/<int:deal_id>/activity/add", methods=["POST"])
def add_activity(deal_id):
    Deal.query.get_or_404(deal_id)
    scheduled_raw = request.form.get("scheduled_at", "").strip()
    scheduled_at = None
    if scheduled_raw:
        try:
            scheduled_at = datetime.strptime(scheduled_raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            pass

    activity = Activity(
        deal_id=deal_id,
        type=request.form.get("type", "meeting"),
        scheduled_at=scheduled_at,
        location=request.form.get("location", "").strip() or None,
        meeting_link=request.form.get("meeting_link", "").strip() or None,
        notes=request.form.get("notes", "").strip() or None,
        outcome=request.form.get("outcome", "").strip() or None,
    )
    db.session.add(activity)
    db.session.commit()
    flash("Activity logged.", "success")
    return redirect(url_for("deal_detail", deal_id=deal_id))


@app.route("/activity/<int:activity_id>/delete", methods=["POST"])
def delete_activity(activity_id):
    activity = Activity.query.get_or_404(activity_id)
    deal_id = activity.deal_id
    db.session.delete(activity)
    db.session.commit()
    flash("Activity removed.", "info")
    return redirect(url_for("deal_detail", deal_id=deal_id))



# ── Tasks ─────────────────────────────────────────────────────────────────────

def _log_task(task, actor, action):
    db.session.add(TaskActivity(task_id=task.id, actor=actor, action=action))


@app.route("/tasks")
def tasks_board():
    status_filter = request.args.get("status")
    assignee_filter = request.args.get("assignee")
    priority_filter = request.args.get("priority")
    label_filter = request.args.get("label")

    query = Task.query
    if assignee_filter:
        query = query.filter_by(assigned_to=assignee_filter)
    if priority_filter:
        query = query.filter_by(priority=priority_filter)
    if label_filter:
        query = query.filter(Task.labels.contains(label_filter))

    tasks_by_status = {}
    for status in TASK_STATUSES:
        q = query.filter_by(status=status).order_by(Task.due_date.asc().nullslast(), Task.created_at.desc())
        tasks_by_status[status] = q.all()

    assignees = db.session.query(Task.assigned_to).filter(Task.assigned_to != None).distinct().all()
    assignees = sorted(set(a[0] for a in assignees if a[0]))

    return render_template(
        "tasks_board.html",
        tasks_by_status=tasks_by_status,
        statuses=TASK_STATUSES,
        priorities=TASK_PRIORITIES,
        labels=TASK_LABELS,
        assignees=assignees,
        filters={"assignee": assignee_filter, "priority": priority_filter, "label": label_filter},
    )


@app.route("/tasks/mine")
def my_tasks():
    person = request.args.get("person", "")
    status_filter = request.args.get("status", "")

    query = Task.query
    if person:
        query = query.filter_by(assigned_to=person)
    if status_filter and status_filter in TASK_STATUSES:
        query = query.filter_by(status=status_filter)
    tasks = query.order_by(Task.due_date.asc().nullslast(), Task.priority.asc()).all()

    assignees = db.session.query(Task.assigned_to).filter(Task.assigned_to != None).distinct().all()
    assignees = sorted(set(a[0] for a in assignees if a[0]))

    return render_template(
        "my_tasks.html",
        tasks=tasks,
        statuses=TASK_STATUSES,
        assignees=assignees,
        person=person,
        status_filter=status_filter,
    )


@app.route("/tasks/new", methods=["GET", "POST"])
def new_task():
    if request.method == "POST":
        label_vals = request.form.getlist("labels")
        due_raw = request.form.get("due_date", "").strip()
        due_date = date.fromisoformat(due_raw) if due_raw else None
        is_recurring = bool(request.form.get("is_recurring"))

        task = Task(
            title=request.form["title"].strip(),
            description=request.form.get("description", "").strip() or None,
            status="todo",
            priority=request.form.get("priority", "medium"),
            due_date=due_date,
            assigned_to=request.form.get("assigned_to", "").strip() or None,
            created_by=request.form.get("created_by", "").strip() or None,
            deal_id=request.form.get("deal_id") or None,
            labels=",".join(label_vals),
            is_recurring=is_recurring,
            recur_interval=request.form.get("recur_interval") if is_recurring else None,
        )
        db.session.add(task)
        db.session.flush()
        _log_task(task, task.created_by or "system", "Task created")
        db.session.commit()

        notifications.notify_task_assigned(task)
        flash("Task created.", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    deals = Deal.query.filter(Deal.stage.notin_(["won", "lost"])).order_by(Deal.created_at.desc()).all()
    return render_template(
        "new_task.html",
        deals=deals,
        priorities=TASK_PRIORITIES,
        labels=TASK_LABELS,
        recur_intervals=RECUR_INTERVALS,
    )


@app.route("/tasks/<int:task_id>")
def task_detail(task_id):
    task = Task.query.get_or_404(task_id)
    deals = Deal.query.filter(Deal.stage.notin_(["won", "lost"])).order_by(Deal.created_at.desc()).all()
    return render_template(
        "task_detail.html",
        task=task,
        statuses=TASK_STATUSES,
        priorities=TASK_PRIORITIES,
        labels=TASK_LABELS,
        recur_intervals=RECUR_INTERVALS,
        deals=deals,
    )


@app.route("/tasks/<int:task_id>/edit", methods=["POST"])
def edit_task(task_id):
    task = Task.query.get_or_404(task_id)
    actor = request.form.get("edited_by", "someone").strip() or "someone"

    label_vals = request.form.getlist("labels")
    due_raw = request.form.get("due_date", "").strip()
    due_date = date.fromisoformat(due_raw) if due_raw else None
    is_recurring = bool(request.form.get("is_recurring"))

    old_assignee = task.assigned_to
    new_assignee = request.form.get("assigned_to", "").strip() or None

    task.title = request.form["title"].strip()
    task.description = request.form.get("description", "").strip() or None
    task.priority = request.form.get("priority", "medium")
    task.due_date = due_date
    task.assigned_to = new_assignee
    task.deal_id = request.form.get("deal_id") or None
    task.labels = ",".join(label_vals)
    task.is_recurring = is_recurring
    task.recur_interval = request.form.get("recur_interval") if is_recurring else None
    task.updated_at = datetime.utcnow()

    _log_task(task, actor, "Task updated")
    if new_assignee and new_assignee != old_assignee:
        _log_task(task, actor, f"Assigned to {new_assignee}")
        notifications.notify_task_assigned(task)

    db.session.commit()
    flash("Task updated.", "success")
    return redirect(url_for("task_detail", task_id=task_id))


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
def delete_task(task_id):
    task = Task.query.get_or_404(task_id)
    db.session.delete(task)
    db.session.commit()
    flash("Task deleted.", "info")
    return redirect(url_for("tasks_board"))


# ── Task status (drag-drop AJAX) ──────────────────────────────────────────────

@app.route("/api/tasks/<int:task_id>/status", methods=["POST"])
def api_update_task_status(task_id):
    task = Task.query.get_or_404(task_id)
    data = request.get_json()
    new_status = data.get("status")
    actor = data.get("actor", "someone")

    if not new_status or new_status not in TASK_STATUSES:
        return jsonify({"error": "invalid status"}), 400

    old_status = task.status
    if new_status != old_status:
        task.status = new_status
        task.updated_at = datetime.utcnow()
        _log_task(task, actor, f"Status changed: {old_status} → {new_status}")

        if new_status == "done":
            # Notify completion
            notifications.notify_task_done(task, actor)
            # Spawn next recurrence if recurring
            if task.is_recurring and task.recur_interval and task.due_date:
                from dateutil.relativedelta import relativedelta
                delta_map = {
                    "daily": timedelta(days=1),
                    "weekly": timedelta(weeks=1),
                }
                if task.recur_interval == "monthly":
                    next_due = task.due_date + relativedelta(months=1)
                else:
                    next_due = task.due_date + delta_map.get(task.recur_interval, timedelta(weeks=1))

                new_task = Task(
                    title=task.title,
                    description=task.description,
                    status="todo",
                    priority=task.priority,
                    due_date=next_due,
                    assigned_to=task.assigned_to,
                    created_by=task.created_by,
                    deal_id=task.deal_id,
                    labels=task.labels,
                    is_recurring=True,
                    recur_interval=task.recur_interval,
                )
                db.session.add(new_task)
                db.session.flush()
                _log_task(new_task, "system", f"Auto-created from recurring task #{task.id}")

        db.session.commit()

    return jsonify({"ok": True, "status": task.status})


# ── Task comments ─────────────────────────────────────────────────────────────

@app.route("/tasks/<int:task_id>/comment", methods=["POST"])
def add_task_comment(task_id):
    task = Task.query.get_or_404(task_id)
    author = request.form.get("author", "").strip() or "Anonymous"
    body = request.form.get("body", "").strip()
    if body:
        comment = TaskComment(task_id=task_id, author=author, body=body)
        db.session.add(comment)
        _log_task(task, author, "Left a comment")
        db.session.commit()
    return redirect(url_for("task_detail", task_id=task_id))


@app.route("/tasks/comment/<int:comment_id>/delete", methods=["POST"])
def delete_task_comment(comment_id):
    comment = TaskComment.query.get_or_404(comment_id)
    task_id = comment.task_id
    db.session.delete(comment)
    db.session.commit()
    return redirect(url_for("task_detail", task_id=task_id))


# ── Task subtasks ─────────────────────────────────────────────────────────────

@app.route("/tasks/<int:task_id>/subtask", methods=["POST"])
def add_subtask(task_id):
    Task.query.get_or_404(task_id)
    title = request.form.get("title", "").strip()
    if title:
        db.session.add(TaskSubtask(task_id=task_id, title=title))
        db.session.commit()
    return redirect(url_for("task_detail", task_id=task_id))


@app.route("/tasks/subtask/<int:subtask_id>/toggle", methods=["POST"])
def toggle_subtask(subtask_id):
    subtask = TaskSubtask.query.get_or_404(subtask_id)
    subtask.done = not subtask.done
    db.session.commit()
    return redirect(url_for("task_detail", task_id=subtask.task_id))


@app.route("/tasks/subtask/<int:subtask_id>/delete", methods=["POST"])
def delete_subtask(subtask_id):
    subtask = TaskSubtask.query.get_or_404(subtask_id)
    task_id = subtask.task_id
    db.session.delete(subtask)
    db.session.commit()
    return redirect(url_for("task_detail", task_id=task_id))


# ── Exports ───────────────────────────────────────────────────────────────────

@app.route("/export/deals")
def export_deals():
    deals = Deal.query.order_by(Deal.created_at.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Deal ID", "Client Name", "Company", "Phone", "Email", "Source",
        "Deal Title", "Value (₹)", "Stage", "Salesperson", "Notes",
        "Created Date", "Last Updated",
    ])
    for d in deals:
        writer.writerow([
            d.id,
            d.client.name,
            d.client.company or "",
            d.client.phone or "",
            d.client.email or "",
            d.client.source or "",
            d.title,
            d.value,
            d.stage.replace("_", " ").title(),
            d.salesperson or "",
            (d.notes or "").replace("\n", " "),
            d.created_at.strftime("%Y-%m-%d %H:%M"),
            d.updated_at.strftime("%Y-%m-%d %H:%M"),
        ])

    filename = f"deals_{date.today().isoformat()}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/export/activities")
def export_activities():
    activities = Activity.query.order_by(Activity.scheduled_at.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Activity ID", "Client Name", "Deal Title", "Type",
        "Scheduled At", "Location", "Meeting Link", "Notes", "Outcome",
        "Created At",
    ])
    for a in activities:
        writer.writerow([
            a.id,
            a.deal.client.name,
            a.deal.title,
            a.type.replace("_", " ").title(),
            a.scheduled_at.strftime("%Y-%m-%d %H:%M") if a.scheduled_at else "",
            a.location or "",
            a.meeting_link or "",
            (a.notes or "").replace("\n", " "),
            (a.outcome or "").replace("\n", " "),
            a.created_at.strftime("%Y-%m-%d %H:%M"),
        ])

    filename = f"activities_{date.today().isoformat()}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/export/tasks")
def export_tasks():
    tasks = Task.query.order_by(Task.created_at.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Task ID", "Title", "Description", "Status", "Priority",
        "Assigned To", "Created By", "Due Date", "Labels",
        "Linked Deal", "Linked Client",
        "Subtasks Total", "Subtasks Done",
        "Comments", "Is Recurring", "Recur Interval",
        "Created At", "Updated At",
    ])
    for t in tasks:
        writer.writerow([
            t.id,
            t.title,
            (t.description or "").replace("\n", " "),
            t.status.replace("_", " ").title(),
            t.priority.title(),
            t.assigned_to or "",
            t.created_by or "",
            t.due_date.isoformat() if t.due_date else "",
            ", ".join(t.label_list),
            t.deal.title if t.deal else "",
            t.deal.client.name if t.deal else "",
            len(t.subtasks),
            t.subtasks_done,
            len(t.comments),
            "Yes" if t.is_recurring else "No",
            t.recur_interval or "",
            t.created_at.strftime("%Y-%m-%d %H:%M"),
            t.updated_at.strftime("%Y-%m-%d %H:%M"),
        ])

    filename = f"tasks_{date.today().isoformat()}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── Calendar ──────────────────────────────────────────────────────────────────

@app.route("/calendar")
def calendar_view():
    members = TeamMember.query.filter_by(active=True).order_by(TeamMember.name).all()
    all_attendees = set(m.name for m in members)
    for ev in CalendarEvent.query.all():
        for a in ev.attendee_list:
            all_attendees.add(a)
    return render_template("calendar.html", all_attendees=sorted(all_attendees))


@app.route("/api/calendar/events")
def api_calendar_events():
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    person = request.args.get("person", "").strip()

    if not year or not month:
        today = date.today()
        year, month = today.year, today.month

    first_day = date(year, month, 1)
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)

    events = (CalendarEvent.query
              .filter(CalendarEvent.date >= first_day, CalendarEvent.date <= last_day)
              .order_by(CalendarEvent.date, CalendarEvent.start_time)
              .all())

    if person:
        events = [e for e in events if person in e.attendee_list or e.created_by == person]

    return jsonify([e.to_dict() for e in events])


@app.route("/calendar/new", methods=["GET", "POST"])
def new_calendar_event():
    if request.method == "POST":
        date_raw = request.form.get("date", "").strip()
        event_date = date.fromisoformat(date_raw) if date_raw else date.today()
        event = CalendarEvent(
            title=request.form["title"].strip(),
            description=request.form.get("description", "").strip() or None,
            date=event_date,
            start_time=request.form.get("start_time", "").strip() or None,
            end_time=request.form.get("end_time", "").strip() or None,
            location=request.form.get("location", "").strip() or None,
            meeting_link=request.form.get("meeting_link", "").strip() or None,
            event_type=request.form.get("event_type", "meeting"),
            attendees=request.form.get("attendees", "").strip(),
            created_by=request.form.get("created_by", "").strip() or None,
            notes=request.form.get("notes", "").strip() or None,
        )
        db.session.add(event)
        db.session.commit()
        notifications.notify_calendar_event_created(event)
        flash("Event created.", "success")
        return redirect(url_for("calendar_event_detail", event_id=event.id))

    prefill_date = request.args.get("date", date.today().isoformat())
    return render_template("new_calendar_event.html", prefill_date=prefill_date, event_types=EVENT_TYPES)


@app.route("/calendar/<int:event_id>")
def calendar_event_detail(event_id):
    event = CalendarEvent.query.get_or_404(event_id)
    return render_template("calendar_event.html", event=event, event_types=EVENT_TYPES)


@app.route("/calendar/<int:event_id>/edit", methods=["GET", "POST"])
def edit_calendar_event(event_id):
    event = CalendarEvent.query.get_or_404(event_id)
    if request.method == "POST":
        date_raw = request.form.get("date", "").strip()
        event.title = request.form["title"].strip()
        event.description = request.form.get("description", "").strip() or None
        event.date = date.fromisoformat(date_raw) if date_raw else event.date
        event.start_time = request.form.get("start_time", "").strip() or None
        event.end_time = request.form.get("end_time", "").strip() or None
        event.location = request.form.get("location", "").strip() or None
        event.meeting_link = request.form.get("meeting_link", "").strip() or None
        event.event_type = request.form.get("event_type", "meeting")
        event.attendees = request.form.get("attendees", "").strip()
        event.created_by = request.form.get("created_by", "").strip() or None
        event.notes = request.form.get("notes", "").strip() or None
        event.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Event updated.", "success")
        return redirect(url_for("calendar_event_detail", event_id=event_id))
    return render_template("edit_calendar_event.html", event=event, event_types=EVENT_TYPES)


@app.route("/calendar/<int:event_id>/delete", methods=["POST"])
def delete_calendar_event(event_id):
    event = CalendarEvent.query.get_or_404(event_id)
    db.session.delete(event)
    db.session.commit()
    flash("Event deleted.", "info")
    return redirect(url_for("calendar_view"))


# ── Google Calendar ICS Sync ─────────────────────────────────────────────────

@app.route("/settings/gcal", methods=["GET", "POST"])
def settings_gcal():
    if request.method == "POST":
        url = (request.get_json() or {}).get("url", "").strip()
        setting = AppSetting.query.get("gcal_ics_url")
        if setting:
            setting.value = url
        else:
            db.session.add(AppSetting(key="gcal_ics_url", value=url))
        db.session.commit()
        return jsonify({"ok": True})
    setting = AppSetting.query.get("gcal_ics_url")
    return jsonify({"url": setting.value if setting else ""})


@app.route("/api/calendar/sync", methods=["POST"])
def sync_gcal():
    setting = AppSetting.query.get("gcal_ics_url")
    if not setting or not setting.value:
        return jsonify({"error": "No ICS URL configured"}), 400
    try:
        resp = http_requests.get(setting.value, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return jsonify({"error": f"Fetch failed: {e}"}), 502

    try:
        cal = iCalendar.from_ical(resp.content)
    except Exception as e:
        return jsonify({"error": f"Parse failed: {e}"}), 400

    imported = updated = 0
    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        uid = str(component.get("UID", ""))
        if not uid:
            continue

        summary = str(component.get("SUMMARY", "No Title"))
        description = str(component.get("DESCRIPTION", "") or "")
        location_val = str(component.get("LOCATION", "") or "")

        # Date + time
        dtstart = component.get("DTSTART")
        dtend   = component.get("DTEND")
        ev_date = ev_start = ev_end = None
        if dtstart:
            dt = dtstart.dt
            if hasattr(dt, "date"):
                ev_date  = dt.date()
                ev_start = dt.strftime("%H:%M")
            else:
                ev_date = dt
        if dtend:
            dt = dtend.dt
            if hasattr(dt, "strftime"):
                ev_end = dt.strftime("%H:%M") if hasattr(dt, "hour") else None

        if not ev_date:
            continue

        # Google Meet URL
        meet_url = None
        m = re.search(r"https://meet\.google\.com/[a-z0-9-]+", description)
        if m:
            meet_url = m.group(0)
        if not meet_url:
            url_field = str(component.get("URL", "") or "")
            if "meet.google.com" in url_field:
                meet_url = url_field

        # Attendees
        raw_attendees = component.get("ATTENDEE", [])
        if not isinstance(raw_attendees, list):
            raw_attendees = [raw_attendees]
        attendee_names = []
        for a in raw_attendees:
            cn = a.params.get("CN", "") if hasattr(a, "params") else ""
            if cn:
                attendee_names.append(cn)
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
            ev = CalendarEvent(
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
            )
            db.session.add(ev)
            imported += 1

    db.session.commit()
    return jsonify({"imported": imported, "updated": updated})


# ── Team Members ──────────────────────────────────────────────────────────────

@app.route("/team")
def team_list():
    members = TeamMember.query.order_by(TeamMember.name).all()
    return render_template("team.html", members=members)


@app.route("/team/new", methods=["POST"])
def new_team_member():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name is required.", "danger")
        return redirect(url_for("team_list"))
    existing = TeamMember.query.filter(db.func.lower(TeamMember.name) == name.lower()).first()
    if existing:
        flash(f"'{name}' already exists.", "info")
    else:
        db.session.add(TeamMember(
            name=name,
            email=request.form.get("email", "").strip() or None,
            role=request.form.get("role", "").strip() or None,
        ))
        db.session.commit()
        flash(f"{name} added to team.", "success")
    return redirect(url_for("team_list"))


@app.route("/team/<int:member_id>/edit", methods=["POST"])
def edit_team_member(member_id):
    m = TeamMember.query.get_or_404(member_id)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name is required.", "danger")
        return redirect(url_for("team_list"))
    m.name = name
    m.email = request.form.get("email", "").strip() or None
    m.role = request.form.get("role", "").strip() or None
    db.session.commit()
    flash("Team member updated.", "success")
    return redirect(url_for("team_list"))


@app.route("/team/<int:member_id>/delete", methods=["POST"])
def delete_team_member(member_id):
    m = TeamMember.query.get_or_404(member_id)
    name = m.name
    db.session.delete(m)
    db.session.commit()
    flash(f"{name} removed from team.", "info")
    return redirect(url_for("team_list"))


@app.route("/api/team/members")
def api_team_members():
    members = TeamMember.query.filter_by(active=True).order_by(TeamMember.name).all()
    return jsonify([{"id": m.id, "name": m.name, "role": m.role or ""} for m in members])


# ── Interviews ────────────────────────────────────────────────────────────────

def _parse_float(val):
    try:
        return float(val) if val and val.strip() else None
    except ValueError:
        return None

def _parse_int(val):
    try:
        return int(val) if val and val.strip() else None
    except ValueError:
        return None

def _parse_date(val):
    try:
        return date.fromisoformat(val) if val and val.strip() else None
    except ValueError:
        return None


@app.route("/interviews")
def interviews():
    source_filter = request.args.get("source", "")
    mode_filter   = request.args.get("mode", "")
    apps_by_status = {}
    total = 0
    for status in APPLICATION_STATUSES:
        q = Application.query.filter_by(status=status)
        if source_filter:
            q = q.filter_by(source=source_filter)
        if mode_filter:
            q = q.filter_by(work_mode=mode_filter)
        items = q.order_by(Application.updated_at.desc()).all()
        apps_by_status[status] = items
        total += len(items)
    return render_template(
        "interviews.html",
        apps_by_status=apps_by_status,
        statuses=APPLICATION_STATUSES,
        sources=APPLICATION_SOURCES,
        work_modes=WORK_MODES,
        source_filter=source_filter,
        mode_filter=mode_filter,
        total=total,
    )


@app.route("/interviews/offers")
def interview_offers():
    apps = (Application.query
            .filter(Application.status.in_(["offer_received", "accepted"]))
            .order_by(Application.offered_ctc.desc().nullslast())
            .all())
    return render_template("interview_offers.html", apps=apps)


@app.route("/interviews/new", methods=["GET", "POST"])
def new_interview():
    if request.method == "POST":
        app_obj = Application(
            company       = request.form["company"].strip(),
            role          = request.form["role"].strip(),
            source        = request.form.get("source", "naukri"),
            applied_date  = _parse_date(request.form.get("applied_date")) or date.today(),
            status        = request.form.get("status", "applied"),
            current_ctc   = _parse_float(request.form.get("current_ctc")),
            expected_ctc  = _parse_float(request.form.get("expected_ctc")),
            offered_ctc   = _parse_float(request.form.get("offered_ctc")),
            notice_period = _parse_int(request.form.get("notice_period")),
            joining_date  = _parse_date(request.form.get("joining_date")),
            work_mode     = request.form.get("work_mode", ""),
            city          = request.form.get("city", "").strip() or None,
            hr_name       = request.form.get("hr_name", "").strip() or None,
            hr_contact    = request.form.get("hr_contact", "").strip() or None,
            notes         = request.form.get("notes", "").strip() or None,
        )
        db.session.add(app_obj)
        db.session.commit()
        notifications.notify_interview_added(app_obj)
        flash(f"Application for {app_obj.company} added.", "success")
        return redirect(url_for("interview_detail", app_id=app_obj.id))
    return render_template(
        "new_interview.html",
        statuses=APPLICATION_STATUSES,
        sources=APPLICATION_SOURCES,
        work_modes=WORK_MODES,
        today=date.today().isoformat(),
    )


@app.route("/interviews/<int:app_id>")
def interview_detail(app_id):
    app_obj = Application.query.get_or_404(app_id)
    return render_template(
        "interview_detail.html",
        app=app_obj,
        statuses=APPLICATION_STATUSES,
        round_types=ROUND_TYPES,
        round_modes=ROUND_MODES,
        round_results=ROUND_RESULTS,
    )


@app.route("/interviews/<int:app_id>/edit", methods=["GET", "POST"])
def edit_interview(app_id):
    app_obj = Application.query.get_or_404(app_id)
    if request.method == "POST":
        old_status            = app_obj.status
        app_obj.company       = request.form["company"].strip()
        app_obj.role          = request.form["role"].strip()
        app_obj.source        = request.form.get("source", "naukri")
        app_obj.applied_date  = _parse_date(request.form.get("applied_date")) or app_obj.applied_date
        app_obj.status        = request.form.get("status", "applied")
        app_obj.current_ctc   = _parse_float(request.form.get("current_ctc"))
        app_obj.expected_ctc  = _parse_float(request.form.get("expected_ctc"))
        app_obj.offered_ctc   = _parse_float(request.form.get("offered_ctc"))
        app_obj.notice_period = _parse_int(request.form.get("notice_period"))
        app_obj.joining_date  = _parse_date(request.form.get("joining_date"))
        app_obj.work_mode     = request.form.get("work_mode", "")
        app_obj.city          = request.form.get("city", "").strip() or None
        app_obj.hr_name       = request.form.get("hr_name", "").strip() or None
        app_obj.hr_contact    = request.form.get("hr_contact", "").strip() or None
        app_obj.notes         = request.form.get("notes", "").strip() or None
        app_obj.updated_at    = datetime.utcnow()
        db.session.commit()
        if app_obj.status != old_status:
            notifications.notify_interview_status_changed(app_obj, old_status)
        flash("Application updated.", "success")
        return redirect(url_for("interview_detail", app_id=app_id))
    return render_template(
        "edit_interview.html",
        app=app_obj,
        statuses=APPLICATION_STATUSES,
        sources=APPLICATION_SOURCES,
        work_modes=WORK_MODES,
    )


@app.route("/interviews/<int:app_id>/delete", methods=["POST"])
def delete_interview(app_id):
    app_obj = Application.query.get_or_404(app_id)
    company = app_obj.company
    db.session.delete(app_obj)
    db.session.commit()
    flash(f"Application for {company} deleted.", "info")
    return redirect(url_for("interviews"))


@app.route("/api/interviews/<int:app_id>/status", methods=["POST"])
def api_interview_status(app_id):
    app_obj = Application.query.get_or_404(app_id)
    data = request.get_json()
    new_status = data.get("status")
    if new_status not in APPLICATION_STATUSES:
        return jsonify({"error": "invalid status"}), 400
    old_status         = app_obj.status
    app_obj.status     = new_status
    app_obj.updated_at = datetime.utcnow()
    db.session.commit()
    if new_status != old_status:
        notifications.notify_interview_status_changed(app_obj, old_status)
    return jsonify({"ok": True})


@app.route("/interviews/<int:app_id>/round/add", methods=["POST"])
def add_interview_round(app_id):
    app_obj = Application.query.get_or_404(app_id)
    sched_raw = request.form.get("scheduled_at", "").strip()
    scheduled_at = None
    if sched_raw:
        try:
            scheduled_at = datetime.strptime(sched_raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            pass

    round_num = len(app_obj.rounds) + 1
    rnd = InterviewRound(
        application_id = app_id,
        round_number   = round_num,
        round_type     = request.form.get("round_type", "hr_screening"),
        scheduled_at   = scheduled_at,
        interviewer    = request.form.get("interviewer", "").strip() or None,
        mode           = request.form.get("mode", "video"),
        meeting_link   = request.form.get("meeting_link", "").strip() or None,
        location       = request.form.get("location", "").strip() or None,
        result         = "waiting",
    )
    db.session.add(rnd)
    db.session.flush()

    # Auto-create calendar event
    if scheduled_at:
        label = rnd.round_type.replace("_", " ").title()
        cal = CalendarEvent(
            title        = f"Interview: {app_obj.company} – {label}",
            date         = scheduled_at.date(),
            start_time   = scheduled_at.strftime("%H:%M"),
            event_type   = "meeting",
            location     = rnd.location,
            meeting_link = rnd.meeting_link,
            attendees    = rnd.interviewer or "",
            notes        = f"Round {round_num} · {app_obj.role}",
            created_by   = "system",
        )
        db.session.add(cal)
        db.session.flush()
        rnd.calendar_event_id = cal.id

    # Move application to in_progress if still at applied/shortlisted
    if app_obj.status in ("applied", "shortlisted"):
        app_obj.status = "in_progress"

    db.session.commit()
    notifications.notify_interview_round_scheduled(app_obj, rnd)
    flash("Interview round added.", "success")
    return redirect(url_for("interview_detail", app_id=app_id))


@app.route("/interviews/round/<int:round_id>/update", methods=["POST"])
def update_interview_round(round_id):
    rnd = InterviewRound.query.get_or_404(round_id)
    old_result   = rnd.result
    rnd.result   = request.form.get("result", rnd.result)
    rnd.feedback = request.form.get("feedback", "").strip() or None
    sched_raw = request.form.get("scheduled_at", "").strip()
    if sched_raw:
        try:
            rnd.scheduled_at = datetime.strptime(sched_raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            pass
    db.session.commit()
    if rnd.result != old_result and rnd.result != "waiting":
        notifications.notify_interview_round_result(rnd.application, rnd)
    flash("Round updated.", "success")
    return redirect(url_for("interview_detail", app_id=rnd.application_id))


@app.route("/interviews/round/<int:round_id>/delete", methods=["POST"])
def delete_interview_round(round_id):
    rnd = InterviewRound.query.get_or_404(round_id)
    app_id = rnd.application_id
    if rnd.calendar_event_id:
        cal = CalendarEvent.query.get(rnd.calendar_event_id)
        if cal:
            db.session.delete(cal)
    db.session.delete(rnd)
    db.session.commit()
    flash("Round removed.", "info")
    return redirect(url_for("interview_detail", app_id=app_id))


@app.route("/interviews/<int:app_id>/doc/add", methods=["POST"])
def add_app_doc(app_id):
    Application.query.get_or_404(app_id)
    title = request.form.get("title", "").strip()
    if title:
        db.session.add(AppDocument(application_id=app_id, title=title))
        db.session.commit()
    return redirect(url_for("interview_detail", app_id=app_id))


@app.route("/interviews/doc/<int:doc_id>/toggle", methods=["POST"])
def toggle_app_doc(doc_id):
    doc = AppDocument.query.get_or_404(doc_id)
    doc.done = not doc.done
    db.session.commit()
    return redirect(url_for("interview_detail", app_id=doc.application_id))


@app.route("/interviews/doc/<int:doc_id>/delete", methods=["POST"])
def delete_app_doc(doc_id):
    doc = AppDocument.query.get_or_404(doc_id)
    app_id = doc.application_id
    db.session.delete(doc)
    db.session.commit()
    return redirect(url_for("interview_detail", app_id=app_id))


if __name__ == "__main__":
    app.run(debug=True, port=5050, use_reloader=False)
