import os
import logging
import re
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

# At the top of your file, make sure you know your App ID
# You can hardcode it, or get it from env vars.
APP_ID = os.getenv("SLACK_APP_ID", "A0A6X1SAT1B") # Find this in "Basic Information"
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
# -----------p--------------
class EventTye(db.Model):
    """Dynamic list of event categories (SAT, AP, Soccer, etc.)"""
    name = db.Column(db.String(50), primary_key=True)

class AppAdmin(db.Model):
    __tablename__ = 'app_admin'
    """List of additional admin user IDs"""
    user_slack_id = db.Column(db.String(50), primary_key=True)

class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    event_type = db.Column(db.String(50), db.ForeignKey('event_type.name'), nullable=False)
    event_date = db.Column(db.Date, nullable=False)
    registration_deadline = db.Column(db.Date, nullable=False)

class EventType(db.Model):
    __tablename__ = 'event_type'
    name = db.Column(db.String(50), primary_key='True')

class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(db.String(50), nullable=False)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    status = db.Column(db.String(20), nullable=True)
    __table_args__ = (db.UniqueConstraint('channel_id', 'event_id', name='_user_event_uc'),)

class AppConfig(db.Model):
    """Stores global settings like the Consultant Channel ID"""
    __tablename__ = 'app_config'
    key = db.Column(db.String(50), primary_key=True) # e.g., "consultant_channel"
    value = db.Column(db.String(200), nullable=False) # e.g., "C12345678"

class TrackedStudent(db.Model):
    """
    Mapping: Which Consultant (Admin) is tracking which Student.
    One consultant can track many students.
    """
    id = db.Column(db.Integer, primary_key=True)
    consultant_id = db.Column(db.String(50), nullable=False) # The Admin
    channel_id = db.Column(db.String(50), nullable=False)    # The Student
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Prevent duplicate tracking entries
    __table_args__ = (db.UniqueConstraint('consultant_id', 'channel_id', name='_consultant_student_uc'),)

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

def get_sorted_events(channel_id, category=None):
    """Fetches events with subscription status via JOIN, sorted by subscription and date."""
    
    today = datetime.now().date()
    
    # LEFT JOIN to get subscription status for this user
    query = db.session.query(
        Event,
        Subscription
    ).outerjoin(
        Subscription,
        (Event.id == Subscription.event_id) & (Subscription.channel_id == channel_id)
    ).filter(Event.registration_deadline >= today)
    
    if category:
        query = query.filter(Event.event_type == category)
    
    # Sort: subscribed first, then by date
    query = query.order_by(
        (Subscription.id.is_(None)),  # False (subscribed) comes first
        Event.event_date
    )
    
    results = query.all()
    
    # Extract events and build subscription set
    events = [row[0] for row in results]
    subs = {row[0].id: row[1] for row in results if row[1]}
    
    return events, subs

def find_event_by_query(query_text):
    """
    Tries to find a single event based on ID (int) or Title (string).
    Returns (Event, ErrorMessage).
    """
    if not query_text:
        return None, "⚠️ 검색어를 입력해주세요. (예: `/check-pending SAT`)"
    
    # 1. Try search by ID
    if query_text.isdigit():
        event = Event.query.get(int(query_text))
        if event: return event, None
    
    # 2. Try search by Title (Partial Match)
    # ilike makes it case-insensitive
    events = Event.query.filter(Event.title.ilike(f"%{query_text}%")).all()
    
    if len(events) == 0:
        return None, f"⚠️ '{query_text}'에 해당하는 이벤트를 찾을 수 없습니다."
    elif len(events) > 1:
        # If multiple matches, ask for ID
        msg = "⚠️ 여러 이벤트가 검색되었습니다. 정확한 ID를 입력해주세요:\n"
        for e in events:
            msg += f"• [ID: {e.id}] {e.title} ({e.event_date})\n"
        return None, msg
        
    return events[0], None

def parse_user_id(text):
    """Extracts U12345 from text like '<@U12345|name>'"""
    match = re.search(r"<@(U[A-Z0-9]+)(\|.*?)?>", text)
    return match.group(1) if match else None

def parse_channel_id(text):
    """Extracts U12345 from text like '<#C12345|general>'"""
    match = re.search(r"<#(C[A-Z0-9]+)(\|.*?)?>", text)
    return match.group(1) if match else None


def build_event_block(event, subscription, is_admin=False):
    """
    Creates event blocks. 
    Returns a LIST of blocks to accommodate the status button.
    """
    date_str = event.event_date.strftime('%Y-%m-%d')
    deadline_str = event.registration_deadline.strftime('%Y-%m-%d')
    is_subscribed = subscription is not None
    status = subscription.status if is_subscribed else None
    # Common Text
    text_section = {
        "type": "mrkdwn", 
        "text": f"*{event.title}*\n📅 {date_str} | ⏰ 데드라인: {deadline_str}"
    }

    # --- ADMIN VIEW (Overflow Menu) ---
    if is_admin:
        
        accessory = {
            "type": "overflow",
            "action_id": "event_actions",
            "options": [
                {"text": {"type": "plain_text", "text": "✏️ Edit"}, "value": f"edit|{event.id}"},
                {"text": {"type": "plain_text", "text": "🗑️ Delete"}, "value": f"delete|{event.id}"}
            ]
        }

    # 1. Create the Main Block
    main_block = {
        "type": "section",
        "text": text_section
    }
    if is_admin:
        main_block['accessory'] = accessory # type: ignore
    
    blocks = [main_block]

    # 2. Add Status Block (Only if subscribed)
    if is_subscribed:
        if status == "Pending":
            # Show "I Registered" Button
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "등록 확인"},
                        "value": str(event.id),
                        "action_id": "confirm_registration",
                        "style": "primary"
                    }
                ]
            })
        elif status == "Registered":
            # Show "Registered" Text
            blocks.append({
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "등록 완료"}
                ]
            })

    return blocks

