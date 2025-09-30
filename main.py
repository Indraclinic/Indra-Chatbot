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
CURRENT_APPT_KEY = 'current_appointment'

# --- CONVERSATION STATES ---
STATE_AWAITING_CONSENT = 'awaiting_consent'
STATE_AWAITING_EMAIL = 'awaiting_email'
STATE_AWAITING_DOB = 'awaiting_dob'
STATE_AWAITING_CATEGORY = 'awaiting_category'
STATE_CHAT_ACTIVE = 'chat_active'
STATE_AWAITING_CONFIRMATION = 'awaiting_confirmation'
STATE_AWAITING_NEW_QUERY = 'awaiting_new_query'
STATE_ADMIN_AWAITING_CURRENT_APPT = 'admin_awaiting_current_appt'
STATE_ADMIN_AWAITING_NEW_APPT = 'admin_awaiting_new_appt'
WORKFLOWS = ["Admin", "Prescription/Medication", "Clinical/Medical"]


async def push_to_semble(patient_email: str, summary: str, transcript: str):
    """Finds a patient by email in Semble, then pushes a new consultation note."""
    if not SEMBLE_API_KEY:
        print("SEMBLE_API_KEY environment variable not set. Skipping EMR push.")
        return

    headers = {"Authorization": f"Bearer {SEMBLE_API_KEY}", "Content-Type": "application/json", "Accept": "application/json"}
    
    async with httpx.AsyncClient() as client:
        try:
            patient_search_url = f"https://api.semble.io/v1/patients?email={patient_email}"
            search_response = await client.get(patient_search_url, headers=headers, timeout=20)
            search_response.raise_for_status()
            
            patients = search_response.json().get('data', [])
            if not patients:
                print(f"ERROR: No patient found in Semble with email: {patient_email}")
                return
            
            semble_patient_id = patients[0]['id']
            print(f"Found Semble Patient ID: {semble_patient_id} for email: {patient_email}")

            consultation_url = f"https://api.semble.io/v1/patients/{semble_patient_id}/consultations"
            note_body = (f"**Indie Bot AI Summary:**\n{summary}\n\n--- Full Conversation Transcript ---\n{transcript}")
            note_data = {"body": note_body}
            
            consult_response = await client.post(consultation_url, headers=headers, json=note_data, timeout=20)
            consult_response.raise_for_status()
            print(f"Successfully pushed note to Semble for Patient ID: {semble_patient_id}")

        except httpx.HTTPStatusError as e:
            print(f"ERROR: Failed to push note to Semble. Status: {e.response.status_code}, Response: {e.response.text}")
        except Exception as e:
            print(f"ERROR: An unexpected error occurred when pushing to Semble: {e}")

# --- REPORTING AND INTEGRATION FUNCTIONS ---
def generate_report_and_send_email(dob: str, patient_email: str, session_id: str, history: list, category: str, summary: str):
    """Generates and sends reports to the admin and a confirmation to the patient."""
    transcript_content = f"Full Conversation Transcript (Session: {session_id})\n\n"
    if not history:
        transcript_content += f"[SYSTEM]: User followed a guided workflow.\n[SUMMARY]: {summary}\n"
    else:
        for message in history:
            transcript_content += f"[{message['role'].upper()}]: {message['text']}\n"
    
    try:
        if not all([SMTP_USERNAME, SMTP_PASSWORD, SMTP_SERVER, SENDER_EMAIL]):
            print("Email skipped: SMTP configuration is incomplete. Check SENDER_EMAIL and SMTP variables.")
            return transcript_content

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            
            # Email to Staff
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
            
            # Email to Patient
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
    system_prompt = textwrap.dedent("""\
        You are Indie, a helpful assistant for Indra Clinic. Your tone is professional and empathetic.
        Your primary goal is to gather information for a report. You must not provide medical advice.
        Your output must be a JSON object with four keys: 'response', 'category', 'summary', and 'action'.

        **Action Logic (CRITICAL):**
        - If your 'response' is a question, your 'action' MUST be 'CONTINUE'.
        - Set 'action' to 'REPORT' only when you have gathered all necessary information.

        **Workflow Instructions:**
        - **Clinical/Medical:** Your role IS to ask clarifying questions about symptoms (onset, duration, severity, etc.).
        - **Prescription/Medication:** Ask clarifying questions to understand the user's need.
    """)
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
        return (parsed.get('response', "I'm having trouble."), parsed.get('category', 'Admin'), parsed.get('summary', 'No summary.'), parsed.get('action', 'CONTINUE').upper())
    except Exception as e:
        print(f"An error occurred in query_openrouter: {e}")
        return "A technical issue occurred.", "Admin", "Unhandled error", "CONTINUE"


