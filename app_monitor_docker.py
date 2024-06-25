import os
import subprocess
import threading
import telebot
import logging
import time
from dotenv import load_dotenv

# Load environment variables from .env file
dotenv_path = "./config/.env"
if not os.path.exists(dotenv_path):
    raise Exception(f".env file not found at path: {dotenv_path}")

load_dotenv(dotenv_path=dotenv_path)

TELE_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELE_CHAT_ID = os.getenv("CHAT_ID")

if not TELE_TOKEN:
    raise Exception("TELEGRAM_BOT_TOKEN is not defined in the .env file")

if not TELE_CHAT_ID:
    raise Exception("CHAT_ID is not defined in the .env file")

TELEGRAM_BOT_TOKEN = TELE_TOKEN
CHAT_ID = TELE_CHAT_ID

print(f"TELEGRAM_BOT_TOKEN: {TELEGRAM_BOT_TOKEN}")
print(f"CHAT_ID: {CHAT_ID}")

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
monitoring_process = None

COMMAND_FILE = '/tmp/monitor_commands.txt'
STATUS_FILE = '/tmp/monitor_status.txt'
CONTAINER_FILE = '/tmp/container_logs.txt'

def initialize_files():
    # Clean up the temporary files
    if os.path.exists(COMMAND_FILE):
        os.remove(COMMAND_FILE)
    if os.path.exists(STATUS_FILE):
        os.remove(STATUS_FILE)
    if os.path.exists(CONTAINER_FILE):
        os.remove(CONTAINER_FILE)
    logging.info("Temporary files cleaned up.")

def start_monitoring():
    global monitoring_process
    if monitoring_process is None or monitoring_process.poll() is not None:
        monitoring_process = subprocess.Popen(['python', 'monitor_docker.py'])
        logging.info("Monitoring script started.")
        bot.send_message(CHAT_ID, "Monitoring script started.")
    else:
        logging.info("Monitoring script is already running.")
        bot.send_message(CHAT_ID, "Monitoring script is already running.")

def stop_monitoring():
    global monitoring_process
    if monitoring_process is not None and monitoring_process.poll() is None:
        monitoring_process.terminate()
        monitoring_process.wait()
        logging.info("Monitoring script stopped.")
        bot.send_message(CHAT_ID, "Monitoring script stopped.")
        monitoring_process = None
    else:
        logging.info("Monitoring script is not running.")
        bot.send_message(CHAT_ID, "Monitoring script is not running.")

def send_command(command):
    with open(COMMAND_FILE, 'w') as f:
        f.write(command)
    logging.info(f"Sent command: {command}")

def read_status_with_polling():
    timeout = 10  # Maximum time to wait in seconds
    start_time = time.time()
    while time.time() - start_time < timeout:
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, 'r') as f:
                status = f.read().strip()
            os.remove(STATUS_FILE)  # Clear status file after reading
            return status
        time.sleep(5)  # Check every 3 minutes
    return "Failed to get status: Timeout"

@bot.message_handler(commands=['start'])
def handle_start(message):
    start_monitoring()

@bot.message_handler(commands=['shutdown'])
def handle_shutdown(message):
    stop_monitoring()

@bot.message_handler(commands=['silence'])
def handle_silence(message):
    try:
        _, container_name, duration = message.text.split()
        command = f'silence {container_name} {duration}'
        send_command(command)
        bot.reply_to(message, f"Silencing {container_name} for {duration} minutes.")
    except ValueError:
        bot.reply_to(message, "Invalid command. Use: /silence <container_name> <duration_minutes>")

@bot.message_handler(commands=['unsilence'])
def handle_unsilence(message):
    try:
        _, container_name = message.text.split()
        command = f'unsilence {container_name}'
        send_command(command)
        bot.reply_to(message, f"Unsilencing {container_name}.")
    except ValueError:
        bot.reply_to(message, "Invalid command. Use: /unsilence <container_name>")

@bot.message_handler(commands=['status'])
def handle_status(message):
    send_command('status')
    time.sleep(5)
    status = read_status_with_polling()
    bot.reply_to(message, f"Status:\n{status}")

@bot.message_handler(commands=['list'])
def handle_list(message):
    send_command('list')
    status = read_status_with_polling()
    bot.reply_to(message, f"Active containers:\n{status}")

@bot.message_handler(commands=['disk_usage'])
def handle_disk_usage(message):
    send_command('disk_usage')
    time.sleep(5)
    status = read_status_with_polling()
    bot.reply_to(message, f"Disk usage information:\n{status}")

@bot.message_handler(commands=['log_clear'])
def handle_log_clear(message):
    send_command('log_clear')
    status = read_status_with_polling()
    bot.reply_to(message, f"Log clear status:\n{status}")


@bot.message_handler(commands=['help'])
def handle_help(message):
    help_text = """
    Available commands:
    /start - Start the monitoring script
    /shutdown - Stop the monitoring script
    /silence <container_name> <duration_minutes> - Silence alerts for a specific container for a set duration
    /unsilence <container_name> - Remove silencing for a specific container
    /status - Show the current status
    /list - List all active container names
    /log_clear - Truncate MongoDB logs
    /disk_usage - Show disk usage information
    /help - Show this help message
    """
    bot.reply_to(message, help_text)

def send_telegram_message(message):
    try:
        bot.send_message(TELE_CHAT_ID, message)
    except Exception as e:
        logging.error(f"Failed to send message: {str(e)}")

def check_and_send_container_logs():
    while True:  # This loop will continuously check the logs
        if os.path.exists(CONTAINER_FILE):
            with open(CONTAINER_FILE, 'r') as file:
                logs = file.read().strip()
            os.remove(CONTAINER_FILE)  # Optionally clear the file after reading
            if logs:
                logging.info("Log Errors Found. Sent Telegram Message.")
                send_telegram_message(f"Container Logs: \n{logs}")
        time.sleep(5)  # Wait for 3 minutes before checking again

def main():
    logging.info("Parent script started.")
    initialize_files()  # Clean up temporary files
    start_monitoring()  # Automatically start monitoring when the parent script starts

    # Create and start the thread for checking container logs
    log_thread = threading.Thread(target=check_and_send_container_logs)
    log_thread.start()

    # Start Telegram bot polling in the main thread
    try:
        bot.polling(none_stop=True)
    except Exception as e:
        logging.error(f"An error occurred: {str(e)}")
        bot.stop_polling()

if __name__ == "__main__":
    main()