def get_dashboard_view(user_id):
    """Constructs the Home Tab Dashboard."""
    is_admin = is_user_admin(user_id)
    event_types = [et.name for et in EventType.query.all()]
    
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "📅 시험/EC 날짜 확인"}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "👋 이 앱은 시험(SAT, AP 등) 및 교내외 활동 일정을 관리해줍니다.\n\n"
                    "📌 *사용 방법:*\n"
                    "• 관심 있는 이벤트의 *'알림 구독'* 버튼을 눌러주세요.\n"
                    "• 구독하시면 *마감일 및 행사 당일 3일 전부터* 매일 아침 DM으로 알림을 보내드립니다.\n"
                    "• 놓치기 쉬운 등록 마감일(Deadline)과 시험 당일을 잊지 마세요!"
                )
            }
        },
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
                {"type": "button", "text": {"type": "plain_text", "text": "Subscribe Channel"}, "action_id": "open_admin_sub_modal"},
                {"type": "button", "text": {"type": "plain_text", "text": "Register Channel"}, "action_id": "open_admin_register_modal"},
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
        
        events, subs = get_sorted_events('', category=cat)
        display_events = events[:items_per_cat]
        
        if not display_events:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "이벤트 없음"}]})
        else:
            for event in display_events:
                blocks.extend(build_event_block(event, subs[event.id] if event.id in subs.keys() else None, is_admin))
        
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
    events, subs = get_sorted_events('', category=category)
    
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
        blocks.extend(build_event_block(event, subs[event.id] if event.id in subs.keys() else None, is_admin))
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

# -------------------------
# 5. Bolt Handlers (Interactivity)
# -------------------------

@bolt_app.command("/list-events")
def handle_list_events(ack, respond):
    ack()
    with flask_app.app_context():
        events = Event.query.filter(Event.registration_deadline >= datetime.now().date()).order_by(Event.event_date).all()
        if not events:
            respond("📅 예정된 이벤트가 없습니다.")
            return
        
        response = "*📅 다가오는 이벤트 목록:*\n"
        for e in events:
            response += f"• [ID: {e.id}] *{e.title}* ({e.event_type}) - {e.event_date} 데드라인: {e.registration_deadline}\n"
        respond(response)

@bolt_app.command("/list-subs")
def handle_list_subs(ack, respond, command):
    ack()
    user_id = command["user_id"]
    text = command["text"].strip()
    
    target_id = parse_channel_id(text) if text else None
    
    # Check permission
    with flask_app.app_context():
        if not is_user_admin(user_id):
            respond("🚫 관리자만 볼 수 있습니다.")
            return
        
        # Use JOIN to fetch subscriptions and events in one query
        subs = db.session.query(Subscription, Event).join(Event).filter(Subscription.channel_id == target_id).all()
        
        if not subs:
            respond(f"<#{target_id}> 님은 구독 중인 이벤트가 없습니다.")
            return
        
        response = f"*📋 <#{target_id}> 님의 구독 리스트:*\n"
        for sub, event in subs:
            status = "미등록" if sub.status == 'Pending' else '등록완료'
            if event and event.registration_deadline >= datetime.now().date():
                response += f"• {event.title} - {event.event_date} 데드라인: {event.registration_deadline} *{status}*\n"
        
        respond(response)

@bolt_app.command("/check-pending")
def handle_check_pending(ack, respond, command):
    ack()
    user_id = command["user_id"]
    query_text = command["text"].strip()

    with flask_app.app_context():
        if not is_user_admin(user_id):
            respond("🚫 관리자 권한이 없습니다.")
            return

        # Find the event
        event, err = find_event_by_query(query_text)
        if err:
            respond(err)
            return

        # Find Pending Subscriptions
        pending_subs = Subscription.query.filter_by(event_id=event.id, status="Pending").all()
        registered_count = Subscription.query.filter_by(event_id=event.id, status="Registered").count()
        
        if not pending_subs:
            respond(f"🎉 *{event.title}*: 모든 학생이 등록을 완료했습니다! ({registered_count}명 완료)")
            return

        # Build List
        msg = f"🚨 *{event.title}* 미등록 학생 리스트 ({len(pending_subs)}명):\n"
        for sub in pending_subs:
            msg += f"• <#{sub.channel_id}>\n"
        
        msg += f"\n✅ 등록 완료: {registered_count}명"
        msg += f"\n👉 `/nudge-pending {event.id}` 를 입력하여 알림을 보낼 수 있습니다."
        
        respond(msg)

@bolt_app.command("/nudge-pending")
def handle_nudge_pending(ack, respond, client, command):
    ack()
    user_id = command["user_id"]
    query_text = command["text"].strip()

    with flask_app.app_context():
        if not is_user_admin(user_id):
            respond("🚫 관리자 권한이 없습니다.")
            return

        # Find the event
        event, err = find_event_by_query(query_text)
        if err:
            respond(err)
            return

        # Find Pending Subscriptions
        pending_subs = Subscription.query.filter_by(event_id=event.id, status="Pending").all()
        
        if not pending_subs:
            respond(f"✅ *{event.title}*: 알림을 보낼 대상이 없습니다 (모두 등록 완료).")
            return

        count = 0
        for sub in pending_subs:
            try:
                # Send the Nudge DM
                client.chat_postMessage(
                    channel=sub.channel_id,
                    text=f"👋 안녕하세요! 담당 컨설턴트가 *{event.title}* 등록 여부를 확인 중입니다.",
                    blocks=[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": f"👋 안녕하세요! \n*{event.title}* 등록을 아직 완료하지 않으신 것 같습니다.\n확인 부탁드립니다!"}
                        },
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "✅ 등록 완료"},
                                    "style": "primary",
                                    "value": str(event.id),
                                    "action_id": "confirm_registration"
                                }
                            ]
                        }
                    ]
                )
                count += 1
            except Exception as e:
                logger.error(f"Failed to nudge {sub.channel_id}: {e}")

        respond(f"📨 *{event.title}*: 미등록 학생 *{count}명*에게 알림을 발송했습니다.")