# --- TELEGRAM HANDLERS & CONVERSATION FLOW ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiates a new conversation."""
    context.user_data.clear()
    context.user_data[SESSION_ID_KEY] = str(uuid.uuid4())
    context.user_data[STATE_KEY] = STATE_AWAITING_CONSENT
    await update.message.reply_text("👋 Welcome to Indra Clinic! I’m Indie, your digital assistant.\n\n**Purpose of this Chat:** This is an administrative tool, not for medical advice.")
    await asyncio.sleep(1.5)
    await update.message.reply_text("This service is in beta. If you prefer, email us at drT@indra.clinic.")
    await asyncio.sleep(1.5)
    consent_message = ("Please read our privacy notice:\n\n**Your Privacy**\n• **Verification:** We use your email and DOB to link this chat to your record.\n• **AI Assistance:** Your anonymised chat is processed by an AI.\n• **Medical Record:** A summary will be added to your Semble EMR file.\n• **Your Records:** A copy will be emailed to you.\n\nTo proceed, type **'I agree'**.")
    await update.message.reply_text(consent_message)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The main state machine for handling all user messages."""
    current_state = context.user_data.get(STATE_KEY)
    user_message = update.message.text.strip()
    
    if current_state == STATE_AWAITING_CONSENT:
        if user_message.lower() == 'i agree':
            context.user_data[STATE_KEY] = STATE_AWAITING_EMAIL
            await update.message.reply_text("Thank you. To begin, please provide your **email address**.")
        else:
            await update.message.reply_text("To continue, please type 'I agree'.")

    elif current_state == STATE_AWAITING_EMAIL:
        if '@' in user_message and '.' in user_message:
            context.user_data[EMAIL_KEY] = user_message
            context.user_data[STATE_KEY] = STATE_AWAITING_DOB
            await update.message.reply_text("Thank you. Please also provide your **Date of Birth** (DD/MM/YYYY).")
        else:
            await update.message.reply_text("Hmmm, that email doesn't look valid. Please try again.")
            
    elif current_state == STATE_AWAITING_DOB:
        if len(user_message) >= 8:
            context.user_data[DOB_KEY] = user_message
            context.user_data[STATE_KEY] = STATE_AWAITING_CATEGORY
            context.user_data[HISTORY_KEY] = []
            await update.message.reply_text(
                f"Thank you. Details noted.\n\n"
                "Please select a category:\n1. **Administrative**\n2. **Prescription/Medication**\n3. **Clinical/Medical**"
            )
        else:
            await update.message.reply_text("Hmmm, that date doesn't look right. Please use DD/MM/YYYY format.")

    elif current_state == STATE_AWAITING_CATEGORY:
        cleaned_message = user_message.lower()
        if any(word in cleaned_message for word in ['1', 'admin']):
            context.user_data[STATE_KEY] = STATE_ADMIN_AWAITING_CURRENT_APPT
            await update.message.reply_text("Understood. To change an appointment, what is the date and time of your **current** appointment?")
        elif any(word in cleaned_message for word in ['2', 'prescription']):
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            context.user_data[HISTORY_KEY].append({"role": "user", "text": "Category: Prescription/Medication."})
            await update.message.reply_text("Thank you. Please describe your prescription request.")
        elif any(word in cleaned_message for word in ['3', 'clinical']):
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            context.user_data[HISTORY_KEY].append({"role": "user", "text": "Category: Clinical/Medical."})
            await update.message.reply_text("Thank you. Please describe the clinical issue.")
        else:
            await update.message.reply_text("I don't understand. Please reply with a number (1-3).")

    elif current_state == STATE_ADMIN_AWAITING_CURRENT_APPT:
        context.user_data[CURRENT_APPT_KEY] = user_message
        context.user_data[STATE_KEY] = STATE_ADMIN_AWAITING_NEW_APPT
        await update.message.reply_text("Thank you. And what is the **new** date and time you would like?")

    elif current_state == STATE_ADMIN_AWAITING_NEW_APPT:
        current_appt = context.user_data.get(CURRENT_APPT_KEY)
        new_appt = user_message
        summary = f"Patient requests to change their appointment from '{current_appt}' to '{new_appt}'."
        context.user_data[TEMP_REPORT_KEY] = {'category': 'Admin', 'summary': summary}
        context.user_data[STATE_KEY] = STATE_AWAITING_CONFIRMATION
        await update.message.reply_text(f"---\n**Query Summary**\n---\nPlease review:\n\n**Summary:** *{summary}*\n\nIs this correct? (Yes/No)")

    elif current_state == STATE_CHAT_ACTIVE:
        context.user_data[HISTORY_KEY].append({"role": "user", "text": user_message})
        await update.message.chat.send_action("typing")
        ai_response_text, category, summary, action = query_openrouter(context.user_data.get(HISTORY_KEY, []))
        context.user_data[HISTORY_KEY].append({"role": "indie", "text": ai_response_text})
        await update.message.reply_text(ai_response_text)
        if action == "REPORT" and category in WORKFLOWS:
            context.user_data[TEMP_REPORT_KEY] = {'category': category, 'summary': summary}
            context.user_data[STATE_KEY] = STATE_AWAITING_CONFIRMATION
            await update.message.reply_text(f"---\n**Query Summary**\n---\nPlease review:\n\n**Summary:** *{summary}*\n\nIs this correct? (Yes/No)")

    elif current_state == STATE_AWAITING_CONFIRMATION:
        confirmation = user_message.lower()
        if confirmation in ['yes', 'y', 'correct', 'confirm']:
            report_data = context.user_data.get(TEMP_REPORT_KEY)
            
            # --- DIAGNOSTIC BREADCRUMBS ---
            print("# --- DIAGNOSTIC: 1. Confirmation received. About to call generate_report_and_send_email. ---")
            
            transcript = generate_report_and_send_email(
                context.user_data.get(DOB_KEY),
                context.user_data.get(EMAIL_KEY),
                context.user_data.get(SESSION_ID_KEY),
                context.user_data.get(HISTORY_KEY, []),
                report_data['category'],
                report_data['summary']
            )
            
            print("# --- DIAGNOSTIC: 2. Finished generate_report_and_send_email. About to call push_to_semble. ---")
            
            await push_to_semble(
                context.user_data.get(EMAIL_KEY),
                report_data['summary'],
                transcript
            )
            
            print("# --- DIAGNOSTIC: 3. Finished push_to_semble. ---")

            context.user_data[STATE_KEY] = STATE_AWAITING_NEW_QUERY
            await update.message.reply_text(
                "Thank you. Your query has been logged and a copy sent to your email.\n\n"
                "Is there anything else I can help with? (Choose 1-3 or say 'No')\n\n"
                "1. Administrative\n2. Prescription/Medication\n3. Clinical/Medical"
            )
            
        elif confirmation in ['no', 'n', 'incorrect', 'amend']:
            if context.user_data.get(HISTORY_KEY):
                context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
                await update.message.reply_text("Understood. Please provide corrections.")
            else:
                context.user_data[STATE_KEY] = STATE_AWAITING_CATEGORY
                await update.message.reply_text("Understood. Let's start over. Please select a category.")
        
        else:
            await update.message.reply_text("I didn't understand. Please confirm with 'Yes' or 'No'.")
            
    elif current_state == STATE_AWAITING_NEW_QUERY:
        cleaned_message = user_message.lower()
        if any(word in cleaned_message for word in ['no', 'nope', 'bye', 'end']):
            await update.message.reply_text("Thank you for using our service. Be well.")
            context.user_data.clear()
        else:
            context.user_data[STATE_KEY] = STATE_AWAITING_CATEGORY
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
        print("FATAL CONFLICT: Another instance is running. Please restart the service.")
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred during polling: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
