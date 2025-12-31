"""
Monetized Finance Bot - Full Version
Bot handles Telegram interactions only
AI parsing and spreadsheet operations handled by user's Google Apps Script

Features:
- User authorization via admin spreadsheet (with subscription expiry)
- Email transaction processing (patterns protected on server)
- Text/voice transaction parsing (via user's GAS + Gemini)
- Admin commands for user management
"""

import os
import logging
import hmac
import hashlib
import json
import requests
from datetime import datetime
from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request, jsonify
import threading
import pytz

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your-webhook-secret")
ADMIN_GAS_URL = os.getenv("ADMIN_GAS_URL")  # Admin spreadsheet GAS URL
EMAIL_PARSER_SECRET = os.getenv("EMAIL_PARSER_SECRET", "your-secret-key")
ADMIN_USER_IDS = [uid.strip() for uid in os.getenv("ADMIN_USER_IDS", "").split(",") if uid.strip()]

# Flask app for webhooks
app = Flask(__name__)

# Initialize bot
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# User context storage (in-memory)
user_context = {}

# User GAS webhook URLs cache
user_gas_webhooks = {}


# ============================================
# HELPER FUNCTIONS
# ============================================

def generate_api_key(user_id):
    """Generate API key for user's GAS to call email parser"""
    return hmac.new(
        EMAIL_PARSER_SECRET.encode(),
        str(user_id).encode(),
        hashlib.sha256
    ).hexdigest()[:32]


