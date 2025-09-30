import os
import sys
import time
import uuid
import asyncio
import textwrap
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import InvalidToken, Conflict
import requests
import json
import smtplib
from email.message import EmailMessage

# --- ENVIRONMENT VARIABLE CONFIGURATION ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
SEMBLE_API_KEY = os.getenv("SEMBLE_API_KEY")
REPORT_EMAIL = os.getenv("REPORT_EMAIL", "drT@indra.clinic")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

if not TELEGRAM_TOKEN or not OPENROUTER_API_KEY:
    raise ValueError("FATAL: Critical environment variables are not set.")

# --- STATE AND DATA KEYS ---
STATE_KEY = 'conversation_state'
HISTORY_KEY = 'chat_history'
TEMP_REPORT_KEY = 'temp_report'
DOB_KEY = 'date_of_birth'
EMAIL_KEY = 'patient_email'
SESSION_ID_KEY = 'session_id'
# --- MODIFICATION --- Keys for the new admin flow
CURRENT_APPT_KEY = 'current_appointment'

# --- CONVERSATION STATES ---
STATE_AWAITING_CONSENT = 'awaiting_consent'
STATE_AWAITING_EMAIL = 'awaiting_email'
STATE_AWAITING_DOB = 'awaiting_dob'
STATE_AWAITING_CATEGORY = 'awaiting_category'
STATE_CHAT_ACTIVE = 'chat_active'
STATE_AWAITING_CONFIRMATION = 'awaiting_confirmation'
STATE_AWAITING_NEW_QUERY = 'awaiting_new_query'
# --- MODIFICATION --- New states for the hard-coded admin flow
STATE_ADMIN_AWAITING_CURRENT_APPT = 'admin_awaiting_current_appt'
STATE_ADMIN_AWAITING_NEW_APPT = 'admin_awaiting_new_appt'
WORKFLOWS = ["Admin", "Prescription/Medication", "Clinical/Medical"]


async def push_to_semble(patient_email: str, summary: str, transcript: str):
    """Finds a patient by email in Semble, then pushes a new consultation note."""
    if not SEMBLE_API_KEY:
        print("SEMBLE_API_KEY environment variable not set. Skipping EMR push.")
        return
    # ... (rest of function is unchanged)

# --- REPORTING AND INTEGRATION FUNCTIONS ---
def generate_report_and_send_email(dob: str, patient_email: str, session_id: str, history: list, category: str, summary: str):
    """Generates and sends reports to the admin and a confirmation to the patient."""
    transcript_content = f"Full Conversation Transcript (Session: {session_id})\n\n"
    # For hard-coded flows, history might be empty, so we build a simple transcript from the summary
    if not history:
        transcript_content += f"[SYSTEM]: User followed a guided workflow.\n[SUMMARY]: {summary}\n"
    else:
        for message in history:
            transcript_content += f"[{message['role'].upper()}]: {message['text']}\n"
    
    # ... (rest of function is unchanged)
    try:
        if not all([SMTP_USERNAME, SMTP_PASSWORD, SMTP_SERVER, SENDER_EMAIL]):
            print("Email skipped: SMTP configuration is incomplete.")
            return transcript_content
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            admin_subject = f"[Indie Bot] {category} Query from: {patient_email} (DOB: {dob})"
            if category == "Clinical/Medical": admin_subject = f"[URGENT] " + admin_subject
            admin_body = (f"A new query has been logged...\n\nPatient Email: {patient_email}\nPatient DOB: {dob}\nCategory: {category}\n\n--- AI-Generated Summary ---\n{summary}")
            admin_msg = EmailMessage()
            admin_msg['Subject'] = admin_subject
            admin_msg['From'] = SENDER_EMAIL
            admin_msg['To'] = REPORT_EMAIL
            admin_msg.set_content(admin_body)
            admin_msg.add_attachment(transcript_content.encode('utf-8'), maintype='text', subtype='plain', filename=f'transcript_{session_id[-6:]}.txt')
            server.send_message(admin_msg)
            print(f"Admin report successfully emailed to {REPORT_EMAIL}")
            patient_subject = "Indra Clinic: A copy of your recent query"
            patient_body = (f"Dear Patient,\n\nFor your records, here is a summary of your recent query.\n\n**Summary:**\n{summary}\n\nKind regards,\nThe Indra Clinic Team")
            patient_msg = EmailMessage()
            patient_msg['Subject'] = patient_subject
            patient_msg['From'] = SENDER_EMAIL
            patient_msg['To'] = patient_email
            patient_msg.set_content(patient_body)
            patient_msg.add_attachment(transcript_content.encode('utf-8'), maintype='text', subtype='plain', filename=f'transcript_summary.txt')
            server.send_message(patient_msg)
            print(f"Patient copy successfully emailed to {patient_email}")
    except Exception as e:
        print(f"EMAIL DISPATCH FAILED: {e}")
    return transcript_content


