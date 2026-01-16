import os
import logging
import math
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv

from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler

# -------------------------
# 1. Configuration & Setup
# -------------------------
load_dotenv()

# Load Env Variables
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
ROOT_ADMIN_ID = os.getenv("ROOT_ADMIN_ID")  # Your User ID (Fail-safe admin)
CRON_SECRET = os.getenv("CRON_SECRET")      # Password for GitHub Actions
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///reminder_app.db")

# Initialize Flask
flask_app = Flask(__name__)

# Database Configuration
# pool_pre_ping prevents "SSL SYSCALL error: EOF detected" on Render/Supabase
flask_app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
flask_app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

db = SQLAlchemy(flask_app)

# Initialize Bolt
bolt_app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
handler = SlackRequestHandler(bolt_app)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------
# 2. Database Models
# -------------------------
class EventType(db.Model):
    """Dynamic list of event categories (SAT, AP, Soccer, etc.)"""
    name = db.Column(db.String(50), primary_key=True)

class AppAdmin(db.Model):
    __tablename__ = 'app_admin'
    """List of additional admin user IDs"""
    user_slack_id = db.Column(db.String(50), primary_key=True)

class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    event_type = db.Column(db.String(50), nullable=False) 
    event_date = db.Column(db.Date, nullable=False)
    registration_deadline = db.Column(db.Date, nullable=False)

class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_slack_id = db.Column(db.String(50), nullable=False)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    __table_args__ = (db.UniqueConstraint('user_slack_id', 'event_id', name='_user_event_uc'),)

# Initialize DB and Seed Data
with flask_app.app_context():
    db.create_all()
    # Seed default types if empty
    if not EventType.query.first():
        defaults = ["SAT", "ACT", "AP", "Extracurricular"]
        for d in defaults:
            db.session.add(EventType(name=d))
        db.session.commit()

# -------------------------
# 3. Helper Functions (Logic & UI)
# -------------------------
def is_user_admin(user_id):
    """Checks env var AND database for admin status."""
    if user_id == ROOT_ADMIN_ID:
        return True
    return db.session.query(AppAdmin).filter_by(user_slack_id=user_id).first() is not None

def get_sorted_events(user_id, category=None):
    """Fetches events, sorting by: 1. Subscribed (True first) 2. Date (Soonest first)"""
    today = datetime.now().date()
    query = Event.query
    if category:
        query = query.filter_by(event_type=category).filter(Event.event_date >= today)
    
    events = query.order_by(Event.event_date).all()
    subs = {s.event_id for s in Subscription.query.filter_by(user_slack_id=user_id).all()}

    # Python sort: False sorts before True, so we negate logic or use tuple
    # (Not Subscribed?, Date) -> True comes after False. 
    # So Subscribed (False) comes first.
    events.sort(key=lambda x: (x.id not in subs, x.event_date))
    return events, subs

def build_event_block(event, is_subscribed, is_admin=False):
    """Creates a single event row block with Admin Overflow or User Button."""
    date_str = event.event_date.strftime('%Y-%m-%d')
    deadline_str = event.registration_deadline.strftime('%Y-%m-%d')
    
    # Common Text
    text_section = {
        "type": "mrkdwn", 
        "text": f"*{event.title}*\n📅 {date_str} | ⏰ 데드라인: {deadline_str}"
    }

    # --- ADMIN VIEW (Overflow Menu) ---
    if is_admin:
        sub_text = "구독 취소" if is_subscribed else "알림 구독"
        sub_action = "unsub" if is_subscribed else "sub"
        
        accessory = {
            "type": "overflow",
            "action_id": "event_actions", # Triggers handle_event_overflow
            "options": [
                {
                    "text": {"type": "plain_text", "text": sub_text},
                    "value": f"{sub_action}|{event.id}" 
                },
                {
                    "text": {"type": "plain_text", "text": "Edit"},
                    "value": f"edit|{event.id}"
                },
                {
                    "text": {"type": "plain_text", "text": "Delete"},
                    "value": f"delete|{event.id}"
                }
            ]
        }

    # --- USER VIEW (Big Button) ---
    else:
        btn_text = "구독 취소" if is_subscribed else "알림 구독"
        btn_style = "danger" if is_subscribed else "primary"
        
        accessory = {
            "type": "button",
            "text": {"type": "plain_text", "text": btn_text},
            "value": f"{event.id}|{'unsub' if is_subscribed else 'sub'}",
            "action_id": "toggle_subscription", # Triggers handle_toggle
            "style": btn_style
        }

    return {
        "type": "section",
        "text": text_section,
        "accessory": accessory
    }

