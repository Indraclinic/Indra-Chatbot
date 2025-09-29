import os
import sys
import signal
import time
import re
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import InvalidToken, Conflict
import requests
import json
import smtplib
from email.message import EmailMessage

# --- ENVIRONMENT VARIABLE CONFIGURATION ---
# Load environment variables. These MUST be set in your Render Worker settings.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
CLINICAL_EMAIL = os.getenv("CLINICAL_EMAIL", "clinical@example.com")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
PRESCRIPTION_EMAIL = os.getenv("PRESCRIPTION_EMAIL", "prescribe@example.com")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.example.com") # Replace with your SMTP server
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

# Check that the tokens loaded correctly
if TELEGRAM_TOKEN is None:
    raise ValueError("TELEGRAM_TOKEN environment variable not set.")
if OPENROUTER_API_KEY is None:
    raise ValueError("OPENROUTER_API_KEY environment variable not set.")


# --- STATE AND CONSTANTS ---
FULL_NAME_KEY = 'full_name'
DOB_KEY = 'dob'
EMAIL_KEY = 'email'
STATE_KEY = 'conversation_state'
HISTORY_KEY = 'chat_history'
TEMP_REPORT_KEY = 'temp_report'
CATEGORY_SELECTED_KEY = 'category_selected' # --- MODIFICATION --- Added key to track if category is selected

STATE_AWAITING_INFO = 'awaiting_info'
STATE_AWAITING_CATEGORY = 'awaiting_category' # --- MODIFICATION --- New state for category selection
STATE_CHAT_ACTIVE = 'chat_active'
STATE_AWAITING_CONFIRMATION = 'awaiting_confirmation'
WORKFLOWS = ["Admin", "Prescription/Medication", "Clinical/Medical"]


# --- REPORTING AND INTEGRATION FUNCTIONS ---

def generate_report_and_send_email(patient_info: dict, history: list, category: str, summary: str):
    """Generates report, sends email to staff and patient, and simulates EMR push."""

    if category == "Admin":
        target_email = ADMIN_EMAIL
    elif category == "Prescription/Medication":
        target_email = PRESCRIPTION_EMAIL
    elif category == "Clinical/Medical":
        target_email = CLINICAL_EMAIL
    else:
        target_email = ADMIN_EMAIL

    # Build the report content for internal staff
    report_content = f"--- INDRA CLINIC BOT REPORT ---\n"
    report_content += f"Category: {category}\n"
    report_content += f"Patient Name: {patient_info.get(FULL_NAME_KEY)}\n"
    report_content += f"Email: {patient_info.get(EMAIL_KEY)}\n"
    report_content += f"----------------------------------\n\n"
    report_content += f"*** AI ACTION SUMMARY ***\n{summary}\n\n"
    report_content += "FULL CONVERSATION TRANSCRIPT:\n"

    for message in history:
        # Don't include the initial detail submission in the transcript
        if message['role'] == 'patient' and '@' in message['text'] and ',' in message['text']:
            continue
        report_content += f"[{message['role'].upper()}]: {message['text']}\n"

    # 2. SEMBLE EMR PUSH (Placeholder)
    print(f"--- SEMBLE EMR PUSH SIMULATION (Data for {category}) ---")

    # 3. EMAIL SENDING
    try:
        if not all([SMTP_USERNAME, SMTP_PASSWORD, SMTP_SERVER]):
            print("Email skipped: SMTP configuration is incomplete in environment variables.")
            return

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)

            # --- EMAIL 1: INTERNAL STAFF REPORT ---
            staff_msg = EmailMessage()
            staff_msg['Subject'] = f"[Indie Bot] NEW {category.upper()} QUERY: {patient_info.get(FULL_NAME_KEY)}"
            if category == "Clinical/Medical":
                 staff_msg['Subject'] = f"[URGENT] [Indie Bot] NEW {category.upper()} QUERY: {patient_info.get(FULL_NAME_KEY)}"
            staff_msg['From'] = SMTP_USERNAME
            staff_msg['To'] = target_email
            staff_msg.set_content(report_content)
            server.send_message(staff_msg)
            print(f"Report successfully emailed to {target_email}")

            # --- MODIFICATION: EMAIL 2: PATIENT CONFIRMATION ---
            patient_email = patient_info.get(EMAIL_KEY)
            if patient_email:
                patient_msg = EmailMessage()
                patient_msg['Subject'] = "Indra Clinic: We've received your query"
                patient_msg['From'] = SMTP_USERNAME
                patient_msg['To'] = patient_email

                patient_body = (
                    f"Dear {patient_info.get(FULL_NAME_KEY)},\n\n"
                    f"Thank you for contacting Indra Clinic. We have successfully received your query and routed it to the correct department.\n\n"
                    f"A member of our team will be in touch with you shortly.\n\n"
                    f"For your records, here is a summary of your request:\n"
                    f"----------------------------------\n"
                    f"Category: {category}\n"
                    f"Summary: {summary}\n"
                    f"----------------------------------\n\n"
                    f"Kind regards,\n"
                    f"The Indra Clinic Team"
                )
                patient_msg.set_content(patient_body)
                server.send_message(patient_msg)
                print(f"Confirmation email successfully sent to {patient_email}")

    except Exception as e:
        print(f"EMAIL FAILED: {e}")


