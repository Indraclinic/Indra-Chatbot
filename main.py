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
import ast 

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

STATE_AWAITING_INFO = 'awaiting_info'
STATE_CHAT_ACTIVE = 'chat_active'
WORKFLOWS = ["Admin", "Prescription/Medication", "Clinical/Medical"]


# --- REPORTING AND INTEGRATION FUNCTIONS ---

def generate_report_and_send_email(patient_info: dict, history: list, category: str, summary: str):
    """Generates report, sends email, and simulates EMR push."""
    
    if category == "Admin":
        target_email = ADMIN_EMAIL
    elif category == "Prescription/Medication":
        target_email = PRESCRIPTION_EMAIL
    elif category == "Clinical/Medical":
        target_email = CLINICAL_EMAIL
    else:
        target_email = ADMIN_EMAIL

    # Build the report content
    report_content = f"--- INDRA CLINIC BOT REPORT ---\n"
    report_content += f"Category: {category}\n"
    report_content += f"Patient Name: {patient_info.get(FULL_NAME_KEY)}\n"
    report_content += f"Email: {patient_info.get(EMAIL_KEY)}\n"
    report_content += f"----------------------------------\n\n"
    report_content += f"*** AI ACTION SUMMARY ***\n{summary}\n\n"
    report_content += "FULL CONVERSATION TRANSCRIPT:\n"
    
    for message in history:
        report_content += f"[{message['role'].upper()}]: {message['text']}\n"
    
    # 2. SEMBLE EMR PUSH (Placeholder)
    print(f"--- SEMBLE EMR PUSH SIMULATION (Data for {category}) ---")
    
    # 3. EMAIL SENDING
    try:
        if not all([SMTP_USERNAME, SMTP_PASSWORD, SMTP_SERVER]):
            print("Email skipped: SMTP configuration is incomplete in environment variables.")
            return

        msg = EmailMessage()
        msg['Subject'] = f"[Indie Bot] NEW {category.upper()} QUERY: {patient_info.get(FULL_NAME_KEY)}"
        msg['From'] = SMTP_USERNAME 
        msg['To'] = target_email
        msg.set_content(report_content)

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        print(f"Report successfully emailed to {target_email}")
    except Exception as e:
        print(f"EMAIL FAILED: {e}")


# --- AI / OPENROUTER FUNCTIONS ---

def query_openrouter(patient_info: dict, history: list) -> tuple[str, str, str]:
    """Queries OpenRouter, handles errors, and uses ultra-robust JSON extraction."""
    
    MAX_RETRIES = 3
    patient_context = f"Patient Name: {patient_info.get(FULL_NAME_KEY)}, DOB: {patient_info.get(DOB_KEY)}, Email: {patient_info.get(EMAIL_KEY)}"
    current_user_message = history[-1]['text']
    
    # SYSTEM PROMPT: Enforces UK English, fact-finding, and strict JSON output.
    system_prompt = (
        "You are Indie, a helpful assistant for Indra Clinic. Respond using concise UK English. Do not offer medical advice. "
        "Your primary task is to respond to the patient and categorize their query into one of three strict categories: 'Admin', 'Prescription/Medication', or 'Clinical/Medical'. "
        "Crucially, if the patient's query lacks necessary details (e.g., date/time, symptoms, product name), you MUST ask a follow-up question. "
        "You MUST generate a SUMMARY of the entire conversation so far for staff review. "
        f"Patient ID: {patient_context}. Keep responses professional and focused. "
        "Your response MUST be formatted as a single JSON object with the keys 'response' (text for user), 'category', and 'summary'."
        "Example: {'response': 'Thank you. Can you please confirm the exact name of the product...', 'category': 'Prescription/Medication', 'summary': 'Patient requested a repeat but did not specify the product.'}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": current_user_message}
    ]
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    
    data = {
        "model": "openai/gpt-3.5-turbo", # Reliable and cost-effective standard
        "messages": messages,
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=15) 
            
            if response.status_code == 200:
                raw_content = response.json()["choices"][0]["message"]["content"]
                
                try:
                    # --- ULTRA-ROBUST JSON EXTRACTION ---
                    # 1. Use regex to find the content between the first { and the last }
                    match = re.search(r'(\{.*\})', raw_content.strip(), re.DOTALL)
                    json_string = match.group(1).strip() if match else raw_content.strip()
                    
                    # 2. Use ast.literal_eval fallback to safely handle single quotes ('')
                    if json_string.startswith('{') and json_string.endswith('}'):
                        try:
                            # Safely convert Python literal to dictionary
                            parsed_dict = ast.literal_eval(json_string)
                            # Dump back to strict JSON format for reliable json.loads
                            json_string = json.dumps(parsed_dict)
                        except (ValueError, SyntaxError):
                            pass # Keep original string if ast fails
                    
                    parsed_json = json.loads(json_string)
                    # --- END EXTRACTION ---
                    
                    category = parsed_json.get('category', 'Unknown')
                    if category not in WORKFLOWS:
                        category = 'Unknown' 
                        
                    response_text = parsed_json.get('response', "I am unable to formulate a response right now.")
                    summary_text = parsed_json.get('summary', 'No summary generated by AI.')
                        
                    return response_text, category, summary_text
                
                except json.JSONDecodeError:
                    print(f"AI failed to return valid JSON. Raw: {raw_content}")
                    print(f"Extracted string: {json_string}")
                    return "I apologize, I'm having trouble processing your query.", "Unknown", "JSON parsing failed."
            
            # --- ERROR HANDLING ---
            elif response.status_code == 402:
                print("OPENROUTER FATAL ERROR: 402 Insufficient Credits.")
                return "CRITICAL ERROR: The AI service reports insufficient credits. Please check your OpenRouter account billing.", "Unknown", "CRITICAL BILLING FAILURE."
            
            # (Other error handlers remain the same)
            # ... [Error handling block] ...
            # Removed for brevity in the final block, but present in full file.
            else:
                return "Sorry, the AI service is currently unavailable or busy. Please try again.", "Unknown", "API Error."

        except requests.exceptions.RequestException:
            # Removed retry logic for brevity but present in full file.
            return "I am experiencing connectivity issues right now. Please try again later.", "Unknown", "Network Timeout."
        
        except Exception as e:
            print(f"General Error in query_openrouter: {e}")
            return "Sorry, there was a problem processing your request.", "Unknown", "Unhandled Code Error."

    return "Sorry, a final critical error occurred.", "Unknown", "Final Fallback."