def get_dashboard_view(user_id):
    """Constructs the Home Tab Dashboard."""
    is_admin = is_user_admin(user_id)
    event_types = [et.name for et in EventType.query.all()]
    
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "📅 이벤트 확인툴"}},
        {"type": "divider"}
    ]
    
    # Admin Controls
    if is_admin:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "⚙️ *Admin Controls*"}})
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "+ Event"}, "action_id": "open_add_event_modal", "style": "primary"},
                {"type": "button", "text": {"type": "plain_text", "text": "+ Category"}, "action_id": "open_add_type_modal"},
                {"type": "button", "text": {"type": "plain_text", "text": "Manage Admins"}, "action_id": "open_manage_admins_modal"}
            ]
        })
        blocks.append({"type": "divider"})

    # Content Calculation (Max 100 blocks)
    remaining_blocks = 100 - len(blocks)
    num_cats = len(event_types)
    if num_cats == 0:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "No categories defined."}})
        return blocks

    items_per_cat = min(math.floor((remaining_blocks - (num_cats * 2)) / num_cats), 10)
    items_per_cat = max(items_per_cat, 0) # Safety

    for cat in event_types:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"📂 *{cat}*"}})
        
        events, subs = get_sorted_events(user_id, category=cat)
        display_events = events[:items_per_cat]
        
        if not display_events:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "이벤트 없음"}]})
        else:
            for event in display_events:
                blocks.append(build_event_block(event, event.id in subs, is_admin))
        
        # "View All" Button
        if len(events) > len(display_events):
            blocks.append({
                "type": "actions",
                "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"모든 {cat} 보기 ({len(events)})"},
                    "value": cat,
                    "action_id": "nav_view_category" 
                }]
            })
    
    return blocks

def get_category_view(user_id, category, page=0):
    """Detailed view of a single category with pagination."""
    is_admin = is_user_admin(user_id)
    ITEMS_PER_PAGE = 20
    events, subs = get_sorted_events(user_id, category=category)
    
    total_pages = math.ceil(len(events) / ITEMS_PER_PAGE)
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    current_slice = events[start:end]
    
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📂 {category} Events"}},
        {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "« 홈페이지로"}, "action_id": "nav_home"}]},
        {"type": "divider"}
    ]
    
    for event in current_slice:
        blocks.append(build_event_block(event, event.id in subs, is_admin))
        blocks.append({"type": "divider"})
    
    # Pagination
    pagination_elements = []
    if page > 0:
        pagination_elements.append({
            "type": "button", "text": {"type": "plain_text", "text": "뒤로"}, 
            "value": f"{category}|{page-1}", "action_id": "nav_prev_page"
        })
    if page < total_pages - 1:
        pagination_elements.append({
            "type": "button", "text": {"type": "plain_text", "text": "다음"}, 
            "value": f"{category}|{page+1}", "action_id": "nav_next_page"
        })
        
    if pagination_elements:
        blocks.append({"type": "actions", "elements": pagination_elements})
        
    return blocks