#sends messages to all students subscribed to an event
@bolt_app.command("/send-event-message")
def open_send_message_modal(ack, body, client):
    ack()
    user_id = body["user_id"]
    channel_id = body['channel_id']
    
    with flask_app.app_context():
        if not is_user_admin(user_id):
            client.chat_postEphemeral(channel=channel_id, user=user_id, text="🚫 관리자 권한이 없습니다.")
            return
        
        # Fetch upcoming events
        events = Event.query.filter(Event.registration_deadline >= datetime.now().date())\
                            .order_by(Event.event_date)\
                            .limit(100).all()
        
        event_options = []
        for e in events:
            date_str = e.event_date.strftime('%Y-%m-%d')
            safe_title = e.title
            safe_cat = e.event_type
            occupied_len = len(safe_cat) + len(date_str) + 2
            if len(safe_title) > 75 - (occupied_len + 6):
                safe_title = safe_title[:occupied_len - 6] + "..."
            
            label_text = f"{safe_cat} {safe_title} ({date_str})"
            event_options.append({
                "text": {"type": "plain_text", "text": label_text},
                "value": str(e.id)
            })
    
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "submit_send_event_message",
            "private_metadata": channel_id,
            "title": {"type": "plain_text", "text": "Send Event Message"},
            "submit": {"type": "plain_text", "text": "Send"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "이벤트를 선택하고 메시지를 작성하세요."}
                },
                {
                    "type": "input",
                    "block_id": "event_select",
                    "label": {"type": "plain_text", "text": "이벤트 선택 (검색)"},
                    "element": {
                        "type": "external_select",
                        "action_id": "event_search",
                        "placeholder": {"type": "plain_text", "text": "이벤트 이름 검색..."},
                        "min_query_length": 1
                    }
                },
                {
                    "type": "input",
                    "block_id": "message",
                    "label": {"type": "plain_text", "text": "메시지"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "msg_text",
                        "multiline": True,
                        "placeholder": {"type": "plain_text", "text": "보낼 메시지를 입력하세요"}
                    }
                }
            ]
        }
    )

@bolt_app.command("/track")
def handle_track_command(ack, respond, command):
    ack()
    
    admin_id = command["user_id"]
    text = command["text"].strip()
    parts = text.split()
    
    # 1. Permission Check
    with flask_app.app_context():
        if not is_user_admin(admin_id):
            respond("🚫 관리자 권한이 없습니다.")
            return

        # 2. Logic Router
        if not parts:
            respond("⚠️ 사용법:\n`/track add #channel`\n`/track remove #channel`\n`/track list`\n`/track #channel` (상세 조회)")
            return

        action = parts[0].lower()

        # --- ACTION: ADD ---
        if action == "add":
            if len(parts) < 2:
                respond("⚠️ 추가할 유저를 선택해주세요. 예: `/track add #John`")
                return
            
            target_id = parse_channel_id(parts[1])
            if not target_id:
                respond("⚠️ 유효한 유저 태그가 아닙니다.")
                return

            if not TrackedStudent.query.filter_by(consultant_id=admin_id, channel_id=target_id).first():
                db.session.add(TrackedStudent(consultant_id=admin_id, channel_id=target_id))
                db.session.commit()
                respond(f"✅ 이제 <#{target_id}> 학생을 추적 관리합니다.")
            else:
                respond(f"ℹ️ <#{target_id}> 학생은 이미 추적 목록에 있습니다.")

        # --- ACTION: REMOVE ---
        elif action == "remove":
            if len(parts) < 2:
                respond("⚠️ 삭제할 유저를 선택해주세요.")
                return
            
            target_id = parse_channel_id(parts[1])
            if not target_id: return

            entry = TrackedStudent.query.filter_by(consultant_id=admin_id, channel_id=target_id).first()
            if entry:
                db.session.delete(entry)
                db.session.commit()
                respond(f"🗑️ <#{target_id}> 학생을 추적 목록에서 제거했습니다.")
            else:
                respond(f"⚠️ 목록에 없는 학생입니다.")

        # --- ACTION: LIST ---
        elif action == "list":
            tracked = TrackedStudent.query.filter_by(consultant_id=admin_id).all()
            if not tracked:
                respond("📭 현재 추적 중인 학생이 없습니다.")
                return
            
            msg = "*📋 내 담당 학생 리스트 (My Roster):*\n"
            for t in tracked:
                msg += f"• <#{t.student_id}>\n"
            respond(msg)

        # --- ACTION: VIEW DETAILS (Default) ---
        # If input is just "#User" or "show #User"
        else:
            # Handle "/track #User" case
            target_id = parse_channel_id(action) 
            # Handle "/track show #User" case (optional safety)
            if not target_id and len(parts) > 1:
                target_id = parse_channel_id(parts[1])

            if not target_id:
                respond("⚠️ 알 수 없는 명령어입니다.")
                return

            # Fetch Student Details
            subs = db.session.query(Subscription, Event).join(Event).filter(Subscription.channel_id == target_id).order_by(Event.event_date).all()
            
            if not subs:
                respond(f"📂 <#{target_id}> 학생은 현재 구독 중인 이벤트가 없습니다.")
                return

            # Build Report
            response_text = f"*👤 학생 분석 보고서: <#{target_id}>*\n\n"
            
            today = datetime.now().date()
            
            upcoming_txt = ""
            history_txt = ""
            
            for sub, event in subs:
                status_icon = "✅" if sub.status == "Registered" else "⏳"
                status_text = "등록 완료" if sub.status == "Registered" else "미등록 (Pending)"
                
                line = f"• {status_icon} *{event.title}* | 📅 {event.event_date} | 상태: *{status_text}*\n"
                
                if event.event_date >= today:
                    # Highlight urgent deadlines
                    if sub.status == "Pending" and event.registration_deadline <= (today + timedelta(days=3)):
                        line += f"    🚨 *경고: 마감 임박 ({event.registration_deadline})*\n"
                    upcoming_txt += line
                else:
                    history_txt += line

            if upcoming_txt:
                response_text += "*📅 예정된 일정 (Upcoming):*\n" + upcoming_txt + "\n"
            
            if history_txt:
                 # Optional: Only show history if requested, or keep it short
                response_text += "*📜 지난 일정 (History):*\n" + history_txt

            respond(response_text)

