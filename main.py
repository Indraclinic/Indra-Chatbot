import os
import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)

# Load environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Safety checks
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN is not set.")
if not OPENROUTER_API_KEY:
    print("⚠️ OPENROUTER_API_KEY is not set — /ask command will not work.")

# /start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Indra Clinic!\n\nI’m Indie, your assistant.\n\nUse /ask followed by a question to get started."
    )

# /ask command handler
async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OPENROUTER_API_KEY:
        await update.message.reply_text("❌ OpenRouter API key not configured.")
        return

    user_input = " ".join(context.args)
    if not user_input:
        await update.message.reply_text("Please provide a question after /ask.")
        return

    # Call OpenRouter
    try:
        response = await query_openrouter(user_input)
        await update.message.reply_text(response)
    except Exception as e:
        print(f"Error querying OpenRouter: {e}")
        await update.message.reply_text("⚠️ Something went wrong. Please try again later.")

# Function to call OpenRouter API
async def query_openrouter(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    body = {
        "model": "openai/gpt-3.5-turbo",
        "messages": [
            {"role": "system", "content": "You are Indie, a helpful assistant for Indra Clinic. Do not give medical advice. Help with admin and clinic info."},
            {"role": "user", "content": prompt}
        ]
    }

    async with httpx.AsyncClient() as client:
        response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            raise RuntimeError(f"OpenRouter API error: {response.status_code} - {response.text}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ask", ask))

    print("✅ Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
