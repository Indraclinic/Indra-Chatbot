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
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

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
EMAIL_KEY = 'patient_email'
SESSION_ID_KEY = 'session_id'

# --- CONVERSATION STATES ---
STATE_AWAITING_CONSENT = 'awaiting_consent'
STATE_AWAITING_PATIENT_ID = 'awaiting_patient_id'
STATE_AWAITING_DOB = 'awaiting_dob'
STATE_AWAITING_EMAIL = 'awaiting_email'
STATE_AWAITING_CATEGORY = 'awaiting_category'
STATE_CHAT_ACTIVE = 'chat_active'
STATE_AWAITING_CONFIRMATION = 'awaiting_confirmation'
STATE_AWAITING_NEW_QUERY = 'awaiting_new_query'
WORKFLOWS = ["Admin", "Prescription/Medication", "Clinical/Medical"]


async def push_to_semble(patient_id: str, dob: str, patient_email: str, summary: str, transcript: str):
    """Connects to the Semble API and pushes a new consultation note."""
    if not SEMBLE_API_KEY:
        print("SEMBLE_API_KEY environment variable not set. Skipping EMR push.")
        return

    SEMBLE_API_URL = f"https://api.semble.io/v1/patients/{patient_id}/consultations"
    headers = {"Authorization": f"Bearer {SEMBLE_API_KEY}", "Content-Type": "application/json", "Accept": "application/json"}
    note_body = (f"**Indie Bot AI Summary:**\n{summary}\n\n--- Full Conversation Transcript ---\n{transcript}")
    note_data = {"body": note_body}
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(SEMBLE_API_URL, headers=headers, json=note_data, timeout=20)
            response.raise_for_status()
            print(f"Successfully pushed note to Semble for Patient ID: {patient_id}")
        except httpx.HTTPStatusError as e:
            print(f"ERROR: Failed to push note to Semble. Status: {e.response.status_code}, Response: {e.response.text}")
        except Exception as e:
            print(f"ERROR: An unexpected error occurred when pushing to Semble: {e}")