def verify_webhook_signature(payload, signature):
    """Verify webhook request from user's GAS"""
    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def check_user_authorized(user_id):
    """Check if user is authorized via admin spreadsheet"""
    try:
        if not ADMIN_GAS_URL:
            logger.warning("ADMIN_GAS_URL not set")
            return False
        
        response = requests.get(
            f"{ADMIN_GAS_URL}?action=check_auth&userId={user_id}",
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            return data.get('authorized', False)
        
        return False
        
    except Exception as e:
        logger.error(f"Error checking authorization: {e}")
        return False


def get_user_gas_webhook(user_id):
    """Get user's GAS webhook URL from admin spreadsheet"""
    try:
        # Check cache first
        if user_id in user_gas_webhooks:
            return user_gas_webhooks[user_id]
        
        if not ADMIN_GAS_URL:
            return None
        
        response = requests.get(
            f"{ADMIN_GAS_URL}?action=get_webhook&userId={user_id}",
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            webhook_url = data.get('webhookUrl')
            if webhook_url:
                user_gas_webhooks[user_id] = webhook_url
            return webhook_url
        
        return None
        
    except Exception as e:
        logger.error(f"Error getting user webhook: {e}")
        return None


def update_user_gas_webhook(user_id, webhook_url):
    """Update user's GAS webhook URL in admin spreadsheet"""
    try:
        if not ADMIN_GAS_URL:
            return False
        
        response = requests.post(
            ADMIN_GAS_URL,
            json={
                "action": "update_webhook",
                "userId": str(user_id),
                "webhookUrl": webhook_url
            },
            timeout=10
        )
        
        if response.status_code == 200:
            user_gas_webhooks[user_id] = webhook_url
            return True
        
        return False
        
    except Exception as e:
        logger.error(f"Error updating user webhook: {e}")
        return False


def call_user_gas(user_id, action, data=None):
    """Call user's GAS for AI parsing or saving transactions"""
    try:
        webhook_url = get_user_gas_webhook(user_id)
        if not webhook_url:
            return None
        
        payload = {"action": action}
        if data:
            payload.update(data)
        
        response = requests.post(webhook_url, json=payload, timeout=30)
        
        if response.status_code == 200:
            return response.json()
        
        return None
        
    except Exception as e:
        logger.error(f"Error calling user GAS: {e}")
        return None


def format_rupiah(amount):
    """Format number as Indonesian Rupiah"""
    try:
        amount = abs(float(amount))
        return f"Rp {amount:,.0f}".replace(",", ".")
    except:
        return f"Rp {amount}"


def format_date(date_str):
    """Format date to more readable format"""
    try:
        date_obj = datetime.strptime(date_str, "%d/%m/%Y %H:%M:%S")
        month_name = date_obj.strftime("%b")
        return date_obj.strftime(f"%d {month_name} %Y %H:%M:%S")
    except (ValueError, TypeError):
        return date_str


def generate_cashew_link(transaction_data):
    """Generate link for Cashew app"""
    from urllib.parse import urlencode, quote
    
    params = {k: v for k, v in transaction_data.items() 
              if k in ['amount', 'title', 'category', 'subcategory', 'account', 'notes'] and v}
    
    if 'date' in transaction_data and transaction_data['date']:
        try:
            date_obj = datetime.strptime(transaction_data['date'], "%d/%m/%Y %H:%M:%S")
            params['date'] = date_obj.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            params['date'] = transaction_data['date']
    
    return f"https://cashewapp.web.app/addTransaction?{urlencode(params)}"


# ============================================
# FLASK WEBHOOK ENDPOINTS
# ============================================

@app.route('/webhook/transaction', methods=['POST'])
def receive_transaction():
    """
    Receive transaction from user's Google Apps Script
    
    Expected payload:
    {
        "user_id": "123456",
        "signature": "hmac_signature",
        "transaction": { ... }
    }
    """
    try:
        data = request.get_json()
        
        user_id = str(data.get('user_id'))
        signature = data.get('signature', '')
        transaction = data.get('transaction')
        
        # Verify signature
        payload = json.dumps(transaction)
        if not verify_webhook_signature(payload, signature):
            logger.warning(f"Invalid signature from user {user_id}")
            return jsonify({"error": "Invalid signature"}), 403
        
        if not user_id or not transaction:
            return jsonify({"error": "Missing user_id or transaction"}), 400
        
        # Check if user is authorized
        if not check_user_authorized(user_id):
            logger.warning(f"Unauthorized user {user_id}")
            return jsonify({"error": "Unauthorized"}), 403
        
        # Send transaction to user via Telegram
        send_email_transaction_to_user(user_id, transaction)
        
        return jsonify({"success": True}), 200
        
    except Exception as e:
        logger.error(f"Error receiving transaction: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/parse-email', methods=['POST'])
def parse_email_endpoint():
    """
    Email parsing endpoint - called by user's GAS
    Protected extraction logic
    """
    try:
        data = request.get_json()
        
        user_id = str(data.get('user_id', ''))
        api_key = data.get('api_key', '')
        email_data = data.get('email', {})
        
        # Verify API key
        expected_key = generate_api_key(user_id)
        if api_key != expected_key:
            return jsonify({"success": False, "error": "Invalid API key"}), 401
        
        # Check user authorization
        if not check_user_authorized(user_id):
            return jsonify({"success": False, "error": "Unauthorized"}), 403
        
        # Extract transaction from email
        result = extract_transaction_from_email(email_data)
        
        if result:
            return jsonify({"success": True, "transaction": result}), 200
        else:
            return jsonify({"success": False, "error": "Could not extract transaction"}), 200
            
    except Exception as e:
        logger.error(f"Error parsing email: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/webhook/register', methods=['POST'])
def register_user_gas():
    """Register user's GAS webhook URL"""
    try:
        data = request.get_json()
        
        user_id = str(data.get('user_id'))
        gas_webhook_url = data.get('gas_webhook_url')
        
        if not user_id or not gas_webhook_url:
            return jsonify({"error": "Missing data"}), 400
        
        success = update_user_gas_webhook(user_id, gas_webhook_url)
        
        if success:
            return jsonify({"success": True}), 200
        else:
            return jsonify({"error": "Failed to register"}), 500
        
    except Exception as e:
        logger.error(f"Error registering webhook: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()}), 200


# ============================================
# EMAIL EXTRACTION (Protected Logic)
# ============================================

def extract_transaction_from_email(email_data):
    """
    Extract transaction data from email
    This is the PROTECTED parsing logic
    """
    import re
    
    sender = email_data.get('sender', '').lower()
    subject = email_data.get('subject', '')
    body = email_data.get('body', '')
    date = email_data.get('date', '')
    time = email_data.get('time', '')
    
    # Shopee - Delivery Confirmation
    if 'shopee' in sender:
        if 'telah dikirim' in subject.lower():
            return extract_shopee_delivery(body, subject, date, time)
    
    # Aladin - Deposito Return
    elif 'aladin' in sender:
        if 'deposito' in subject.lower() and 'diperpanjang' in subject.lower():
            return extract_aladin_deposito(body, subject, date, time)
    
    # Tokopedia (future)
    elif 'tokopedia' in sender:
        return extract_tokopedia_order(body, subject, date, time)
    
    # Gojek (future)
    elif 'gojek' in sender:
        return extract_gojek_transaction(body, subject, date, time)
    
    return None


def extract_shopee_delivery(body, subject, date, time):
    """Extract Shopee delivery confirmation"""
    import re
    
    try:
        data = {
            'merchant_type': 'shopee',
            'is_income': False,
            'account': 'SeaBank',
            'category': 'Shopee',
            'hashtag': '#email'
        }
        
        # Extract product title from numbered list
        title_patterns = [
            r'1\.\s(.+?)(?:\n|Variasi:|$)',
            r'(?:^|\n)1\.\s(.+?)(?:\n|Variasi:|$)',
        ]
        
        for pattern in title_patterns:
            match = re.search(pattern, body, re.MULTILINE)
            if match:
                title = match.group(1).strip()
                title = re.sub(r'<[^>]*>', '', title)
                title = re.sub(r'https?://[^\s]+', '', title)
                title = ' '.join(title.split())
                if len(title) > 40:
                    words = title.split()
                    truncated = ''
                    for word in words:
                        if len(truncated + ' ' + word) <= 40:
                            truncated += (' ' if truncated else '') + word
                        else:
                            break
                    title = truncated or title[:37] + '...'
                data['title'] = title
                break
        
        # Extract amount
        amount_match = re.search(r'Total Pembayaran:\s*Rp\s?([\d.,]+)', body)
        if amount_match:
            amount_str = amount_match.group(1)
            # Indonesian format: "60.471,23" -> 60471
            if ',' in amount_str:
                amount_str = amount_str.replace('.', '').split(',')[0]
            else:
                amount_str = amount_str.replace('.', '')
            data['amount'] = int(amount_str)
        
        # Use email date/time
        data['date'] = date
        data['time'] = time
        
        # Extract order number
        order_match = re.search(r'Pesanan\s(#[A-Z0-9]+)', subject)
        if order_match:
            data['order_number'] = order_match.group(1)
        
        if not data.get('title') or not data.get('amount'):
            return None
        
        return data
        
    except Exception as e:
        logger.error(f"Error extracting Shopee data: {e}")
        return None


def extract_aladin_deposito(body, subject, date, time):
    """Extract Aladin deposito return"""
    import re
    
    try:
        data = {
            'merchant_type': 'aladin',
            'is_income': True,
            'account': 'Ala Dompet (Aladin)',
            'title': 'Bagi Hasil Deposito',
            'category': 'Return',
            'subcategory': 'Deposito',
            'hashtag': '#email'
        }
        
        # Extract amount
        amount_patterns = [
            r'Total Bagi Hasil\s*Rp\s?([\d.,]+)',
            r'Bagi Hasil[:\s]*Rp\s?([\d.,]+)',
        ]
        
        for pattern in amount_patterns:
            match = re.search(pattern, body)
            if match:
                amount_str = match.group(1)
                # Indonesian format: "60.471,23" -> 60471
                if ',' in amount_str:
                    amount_str = amount_str.replace('.', '').split(',')[0]
                else:
                    amount_str = amount_str.replace('.', '')
                data['amount'] = int(amount_str)
                break
        
        # Use email date/time
        data['date'] = date
        data['time'] = time
        
        if not data.get('amount'):
            return None
        
        return data
        
    except Exception as e:
        logger.error(f"Error extracting Aladin data: {e}")
        return None


def extract_tokopedia_order(body, subject, date, time):
    """Extract Tokopedia order (placeholder)"""
    # TODO: Implement Tokopedia parsing
    return None


def extract_gojek_transaction(body, subject, date, time):
    """Extract Gojek transaction (placeholder)"""
    # TODO: Implement Gojek parsing
    return None


# ============================================
# TELEGRAM MESSAGE SENDING
# ============================================

def send_email_transaction_to_user(user_id, transaction):
    """Send email transaction notification to user via Telegram"""
    try:
        # Format transaction message
        is_income = transaction.get('is_income', False)
        amount = transaction.get('amount', 0)
        title = transaction.get('title', 'Unknown')
        account = transaction.get('account', 'Unknown')
        category = transaction.get('category', '')
        subcategory = transaction.get('subcategory', '')
        hashtag = transaction.get('hashtag', '#email')
        date = transaction.get('date', '')
        time_str = transaction.get('time', '')
        
        # Format amount
        formatted_amount = format_rupiah(amount)
        amount_display = f"+{formatted_amount}" if is_income else f"-{formatted_amount}"
        
        # Format date/time
        if date and time_str:
            datetime_str = f"{date} {time_str}"
        else:
            jakarta_tz = pytz.timezone('Asia/Jakarta')
            datetime_str = datetime.now(jakarta_tz).strftime("%d/%m/%Y %H:%M:%S")
        
        # Build message
        message = f"📧 *Email Transaction Detected*\n\n"
        message += f"*Title:* {title}\n"
        message += f"*Amount:* {amount_display}\n"
        message += f"*Account:* {account}\n"
        
        if category:
            message += f"*Category:* {category}\n"
        if subcategory:
            message += f"*Subcategory:* {subcategory}\n"
        
        message += f"*Date:* {format_date(datetime_str)}\n"
        message += f"\n{hashtag}"
        
        # Generate Cashew link
        transaction_data = {
            'amount': str(-amount) if not is_income else str(amount),
            'title': title,
            'account': account,
            'category': category,
            'subcategory': subcategory,
            'date': datetime_str,
            'income': 'TRUE' if is_income else 'FALSE'
        }
        cashew_link = generate_cashew_link(transaction_data)
        
        # Create keyboard with Cashew button
        keyboard = telebot.types.InlineKeyboardMarkup()
        keyboard.add(
            telebot.types.InlineKeyboardButton("📱 Open in Cashew", url=cashew_link)
        )
        
        # Send message
        bot.send_message(
            chat_id=int(user_id),
            text=message,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        
        logger.info(f"Email transaction sent to user {user_id}: {title}")
        
    except Exception as e:
        logger.error(f"Error sending email transaction to user: {e}")


# ============================================
# TELEGRAM BOT HANDLERS
# ============================================

def is_admin(user_id):
    """Check if user is admin"""
    return str(user_id) in ADMIN_USER_IDS


@bot.message_handler(commands=['start'])
def start_command(message):
    """Handle /start command"""
    user_id = str(message.from_user.id)
    
    # Check authorization
    if not check_user_authorized(user_id):
        bot.reply_to(message, 
            "❌ *Unauthorized*\n\n"
            "You are not registered to use this bot.\n"
            "Please contact the admin to get access.",
            parse_mode="Markdown"
        )
        return
    
    bot.reply_to(message, 
        "🎉 *Welcome to Finance Tracker Bot!*\n\n"
        "How to use:\n"
        "1. Send transaction text: 'beli kebab 10k cash'\n"
        "2. Send receipt photo for auto-processing\n"
        "3. Send voice message with transaction\n\n"
        "Commands:\n"
        "/help - Show help\n"
        "/status - Check your subscription\n"
        "/setup - Setup your Google Apps Script",
        parse_mode="Markdown"
    )


@bot.message_handler(commands=['help'])
def help_command(message):
    """Handle /help command"""
    user_id = str(message.from_user.id)
    
    if not check_user_authorized(user_id):
        bot.reply_to(message, "❌ Unauthorized")
        return
    
    help_text = """
📖 *Finance Tracker Bot Help*

*Text Input:*
- 'beli kebab 10k cash'
- 'gajian 4jt mandiri'
- 'transfer BCA ke Cash 400rb'

*Voice Input:*
Send voice message with same format

*Photo Input:*
Send receipt photo for auto-processing

*Commands:*
/status - Check subscription status
/setup - Setup instructions
/mykey - Get your API key
"""
    
    if is_admin(message.from_user.id):
        help_text += """
*Admin Commands:*
/adduser [user_id] [username] [days] - Add user
/removeuser [user_id] - Remove user
/extenduser [user_id] [days] - Extend subscription
/listusers - List all users
"""
    
    bot.reply_to(message, help_text, parse_mode="Markdown")


@bot.message_handler(commands=['status'])
def status_command(message):
    """Check user subscription status"""
    user_id = str(message.from_user.id)
    
    try:
        if not ADMIN_GAS_URL:
            bot.reply_to(message, "❌ Admin system not configured")
            return
        
        response = requests.get(
            f"{ADMIN_GAS_URL}?action=get_user_info&userId={user_id}",
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get('user'):
                user = data['user']
                status_emoji = "✅" if user.get('status') == 'Active' else "❌"
                
                bot.reply_to(message,
                    f"📊 *Your Subscription Status*\n\n"
                    f"*Status:* {status_emoji} {user.get('status', 'Unknown')}\n"
                    f"*Tier:* {user.get('tier', 'Basic')}\n"
                    f"*Expires:* {user.get('expiredDate', 'Unknown')}\n"
                    f"*Registered:* {user.get('registrationDate', 'Unknown')}",
                    parse_mode="Markdown"
                )
            else:
                bot.reply_to(message, "❌ User not found in system")
        else:
            bot.reply_to(message, "❌ Error checking status")
            
    except Exception as e:
        logger.error(f"Error checking status: {e}")
        bot.reply_to(message, "❌ Error checking status")


@bot.message_handler(commands=['setup'])
def setup_command(message):
    """Show setup instructions"""
    user_id = str(message.from_user.id)
    
    if not check_user_authorized(user_id):
        bot.reply_to(message, "❌ Unauthorized")
        return
    
    api_key = generate_api_key(user_id)
    
    bot.reply_to(message,
        f"⚙️ *Setup Instructions*\n\n"
        f"1. Create a Google Spreadsheet\n"
        f"2. Open Apps Script (Extensions > Apps Script)\n"
        f"3. Copy the Code.gs from setup guide\n"
        f"4. Set your Script Properties:\n"
        f"   - `TELEGRAM_USER_ID`: `{user_id}`\n"
        f"   - `API_KEY`: `{api_key}`\n"
        f"   - `GEMINI_API_KEY`: Your Gemini API key\n"
        f"   - `SPREADSHEET_ID`: Your spreadsheet ID\n"
        f"5. Deploy as Web App\n"
        f"6. Run /setwebhook [your_webapp_url]\n\n"
        f"📖 Full guide: See USER_SETUP_GUIDE.md",
        parse_mode="Markdown"
    )


@bot.message_handler(commands=['mykey'])
def mykey_command(message):
    """Get user's API key"""
    user_id = str(message.from_user.id)
    
    if not check_user_authorized(user_id):
        bot.reply_to(message, "❌ Unauthorized")
        return
    
    api_key = generate_api_key(user_id)
    
    bot.reply_to(message,
        f"🔑 *Your API Key*\n\n"
        f"`{api_key}`\n\n"
        f"Use this in your Google Apps Script configuration.",
        parse_mode="Markdown"
    )


@bot.message_handler(commands=['setwebhook'])
def setwebhook_command(message):
    """Set user's GAS webhook URL"""
    user_id = str(message.from_user.id)
    
    if not check_user_authorized(user_id):
        bot.reply_to(message, "❌ Unauthorized")
        return
    
    # Parse webhook URL from message
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, 
            "❌ Please provide your GAS Web App URL\n\n"
            "Usage: /setwebhook https://script.google.com/macros/s/xxx/exec"
        )
        return
    
    webhook_url = parts[1].strip()
    
    # Validate URL
    if not webhook_url.startswith('https://script.google.com/'):
        bot.reply_to(message, "❌ Invalid URL. Must be a Google Apps Script Web App URL.")
        return
    
    # Update webhook
    if update_user_gas_webhook(user_id, webhook_url):
        bot.reply_to(message, 
            "✅ *Webhook URL Updated!*\n\n"
            "Your Google Apps Script is now connected.\n"
            "Email transactions will be processed automatically.",
            parse_mode="Markdown"
        )
    else:
        bot.reply_to(message, "❌ Failed to update webhook URL")


# ============================================
# ADMIN COMMANDS
# ============================================

@bot.message_handler(commands=['adduser'])
def adduser_command(message):
    """Admin: Add new user"""
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only command")
        return
    
    # Parse: /adduser user_id username days
    parts = message.text.split()
    if len(parts) < 3:
        bot.reply_to(message, 
            "Usage: /adduser [user_id] [username] [days]\n"
            "Example: /adduser 123456789 john_doe 30"
        )
        return
    
    user_id = parts[1]
    username = parts[2]
    days = int(parts[3]) if len(parts) > 3 else 30
    
    try:
        response = requests.post(
            ADMIN_GAS_URL,
            json={
                "action": "add_user",
                "userId": user_id,
                "username": username,
                "days": days
            },
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                # Generate API key for new user
                api_key = generate_api_key(user_id)
                bot.reply_to(message, 
                    f"✅ *User Added*\n\n"
                    f"*User ID:* {user_id}\n"
                    f"*Username:* {username}\n"
                    f"*Duration:* {days} days\n"
                    f"*API Key:* `{api_key}`",
                    parse_mode="Markdown"
                )
            else:
                bot.reply_to(message, f"❌ Failed: {data.get('error', 'Unknown error')}")
        else:
            bot.reply_to(message, "❌ Failed to add user")
            
    except Exception as e:
        logger.error(f"Error adding user: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")


@bot.message_handler(commands=['removeuser'])
def removeuser_command(message):
    """Admin: Remove user"""
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only command")
        return
    
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /removeuser [user_id]")
        return
    
    user_id = parts[1]
    
    try:
        response = requests.post(
            ADMIN_GAS_URL,
            json={"action": "remove_user", "userId": user_id},
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                # Clear cache
                user_gas_webhooks.pop(user_id, None)
                bot.reply_to(message, f"✅ User {user_id} removed")
            else:
                bot.reply_to(message, f"❌ Failed: {data.get('error', 'Unknown error')}")
        else:
            bot.reply_to(message, "❌ Failed to remove user")
            
    except Exception as e:
        logger.error(f"Error removing user: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")


@bot.message_handler(commands=['extenduser'])
def extenduser_command(message):
    """Admin: Extend user subscription"""
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only command")
        return
    
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /extenduser [user_id] [days]")
        return
    
    user_id = parts[1]
    days = int(parts[2]) if len(parts) > 2 else 30
    
    try:
        response = requests.post(
            ADMIN_GAS_URL,
            json={"action": "extend_subscription", "userId": user_id, "days": days},
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                bot.reply_to(message, f"✅ User {user_id} extended by {days} days")
            else:
                bot.reply_to(message, f"❌ Failed: {data.get('error', 'Unknown error')}")
        else:
            bot.reply_to(message, "❌ Failed to extend subscription")
            
    except Exception as e:
        logger.error(f"Error extending subscription: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")


@bot.message_handler(commands=['listusers'])
def listusers_command(message):
    """Admin: List all users"""
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only command")
        return
    
    try:
        response = requests.get(
            f"{ADMIN_GAS_URL}?action=list_users",
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            users = data.get('users', [])
            
            if not users:
                bot.reply_to(message, "📋 No users registered")
                return
            
            msg = "📋 *Registered Users*\n\n"
            for user in users[:20]:  # Limit to 20
                status_emoji = "✅" if user.get('status') == 'Active' else "❌"
                msg += f"{status_emoji} `{user.get('userId')}` - {user.get('username', 'N/A')}\n"
            
            if len(users) > 20:
                msg += f"\n... and {len(users) - 20} more"
            
            bot.reply_to(message, msg, parse_mode="Markdown")
        else:
            bot.reply_to(message, "❌ Failed to get users")
            
    except Exception as e:
        logger.error(f"Error listing users: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")


# ============================================
# TEXT MESSAGE HANDLER
# ============================================

@bot.message_handler(content_types=['text'])
def handle_text(message):
    """Handle text messages - parse transactions via user's GAS"""
    if message.text.startswith('/'):
        return
    
    user_id = str(message.from_user.id)
    
    if not check_user_authorized(user_id):
        bot.reply_to(message, "❌ Unauthorized. Contact admin for access.")
        return
    
    text = message.text.strip()
    
    # Check if user has GAS webhook configured
    webhook_url = get_user_gas_webhook(user_id)
    if not webhook_url:
        bot.reply_to(message,
            "⚠️ *Setup Required*\n\n"
            "You haven't configured your Google Apps Script yet.\n"
            "Run /setup for instructions.",
            parse_mode="Markdown"
        )
        return
    
    # Send to user's GAS for parsing with their Gemini API
    bot.send_chat_action(message.chat.id, 'typing')
    
    result = call_user_gas(user_id, 'parse_text', {'text': text})
    
    if result and result.get('success'):
        transaction = result.get('transaction')
        if transaction:
            # Store in context for confirmation
            user_context[user_id] = {'pending_transaction': transaction, 'original_text': text}
            
            # Display confirmation
            display_transaction_confirmation(message.chat.id, transaction, user_id)
        else:
            bot.reply_to(message, "❌ Could not parse transaction. Please try again.")
    else:
        error = result.get('error', 'Unknown error') if result else 'No response from your GAS'
        bot.reply_to(message, f"❌ Error: {error}")


def display_transaction_confirmation(chat_id, transaction, user_id):
    """Display transaction confirmation with buttons"""
    is_income = transaction.get('is_income', False)
    amount = transaction.get('amount', 0)
    title = transaction.get('title', 'Unknown')
    account = transaction.get('account', 'Unknown')
    category = transaction.get('category', '')
    subcategory = transaction.get('subcategory', '')
    
    # Format amount
    formatted_amount = format_rupiah(amount)
    transaction_type = "Income" if is_income else "Expense"
    
    # Build message
    message = f"📝 *Transaction Details*\n\n"
    message += f"*Title:* {title}\n"
    message += f"*Amount:* {formatted_amount}\n"
    message += f"*Type:* {transaction_type}\n"
    message += f"*Account:* {account}\n"
    
    if category:
        message += f"*Category:* {category}\n"
    if subcategory:
        message += f"*Subcategory:* {subcategory}\n"
    
    # Create keyboard
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        telebot.types.InlineKeyboardButton("✅ Confirm", callback_data="confirm_tx"),
        telebot.types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_tx")
    )
    keyboard.add(
        telebot.types.InlineKeyboardButton("🔄 Change Category", callback_data="change_cat"),
        telebot.types.InlineKeyboardButton("🔄 Change Account", callback_data="change_acc")
    )
    
    bot.send_message(chat_id, message, reply_markup=keyboard, parse_mode="Markdown")


# ============================================
# CALLBACK HANDLERS
# ============================================

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    """Handle all callback queries"""
    user_id = str(call.from_user.id)
    data = call.data
    
    if not check_user_authorized(user_id):
        bot.answer_callback_query(call.id, "❌ Unauthorized")
        return
    
    if data == "confirm_tx":
        handle_confirm_transaction(call, user_id)
    elif data == "cancel_tx":
        handle_cancel_transaction(call, user_id)
    elif data == "change_cat":
        handle_change_category(call, user_id)
    elif data == "change_acc":
        handle_change_account(call, user_id)
    elif data.startswith("select_cat:"):
        handle_select_category(call, user_id, data)
    elif data.startswith("select_acc:"):
        handle_select_account(call, user_id, data)
    else:
        bot.answer_callback_query(call.id)


def handle_confirm_transaction(call, user_id):
    """Confirm and save transaction"""
    context = user_context.get(user_id, {})
    transaction = context.get('pending_transaction')
    
    if not transaction:
        bot.answer_callback_query(call.id, "❌ No pending transaction")
        return
    
    # Send to user's GAS to save
    result = call_user_gas(user_id, 'save_transaction', {'transaction': transaction})
    
    if result and result.get('success'):
        # Generate Cashew link
        jakarta_tz = pytz.timezone('Asia/Jakarta')
        now = datetime.now(jakarta_tz).strftime("%d/%m/%Y %H:%M:%S")
        
        transaction_data = {
            'amount': str(transaction.get('amount', 0)),
            'title': transaction.get('title', ''),
            'account': transaction.get('account', ''),
            'category': transaction.get('category', ''),
            'subcategory': transaction.get('subcategory', ''),
            'date': now,
            'income': 'TRUE' if transaction.get('is_income') else 'FALSE'
        }
        cashew_link = generate_cashew_link(transaction_data)
        
        # Update message with Cashew button
        keyboard = telebot.types.InlineKeyboardMarkup()
        keyboard.add(
            telebot.types.InlineKeyboardButton("📱 Open in Cashew", url=cashew_link)
        )
        
        bot.edit_message_text(
            f"✅ *Transaction Saved!*\n\n"
            f"*{transaction.get('title')}* - {format_rupiah(transaction.get('amount', 0))}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        
        # Clear context
        user_context.pop(user_id, None)
    else:
        bot.answer_callback_query(call.id, "❌ Failed to save transaction")


def handle_cancel_transaction(call, user_id):
    """Cancel pending transaction"""
    user_context.pop(user_id, None)
    
    bot.edit_message_text(
        "❌ Transaction cancelled",
        call.message.chat.id,
        call.message.message_id
    )
    bot.answer_callback_query(call.id)


def handle_change_category(call, user_id):
    """Show category selection"""
    # Get categories from user's GAS
    result = call_user_gas(user_id, 'get_categories')
    
    if result and result.get('categories'):
        categories = result['categories']
        
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=3)
        buttons = []
        for cat in list(categories.keys())[:12]:  # Limit to 12
            buttons.append(telebot.types.InlineKeyboardButton(cat, callback_data=f"select_cat:{cat}"))
        
        # Add buttons in rows of 3
        for i in range(0, len(buttons), 3):
            keyboard.row(*buttons[i:i+3])
        
        keyboard.add(telebot.types.InlineKeyboardButton("↩️ Back", callback_data="back_to_confirm"))
        
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=keyboard)
    else:
        bot.answer_callback_query(call.id, "❌ Could not load categories")


def handle_change_account(call, user_id):
    """Show account selection"""
    result = call_user_gas(user_id, 'get_accounts')
    
    if result and result.get('accounts'):
        accounts = result['accounts']
        
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=3)
        buttons = []
        for acc in accounts[:12]:
            buttons.append(telebot.types.InlineKeyboardButton(acc, callback_data=f"select_acc:{acc}"))
        
        for i in range(0, len(buttons), 3):
            keyboard.row(*buttons[i:i+3])
        
        keyboard.add(telebot.types.InlineKeyboardButton("↩️ Back", callback_data="back_to_confirm"))
        
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=keyboard)
    else:
        bot.answer_callback_query(call.id, "❌ Could not load accounts")


def handle_select_category(call, user_id, data):
    """Handle category selection"""
    category = data.replace("select_cat:", "")
    
    context = user_context.get(user_id, {})
    if 'pending_transaction' in context:
        context['pending_transaction']['category'] = category
        user_context[user_id] = context
        
        # Refresh confirmation display
        display_transaction_confirmation(call.message.chat.id, context['pending_transaction'], user_id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
    
    bot.answer_callback_query(call.id, f"Category: {category}")


def handle_select_account(call, user_id, data):
    """Handle account selection"""
    account = data.replace("select_acc:", "")
    
    context = user_context.get(user_id, {})
    if 'pending_transaction' in context:
        context['pending_transaction']['account'] = account
        user_context[user_id] = context
        
        display_transaction_confirmation(call.message.chat.id, context['pending_transaction'], user_id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
    
    bot.answer_callback_query(call.id, f"Account: {account}")


# ============================================
# MAIN ENTRY POINT
# ============================================

def run_flask():
    """Run Flask server for webhooks"""
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)


def main():
    """Main entry point"""
    logger.info("🚀 Starting Monetized Finance Bot...")
    
    # Check required environment variables
    if not TELEGRAM_TOKEN:
        logger.error("❌ TELEGRAM_TOKEN not set")
        return
    
    if not ADMIN_GAS_URL:
        logger.warning("⚠️ ADMIN_GAS_URL not set - user authorization will fail")
    
    # Start Flask in background thread for webhooks
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("✅ Flask webhook server started")
    
    # Start Telegram bot polling
    logger.info("✅ Starting Telegram bot polling...")
    
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        logger.error(f"❌ Bot polling error: {e}")


if __name__ == '__main__':
    main()
