"""
Monetized Finance Bot - Simplified Version
No Gemini API, No Google Spreadsheet access
All AI and data operations handled by user's Google Apps Script
"""

import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import telebot
from flask import Flask, request, jsonify
import hmac
import hashlib

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # Secret for validating requests from user's GAS
AUTHORIZED_USER_IDS = [uid.strip() for uid in os.getenv("AUTHORIZED_USER_ID", "").split(",") if uid.strip()]

# Flask app for receiving webhooks from user's Google Apps Script
app = Flask(__name__)

# User context storage (in-memory, consider Redis for production)
user_context = {}

# User's GAS webhook URLs (stored per user)
user_gas_webhooks = {}  # Format: {user_id: "https://script.google.com/..."}


def verify_webhook_signature(payload, signature):
    """Verify that webhook request came from authorized Google Apps Script"""
    expected_signature = hmac.new(
        WEBHOOK_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_signature, signature)


@app.route('/webhook/transaction', methods=['POST'])
def receive_transaction():
    """
    Receive transaction from user's Google Apps Script
    
    Expected payload:
    {
        "user_id": "123456",
        "signature": "hmac_signature",
        "transaction": {
            "title": "Product Name",
            "amount": 87248,
            "account": "seabank",
            "category": "Shopee",
            "subcategory": "Elektronik",
            "date": "27/08/2025",
            "time": "18:05:00",
            "is_income": false,
            "merchant_type": "shopee"
        }
    }
    """
    try:
        data = request.get_json()
        
        # Verify signature
        signature = data.get('signature', '')
        payload = str(data.get('transaction', {}))
        
        if not verify_webhook_signature(payload, signature):
            logger.warning("Invalid webhook signature")
            return jsonify({"error": "Invalid signature"}), 403
        
        user_id = data.get('user_id')
        transaction = data.get('transaction')
        
        if not user_id or not transaction:
            return jsonify({"error": "Missing user_id or transaction"}), 400
        
        # Send transaction to user via Telegram
        send_transaction_to_user(user_id, transaction)
        
        return jsonify({"success": True, "message": "Transaction sent to user"}), 200
        
    except Exception as e:
        logger.error(f"Error receiving transaction: {e}")
        return jsonify({"error": str(e)}), 500


def send_transaction_to_user(user_id, transaction):
    """Send transaction confirmation to user via Telegram"""
    try:
        bot = telebot.TeleBot(TELEGRAM_TOKEN)
        
        # Format transaction message
        title = transaction.get('title', 'Unknown')
        amount = transaction.get('amount', 0)
        account = transaction.get('account', 'Unknown')
        category = transaction.get('category', '')
        subcategory = transaction.get('subcategory', '')
        date = transaction.get('date', '')
        time = transaction.get('time', '')
        is_income = transaction.get('is_income', False)
        
        # Format amount
        formatted_amount = f"Rp {abs(amount):,.0f}".replace(",", ".")
        amount_display = f"+{formatted_amount}" if is_income else f"-{formatted_amount}"
        
        # Create message
        message = f"""
📧 *Email Transaction Detected*

*Type:* {'Income' if is_income else 'Expense'}
*Title:* {title}
*Amount:* {amount_display}
*Account:* {account}
*Category:* {category}
*Subcategory:* {subcategory}
*Date:* {date} {time}

Confirm to save to your spreadsheet?
"""
        
        # Create keyboard
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            telebot.types.InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_email_tx:{user_id}"),
            telebot.types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_email_tx:{user_id}")
        )
        
        # Store transaction in context
        user_context[user_id] = {
            "pending_email_transaction": transaction
        }
        
        bot.send_message(user_id, message, reply_markup=keyboard, parse_mode="Markdown")
        logger.info(f"Transaction sent to user {user_id}")
        
    except Exception as e:
        logger.error(f"Error sending transaction to user: {e}")


