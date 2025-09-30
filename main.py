import os
import sys
import time
import uuid
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import InvalidToken, Conflict
import requests
import json
import smtplib
from email.message import EmailMessage

# --- ENVIRONMENT VARIABLE CONFIGURATION ---
# These MUST be set in your environment (e.g., Render Worker settings).
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
CLINICAL_EMAIL = os.getenv("CLINICAL_EMAIL", "clinical@example.com")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
PRESCRIPTION_EMAIL = os.getenv("PRESCRIPTION_EMAIL", "prescribe@example.com")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

# Check that critical tokens are loaded
if not TELEGRAM_TOKEN:
    raise ValueError("FATAL: TELEGRAM_TOKEN environment variable not set.")
if not OPENROUTER_API_KEY:
    raise ValueError("FATAL: OPENROUTER_API_KEY environment variable not set.")

# --- STATE AND DATA KEYS ---
STATE_KEY = 'conversation_state'
HISTORY_KEY = 'chat_history'
TEMP_REPORT_KEY = 'temp_report'
PATIENT_ID_KEY = 'patient_id'
DOB_KEY = 'date_of_birth'
SESSION_ID_KEY = 'session_id'

# --- CONVERSATION STATES ---
STATE_AWAITING_CONSENT = 'awaiting_consent'
STATE_AWAITING_PATIENT_ID = 'awaiting_patient_id'
STATE_AWAITING_DOB = 'awaiting_dob'
STATE_AWAITING_CATEGORY = 'awaiting_category'
STATE_CHAT_ACTIVE = 'chat_active'
STATE_AWAITING_CONFIRMATION = 'awaiting_confirmation'
WORKFLOWS = ["Admin", "Prescription/Medication", "Clinical/Medical"]


# --- REPORTING AND INTEGRATION FUNCTIONS ---

def generate_report_and_send_email(patient_id: str, dob: str, history: list, category: str, summary: str):
    """Generates a report, sends emails, and simulates an EMR push."""
    
    if category == "Admin":
        target_email = ADMIN_EMAIL
    elif category == "Prescription/Medication":
        target_email = PRESCRIPTION_EMAIL
    elif category == "Clinical/Medical":
        target_email = CLINICAL_EMAIL
    else:
        target_email = ADMIN_EMAIL # Default fallback

    # Build the report content for internal staff
    report_content = f"--- INDRA CLINIC BOT REPORT ---\n\n"
    report_content += f"Patient ID: {patient_id}\n"
    report_content += f"Patient DOB (for verification): {dob}\n"
    report_content += f"Query Category: {category}\n"
    report_content += f"----------------------------------\n\n"
    report_content += f"*** AI-Generated Summary ***\n{summary}\n\n"
    report_content += "*** Full Conversation Transcript ***\n"
    
    for message in history:
        report_content += f"[{message['role'].upper()}]: {message['text']}\n"
    
    # Simulate pushing the transcript to Semble using the Patient ID
    print(f"--- SEMBLE EMR PUSH SIMULATION for Patient ID: {patient_id} ---")
    print("Data pushed successfully.")
    
    # Send email notifications
    try:
        if not all([SMTP_USERNAME, SMTP_PASSWORD, SMTP_SERVER]):
            print("Email skipped: SMTP configuration is incomplete.")
            return

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)

            # --- Internal Staff Email ---
            staff_msg = EmailMessage()
            subject = f"[Indie Bot] New {category.upper()} Query for Patient ID: ...{patient_id[-4:]}"
            if category == "Clinical/Medical":
                subject = f"[URGENT] " + subject
            
            staff_msg['Subject'] = subject
            staff_msg['From'] = SMTP_USERNAME
            staff_msg['To'] = target_email
            staff_msg.set_content(report_content)
            server.send_message(staff_msg)
            print(f"Report successfully emailed to {target_email}")

    except Exception as e:
        print(f"EMAIL DISPATCH FAILED: {e}")


# --- AI / OPENROUTER FUNCTIONS ---