# --- AI / OPENROUTER FUNCTIONS ---
def query_openrouter(history: list) -> tuple[str, str, str, str]:
    """Queries OpenRouter for open-ended conversations (Clinical/Prescription)."""
    # --- MODIFICATION --- Simplified the prompt, as the complex Admin logic is now in Python.
    system_prompt = textwrap.dedent("""\
        You are Indie, a helpful assistant for Indra Clinic. Your tone is professional and empathetic.
        Your primary goal is to gather information for a report. You must not provide medical advice.
        Your output must be a JSON object with four keys: 'response', 'category', 'summary', and 'action'.

        **Action Logic (CRITICAL):**
        - If your 'response' is a question, your 'action' MUST be 'CONTINUE'.
        - Set 'action' to 'REPORT' only when you have gathered all necessary information.

        **Workflow Instructions:**
        - **Clinical/Medical:** Your role IS to ask clarifying questions about symptoms (onset, duration, severity, location, etc.). This is essential data collection.
        - **Prescription/Medication:** Ask clarifying questions to understand the user's need (e.g., which medication, what is the specific request).
    """)
    # ... (rest of function is unchanged)
    messages = [{"role": "system", "content": system_prompt}]
    for turn in history:
        role = 'assistant' if turn['role'] == 'indie' else 'user'
        messages.append({"role": role, "content": turn['text']})
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    data = {"model": "openai/gpt-4o-mini", "messages": messages, "response_format": {"type": "json_object"}}
    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=20)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return (parsed.get('response', "I'm having trouble formulating a response."), parsed.get('category', 'Admin'), parsed.get('summary', 'No summary generated.'), parsed.get('action', 'CONTINUE').upper())
    except Exception as e:
        print(f"An error occurred in query_openrouter: {e}")
        return "A technical issue occurred.", "Admin", "Unhandled error", "CONTINUE"


