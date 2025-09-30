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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
CLINICAL_EMAIL = os.getenv("CLINICAL_EMAIL", "clinical@example.com")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
PRESCRIPTION_EMAIL = os.getenv("PRESCRIPTION_EMAIL", "prescribe@example.com")
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
    target_email = {
        "Admin": ADMIN_EMAIL,
        "Prescription/Medication": PRESCRIPTION_EMAIL,
        "Clinical/Medical": CLINICAL_EMAIL
    }.get(category, ADMIN_EMAIL)

    report_content = (
        f"--- INDRA CLINIC BOT REPORT ---\n\n"
        f"Patient ID: {patient_id}\n"
        f"Patient DOB (for verification): {dob}\n"
        f"Query Category: {category}\n"
        f"----------------------------------\n\n"
        f"*** AI-Generated Summary ***\n{summary}\n\n"
        f"*** Full Conversation Transcript ***\n"
    )
    for message in history:
        report_content += f"[{message['role'].upper()}]: {message['text']}\n"
    
    print(f"--- SEMBLE EMR PUSH SIMULATION for Patient ID: {patient_id} ---")
    
    try:
        if not all([SMTP_USERNAME, SMTP_PASSWORD, SMTP_SERVER]):
            print("Email skipped: SMTP configuration is incomplete.")
            return

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
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

def query_openrouter(history: list) -> tuple[str, str, str, str]:
    """
    Queries OpenRouter with an ANONYMIZED conversation history.
    The AI is grounded with information from the patient guidance document.
    """
    # --- MODIFICATION --- New system prompt incorporating the patient guidance leaflet.
    system_prompt = (
        "You are Indie, a helpful assistant for Indra Clinic, a UK-based medical cannabis clinic. "
        "Your tone must be professional, empathetic, and clear. Use appropriate medical terminology "
        "(e.g., 'Cannabis-Based Medicinal Products' or 'CBPMs') but avoid complex jargon. "
        "Your primary goal is to gather sufficient information to create a detailed report for the clinical team. "
        "You must not provide medical advice. Your output must be a JSON object with four keys: 'response', 'category', 'summary', and 'action'.\n\n"
        "**CRITICAL INSTRUCTION:** You can answer general patient questions based *only* on the official clinic guidance provided below. "
        "Frame your answers as 'According to the patient guidance leaflet...'. "
        "If the guidance does not cover a specific question, you must state that you do not have that information and advise the user to contact the clinic directly.\n\n"
        "--- OFFICIAL PATIENT GUIDANCE --- \n"
        "1.  **Medication Usage:**\n"
        "    - **Flower:** Must be used in a medical vaporiser. Start at 180°C (max 210°C). Take one small inhalation and wait at least 5 minutes before another. [cite_start]Never smoke or dab it. [cite: 17, 18, 19, 20, 21, 22]\n"
        "    - **Vapes:** Use with an approved device. [cite_start]Start with one short puff (2 seconds) and wait at least 5 minutes before repeating. [cite: 24, 25]\n"
        "    - **Pastilles:** Let them dissolve slowly in the mouth. Effects can take 30-90 minutes. [cite_start]Absorption may be improved with a light, fatty meal (e.g., yoghurt). [cite: 29, 30, 31, 32]\n"
        "    - **Oils:** Place under the tongue with the syringe and hold for about 1 minute. [cite_start]Can be taken with fatty food for better absorption. [cite: 35, 36, 38]\n"
        "2.  **Side Effects:**\n"
        [cite_start]"    - **Mild (dizzy, sleepy, fast heartbeat):** Rest and contact the clinic if concerned. [cite: 42, 43]\n"
        "    - **Severe (chest pain, severe paranoia, trouble breathing):** The user must call 999 or 111 immediately. [cite_start]This is a red flag. [cite: 12, 44]\n"
        "3.  **Safety:**\n"
        [cite_start]"    - **Driving:** It is illegal to drive if impaired by cannabis, even if prescribed. [cite: 58]\n"
        [cite_start]"    - **Alcohol:** Avoid alcohol as it can worsen side effects. [cite: 54]\n"
        [cite_start]"    - **Storage:** Keep medicine in its original container, locked away from children in a cool, dark place. [cite: 60, 61, 62]\n"
        "    - **Travel:** Prescriptions are valid in the UK only. [cite_start]For international travel, the user must check with the relevant embassy. [cite: 64, 65]\n"
        "--- END OF GUIDANCE ---"
    )

    messages = [{"role": "system", "content": system_prompt}]
    for turn in history:
        role = 'assistant' if turn['role'] == 'indie' else 'user'
        messages.append({"role": role, "content": turn['text']})

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    data = {"model": "openai/gpt-4o-mini", "messages": messages, "response_format": {"type": "json_object"}}

    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=20)
        response.raise_for_status() # Raises an exception for bad status codes (4xx or 5xx)
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return (
            parsed.get('response', "I'm having trouble formulating a response. Could you please rephrase?"),
            parsed.get('category', 'Admin'),
            parsed.get('summary', 'No summary was generated.'),
            parsed.get('action', 'CONTINUE').upper()
        )
    except requests.exceptions.RequestException as e:
        print(f"Network Error querying OpenRouter: {e}")
        return "I'm experiencing connectivity issues at the moment. Please try again in a little while.", "Admin", "Network error", "CONTINUE"
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Error parsing AI response: {e}")
        return "I received an unexpected response from our AI service. Let's try that again.", "Admin", "Parsing error", "CONTINUE"
    except Exception as e:
        print(f"An unexpected error occurred in query_openrouter: {e}")
        return "A technical issue occurred. Please try your request again.", "Admin", "Unhandled error", "CONTINUE"