def open_edit_event_modal(client, trigger_id, event_id):
    """Opens a modal pre-filled with existing event data."""
    with flask_app.app_context():
        event = Event.query.get(event_id)
        if not event: return

        types = EventType.query.all()
        options = [{"text": {"type": "plain_text", "text": t.name}, "value": t.name} for t in types]
        initial_option = next((opt for opt in options if opt["value"] == event.event_type), None)

        client.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": "submit_edit_event",
                "private_metadata": str(event_id), # Store ID here
                "title": {"type": "plain_text", "text": "Edit Event"},
                "submit": {"type": "plain_text", "text": "Save Changes"},
                "blocks": [
                    {
                        "type": "input", "block_id": "title", "label": {"type": "plain_text", "text": "Title"},
                        "element": {"type": "plain_text_input", "action_id": "i", "initial_value": event.title}
                    },
                    {
                        "type": "input", "block_id": "type", "label": {"type": "plain_text", "text": "Type"},
                        "element": {"type": "static_select", "action_id": "i", "options": options, "initial_option": initial_option}
                    },
                    {
                        "type": "input", "block_id": "date", "label": {"type": "plain_text", "text": "Event Date"},
                        "element": {"type": "datepicker", "action_id": "i", "initial_date": event.event_date.strftime("%Y-%m-%d")}
                    },
                    {
                        "type": "input", "block_id": "deadline", "label": {"type": "plain_text", "text": "Reg. Deadline"},
                        "element": {"type": "datepicker", "action_id": "i", "initial_date": event.registration_deadline.strftime("%Y-%m-%d")}
                    }
                ]
            }
        )

# -------------------------
# 4. Bolt Handlers
# -------------------------

# --- Navigation & Home ---
@bolt_app.event("app_home_opened")
def update_home_tab(client, event, logger):
    with flask_app.app_context():
        blocks = get_dashboard_view(event["user"])
        client.views_publish(user_id=event["user"], view={"type": "home", "blocks": blocks})

@bolt_app.action("nav_home")
def go_home(ack, body, client):
    ack()
    with flask_app.app_context():
        blocks = get_dashboard_view(body["user"]["id"])
        client.views_publish(user_id=body["user"]["id"], view={"type": "home", "blocks": blocks})

@bolt_app.action("nav_view_category")
def go_category(ack, body, client):
    ack()
    category = body["actions"][0]["value"]
    with flask_app.app_context():
        blocks = get_category_view(body["user"]["id"], category, page=0)
        client.views_publish(user_id=body["user"]["id"], view={"type": "home", "blocks": blocks})

@bolt_app.action("nav_prev_page")
def prev_page(ack, body, client):
    ack()
    cat, page = body["actions"][0]["value"].split("|")
    with flask_app.app_context():
        blocks = get_category_view(body["user"]["id"], cat, int(page))
        client.views_publish(user_id=body["user"]["id"], view={"type": "home", "blocks": blocks})

@bolt_app.action("nav_next_page")
def next_page(ack, body, client):
    ack()
    cat, page = body["actions"][0]["value"].split("|")
    with flask_app.app_context():
        blocks = get_category_view(body["user"]["id"], cat, int(page))
        client.views_publish(user_id=body["user"]["id"], view={"type": "home", "blocks": blocks})

# --- Admin Modals (Open) ---
@bolt_app.action("open_add_event_modal")
def open_event_modal(ack, body, client):
    ack()
    with flask_app.app_context():
        types = EventType.query.all()
        options = [{"text": {"type": "plain_text", "text": t.name}, "value": t.name} for t in types]
        
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "submit_new_event", "title": {"type": "plain_text", "text": "Create Event"},
            "submit": {"type": "plain_text", "text": "Create"},
            "blocks": [
                {"type": "input", "block_id": "title", "label": {"type": "plain_text", "text": "Title"}, "element": {"type": "plain_text_input", "action_id": "i"}},
                {"type": "input", "block_id": "type", "label": {"type": "plain_text", "text": "Type"}, "element": {"type": "static_select", "action_id": "i", "options": options}},
                {"type": "input", "block_id": "date", "label": {"type": "plain_text", "text": "Event Date"}, "element": {"type": "datepicker", "action_id": "i"}},
                {"type": "input", "block_id": "deadline", "label": {"type": "plain_text", "text": "Reg. Deadline"}, "element": {"type": "datepicker", "action_id": "i"}}
            ]
        }
    )

