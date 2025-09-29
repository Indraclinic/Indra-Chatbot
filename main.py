import os
import sys
import signal # Required for clean shutdown handling
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import InvalidToken
import requests
import json 
import smtplib
from email.message import EmailMessage

# Load environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
# New Environment Variables for Email/Workflow routing (SET THESE ON RENDER!)
CLINICAL_EMAIL = os.getenv("CLINICAL_EMAIL", "clinical@example.com") 
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
PRESCRIPTION_EMAIL = os.getenv("PRESCRIPTION_EMAIL", "prescribe@example.com")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.sendgrid.net") # Example SMTP server
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

# Check that the tokens loaded correctly
if TELEGRAM_TOKEN is None:
    raise ValueError("TELEGRAM_TOKEN environment variable not set.")
if OPENROUTER_API_KEY is None:
    raise ValueError("OPENROUTER_API_KEY environment variable not set.")


# State and Key Management
FULL_NAME_KEY = 'full_name'
DOB_KEY = 'dob'
EMAIL_KEY = 'email'
STATE_KEY = 'conversation_state'
HISTORY_KEY = 'chat_history'

STATE_AWAITING_INFO = 'awaiting_info'
STATE_CHAT_ACTIVE = 'chat_active'
WORKFLOWS = ["Admin", "Prescription/Medication", "Clinical/Medical"]


# --- REPORTING AND INTEGRATION FUNCTIONS (PHASE 3) ---

def generate_report_and_send_email(patient_info: dict, history: list, category: str):
    """Generates report, sends email, and simulates EMR push."""
    
    # 1. Determine target email based on AI category
    if category == "Admin":
        target_email = ADMIN_EMAIL
    elif category == "Prescription/Medication":
        target_email = PRESCRIPTION_EMAIL
    elif category == "Clinical/Medical":
        target_email = CLINICAL_EMAIL
    else:
        print(f"Warning: Unknown category '{category}'. Sending to Admin.")
        target_email = ADMIN_EMAIL

    # ... (Report content generation remains the same) ...
    report_content = f"--- INDRA CLINIC BOT REPORT ---\n"
    report_content += f"Category: {category}\n"
    report_content += f"Patient Name: {patient_info.get(FULL_NAME_KEY)}\n"
    report_content += f"DOB: {patient_info.get(DOB_KEY)}\n"
    report_content += f"Email: {patient_info.get(EMAIL_KEY)}\n"
    report_content += f"----------------------------------\n\n"
    report_content += "CONVERSATION TRANSCRIPT:\n"
    
    for message in history:
        report_content += f"[{message['role'].upper()}]: {message['text']}\n"
    
    # 2. SEMBLE EMR PUSH (Placeholder)
    print(f"--- SEMBLE EMR PUSH SIMULATION (Data for {category}) ---")
    
    # 3. EMAIL SENDING
    try:
        # Check for basic SMTP configuration before attempting to send
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

