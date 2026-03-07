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
ble_connected = False  # The missing flag!
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
    global running, hm10_name, ble_connected

    print("[BLE] Initializing ...")
    add_to_log("[BLE] Initializing ...")
    if btfpy.Init_blue("devices.txt") != 1:
        print("[BLE] Failed to initialize.")
        add_to_log("[BLE] Failed to initialize.")
        return

    while running:
        ble_connected = False # Ensure it's False while searching

        update_status(f"[BLE] Connecting to HM-10 (Node {HM10_NODE})...", "orange")
        print(f"[BLE] Connecting to HM-10 (Node {HM10_NODE})...")
        add_to_log(f"[BLE] Connecting to HM-10 (Node {HM10_NODE})...")

        if (btfpy.Connect_node(HM10_NODE,btfpy.CHANNEL_LE,0) == 1):
            # --- SUCCESSFUL CONNECTION ---
            ble_connected = True # Set to True once connection is solid
            print("[BLE] Connected successfully")
            add_to_log("[BLE] Connected successfully")
            
            if(btfpy.Ctic_ok(HM10_NODE,CHAR_HANDLE) == 1):
                # 1. Fetch the name from the module
                raw_name = btfpy.Device_name(HM10_NODE)
                
                # 2. Convert to string (handling potential byte-list format)
                if isinstance(raw_name, list):
                    hm10_name = "".join(chr(b) for b in raw_name).strip()
                else:
                    hm10_name = str(raw_name).strip()

                # 3. Update the GUI label
                update_status(f"[BLE] {hm10_name} Connected", "green")

                # Register callback and enable notifications
                print(f"[BLE] Connected to LE server: {hm10_name}")
                add_to_log(f"[BLE] Connected to LE server: {hm10_name}")
                btfpy.Notify_ctic(HM10_NODE,CHAR_HANDLE,btfpy.NOTIFY_ENABLE,ble_callback)
                
                # --- INTERNAL DATA LOOP ---
                while running:
                    try:
                        # 1. Check for outgoing commands from MQTT/GUI
                        try:
                            # Get command from GUI or MQTT
                            cmd = command_queue.get_nowait()
                            print(f"[BLE] Sending: {cmd}")
                            add_to_log(f"[BLE] Sending: {cmd}")
                            bytes_written = btfpy.Write_ctic(HM10_NODE,CHAR_HANDLE,cmd + "\r",0)
                            if bytes_written > 0:
                                # Success!
                                btfpy.Read_notify(100)
                                command_queue.task_done()
                            else:
                                # If 0 bytes were written, the connection is likely lost.
                                raise ConnectionError("[BLE] Write failed.")
                        except queue.Empty:
                            pass

                        time.sleep(0.05) # CPU breathing room 

                    except Exception as e:
                        ble_connected = False # Set back to False on crash
                        add_to_log(f"[BLE] Connection lost ({e})")
                        update_status(f"[BLE] Connection lost ({e})", "red")
                        break # Break internal loop to trigger retry

            else:
                print("[BLE] Data characteristic FFE1 not found")
                add_to_log("[BLE] Data characteristic FFE1 not found")
                update_status(f"[BLE] LECHAR FFE1 not found", "red")

        else:
            # --- CONNECTION FAILED ---
            ble_connected = False
            print("[BLE] Connection failed")
            add_to_log("[BLE] Connection failed")
            update_status("[BLE] Failed to connect.", "red")

            # Wait 5 seconds before trying again to avoid spamming the radio
            for _ in range(50): # Check 'running' flag every 0.1s during the 5s wait
                if not running: break
                time.sleep(0.1)

    add_to_log("[BLE] Thread shutting down.")

# --- REPEATED TASKS ---
def repeat_tasks():
    # Guizero's app.display() has an internal timer with app.repeat(), set just before app.display().
    
    # Publish a simple timestamp (heart beat) to let everyone know we are alive
    timestamp = datetime.now().strftime("%H:%M:%S")
    mqtt_client.publish(TOPIC_HEARTBEAT, timestamp)

