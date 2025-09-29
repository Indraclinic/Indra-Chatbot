import os
import sys # New: Import sys for environment debugging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import requests
import json # New: for structured data handling
# from firestore import save_patient_data, push_to_emr_api # Placeholder for integration logic

# Load environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Check that the tokens loaded correctly
if TELEGRAM_TOKEN is None:
    raise ValueError("TELEGRAM_TOKEN environment variable not set.")
if OPENROUTER_API_KEY is None:
    raise ValueError("OPENROUTER_API_KEY environment variable not set.")

# State Management (Simple example using context.user_data)
FULL_NAME_KEY = 'full_name'
DOB_KEY = 'dob'
EMAIL_KEY = 'email'
STATE_KEY = 'conversation_state'
STATE_AWAITING_INFO = 'awaiting_info'
STATE_CHAT_ACTIVE = 'chat_active'

# Define command handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Reset state and prompt for patient identification info
    context.user_data[STATE_KEY] = STATE_AWAITING_INFO
    await update.message.reply_text(
        "👋 Welcome to Indra Clinic!\n\nI’m Indie, your assistant.\n\nPlease enter your full name, date of birth (DD/MM/YYYY), and email, separated by commas, to begin (e.g., Jane Doe, 01/01/1980, jane@example.com):"
    )

# Function to query OpenRouter
def query_openrouter(patient_info: dict, message: str) -> str:
    # Use patient info in the system prompt for context and reporting
    patient_context = f"Patient Name: {patient_info.get(FULL_NAME_KEY)}, DOB: {patient_info.get(DOB_KEY)}, Email: {patient_info.get(EMAIL_KEY)}"
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Define the system prompt with workflow guidance
    system_prompt = (
        "You are Indie, a helpful assistant for Indra Clinic. "
        "Your primary goal is to categorize and gather information for workflows: 'Admin', 'Prescription/Medication', or 'Clinical/Medical'. "
        "Do not offer medical advice. If the user has a clinical/medical concern, state you are escalating it to a specialist nurse. "
        f"Patient ID: {patient_context}. Keep responses professional and concise."
    )
    
    data = {
        "model": "openai/gpt-3.5-turbo", # Cost-effective model for simple routing
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message}
        ]
    }

    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=10)
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        return response.json()["choices"][0]["message"]["content"]
    except requests.exceptions.RequestException as e:
        print(f"OpenRouter API Error: {e}")
        return "I am experiencing connectivity issues right now. Please try again later."
    except Exception as e:
        print(f"General Error in query_openrouter: {e}")
        return "Sorry, there was a problem processing your request."

# Define message handler
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.effective_chat.id
    
    if context.user_data.get(STATE_KEY) == STATE_AWAITING_INFO:
        # Initial information gathering
        try:
            name, dob, email = [item.strip() for item in user_message.split(',')]
            context.user_data[FULL_NAME_KEY] = name
            context.user_data[DOB_KEY] = dob
            context.user_data[EMAIL_KEY] = email
            context.user_data[STATE_KEY] = STATE_CHAT_ACTIVE
            context.user_data['chat_history'] = [{"role": "patient", "text": user_message}] # Start history
            
            await update.message.reply_text(
                f"Thank you, {name}. Your information has been securely noted. "
                "How can I assist you today? Please tell me if your query relates to **Admin**, a **Prescription/Medication** issue, or a **Clinical/Medical** concern."
            )
        except ValueError:
            await update.message.reply_text(
                "I couldn't parse your details. Please ensure you enter them in the exact format: **Full Name, DD/MM/YYYY, Email**"
            )
    
    elif context.user_data.get(STATE_KEY) == STATE_CHAT_ACTIVE:
        # Active chat session
        context.user_data['chat_history'].append({"role": "patient", "text": user_message})
        
        patient_info = {
            FULL_NAME_KEY: context.user_data.get(FULL_NAME_KEY),
            DOB_KEY: context.user_data.get(DOB_KEY),
            EMAIL_KEY: context.user_data.get(EMAIL_KEY)
        }
        
        response = query_openrouter(patient_info, user_message)
        
        context.user_data['chat_history'].append({"role": "indie", "text": response})
        
        await update.message.reply_text(response)
        
        # --- EMR / Report Generation Logic Placeholder ---
        # This is where you would call your logic to check if a report needs pushing
        # For example, if the response indicates 'escalation' or the user explicitly asks for help.
        # This will be crucial for the Semble integration and email reports.
        # if "escalating it" in response or "prescription" in user_message.lower():
        #     generate_report_and_push_to_emr(chat_id, context.user_data['chat_history'])
        # --------------------------------------------------
    else:
        # Should not happen, but restarts the flow
        await start(update, context)

# Main function
def main():
    # --- Check Python Version for debugging Render environment ---
    print(f"--- RENDER ENVIRONMENT DEBUG ---")
    print(f"Running with Python Version: {sys.version}")
    print(f"--- RENDER ENVIRONMENT DEBUG ---")
    # -----------------------------------------------------------
    
    # The ApplicationBuilder is the modern approach and should be used
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # The run_polling() method is the recommended way to start the bot
    app.run_polling()

if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