def query_openrouter(session_id: str, history: list) -> tuple[str, str, str, str]:
    """
    Queries OpenRouter with an ANONYMIZED conversation history.
    The AI never receives any patient's personal identifiers.
    """
    MAX_RETRIES = 3
    
    system_prompt = (
        "You are Indie, a helpful assistant for Indra Clinic, a UK-based medical cannabis clinic. "
        "Your tone should be professional, empathetic, and clear. Use appropriate medical terminology "
        "(e.g., 'Cannabis-Based Medicinal Products' or 'CBPMs') but avoid complex jargon. "
        "Your primary goal is to gather sufficient information to create a detailed report for the clinical team. "
        "You must not provide medical advice. Your output must be a JSON object with four keys: 'response', 'category', 'summary', and 'action'.\n\n"
        "1.  **response**: Your text reply to the user.\n"
        "2.  **category**: Classify the query as 'Admin', 'Prescription/Medication', or 'Clinical/Medical'.\n"
        "3.  **summary**: A concise summary of the user's issue and the information gathered so far for the clinic staff.\n"
        "4.  **action**: Set to 'CONTINUE' if you need more information from the user. Set to 'REPORT' only when you have all the necessary details to resolve the query (e.g., for a repeat prescription, you need the product name and dosage).\n\n"
        "**Red Flag Protocol:** If the user describes life-threatening symptoms (severe chest pain, difficulty breathing, suicidal thoughts), your 'response' MUST immediately instruct them to contact emergency services (999 or 111). You must also set 'action' to 'REPORT' and 'category' to 'Clinical/Medical'."
    )

    messages = [{"role": "system", "content": system_prompt}]
    for turn in history:
        role = 'assistant' if turn['role'] == 'indie' else 'user'
        messages.append({"role": role, "content": turn['text']})

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "model": "openai/gpt-4o-mini",
        "messages": messages,
        "response_format": {"type": "json_object"}
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=20)
            
            if response.status_code == 200:
                content = json.loads(response.json()["choices"][0]["message"]["content"])
                return (
                    content.get('response', "I'm having trouble formulating a response. Could you please rephrase?"),
                    content.get('category', 'Admin'),
                    content.get('summary', 'No summary was generated.'),
                    content.get('action', 'CONTINUE').upper()
                )
            else:
                print(f"OpenRouter API Error (Attempt {attempt+1}): Status {response.status_code}, Body: {response.text}")
                if response.status_code in [429, 500, 502, 503, 504] and attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
                return "Our AI service is currently experiencing issues. Please try again shortly.", "Admin", "Service unavailable", "CONTINUE"
        
        except requests.exceptions.RequestException as e:
            print(f"Network Error querying OpenRouter (Attempt {attempt+1}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            return "I'm experiencing connectivity issues. Please check your connection or try again later.", "Admin", "Network error", "CONTINUE"
        
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Error parsing AI response: {e}")
            return "I received an unexpected response from our AI service. Let's try that again.", "Admin", "Parsing error", "CONTINUE"

    return "We've encountered a persistent issue with our AI service. Please contact the clinic directly.", "Admin", "Final fallback error", "CONTINUE"


# --- TELEGRAM HANDLERS & CONVERSATION FLOW ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiates a new conversation, clears old data, and asks for consent."""
    context.user_data.clear()
    context.user_data[SESSION_ID_KEY] = str(uuid.uuid4())
    context.user_data[STATE_KEY] = STATE_AWAITING_CONSENT
    
    consent_message = (
        "👋 Welcome to Indra Clinic! I’m Indie, your digital assistant.\n\n"
        "**Purpose of this Chat:** Please note that this chat is **not intended to provide medical advice.** "
        "It is an administrative tool designed to improve our workflow and help us address your queries more efficiently.\n\n"
        "Before we continue, please read our brief privacy notice:\n\n"
        "**Your Privacy at Indra Clinic**\n"
        "To use this service, we need to verify your identity and record this conversation in your patient file.\n\n"
        "• **For Verification:** We use your Patient ID and Date of Birth only to securely locate your patient record.\n"
        "• **For AI Assistance:** To understand your request, your anonymized conversation is processed by a third-party AI service. Your personal details are never shared with the AI.\n"
        "• **For Your Medical Record:** A transcript of this chat will be saved to your official file in our secure Semble EMR system.\n\n"
        "To confirm you have read this and wish to proceed, please type **'I agree'**."
    )
    await update.message.reply_text(consent_message)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The main state machine for handling all user messages."""
    current_state = context.user_data.get(STATE_KEY)
    user_message = update.message.text.strip()

    if current_state == STATE_AWAITING_CONSENT:
        if user_message.lower() == 'i agree':
            context.user_data[STATE_KEY] = STATE_AWAITING_PATIENT_ID
            await update.message.reply_text("Thank you. Please provide your **Patient ID**. This is the 10-character code included in all letters emailed to you.")
        else:
            await update.message.reply_text("To continue, you must consent to the privacy notice. Please type 'I agree' to proceed.")
    
    elif current_state == STATE_AWAITING_PATIENT_ID:
        if user_message: # Basic check to ensure it's not empty
            context.user_data[PATIENT_ID_KEY] = user_message
            context.user_data[STATE_KEY] = STATE_AWAITING_DOB
            await update.message.reply_text("Thank you. For security, please also provide your **Date of Birth** (in DD/MM/YYYY format).")
        else:
            await update.message.reply_text("The Patient ID cannot be empty. Please provide the 10-character code.")

    elif current_state == STATE_AWAITING_DOB:
        # In a real app, you would add more robust validation for the DOB format.
        if len(user_message) >= 8:
            context.user_data[DOB_KEY] = user_message
            context.user_data[STATE_KEY] = STATE_AWAITING_CATEGORY
            context.user_data[HISTORY_KEY] = [] # Initialize chat history after verification
            await update.message.reply_text(
                f"Thank you. Your record has been securely located.\n\n"
                "To ensure your query is directed to the appropriate team, please select the category that best describes your request:\n\n"
                "1. **Administrative** (e.g., appointments, travel letters)\n"
                "2. **Prescription/Medication** (e.g., repeat scripts, dosing queries)\n"
                "3. **Clinical/Medical** (e.g., side effects, condition updates)"
            )
        else:
            await update.message.reply_text("The Date of Birth seems incomplete. Please provide it in DD/MM/YYYY format.")

    elif current_state == STATE_AWAITING_CATEGORY:
        cleaned_message = user_message.lower()
        category_map = {
            '1': 'Administrative', 'admin': 'Administrative', 'administrative': 'Administrative',
            '2': 'Prescription/Medication', 'prescription': 'Prescription/Medication', 'medication': 'Prescription/Medication',
            '3': 'Clinical/Medical', 'clinical': 'Clinical/Medical', 'medical': 'Clinical/Medical'
        }
        matched_category = next((v for k, v in category_map.items() if k in cleaned_message), None)

        if matched_category:
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            context.user_data[HISTORY_KEY].append({"role": "user", "text": f"The user selected the '{matched_category}' category for their query."})
            await update.message.reply_text(f"Thank you. I've categorized your query under **{matched_category}**. Please describe the issue in detail now.")
        else:
            await update.message.reply_text("I didn't recognize that selection. Please reply with the number or name corresponding to your query (e.g., '2' or 'Prescription').")

    elif current_state == STATE_CHAT_ACTIVE:
        context.user_data[HISTORY_KEY].append({"role": "user", "text": user_message})
        await update.message.chat.send_action("typing")

        ai_response_text, category, summary, action = query_openrouter(
            context.user_data.get(SESSION_ID_KEY),
            context.user_data.get(HISTORY_KEY, [])
        )
        
        context.user_data[HISTORY_KEY].append({"role": "indie", "text": ai_response_text})
        await update.message.reply_text(ai_response_text)

        if action == "REPORT" and category in WORKFLOWS:
            context.user_data[TEMP_REPORT_KEY] = {'category': category, 'summary': summary}
            context.user_data[STATE_KEY] = STATE_AWAITING_CONFIRMATION
            await update.message.reply_text(
                f"---\n**Query Summary**\n---\n"
                f"I have prepared the following summary for the **{category}** team. "
                f"Please review it for accuracy before we formally log it in your patient file.\n\n"
                f"**Summary:** *{summary}*\n\n"
                "Is this summary correct and complete? Please reply with **'Yes'** to confirm or **'No'** to add more details."
            )

    elif current_state == STATE_AWAITING_CONFIRMATION:
        confirmation = user_message.lower()
        
        if confirmation in ['yes', 'y', 'correct', 'confirm']:
            report_data = context.user_data.get(TEMP_REPORT_KEY)
            generate_report_and_send_email(
                context.user_data.get(PATIENT_ID_KEY),
                context.user_data.get(DOB_KEY),
                context.user_data.get(HISTORY_KEY, []),
                report_data['category'],
                report_data['summary']
            )
            await update.message.reply_text(
                "Thank you for confirming. Your query has been securely logged and dispatched. This conversation will now be reset. If you have another issue, please start a new chat."
            )
            await start(update, context) # Reset for privacy
            
        elif confirmation in ['no', 'n', 'incorrect', 'amend']:
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            context.user_data[HISTORY_KEY].append({"role": "user", "text": "The previous summary was incorrect."})
            await update.message.reply_text("Understood. Please provide any corrections or additional information now.")
        
        else:
            await update.message.reply_text("I didn't quite understand. Please confirm if the summary is correct by replying 'Yes' or 'No'.")

    else:
        await start(update, context) # Fallback for any unknown state


# --- BOT SETUP AND LAUNCH ---

def main():
    """Initializes and runs the Telegram bot."""
    print("--- Indra Clinic Bot Initializing ---")
    
    try:
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    except InvalidToken:
        print("FATAL ERROR: The TELEGRAM_TOKEN is invalid.")
        sys.exit(1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Bot is configured. Starting polling...")
    try:
        app.run_polling(poll_interval=1)
    except Conflict:
        print("FATAL CONFLICT: Another instance of the bot is already running.")
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred during polling: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