@bolt_app.command("/admin-sub")
def open_admin_sub_modal(ack, body, client, command):
    ack()
    user_id = command["user_id"]
    channel_id = body['channel_id']
    # 1. Fetch upcoming events for the dropdown
    with flask_app.app_context():
        if not is_user_admin(user_id):
            client.chat_postEphemeral(channel=user_id, user=user_id, text="🚫 관리자 권한이 없습니다.")
            return

    # 2. Open the Modal
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "submit_admin_sub",
            "private_metadata": channel_id,
            "title": {"type": "plain_text", "text": "구독"},
            "submit": {"type": "plain_text", "text": "유저 구독하기"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "유저를 선택하고 이벤트를 지정하세요."}
                },
                # Input 1: User Picker
                {
                    "type": "input",
                    "block_id": "target_user",
                    "label": {"type": "plain_text", "text": "유저 선택"},
                    "element": {
                        "type": "conversations_select",
                        "action_id": "conversations_select",
                        "placeholder": {"type": "plain_text", "text": "유저를 선택하세요"},
                        "filter": {
                            "include": [
                                "public",
                                "private"
                            ],
                            "exclude_bot_users": True
                        }
                    }
                },
                # Input 2: Action Type (Single Event or Category?)
                {
                    "type": "input",
                    "block_id": "sub_type",
                    "label": {"type": "plain_text", "text": "모드"},
                    "element": {
                        "type": "static_select",
                        "action_id": "mode_select",
                        "initial_option": {"text": {"type": "plain_text", "text": "1개 이벤트"}, "value": "item"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "1개 이벤트"}, "value": "item"},
                            {"text": {"type": "plain_text", "text": "카테고리"}, "value": "cat"},
                            {"text": {"type": "plain_text", "text": "모든 이벤트"}, "value": "all"}
                        ]
                    }
                },
                # Input 3: Event Picker (Searchable Dropdown)
                # Note: This is optional because "All" doesn't need it.
                {
                    "type": "input",
                    "block_id": "event_select",
                    "optional": True, 
                    "label": {"type": "plain_text", "text": "이벤트 선택 (이름 검색)"},
                    "element": {
                        "type": "external_select",
                        "action_id": "event_id",
                        "placeholder": {"type": "plain_text", "text": "검색어 입력"},
                        "min_query_length": 1
                    }
                },
                # Input 4: Category Picker (Only needed if Mode is Category)
                {
                    "type": "input",
                    "block_id": "cat_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "카테고리 선택 (카테고리 모드를 선택했을경우)"},
                    "element": {
                        "type": "static_select",
                        "action_id": "cat_name",
                        "options": get_category_options() 
                    }
                }
            ]
        }
    )

@bolt_app.command("/admin-register")
def open_admin_register_modal(ack, body, client, command):
    ack()
    user_id = command["user_id"]
    channel_id = body['channel_id']
    # 1. Fetch upcoming events for the dropdown
    with flask_app.app_context():
        if not is_user_admin(user_id):
            client.chat_postEphemeral(channel=user_id, user=user_id, text="🚫 관리자 권한이 없습니다.")
            return

    # 2. Open the Modal
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "submit_admin_register",
            "private_metadata": channel_id,
            "title": {"type": "plain_text", "text": "등록"},
            "submit": {"type": "plain_text", "text": "유저 등록하기"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "유저를 선택하고 이벤트를 지정하세요."}
                },
                # Input 1: User Picker
                {
                    "type": "input",
                    "block_id": "target_user",
                    "label": {"type": "plain_text", "text": "유저 선택"},
                    "element": {
                        "type": "conversations_select",
                        "action_id": "conversations_select",
                        "placeholder": {"type": "plain_text", "text": "유저를 선택하세요"},
                        "filter": {
                            "include": [
                                "public",
                                "private"
                            ],
                            "exclude_bot_users": True
                        }
                    }
                },
                # Input 2: Action Type (Single Event or Category?)
                {
                    "type": "input",
                    "block_id": "sub_type",
                    "label": {"type": "plain_text", "text": "모드"},
                    "element": {
                        "type": "static_select",
                        "action_id": "mode_select",
                        "initial_option": {"text": {"type": "plain_text", "text": "1개 이벤트"}, "value": "item"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "1개 이벤트"}, "value": "item"},
                            {"text": {"type": "plain_text", "text": "카테고리"}, "value": "cat"},
                            {"text": {"type": "plain_text", "text": "모든 이벤트"}, "value": "all"}
                        ]
                    }
                },
                # Input 3: Event Picker (Searchable Dropdown)
                # Note: This is optional because "All" doesn't need it.
                {
                    "type": "input",
                    "block_id": "event_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "이벤트 선택"},
                    "element": {
                        "type": "external_select",
                        "action_id": "event_subscribed",
                        "placeholder": {"type": "plain_text", "text": "이벤트 선택"},
                        "min_query_length": 0  # <--- Change this to 0 to auto-load on click
                    }
                },
                # Input 4: Category Picker (Only needed if Mode is Category)
                {
                    "type": "input",
                    "block_id": "cat_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "카테고리 선택 (카테고리 모드를 선택했을경우)"},
                    "element": {
                        "type": "static_select",
                        "action_id": "cat_name",
                        "options": get_category_options() 
                    }
                }
            ]
        }
    )

