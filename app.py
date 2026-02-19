from collections import defaultdict
import os
import logging
import re
import math
import secrets
import json
from datetime import datetime, timedelta
from xml.parsers.expat import errors
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

class ChannelNotification(db.Model):
    """Stores notification intervals for specific channels"""
    __tablename__ = 'channel_notification'
    id = db.Column(db.BigInteger, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    interval = db.Column(db.Integer, nullable=False, default=7)
    channel_id = db.Column(db.String(50), nullable=False, unique=True)
    last_interval = db.Column(db.DateTime, nullable=True)

class EventReminder(db.Model):
    """Stores custom reminders scheduled by admins for specific events"""
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('event.id'), nullable=False)
    days_before = db.Column(db.Integer, nullable=False)  # e.g., 3 means "3 days before event"
    message_template = db.Column(db.String(500), nullable=False)
    created_by = db.Column(db.String(50), nullable=False) # Admin ID
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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
    ).filter(Event.event_date >= today)
    
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
                {"type": "button", "text": {"type": "plain_text", "text": "채널구독 관리"}, "action_id": "open_admin_control_modal"},
                {"type": "button", "text": {"type": "plain_text", "text": "어드민 추가"}, "action_id": "open_manage_admins_modal"},
                {"type": "button", "text": {"type": "plain_text", "text": "알림 설정"}, "action_id": "open_interval_settings_modal"}
            ]
        })
        blocks.append({"type": "divider"})

    # Content Calculation (Max 100 blocks)
    remaining_blocks = 100 - len(blocks)
    num_cats = len(event_types)
    if num_cats == 0:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "No categories defined."}})
        return blocks

    items_per_cat = min(math.floor((remaining_blocks - (num_cats * 2)) / num_cats / 2), 5)
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
    ITEMS_PER_PAGE = 18
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

# ---------------------------------------------------------
# NEW: Manage / Unsubscribe Tool
# ---------------------------------------------------------

@bolt_app.command("/admin-manage")
def open_admin_manage_modal(ack, body, client, command):
    ack()
    user_id = command["user_id"]
    open_manage_modal_logic(client, body["trigger_id"], user_id)

@bolt_app.action("open_admin_manage_modal")
def open_admin_manage_modal_button(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    open_manage_modal_logic(client, body["trigger_id"], user_id)

def open_manage_modal_logic(client, trigger_id, user_id):
    """Shared logic to open the management modal"""
    with flask_app.app_context():
        if not is_user_admin(user_id):
            client.chat_postEphemeral(channel=user_id, user=user_id, text="🚫 관리자 권한이 없습니다.")
            return

    client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "submit_admin_manage",
            "private_metadata": user_id,
            "title": {"type": "plain_text", "text": "구독 관리/삭제"},
            "submit": {"type": "plain_text", "text": "실행"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "선택한 채널들의 구독 상태를 변경하거나 삭제합니다."}
                },
                # 1. Select Action (Delete vs Demote)
                {
                    "type": "input",
                    "block_id": "action_type",
                    "label": {"type": "plain_text", "text": "작업 유형"},
                    "element": {
                        "type": "static_select",
                        "action_id": "action_select",
                        "initial_option": {"text": {"type": "plain_text", "text": "🗑️ 구독 취소 (완전 삭제)"}, "value": "delete"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "🗑️ 구독 취소 (완전 삭제)"}, "value": "delete"},
                            {"text": {"type": "plain_text", "text": "📉 등록 취소 (Pending으로 강등)"}, "value": "demote"}
                        ]
                    }
                },
                # 2. Select Channels
                {
                    "type": "input",
                    "block_id": "target_user",
                    "label": {"type": "plain_text", "text": "대상 채널 선택"},
                    "element": {
                        "type": "multi_conversations_select", 
                        "action_id": "conversations_select",
                        "placeholder": {"type": "plain_text", "text": "채널 검색"},
                        "filter": {
                            "include": ["public", "private"], 
                            "exclude_bot_users": True 
                        }
                    }
                },
                {
                    "type": "divider"
                },
                # 3. Select Scope (Item, Category, All)
                {
                    "type": "input",
                    "block_id": "sub_type",
                    "label": {"type": "plain_text", "text": "적용 범위"},
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
                # 4. Specific Event Selector
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
                # 5. Category Selector
                {
                    "type": "input",
                    "block_id": "cat_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "카테고리 선택"},
                    "element": {
                        "type": "static_select",
                        "action_id": "cat_name",
                        "options": get_category_options() 
                    }
                }
            ]
        }
    )

@bolt_app.view("submit_admin_manage")
def handle_admin_manage_submission(ack, body, view, client):
    ack()
    
    values = view["state"]["values"]
    
    # Get Data
    target_channels = values["target_user"]["conversations_select"]["selected_conversations"]
    action_type = values["action_type"]["action_select"]["selected_option"]["value"]
    mode = values["sub_type"]["mode_select"]["selected_option"]["value"]
    admin_id = body["user"]["id"]
    
    if not target_channels:
        return

    success_count = 0
    total_targets = len(target_channels)
    affected_channels = []

    with flask_app.app_context():
        # A. Determine Target Events
        target_events = []
        mode_label = ""

        if mode == "item":
            selected_option = values["event_select"]["event_id"]["selected_option"]
            if not selected_option:
                client.chat_postEphemeral(channel=admin_id, user=admin_id, text="⚠️ 이벤트를 선택해야 합니다.")
                return
            event = Event.query.get(int(selected_option["value"]))
            target_events = [event]
            mode_label = f"이벤트 *{event.title}*"

        elif mode == "cat":
            selected_cat = values["cat_select"]["cat_name"]["selected_option"]
            if not selected_cat:
                client.chat_postEphemeral(channel=admin_id, user=admin_id, text="⚠️ 카테고리를 선택해야 합니다.")
                return
            cat_name = selected_cat["value"]
            # Fetch ALL events in category (even past ones, if we want to clean up)
            target_events = Event.query.filter_by(event_type=cat_name).all()
            mode_label = f"카테고리 *{cat_name}*"

        elif mode == "all":
            target_events = Event.query.all()
            mode_label = "*모든 이벤트*"

        # B. Execute Action
        for channel in target_channels:
            channel_hit = False
            
            for event in target_events:
                sub = Subscription.query.filter_by(channel_id=channel, event_id=event.id).first()
                
                if sub:
                    if action_type == "delete":
                        db.session.delete(sub)
                        channel_hit = True
                    elif action_type == "demote":
                        if sub.status == "Registered":
                            sub.status = "Pending"
                            channel_hit = True
            
            if channel_hit:
                success_count += 1
                affected_channels.append(f"<#{channel}>")
                
                # Notify User (Optional - remove if you want silent deletion)
                try:
                    action_msg = "구독이 취소되었습니다." if action_type == "delete" else "상태가 '미등록(Pending)'으로 변경되었습니다."
                    client.chat_postMessage(
                        channel=channel, 
                        text=f"ℹ️ 관리자가 귀하의 {mode_label} {action_msg}"
                    )
                except Exception:
                    pass

        # C. Consultant Report
        if affected_channels:
            config = AppConfig.query.get("consultant_channel")
            if config:
                action_str = "구독 취소" if action_type == "delete" else "등록 취소(Pending)"
                channel_list_str = ", ".join(affected_channels)
                
                msg = f"{channel_list_str} 님의 {mode_label} {action_str} 처리가 완료되었습니다."
                try:
                    client.chat_postMessage(channel=config.value, text=msg)
                except Exception:
                    pass

        db.session.commit()

    # Final Report to Admin
    action_word = "삭제" if action_type == "delete" else "변경"
    client.chat_postEphemeral(
        channel=admin_id, 
        user=admin_id, 
        text=f"✅ 작업 완료: {success_count}개 채널에 대해 {mode_label} {action_word} 처리를 완료했습니다."
    )


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

@bolt_app.command("/list-channels")
def handle_list_channels(ack, respond, command):
    # Acknowledge command request
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

        # Find Subscribed Channels
        subs = Subscription.query.filter_by(event_id=event.id).all()
        if not subs:
            respond(f"ℹ️ *{event.title}*: 구독한 채널이 없습니다.")
            return

        # Build List
        msg = f"*📋 {event.title}* 구독 채널 리스트 ({len(subs)}개):\n"
        for sub in subs:
            status_emoji = "✅" if sub.status == "Registered" else "⏳"
            status_text = "*등록* *완료*" if sub.status == "Registered" else "*미등록*"
            msg += f"• {status_emoji} {status_text} <#{sub.channel_id}>\n"

        respond(msg)



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