@app.route('/webhook/register', methods=['POST'])
def register_user_gas():
    """
    Register user's Google Apps Script webhook URL
    
    Expected payload:
    {
        "user_id": "123456",
        "gas_webhook_url": "https://script.google.com/macros/s/.../exec",
        "signature": "hmac_signature"
    }
    """
    try:
        data = request.get_json()
        
        user_id = data.get('user_id')
        gas_webhook_url = data.get('gas_webhook_url')
        
        if not user_id or not gas_webhook_url:
            return jsonify({"error": "Missing user_id or gas_webhook_url"}), 400
        
        # Store user's GAS webhook URL
        user_gas_webhooks[user_id] = gas_webhook_url
        
        logger.info(f"Registered GAS webhook for user {user_id}")
        return jsonify({"success": True, "message": "Webhook registered"}), 200
        
    except Exception as e:
        logger.error(f"Error registering webhook: {e}")
        return jsonify({"error": str(e)}), 500


# Telegram Bot Handlers

def start_command(message):
    """Handle /start command"""
    user_id = str(message.from_user.id)
    
    if user_id not in AUTHORIZED_USER_IDS:
        bot.reply_to(message, "❌ Unauthorized. Please contact admin for access.")
        return
    
    welcome_text = """
👋 Welcome to Finance Bot!

This bot works with your Google Apps Script setup.

*Setup Steps:*
1. Deploy Google Apps Script to your account
2. Configure your Gemini API key
3. Connect your Google Spreadsheet
4. Register your webhook with /register

*Commands:*
/start - Show this message
/register - Register your Google Apps Script webhook
/status - Check connection status
/help - Get help

For setup guide, visit: [Setup Documentation]
"""
    
    bot.reply_to(message, welcome_text, parse_mode="Markdown")


def register_command(message):
    """Handle /register command"""
    user_id = str(message.from_user.id)
    
    if user_id not in AUTHORIZED_USER_IDS:
        bot.reply_to(message, "❌ Unauthorized.")
        return
    
    register_text = """
📝 *Register Your Google Apps Script*

After deploying your Google Apps Script:

1. Copy your Web App URL
2. Send it here in this format:
   `/register https://script.google.com/macros/s/YOUR_SCRIPT_ID/exec`

Example:
`/register https://script.google.com/macros/s/AKfycbx.../exec`
"""
    
    bot.reply_to(message, register_text, parse_mode="Markdown")


def handle_callback(call):
    """Handle callback queries"""
    data = call.data
    user_id = str(call.from_user.id)
    
    if data.startswith("confirm_email_tx:"):
        # User confirmed email transaction
        context = user_context.get(user_id, {})
        transaction = context.get("pending_email_transaction")
        
        if not transaction:
            bot.answer_callback_query(call.id, "Transaction not found")
            return
        
        # Send confirmation back to user's GAS
        if user_id in user_gas_webhooks:
            import requests
            
            gas_url = user_gas_webhooks[user_id]
            payload = {
                "action": "save_transaction",
                "transaction": transaction
            }
            
            try:
                response = requests.post(gas_url, json=payload, timeout=30)
                if response.status_code == 200:
                    bot.edit_message_text(
                        "✅ Transaction saved to your spreadsheet!",
                        call.message.chat.id,
                        call.message.message_id
                    )
                else:
                    bot.edit_message_text(
                        "❌ Failed to save transaction. Please try again.",
                        call.message.chat.id,
                        call.message.message_id
                    )
            except Exception as e:
                logger.error(f"Error saving transaction: {e}")
                bot.edit_message_text(
                    "❌ Error connecting to your Google Apps Script.",
                    call.message.chat.id,
                    call.message.message_id
                )
        else:
            bot.edit_message_text(
                "❌ Google Apps Script not registered. Use /register first.",
                call.message.chat.id,
                call.message.message_id
            )
        
        # Clear context
        user_context.pop(user_id, None)
        bot.answer_callback_query(call.id)
        
    elif data.startswith("reject_email_tx:"):
        # User rejected email transaction
        bot.edit_message_text(
            "❌ Transaction rejected.",
            call.message.chat.id,
            call.message.message_id
        )
        user_context.pop(user_id, None)
        bot.answer_callback_query(call.id, "Transaction rejected")


def main():
    """Start the bot"""
    global bot
    bot = telebot.TeleBot(TELEGRAM_TOKEN)
    
    # Register handlers
    bot.message_handler(commands=['start'])(start_command)
    bot.message_handler(commands=['register'])(register_command)
    bot.callback_query_handler(func=lambda call: True)(handle_callback)
    
    # Start Flask app for webhooks
    logger.info("Starting Flask webhook server...")
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))


if __name__ == "__main__":
    main()
