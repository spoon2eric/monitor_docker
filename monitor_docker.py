import docker
import telebot
import time
import logging
import re
import psutil
from datetime import datetime, timedelta
import threading
import subprocess

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Docker client
docker_client = docker.from_env()

# Telegram bot setup
TELEGRAM_BOT_TOKEN = 'TELE_BOT_TOKEN_HERE'
CHAT_ID = 'CHAT_ID_HERE'
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Error patterns to look for in logs
ERROR_PATTERNS = [
    r'error',
    r'exception',
    r'fail',
    r'critical',
]

# Patterns to ignore in logs
IGNORE_PATTERNS = [
    r'Failed to fetch price for ticker',
    r'No price fetched',
    r'use of closed network connection',
    r'Failed to list containers for docker',
    r'Cannot connect to docker server context canceled'
]

# Resource thresholds
CPU_THRESHOLD = 90  # percent
MEMORY_THRESHOLD = 90  # percent
DISK_THRESHOLD = 90  # percent

# Dictionary to store silenced alerts
silenced_alerts = {}


def silence_alert(container_name, duration_minutes):
    end_time = datetime.now() + timedelta(minutes=duration_minutes)
    silenced_alerts[container_name] = end_time
    return f"Alerts for container {container_name} silenced for {duration_minutes} minutes."


def unsilence_alert(container_name):
    if container_name in silenced_alerts:
        del silenced_alerts[container_name]
        return f"Alerts for container {container_name} unsilenced."
    else:
        return f"Container {container_name} was not silenced."


def is_silenced(container_name):
    if container_name in silenced_alerts:
        if datetime.now() < silenced_alerts[container_name]:
            return True
        else:
            del silenced_alerts[container_name]
    return False


def send_alert(container_name, message):
    if not is_silenced(container_name):
        bot.send_message(CHAT_ID, message)
        logging.warning(message)


def check_container_logs(container):
    try:
        logs = container.logs(tail=25).decode('utf-8')
        for pattern in ERROR_PATTERNS:
            matches = re.finditer(pattern, logs, re.IGNORECASE)
            for match in matches:
                context = logs[max(0, match.start() - 100):min(len(logs), match.end() + 100)]
                # Check if context contains any ignore patterns
                if any(re.search(ignore_pattern, context, re.IGNORECASE) for ignore_pattern in IGNORE_PATTERNS):
                    continue
                message = f"Error detected in container {container.name}:\n\n{context}"
                send_alert(container.name, message)
    except Exception as e:
        logging.error(f"Error checking logs for container {container.name}: {str(e)}")


def check_container_resources(container):
    try:
        stats = container.stats(stream=False)
        cpu_usage = stats['cpu_stats']['cpu_usage']['total_usage']
        system_cpu_usage = stats['cpu_stats']['system_cpu_usage']
        cpu_percent = (cpu_usage / system_cpu_usage) * 100

        memory_usage = stats['memory_stats']['usage']
        memory_limit = stats['memory_stats']['limit']
        memory_percent = (memory_usage / memory_limit) * 100

        if cpu_percent > CPU_THRESHOLD:
            message = f"High CPU usage detected in container {container.name}: {cpu_percent:.2f}%"
            send_alert(container.name, message)

        if memory_percent > MEMORY_THRESHOLD:
            message = f"High memory usage detected in container {container.name}: {memory_percent:.2f}%"
            send_alert(container.name, message)

    except Exception as e:
        logging.error(f"Error checking resources for container {container.name}: {str(e)}")


def check_disk_usage():
    disk_usage = psutil.disk_usage('/')
    if disk_usage.percent > DISK_THRESHOLD:
        message = f"High disk usage detected: {disk_usage.percent}%"
        send_alert('system', message)


def check_container_restarts(container):
    try:
        restart_count = container.attrs['RestartCount']
        if restart_count > 5:  # Adjust this threshold as needed
            message = f"Container {container.name} has restarted {restart_count} times"
            send_alert(container.name, message)
    except Exception as e:
        logging.error(f"Error checking restart count for container {container.name}: {str(e)}")