@bolt_app.command("/check-settings")
def handle_check_settings(ack, body, client, respond): # <--- 1. Add 'respond' here
    ack()
    user_id = body["user_id"]
    
    with flask_app.app_context():
        if not is_user_admin(user_id):
            # You can also switch this to respond() for consistency
            respond(text="🚫 관리자 권한이 없습니다.") 
            return

        # Get 'Today' for filtering
        today = datetime.now().date()

        # ---------------------------------------------------------
        # 1. Fetch Morning Briefing Settings (Global)
        # ---------------------------------------------------------
        global_interval = AppConfig.query.get("notification_interval")
        last_triggered = AppConfig.query.get("notification_last_triggered")
        
        g_val = global_interval.value if global_interval else "1 (Default)"
        l_val = last_triggered.value if last_triggered else "Never"

        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "⚙️ 현재 알림 설정 상태 (Active Only)"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "🌅 *모닝 브리핑 (Global)*"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Trigger Interval:*\n{g_val}일 마다"},
                    {"type": "mrkdwn", "text": f"*Last Triggered:*\n{l_val}"}
                ]
            },
            {"type": "divider"}
        ]

        # ---------------------------------------------------------
        # 2. Fetch Channel-Specific Intervals (Active Only)
        # ---------------------------------------------------------
        active_channels = db.session.query(Subscription.channel_id)\
            .join(Event, Subscription.event_id == Event.id)\
            .filter(Event.event_date >= today)\
            .distinct().all()
        
        custom_settings = ChannelNotification.query.all()
        custom_map = {c.channel_id: c for c in custom_settings}
        
        channel_text = ""
        if not active_channels:
            channel_text = "ℹ️ 활성화된(미래 일정이 있는) 채널이 없습니다."
        else:
            for ch_row in active_channels:
                cid = ch_row[0]
                setting = custom_map.get(cid)
                
                interval_disp = f"{setting.interval} days" if setting else "7 days (Default)"
                last_run_disp = setting.last_interval.strftime('%Y-%m-%d') if (setting and setting.last_interval) else "Never"
                
                channel_text += f"• <#{cid}>: *{interval_disp}* (Last: {last_run_disp})\n"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "📺 *채널별 알림 간격 (Upcoming Events Only)*"}})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": channel_text}})
        blocks.append({"type": "divider"})

        # ---------------------------------------------------------
        # 3. Fetch Saved Event Reminders (Not Yet Sent)
        # ---------------------------------------------------------
        reminders = db.session.query(EventReminder, Event)\
            .join(Event, EventReminder.event_id == Event.id)\
            .order_by(Event.event_date).all()
        
        active_reminders = []
        for r, e in reminders:
            trigger_date = e.event_date - timedelta(days=r.days_before)
            if trigger_date >= today:
                active_reminders.append((r, e, trigger_date))

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "⏰ *예약된 리마인더 (대기중)*"}})

        if not active_reminders:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "대기 중인 리마인더가 없습니다."}]})
        else:
            for r, e, t_date in active_reminders:
                msg_preview = (r.message_template[:40] + '..') if len(r.message_template) > 40 else r.message_template
                
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn", 
                        "text": f"*{e.title}* (D-{r.days_before})\n📅 발송예정: {t_date}\n📝 {msg_preview}"
                    },
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "삭제", "emoji": True},
                        "style": "danger",
                        "value": str(r.id),
                        "action_id": "delete_event_reminder"
                    }
                })

        # ---------------------------------------------------------
        # FINAL FIX: Use 'respond' instead of client.chat_postMessage
        # ---------------------------------------------------------
        respond(blocks=blocks) 
        # Note: By default, this is ephemeral (visible only to you). 
        # If you want everyone in the channel to see it, use: respond(blocks=blocks, response_type='in_channel')

@bolt_app.action("delete_event_reminder")
def handle_delete_event_reminder(ack, body, client, respond):
    ack()
    
    # 1. Get the Reminder ID from the button value
    reminder_id = int(body["actions"][0]["value"])
    user_id = body["user"]["id"]
    
    with flask_app.app_context():
        # 2. Find and Delete
        reminder = EventReminder.query.get(reminder_id)
        
        if reminder:
            # Save info for the confirmation message before deleting
            event_title = Event.query.get(reminder.event_id).title
            days = reminder.days_before
            
            db.session.delete(reminder)
            db.session.commit()
            
            # 3. Post a confirmation (Ephemeral)
            client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=user_id,
                text=f"🗑️ *{event_title}* (D-{days}) 리마인더가 삭제되었습니다."
            )
            
            # Optional: You could also try to refresh the original message using respond(replace_original=True, ...) 
            # but usually just letting them know it's deleted is enough.
        else:
            client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=user_id,
                text="⚠️ 이미 삭제된 리마인더입니다."
            )

# ---------------------------------------------------------
# UNIFIED MANAGEMENT TOOL
# ---------------------------------------------------------

@bolt_app.command("/admin-control")
def open_admin_control_modal(ack, body, client, command):
    ack()
    user_id = command["user_id"]
    open_control_modal_logic(client, body["trigger_id"], user_id)

@bolt_app.action("open_admin_control_modal")
def open_admin_control_modal_button(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    open_control_modal_logic(client, body["trigger_id"], user_id)

def open_control_modal_logic(client, trigger_id, user_id):
    with flask_app.app_context():
        if not is_user_admin(user_id):
            client.chat_postEphemeral(channel=user_id, user=user_id, text="🚫 관리자 권한이 없습니다.")
            return

    client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "submit_admin_control",
            "private_metadata": user_id,
            "title": {"type": "plain_text", "text": "채널 통합 관리"},
            "submit": {"type": "plain_text", "text": "실행"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "선택한 채널들에 대해 일괄 작업을 수행합니다."}
                },
                # 1. THE MASTER ACTION SWITCH
                {
                    "type": "input",
                    "block_id": "action_type",
                    "label": {"type": "plain_text", "text": "수행할 작업"},
                    "element": {
                        "type": "static_select",
                        "action_id": "action_select",
                        "options": [
                            {"text": {"type": "plain_text", "text": "⏳ 구독 - 특정 채널에 이벤트 알람 추가"}, "value": "subscribe"},
                            {"text": {"type": "plain_text", "text": "✅ 등록 - '등록 완료'로 변경/추가"}, "value": "register"},
                            {"text": {"type": "plain_text", "text": "📉 등록 취소 - 채널의 이벤트를 '대기중'으로 강등"}, "value": "demote"},
                            {"text": {"type": "plain_text", "text": "🗑️ 구독 삭제 - 채널의 이벤트 구독 취소"}, "value": "delete"}
                        ]
                    }
                },
                # 2. Select Channels
                {
                    "type": "input",
                    "block_id": "target_user",
                    "label": {"type": "plain_text", "text": "대상 채널 선택"},
                    "element": {
                        "type": "multi_conversations_select", 
                        "action_id": "conversations_select",
                        "placeholder": {"type": "plain_text", "text": "채널 검색"},
                        "filter": {
                            "include": ["public", "private"], 
                            "exclude_bot_users": True 
                        }
                    }
                },
                {"type": "divider"},
                # 3. Scope Selectors (Item/Cat/All)
                {
                    "type": "input",
                    "block_id": "sub_type",
                    "label": {"type": "plain_text", "text": "적용 범위"},
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
                {
                    "type": "input",
                    "block_id": "event_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "이벤트 선택"},
                    "element": {
                        "type": "external_select",
                        "action_id": "event_id",
                        "placeholder": {"type": "plain_text", "text": "검색어 입력"},
                        "min_query_length": 1
                    }
                },
                {
                    "type": "input",
                    "block_id": "cat_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "카테고리 선택"},
                    "element": {
                        "type": "static_select",
                        "action_id": "cat_name",
                        "options": get_category_options() 
                    }
                }
            ]
        }
    )