def query_openrouter(patient_info: dict, history: list) -> tuple[str, str]:
    """Queries OpenRouter to get the bot's response AND categorize the workflow."""
    
    # ... (OpenRouter logic remains the same) ...
    patient_context = f"Patient Name: {patient_info.get(FULL_NAME_KEY)}, DOB: {patient_info.get(DOB_KEY)}, Email: {patient_info.get(EMAIL_KEY)}"
    current_user_message = history[-1]['text']
    
    system_prompt = (
        "You are Indie, a helpful assistant for Indra Clinic. Do not offer medical advice. "
        "Your primary task is to respond to the patient and categorize their query into one of three strict categories: 'Admin', 'Prescription/Medication', or 'Clinical/Medical'. "
        f"Patient ID: {patient_context}. Keep responses professional, concise, and focused on gathering necessary information for the relevant team."
        "If the query is Clinical/Medical, state clearly that a specialist nurse will review the transcript and be in touch soon. "
        "Your response MUST be formatted as a single JSON object with the keys 'response' (the text for the user) and 'category' (one of the three strict categories). "
        "Example: {'response': 'Your repeat prescription request has been noted...', 'category': 'Prescription/Medication'}"
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
        "model": "openai/gpt-3.5-turbo",
        "messages": messages,
    }

    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=15) 
        response.raise_for_status() 
        
        raw_content = response.json()["choices"][0]["message"]["content"]
        
        try:
            # Clean up potential markdown wrapper
            if raw_content.strip().startswith("```json"):
                 raw_content = raw_content.strip()[7:-3].strip()
            
            parsed_json = json.loads(raw_content)
            
            category = parsed_json.get('category', 'Unknown')
            if category not in WORKFLOWS:
                category = 'Unknown' 
                
            return parsed_json.get('response', "I am unable to formulate a response right now."), category
        
        except json.JSONDecodeError:
            print(f"AI failed to return valid JSON. Raw: {raw_content}")
            return "I apologize, I'm having trouble processing your query.", "Unknown"
        
    except requests.exceptions.RequestException as e:
        print(f"OpenRouter API Error: {e}")
        return "I am experiencing connectivity issues right now. Please try again later.", "Unknown"
    except Exception as e:
        print(f"General Error in query_openrouter: {e}")
        return "Sorry, there was a problem processing your request.", "Unknown"


# --- TELEGRAM HANDLERS ---
# (start and handle_message remain the same)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Reset state and prompt for patient identification info
    context.user_data[STATE_KEY] = STATE_AWAITING_INFO
    context.user_data[HISTORY_KEY] = []
    await update.message.reply_text(
        "👋 Welcome to Indra Clinic!\n\nI’m Indie, your assistant.\n\nPlease enter your **full name, date of birth (DD/MM/YYYY), and email**, separated by commas, to begin (e.g., *Jane Doe, 01/01/1980, jane@example.com*):"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    # chat_id = update.effective_chat.id # Use if needed
    
    if context.user_data.get(STATE_KEY) == STATE_AWAITING_INFO:
        # Initial information gathering
        try:
            name, dob, email = [item.strip() for item in user_message.split(',', 2)]
            
            # Simple validation check
            if '@' not in email or len(name) < 3 or len(dob) < 8:
                 raise ValueError("Validation failed")
            
            context.user_data[FULL_NAME_KEY] = name
            context.user_data[DOB_KEY] = dob
            context.user_data[EMAIL_KEY] = email
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            context.user_data[HISTORY_KEY] = [{"role": "patient", "text": user_message}] # Start history
            
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
        
        # Get response and category from AI
        await update.message.chat.send_action("typing") # Show typing indicator
        ai_response_text, category = query_openrouter(patient_info, context.user_data[HISTORY_KEY])
        
        # Append bot response to history
        context.user_data[HISTORY_KEY].append({"role": "indie", "text": ai_response_text})
        
        # Send response to user
        await update.message.reply_text(ai_response_text)
        
        # --- REPORTING TRIGGER ---
        print(f"AI CATEGORIZED QUERY: {category}")
        if category in WORKFLOWS:
            generate_report_and_send_email(patient_info, context.user_data[HISTORY_KEY], category)
        # --- END REPORTING TRIGGER ---
        
    else:
        # If state is corrupted, restart the flow
        await start(update, context)


def start_bot_loop():
    """Builds the application and starts the polling loop safely."""
    try:
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    except InvalidToken:
        print("FATAL ERROR: The TELEGRAM_TOKEN is invalid. Please check your environment variables.")
        sys.exit(1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Use the explicit start/idle method for better environment compatibility
    app.run_polling(poll_interval=1.0, timeout=10)
    
    print("Bot polling initiated and running.")


# Main function
def main():
    # Print Python version for debug
    print(f"--- RENDER ENVIRONMENT DEBUG ---")
    print(f"Running with Python Version: {sys.version}")
    print(f"--- RENDER ENVIRONMENT DEBUG ---")
    
    start_bot_loop()


if __name__ == "__main__":
    # Signal handling is complex in Render Workers, but run_polling is generally the right call.
    # We will stick to the revised run_polling with explicit arguments.
    main()