# Helper for category options
def get_category_options():
    with flask_app.app_context():
        cats = EventType.query.all()
        return [{"text": {"type": "plain_text", "text": c.name}, "value": c.name} for c in cats]

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

@bolt_app.action("open_admin_register_modal")
def open_admin_register_modal_(ack, body, client):
    ack()
    user_id = body['user']['id']
    # 2. Open the Modal
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "submit_admin_register",
            "private_metadata": user_id,
            "title": {"type": "plain_text", "text": "등록"},
            "submit": {"type": "plain_text", "text": "유저 등록하기"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "유저를 선택하고 이벤트를 지정하세요."}
                },
                # Input 1: User Picker
                {
                    "type": "input",
                    "block_id": "target_user",
                    "label": {"type": "plain_text", "text": "유저 선택"},
                    "element": {
                        "type": "conversations_select",
                        "action_id": "conversations_select",
                        "placeholder": {"type": "plain_text", "text": "유저를 선택하세요"},
                        "filter": {
                            "include": [
                                "public",
                                "private"
                            ],
                            "exclude_bot_users": True
                        }
                    }
                },
                # Input 2: Action Type (Single Event or Category?)
                {
                    "type": "input",
                    "block_id": "sub_type",
                    "label": {"type": "plain_text", "text": "모드"},
                    "element": {
                        "type": "static_select",
                        "action_id": "mode_select",
                        "initial_option": {"text": {"type": "plain_text", "text": "1개 이벤트"}, "value": "item"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "1개 이벤트"}, "value": "item"},
                            {"text": {"type": "plain_text", "text": "카테고리"}, "value": "cat"},
                            {"text": {"type": "plain_text", "text": "모든 이벤트"}, "value": "all"}
                        ]
                    }
                },
                # Input 3: Event Picker (Searchable Dropdown)
                # Note: This is optional because "All" doesn't need it.
                {
                    "type": "input",
                    "block_id": "event_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "이벤트 선택"},
                    "element": {
                        "type": "external_select",
                        "action_id": "event_subscribed",
                        "placeholder": {"type": "plain_text", "text": "이벤트 선택"},
                        "min_query_length": 0  # <--- Change this to 0 to auto-load on click
                    }
                },
                # Input 4: Category Picker (Only needed if Mode is Category)
                {
                    "type": "input",
                    "block_id": "cat_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "카테고리 선택 (카테고리 모드를 선택했을경우)"},
                    "element": {
                        "type": "static_select",
                        "action_id": "cat_name",
                        "options": get_category_options() 
                    }
                }
            ]
        }
    )

@bolt_app.action("open_admin_sub_modal")
def open_admin_sub_modal_(ack, body, client):
    ack()
    user_id = body["user"]["id"]

    # 2. Open the Modal
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "submit_admin_sub",
            "private_metadata": user_id,
            "title": {"type": "plain_text", "text": "구독"},
            "submit": {"type": "plain_text", "text": "유저 구독하기"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "유저를 선택하고 이벤트를 지정하세요."}
                },
                # Input 1: User Picker
                {
                    "type": "input",
                    "block_id": "target_user",
                    "label": {"type": "plain_text", "text": "유저 선택"},
                    "element": {
                        "type": "conversations_select",
                        "action_id": "conversations_select",
                        "placeholder": {"type": "plain_text", "text": "유저를 선택하세요"},
                        "filter": {
                            "include": [
                                "public",
                                "private"
                            ],
                            "exclude_bot_users": True
                        }
                    }
                },
                # Input 2: Action Type (Single Event or Category?)
                {
                    "type": "input",
                    "block_id": "sub_type",
                    "label": {"type": "plain_text", "text": "모드"},
                    "element": {
                        "type": "static_select",
                        "action_id": "mode_select",
                        "initial_option": {"text": {"type": "plain_text", "text": "1개 이벤트"}, "value": "item"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "1개 이벤트"}, "value": "item"},
                            {"text": {"type": "plain_text", "text": "카테고리"}, "value": "cat"},
                            {"text": {"type": "plain_text", "text": "모든 이벤트"}, "value": "all"}
                        ]
                    }
                },
                # Input 3: Event Picker (Searchable Dropdown)
                # Note: This is optional because "All" doesn't need it.
                {
                    "type": "input",
                    "block_id": "event_select",
                    "optional": True, 
                    "label": {"type": "plain_text", "text": "이벤트 선택 (이름 검색)"},
                    "element": {
                        "type": "external_select",
                        "action_id": "event_id",
                        "placeholder": {"type": "plain_text", "text": "검색어 입력"},
                        "min_query_length": 1
                    }
                },
                # Input 4: Category Picker (Only needed if Mode is Category)
                {
                    "type": "input",
                    "block_id": "cat_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "카테고리 선택 (카테고리 모드를 선택했을경우)"},
                    "element": {
                        "type": "static_select",
                        "action_id": "cat_name",
                        "options": get_category_options() 
                    }
                }
            ]
        }
    )