def check_image_version(container):
    try:
        image = container.image
        tags = image.tags
        if not tags or 'latest' in tags:
            message = f"Container {container.name} is using an untagged or 'latest' image. Consider using specific version tags."
            send_alert(container.name, message)
    except Exception as e:
        logging.error(f"Error checking image version for container {container.name}: {str(e)}")


def check_containers():
    for container in docker_client.containers.list():
        try:
            container.reload()
            if container.status == 'exited':
                logs = container.logs(tail=50).decode('utf-8')
                message = f"Container {container.name} has stopped. Last 50 lines of logs:\n\n{logs}"
                send_alert(container.name, message)
            else:
                check_container_logs(container)
                check_container_resources(container)
                check_container_restarts(container)
                check_image_version(container)
        except docker.errors.APIError as e:
            message = f"Error checking container {container.name}: {str(e)}"
            send_alert(container.name, message)
            logging.error(message)

    check_disk_usage()


@bot.message_handler(commands=['silence'])
def handle_silence(message):
    try:
        _, container_name, duration = message.text.split()
        duration = int(duration)
        response = silence_alert(container_name, duration)
        bot.reply_to(message, response)
    except ValueError:
        bot.reply_to(message, "Invalid command. Use: /silence <container_name> <duration_minutes>")


@bot.message_handler(commands=['unsilence'])
def handle_unsilence(message):
    try:
        _, container_name = message.text.split()
        response = unsilence_alert(container_name)
        bot.reply_to(message, response)
    except ValueError:
        bot.reply_to(message, "Invalid command. Use: /unsilence <container_name>")


@bot.message_handler(commands=['status'])
def handle_status(message):
    status = "Currently silenced alerts:\n"
    for container, end_time in silenced_alerts.items():
        remaining = end_time - datetime.now()
        status += f"{container}: silenced for {remaining.total_seconds() / 60:.1f} more minutes\n"
    bot.reply_to(message, status if len(silenced_alerts) > 0 else "No alerts are currently silenced.")


def restart_container(container_name):
    try:
        container = docker_client.containers.get(container_name)
        container.restart()
        return f"Container {container_name} has been restarted."
    except docker.errors.NotFound:
        return f"Container {container_name} not found."
    except Exception as e:
        return f"Error restarting container {container_name}: {str(e)}"


def get_container_names():
    return [container.name for container in docker_client.containers.list()]


@bot.message_handler(commands=['log_clear'])
def handle_log_clear(message):
    try:
        # Execute the truncate command
        result = subprocess.run(['sudo', 'truncate', '-s', '0', '/var/lib/docker/containers/75a6b3ac07e26190484b2244901b71c447e1431adb93c81717cb07095142e8d0/75a6b3ac07e26190484b2244901b71c447e1431adb93c81717cb07095142e8d0-json.log'],
                                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        bot.reply_to(message, "MongoDB logs have been truncated successfully.")
    except subprocess.CalledProcessError as e:
        bot.reply_to(message, f"Failed to truncate MongoDB logs: {e.stderr.decode()}")


@bot.message_handler(commands=['restart'])
def handle_restart(message):
    try:
        _, container_name = message.text.split()
        response = restart_container(container_name)
        bot.reply_to(message, response)
    except ValueError:
        bot.reply_to(message, "Invalid command. Use: /restart <container_name>")


@bot.message_handler(commands=['list'])
def handle_list(message):
    container_names = get_container_names()
    response = "Active containers:\n" + "\n".join(container_names)
    bot.reply_to(message, response)


@bot.message_handler(commands=['help'])
def handle_help(message):
    help_text = """
    Available commands:
    /silence <container_name> <duration_minutes> - Silence alerts for a specific container for a set duration
    /unsilence <container_name> - Remove silencing for a specific container
    /status - Show which alerts are currently silenced and for how long
    /restart <container_name> - Restart a specific container
    /list - List all active container names
    /log_clear - Truncate MongoDB logs
    /help - Show this help message
    """
    bot.reply_to(message, help_text)


def main():
    # Start the bot polling in a separate thread
    threading.Thread(target=bot.polling, daemon=True).start()

    while True:
        try:
            check_containers()
        except Exception as e:
            logging.error(f"An error occurred: {str(e)}")
        time.sleep(300)  # Check every 5 minutes


if __name__ == "__main__":
    main()
