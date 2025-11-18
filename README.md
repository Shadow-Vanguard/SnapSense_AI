# SnapMind

AI-powered screenshot reminder system that automatically extracts information from screenshots and sends reminders via desktop notifications and Telegram.

## Installation

```bash
pip install -r requirements.txt
```

## Setup

### 1. Get Gemini API Key

1. Go to [Google AI Studio](https://aistudio.google.com)
2. Sign in with your Google account
3. Click **Get API key** or **API keys** in the sidebar
4. Click **Create API key**
5. Copy your API key (starts with `AIza...`)

### 2. Get Telegram Bot Token

1. Open Telegram and search for **@BotFather**
2. Send `/start` command
3. Send `/newbot` command
4. Follow the prompts to name your bot
5. Copy the token provided (format: `123456789:ABC...`)

### 3. Get Telegram Chat ID

1. Start a chat with your bot (search for your bot's username)
2. Send any message to the bot
3. Open this URL in your browser (replace `YOUR_BOT_TOKEN` with your actual token):
   ```
   https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates
   ```
4. Look for `"chat":{"id":123456789}` in the JSON response
5. Copy that number - that's your Chat ID

### 4. Create .env File

Create a `.env` file in the project directory with:

```env
GEMINI_API_KEY=your_gemini_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
SCREENSHOT_DIR=Q:\images\Screenshots 1
```

## Run

```bash
cd q:\AIGENESIS
python snapmind.py
```

## Usage

1. Take a screenshot (saves to your `SCREENSHOT_DIR`)
2. SnapMind automatically detects and analyzes it
3. You'll receive:
   - Desktop notification
   - Telegram message with reminder buttons
   - Calendar event (if flight/meeting detected)
