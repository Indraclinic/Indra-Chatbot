import os
import sys
import signal # Required for clean shutdown handling
import time # Import time for a small delay
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import InvalidToken, Conflict
import requests
import json 
import smtplib
from email.message import EmailMessage
import ast # <-- NEW: For robust JSON parsing

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
    
    # --- UPDATED SYSTEM PROMPT FOR UK ENGLISH & FACT-FINDING AND SUMMARY REQUEST ---
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
    # --- END UPDATED SYSTEM PROMPT ---

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

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post("[https://openrouter.ai/api/v1/chat/completions](https://openrouter.ai/api/v1/chat/completions)", headers=headers, json=data, timeout=15) 
            
            # Successful response
            if response.status_code == 200:
                raw_content = response.json()["choices"][0]["message"]["content"]
                
                try:
                    # --- JSON ROBUSTNESS IMPROVEMENT ---
                    cleaned_content = raw_content.strip()
                    
                    # FIX APPLIED HERE: The string slice is now correctly terminated.
                    if cleaned_content.startswith("```json"):
                         cleaned_content = cleaned_content[len("