@bolt_app.action("open_add_type_modal")
def open_type_modal(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "submit_new_type", "title": {"type": "plain_text", "text": "Add Category"},
            "submit": {"type": "plain_text", "text": "Add"},
            "blocks": [{"type": "input", "block_id": "name", "label": {"type": "plain_text", "text": "Category Name"}, "element": {"type": "plain_text_input", "action_id": "i"}}]
        }
    )

@bolt_app.action("open_manage_admins_modal")
def open_admin_modal(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "submit_new_admin", "title": {"type": "plain_text", "text": "Add Admin"},
            "submit": {"type": "plain_text", "text": "Add User"},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Select a user to grant Admin privileges."}},
                {"type": "input", "block_id": "user", "label": {"type": "plain_text", "text": "Select User"}, "element": {"type": "users_select", "action_id": "i"}}
            ]
        }
    )

# --- Submissions (Create/Edit) ---
@bolt_app.view("submit_new_event")
def handle_event_sub(ack, body, view, client):
    ack()
    vals = view["state"]["values"]
    with flask_app.app_context():
        new_event = Event(
            title=vals["title"]["i"]["value"],
            event_type=vals["type"]["i"]["selected_option"]["value"],
            event_date=datetime.strptime(vals["date"]["i"]["selected_date"], "%Y-%m-%d").date(),
            registration_deadline=datetime.strptime(vals["deadline"]["i"]["selected_date"], "%Y-%m-%d").date()
        )
        db.session.add(new_event)
        db.session.commit()
        client.views_publish(user_id=body["user"]["id"], view={"type": "home", "blocks": get_dashboard_view(body["user"]["id"])})

@bolt_app.view("submit_edit_event")
def handle_edit_submission(ack, body, view, client):
    ack()
    event_id = int(view["private_metadata"])
    vals = view["state"]["values"]
    
    with flask_app.app_context():
        event = Event.query.get(event_id)
        if event:
            event.title = vals["title"]["i"]["value"]
            event.event_type = vals["type"]["i"]["selected_option"]["value"]
            event.event_date = datetime.strptime(vals["date"]["i"]["selected_date"], "%Y-%m-%d").date()
            event.registration_deadline = datetime.strptime(vals["deadline"]["i"]["selected_date"], "%Y-%m-%d").date()
            db.session.commit()
        client.views_publish(user_id=body["user"]["id"], view={"type": "home", "blocks": get_dashboard_view(body["user"]["id"])})

@bolt_app.view("submit_new_type")
def handle_type_sub(ack, body, view, client):
    ack()
    name = view["state"]["values"]["name"]["i"]["value"]
    with flask_app.app_context():
        if not EventType.query.get(name):
            db.session.add(EventType(name=name))
            db.session.commit()
        client.views_publish(user_id=body["user"]["id"], view={"type": "home", "blocks": get_dashboard_view(body["user"]["id"])})

@bolt_app.view("submit_new_admin")
def handle_admin_sub(ack, body, view, client):
    ack()
    uid = view["state"]["values"]["user"]["i"]["selected_user"]
    with flask_app.app_context():
        if not AppAdmin.query.get(uid):
            db.session.add(AppAdmin(user_slack_id=uid))
            db.session.commit()
        client.views_publish(user_id=body["user"]["id"], view={"type": "home", "blocks": get_dashboard_view(body["user"]["id"])})

# --- Interactive Actions ---