# --- AI / OPENROUTER FUNCTIONS ---

def query_openrouter(patient_info: dict, history: list) -> tuple[str, str, str, str]:
    """Queries OpenRouter, handles errors, uses native JSON mode, and returns action."""

    MAX_RETRIES = 3
    patient_context = f"Patient Name: {patient_info.get(FULL_NAME_KEY)}, DOB: {patient_info.get(DOB_KEY)}, Email: {patient_info.get(EMAIL_KEY)}"

    # SYSTEM PROMPT: Refined for Workflow Completion, CBPM Lingo, and Red Flags.
    system_prompt = (
        "You are Indie, a helpful assistant for Indra Clinic, a UK-based medical cannabis clinic. Respond using concise UK English. "
        "Do not offer medical advice. You MUST use terminology related to Cannabis-Based Medicinal Products (CBPMs) when appropriate. "
        "Your output must be a JSON object with the keys 'response' (text for user), 'category', 'summary', and 'action'. "

        # CATEGORIZATION & ACTION RULES
        "1. CATEGORY: One of 'Admin', 'Prescription/Medication', or 'Clinical/Medical'. "
        "2. ACTION: Set to 'CONTINUE' if more detail is needed from the user. Set to 'REPORT' when sufficient detail is gathered. "

        # RED FLAG PROTOCOL (CRITICAL)
        "If the user mentions urgent, life-threatening symptoms (e.g., severe chest pain, difficulty breathing, major uncontrolled bleeding, sudden paralysis, suicidal ideation), "
        "your 'response' MUST immediately instruct the user to call 999 or 111, and you MUST set 'category' to 'Clinical/Medical' and 'action' to 'REPORT'. "

        # WORKFLOW COMPLETION RULES
        "For Admin (e.g., appointment changes, travel letter): Continue asking questions until the patient provides all required details. Then set 'action' to 'REPORT'. "
        "For Prescription (e.g., repeat, dosing): Continue asking questions until the patient provides the product name and specific request. Then set 'action' to 'REPORT'. "
        "For Clinical (non-urgent): Ask relevant clarifying questions about their symptoms, medication, or side effects, aiming to gather enough information for the clinical team to respond within 48h. Once gathered, set 'action' to 'REPORT'. "

        f"Patient ID: {patient_context}. Keep responses professional and focused. "
    )

    # The messages list should contain the full conversation history for context
    messages = [
        {"role": "system", "content": system_prompt}
    ]
    for turn in history:
        role = 'assistant' if turn['role'] == 'indie' else 'user'
        # --- MODIFICATION --- Do not include the initial credential submission in the AI context
        if role == 'user' and '@' in turn['text'] and ',' in turn['text']:
            continue
        messages.append({"role": role, "content": turn['text']})


    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "openai/gpt-4o-mini",
        "messages": messages,
        "response_format": {"type": "json_object"}
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=15)

            if response.status_code == 200:
                raw_content = response.json()["choices"][0]["message"]["content"]

                try:
                    # Native JSON parsing is now guaranteed to work or fail cleanly
                    parsed_json = json.loads(raw_content)

                    category = parsed_json.get('category', 'Unknown')
                    if category not in WORKFLOWS:
                        category = 'Unknown'

                    response_text = parsed_json.get('response', "I am unable to formulate a response right now.")
                    summary_text = parsed_json.get('summary', 'No summary generated by AI.')
                    action_type = parsed_json.get('action', 'CONTINUE').upper()

                    return response_text, category, summary_text, action_type

                except json.JSONDecodeError:
                    print(f"AI failed to return valid JSON despite JSON mode. Raw: {raw_content}")
                    # Return error and force action to continue to prevent immediate report spam
                    return "I apologize, I'm having trouble processing your query.", "Unknown", "JSON parsing failed.", "CONTINUE"

            # --- ERROR HANDLING (Unchanged) ---
            elif response.status_code == 402:
                print("OPENROUTER FATAL ERROR: 402 Insufficient Credits.")
                return "CRITICAL ERROR: The AI service reports insufficient credits. Please check your OpenRouter account billing.", "Unknown", "CRITICAL BILLING FAILURE.", "CONTINUE"

            elif response.status_code in (401, 403):
                print(f"OPENROUTER FATAL ERROR: Status Code {response.status_code}. Details: {response.text}")
                return "ERROR: Authentication failed. Please check the OPENROUTER_API_KEY.", "Unknown", "Auth Failure.", "CONTINUE"

            elif response.status_code in (429, 500, 502, 503, 504):
                if attempt < MAX_RETRIES - 1:
                    wait_time = 2 ** attempt
                    print(f"OPENROUTER RETRYABLE ERROR: Status Code {response.status_code}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    print(f"OPENROUTER FAILED after {MAX_RETRIES} attempts. Status Code {response.status_code}. Details: {response.text}")
                    return "Sorry, the AI service is currently unavailable or busy. Please try again.", "Unknown", "Service Unavailable.", "CONTINUE"

            else:
                print(f"OPENROUTER NON-RETRYABLE ERROR: Status Code {response.status_code}. Details: {response.text}")
                return "Sorry, the AI service is currently unavailable or busy. Please try again.", "Unknown", "API Error.", "CONTINUE"

        except requests.exceptions.RequestException:
            if attempt < MAX_RETRIES - 1:
                wait_time = 2 ** attempt
                print(f"OpenRouter Network Error: Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                print(f"OpenRouter Network/Timeout FAILED after {MAX_RETRIES} attempts.")
                return "I am experiencing connectivity issues right now. Please try again later.", "Unknown", "Network Timeout.", "CONTINUE"

        except Exception as e:
            print(f"General Error in query_openrouter: {e}")
            return "Sorry, there was a problem processing your request.", "Unknown", "Unhandled Code Error.", "CONTINUE"

    return "Sorry, a final critical error occurred.", "Unknown", "Final Fallback.", "CONTINUE"


# --- TELEGRAM HANDLERS & CLEANUP ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear() # --- MODIFICATION --- Clear all previous data on /start
    context.user_data[STATE_KEY] = STATE_AWAITING_INFO
    context.user_data[HISTORY_KEY] = []
    await update.message.reply_text(
        "👋 Welcome to Indra Clinic!\n\nI’m Indie, your assistant.\n\nPlease enter your **full name, date of birth (DD/MM/YYYY), and email**, separated by commas, to begin (e.g., *Jane Doe, 01/01/1980, jane@example.com*):"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    current_state = context.user_data.get(STATE_KEY)

    # --- MODIFICATION --- Simplified and corrected state machine logic
    if current_state == STATE_AWAITING_INFO:
        # Initial information gathering
        try:
            name, dob, email = [item.strip() for item in user_message.split(',', 2)]

            if '@' not in email or len(name) < 3 or len(dob) < 8:
                 raise ValueError("Validation failed")

            context.user_data[FULL_NAME_KEY] = name
            context.user_data[DOB_KEY] = dob
            context.user_data[EMAIL_KEY] = email
            context.user_data[STATE_KEY] = STATE_AWAITING_CATEGORY # Move to new category state
            context.user_data[HISTORY_KEY] = [{"role": "patient", "text": user_message}]

            # Ask for initial category
            await update.message.reply_text(
                f"Thank you, {name}. Your information has been securely noted.\n\n"
                "To help me direct your query, please tell me what your request is about:\n\n"
                "1. **Administrative** (appointments, travel letters)\n"
                "2. **Prescription/Medication** (repeats, dosing)\n"
                "3. **Clinical/Medical** (side effects, condition updates)\n\n"
                "Please reply with the number or the category name."
            )
        except (ValueError, IndexError):
            await update.message.reply_text(
                "I couldn't parse your details. Please ensure you enter them in the exact format: **Full Name, DD/MM/YYYY, Email** (separated by commas)."
            )

    elif current_state == STATE_AWAITING_CATEGORY:
        # --- MODIFICATION --- New logic block to handle category selection
        cleaned_message = user_message.lower().strip()
        category_map = {
            '1': 'Administrative', 'administrative': 'Administrative',
            '2': 'Prescription/Medication', 'prescription': 'Prescription/Medication', 'medication': 'Prescription/Medication',
            '3': 'Clinical/Medical', 'clinical': 'Clinical/Medical', 'medical': 'Clinical/Medical'
        }

        matched_category = None
        for key, value in category_map.items():
            if key in cleaned_message:
                matched_category = value
                break

        if matched_category:
            # Add a user-like message to the history for AI context
            context.user_data[HISTORY_KEY].append({"role": "patient", "text": f"My query is about a {matched_category} issue."})
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            await update.message.reply_text(
                f"Great, I've noted this as a **{matched_category}** query. Please describe your request in detail now."
            )
        else:
            await update.message.reply_text(
                "I didn't recognize that category. Please choose one of the options by replying with the number or name (e.g., '1' or 'Admin')."
            )

    elif current_state == STATE_AWAITING_CONFIRMATION:
        # Check for user confirmation
        confirmation = user_message.lower().strip()
        report_data = context.user_data.get(TEMP_REPORT_KEY)

        if confirmation in ['yes', 'y', '1', 'ok', 'confirm', 'correct']:
            # Final step: Send report and push to EMR
            generate_report_and_send_email(
                context.user_data,
                context.user_data.get(HISTORY_KEY, []),
                report_data['category'],
                report_data['summary']
            )
            context.user_data.clear() # Clear data for privacy and a clean slate
            await start(update, context) # Restart the conversation flow
            await update.message.reply_text(
                "Thank you for confirming. Your request has been securely logged and dispatched. You'll also receive an email confirmation shortly. The chat has now been reset for your privacy."
            )

        elif confirmation in ['no', 'n', '0', 'edit', 'amend', 'incorrect']:
            # User wants to edit, go back to active chat
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            await update.message.reply_text(
                "No problem. Please provide the correction or add more details now."
            )

        else:
            await update.message.reply_text(
                "I didn't recognize that. Please reply with 'Yes' to confirm the summary, or 'No' to add more details."
            )

    elif current_state == STATE_CHAT_ACTIVE:
        # Active chat session
        context.user_data[HISTORY_KEY].append({"role": "patient", "text": user_message})

        patient_info = {
            FULL_NAME_KEY: context.user_data.get(FULL_NAME_KEY),
            DOB_KEY: context.user_data.get(DOB_KEY),
            EMAIL_KEY: context.user_data.get(EMAIL_KEY)
        }

        await update.message.chat.send_action("typing")
        # Query returns 4 values now: text, category, summary, action
        ai_response_text, category, report_summary, action_type = query_openrouter(patient_info, context.user_data[HISTORY_KEY])

        context.user_data[HISTORY_KEY].append({"role": "indie", "text": ai_response_text})
        await update.message.reply_text(ai_response_text)

        # --- NEW WORKFLOW CONTROL ---
        print(f"AI CATEGORIZED QUERY: {category}, ACTION: {action_type}")

        if action_type == "REPORT" and category in WORKFLOWS:
            # Report gathered: transition to confirmation state

            # Store report data temporarily
            context.user_data[TEMP_REPORT_KEY] = {
                'category': category,
                'summary': report_summary
            }
            context.user_data[STATE_KEY] = STATE_AWAITING_CONFIRMATION

            await update.message.reply_text(
                "\n---\n**Report Summary for Staff**\n---\n"
                f"**Category:** {category}\n"
                f"**Summary:** {report_summary}\n\n"
                "Please review this summary. If it is accurate, reply **'Yes'** to send the report to our team. "
                "If anything is missing or incorrect, reply **'No'** to continue adding details."
            )

    else:
        # Fallback for any unknown state
        await start(update, context)


def telegram_cleanup(token):
    """Synchronously attempts to delete any lingering webhooks to prevent Conflict error."""
    try:
        url = f"https://api.telegram.org/bot{token}/deleteWebhook"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        print("Telegram cleanup attempted successfully.")
    except Exception as e:
        print(f"General error during Telegram cleanup: {e}")


def start_bot_loop():
    """Builds the application and starts the polling loop safely."""

    # 1. CLEANUP STEP: Kill any previous polling/webhook sessions
    telegram_cleanup(TELEGRAM_TOKEN)
    time.sleep(1) # Wait briefly

    try:
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    except InvalidToken:
        print("FATAL ERROR: The TELEGRAM_TOKEN is invalid.")
        sys.exit(1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 2. Start polling
    try:
        print("Bot polling initiated...")
        app.run_polling(poll_interval=1.0, timeout=10)
    except Conflict as e:
        print(f"FATAL CONFLICT ERROR: {e}")
        print("Another bot instance is active. The cleanup failed or the system is race-locking.")
        sys.exit(1)


# Main function
def main():
    print(f"--- RENDER ENVIRONMENT DEBUG ---")
    print(f"Running with Python Version: {sys.version}")
    print(f"--- RENDER ENVIRONMENT DEBUG ---")

    start_bot_loop()


if __name__ == "__main__":
    main()
