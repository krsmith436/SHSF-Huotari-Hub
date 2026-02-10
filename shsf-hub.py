import os  # Add this at the top
import btfpy
import threading
import queue
import paho.mqtt.client as mqtt
from guizero import App, PushButton, Text, Box
import signal
import sys
import time
from datetime import datetime

# --- CONFIGURATION ---
HM10_NODE = 7          # Position in devices.txt
CHAR_HANDLE = 0        # HM-10 Serial Write Handle
MQTT_BROKER = "localhost"
TOPIC_WILDCARD = "shsf/+/commands" # Use a wildcard '+' so we hear from everyone
TOPIC_HEARTBEAT = "shsf/heartbeat"
GUI_SENDER = "hub"
mqtt_sender = "Unknown"

command_queue = queue.Queue()
running = True
hm10_name = "Unknown Device"

# --- BLE CALLBACK (Nano -> Pi) ---
def ble_callback(HM10_NODE, CHAR_HANDLE, data, datalen):
    # 'data' arrives as a list of bytes
    message = "".join(chr(b) for b in data).strip()
    print(f"[BLE] Received: {message}")

    # Manage the response
    if mqtt_sender == GUI_SENDER:
        # Update GUI
        status_label.value = f"{hm10_name} Status: {message}"
        status_label.text_color = "green"
    else:
        # Forward to sender via MQTT
        topic = f"shsf/{mqtt_sender}/responses"
        mqtt_client.publish(topic, message)

# --- BLE WORKER THREAD ---
def ble_worker():
    global running
    global hm10_name
    print("[BLE] Initializing ...")
    if btfpy.Init_blue("devices.txt") != 1:
        print("[BLE] Failed to initialize.")
        return

    print(f"[BLE] Connecting to HM-10 (Node {HM10_NODE})...")
    if (btfpy.Connect_node(HM10_NODE,btfpy.CHANNEL_LE,0) == 0):
        print("[BLE] Failed to connect.")
        return
        
    if(btfpy.Ctic_ok(HM10_NODE,CHAR_HANDLE) == 1):
        # 1. Fetch the name from the module
        raw_name = btfpy.Device_name(HM10_NODE)
        
        # 2. Convert to string (handling potential byte-list format)
        if isinstance(raw_name, list):
            hm10_name = "".join(chr(b) for b in raw_name).strip()
        else:
            hm10_name = str(raw_name).strip()

        # 3. Update the GUI label directly
        status_label.value = f"{hm10_name} Status: Connected"
        status_label.text_color = "green"

        # Register callback and enable notifications
        print("")
        print(f"Connect OK to LE server: {hm10_name}")
        print("Enabling LE server notifications.")
        print("")
        btfpy.Notify_ctic(HM10_NODE,CHAR_HANDLE,btfpy.NOTIFY_ENABLE,ble_callback)
        
        while running:
            try:
                # Get command from GUI or MQTT
                cmd = command_queue.get(timeout=0.1)
                print(f"[BLE] Sending: {cmd}")
                btfpy.Write_ctic(HM10_NODE,CHAR_HANDLE,cmd + "\r",0)
                btfpy.Read_notify(100)
                command_queue.task_done()
            except queue.Empty:
                # Essential: allows btfpy to process incoming data packets
                btfpy.Sleep_ms(50) 
    else:
        print("[BLE] Data characteristic FFE1 not found.")

# --- HEARTBEAT ---
def send_heartbeat():
    # Using app.display(), so using guizero's internal timer with app.repeat()
    timestamp = datetime.now().strftime("%H:%M:%S")
    
    # Publish a simple timestamp to let everyone know we are alive
    mqtt_client.publish(TOPIC_HEARTBEAT, timestamp)

# --- THE COMMAND PROCESSOR ---
def process_command(payload, sender):
    """Handles commands from any source (MQTT or GUI)"""
    print(f"Received command '{payload}' from '{sender}'")
    
    # Update global variable mqtt_sender for ble_callback
    global mqtt_sender
    mqtt_sender = sender
    
    # Update the GUI label to show identity
    if sender == GUI_SENDER:
        status_label.value = f"Last Cmd: {payload} (from {sender})"
        status_label.text_color = "blue"

    # Send to the BLE queue for the Nano
    command_queue.put(payload)

# --- MQTT SETUP ---
def on_message(client, userdata, message):
    topic = message.topic  # e.g., "home/r4/commands"
    payload = message.payload.decode("utf-8")
    
    # Identify the sender by splitting the topic string
    # topic.split('/') results in ['home', 'sender', 'commands']
    parts = topic.split('/')
    sender = parts[1] if len(parts) > 1 else "unknown"

    # Route to the processor
    process_command(payload, sender)    

mqtt_client = mqtt.Client()
mqtt_client.on_message = on_message

# --- EXIT FUNCTION ---
def shutdown_system():
    """Cleans up all processes and exits the script."""
    global running
    print("\n[!] Shutting down system...")
    running = False              # Stops the BLE thread loop
    # btfpy.disconnect()
    mqtt_client.loop_stop()      # Stops the MQTT background thread
    app.destroy()                # Closes the GUI window
    # sys.exit(0) is called automatically after app.display() ends

# --- SHUTDOWN FUNCTION ---
def pi_shutdown():
    if app.yesno("Shutdown", "Are you sure you want to shut down the Pi?"):
        print("Shutting down Pi...")
        # Clean up before hardware off
        global running
        running = False
        mqtt_client.loop_stop()
        # Trigger the system shutdown command
        os.system("sudo shutdown -h now")

# Signal handler for CTRL+C (calls the same shutdown function)
def signal_handler(signum, frame):
    shutdown_system()

signal.signal(signal.SIGINT, signal_handler)

# --- GUI ---
app = App(title="SHSF - Pi Hub", width=400, height=280)
# Spacer
Text(app, "")

Text(app, "System Bridge Active", size=14, color="green")

# Spacer
Text(app, "")

# Initialize with a placeholder, set in ble_worker
status_label = Text(app, text="Searching for Device...", color="orange")

# Container for control buttons
button_box = Box(app, layout="grid")
PushButton(button_box, text="Horn", grid=[0,0], command=lambda: process_command("h", GUI_SENDER))
PushButton(button_box, text="All Blocks ON", grid=[1,0], command=lambda: process_command("ba o", GUI_SENDER))

# Spacer
Text(app, "")

# The Exit Button
exit_button = PushButton(app, text="EXIT SYSTEM", command=shutdown_system, width=20)
exit_button.bg = "red"
exit_button.text_color = "white"

# Spacer
Text(app, "")

# The Pi Shutdown button
shutdown_btn = PushButton(app, text="SHUTDOWN PI", command=pi_shutdown, width=20)
shutdown_btn.bg = "black"
shutdown_btn.text_color = "white"

# --- START ---
try:
    mqtt_client.connect(MQTT_BROKER, 1883)
    mqtt_client.subscribe(TOPIC_WILDCARD)
    mqtt_client.loop_start()

    ble_thread = threading.Thread(target=ble_worker, daemon=True)
    ble_thread.start()
    
    app.repeat(10000, send_heartbeat) # Runs send_heartbeat every 10,000ms
    app.display() # This blocks until shutdown_system() calls app.destroy()
    
    print("Script finished safely.")
    sys.exit(0)

except Exception as e:
    print(f"Main Loop Error: {e}")
    shutdown_system()
