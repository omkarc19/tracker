from datetime import datetime, timedelta, date
import base64
import csv
import io
import logging

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, Response

import config
import notifications
import scheduler
from models import (db, Client, Deal, Activity, STAGES, ACTIVITY_TYPES,
                    Task, TaskComment, TaskSubtask, TaskActivity,
                    TASK_STATUSES, TASK_PRIORITIES, TASK_LABELS, RECUR_INTERVALS,
                    DEAL_LABELS)

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


if __name__ == "__main__":
    app.run(debug=True, port=5050, use_reloader=False)