@bolt_app.action("confirm_registration")
def handle_registration_confirm(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    event_id = int(body["actions"][0]["value"])
    
    with flask_app.app_context():
        sub = Subscription.query.filter_by(channel_id=user_id, event_id=event_id).first()
        if sub and sub.status == "Pending":
            sub.status = "Registered"
            
            # Check if message exists (it may not in all contexts)
            if body.get('message') and body['message'].get('text'):
                # 1. Update Student's Button (UI Refresh)
                original_text = body["message"]["text"]
                client.chat_update(
                    channel=body["channel"]["id"],
                    ts=body["message"]["ts"],
                    text=original_text,
                    blocks=[
                        {"type": "section", "text": {"type": "mrkdwn", "text": original_text}},
                        {"type": "context", "elements": [{"type": "mrkdwn", "text": "✅ *등록 확인 완료*"}]}
                    ]
                )

            # 2. 🆕 SUCCESS FEED: Notify Consultants
            config = AppConfig.query.get("consultant_channel")
            if config:
                event = Event.query.get(event_id)
                client.chat_postMessage(
                    channel=config.value,
                    text=f"🎉 *등록 확인:* <#{user_id}> 님이 *{event.title}* 등록을 완료했습니다!"
                )
            
            db.session.commit()

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

@bolt_app.view("submit_admin_sub")
def handle_admin_sub_submission(ack, body, view, client):
    ack()
    
    # 1. Extract Data
    values = view["state"]["values"]
    target_user = values["target_user"]["conversation_select"]["selected_conversation"]
    mode = values["sub_type"]["mode_select"]["selected_option"]["value"]
    
    # Context info
    admin_id = body["user"]["id"]
    channel_id = view["private_metadata"]
    msg = ""

    with flask_app.app_context():
        
        # --- MODE 1: SINGLE ITEM ---
        if mode == "item":
            selected_option = values["event_select"]["event_id"]["selected_option"]
            if not selected_option or selected_option["value"] == "none":
                # Send error message to Admin
                client.chat_postMessage(channel=admin_id, text="⚠️ 이벤트를 선택해야 합니다.")
                return

            event_id = int(selected_option["value"])
            event = Event.query.get(event_id)
            
            # Subscribe
            if not Subscription.query.filter_by(channel_id=target_user, event_id=event_id).first():
                db.session.add(Subscription(channel_id=target_user, event_id=event_id, status='Pending'))
                msg = f"✅ <#{target_user}> 님을 *{event.title}*에 구독시켰습니다."
            else:
                msg = f"ℹ️ <#{target_user}> 님은 이미 해당 이벤트에 구독 중입니다."

        # --- MODE 2: CATEGORY ---
        elif mode == "cat":
            selected_cat = values["cat_select"]["cat_name"]["selected_option"]
            if not selected_cat:
                client.chat_postMessage(channel=admin_id, text="⚠️ 카테고리를 선택해야 합니다.")
                return
            
            cat_name = selected_cat["value"]
            cat_events = Event.query.filter_by(event_type=cat_name).filter(Event.registration_deadline >= datetime.now().date()).all()
            
            count = 0
            for event in cat_events:
                if not Subscription.query.filter_by(channel_id=target_user, event_id=event.id).first():
                    db.session.add(Subscription(channel_id=target_user, event_id=event.id, status='Pending'))
                    count += 1
            msg = f"✅ <#{target_user}> 님을 *{cat_name}* 카테고리 전체({count}개)에 구독시켰습니다."

        # --- MODE 3: ALL ---
        elif mode == "all":
            all_events = Event.query.filter(Event.registration_deadline >= datetime.now().date()).all()
            count = 0
            for event in all_events:
                if not Subscription.query.filter_by(channel_id=target_user, event_id=event.id).first():
                    db.session.add(Subscription(channel_id=target_user, event_id=event.id, status='Pending'))
                    count += 1
            msg = f"✅ <#{target_user}> 님을 *모든 이벤트({count}개)*에 구독시켰습니다."

        db.session.commit()
    
    # Notify Admin of success
    client.chat_postEphemeral(channel=channel_id, user=admin_id, text=msg)

@bolt_app.view("submit_admin_register")
def handle_admin_register_submission(ack, body, view, client):
    ack()
    
    # 1. Extract Data
    values = view["state"]["values"]
    target_user = values["target_user"]["conversation_select"]["selected_conversation"]
    mode = values["sub_type"]["mode_select"]["selected_option"]["value"]
    
    # Context info
    admin_id = body["user"]["id"]
    channel_id = view["private_metadata"]
    msg = ""

    with flask_app.app_context():
        
        # --- MODE 1: SINGLE ITEM ---
        if mode == "item":
            selected_option = values["event_select"]["event_id"]["selected_option"]
            if not selected_option or selected_option["value"] == "none":
                # Send error message to Admin
                client.chat_postMessage(channel=admin_id, text="⚠️ 이벤트를 선택해야 합니다.")
                return

            event_id = int(selected_option["value"])
            event = Event.query.get(event_id)
            sub = Subscription.query.filter_by(channel_id=target_user, event_id=event_id).first()
            if sub:
                sub.status = 'Registered'
                db.session.commit()
            msg = f"✅ <#{target_user}> 님을 *{event.title}*에 등록시켰습니다."

        # --- MODE 2: CATEGORY ---
        elif mode == "cat":
            selected_cat = values["cat_select"]["cat_name"]["selected_option"]
            if not selected_cat:
                client.chat_postMessage(channel=admin_id, text="⚠️ 카테고리를 선택해야 합니다.")
                return
            
            cat_name = selected_cat["value"]
            cat_events = Event.query.filter_by(event_type=cat_name).filter(Event.registration_deadline >= datetime.now().date()).all()
            
            count = 0
            for event in cat_events:
                if not Subscription.query.filter_by(channel_id=target_user, event_id=event.id).first():
                    db.session.add(Subscription(channel_id=target_user, event_id=event.id, status='Pending'))
                    count += 1
            msg = f"✅ <#{target_user}> 님을 *{cat_name}* 카테고리 전체({count}개)에 등록시켰습니다."

        # --- MODE 3: ALL ---
        elif mode == "all":
            all_events = Event.query.filter(Event.registration_deadline >= datetime.now().date()).all()
            count = 0
            for event in all_events:
                if not Subscription.query.filter_by(channel_id=target_user, event_id=event.id).first():
                    db.session.add(Subscription(channel_id=target_user, event_id=event.id, status='Pending'))
                    count += 1
            msg = f"✅ <#{target_user}> 님을 *모든 이벤트({count}개)*에 등록시켰습니다."

        db.session.commit()
    
    # Notify Admin of success
    client.chat_postEphemeral(channel=channel_id, user=admin_id, text=msg)

@bolt_app.view("submit_send_event_message")
def handle_send_message_submission(ack, body, view, client):
    ack()
    values = view["state"]["values"]
    selected_event = values["event_select"]["event_id"]["selected_option"]
    message_text = values["message"]["msg_text"]["value"]
    admin_id = body["user"]["id"]
    channel_id = view["private_metadata"]
    
    if not selected_event or selected_event["value"] == "none":
        client.chat_postEphemeral(channel=channel_id, user=admin_id, text="⚠️ 이벤트를 선택해야 합니다.")
        return
    
    event_id = int(selected_event["value"])
    
    with flask_app.app_context():
        event = Event.query.get(event_id)
        if not event:
            client.chat_postEphemeral(channel=channel_id, user=admin_id, text="⚠️ 이벤트를 찾을 수 없습니다.")
            return
        
        # Get all subscriptions for this event
        subs = Subscription.query.filter_by(event_id=event_id).all()
        
        if not subs:
            client.chat_postEphemeral(channel=channel_id, user=admin_id, text=f"ℹ️ *{event.title}*: 구독한 학생이 없습니다.")
            return
        
        count = 0
        for sub in subs:
            try:
                client.chat_postMessage(
                    channel=sub.channel_id,
                    text=message_text,
                    blocks=[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": message_text}
                        }
                    ]
                )
                count += 1
            except Exception as e:
                logger.error(f"Failed to send message to {sub.channel_id}: {e}")
        
        client.chat_postEphemeral(channel=channel_id, user=admin_id, text=f"📨 *{event.title}*: {count}명에게 메시지를 발송했습니다.")

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
            if not Subscription.query.filter_by(channel_id=user_id, event_id=event_id).first():
                db.session.add(Subscription(channel_id=user_id, event_id=event_id, status='Pending'))
        else:
            Subscription.query.filter_by(channel_id=user_id, event_id=event_id).delete()
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
                if not Subscription.query.filter_by(channel_id=user_id, event_id=event_id).first():
                    db.session.add(Subscription(channel_id=user_id, event_id=event_id, status='Pending'))
            else:
                Subscription.query.filter_by(user_slack_id=user_id, event_id=event_id).delete()
            db.session.commit()
            client.views_publish(user_id=user_id, view={"type": "home", "blocks": get_dashboard_view(user_id)})

