import os  # Add this at the top
import btfpy
import threading
import queue
import paho.mqtt.client as mqtt
from guizero import App, PushButton, Text, Box, TextBox
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
TOPIC_RSSI = "shsf/giebel_throttle/rssi"
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
    add_to_log(f"[BLE] Received: {message}")

    # Manage the response
    if mqtt_sender != GUI_SENDER:
        # Forward to sender via MQTT
        topic = f"shsf/{mqtt_sender}/responses"
        mqtt_client.publish(topic, message)

# --- BLE WORKER THREAD ---
def ble_worker():
    global running
    global hm10_name
    print("[BLE] Initializing ...")
    add_to_log("[BLE] Initializing ...")
    if btfpy.Init_blue("devices.txt") != 1:
        print("[BLE] Failed to initialize.")
        add_to_log("[BLE] Failed to initialize.")
        return

    print(f"[BLE] Connecting to HM-10 (Node {HM10_NODE})...")
    add_to_log(f"[BLE] Connecting to HM-10 (Node {HM10_NODE})...")
    if (btfpy.Connect_node(HM10_NODE,btfpy.CHANNEL_LE,0) == 0):
        print("[BLE] Failed to connect.")
        add_to_log("[BLE] Failed to connect.")
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
        update_status(f"{hm10_name} Status: Connected", "green")

        # Register callback and enable notifications
        print(f"[BLE] Connected to LE server: {hm10_name}")
        add_to_log(f"[BLE] Connected to LE server: {hm10_name}")
        btfpy.Notify_ctic(HM10_NODE,CHAR_HANDLE,btfpy.NOTIFY_ENABLE,ble_callback)
        
        while running:
            try:
                # Get command from GUI or MQTT
                cmd = command_queue.get(timeout=0.1)
                print(f"[BLE] Sending: {cmd}")
                add_to_log(f"[BLE] Sending: {cmd}")
                btfpy.Write_ctic(HM10_NODE,CHAR_HANDLE,cmd + "\r",0)
                btfpy.Read_notify(100)
                command_queue.task_done()
            except queue.Empty:
                # Essential: allows btfpy to process incoming data packets
                btfpy.Sleep_ms(50) 
    else:
        print("[BLE] Data characteristic FFE1 not found.")
        add_to_log("[BLE] Data characteristic FFE1 not found.")

# --- HEARTBEAT ---
def send_heartbeat():
    # Using app.display(), so using guizero's internal timer with app.repeat()
    timestamp = datetime.now().strftime("%H:%M:%S")
    
    # Publish a simple timestamp to let everyone know we are alive
    mqtt_client.publish(TOPIC_HEARTBEAT, timestamp)

# --- THE COMMAND PROCESSOR ---
def process_command(payload, sender):
    """Handles commands from any source (MQTT or GUI)"""

    add_to_log(f"[MQTT] Command '{payload}' from {sender}")
    
    # Update global variable mqtt_sender for ble_callback
    global mqtt_sender
    mqtt_sender = sender
    
    # Update the GUI label to show identity
    update_status(f"Last Cmd: {payload} (from {sender})", "blue")

    # Send to the BLE queue for the Nano
    command_queue.put(payload)

# --- MQTT SETUP ---
def on_message(client, userdata, message):
    topic = message.topic  # e.g., "home/r4/commands"
    payload = message.payload.decode("utf-8")

    if topic == TOPIC_RSSI:
        try:
            dbm = int(payload)
            # Convert dBm to Percentage (-100 to -50 scale)
            # -50 or better = 100%, -100 or worse = 0%
            quality = 2 * (dbm + 100)
            quality = max(0, min(100, quality)) # Keep between 0-100
            
            health_label.value = f"Giebel Throttle WiFi Signal: {quality}%"
            
            # Change color based on health
            if quality > 75: health_label.text_color = "green"
            elif quality > 40: health_label.text_color = "orange"
            else: health_label.text_color = "red"

            # Log RSSI if the signal gets too low
            if quality <= 75: add_to_log("[WIFI] Giebel Throttle signal is weak!")
        except:
            pass
    else:
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
    add_to_log("\n[!] Shutting down system...")
    running = False              # Stops the BLE thread loop
    mqtt_client.loop_stop()      # Stops the MQTT background thread
    app.destroy()                # Closes the GUI window
    # sys.exit(0) is called automatically after app.display() ends

# --- SHUTDOWN FUNCTION ---
def pi_shutdown():
    if app.yesno("Shutdown", "Are you sure you want to shut down the Pi?"):
        print("\n[!] Shutting down Pi...")
        add_to_log("\n[!] Shutting down Pi...")
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

# --- LOG HELPER FUNCTION ---
def add_to_log(message):
    """Adds a timestamped message to the GUI log window."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    new_entry = f"[{timestamp}] {message}"
    
    # Prepend the new text at the top (or append to bottom)
    # We'll append to the bottom for a traditional log feel
    log_window.append(new_entry)
    
    # Auto-scroll to the bottom
    # (In guizero/tkinter, this happens automatically when appending)

# --- CLEAR LOG FUNCTION ---
def clear_log():
    log_window.clear()
    add_to_log("Log cleared.")

# --- UPDATE STATUS INDICATOR ---
def update_status(message, color):
    status_label.value = message
    status_label.text_color = color

# --- GUI ---
app = App(title="SHSF - Pi Hub", width=500, height=600)
# Spacer
Text(app, "")

Text(app, text="Smith Huotari & Santa Fe Railroad", font="Times New Roman", size=24, color="green", style="bold")

# Spacer
Text(app, "")

# Status bar, initialize for ble_worker
status_box = Box(app, width="fill", height=30, border=True)
Text(status_box, text="  [Status] ", align="left", size=10)
status_label = Text(status_box, text="Searching for Device...", align="left", color="orange", size=10)

# Spacer
Text(app, "")

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

# Spacer
Text(app, "")

# Create a text label for WiFi signal strength
health_label = Text(app, text="Signal: --%", color="gray")

# Create the log window
log_window = TextBox(app, width="fill", height=10, multiline=True, scrollbar=True)
log_window.text_size = 8
log_window.bg = "#f0f0f0" # Light gray background

# CLear log window
PushButton(app, text="Clear Log", command=clear_log, align="left")

# --- START ---
try:
    mqtt_client.connect(MQTT_BROKER, 1883)
    # update_status("[MQTT] Connected to Broker!", "green")

    mqtt_client.subscribe(TOPIC_WILDCARD)
    add_to_log(f"[MQTT] Subscribed to: {TOPIC_WILDCARD}")

    mqtt_client.subscribe(TOPIC_RSSI)
    add_to_log(f"[MQTT] Subscribed to: {TOPIC_RSSI}")

    mqtt_client.loop_start()

    ble_thread = threading.Thread(target=ble_worker, daemon=True)
    ble_thread.start()
    
    app.repeat(10000, send_heartbeat) # Runs send_heartbeat every 10,000ms
    app.display() # This blocks until shutdown_system() calls app.destroy()
    
    print("Script finished safely.")
    sys.exit(0)

except Exception as e:
    print(f"\n[!] Main Loop Error: {e}")
    add_to_log(f"\n[!] Main Loop Error: {e}")
    shutdown_system()
