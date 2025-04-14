#!/usr/bin/env python3
from pythonosc import udp_client, dispatcher, osc_server
import time
import keyboard
import threading

# Global OSC dispatcher and server for receiving Ableton responses on port 11001.
global_dispatcher = dispatcher.Dispatcher()
global_osc_server = None
base_clip_length = None  # Holds the length (in beats) of the first recorded clip.
# Global toggling variables.
waiting_for_refire = False
current_active_track = None  # The track index of the clip currently recording.

def start_global_osc_server():
    global global_osc_server
    ip = "127.0.0.1"
    port = 11001  # Fixed port for receiving from Ableton.
    global_osc_server = osc_server.ThreadingOSCUDPServer((ip, port), global_dispatcher)
    thread = threading.Thread(target=global_osc_server.serve_forever, daemon=True)
    thread.start()
    print(f"Global OSC server started on {ip}:{port}")
    return thread

# --- State Tracking ---
class StateTracker:
    def __init__(self):
        # Designated tracks: tracks 1, 3, 5, 7 (indexes 0, 2, 4, 6)
        self.track_has_clip = {0: False, 2: False, 4: False, 6: False}
        self.clip_slot_index = 0  # Always using the first clip slot.
        self.validation_interval = 7.0  # For background validation.
        self.validation_running = False
        self.validation_thread = None
        self.lock = threading.Lock()
    
    def mark_track_has_clip(self, track_index, has_clip=True):
        with self.lock:
            if track_index in self.track_has_clip:
                self.track_has_clip[track_index] = has_clip
                print(f"Internal state updated: Track {track_index+1} has clip: {has_clip}")
    
    def get_next_empty_track(self):
        with self.lock:
            for track_idx in [0, 2, 4, 6]:
                if not self.track_has_clip[track_idx]:
                    return track_idx
            return None
    
    def get_filled_tracks(self):
        with self.lock:
            return [idx for idx, has_clip in self.track_has_clip.items() if has_clip]
    
    def get_empty_tracks(self):
        with self.lock:
            return [idx for idx, has_clip in self.track_has_clip.items() if not has_clip]
    
    def start_background_validation(self, client):
        if self.validation_thread is not None and self.validation_thread.is_alive():
            print("Background validation already running")
            return
        self.validation_running = True
        self.validation_thread = threading.Thread(
            target=self._background_validation_loop,
            args=(client,),
            daemon=True
        )
        self.validation_thread.start()
        print(f"Background validation started (every {self.validation_interval} seconds)")
    
    def stop_background_validation(self):
        self.validation_running = False
        if self.validation_thread:
            self.validation_thread.join(timeout=1.0)
            print("Background validation stopped")
    
    def _background_validation_loop(self, client):
        print("Background validation thread started")
        while self.validation_running:
            for _ in range(int(self.validation_interval * 2)):
                if not self.validation_running:
                    break
                time.sleep(0.5)
            if not self.validation_running:
                break
            try:
                print("\n=== Background validation running ===")
                validate_state_with_ableton(client, self)
                print("=== Background validation complete ===\n")
            except Exception as e:
                print(f"Error in background validation: {e}")

# --- OSC Query Helpers ---
def query_clip_markers(client, track_index, clip_slot_index, timeout=6.0):
    """
    Queries Ableton for the start and end markers of a clip.
    Returns [start_marker, end_marker] (in beats) or [None, None] if not available.
    This function will wait (polling in a loop) until the timeout expires.
    """
    event = threading.Event()
    result = [None, None]

    def start_marker_handler(unused_addr, *args):
        # Debug: print raw args received.
        print(f"[DEBUG] Start marker handler received args: {args}")
        if len(args) >= 3:
            if int(args[0]) == track_index and int(args[1]) == clip_slot_index:
                result[0] = float(args[2])
                if result[1] is not None:
                    event.set()

    def end_marker_handler(unused_addr, *args):
        print(f"[DEBUG] End marker handler received args: {args}")
        if len(args) >= 3:
            if int(args[0]) == track_index and int(args[1]) == clip_slot_index:
                result[1] = float(args[2])
                if result[0] is not None:
                    event.set()

    global_dispatcher.map("/live/clip/get/start_marker", start_marker_handler)
    global_dispatcher.map("/live/clip/get/end_marker", end_marker_handler)
    client.send_message("/live/clip/get/start_marker", [track_index, clip_slot_index])
    client.send_message("/live/clip/get/end_marker", [track_index, clip_slot_index])
    start_time = time.time()
    while not event.is_set() and time.time() - start_time < timeout:
        time.sleep(0.1)
    global_dispatcher.unmap("/live/clip/get/start_marker", start_marker_handler)
    global_dispatcher.unmap("/live/clip/get/end_marker", end_marker_handler)
    return result

