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


# --- MODIFICATION --- Rewrote Semble push to use GraphQL
async def push_to_semble(patient_email: str, summary: str, transcript: str):
    """Finds a patient by email using GraphQL, then pushes a new consultation note."""
    if not SEMBLE_API_KEY:
        print("--- SEMBLE ERROR: SEMBLE_API_KEY environment variable not set. EMR push aborted. ---")
        return

    SEMBLE_GRAPHQL_URL = "https://open.semble.io/graphql"
    headers = {"x-token": SEMBLE_API_KEY, "Content-Type": "application/json"}
    
    # Define the GraphQL query to find a patient by email
    find_patient_query = """
      query FindPatientByEmail($search: String!) {
        patients(search: $search) {
          data {
            id
          }
        }
      }
    """
    
    async with httpx.AsyncClient() as client:
        try:
            # Step 1: Find the patient by their email address
            print(f"Searching for patient with email: {patient_email}")
            find_payload = {"query": find_patient_query, "variables": {"search": patient_email}}
            search_response = await client.post(SEMBLE_GRAPHQL_URL, headers=headers, json=find_payload, timeout=20)
            search_response.raise_for_status()
            
            response_data = search_response.json()
            if response_data.get("errors"):
                raise Exception(f"GraphQL error during patient search: {response_data['errors']}")

            patients = response_data.get('data', {}).get('patients', {}).get('data', [])
            if not patients:
                raise Exception(f"No patient found in Semble with email: {patient_email}")
            
            semble_patient_id = patients[0]['id']
            print(f"Found Semble Patient ID: {semble_patient_id}")

            # Step 2: Create the consultation note mutation
            # Note: We need the exact mutation from Semble docs. This is a plausible guess.
            create_consultation_mutation = """
                mutation CreateConsultation($patientId: ID!, $body: String!) {
                    createConsultation(patientId: $patientId, input: {body: $body}) {
                        data {
                            id
                        }
                        error
                    }
                }
            """
            note_body = (f"**Indie Bot AI Summary:**\n{summary}\n\n--- Full Conversation Transcript ---\n{transcript}")
            mutation_variables = {"patientId": semble_patient_id, "body": note_body}
            
            consult_payload = {"query": create_consultation_mutation, "variables": mutation_variables}
            consult_response = await client.post(SEMBLE_GRAPHQL_URL, headers=headers, json=consult_payload, timeout=20)
            consult_response.raise_for_status()

            consult_data = consult_response.json()
            if consult_data.get("errors") or (consult_data.get("data", {}).get("createConsultation") or {}).get("error"):
                 raise Exception(f"GraphQL error during consultation creation: {consult_data}")

            print(f"Successfully pushed note to Semble for Patient ID: {semble_patient_id}")

        except Exception as e:
            print(f"--- CRITICAL SEMBLE ERROR: {e} ---")

# --- REPORTING AND INTEGRATION FUNCTIONS ---
def generate_report_and_send_email(dob: str, patient_email: str, session_id: str, history: list, category: str, summary: str):
    transcript_content = f"Full Conversation Transcript (Session: {session_id})\n\n"
    if not history:
        transcript_content += f"[SYSTEM]: User followed a guided workflow.\n[SUMMARY]: {summary}\n"
    else:
        for message in history:
            transcript_content += f"[{message['role'].upper()}]: {message['text']}\n"
    
    try:
        if not all([SMTP_USERNAME, SMTP_PASSWORD, SMTP_SERVER, SENDER_EMAIL]):
            print("Email skipped: SMTP configuration is incomplete.")
            return transcript_content

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            
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

    except Exception as e:
        print(f"EMAIL DISPATCH FAILED: {e}")
    
    return transcript_content

# ... (query_openrouter function is unchanged) ...
def query_openrouter(history: list) -> tuple[str, str, str, str]:
    system_prompt = textwrap.dedent("""\