@bolt_app.view("submit_admin_control")
def handle_admin_control_submission(ack, body, view, client):
    ack()
    
    values = view["state"]["values"]
    admin_id = body["user"]["id"]
    
    # 1. Parse Inputs
    input_block = values["target_user"]["conversations_select"]
    target_channels = input_block.get("selected_conversations") or input_block.get("selected_channels")
    if not target_channels: return

    action = values["action_type"]["action_select"]["selected_option"]["value"]
    mode = values["sub_type"]["mode_select"]["selected_option"]["value"]
    
    success_count = 0
    success_list = [] # For Consultant Report

    with flask_app.app_context():
        # A. Determine Target Events
        target_events = []
        mode_label = ""

        if mode == "item":
            selected_option = values["event_select"]["event_id"]["selected_option"]
            if not selected_option:
                client.chat_postEphemeral(channel=admin_id, user=admin_id, text="⚠️ 이벤트를 선택해야 합니다.")
                return
            event = Event.query.get(int(selected_option["value"]))
            target_events = [event]
            mode_label = f"*{event.title}*"
        elif mode == "cat":
            selected_cat = values["cat_select"]["cat_name"]["selected_option"]
            if not selected_cat: return
            cat_name = selected_cat["value"]
            target_events = Event.query.filter_by(event_type=cat_name).all() # Fetch all (even past) for management
            mode_label = f"*{cat_name}* 카테고리"
        elif mode == "all":
            target_events = Event.query.all()
            mode_label = "*모든 이벤트*"

        # B. Execute Logic Loop
        for channel in target_channels:
            channel_hit = False
            
            for event in target_events:
                sub = Subscription.query.filter_by(channel_id=channel, event_id=event.id).first()
                
                # --- LOGIC BRANCHING ---
                if action == "register":
                    # Create or Update to Registered
                    if not sub:
                        db.session.add(Subscription(channel_id=channel, event_id=event.id, status='Registered'))
                        channel_hit = True
                    elif sub.status != 'Registered':
                        sub.status = 'Registered'
                        channel_hit = True
                        
                elif action == "subscribe":
                    # Create only if missing (Pending)
                    if not sub:
                        db.session.add(Subscription(channel_id=channel, event_id=event.id, status='Pending'))
                        channel_hit = True
                        
                elif action == "demote":
                    # Update Registered -> Pending
                    if sub and sub.status == 'Registered':
                        sub.status = 'Pending'
                        channel_hit = True
                        
                elif action == "delete":
                    # Delete if exists
                    if sub:
                        db.session.delete(sub)
                        channel_hit = True

            # C. Notify Channel (Only if something changed)
            if channel_hit:
                success_count += 1
                success_list.append(f"<#{channel}>")
                
                # Define messages for each action
                msgs = {
                    "register": f"✅ 관리자가 이 채널의 {mode_label}\u200B에 등록 완료했습니다. ",
                    "subscribe": f"⏳ 관리자가 이 채널의 {mode_label}\u200B의 알림 목록에 추가했습니다.",
                    "demote": f"📉 관리자가 이 채널의 {mode_label}\u200B에 등록을 취소했습니다..",
                    "delete": f"🗑️ 관리자가 이 채널의 {mode_label}\u200B에 구독을 취소했습니다."
                }

                try:
                    client.chat_postMessage(channel=channel, text=msgs[action])
                except Exception:
                    pass
        
        # D. Consultant Report
        if success_list:
            config = AppConfig.query.get("consultant_channel")
            if config:
                verb_map = {
                    "register": "등록 완료",
                    "subscribe": "구독 추가",
                    "demote": "등록 취소",
                    "delete": "구독 삭제"
                }

                emoji_map = {
                    "register": "✅",
                    "subscribe": "⏳",
                    "demote": "📉",
                    "delete": "🗑️"
                }
                
                channel_str = ", ".join(success_list)
                consultant_msg = f"{emoji_map[action]} {channel_str} 채널이 {mode_label}에 *{verb_map[action]}* 처리되었습니다."
                
                try:
                    client.chat_postMessage(channel=config.value, text=consultant_msg)
                except Exception:
                    pass

        db.session.commit()
    
    # E. Final Report to Admin
    client.chat_postEphemeral(
        channel=admin_id, 
        user=admin_id, 
        text=f"✅ 작업 완료: 총 {len(target_channels)}개 중 {success_count}개 채널에 {action} 작업을 수행했습니다."
    )

@bolt_app.action("open_interval_settings_modal")
def open_interval_settings_modal(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    origin_channel = body.get("channel_id") or body.get("channel", {}).get("id") or user_id

    with flask_app.app_context():
        if not is_user_admin(user_id):
            client.chat_postEphemeral(channel=user_id, user=user_id, text="🚫 관리자 권한이 없습니다.")
            return
        
        # Fetch current setting
        config = AppConfig.query.filter_by(key="notification_interval").first()
        current_value = config.value if config else "7"
        
        # Fetch channels for dropdown
        subscribed_channels = db.session.query(Subscription.channel_id).distinct().all()
        channel_options = [
            {"text": {"type": "plain_text", "text": f"<#{ch[0]}>"}, "value": ch[0]} 
            for ch in subscribed_channels
        ]
        
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "submit_nothing",
            "private_metadata": origin_channel,
            "title": {"type": "plain_text", "text": "Notification Settings"},
            "submit": {"type": "plain_text", "text": "Close"},
            
            "blocks": [
                # --- GROUP SETTINGS ---
                {"type": "section", "text": {"type": "mrkdwn", "text": "🏢 *모닝 브리핑 간격설정*"}},
                {
                    "type": "input",
                    "block_id": "interval_block",
                    "label": {"type": "plain_text", "text": "알림 간격 (Default Days)"},
                    "element": {"type": "plain_text_input", "action_id": "interval_input", "initial_value": str(current_value)}
                },
                {
                    "type": "actions",
                    "block_id": "group_actions",
                    "elements": [{"type": "button", "text": {"type": "plain_text", "text": "💾 모닝브리핑 설정 저장"}, "value": "group", "action_id": "save_group_interval", "style": "primary"}]
                },
                {"type": "divider"},

                # --- CHANNEL SETTINGS ---
                {"type": "section", "text": {"type": "mrkdwn", "text": "📺 *채널별 개별 설정*"}},
                {
                    "type": "input",
                    "block_id": "channels_block",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "대상 채널 선택"},
                    "element": {
                        "type": "multi_static_select",
                        "action_id": "channels_select",
                        "placeholder": {"type": "plain_text", "text": "채널을 선택하세요"},
                        "options": channel_options if channel_options else [{"text": {"type": "plain_text", "text": "No Channels"}, "value": "none"}]
                    }
                },
                {
                    "type": "input",
                    "block_id": "channel_interval_block",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "적용할 알림 간격 (Days)"},
                    "element": {"type": "plain_text_input", "action_id": "channel_interval_input", "initial_value": "7"}
                },
                {
                    "type": "actions",
                    "block_id": "channel_actions",
                    "elements": [{"type": "button", "text": {"type": "plain_text", "text": "💾 채널 설정 저장"}, "value": "channel", "action_id": "save_channel_interval"}]
                },
                {"type": "divider"},

                # --- NEW: EVENT REMINDER SETTINGS ---
                {"type": "section", "text": {"type": "mrkdwn", "text": "⏰ *이벤트별 리마인더 예약*"}},
                {
                    "type": "input",
                    "block_id": "event_reminder_select", # RENAMED
                    "label": {"type": "plain_text", "text": "이벤트 선택"},
                    "element": {
                        "type": "external_select",
                        "action_id": "event_id",
                        "placeholder": {"type": "plain_text", "text": "검색어 입력"},
                        "min_query_length": 1
                    }
                },
                {
                    "type": "input",
                    "block_id": "event_reminder_days", # RENAMED
                    "label": {"type": "plain_text", "text": "D-Day 설정 (몇 일 전?)"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "days_input",
                        "placeholder": {"type": "plain_text", "text": "ex: 3 (3일 전 발송)"}
                    }
                },
                {
                    "type": "input",
                    "block_id": "event_reminder_msg", # RENAMED
                    "label": {"type": "plain_text", "text": "발송할 메시지"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "msg_text",
                        "multiline": True,
                        "placeholder": {"type": "plain_text", "text": "ex: 잊지말고 준비하세요!"}
                    }
                },
                {
                    "type": "actions",
                    "block_id": "event_reminder_actions", # RENAMED
                    "elements": [{"type": "button", "text": {"type": "plain_text", "text": "💾 리마인더 예약 저장"}, "value": "reminder", "action_id": "save_event_reminder"}]
                },
            ]
        }
    )