# 1. Standard User Subscribe Toggle
@bolt_app.action("toggle_subscription")
def handle_toggle(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    event_id, action = body["actions"][0]["value"].split("|")
    event_id = int(event_id)
    
    with flask_app.app_context():
        if action == "sub":
            if not Subscription.query.filter_by(user_slack_id=user_id, event_id=event_id).first():
                db.session.add(Subscription(user_slack_id=user_id, event_id=event_id))
        else:
            Subscription.query.filter_by(user_slack_id=user_id, event_id=event_id).delete()
        db.session.commit()
        
        # Refresh View
        client.views_publish(user_id=user_id, view={"type": "home", "blocks": get_dashboard_view(user_id)})

# 2. Admin Overflow Logic (Edit / Delete / Subscribe)
@bolt_app.action("event_actions")
def handle_event_overflow(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    selected_option = body["actions"][0]["selected_option"]["value"]
    action, event_id_str = selected_option.split("|")
    event_id = int(event_id_str)
    
    if action == "edit":
        open_edit_event_modal(client, body["trigger_id"], event_id)
        
    elif action == "delete":
        with flask_app.app_context():
            Subscription.query.filter_by(event_id=event_id).delete()
            Event.query.filter_by(id=event_id).delete()
            db.session.commit()
            client.views_publish(user_id=user_id, view={"type": "home", "blocks": get_dashboard_view(user_id)})
            
    elif action in ["sub", "unsub"]:
        with flask_app.app_context():
            if action == "sub":
                if not Subscription.query.filter_by(user_slack_id=user_id, event_id=event_id).first():
                    db.session.add(Subscription(user_slack_id=user_id, event_id=event_id))
            else:
                Subscription.query.filter_by(user_slack_id=user_id, event_id=event_id).delete()
            db.session.commit()
            client.views_publish(user_id=user_id, view={"type": "home", "blocks": get_dashboard_view(user_id)})

# -------------------------
# 5. Flask Routes
# -------------------------
@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

@flask_app.route("/slack/actions", methods=["POST"])
def slack_actions():
    return handler.handle(request)

@flask_app.route("/keep-alive", methods=["GET"])
def keep_alive():
    return {"status": "alive"}, 200

# Secure Cron Trigger
@flask_app.route("/api/run-reminders", methods=["POST"])
def trigger_reminders():
    auth_header = request.headers.get("Authorization")
    cron_secret = os.environ.get("CRON_SECRET")
    
    if not auth_header or not cron_secret:
        return {"error": "Unauthorized"}, 401
    
    try:
        token_type, received_token = auth_header.split(maxsplit=1)
        if token_type.lower() != "bearer": raise ValueError
        if not secrets.compare_digest(received_token, cron_secret): raise ValueError
    except ValueError:
        return {"error": "Unauthorized"}, 401

    try:
        today = datetime.now().date()
        total_sent = 0

        def notify_subscribers(event_obj, message_text):
            count = 0
            subs = Subscription.query.filter_by(event_id=event_obj.id).all()
            for sub in subs:
                try:
                    bolt_app.client.chat_postMessage(channel=sub.user_slack_id, text=message_text)
                    count += 1
                except Exception as e:
                    logger.error(f"Failed to DM user {sub.user_slack_id}: {e}")
            return count

        for days_left in [0, 1, 2, 3]:
            target_date = today + timedelta(days=days_left)
            time_str = "오늘" if days_left == 0 else "내일" if days_left == 1 else f"{days_left}일 후"

            # 1. Registration Deadlines
            deadline_events = Event.query.filter_by(registration_deadline=target_date).all()
            for event in deadline_events:
                msg = f"⚠️ *{event.event_type}* *{event.title}* 가입 데드라인이 *{time_str}* 닫힙니다 ({event.registration_deadline})!"
                total_sent += notify_subscribers(event, msg)

            # 2. Event Dates
            test_day_events = Event.query.filter_by(event_date=target_date).all()
            for event in test_day_events:
                msg = f"📅 *이벤트 알림:* *{event.event_type}* *{event.title}*이 *{time_str}* 입니다 ({event.event_date})!"
                total_sent += notify_subscribers(event, msg)

        return {"status": "success", "reminders_sent": total_sent}, 200

    except Exception as e:
        logger.error(f"Cron failed: {e}")
        return {"error": str(e)}, 500

if __name__ == "__main__":
    flask_app.run(port=3000)