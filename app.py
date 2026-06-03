from datetime import datetime, timedelta, date
import logging

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash

import config
import notifications
import scheduler
from models import (db, Client, Deal, Activity, Target, STAGES, ACTIVITY_TYPES,
                    Task, TaskComment, TaskSubtask, TaskActivity,
                    TASK_STATUSES, TASK_PRIORITIES, TASK_LABELS, RECUR_INTERVALS)

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = config.DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = config.SECRET_KEY

db.init_app(app)

with app.app_context():
    db.create_all()

scheduler.start(app)


# ── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today_start + timedelta(days=1)

    today_meetings = Activity.query.filter(
        Activity.type == "meeting",
        Activity.scheduled_at >= today_start,
        Activity.scheduled_at < tomorrow,
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

    weekly_target = Target.query.filter_by(period_type="weekly").order_by(Target.id.desc()).first()

    return render_template(
        "dashboard.html",
        today_meetings=today_meetings,
        stale_deals=stale_deals,
        pipeline_by_stage=pipeline_by_stage,
        stages=STAGES,
        won_this_week=len(won_this_week),
        won_value=won_value,
        weekly_target=weekly_target,
    )


# ── Pipeline ─────────────────────────────────────────────────────────────────

@app.route("/pipeline")
def pipeline():
    stage_filter = request.args.get("stage")
    query = Deal.query
    if stage_filter and stage_filter in STAGES:
        query = query.filter_by(stage=stage_filter)
    deals_by_stage = {}
    for stage in STAGES:
        deals_by_stage[stage] = Deal.query.filter_by(stage=stage).order_by(Deal.updated_at.desc()).all()
    return render_template("pipeline.html", deals_by_stage=deals_by_stage, stages=STAGES)


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
        )
        db.session.add(client)
        db.session.flush()

        deal = Deal(
            client_id=client.id,
            title=request.form["deal_title"].strip(),
            value=float(request.form.get("deal_value") or 0),
            stage="new",
            salesperson=request.form.get("salesperson", "").strip() or None,
            notes=request.form.get("notes", "").strip() or None,
        )
        db.session.add(deal)
        db.session.commit()

        notifications.notify_deal_created(deal)
        flash("Deal added successfully.", "success")
        return redirect(url_for("deal_detail", deal_id=deal.id))

    return render_template("new_client.html")


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
        deal.client.name = request.form["client_name"].strip()
        deal.client.company = request.form.get("client_company", "").strip() or None
        deal.client.phone = request.form.get("client_phone", "").strip() or None
        deal.client.email = request.form.get("client_email", "").strip() or None
        deal.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Deal updated.", "success")
        return redirect(url_for("deal_detail", deal_id=deal_id))
    return render_template("edit_deal.html", deal=deal)


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


# ── Targets ───────────────────────────────────────────────────────────────────

@app.route("/targets", methods=["GET", "POST"])
def targets():
    if request.method == "POST":
        period_type = request.form["period_type"]
        amount = float(request.form["amount"])
        period_start = date.fromisoformat(request.form["period_start"])
        target = Target(period_type=period_type, amount=amount, period_start=period_start)
        db.session.add(target)
        db.session.commit()
        flash("Target saved.", "success")
        return redirect(url_for("targets"))

    all_targets = Target.query.order_by(Target.period_start.desc()).all()
    return render_template("targets.html", targets=all_targets, today=date.today())


@app.route("/targets/<int:target_id>/delete", methods=["POST"])
def delete_target(target_id):
    target = Target.query.get_or_404(target_id)
    db.session.delete(target)
    db.session.commit()
    flash("Target deleted.", "info")
    return redirect(url_for("targets"))


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


if __name__ == "__main__":
    app.run(debug=True, port=5050, use_reloader=False)