@bolt_app.options("event_search")
def handle_event_search(ack, body):
    """Dynamically load events based on user search query."""
    search_value = body.get("value", "").lower()
    
    with flask_app.app_context():
        # Search events by title
        events = Event.query.filter(
            Event.title.ilike(f"%{search_value}%"),
            Event.registration_deadline >= datetime.now().date()
        ).limit(100).all()
        
        options = []
        for e in events:
            date_str = e.event_date.strftime('%Y-%m-%d')
            safe_title = e.title
            safe_cat = e.event_type
            occupied_len = len(safe_cat) + len(date_str) + 5
            
            if len(safe_title) > 75 - occupied_len:
                safe_title = safe_title[:max(0, 75 - occupied_len - 3)] + "..."
            
            label_text = f"{safe_cat} - {safe_title} ({date_str})"
            options.append({
                "text": {"type": "plain_text", "text": label_text},
                "value": str(e.id)
            })
    ack(options=options)

@bolt_app.options("event_id")
def handle_admin_event_search(ack, body):
    """Dynamically load events for admin subscription modal."""
    search_value = body.get("value", "").lower()

    with flask_app.app_context():
        events = Event.query.filter(
            Event.title.ilike(f"%{search_value}%"),
            Event.registration_deadline >= datetime.now().date()
        ).limit(100).all()
        options = []
        for e in events:
            date_str = e.event_date.strftime('%Y-%m-%d')
            safe_title = e.title
            safe_cat = e.event_type
            occupied_len = len(date_str) + 5
            
            if len(safe_title) > 75 - occupied_len:
                safe_title = safe_title[:max(0, 75 - occupied_len - 3)] + "..."
            
            label_text = f"{safe_title} ({date_str})"
            options.append({
                "text": {"type": "plain_text", "text": label_text},
                "value": str(e.id)
            })
    ack(options=options)

