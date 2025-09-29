import os
import sys
import signal # Required for clean shutdown handling
import time # Import time for a small delay
import re # For robust JSON extraction
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import InvalidToken, Conflict
import requests
import json 
import smtplib
from email.message import EmailMessage
import ast # For robust JSON parsing

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

def generate_report_and_send_email(patient_info: dict, history: list, category: str, summary: str):
    """Generates report, sends email, and simulates EMR push, now including the AI-generated summary."""
    
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

    # Build the report content, starting with the summary
    report_content = f"--- INDRA CLINIC BOT REPORT ---\n"
    report_content += f"Category: {category}\n"
    report_content += f"Patient Name: {patient_info.get(FULL_NAME_KEY)}\n"
    report_content += f"DOB: {patient_info.get(DOB_KEY)}\n"
    report_content += f"Email: {patient_info.get(EMAIL_KEY)}\n"
    report_content += f"----------------------------------\n\n"
    report_content += f"*** AI ACTION SUMMARY ***\n{summary}\n\n"
    report_content += "FULL CONVERSATION TRANSCRIPT:\n"
    
    for message in history:
        report_content += f"[{message['role'].upper()}]: {message['text']}\n"
    
    # 2. SEMBLE EMR PUSH (Placeholder)
    print(f"--- SEMBLE EMR PUSH SIMULATION (Data for {category}) ---")
    print(f"Summary Pushed to EMR: {summary}")
    
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