@bolt_app.action("save_event_reminder")
def handle_save_event_reminder(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    view = body["view"]
    values = view["state"]["values"]
    
    # 1. Extract Values using the NEW Block IDs
    selected_option = values["event_reminder_select"]["event_id"]["selected_option"]
    days_val = values["event_reminder_days"]["days_input"]["value"]
    msg_val = values["event_reminder_msg"]["msg_text"]["value"]
    
    # 2. Validation
    errors = []
    if not selected_option:
        errors.append("이벤트를 선택해주세요.")
    if not days_val or not days_val.isdigit():
        errors.append("날짜(D-Day)는 숫자여야 합니다.")
    if not msg_val:
        errors.append("메시지를 입력해주세요.")
        
    if errors:
        client.chat_postEphemeral(channel=user_id, user=user_id, text=f"⚠️ 저장 실패: {', '.join(errors)}")
        return

    # 3. Save to DB
    event_id = int(selected_option["value"])
    days_int = int(days_val)
    
    with flask_app.app_context():
        # Check if identical reminder exists to prevent duplicates (optional)
        exists = EventReminder.query.filter_by(event_id=event_id, days_before=days_int).first()
        if exists:
            exists.message_template = msg_val # Update message if exists
            action_text = "업데이트"
        else:
            new_reminder = EventReminder(
                event_id=event_id,
                days_before=days_int,
                message_template=msg_val,
                created_by=user_id
            )
            db.session.add(new_reminder)
            action_text = "저장"
        
        db.session.commit()
        
        # Fetch event title for confirmation
        event_title = Event.query.get(event_id).title
        
    client.chat_postEphemeral(
        channel=user_id, 
        user=user_id, 
        text=f"✅ *{event_title}* {days_int}일 전 알림이 {action_text}되었습니다."
    )

@bolt_app.action("save_group_interval")
def handle_save_group_interval(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    
    # ✅ FIX: Retrieve channel from private_metadata (body['channel'] is unreliable in modals)
    channel_id = body["view"]["private_metadata"]
    
    view = body["view"]
    values = view["state"]["values"]
    interval_value = values["interval_block"]["interval_input"]["value"]
    
    with flask_app.app_context():
        if not is_user_admin(user_id):
            client.chat_postEphemeral(channel=channel_id, user=user_id, text="🚫 관리자 권한이 없습니다.")
            return
        
        config = AppConfig.query.filter_by(key="notification_interval").first()
        if not config:
            config = AppConfig(key="notification_interval", value=interval_value)
            db.session.add(config)
        else:
            config.value = interval_value
        db.session.commit()
        
        client.chat_postEphemeral(channel=channel_id, user=user_id, text=f"✅ 그룹 알림 간격이 '{interval_value}'로 설정되었습니다.")

@bolt_app.action("save_channel_interval")
def handle_save_channel_interval(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    channel_id = body["view"]["private_metadata"] # ✅ FIX
    
    view = body["view"]
    values = view["state"]["values"]
    
    # ✅ FIX: Correct key for multi_static_select is 'selected_options'
    # And we must extract the 'value' from each option object
    raw_selected_options = values.get("channels_block", {}).get("channels_select", {}).get("selected_options", [])
    selected_channels = [opt['value'] for opt in raw_selected_options]
    
    interval_value = values.get("channel_interval_block", {}).get("channel_interval_input", {}).get("value", "7")
    
    with flask_app.app_context():
        if not is_user_admin(user_id):
            client.chat_postEphemeral(channel=channel_id, user=user_id, text="🚫 관리자 권한이 없습니다.")
            return
        
        if not selected_channels or selected_channels == ["none"]:
            client.chat_postEphemeral(channel=channel_id, user=user_id, text="⚠️ 적어도 하나의 채널을 선택해주세요.")
            return
        
        for selected_channel in selected_channels:
            channel_notif = ChannelNotification.query.filter_by(channel_id=selected_channel).first()
            if not channel_notif:
                channel_notif = ChannelNotification(channel_id=selected_channel, interval=int(interval_value))
                db.session.add(channel_notif)
            else:
                channel_notif.interval = int(interval_value)
        
        db.session.commit()
        
        channel_names = ", ".join([f"<#{ch}>" for ch in selected_channels])
        client.chat_postEphemeral(channel=channel_id, user=user_id, text=f"✅ {channel_names} 채널의 알림 간격이 '{interval_value}'로 설정되었습니다.")

@bolt_app.view("submit_send_event_message")
def handle_send_message_submission(ack, body, view, client):
    # 1. Ack immediately to tell Slack we received the submission
    ack()
    
    values = view["state"]["values"]
    admin_id = body["user"]["id"]
    channel_id = view["private_metadata"]
    
    # Correct Keys based on your modal: event_select -> event_search
    selected_event = values.get("event_select", {}).get("event_search", {}).get("selected_option")
    message_text = values.get("message", {}).get("msg_text", {}).get("value")
    
    # Validation
    if not selected_event or selected_event["value"] == "none":
        client.chat_postEphemeral(channel=channel_id, user=admin_id, text="⚠️ 이벤트를 선택해야 합니다.")
        return
    if not message_text:
        client.chat_postEphemeral(channel=channel_id, user=admin_id, text="⚠️ 메시지 내용을 입력해주세요.")
        return
    
    event_id = int(selected_event["value"])
    
    try:
        with flask_app.app_context():
            event = Event.query.get(event_id)
            if not event:
                client.chat_postEphemeral(channel=channel_id, user=admin_id, text="⚠️ 이벤트를 찾을 수 없습니다.")
                return
            
            # Fetch subscribers
            subs = Subscription.query.filter_by(event_id=event_id).all()
            
            if not subs:
                client.chat_postEphemeral(channel=channel_id, user=admin_id, text=f"ℹ️ *{event.title}*: 구독한 채널이 없습니다.")
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
                                "text": {"type": "mrkdwn", "text": f"📢 *{event.title}* 관련 공지\n\n{message_text}"}
                            }
                        ]
                    )
                    count += 1
                except Exception as e:
                    print(f"Failed to send message to {sub.channel_id}: {e}")
            
            # Final success message to Admin
            client.chat_postEphemeral(channel=channel_id, user=admin_id, text=f"📨 *{event.title}*: {count}개 채널에 메시지를 발송했습니다.")

    except Exception as e:
        print(f"Submission Error: {e}")
        client.chat_postMessage(channel=admin_id, text=f"❌ 발송 중 오류 발생: {str(e)}")