def enforce_clip_markers(client, track_index, clip_slot_index, expected_start, expected_end, delay=0.5, timeout=2.0):
    """
    After a delay, queries back the markers and, if they do not match the expected values,
    re-sends the set commands.
    """
    time.sleep(delay)
    markers = query_clip_markers(client, track_index, clip_slot_index, timeout)
    print(f"[DEBUG] Enforcement: Queried markers for track {track_index+1}, slot {clip_slot_index+1}: {markers}")
    if markers[0] != expected_start or markers[1] != expected_end:
        print(f"[DEBUG] Markers mismatch. Re-sending commands: start={expected_start}, end={expected_end}")
        client.send_message("/live/clip/set/start_marker", [track_index, clip_slot_index, expected_start])
        client.send_message("/live/clip/set/end_marker", [track_index, clip_slot_index, expected_end])
        markers = query_clip_markers(client, track_index, clip_slot_index, timeout)
        print(f"[DEBUG] Markers after enforcement: {markers}")
    else:
        print("[DEBUG] Markers correctly set.")

# --- Finalizing Recording ---
def finalize_recording(client, track_index, clip_slot_index):
    """
    After a recording has been stopped by re-firing the clip, this function waits and
    polls repeatedly for valid start and end markers.
    • If the finalized clip is the first clip (track 1, slot 1) and base_clip_length is unset,
      it updates base_clip_length.
    • For subsequent clips, if base_clip_length is available, it enforces that the clip's
      markers match (start = 0.0, end = base_clip_length).
    """
    global base_clip_length
    print(f"[DEBUG] Finalizing recording on track {track_index+1}, slot {clip_slot_index+1}...")
    max_attempts = 20  # Poll up to 20 times (e.g., 10 seconds with a 0.5-second interval)
    attempt = 0
    markers = [None, None]
    while attempt < max_attempts:
        markers = query_clip_markers(client, track_index, clip_slot_index, timeout=1.0)
        if markers[0] is not None and markers[1] is not None:
            break
        attempt += 1
        time.sleep(0.5)
    print(f"[DEBUG] Final markers for track {track_index+1}, slot {clip_slot_index+1}: {markers}")
    if markers[0] is not None and markers[1] is not None:
        clip_length = markers[1] - markers[0]
        if base_clip_length is None and track_index == 0 and clip_slot_index == 0:
            base_clip_length = clip_length
            print(f"[DEBUG] Base clip length updated to {base_clip_length} beats (from first clip).")
        elif base_clip_length is not None:
            print(f"[DEBUG] Enforcing markers for track {track_index+1}, slot {clip_slot_index+1} to base length {base_clip_length}.")
            client.send_message("/live/clip/set/start_marker", [track_index, clip_slot_index, 0.0])
            client.send_message("/live/clip/set/end_marker", [track_index, clip_slot_index, base_clip_length])
            threading.Thread(target=enforce_clip_markers, args=(client, track_index, clip_slot_index, 0.0, base_clip_length), daemon=True).start()
    else:
        print("[DEBUG] Unable to capture marker values after finalization.")

# --- Recording Function ---
def record_clip(client, track_index, clip_slot_index, state_tracker):
    """
    Fires a clip slot to record a new clip.
    This function disarms all tracks, arms the chosen track, and fires the clip slot.
    It does not finalize (query markers) immediately.
    Instead, toggling behavior (via comma key) will control stopping and finalization.
    """
    print(f"Recording new clip on track {track_index+1}, slot {clip_slot_index+1}")
    for i in range(8):
        client.send_message("/live/track/set/arm", [i, 0])
    client.send_message("/live/track/set/arm", [track_index, 1])
    client.send_message("/live/clip_slot/fire", [track_index, clip_slot_index])
    state_tracker.mark_track_has_clip(track_index, True)
    print(f"Recording started on track {track_index+1}, slot {clip_slot_index+1}")
    return

# --- Connection and Validation Helpers ---
def verify_ableton_connection(client, timeout=1.0):
    print("Verifying connection to Ableton...")
    event = threading.Event()
    def handle_response(*args):
        print(f"Received response from Ableton: {args}")
        event.set()
    global_dispatcher.map("/live/song/get/tempo", handle_response)
    client.send_message("/live/song/get/tempo", [])
    client.send_message("/live/test", [])
    start_time = time.time()
    while not event.is_set() and time.time() - start_time < timeout:
        time.sleep(0.005)
    global_dispatcher.unmap("/live/song/get/tempo", handle_response)
    if event.is_set():
        print("Successfully connected to Ableton!")
        return True
    else:
        print("Could not verify connection to Ableton. Check that AbletonOSC is running.")
        return False

def validate_state_with_ableton(client, state_tracker):
    print("Validating internal clip state with Ableton...")
    for track_idx in [0, 2, 4, 6]:
        has_clip = check_track_has_clip(client, track_idx, state_tracker.clip_slot_index)
        state_tracker.mark_track_has_clip(track_idx, has_clip)
    filled_tracks = [t+1 for t in state_tracker.get_filled_tracks()]
    empty_tracks  = [t+1 for t in state_tracker.get_empty_tracks()]
    print(f"Current state - Tracks with clips: {filled_tracks if filled_tracks else 'none'}")
    print(f"Current state - Empty tracks: {empty_tracks if empty_tracks else 'none'}")