# --- REPORTING AND INTEGRATION FUNCTIONS ---
def generate_report_and_send_email(patient_id: str, dob: str, patient_email: str, history: list, category: str, summary: str):
    """Generates and sends reports to the admin and a confirmation to the patient."""
    
    transcript_content = f"Full Conversation Transcript for Patient ID: {patient_id}\n\n"
    for message in history:
        transcript_content += f"[{message['role'].upper()}]: {message['text']}\n"
    
    try:
        if not all([SMTP_USERNAME, SMTP_PASSWORD, SMTP_SERVER]):
            print("Email skipped: SMTP configuration is incomplete.")
            return transcript_content

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            
            # --- EMAIL 1: To Clinic Staff ---
            admin_subject = f"[Indie Bot] {category} Query for Patient ID: {patient_id} (DOB: {dob})"
            if category == "Clinical/Medical":
                admin_subject = f"[URGENT] " + admin_subject
            
            admin_body = (
                f"A new query has been logged via the Indra Clinic Bot.\n\n"
                f"Patient ID: {patient_id}\n"
                f"Patient DOB: {dob}\n"
                f"Patient Email: {patient_email}\n"
                f"Category: {category}\n\n"
                f"--- AI-Generated Summary ---\n{summary}"
            )
            
            admin_msg = EmailMessage()
            admin_msg['Subject'] = admin_subject
            admin_msg['From'] = SMTP_USERNAME
            admin_msg['To'] = REPORT_EMAIL
            admin_msg.set_content(admin_body)
            admin_msg.add_attachment(transcript_content.encode('utf-8'), maintype='text', subtype='plain', filename=f'transcript_{patient_id}.txt')
            server.send_message(admin_msg)
            print(f"Admin report successfully emailed to {REPORT_EMAIL}")

            # --- EMAIL 2: To Patient ---
            patient_subject = "Indra Clinic: A copy of your recent query"
            patient_body = (
                f"Dear Patient,\n\n"
                f"Thank you for contacting Indra Clinic. For your records, a summary and full transcript of your recent query are attached.\n\n"
                f"The clinical team will review your submission and be in touch shortly.\n\n"
                f"**Summary of your query:**\n{summary}\n\n"
                f"Kind regards,\nThe Indra Clinic Team"
            )
            patient_msg = EmailMessage()
            patient_msg['Subject'] = patient_subject
            patient_msg['From'] = SMTP_USERNAME
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
    """Queries OpenRouter with an anonymised conversation history."""
    # --- MODIFICATION --- Added stricter rules for Admin appointment changes.
    system_prompt = textwrap.dedent("""\
        You are Indie, a helpful assistant for Indra Clinic. Your tone is professional and empathetic.
        Your primary goal is to gather information for a report. You must not provide medical advice.
        Your output must be a JSON object with four keys: 'response', 'category', 'summary', and 'action'.

        **Action Logic (CRITICAL):**
        - If your 'response' is a question, your 'action' MUST be 'CONTINUE'.
        - Only set 'action' to 'REPORT' when you have gathered all necessary information and are providing a final statement, not a question.

        **Workflow-Specific Instructions (CRITICAL):**
        - **Admin - Appointment Change:** Your ONLY goal is to collect two pieces of information: 1. The date/time of the CURRENT appointment. 2. The date/time of the DESIRED new appointment.
            - Do NOT ask for the patient's name or reference numbers.
            - Do NOT pretend to check for availability.
            - Once you have the current and desired times, your 'response' must be a simple confirmation like 'Thank you, I have all the details needed.' and you MUST set 'action' to 'REPORT'. Do not provide a summary in your response text; the system will handle that.
        - **Clinical/Medical:** Your role IS to ask clarifying questions about symptoms (onset, duration, severity, location, etc.). This is essential data collection.
        - **General Questions:** You can answer general questions based ONLY on the official clinic guidance below.

        --- OFFICIAL PATIENT GUIDANCE ---
        - **Medication Usage:** Flower must be vaporised (180-210°C). Vapes are one short puff. Wait 5 mins between doses for both.
        - **Side Effects:** For mild symptoms (dizzy, sleepy), rest and contact the clinic. For severe symptoms (chest pain, trouble breathing), call 999 immediately.
        - **Safety:** Driving while impaired is illegal. Avoid alcohol. Store medicine securely.
        --- END OF GUIDANCE ---
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
        return (
            parsed.get('response', "I'm having trouble formulating a response. Could you please rephrase?"),
            parsed.get('category', 'Admin'),
            parsed.get('summary', 'No summary was generated.'),
            parsed.get('action', 'CONTINUE').upper()
        )
    except Exception as e:
        print(f"An error occurred in query_openrouter: {e}")
        return "A technical issue occurred. Please try your request again.", "Admin", "Unhandled error", "CONTINUE"


# --- TELEGRAM HANDLERS & CONVERSATION FLOW ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiates a new conversation, clears old data, and asks for consent."""
    context.user_data.clear()
    context.user_data[STATE_KEY] = STATE_AWAITING_CONSENT
    
    await update.message.reply_text(
        "👋 Welcome to Indra Clinic! I’m Indie, your digital assistant.\n\n"
        "**Purpose of this Chat:** Please note that this chat is **not intended to provide medical advice.** "
        "It is an administrative tool to improve our workflow."
    )
    await asyncio.sleep(1.5)

    await update.message.reply_text(
        "This service is in beta testing. If you prefer, you can email us directly at drT@indra.clinic."
    )
    await asyncio.sleep(1.5)

    consent_message = (
        "Before we continue, please read our brief privacy notice:\n\n"
        "**Your Privacy at Indra Clinic**\n"
        "• **For Verification:** We use your Patient ID, Date of Birth, and email address to securely associate this chat with your patient record.\n"
        "• **For AI Assistance:** Your anonymised conversation is processed by a third-party AI to understand your request.\n"
        "• **For Your Medical Record:** A summary of this chat will be added to your secure Semble EMR file.\n"
        "• **For Your Records:** A copy of the summary and transcript will be emailed to you upon completion.\n\n"
        "To confirm and proceed, please type **'I agree'**."
    )
    await update.message.reply_text(consent_message)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The main state machine for handling all user messages."""
    current_state = context.user_data.get(STATE_KEY)
    user_message = update.message.text.strip()
    
    category_map = {
        '1': 'Administrative', 'admin': 'Administrative',
        '2': 'Prescription/Medication', 'prescription': 'Prescription/Medication',
        '3': 'Clinical/Medical', 'clinical': 'Clinical/Medical'
    }

    if current_state == STATE_AWAITING_CONSENT:
        if user_message.lower() == 'i agree':
            context.user_data[STATE_KEY] = STATE_AWAITING_PATIENT_ID
            await update.message.reply_text("Thank you. Please provide your **Patient ID**. This is the 10-character code included in all letters emailed to you.")
        else:
            await update.message.reply_text("To continue, please type 'I agree' to proceed.")
    
    elif current_state == STATE_AWAITING_PATIENT_ID:
        if user_message:
            context.user_data[PATIENT_ID_KEY] = user_message
            context.user_data[STATE_KEY] = STATE_AWAITING_DOB
            await update.message.reply_text("Thank you. For security, please also provide your **Date of Birth** (in DD/MM/YYYY format).")
        else:
            await update.message.reply_text("Hmmm, that seems to be empty. Please provide your 10-character Patient ID.")

    elif current_state == STATE_AWAITING_DOB:
        if len(user_message) >= 8:
            context.user_data[DOB_KEY] = user_message
            context.user_data[STATE_KEY] = STATE_AWAITING_EMAIL
            await update.message.reply_text("And finally, please provide the **email address** you registered with the clinic.")
        else:
            await update.message.reply_text("Hmmm, that date doesn't look right. Please provide it in DD/MM/YYYY format.")
    
    elif current_state == STATE_AWAITING_EMAIL:
        if '@' in user_message and '.' in user_message:
            context.user_data[EMAIL_KEY] = user_message
            context.user_data[STATE_KEY] = STATE_AWAITING_CATEGORY
            context.user_data[HISTORY_KEY] = []
            
            await update.message.reply_text(
                f"Thank you. I've securely noted those details for our report.\n\n"
                "Please select the category for your query:\n\n"
                "1. **Administrative**\n"
                "2. **Prescription/Medication**\n"
                "3. **Clinical/Medical**"
            )
        else:
            await update.message.reply_text("Hmmm, that email address doesn't seem valid. Please check and try again.")

    elif current_state == STATE_AWAITING_CATEGORY:
        cleaned_message = user_message.lower()
        matched_category = next((v for k, v in category_map.items() if k in cleaned_message), None)

        if matched_category:
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            context.user_data[HISTORY_KEY].append({"role": "user", "text": f"Selected category: '{matched_category}'."})
            
            if matched_category == 'Clinical/Medical':
                await update.message.reply_text(f"Thank you. For this **Clinical/Medical** query, please start by describing the issue.")
            else:
                await update.message.reply_text(f"Thank you. For this **{matched_category}** query, please describe your request.")
        else:
            await update.message.reply_text("I don't understand that choice. Please reply with a number (1-3) or name.")

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
                f"I have prepared the following summary for the **{category}** team. Please review it for accuracy.\n\n"
                f"**Summary:** *{summary}*\n\n"
                "Is this summary correct? Please reply with **'Yes'** to confirm or **'No'** to add more details."
            )

    elif current_state == STATE_AWAITING_CONFIRMATION:
        confirmation = user_message.lower()
        if confirmation in ['yes', 'y', 'correct', 'confirm']:
            report_data = context.user_data.get(TEMP_REPORT_KEY)
            
            transcript = generate_report_and_send_email(
                context.user_data.get(PATIENT_ID_KEY),
                context.user_data.get(DOB_KEY),
                context.user_data.get(EMAIL_KEY),
                context.user_data.get(HISTORY_KEY, []),
                report_data['category'],
                report_data['summary']
            )
            
            await push_to_semble(
                context.user_data.get(PATIENT_ID_KEY),
                context.user_data.get(DOB_KEY),
                context.user_data.get(EMAIL_KEY),
                report_data['summary'],
                transcript
            )

            context.user_data[STATE_KEY] = STATE_AWAITING_NEW_QUERY
            await update.message.reply_text(
                "Thank you for confirming. Your query has been logged and a copy has been sent to your email address.\n\n"
                "Is there anything else I can help you with? You can choose a category or type **'No'** to end the chat.\n\n"
                "1. **Administrative**\n2. **Prescription/Medication**\n3. **Clinical/Medical**"
            )
            
        elif confirmation in ['no', 'n', 'incorrect', 'amend']:
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            context.user_data[HISTORY_KEY].append({"role": "user", "text": "The summary was not correct."})
            await update.message.reply_text("Understood. Please provide any corrections or additional information.")
        
        else:
            await update.message.reply_text("I didn't understand. Please confirm with 'Yes' or 'No'.")
            
    elif current_state == STATE_AWAITING_NEW_QUERY:
        cleaned_message = user_message.lower()
        matched_category = next((v for k, v in category_map.items() if k in cleaned_message), None)

        if matched_category:
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            context.user_data[HISTORY_KEY] = [{"role": "user", "text": f"Starting new query: '{matched_category}'."}]
            context.user_data.pop(TEMP_REPORT_KEY, None)
            await update.message.reply_text(f"Of course. Let's begin a new query for **{matched_category}**. Please describe your issue.")
        elif any(word in cleaned_message for word in ['no', 'nope', 'bye', 'end', 'finish', 'done']):
            await update.message.reply_text("Thank you for using our service. Be well. You can now close this chat, or type /start to begin again.")
            context.user_data.clear()
        else:
            await update.message.reply_text("I didn't understand. Please select a category (1-3) or type 'No' to end.")

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
        print("FATAL CONFLICT: Another instance of the bot is already running.")
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred during polling: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
