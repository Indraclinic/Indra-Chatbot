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


# --- MODIFICATION --- Using the correct createFreeTextRecord mutation
async def push_to_semble(patient_email: str, category: str, summary: str, transcript: str):
    """Finds a patient by email using GraphQL, then pushes a new FreeTextRecord."""
    if not SEMBLE_API_KEY:
        raise ValueError("Semble API Key is not configured on the server.")

    SEMBLE_GRAPHQL_URL = "https://open.semble.io/graphql"
    headers = {"x-token": SEMBLE_API_KEY, "Content-Type": "application/json"}
    
    find_patient_query = """
      query FindPatientByEmail($search: String!) {
        patients(search: $search) {
          data { id }
        }
      }
    """
    
    async with httpx.AsyncClient() as client:
        try:
            # Step 1: Find the patient by their email address (this part was already working)
            find_payload = {"query": find_patient_query, "variables": {"search": patient_email}}
            search_response = await client.post(SEMBLE_GRAPHQL_URL, headers=headers, json=find_payload, timeout=20)
            search_response.raise_for_status()
            response_data = search_response.json()
            if response_data.get("errors"): raise Exception(f"GraphQL error during patient search: {response_data['errors']}")
            patients = response_data.get('data', {}).get('patients', {}).get('data', [])
            if not patients: raise Exception(f"No patient found in Semble with email: {patient_email}")
            semble_patient_id = patients[0]['id']
            print(f"Found Semble Patient ID: {semble_patient_id}")

            # Step 2: Create the FreeTextRecord using the correct mutation
            create_record_mutation = """
                mutation CreateFreeTextRecord($patientId: ID!, $question: String!, $answer: String!) {
                    createFreeTextRecord(patientId: $patientId, input: {question: $question, answer: $answer}) {
                        data { id }
                        error
                    }
                }
            """
            note_question = f"Indie Bot Query: {category}"
            note_answer = (f"**AI Summary:**\n{summary}\n\n--- Full Conversation Transcript ---\n{transcript}")
            mutation_variables = {"patientId": semble_patient_id, "question": note_question, "answer": note_answer}
            
            consult_payload = {"query": create_record_mutation, "variables": mutation_variables}
            consult_response = await client.post(SEMBLE_GRAPHQL_URL, headers=headers, json=consult_payload, timeout=20)
            consult_response.raise_for_status()
            consult_data = consult_response.json()
            if consult_data.get("errors") or (consult_data.get("data", {}).get("createFreeTextRecord") or {}).get("error"):
                 raise Exception(f"GraphQL error during record creation: {consult_data}")

            print(f"Successfully pushed FreeTextRecord to Semble for Patient ID: {semble_patient_id}")

        except Exception as e:
            raise e

# --- REPORTING AND INTEGRATION FUNCTIONS ---
def generate_report_and_send_email(dob: str, patient_email: str, session_id: str, history: list, category: str, summary: str):
    transcript_content = f"Full Conversation Transcript (Session: {session_id})\n\n"
    if not history:
        transcript_content += f"[SYSTEM]: User followed a guided workflow.\n[SUMMARY]: {summary}\n"
    else:
        for message in history:
            transcript_content += f"[{message['role'].upper()}]: {message['text']}\n"
    
    if not all([SMTP_USERNAME, SMTP_PASSWORD, SMTP_SERVER, SENDER_EMAIL]):
        raise ValueError("SMTP configuration is incomplete on the server.")

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