@bolt_app.options("event_subscribed")
def handle_admin_event_subscribed_search(ack, body):
    """Dynamically load events for admin subscription modal."""
    
    # 1. FIX: Extract private_metadata correctly from the view payload
    # Note: private_metadata is only available if this input is inside a Modal.
    try:
        channel_id = body.get('view', {}).get('private_metadata')
        if not channel_id:
            # Fallback or error handling if metadata is missing
            ack(options=[])
            return
    except KeyError:
        ack(options=[])
        return

    with flask_app.app_context():
        # 2. Query returns a list of tuples: [(Subscription, Event), (Subscription, Event)...]
        results = db.session.query(Subscription, Event)\
            .join(Event)\
            .filter(
                Subscription.channel_id == channel_id, 
                Subscription.status == 'Pending'
            ).all()

        options = []
        
        # 3. FIX: Unpack the tuple (sub, event)
        for sub, event in results:
            date_str = event.event_date.strftime('%Y-%m-%d')
            safe_title = event.title
            
            # Formatting logic (User's original logic preserved)
            occupied_len = len(date_str) + 5
            max_title_len = 75 - occupied_len
            
            if len(safe_title) > max_title_len:
                safe_title = safe_title[:max(0, max_title_len - 3)] + "..."
            
            label_text = f"{safe_title} ({date_str})"
            
            options.append({
                "text": {"type": "plain_text", "text": label_text},
                "value": str(event.id) # Ensure value is the Event ID (or Subscription ID based on your need)
            })
    
    ack(options=options)

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

        def notify(evt, msg):
            cnt = 0
            # 🆕 Only notify if they haven't registered yet? 
            # Or notify everyone and let them confirm? 
            # Decision: Notify everyone, but only show button if status is Pending.
            for sub in Subscription.query.filter_by(event_id=evt.id).all():
                try:
                    blocks = [
                        {"type": "section", "text": {"type": "mrkdwn", "text": msg}}
                    ]
                    
                    # 🆕 ADD CONFIRM BUTTON if Pending
                    if sub.status == "Pending":
                        blocks.append({
                            "type": "actions",
                            "elements": [{
                                "type": "button",
                                "text": {"type": "plain_text", "text": "✅ I Registered (등록 완료)"},
                                "style": "primary",
                                "value": str(evt.id),
                                "action_id": "confirm_registration"
                            }]
                        })
                    else:
                        blocks.append({
                            "type": "context",
                            "elements": [{"type": "mrkdwn", "text": "✅ Status: Registered"}]
                        })

                    bolt_app.client.chat_postMessage(channel=sub.user_slack_id, text=msg, blocks=blocks)
                    cnt += 1
                except Exception as e: logger.error(f"Fail DM {sub.user_slack_id}: {e}")
            return cnt

        for days_left in [0, 1, 2, 3]:
            target_date = today + timedelta(days=days_left)
            time_str = "오늘" if days_left == 0 else "내일" if days_left == 1 else f"{days_left}일 후"

            # 1. Registration Deadlines
            deadline_events = Event.query.filter_by(registration_deadline=target_date).all()
            for event in deadline_events:
                msg = f"⚠️ *{event.event_type}* *{event.title}* 가입 데드라인이 *{time_str}* 닫힙니다 ({event.registration_deadline})!"
                total_sent += notify(event, msg)

            # 2. Event Dates
            test_day_events = Event.query.filter_by(event_date=target_date).all()
            for event in test_day_events:
                msg = f"📅 *이벤트 알림:* *{event.event_type}* *{event.title}*이 *{time_str}* 입니다 ({event.event_date})!"
                total_sent += notify(event, msg)

    # 2. Run Consultant Briefing
        try:
            config = AppConfig.query.get("consultant_channel")
            if config:
                # Generate the fancy blocks
                briefing_blocks = generate_morning_briefing(today)
                
                # Post to the consultant channel
                bolt_app.client.chat_postMessage(
                    channel=config.value,
                    text="Morning Briefing", # Fallback text
                    blocks=briefing_blocks
                )
                print("Briefing sent successfully.")
        except Exception as e:
            logger.error(f"Failed to send briefing: {e}")

        return {"status": "success", "reminders_sent": total_sent}, 200

    except Exception as e:
        logger.error(f"Cron failed: {e}")
        return {"error": str(e)}, 500

def generate_morning_briefing(today):
    """
    Generates a Block Kit message for the daily Consultant Briefing in Korean.
    Includes:
    1. Red Zone: Deadlines in next 48 hours with Pending students.
    2. Horizon: Events in next 7 days with status summary.
    """
    # Korean Date Format (e.g., 2026년 01월 20일)
    date_str = today.strftime('%Y년 %m월 %d일')
    
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🌅 모닝 브리핑: {date_str}"}},
        {"type": "divider"}
    ]
    
    # --- SECTION 1: 🚨 THE RED ZONE (Urgent Deadlines) ---
    # Look for deadlines Today (0) and Tomorrow (1)
    urgent_found = False
    
    for d in [0, 1]:
        target_date = today + timedelta(days=d)
        time_str = "오늘" if d == 0 else "내일"
        
        # Find events with deadlines on this day
        deadlines = Event.query.filter_by(registration_deadline=target_date).all()
        
        for e in deadlines:
            # Find who hasn't registered yet
            pending_subs = Subscription.query.filter_by(event_id=e.id, status="Pending").all()
            
            if pending_subs:
                urgent_found = True
                names = [f"<#{s.channel_id}>" for s in pending_subs]
                student_list = ", ".join(names)
                
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn", 
                        "text": f"🚨 *긴급 점검: {e.title}*\n등록 마감이 *{time_str}* 입니다!"
                    }
                })
                blocks.append({
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"⚠️ *미등록 학생 {len(pending_subs)}명:* {student_list}"}]
                })
                # Actionable Tip
                blocks.append({
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"👉 *조치:* `/nudge-pending {e.id}` 명령어로 독촉 알림 보내기"}]
                })
            else:
                # If everyone registered, show a mini success message
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"✅ *{e.title}* (마감 {time_str}): 구독한 모든 학생이 등록을 완료했습니다."}
                })

    if not urgent_found:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "✅ *긴급 사항 없음:* 48시간 내 마감되는 일정의 등록이 모두 완료되었습니다."}
        })

    blocks.append({"type": "divider"})

    # --- SECTION 2: 📅 THE HORIZON (Next 7 Days) ---
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*📅 다가오는 일정 (향후 7일)*"}})
    
    horizon_found = False
    end_date = today + timedelta(days=7)
    
    # Query events happening between tomorrow and 7 days from now
    upcoming_events = Event.query.filter(Event.event_date > today, Event.event_date <= end_date).order_by(Event.event_date).all()
    
    if upcoming_events:
        text_lines = ""
        for e in upcoming_events:
            # Calculate status summary
            total = Subscription.query.filter_by(event_id=e.id).count()
            pending = Subscription.query.filter_by(event_id=e.id, status="Pending").count()
            registered = total - pending
            
            # Status Logic: Green if all registered, Yellow if <3 pending, Red otherwise
            status_icon = "🟢" if pending == 0 else "🟡" if pending < 3 else "🔴"
            date_pretty = e.event_date.strftime('%m/%d')
            
            text_lines += f"{status_icon} *{date_pretty}:* {e.title} ({total}명 중 {registered}명 완료)\n"
        
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": text_lines}
        })
    else:
         blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "이번 주 예정된 이벤트가 없습니다."}]})

    return blocks

if __name__ == "__main__":
    flask_app.run(port=3000)