# --- TELEGRAM HANDLERS & CONVERSATION FLOW ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiates a new conversation, clears old data, and asks for consent."""
    context.user_data.clear()
    context.user_data[STATE_KEY] = STATE_AWAITING_CONSENT
    
    consent_message = (
        "👋 Welcome to Indra Clinic! I’m Indie, your digital assistant.\n\n"
        "**Purpose of this Chat:** Please note that this chat is **not intended to provide medical advice.** "
        "It is an administrative tool designed to improve our workflow and help us address your queries more efficiently.\n\n"
        "Before we continue, please read our brief privacy notice:\n\n"
        "**Your Privacy at Indra Clinic**\n"
        "To use this service, we need to verify your identity and record this conversation in your patient file.\n\n"
        [cite_start]"• **For Verification:** We use your Patient ID and Date of Birth only to securely locate your patient record. [cite: 74, 75]\n"
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
        if user_message:
            context.user_data[PATIENT_ID_KEY] = user_message
            context.user_data[STATE_KEY] = STATE_AWAITING_DOB
            await update.message.reply_text("Thank you. For security, please also provide your **Date of Birth** (in DD/MM/YYYY format).")
        else:
            # --- MODIFICATION --- Friendlier error message
            await update.message.reply_text("Hmmm, that seems to be empty. Please provide your 10-character Patient ID to continue.")

    elif current_state == STATE_AWAITING_DOB:
        if len(user_message) >= 8:
            context.user_data[DOB_KEY] = user_message
            context.user_data[STATE_KEY] = STATE_AWAITING_CATEGORY
            context.user_data[HISTORY_KEY] = []
            await update.message.reply_text(
                f"Thank you. Your record has been securely located.\n\n"
                "To ensure your query is directed to the appropriate team, please select the category that best describes your request:\n\n"
                "1. [cite_start]**Administrative** (e.g., appointments, travel letters) [cite: 9]\n"
                "2. [cite_start]**Prescription/Medication** (e.g., repeat scripts, delivery issues) [cite: 10]\n"
                "3. [cite_start]**Clinical/Medical** (e.g., side effects, condition updates) [cite: 11]"
            )
        else:
            # --- MODIFICATION --- Friendlier error message
            await update.message.reply_text("Hmmm, that date doesn't look quite right. Could you please provide it in DD/MM/YYYY format?")

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
            await update.message.reply_text("Hmmm, I don't quite understand that choice. Please reply with the number or name of the category that best fits your query.")

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
                f"I have prepared the following summary for the **{category}** team. "
                f"Please review it for accuracy before we formally log it.\n\n"
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
            await update.message.reply_text("Thank you for confirming. Your query has been securely logged and dispatched. This conversation will now be reset.")
            await start(update, context)
            
        elif confirmation in ['no', 'n', 'incorrect', 'amend']:
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            context.user_data[HISTORY_KEY].append({"role": "user", "text": "The previous summary was not correct."})
            await update.message.reply_text("Understood. Please provide any corrections or additional information now.")
        
        else:
            await update.message.reply_text("I didn't quite understand. Please confirm with 'Yes' or 'No'.")

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