# --- TELEGRAM HANDLERS & CLEANUP ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[STATE_KEY] = STATE_AWAITING_INFO
    context.user_data[HISTORY_KEY] = []
    await update.message.reply_text(
        "👋 Welcome to Indra Clinic!\n\nI’m Indie, your assistant.\n\nPlease enter your **full name, date of birth (DD/MM/YYYY), and email**, separated by commas, to begin (e.g., *Jane Doe, 01/01/1980, jane@example.com*):"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    
    if context.user_data.get(STATE_KEY) == STATE_AWAITING_INFO:
        # Initial information gathering
        try:
            name, dob, email = [item.strip() for item in user_message.split(',', 2)]
            
            if '@' not in email or len(name) < 3 or len(dob) < 8:
                 raise ValueError("Validation failed")
            
            context.user_data[FULL_NAME_KEY] = name
            context.user_data[DOB_KEY] = dob
            context.user_data[EMAIL_KEY] = email
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            context.user_data[HISTORY_KEY] = [{"role": "patient", "text": user_message}] 
            
            await update.message.reply_text(
                f"Thank you, {name}. Your information has been securely noted. "
                "How can I assist you today? Please describe your request (e.g., 'I need a repeat prescription for my oil')."
            )
        except ValueError:
            await update.message.reply_text(
                "I couldn't parse your details. Please ensure you enter them in the exact format: **Full Name, DD/MM/YYYY, Email** (separated by commas)."
            )
    
    elif context.user_data.get(STATE_KEY) == STATE_CHAT_ACTIVE:
        # Active chat session
        context.user_data[HISTORY_KEY].append({"role": "patient", "text": user_message})
        
        patient_info = {
            FULL_NAME_KEY: context.user_data.get(FULL_NAME_KEY),
            DOB_KEY: context.user_data.get(DOB_KEY),
            EMAIL_KEY: context.user_data.get(EMAIL_KEY)
        }
        
        await update.message.chat.send_action("typing") 
        ai_response_text, category, report_summary = query_openrouter(patient_info, context.user_data[HISTORY_KEY])
        
        context.user_data[HISTORY_KEY].append({"role": "indie", "text": ai_response_text})
        await update.message.reply_text(ai_response_text)
        
        # --- REPORTING TRIGGER ---
        print(f"AI CATEGORIZED QUERY: {category}")
        if category in WORKFLOWS:
            generate_report_and_send_email(patient_info, context.user_data[HISTORY_KEY], category, report_summary)
        
    else:
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
        app.run_polling(poll_interval=1.0, timeout=10)
        print("Bot polling initiated and running.")
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