# --- THE COMMAND PROCESSOR ---
def process_command(payload, sender):
    """Handles commands from any source (MQTT or GUI)"""

    add_to_log(f"[CMDPROC] Command '{payload}' from {sender}")
    
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
            if quality <= 75: add_to_log(f"[WIFI] Giebel Throttle signal is weak: {quality}%")
        except:
            pass
    else:
        # Identify the sender by splitting the topic string
        # topic.split('/') results in ['home', 'sender', 'commands']
        parts = topic.split('/')
        sender = parts[1] if len(parts) > 1 else "unknown"

        # Route to the processor
        process_command(payload, sender)    

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        add_to_log("[MQTT] Connected to Broker")
        update_status("[MQTT] Connected to Broker", "green")
        
        mqtt_client.subscribe(TOPIC_WILDCARD)
        add_to_log(f"[MQTT] Subscribed to: {TOPIC_WILDCARD}")

        mqtt_client.subscribe(TOPIC_RSSI)
        add_to_log(f"[MQTT] Subscribed to: {TOPIC_RSSI}")
    else:
        add_to_log(f"[MQTT] Connection failed (Code {rc})")
        
        update_status("[MQTT] Connection Error", "red")

def on_disconnect(client, userdata, rc):
    add_to_log("[MQTT] Disconnected from Broker")
    update_status("[MQTT] Offline (Retrying...)", "orange")
    # Note: loop_start() handles the actual reconnection logic automatically!

mqtt_client = mqtt.Client()
mqtt_client.on_message = on_message
mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect

# --- EXIT FUNCTION ---
def shutdown_system():
    """Cleans up all processes and exits the script."""
    global running
    print("\n[!] Shutting down system...")
    add_to_log("\n[!] Shutting down system...")
    running = False              # Stops the BLE thread loop
    mqtt_client.loop_stop()      # Stops the MQTT background thread
    mqtt_client.disconnect()     # Cleanly tells the broker we're leaving
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
        mqtt_client.disconnect()
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
app = App(title="SHSF - Pi Hub", width=500, height=410)
# Spacer
Text(app, "")

Text(app, text="Smith Huotari & Santa Fe Railroad", font="Times New Roman", size=24, color="green")

# Spacer
Text(app, "")

# Status bar, initialize for ble_worker
status_box = Box(app, width="fill", height=30, border=True)
Text(status_box, text="  [Status] ", align="left", size=10)
status_label = Text(status_box, text="[BLE] Searching for Device...", align="left", color="orange", size=10)

# Spacer
Text(app, "")

# Container for control buttons
button_box = Box(app, layout="grid")
PushButton(button_box, text="Horn", grid=[0,0], command=lambda: process_command("h", GUI_SENDER))
Text(button_box, text=" ", grid=[1,0]) # spacer
PushButton(button_box, text="All Blocks ON", grid=[2,0], command=lambda: process_command("ba o", GUI_SENDER))

# Spacer
Text(app, "")

# Create a text label for WiFi signal strength
health_label = Text(app, text="Giebel Throttle WiFi Signal: --%", color="gray")

# Create the log window
log_window = TextBox(app, width="fill", height=10, multiline=True, scrollbar=True)
log_window.text_size = 8
log_window.bg = "#f0f0f0" # Light gray background

# Container for system buttons
system_button_box = Box(app, layout="grid")

# CLear Log window button
clear_log_button = PushButton(system_button_box, text="Clear Log", grid=[0,0], command=clear_log)
Text(system_button_box, text="               ", grid=[1,0]) # spacer

# The Exit Button
exit_button = PushButton(system_button_box, text="EXIT SYSTEM", grid=[2,0], command=shutdown_system)
exit_button.bg = "red"
exit_button.text_color = "white"
Text(system_button_box, text="  ", grid=[3,0]) # spacer

# The Pi Shutdown button
shutdown_btn = PushButton(system_button_box, text="SHUTDOWN PI", grid=[4,0], command=pi_shutdown) # , width=20
shutdown_btn.bg = "black"
shutdown_btn.text_color = "white"

# --- START ---
try:
    ble_thread = threading.Thread(target=ble_worker, daemon=True)
    ble_thread.start()

    # connect_async doesn't block. It will just start trying in the background.
    try:
        mqtt_client.connect_async("localhost", 1883, 60)
        mqtt_client.loop_start() # This starts the background thread that handles retries
        add_to_log("[MQTT] Background connection thread started")
    except Exception as e:
        add_to_log(f"[MQTT] Could not start thread: {e}")


    
    app.repeat(10000, repeat_tasks) # Runs repeat_tasks every 10,000ms
    app.display() # This blocks until shutdown_system() calls app.destroy()
    
    print("Script finished safely.")
    sys.exit(0)

except Exception as e:
    print(f"\n[!] Main Loop Error: {e}")
    add_to_log(f"\n[!] Main Loop Error: {e}")
    shutdown_system()
