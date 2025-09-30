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
        raise ValueError("Semble API Key is not configured on the server.")

    headers = {"Authorization": f"Bearer {SEMBLE_API_KEY}", "Content-Type": "application/json", "Accept": "application/json"}
    
    async with httpx.AsyncClient() as client:
        patient_search_url = f"https://api.semble.io/v1/patients/search?email={patient_email}"
        search_response = await client.get(patient_search_url, headers=headers, timeout=20)
        search_response.raise_for_status()
        
        patients = search_response.json().get('data', [])
        if not patients:
            raise Exception(f"No patient found in Semble with email: {patient_email}")
        
        semble_patient_id = patients[0]['id']
        print(f"Found Semble Patient ID: {semble_patient_id} for email: {patient_email}")

        consultation_url = f"https://api.semble.io/v1/patients/{semble_patient_id}/consultations"
        note_body = (f"**Indie Bot AI Summary:**\n{summary}\n\n--- Full Conversation Transcript ---\n{transcript}")
        note_data = {"body": note_body}
        
        consult_response = await client.post(consultation_url, headers=headers, json=note_data, timeout=20)
        consult_response.raise_for_status()
        print(f"Successfully pushed note to Semble for Patient ID: {semble_patient_id}")

def generate_report_and_send_email(dob: str, patient_email: str, session_id: str, history: list, category: str, summary: str):
    """Generates and sends reports to the admin and a confirmation to the patient."""
    transcript_content = f"Full Conversation Transcript (Session: {session_id})\n\n"
    if not history:
        transcript_content += f"[SYSTEM]: User followed a guided workflow.\n[SUMMARY]: {summary}\n"
    else:
        for message in history:
            transcript_content += f"[{message['role'].upper()}]: {message['text']}\n"
    
    if not all([SMTP_USERNAME, SMTP_PASSWORD, SMTP_SERVER, SENDER_EMAIL]):
        raise ValueError("SMTP configuration is incomplete on the server. Check all SMTP and SENDER_EMAIL variables.")

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        
        # Email to Staff & Patient logic...
        # ... (This part is working, so keeping it concise for readability)
        admin_subject = f"[Indie Bot] {category} Query from: {patient_email} (DOB: {dob})"
        admin_msg = EmailMessage()
        admin_msg['Subject'] = admin_subject
        admin_msg['From'] = SENDER_EMAIL
        admin_msg['To'] = REPORT_EMAIL
        admin_msg.set_content(f"Query from {patient_email}...\n\n--- AI-Generated Summary ---\n{summary}")
        admin_msg.add_attachment(transcript_content.encode('utf-8'), maintype='text', subtype='plain', filename=f'transcript_{session_id[-6:]}.txt')
        server.send_message(admin_msg)
        print(f"Admin report successfully emailed to {REPORT_EMAIL}")
        
        patient_subject = "Indra Clinic: A copy of your recent query"
        patient_msg = EmailMessage()
        patient_msg['Subject'] = patient_subject
        patient_msg['From'] = SENDER_EMAIL
        patient_msg['To'] = patient_email
        patient_msg.set_content(f"Dear Patient,\n\nFor your records, here is a summary of your recent query.\n\n**Summary:**\n{summary}\n\nKind regards,\nThe Indra Clinic Team")
        patient_msg.add_attachment(transcript_content.encode('utf-8'), maintype='text', subtype='plain', filename=f'transcript_summary.txt')
        server.send_message(patient_msg)
        print(f"Patient copy successfully emailed to {patient_email}")
    
    return transcript_content

def query_openrouter(history: list) -> tuple[str, str, str, str]:
    # This function is unchanged
    system_prompt = "..." # Assuming the long prompt is here
    # ...
    return "Example response", "Admin", "Example summary", "CONTINUE" # Placeholder

# --- TELEGRAM HANDLERS & CONVERSATION FLOW ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This function is unchanged
    context.user_data.clear()
    # ...
    await update.message.reply_text("👋 Welcome to Indra Clinic!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The main state machine for handling all user messages."""
    current_state = context.user_data.get(STATE_KEY)
    user_message = update.message.text.strip()
    
    # ... (All states from AWAITING_