def check_track_has_clip(client, track_index, clip_slot_index):
    print(f"Checking if track {track_index+1}, slot {clip_slot_index+1} has a clip...")
    event = threading.Event()
    result = [None]
    def handler(unused_addr, *args):
        if len(args) >= 3:
            if int(args[0]) == track_index and int(args[1]) == clip_slot_index:
                result[0] = bool(int(args[2]))
                event.set()
                print(f"Response: Track {track_index+1}, slot {clip_slot_index+1} has clip: {result[0]}")
    global_dispatcher.map("/live/clip_slot/get/has_clip/return", handler)
    global_dispatcher.map("/live/clip_slot/get/has_clip", handler)
    global_dispatcher.map("/live/clip/get/exists/return", handler)
    client.send_message("/live/clip_slot/get/has_clip", [track_index, clip_slot_index])
    time.sleep(0.0005)
    client.send_message("/live/clip/get/exists", [track_index, clip_slot_index])
    start_time = time.time()
    while not event.is_set() and time.time() - start_time < 0.3:
        time.sleep(0.005)
    global_dispatcher.unmap("/live/clip_slot/get/has_clip/return", handler)
    global_dispatcher.unmap("/live/clip_slot/get/has_clip", handler)
    global_dispatcher.unmap("/live/clip/get/exists/return", handler)
    if result[0] is None:
        print(f"No response received for track {track_index+1}; assuming no clip.")
        return False
    return result[0]

# --- Main and Keyboard Handling ---
def main():
    global waiting_for_refire, current_active_track
    start_global_osc_server()
    state_tracker = StateTracker()
    ip = "127.0.0.1"   # AbletonOSC sending address
    port = 11000       # AbletonOSC sending port
    client = udp_client.SimpleUDPClient(ip, port)
    
    print(f"Attempting to connect to AbletonOSC server at {ip}:{port}")
    if not verify_ableton_connection(client):
        input("Press Enter to exit...")
        return
    
    validate_state_with_ableton(client, state_tracker)
    state_tracker.start_background_validation(client)
    
    print("Foot controller started.")
    print("- Press ',' to toggle recording/refiring")
    print("- Press '.' to stop all clips (playback only)")
    print("- Press '/' to force state validation with Ableton")
    print("- Press 'up' and 'down' for other controls (not used here)")
    print("- Press 'esc' to exit")
    
    is_processing = False  # Local to main
    
    def handle_comma_press(e):
        nonlocal is_processing
        global waiting_for_refire, current_active_track
        with threading.Lock():
            if is_processing:
                print("Already processing a command. Please wait...")
                return
            is_processing = True
            try:
                if waiting_for_refire:
                    # Second press: re-fire current clip to stop recording.
                    print(f"Refiring clip in track {current_active_track+1}, slot {state_tracker.clip_slot_index+1} to stop recording.")
                    client.send_message("/live/clip_slot/fire", [current_active_track, state_tracker.clip_slot_index])
                    # Finalize the recording in a background thread.
                    threading.Thread(target=finalize_recording, args=(client, current_active_track, state_tracker.clip_slot_index), daemon=True).start()
                    waiting_for_refire = False
                else:
                    # First or third press: record a new clip.
                    track_to_use = state_tracker.get_next_empty_track()
                    if track_to_use is None:
                        print("All designated tracks (1, 3, 5, 7) are full! Clear some clips before recording more.")
                        return
                    print(f"Recording new clip in track {track_to_use+1}, slot {state_tracker.clip_slot_index+1}")
                    record_clip(client, track_to_use, state_tracker.clip_slot_index, state_tracker)
                    current_active_track = track_to_use
                    waiting_for_refire = True
            finally:
                is_processing = False
    
    def stop_clips(e):
        print("Stopping all clips (playback only)...")
        client.send_message("/live/song/stop_all_clips", [])
    
    def force_validation(e):
        nonlocal is_processing
        with threading.Lock():
            if is_processing:
                print("Already processing a command. Please wait...")
                return
            is_processing = True
            try:
                print("Forcing state validation with Ableton...")
                validate_state_with_ableton(client, state_tracker)
            finally:
                is_processing = False
    
    def handle_up_press(e):
        print("Up key pressed (no marker action)")
    
    def handle_down_press(e):
        print("Down key pressed (no marker action)")
    
    keyboard.on_press_key(',', handle_comma_press)
    keyboard.on_press_key('.', stop_clips)
    keyboard.on_press_key('/', force_validation)
    keyboard.on_press_key('up', handle_up_press)
    keyboard.on_press_key('down', handle_down_press)
    
    keyboard.wait('esc')
    print("Exiting foot controller...")
    state_tracker.stop_background_validation()
    if global_osc_server:
        global_osc_server.shutdown()

if __name__ == "__main__":
    main()
