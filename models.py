from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Client(db.Model):
    __tablename__ = "clients"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    company = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    email = db.Column(db.String(120))
    source = db.Column(db.String(60))  # referral / cold / inbound / etc.
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    deals = db.relationship("Deal", backref="client", lazy=True, cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "company": self.company,
            "phone": self.phone,
            "email": self.email,
            "source": self.source,
        }


STAGES = ["new", "contacted", "pitched", "follow_up", "negotiation", "won", "lost"]


class Deal(db.Model):
    __tablename__ = "deals"
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    value = db.Column(db.Float, default=0.0)
    stage = db.Column(db.String(30), default="new")
    salesperson = db.Column(db.String(120))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    activities = db.relationship(
        "Activity", backref="deal", lazy=True, cascade="all, delete-orphan",
        order_by="Activity.scheduled_at.desc()"
    )

    @property
    def days_in_stage(self):
        return (datetime.utcnow() - self.updated_at).days

    @property
    def last_activity_at(self):
        if self.activities:
            return max(a.scheduled_at for a in self.activities if a.scheduled_at)
        return None

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "value": self.value,
            "stage": self.stage,
            "salesperson": self.salesperson,
            "client_name": self.client.name,
            "client_company": self.client.company,
            "days_in_stage": self.days_in_stage,
        }


ACTIVITY_TYPES = ["meeting", "call", "email", "follow_up", "demo", "other"]


class Activity(db.Model):
    __tablename__ = "activities"
    id = db.Column(db.Integer, primary_key=True)
    deal_id = db.Column(db.Integer, db.ForeignKey("deals.id"), nullable=False)
    type = db.Column(db.String(30), default="meeting")
    scheduled_at = db.Column(db.DateTime)
    location = db.Column(db.String(300))
    meeting_link = db.Column(db.String(500))
    notes = db.Column(db.Text)
    outcome = db.Column(db.Text)
    reminder_sent = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "deal_id": self.deal_id,
            "type": self.type,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "location": self.location,
            "meeting_link": self.meeting_link,
            "notes": self.notes,
            "outcome": self.outcome,
        }


class Target(db.Model):
    __tablename__ = "targets"
    id = db.Column(db.Integer, primary_key=True)
    period_type = db.Column(db.String(20), nullable=False)  # daily / weekly / monthly
    amount = db.Column(db.Float, nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ── Tasks ─────────────────────────────────────────────────────────────────────

TASK_STATUSES = ["todo", "in_progress", "done"]
TASK_PRIORITIES = ["low", "medium", "high", "urgent"]
TASK_LABELS = ["follow_up", "proposal", "admin", "call", "research", "other"]
RECUR_INTERVALS = ["daily", "weekly", "monthly"]


class Task(db.Model):
    __tablename__ = "tasks"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(20), default="todo")
    priority = db.Column(db.String(20), default="medium")
    due_date = db.Column(db.Date)
    assigned_to = db.Column(db.String(120))
    created_by = db.Column(db.String(120))
    deal_id = db.Column(db.Integer, db.ForeignKey("deals.id"), nullable=True)
    labels = db.Column(db.String(300), default="")   # comma-separated
    is_recurring = db.Column(db.Boolean, default=False)
    recur_interval = db.Column(db.String(20))        # daily / weekly / monthly
    due_reminder_sent = db.Column(db.Boolean, default=False)
    overdue_reminder_sent = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    deal = db.relationship("Deal", backref=db.backref("tasks", lazy=True))
    comments = db.relationship("TaskComment", backref="task", lazy=True,
                               cascade="all, delete-orphan",
                               order_by="TaskComment.created_at.asc()")
    subtasks = db.relationship("TaskSubtask", backref="task", lazy=True,
                               cascade="all, delete-orphan",
                               order_by="TaskSubtask.created_at.asc()")
    activity_log = db.relationship("TaskActivity", backref="task", lazy=True,
                                   cascade="all, delete-orphan",
                                   order_by="TaskActivity.created_at.desc()")

    @property
    def label_list(self):
        return [l.strip() for l in (self.labels or "").split(",") if l.strip()]

    @property
    def is_overdue(self):
        if self.due_date and self.status != "done":
            from datetime import date
            return self.due_date < date.today()
        return False

    @property
    def subtasks_done(self):
        return sum(1 for s in self.subtasks if s.done)


class TaskComment(db.Model):
    __tablename__ = "task_comments"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False)
    author = db.Column(db.String(120), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TaskSubtask(db.Model):
    __tablename__ = "task_subtasks"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    done = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TaskActivity(db.Model):
    __tablename__ = "task_activity"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False)
    actor = db.Column(db.String(120))
    action = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
