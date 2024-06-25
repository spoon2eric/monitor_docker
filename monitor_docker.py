import os
from dotenv import load_dotenv
import docker
import time
import logging
import re
import psutil
from datetime import datetime, timedelta
import threading
import subprocess
from requests.exceptions import ReadTimeout, ConnectionError

# Load environment variables from .env file
dotenv_path = "./config/.env"
if not os.path.exists(dotenv_path):
    raise Exception(f".env file not found at path: {dotenv_path}")

load_dotenv(dotenv_path=dotenv_path)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Disable debug logging for the Docker client
docker_logger = logging.getLogger("urllib3.connectionpool")
docker_logger.setLevel(logging.WARNING)

# Docker client with increased timeout
docker_client = docker.from_env(timeout=600)

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
    r'Cannot connect to docker server context canceled',
    r'/lib/python2.7/site-packages/chameleon/py26.py'
]

# Resource thresholds
CPU_THRESHOLD = 90  # percent
MEMORY_THRESHOLD = 90  # percent
DISK_THRESHOLD = 90  # percent

# Dictionary to store silenced alerts
silenced_alerts = {}
shutdown_flag = threading.Event()

COMMAND_FILE = '/tmp/monitor_commands.txt'
STATUS_FILE = '/tmp/monitor_status.txt'
CONTAINER_FILE = '/tmp/container_logs.txt'

def initialize_files():
    # Clean up the temporary files
    if os.path.exists(COMMAND_FILE):
        os.remove(COMMAND_FILE)
    if os.path.exists(STATUS_FILE):
        os.remove(STATUS_FILE)
    logging.info("Temporary files cleaned up.")

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
    logging.warning(message)

def check_container_logs(container):
    # Check if the container is currently silenced
    if container.name in silenced_alerts:
        if datetime.now() < silenced_alerts[container.name]:
            # If the container is silenced and the silence period is still valid, skip checking logs
            return
        else:
            # If the silence period has expired, remove from the dictionary
            del silenced_alerts[container.name]

    try:
        logs = container.logs(tail=25).decode('utf-8')  # Fetch last 25 lines of logs
        for pattern in ERROR_PATTERNS:
            matches = re.finditer(pattern, logs, re.IGNORECASE)
            for match in matches:
                context = logs[max(0, match.start() - 100):min(len(logs), match.end() + 100)]
                if any(re.search(ignore_pattern, context, re.IGNORECASE) for ignore_pattern in IGNORE_PATTERNS):
                    continue  # Skip writing the log if it matches any ignore pattern

                # Compose the message to be sent as alert and logged
                message = f"Error detected in container {container.name}:\n\n{context}"
                send_alert(container.name, message)
                write_container_logs(container.name, ": Errors in Log")
    except (ReadTimeout, ConnectionError) as e:
        logging.error(f"Error checking logs for container {container.name}: {str(e)}")
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
            write_container_logs(container.name, ": CPU Threshold Too High")

        if memory_percent > MEMORY_THRESHOLD:
            message = f"High memory usage detected in container {container.name}: {memory_percent:.2f}%"
            send_alert(container.name, message)
            write_container_logs(container.name, ": Memory Threshold Too High")

    except (ReadTimeout, ConnectionError) as e:
        logging.error(f"Error checking resources for container {container.name}: {str(e)}")
    except Exception as e:
        logging.error(f"Error checking resources for container {container.name}: {str(e)}")

last_logged_usage = {}

def check_disk_usage():
    global last_logged_usage
    try:
        partitions = psutil.disk_partitions()
        usage_info = []
        for partition in partitions:
            if 'snap' in partition.mountpoint:
                continue
            usage = psutil.disk_usage(partition.mountpoint)
            current_usage = f"{partition.device} ({partition.mountpoint}): {usage.percent}% used ({usage.used / (1024 ** 3):.2f}GB of {usage.total / (1024 ** 3):.2f}GB)"

            # Log and update only if there is a significant change or if it's not previously logged
            if partition.device not in last_logged_usage or abs(last_logged_usage[partition.device] - usage.percent) > 5:  # threshold of 5% change
                logging.info("Disk usage information: " + current_usage)
                last_logged_usage[partition.device] = usage.percent

            usage_info.append(current_usage)
        
        return "\n".join(usage_info)
    except Exception as e:
        logging.error(f"Error checking disk usage: {str(e)}")
        return "Error getting disk usage"


def get_disk_usage():
    try:
        partitions = psutil.disk_partitions()
        usage_info = []
        for partition in partitions:
            if 'snap' in partition.mountpoint:
                continue
            usage = psutil.disk_usage(partition.mountpoint)
            usage_info.append(
                f"{partition.device} ({partition.mountpoint}): "
                f"{usage.percent}% used ({usage.used / (1024 ** 3):.2f}GB of "
                f"{usage.total / (1024 ** 3):.2f}GB)")
        logging.info("Disk usage information: " + "\n".join(usage_info))
        return "\n".join(usage_info)
    except Exception as e:
        logging.error(f"Error getting disk usage: {str(e)}")
        return "Error getting disk usage"