# --- TELEGRAM HANDLERS & CONVERSATION FLOW ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiates a new conversation, clears old data, and asks for consent."""
    # ... (function is unchanged)
    context.user_data.clear()
    context.user_data[SESSION_ID_KEY] = str(uuid.uuid4())
    context.user_data[STATE_KEY] = STATE_AWAITING_CONSENT
    await update.message.reply_text("👋 Welcome to Indra Clinic! I’m Indie, your digital assistant.\n\n**Purpose of this Chat:** Please note that this chat is **not intended to provide medical advice.** It is an administrative tool to improve our workflow.")
    await asyncio.sleep(1.5)
    await update.message.reply_text("This service is in beta testing. If you prefer, you can email us directly at drT@indra.clinic.")
    await asyncio.sleep(1.5)
    consent_message = ("Before we continue, please read our brief privacy notice:\n\n**Your Privacy at Indra Clinic**\n• **For Verification:** We use your email address and Date of Birth to associate this chat with your patient record.\n• **For AI Assistance:** Your anonymised conversation is processed by a third-party AI to understand your request.\n• **For Your Medical Record:** A summary of this chat will be added to your secure Semble EMR file.\n• **For Your Records:** A copy of the summary and transcript will be emailed to you upon completion.\n\nTo confirm and proceed, please type **'I agree'**.")
    await update.message.reply_text(consent_message)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The main state machine for handling all user messages."""
    current_state = context.user_data.get(STATE_KEY)
    user_message = update.message.text.strip()
    
    # --- MODIFICATION --- The entire state machine has been restructured for the new Admin flow ---
    
    if current_state == STATE_AWAITING_CONSENT:
        if user_message.lower() == 'i agree':
            context.user_data[STATE_KEY] = STATE_AWAITING_EMAIL
            await update.message.reply_text("Thank you. To begin, please provide the **email address** you registered with the clinic.")
        else:
            await update.message.reply_text("To continue, please type 'I agree' to proceed.")

    elif current_state == STATE_AWAITING_EMAIL:
        if '@' in user_message and '.' in user_message:
            context.user_data[EMAIL_KEY] = user_message
            context.user_data[STATE_KEY] = STATE_AWAITING_DOB
            await update.message.reply_text("Thank you. For security, please also provide your **Date of Birth** (DD/MM/YYYY).")
        else:
            await update.message.reply_text("Hmmm, that email address doesn't seem valid. Please check and try again.")
            
    elif current_state == STATE_AWAITING_DOB:
        if len(user_message) >= 8:
            context.user_data[DOB_KEY] = user_message
            context.user_data[STATE_KEY] = STATE_AWAITING_CATEGORY
            context.user_data[HISTORY_KEY] = []
            await update.message.reply_text(
                f"Thank you. Your details have been noted for our report.\n\n"
                "Please select the category for your query:\n\n"
                "1. **Administrative**\n2. **Prescription/Medication**\n3. **Clinical/Medical**"
            )
        else:
            await update.message.reply_text("Hmmm, that date doesn't look right. Please provide it in DD/MM/YYYY format.")

    elif current_state == STATE_AWAITING_CATEGORY:
        cleaned_message = user_message.lower()
        
        if any(word in cleaned_message for word in ['1', 'admin']):
            context.user_data[STATE_KEY] = STATE_ADMIN_AWAITING_CURRENT_APPT
            await update.message.reply_text("Understood. To request an appointment change, please tell me the date and time of your **current** appointment.")
        elif any(word in cleaned_message for word in ['2', 'prescription', 'medication']):
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            context.user_data[HISTORY_KEY].append({"role": "user", "text": "Selected category: 'Prescription/Medication'."})
            await update.message.reply_text("Thank you. For this **Prescription/Medication** query, please describe your request in detail now.")
        elif any(word in cleaned_message for word in ['3', 'clinical', 'medical']):
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            context.user_data[HISTORY_KEY].append({"role": "user", "text": "Selected category: 'Clinical/Medical'."})
            await update.message.reply_text("Thank you. For this **Clinical/Medical** query, please start by describing the issue you are experiencing.")
        else:
            await update.message.reply_text("I don't understand that choice. Please reply with a number (1-3) or name.")

    # --- NEW ADMIN WORKFLOW STATES ---
    elif current_state == STATE_ADMIN_AWAITING_CURRENT_APPT:
        context.user_data[CURRENT_APPT_KEY] = user_message
        context.user_data[STATE_KEY] = STATE_ADMIN_AWAITING_NEW_APPT
        await update.message.reply_text("Thank you. And what is the **new** date and time you would like to request?")

    elif current_state == STATE_ADMIN_AWAITING_NEW_APPT:
        current_appt = context.user_data.get(CURRENT_APPT_KEY)
        new_appt = user_message
        summary = f"Patient requests to change their appointment from '{current_appt}' to '{new_appt}'."
        
        context.user_data[TEMP_REPORT_KEY] = {'category': 'Admin', 'summary': summary}
        context.user_data[STATE_KEY] = STATE_AWAITING_CONFIRMATION
        
        await update.message.reply_text(
            f"---\n**Query Summary**\n---\n"
            f"Please review the summary of your request for accuracy:\n\n"
            f"**Summary:** *{summary}*\n\n"
            "Is this summary correct? Please reply with **'Yes'** to confirm or **'No'** to start over."
        )

    # --- AI-DRIVEN WORKFLOW STATE ---
    elif current_state == STATE_CHAT_ACTIVE:
        context.user_data[HISTORY_KEY].append({"role": "user", "text": user_message})
        await update.message.chat.send_action("typing")
        ai_response_text, category, summary, action = query_openrouter(context.user_data.get(HISTORY_KEY, []))
        
        context.user_data[HISTORY_KEY].append({"role": "indie", "text": ai_response_text})
        await update.message.reply_text(ai_response_text)

        if action == "REPORT" and category in WORKFLOWS:
            context.user_data[TEMP_REPORT_KEY] = {'category': category, 'summary': summary}
            context.user_data[STATE_KEY] = STATE_AWAITING_CONFIRMATION
            await update.message.reply_text(
                f"---\n**Query Summary**\n---\n"
                f"I have prepared the following summary. Please review it for accuracy.\n\n"
                f"**Summary:** *{summary}*\n\n"
                "Is this summary correct? Please reply with **'Yes'** to confirm or **'No'** to add more details."
            )

    # --- FINAL WORKFLOW STATES (Shared by all) ---
    elif current_state == STATE_AWAITING_CONFIRMATION:
        confirmation = user_message.lower()
        if confirmation in ['yes', 'y', 'correct', 'confirm']:
            report_data = context.user_data.get(TEMP_REPORT_KEY)
            
            transcript = generate_report_and_send_email(
                context.user_data.get(DOB_KEY),
                context.user_data.get(EMAIL_KEY),
                context.user_data.get(SESSION_ID_KEY),
                context.user_data.get(HISTORY_KEY, []), # History might be empty for admin flow
                report_data['category'],
                report_data['summary']
            )
            
            await push_to_semble(
                context.user_data.get(EMAIL_KEY),
                report_data['summary'],
                transcript
            )

            context.user_data[STATE_KEY] = STATE_AWAITING_NEW_QUERY
            await update.message.reply_text(
                "Thank you for confirming. Your query has been logged and a copy has been sent to your email.\n\n"
                "Is there anything else I can help you with? You can choose a category or type **'No'** to end the chat.\n\n"
                "1. **Administrative**\n2. **Prescription/Medication**\n3. **Clinical/Medical**"
            )
            
        elif confirmation in ['no', 'n', 'incorrect', 'amend']:
            # For AI-driven flows, go back to chatting. For hard-coded flows, restart the category choice.
            if context.user_data.get(HISTORY_KEY):
                context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
                await update.message.reply_text("Understood. Please provide any corrections or additional information.")
            else:
                context.user_data[STATE_KEY] = STATE_AWAITING_CATEGORY
                await update.message.reply_text("Understood. Let's start over. Please select a category for your query.")
        
        else:
            await update.message.reply_text("I didn't understand. Please confirm with 'Yes' or 'No'.")
            
    elif current_state == STATE_AWAITING_NEW_QUERY:
        cleaned_message = user_message.lower()
        if any(word in cleaned_message for word in ['no', 'nope', 'bye', 'end', 'finish', 'done']):
            await update.message.reply_text("Thank you for using our service. Be well. You can now close this chat, or type /start to begin again.")
            context.user_data.clear()
        else: # Assume any other response is an attempt to start a new query
            context.user_data[STATE_KEY] = STATE_AWAITING_CATEGORY
            # Call this function again to process the message (e.g., '1' or 'clinical') in the correct state
            await handle_message(update, context)

    else:
        await start(update, context)


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
        print("FATAL CONFLICT: Another instance of the bot is already running. Please restart the service.")
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred during polling: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