def query_openrouter(patient_info: dict, history: list) -> tuple[str, str, str]:
    """Queries OpenRouter to get the bot's response, categorize the workflow, AND generate a summary."""
    
    # Configuration for exponential backoff
    MAX_RETRIES = 3
    
    # ... (OpenRouter logic remains the same) ...
    patient_context = f"Patient Name: {patient_info.get(FULL_NAME_KEY)}, DOB: {patient_info.get(DOB_KEY)}, Email: {patient_info.get(EMAIL_KEY)}"
    current_user_message = history[-1]['text']
    
    # --- SYSTEM PROMPT FOR UK ENGLISH & FACT-FINDING AND SUMMARY REQUEST ---
    system_prompt = (
        "You are Indie, a helpful assistant for Indra Clinic. Respond using concise UK English. Do not offer medical advice. "
        "Your primary task is to respond to the patient and categorize their query into one of three strict categories: 'Admin', 'Prescription/Medication', or 'Clinical/Medical'. "
        
        "Crucially, if the patient's query lacks necessary details (e.g., specific date/time for admin, symptoms for clinical, product name for prescription), "
        "you MUST ask a follow-up question in your response to gather the missing facts. Only categorize when the query is complete enough for a staff member to action it, "
        "or if it is a definite clinical emergency (in which case, redirect them appropriately)."
        
        "Finally, you MUST generate a **SUMMARY** of the entire conversation so far for staff to quickly review."
        
        f"Patient ID: {patient_context}. Keep responses professional, concise, and focused on gathering necessary information for the relevant team."
        "If the query is Clinical/Medical, state clearly that a specialist nurse will review the transcript and be in touch soon. "
        "Your response MUST be formatted as a single JSON object with the keys 'response' (the text for the user), 'category' (one of the three strict categories), and 'summary' (a brief summary of the query for staff)."
        "Example: {'response': 'Thank you. Can you please confirm the exact name of the product...', 'category': 'Prescription/Medication', 'summary': 'Patient has requested a repeat prescription but did not specify the product name.'}"
    )
    # --- END SYSTEM PROMPT ---

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": current_user_message}
    ]
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    
    data = {
        # --- MODEL SWITCHED TO GPT-3.5-TURBO-1106 FOR COST SAVINGS ---
        "model": "openai/gpt-3.5-turbo-1106", 
        "messages": messages,
    }
    
    # *** IMPORTANT NOTE: If you experience renewed JSON errors, switch back to "anthropic/claude-3-haiku" ***

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=15) 
            
            # Successful response
            if response.status_code == 200:
                raw_content = response.json()["choices"][0]["message"]["content"]
                
                try:
                    # --- ULTRA-ROBUST JSON EXTRACTION ---
                    # Regex to find the first '{' and the last '}' to isolate the core JSON object.
                    start_index = raw_content.find('{')
                    end_index = raw_content.rfind('}')
                    
                    json_string = ""
                    if start_index != -1 and end_index != -1 and end_index > start_index:
                        # Slice the raw content to isolate the suspected JSON string
                        json_string = raw_content[start_index : end_index + 1]
                    else:
                        json_string = raw_content 
                    
                    # 2. Use ast.literal_eval fallback for single quotes
                    if json_string.startswith('{') and json_string.endswith('}'):
                        try:
                            # Safely evaluate as a Python literal (handles single quotes)
                            parsed_dict = ast.literal_eval(json_string)
                            # Convert back to JSON string format for reliable json.loads
                            json_string = json.dumps(parsed_dict)
                        except (ValueError, SyntaxError):
                            # If literal_eval fails, pass the string as-is for json.loads
                            pass
                    
                    parsed_json = json.loads(json_string)
                    # --- END ULTRA-ROBUST JSON EXTRACTION ---
                    
                    category = parsed_json.get('category', 'Unknown')
                    if category not in WORKFLOWS:
                        category = 'Unknown' 
                        
                    response_text = parsed_json.get('response', "I am unable to formulate a response right now.")
                    summary_text = parsed_json.get('summary', 'No summary generated by AI.')
                        
                    return response_text, category, summary_text
                
                except json.JSONDecodeError:
                    print(f"AI failed to return valid JSON. Raw: {raw_content}")
                    print(f"Extracted string: {json_string}")
                    # This is the point of failure. Returns the generic message.
                    return "I apologize, I'm having trouble processing your query.", "Unknown", "JSON parsing failed."

            # Failure Response (Authentication, Rate Limit, Server Error)
            elif response.status_code == 402:
                 # Specific handling for 402 Insufficient Credit error
                error_details = response.json().get('error', {}).get('message', 'No details provided.')
                print(f"OPENROUTER FATAL ERROR: Status Code 402 (Insufficient Credits). Details: {error_details}")
                return "CRITICAL ERROR: The AI service reports insufficient credits. Please check your OpenRouter account billing.", "Unknown", "CRITICAL BILLING FAILURE."
            
            elif response.status_code in (401, 403):
                print(f"OPENROUTER FATAL ERROR: Status Code {response.status_code}. Details: {response.text}")
                return "ERROR: Authentication failed. Please check the OPENROUTER_API_KEY.", "Unknown", "Auth Failure."

            elif response.status_code in (429, 500, 502, 503, 504):
                if attempt < MAX_RETRIES - 1:
                    wait_time = 2 ** attempt
                    print(f"OPENROUTER RETRYABLE ERROR: Status Code {response.status_code}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    print(f"OPENROUTER FAILED after {MAX_RETRIES} attempts. Status Code {response.status_code}. Details: {response.text}")
                    return "Sorry, the AI service is currently unavailable or busy. Please try again.", "Unknown", "Service Unavailable."
            
            else:
                print(f"OPENROUTER NON-RETRYABLE ERROR: Status Code {response.status_code}. Details: {response.text}")
                return "Sorry, the AI service is currently unavailable or busy. Please try again.", "Unknown", "API Error."

        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                wait_time = 2 ** attempt
                print(f"OpenRouter Network Error: {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                print(f"OpenRouter Network/Timeout FAILED after {MAX_RETRIES} attempts. Error: {e}")
                return "I am experiencing connectivity issues right now. Please try again later.", "Unknown", "Network Timeout."
        
        except Exception as e:
            print(f"General Error in query_openrouter: {e}")
            return "Sorry, there was a problem processing your request.", "Unknown", "Unhandled Code Error."

    return "Sorry, a final critical error occurred.", "Unknown", "Final Fallback."


# --- TELEGRAM HANDLERS ---
# (start remains the same)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Reset state and prompt for patient identification info
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
        
        # Get response, category, AND SUMMARY from AI
        await update.message.chat.send_action("typing") # Show typing indicator
        ai_response_text, category, report_summary = query_openrouter(patient_info, context.user_data[HISTORY_KEY])
        
        # Append bot response to history
        context.user_data[HISTORY_KEY].append({"role": "indie", "text": ai_response_text})
        
        # Send response to user
        await update.message.reply_text(ai_response_text)
        
        # --- REPORTING TRIGGER ---
        print(f"AI CATEGORIZED QUERY: {category}")
        if category in WORKFLOWS:
            # Pass the new summary to the report function
            generate_report_and_send_email(patient_info, context.user_data[HISTORY_KEY], category, report_summary)
        # --- END REPORTING TRIGGER ---
        
    else:
        # If state is corrupted, restart the flow
        await start(update, context)


def telegram_cleanup(token):
    """Synchronously attempts to delete any lingering webhooks."""
    try:
        url = f"https://api.telegram.org/bot{token}/deleteWebhook"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        result = response.json()
        if result.get('ok') and result.get('result'):
            print("Telegram cleanup successful: Previous webhook/polling session terminated.")
        else:
            print("Telegram cleanup attempted. No active webhook found (This is expected for polling).")

    except requests.exceptions.RequestException as e:
        print(f"Telegram cleanup request failed: {e}. Attempting to proceed with polling.")
    except Exception as e:
        print(f"General error during Telegram cleanup: {e}")


def start_bot_loop():
    """Builds the application and starts the polling loop safely."""
    
    # 1. CLEANUP STEP: Kill any previous polling/webhook sessions
    telegram_cleanup(TELEGRAM_TOKEN)
    time.sleep(1) # Wait briefly for the API change to register

    try:
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    except InvalidToken:
        print("FATAL ERROR: The TELEGRAM_TOKEN is invalid. Please check your environment variables.")
        sys.exit(1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # 2. Start polling
    try:
        app.run_polling(poll_interval=1.0, timeout=10)
        print("Bot polling initiated and running.")
    except Conflict as e:
        print(f"FATAL CONFLICT ERROR: {e}")
        print("This means another bot instance with the same token is still running elsewhere.")
        print("Please ensure ALL previous Render Workers and local instances are stopped or deleted.")
        sys.exit(1) # Force exit if conflict occurs


# Main function
def main():
    # Print Python version for debug
    print(f"--- RENDER ENVIRONMENT DEBUG ---")
    print(f"Running with Python Version: {sys.version}")
    print(f"--- RENDER ENVIRONMENT DEBUG ---")
    
    start_bot_loop()


if __name__ == "__main__":
    main()