def check_container_restarts(container):
    try:
        restart_count = container.attrs['RestartCount']
        if restart_count > 3:  # Adjust this threshold as needed
            message = f"Container {container.name} has restarted {restart_count} times"
            send_alert(container.name, message)
            write_container_logs(container.name, ": Too Many Restarts")
    except (ReadTimeout, ConnectionError) as e:
        logging.error(f"Error checking restart count for container {container.name}: {str(e)}")
    except Exception as e:
        logging.error(f"Error checking restart count for container {container.name}: {str(e)}")

def check_image_version(container):
    try:
        image = container.image
        tags = image.tags
        if not tags or 'latest' in tags:
            message = f"Container {container.name} is using an untagged or 'latest' image. Consider using specific version tags."
            send_alert(container.name, message)
            write_container_logs(container.name, ": Image Check issue")
    except (ReadTimeout, ConnectionError) as e:
        logging.error(f"Error checking image version for container {container.name}: {str(e)}")
    except Exception as e:
        logging.error(f"Error checking image version for container {container.name}: {str(e)}")

def check_containers():
    for container in docker_client.containers.list():
        try:
            container.reload()
            if container.status == 'exited':
                logs = container.logs(tail=25).decode('utf-8')
                message = f"Container {container.name} has stopped. Last 25 lines of logs:\n\n{logs}"
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

def read_command():
    if os.path.exists(COMMAND_FILE):
        with open(COMMAND_FILE, 'r') as f:
            command = f.read().strip()
        os.remove(COMMAND_FILE)
        logging.info(f"Read command: {command}")
        return command
    logging.debug("No command found.")
    return None

def write_status(status):
    logging.info(f"Writing status: {status}")
    with open(STATUS_FILE, 'w') as f:
        f.write(status)
    logging.info(f"Status written: {status}")

def write_container_logs(container_name, container_logs):
    try:
        log_entry = f"{container_name}: {container_logs}\n"
        logging.info(f"Attempting to write log for container '{container_name}'.")
        with open(CONTAINER_FILE, 'a') as f:
            f.write(log_entry)
        logging.info(f"Successfully written log for container '{container_name}'.")
    except Exception as e:
        logging.error(f"Failed to write log for container '{container_name}': {str(e)}")


def handle_command(command):
    logging.info(f"Received command: {command}")
    status = ""
    if command.startswith('silence'):
        _, container_name, duration = command.split()
        status = silence_alert(container_name, int(duration))
        logging.info(f"Silenced alert for {container_name} for {duration} minutes.")

    elif command.startswith('unsilence'):
        _, container_name = command.split()
        status = unsilence_alert(container_name)
        logging.info(f"Unsilenced alert for {container_name}.")

    elif command == 'status':
        status = "Currently silenced alerts:\n"
        if len(silenced_alerts) > 0:
            for container, end_time in silenced_alerts.items():
                remaining = end_time - datetime.now()
                status += f"{container}: silenced for {remaining.total_seconds() / 60:.1f} more minutes\n"
            logging.info("Displayed currently silenced alerts.")
        else:
            status += "No alerts are currently silenced."
            logging.info("No alerts to display.")

    elif command == 'list':
        logging.info("Processing 'list' command.")
        try:
            container_names = [container.name for container in docker_client.containers.list()]
            if container_names:
                status = "\n".join(container_names)
                logging.info(f"Active containers found: {container_names}")
            else:
                status = "No active containers found."
                logging.info("No active containers found.")
        except Exception as e:
            logging.error(f"Error listing containers: {str(e)}")
            status = f"Error listing containers: {str(e)}"

    elif command == 'log_clear':
        try:
            subprocess.run(['sudo', 'truncate', '-s', '0', '/var/lib/docker/containers/75a6b3ac07e26190484b2244901b71c447e1431adb93c81717cb07095142e8d0/75a6b3ac07e26190484b2244901b71c447e1431adb93c81717cb07095142e8d0-json.log'],
                        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            status = "MongoDB logs have been truncated successfully."
            logging.info(status)
        except subprocess.CalledProcessError as e:
            status = f"Failed to truncate MongoDB logs: {e.stderr.decode()}"
            logging.error(status)
        write_status(status)

    elif command == 'disk_usage':
        disk_usage = get_disk_usage()
        status = disk_usage
        logging.info(f"Disk usage reported: {disk_usage}")

    write_status(status)
    logging.info(f"Status written for command '{command}': {status}")

def check_containers_periodically():
    while not shutdown_flag.is_set():
        try:
            check_containers()
        except Exception as e:
            logging.error(f"An error occurred while checking containers: {str(e)}")
        time.sleep(300)  # Sleep for 300 seconds or 5 minutes

def main():
    logging.info("Monitoring script started.")
    initialize_files()  # Clean up temporary files
    
    # Start the thread for checking containers
    container_thread = threading.Thread(target=check_containers_periodically)
    container_thread.start()

    # Main loop for handling commands
    while not shutdown_flag.is_set():
        command = read_command()
        if command:
            handle_command(command)
        time.sleep(5)

if __name__ == "__main__":
    main()