@bolt_app.command("/track")
def handle_track_command(ack, respond, command):
    ack()
    
    admin_id = command["user_id"]
    text = command["text"].strip()
    parts = text.split()
    
    if not parts:
        respond("⚠️ 사용법:\n`/track #channel` (조회)\n`/track add #channel` (추가)\n`/track remove #channel` (제거)\n`/track list` (목록)")
        return

    with flask_app.app_context():
        if not is_user_admin(admin_id):
            respond("🚫 관리자 권한이 없습니다.")
            return

        # 1. Try to see if the first word is a channel ID (Direct View)
        # We don't lowercase it yet because IDs are case-sensitive
        direct_target = parse_channel_id(parts[0])
        
        # 2. Logic Router
        action = parts[0].lower()

        # --- ACTION: ADD ---
        if action == "add" and len(parts) > 1:
            target_id = parse_channel_id(parts[1])
            if not target_id:
                respond("⚠️ 유효한 채널 태그가 아닙니다.")
                return

            if not TrackedStudent.query.filter_by(consultant_id=admin_id, channel_id=target_id).first():
                db.session.add(TrackedStudent(consultant_id=admin_id, channel_id=target_id))
                db.session.commit()
                respond(f"✅ 이제 <#{target_id}> 학생을 추적 관리합니다.")
            else:
                respond(f"ℹ️ <#{target_id}> 학생은 이미 목록에 있습니다.")

        # --- ACTION: REMOVE ---
        elif action == "remove" and len(parts) > 1:
            target_id = parse_channel_id(parts[1])
            if not target_id: return

            entry = TrackedStudent.query.filter_by(consultant_id=admin_id, channel_id=target_id).first()
            if entry:
                db.session.delete(entry)
                db.session.commit()
                respond(f"🗑️ <#{target_id}> 학생을 목록에서 제거했습니다.")
            else:
                respond("⚠️ 목록에 없는 학생입니다.")

        # --- ACTION: LIST ---
        elif action == "list":
            # 1. Fetch everything in a single query using outer joins
            # This joins TrackedStudent -> Subscription -> Event
            results = db.session.query(
                TrackedStudent.channel_id, Subscription, Event
            ).outerjoin(
                Subscription, TrackedStudent.channel_id == Subscription.channel_id
            ).outerjoin(
                Event # Assumes Subscription has a relationship to Event defined. If not, use: Event, Subscription.event_id == Event.id
            ).filter(
                TrackedStudent.consultant_id == admin_id
            ).order_by(
                TrackedStudent.channel_id, 
                Event.event_date
            ).all()

            if not results:
                respond("📭 현재 추적 중인 학생이 없습니다.")
                return

            # 2. Group the flat results by channel_id
            students_data = {}
            for channel_id, sub, event in results:
                if channel_id not in students_data:
                    students_data[channel_id] = []
                # Only append if a subscription actually exists (handles the outer join nulls)
                if sub and event:
                    students_data[channel_id].append((sub, event))

            # 3. Build the Slack message
            msg = "*📋 내 담당 학생 리스트 상세 보고서:*\n\n"
            today = datetime.now().date()

            for target_id, subs in students_data.items():
                msg += f"👤 *<#{target_id}>*\n"
                
                if not subs:
                    msg += "  📂 현재 구독 중인 이벤트가 없습니다.\n\n"
                    continue
                
                upcoming_txt = ""
                history_txt = ""
                
                for sub, event in subs:
                    status_icon = "✅" if sub.status == "Registered" else "⏳"
                    status_text = "등록 완료" if sub.status == "Registered" else "미등록 (Pending)"
                    line = f"  • {status_icon} *{event.title}* | 📅 {event.event_date} | *{status_text}*\n"
                    
                    if event.event_date >= today:
                        if sub.status == "Pending" and event.registration_deadline <= (today + timedelta(days=3)):
                            line += f"      🚨 *경고: 마감 임박 ({event.registration_deadline})*\n"
                        upcoming_txt += line
                    else:
                        history_txt += line

                if upcoming_txt: msg += "  *📅 예정된 일정:*\n" + upcoming_txt
                if history_txt: msg += "  *📜 지난 일정:*\n" + history_txt
                msg += "\n" # Spacing between students
                
            respond(msg.strip())

        # --- ACTION: VIEW DETAILS ---
        # Triggered by '/track #channel' OR '/track show #channel'
        elif direct_target or (action == "show" and len(parts) > 1):
            target_id = direct_target if direct_target else parse_channel_id(parts[1])
            
            if not target_id:
                respond("⚠️ 채널을 지정해주세요.")
                return

            # Fetch Student Details
            subs = db.session.query(Subscription, Event).join(Event).filter(Subscription.channel_id == target_id).order_by(Event.event_date).all()
            
            if not subs:
                respond(f"📂 <#{target_id}> 학생은 현재 구독 중인 이벤트가 없습니다.")
                return

            response_text = f"*👤 학생 분석 보고서: <#{target_id}>*\n\n"
            today = datetime.now().date()
            upcoming_txt = ""
            history_txt = ""
            
            for sub, event in subs:
                status_icon = "✅" if sub.status == "Registered" else "⏳"
                status_text = "등록 완료" if sub.status == "Registered" else "미등록 (Pending)"
                line = f"• {status_icon} *{event.title}* | 📅 {event.event_date} | *{status_text}*\n"
                
                if event.event_date >= today:
                    if sub.status == "Pending" and event.registration_deadline <= (today + timedelta(days=3)):
                        line += f"    🚨 *경고: 마감 임박 ({event.registration_deadline})*\n"
                    upcoming_txt += line
                else:
                    history_txt += line

            if upcoming_txt: response_text += "*📅 예정된 일정:*\n" + upcoming_txt + "\n"
            if history_txt: response_text += "*📜 지난 일정:*\n" + history_txt
            respond(response_text)
            
        else:
            respond("⚠️ 알 수 없는 명령어입니다. `/track #channel` 혹은 `/track list`를 사용하세요.")

@bolt_app.command("/admin-sub")
def open_admin_sub_modal(ack, body, client, command):
    ack()
    user_id = command["user_id"]

    # Check Admin
    with flask_app.app_context():
        if not is_user_admin(user_id):
            client.chat_postEphemeral(channel=user_id, user=user_id, text="🚫 관리자 권한이 없습니다.")
            return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "submit_admin_sub",
            "private_metadata": user_id,
            "title": {"type": "plain_text", "text": "채널 구독 (다중)"},
            "submit": {"type": "plain_text", "text": "구독하기"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "구독시킬 채널들을 선택하세요."}
                },
                # --- MULTI CHANNEL SELECT ---
                {
                    "type": "input",
                    "block_id": "target_user",
                    "label": {"type": "plain_text", "text": "채널 선택"},
                    "element": {
                        "type": "multi_conversations_select", 
                        "action_id": "conversations_select",
                        "placeholder": {"type": "plain_text", "text": "채널 검색 (비공개 포함)"},
                        "filter": {
                            # 1. Only show Public and Private channels
                            "include": ["public", "private"], 
                            # 2. explicit safety to hide bots (though 'im' exclusion does this mostly)
                            "exclude_bot_users": True 
                        }
                    }
                },
                
                # ----------------------------
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
                {
                    "type": "input",
                    "block_id": "cat_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "카테고리 선택"},
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
    
    with flask_app.app_context():
        if not is_user_admin(user_id):
            client.chat_postEphemeral(channel=user_id, user=user_id, text="🚫 관리자 권한이 없습니다.")
            return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "submit_admin_register",
            "private_metadata": user_id,
            "title": {"type": "plain_text", "text": "채널 등록 (다중)"},
            "submit": {"type": "plain_text", "text": "등록하기"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "등록 완료 처리할 채널을 선택하세요.\n(미구독 채널은 *자동으로 구독 및 등록*됩니다)"}
                },
                # --- MULTI CHANNEL SELECT (No Users) ---
                {
                    "type": "input",
                    "block_id": "target_user",
                    "label": {"type": "plain_text", "text": "채널 선택"},
                    "element": {
                        "type": "multi_conversations_select", 
                        "action_id": "conversations_select",
                        "placeholder": {"type": "plain_text", "text": "채널 검색 (비공개 포함)"},
                        "filter": {
                            "include": ["public", "private"], 
                            "exclude_bot_users": True 
                        }
                    }
                },
                # ---------------------------------------
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
                # --- CHANGED: Use Generic 'event_id' to allow searching ALL events ---
                {
                    "type": "input",
                    "block_id": "event_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "이벤트 선택 (이름 검색)"},
                    "element": {
                        "type": "external_select",
                        "action_id": "event_id", # Using generic search
                        "placeholder": {"type": "plain_text", "text": "검색어 입력"},
                        "min_query_length": 1
                    }
                },
                {
                    "type": "input",
                    "block_id": "cat_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "카테고리 선택"},
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


@bolt_app.action("conversations_select")
def handle_channel_selection(ack, body, client):
    ack()
    current_view = body["view"]
    selected_channel = body["actions"][0]["selected_conversation"]
    
    # Rebuild blocks to inject 'initial_conversation'
    new_blocks = []
    for block in current_view["blocks"]:
        if block.get("block_id") == "target_user":
            # Force the choice into the block definition
            block["accessory"]["initial_conversation"] = selected_channel
        new_blocks.append(block)
    
    client.views_update(
        view_id=current_view["id"],
        hash=current_view["hash"],
        view={
            "type": "modal",
            "callback_id": "submit_admin_register",
            "private_metadata": current_view.get("private_metadata"),
            "title": current_view["title"],
            "submit": current_view["submit"],
            "blocks": new_blocks
        }
    )

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
    user_id = body["user"]["id"]
    
    with flask_app.app_context():
        if not is_user_admin(user_id):
            client.chat_postEphemeral(channel=user_id, user=user_id, text="🚫 관리자 권한이 없습니다.")
            return

    # Exact duplicate of the modal above
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "submit_admin_register",
            "private_metadata": user_id,
            "title": {"type": "plain_text", "text": "채널 등록 (다중)"},
            "submit": {"type": "plain_text", "text": "등록하기"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "등록 완료 처리할 채널을 선택하세요.\n(미구독 채널은 *자동으로 구독 및 등록*됩니다)"}
                },
                {
                    "type": "input",
                    "block_id": "target_user",
                    "label": {"type": "plain_text", "text": "채널 선택"},
                    "element": {
                        "type": "multi_conversations_select", 
                        "action_id": "conversations_select",
                        "placeholder": {"type": "plain_text", "text": "채널 검색 (비공개 포함)"},
                        "filter": {
                            "include": ["public", "private"], 
                            "exclude_bot_users": True 
                        }
                    }
                },
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
                {
                    "type": "input",
                    "block_id": "event_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "이벤트 선택 (이름 검색)"},
                    "element": {
                        "type": "external_select",
                        "action_id": "event_id", # Generic search
                        "placeholder": {"type": "plain_text", "text": "검색어 입력"},
                        "min_query_length": 1
                    }
                },
                {
                    "type": "input",
                    "block_id": "cat_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "카테고리 선택"},
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

    # Check Admin
    with flask_app.app_context():
        if not is_user_admin(user_id):
            client.chat_postEphemeral(channel=user_id, user=user_id, text="🚫 관리자 권한이 없습니다.")
            return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "submit_admin_sub",
            "private_metadata": user_id,
            "title": {"type": "plain_text", "text": "채널 구독 (다중)"},
            "submit": {"type": "plain_text", "text": "구독하기"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "구독시킬 채널들을 선택하세요."}
                },
                # --- MULTI CHANNEL SELECT ---
                {
                    "type": "input",
                    "block_id": "target_user",
                    "label": {"type": "plain_text", "text": "채널 선택"},
                    "element": {
                        "type": "multi_conversations_select", 
                        "action_id": "conversations_select",
                        "placeholder": {"type": "plain_text", "text": "채널 검색 (비공개 포함)"},
                        "filter": {
                            # 1. Only show Public and Private channels
                            "include": ["public", "private"], 
                            # 2. explicit safety to hide bots (though 'im' exclusion does this mostly)
                            "exclude_bot_users": True 
                        }
                    }
                },
                # ----------------------------
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
                {
                    "type": "input",
                    "block_id": "cat_select",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "카테고리 선택"},
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
    
    # 1. FIX: Get the CHANNEL ID, not the USER ID
    # Since the subscription is tied to the channel
    target_channel_id = body["channel"]["id"]
    event_id = int(body["actions"][0]["value"])
    
    with flask_app.app_context():
        # Look up by channel_id
        sub = Subscription.query.filter_by(channel_id=target_channel_id, event_id=event_id).first()
        
        # DEBUG: Print to logs if not found
        if not sub:
            print(f"DEBUG: No sub found for channel {target_channel_id} and event {event_id}")
            return

        if sub.status == "Pending":
            sub.status = "Registered"
            
            # Fetch event details
            event = Event.query.get(event_id)
            
            # 2. Update UI: Remove the button so they can't click it again
            # We pull the text from the first block if it exists
            try:
                blocks = body.get("message", {}).get("blocks", [])
                original_text = blocks[0]["text"]["text"] if blocks else "알림 메시지"
                
                client.chat_update(
                    channel=target_channel_id,
                    ts=body["message"]["ts"],
                    text="✅ 등록 확인 완료",
                    blocks=[
                        {"type": "section", "text": {"type": "mrkdwn", "text": original_text}},
                        {"type": "context", "elements": [{"type": "mrkdwn", "text": "✅ *등록 확인 완료*"}]}
                    ]
                )
            except Exception as e:
                print(f"UI Update Error: {e}")

            # 3. SUCCESS FEED
            config = AppConfig.query.get("consultant_channel")
            
            # Notify the channel where the button was clicked
            client.chat_postMessage(
                channel=target_channel_id,
                text=f"🎉 *{event.title}* 신청과 등록을 완료했습니다!"
            )
            
            # Notify the Consultants
            if config:
                client.chat_postMessage(
                    channel=config.value,
                    text=f"🎉 *등록 확인:* <#{target_channel_id}> 채널이 *{event.title}* 신청과 등록을 완료했습니다!"
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

@bolt_app.view("submit_nothing")
def submit_nothing(ack, body, view, client):
    ack()

@bolt_app.view("submit_admin_sub")
def handle_admin_sub_submission(ack, body, view, client):
    ack()
    
    values = view["state"]["values"]
    
    # 1. Get List of Channels
    target_channels = values["target_user"]["conversations_select"]["selected_conversations"]
    
    if not target_channels:
        return
        
    mode = values["sub_type"]["mode_select"]["selected_option"]["value"]
    admin_id = body["user"]["id"]
    
    # Track stats and names for the report
    success_count = 0
    total_targets = len(target_channels)
    success_list = []  # List to store formatted channel names (e.g. <#C123>)
    
    with flask_app.app_context():
        # A. Prepare Event Data
        events_to_subscribe = []
        mode_label = ""

        if mode == "item":
            selected_option = values["event_select"]["event_id"]["selected_option"]
            if not selected_option:
                client.chat_postEphemeral(channel=admin_id, user=admin_id, text="⚠️ 이벤트를 선택해야 합니다.")
                return
            event = Event.query.get(int(selected_option["value"]))
            events_to_subscribe = [event]
            mode_label = f"*{event.title}* 이벤트"

        elif mode == "cat":
            selected_cat = values["cat_select"]["cat_name"]["selected_option"]
            if not selected_cat:
                client.chat_postEphemeral(channel=admin_id, user=admin_id, text="⚠️ 카테고리를 선택해야 합니다.")
                return
            cat_name = selected_cat["value"]
            events_to_subscribe = Event.query.filter_by(event_type=cat_name)\
                                       .filter(Event.registration_deadline >= datetime.now().date())\
                                       .all()
            mode_label = f"카테고리 *{cat_name}*"

        elif mode == "all":
            events_to_subscribe = Event.query.filter(Event.registration_deadline >= datetime.now().date()).all()
            mode_label = "*모든 이벤트*"

        # B. Loop through Each Selected Channel
        for target_channel in target_channels:
            channel_subscribed_count = 0
            
            for event in events_to_subscribe:
                if not Subscription.query.filter_by(channel_id=target_channel, event_id=event.id).first():
                    db.session.add(Subscription(channel_id=target_channel, event_id=event.id, status='Pending'))
                    channel_subscribed_count += 1
            
            # If at least one new subscription was added
            if channel_subscribed_count > 0:
                success_count += 1
                success_list.append(f"<#{target_channel}>") # Add to summary list
                
                # Send DM to the Target Channel
                try:
                    msg_text = f"⏳ 관리자가 이 채널을 {mode_label} 알림 목록에 추가했습니다."
                    if mode == "item":
                         msg_text = f"⏳ 관리자가 이 채널을 *{events_to_subscribe[0].title}* 알림 목록에 추가했습니다."
                    
                    client.chat_postMessage(channel=target_channel, text=msg_text)
                except Exception as e:
                    print(f"Failed to notify {target_channel}: {e}")

        # C. Send Summary to Consultant Channel (Updated Format)
        if success_list:
            config = AppConfig.query.get("consultant_channel")
            if config:
                consultant_channel_id = config.value
                
                # Format: "<#C123>, <#C456> 님이 ..."
                channel_list_str = ", ".join(success_list)
                
                # New concise message format
                consultant_msg = f"⏳ {channel_list_str} 님이 {mode_label}\u200b에 구독되었습니다."
                
                try:
                    client.chat_postMessage(channel=consultant_channel_id, text=consultant_msg)
                except Exception as e:
                    print(f"Failed to notify consultant channel: {e}")

        db.session.commit()
    
    # D. Final Ephemeral Report to Admin
    client.chat_postEphemeral(
        channel=admin_id, 
        user=admin_id, 
        text=f"⏳ 작업 완료: 총 {total_targets}개 채널 중 {success_count}곳에 {mode_label} 구독을 적용했습니다."
    )

from datetime import datetime

@bolt_app.view("submit_admin_register")
def handle_admin_register_submission(ack, body, view, client):
    ack()
    
    values = view["state"]["values"]
    
    # 1. Get List of Channels (from multi_conversations_select)
    target_channels = values["target_user"]["conversations_select"]["selected_conversations"]
    
    if not target_channels:
        return
        
    mode = values["sub_type"]["mode_select"]["selected_option"]["value"]
    admin_id = body["user"]["id"]
    
    success_count = 0
    total_targets = len(target_channels)
    success_list = [] # For Consultant Report
    
    with flask_app.app_context():
        # A. Prepare Event Data
        events_to_register = []
        mode_label = ""

        if mode == "item":
            # Note: We use "event_id" now (generic search), not "event_subscribed"
            selected_option = values["event_select"]["event_id"]["selected_option"]
            if not selected_option:
                client.chat_postEphemeral(channel=admin_id, user=admin_id, text="⚠️ 이벤트를 선택해야 합니다.")
                return
            event = Event.query.get(int(selected_option["value"]))
            events_to_register = [event]
            mode_label = f"*{event.title}*"

        elif mode == "cat":
            selected_cat = values["cat_select"]["cat_name"]["selected_option"]
            if not selected_cat:
                client.chat_postEphemeral(channel=admin_id, user=admin_id, text="⚠️ 카테고리를 선택해야 합니다.")
                return
            cat_name = selected_cat["value"]
            events_to_register = Event.query.filter_by(event_type=cat_name)\
                                       .filter(Event.registration_deadline >= datetime.now().date())\
                                       .all()
            mode_label = f"카테고리 *{cat_name}*"

        elif mode == "all":
            events_to_register = Event.query.filter(Event.registration_deadline >= datetime.now().date()).all()
            mode_label = "*모든 이벤트*"

        # B. Loop through Channels
        for target_channel in target_channels:
            channel_updated = False
            
            for event in events_to_register:
                # Find existing subscription
                sub = Subscription.query.filter_by(channel_id=target_channel, event_id=event.id).first()
                
                if sub:
                    # If exists, just update status
                    if sub.status != 'Registered':
                        sub.status = 'Registered'
                        channel_updated = True
                else:
                    # If NOT exists, Create New (Auto-Subscribe + Register)
                    db.session.add(Subscription(channel_id=target_channel, event_id=event.id, status='Registered'))
                    channel_updated = True
            
            # Notify Channel
            if channel_updated:
                success_count += 1
                success_list.append(f"<#{target_channel}>")
                try:
                    msg_text = f"✅ 관리자가 이 채널을 {mode_label}에 등록 완료시켰습니다."
                    if mode == "item":
                         msg_text = f"✅ 관리자가 이 채널을 *{events_to_register[0].title}*에 등록 완료시켰습니다."
                    client.chat_postMessage(channel=target_channel, text=msg_text)
                except Exception as e:
                    print(f"Failed to notify {target_channel}: {e}")

        # C. Send Summary to Consultant Channel
        if success_list:
            config = AppConfig.query.get("consultant_channel")
            if config:
                consultant_channel_id = config.value
                channel_list_str = ", ".join(success_list)
                
                # Format: #Channel1, #Channel2 님이 [Event]에 등록되었습니다.
                consultant_msg = f"✅ {channel_list_str} 님이 {mode_label}에 등록 완료되었습니다."
                
                try:
                    client.chat_postMessage(channel=consultant_channel_id, text=consultant_msg)
                except Exception as e:
                    print(f"Failed to notify consultant channel: {e}")

        db.session.commit()

    # D. Final Ephemeral Report
    client.chat_postEphemeral(
        channel=admin_id, 
        user=admin_id, 
        text=f"✅ 작업 완료: 총 {total_targets}개 채널 중 {success_count}곳에 {mode_label} 등록을 적용했습니다."
    )

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
            Event.event_date >= datetime.now().date()
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
            Event.event_date >= datetime.now().date()
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


import json

@bolt_app.options("event_subscribed")
def handle_admin_event_subscribed_search(ack, body):
    # DEBUG: Full body print as requested
    print(f"DEBUG: Full Options Body: {json.dumps(body, indent=2)}")
    
    view = body.get("view", {})
    state_values = view.get("state", {}).get("values", {})
    
    # Path 1: Check standard state
    channel_id = state_values.get("target_user", {}).get("conversations_select", {}).get("selected_conversation")
    
    # Path 2: Check block definition (Initial Conversation Injection)
    if not channel_id:
        for block in view.get("blocks", []):
            if block.get("block_id") == "target_user":
                channel_id = block.get("accessory", {}).get("initial_conversation")
                break

    # Path 3: Check action (sometimes present in options payload)
    if not channel_id:
        actions = body.get("actions", [])
        if actions:
            channel_id = actions[0].get("selected_conversation")

    if not channel_id:
        return ack(options=[{"text": {"type": "plain_text", "text": "⚠️ 채널을 먼저 선택하세요"}, "value": "none"}])

    try:
        with flask_app.app_context():
            results = db.session.query(Subscription, Event)\
                .join(Event, Subscription.event_id == Event.id)\
                .filter(Subscription.channel_id == channel_id, Subscription.status == 'Pending')\
                .all()

            options = []
            for sub, event in results:
                date_str = event.event_date.strftime('%Y-%m-%d')
                title = (event.title[:50] + '..') if len(event.title) > 50 else event.title
                options.append({
                    "text": {"type": "plain_text", "text": f"{title} ({date_str})"},
                    "value": str(event.id)
                })

            if not options:
                return ack(options=[{"text": {"type": "plain_text", "text": "신청건 없음"}, "value": "none"}])
            
            ack(options=options)
    except Exception as e:
        print(f"DB Error: {e}")
        ack(options=[])

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
    # --- 1. Security Check ---
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
    
    # --- 2. Process Reminders ---
    try:
        today = datetime.now().date()
        total_sent = 0
        
        # Query active events
        data = db.session.query(Subscription, Event)\
            .join(Event, Subscription.event_id == Event.id)\
            .filter(Event.registration_deadline >= today).all()
        channel_intervals = db.session.query(ChannelNotification).all()
        channel_interval_map = {cn.channel_id: cn for cn in channel_intervals}
        channel_successful_map = defaultdict(set)
        # Group by channel_id
        events_by_user = defaultdict(list)
        for sub, event in data:
            events_by_user[sub.channel_id].append((event, sub))
        
        # Iterate through each user
        for channel_id, items in events_by_user.items():
            if not items: continue
            if channel_interval_map.get(channel_id):
                last_notif = channel_interval_map[channel_id].last_interval
                interval_days = (today - last_notif).days
                required_interval = channel_interval_map[channel_id].interval
                if interval_days < required_interval:
                    continue
            user_slack_id = items[0][1].channel_id
            
            # We will build the blocks list directly now
            blocks = []

            # -- Header --
            blocks.append({
                "type": "header",
                "text": {"type": "plain_text", "text": "👤 학생 분석 보고서"}
            })
            blocks.append({"type": "divider"})
            
            # Lists to track counts for the fallback text
            pending_count = 0
            registered_lines = []

            # -- Dynamic Event Loop --
            # We iterate through events and append blocks immediately
            
            # 1. Header for Pending (only if there are pending items)
            # Check if there are any pending items first to render the header
            has_pending = any(sub.status != "Registered" for event, sub in items)
            
            if has_pending:
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "*📅 미등록된 이벤트 (작업 필요):*"}
                })

            for event, sub in items:
                # --- REGISTERED EVENTS (Keep compact) ---
                if sub.status == "Registered":
                    registered_lines.append(f"• ✅ *{event.title}* | 📅 {event.event_date}")
                
                # --- PENDING EVENTS (One block per event with button) ---
                else:
                    pending_count += 1
                    
                    # 1. Build the text detail
                    text_detail = f"*{event.title}*\n📅 날짜: {event.event_date}\n⏳ 상태: 미등록"
                    
                    # 2. Add Warning if close to deadline
                    if event.registration_deadline <= (today + timedelta(days=3)):
                        text_detail += f"\n🚨 *마감* *임박!* *({event.registration_deadline})*"
                    
                    # 3. Create the Section Block with Accessory Button
                    event_block = {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": text_detail
                        },
                        "accessory": {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "✅ 등록하기", # "Register"
                                "emoji": True
                            },
                            "style": "primary", # Green button
                            "value": str(event.id),
                            "action_id": "confirm_registration"
                        }
                    }
                    blocks.append(event_block)
                    # Add a small divider between pending items for cleaner look (optional)
                    blocks.append({"type": "divider"})

            # If no pending events, show a success message
            if not has_pending:
                 blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "*📅 미등록된 이벤트:* 없음 (모두 완료! 🎉)"}
                })

            # -- Registered Section --
            # We keep these grouped to save vertical space, as no action is needed
            if registered_lines:
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "*📜 등록된 이벤트:*\n" + "\n".join(registered_lines)}
                })

            # --- Send DM ---
            try:
                fallback_msg = f"학생 분석 보고서: 미등록 {pending_count}건"
                bolt_app.client.chat_postMessage(
                    channel=user_slack_id, 
                    text=fallback_msg, 
                    blocks=blocks
                )
                total_sent += 1
                channel_successful_map[channel_id].add(user_slack_id)
            except Exception as e:
                logger.error(f"Fail DM {user_slack_id}: {e}")
                channel_successful_map[channel_id].discard(user_slack_id)

        # --- UPSERT LOGIC ---
        for channel_id in channel_successful_map:
            if channel_id in channel_interval_map:
                # 1. UPDATE Case
                # This object is already in the session (from your initial query).
                # Just modifying the attribute marks it as "dirty".
                cn = channel_interval_map[channel_id]
                cn.last_interval = today
            else:
                # 2. INSERT Case
                # This is a new channel not in the DB yet. Create and add.
                cn = ChannelNotification(channel_id=channel_id, last_interval=today, interval=7)
                db.session.add(cn)
        
        # 3. COMMIT
        # SQLAlchemy will batch all the UPDATES and INSERTS into a single transaction here.
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f"User Reminder Database commit failed: {e}")

        # --- 3. Run Consultant Briefing (Existing Logic) ---
        try:
            channel = AppConfig.query.get("consultant_channel")
            interval = AppConfig.query.get("notification_interval")# days integer as string
            last = AppConfig.query.get("notification_last_triggered") #YYYY-MM-DD string
            if interval and interval.value.isdigit():
                interval_days = int(interval.value)
            else:
                interval_days = 1  # Default to 1 day if not set properly
            tosend = True
            if last and last.value:
                try:
                    last_date = datetime.strptime(last.value, "%Y-%m-%d").date()
                    if (today - last_date).days < interval_days:
                        logger.info("Briefing already sent within the configured interval.")
                        tosend = False
                except ValueError:
                    logger.warning("Failed to parse last triggered date.")

            if tosend is True and channel:
                briefing_blocks = generate_morning_briefing(today)
                bolt_app.client.chat_postMessage(
                    channel=channel.value,
                    text="Morning Briefing",
                    blocks=briefing_blocks
                )
                # Update last triggered date
                if last:
                    last.value = today.strftime("%Y-%m-%d")
                else:
                    new_config = AppConfig(key="notification_last_triggered", value=today.strftime("%Y-%m-%d"))
                    db.session.add(new_config)
                db.session.commit()
                print("Briefing sent successfully.")
        except Exception as e:
            logger.error(f"Failed to send briefing: {e}")

        custom_reminders = EventReminder.query.all()
        
        for reminder in custom_reminders:
            try:
                # 1. Calculate the Trigger Date
                event = Event.query.get(reminder.event_id)
                if not event: continue
                
                trigger_date = event.event_date - timedelta(days=reminder.days_before)
                
                # 2. Check if TODAY is the trigger date
                if today == trigger_date:
                    
                    # 3. Find Subscribers (Registered users only? or all? Assuming Registered for now)
                    # If you want ALL subscribers (pending + registered), remove the status filter.
                    subs = Subscription.query.filter_by(event_id=event.id).all()
                    
                    for sub in subs:
                        try:
                            # Build the Message
                            blocks = [
                                {
                                    "type": "header",
                                    "text": {"type": "plain_text", "text": f"⏰ D-{reminder.days_before} 알림: {event.title}"}
                                },
                                {
                                    "type": "section",
                                    "text": {"type": "mrkdwn", "text": reminder.message_template}
                                },
                                {
                                    "type": "context",
                                    "elements": [{"type": "mrkdwn", "text": f"📅 행사일: {event.event_date}"}]
                                }
                            ]
                            bolt_app.client.chat_postMessage(channel=sub.channel_id, text=reminder.message_template, blocks=blocks)
                            total_sent += 1
                        except Exception as inner_e:
                            print(f"Failed custom reminder for {sub.channel_id}: {inner_e}")
                            
            except Exception as e:
                logger.error(f"Error processing reminder ID {reminder.id}: {e}")
        

        tomorrow = today + timedelta(days=1)
        # ====================================================
        # TASK A: Registration Deadline Reminder (Day Before)
        # Target: Students subscribed but NOT yet 'Registered'
        # ====================================================
        deadline_evts = db.session.query(Subscription, Event)\
            .join(Event, Subscription.event_id == Event.id)\
            .filter(Event.registration_deadline == tomorrow)\
            .filter(Subscription.status != 'Registered')\
            .all()

        for sub, event in deadline_evts:
            try:
                msg_text = f"🚨 *마감 임박 알림: {event.title}*"
                blocks = [
                    {
                        "type": "header",
                        "text": {"type": "mrkdwn", "text": f"🚨 *마감 임박! 내일이 등록 마감일입니다.*"}
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn", 
                            "text": f"*{event.title}*\n마감일: {event.registration_deadline}\n\n아직 등록이 완료되지 않았습니다. 참가를 원하시면 서둘러주세요!"
                        },
                        "accessory": {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✅ 지금 등록하기"},
                            "style": "primary",
                            "value": str(event.id),
                            "action_id": "confirm_registration"
                        }
                    }
                ]
                
                bolt_app.client.chat_postMessage(channel=sub.channel_id, text=msg_text, blocks=blocks)
            except Exception as e:
                logger.error(f"Deadline Fail {sub.channel_id}: {str(e)}")

        # ====================================================
        # TASK B: Event Day Reminder (Day Before Event)
        # Target: Students who ARE 'Registered'
        # ====================================================
        upcoming_evts = db.session.query(Subscription, Event)\
            .join(Event, Subscription.event_id == Event.id)\
            .filter(Event.event_date == tomorrow)\
            .filter(Subscription.status == 'Registered')\
            .all()

        for sub, event in upcoming_evts:
            try:
                msg_text = f"📅 *D-1 알림: {event.title}*"
                blocks = [
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": "📅 내일 만나요!"}
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn", 
                            "text": f"내일은 *{event.title}* 이벤트가 있는 날입니다.\n잊지 말고 참석해주세요! 🙌"
                        }
                    },
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": f"🗓 일시: {event.event_date}"}]
                    }
                ]

                bolt_app.client.chat_postMessage(channel=sub.channel_id, text=msg_text, blocks=blocks)
            except Exception as e:
                logger.error(f"Event Fail {sub.channel_id}: {str(e)}")

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
            total = Subscription.query.filter_by(event_id=e.id).all()
            pending = Subscription.query.filter_by(event_id=e.id, status="Pending").all()
            registered = len(total) - len(pending)
            
            # Status Logic: Green if all registered, Yellow if <3 pending, Red otherwise
            status_icon = "🟢" if len(pending) == 0 else "🟡" if len(pending) < 3 else "🔴"
            date_pretty = e.event_date.strftime('%m/%d')
            if len(total) == 0:
                text_lines += f"ℹ️ *{date_pretty}:* {e.title} (구독자 없음)\n"
            else:
                text_lines += f"{status_icon} *{date_pretty}:* {e.title} ({len(total)}명 중 {registered}명 완료)\n"
                registered_students = [f"<#{sub.channel_id}>" for sub in total]
                pending_students = [f"<#{sub.channel_id}>" for sub in pending]
                text_lines += f"    • 등록 완료: {', '.join(registered_students) if registered_students else '없음'}\n"
                text_lines += f"    • 미등록: {', '.join(pending_students) if pending_students else '없음'}\n"
        
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": text_lines}
        })
    else:
         blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "이번 주 예정된 이벤트가 없습니다."}]})

    return blocks

if __name__ == "__main__":
    flask_app.run(port=3000)