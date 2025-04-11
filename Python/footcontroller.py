#!/usr/bin/env python3

from pythonosc import udp_client, dispatcher, osc_server
import time
import keyboard
import threading

def verify_ableton_connection(client, timeout=2.0):
    """Verify connection with Ableton by sending a test message and waiting for response."""
    print("Verifying connection to Ableton...")
    
    # Create variables for connection status
    connection_verified = False
    
    # Create a simple dispatcher to handle the response
    disp = dispatcher.Dispatcher()
    
    # Set up a response handler
    def handle_response(*args):
        nonlocal connection_verified
        print(f"Received response from Ableton: {args}")
        connection_verified = True
    
    # Register the response handler for expected message types
    disp.map("/live/song/get/tempo", handle_response)
    
    # Create a server to receive responses
    ip = "127.0.0.1"
    receive_port = 11001  # Common port for receiving from AbletonOSC
    
    # Start server in a separate thread
    server = osc_server.ThreadingOSCUDPServer((ip, receive_port), disp)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()
    
    # Send a test message to get Ableton's tempo
    client.send_message("/live/song/get/tempo", [])
    client.send_message("/live/test", [])
    
    # Wait for response with timeout
    start_time = time.time()
    while not connection_verified and time.time() - start_time < timeout:
        time.sleep(0.01)
    
    # Clean up server
    server.shutdown()
    
    if connection_verified:
        print("Successfully connected to Ableton!")
        return True
    else:
        print("Could not verify connection to Ableton. Please check if Ableton is running with AbletonOSC.")
        return False

def safely_create_clip(client, track_index, clip_slot_index):
    """Try to create a clip in the specified slot and return whether it was successful."""
    # Ensure clip_slot_index is within reasonable bounds (0-7)
    clip_slot_index = max(0, min(7, clip_slot_index))
    
    # First, check if there's a clip in this track and slot
    client.send_message("/live/clip/get/exists", [track_index, clip_slot_index])
    time.sleep(0.02)
    
    # We'll measure how long it takes for a clip creation operation
    print(f"Testing if we can create a clip in track {track_index+1}, slot {clip_slot_index+1}...")
    
    
    # Now try to create a new clip
    client.send_message("/live/clip/create", [track_index, clip_slot_index, 4.0])
    time.sleep(0.05)
    
    # Since we deleted and created, we should now have a clip
    return True

def record_clip(client, track_index, clip_slot_index):
    """Start recording in the specified track and clip slot."""
    print(f"Starting recording process for track {track_index+1}, slot {clip_slot_index+1}")
    
    # First, disarm all tracks to ensure clean recording setup
    print("Disarming all tracks")
    for i in range(8):
        client.send_message("/live/track/set/arm", [i, 0])  # 0 = disarmed
    time.sleep(0.01)
    
    # Step 1: Arm the specific track for recording
    print(f"Arming track {track_index+1} for recording")
    client.send_message("/live/track/set/arm", [track_index, 1])  # 1 = armed
    time.sleep(0.01)
    
    # Step 2: Make sure we have session record enabled
    print("Enabling session record")
    client.send_message("/live/song/set/session_record", [1])  # 1 = enabled
    time.sleep(0.01)
    
    # Create a new clip for recording
    print(f"Creating clip in track {track_index+1}, slot {clip_slot_index+1}")
    
    
    # Set looping to on for longer recording
    client.send_message("/live/clip/set/looping", [track_index, clip_slot_index, 1])
    time.sleep(0.005)
    
    # Fire the clip slot to start recording
    client.send_message("/live/clip_slot/fire", [track_index, clip_slot_index])
    
    print(f"Recording started in track {track_index+1}, slot {clip_slot_index+1}")
    return True

def check_clip_properties(client, track_index, clip_slot_index):
    """Check if a clip exists by querying properties."""
    print(f"Checking clip properties for track {track_index+1}, slot {clip_slot_index+1}...")
    
    
    # Set up variables to store results
    property_results = {}
    
    # Create dispatcher to receive responses
    disp = dispatcher.Dispatcher()
    
    # Set up handlers for different properties
    def name_handler(unused_addr, *args):
        property_results["name"] = args
        print(f"Got name response for track {track_index+1}: {args}")
    
    def length_handler(unused_addr, *args):
        property_results["length"] = args
        print(f"Got length response for track {track_index+1}: {args}")
    
    def exists_handler(unused_addr, *args):
        property_results["exists"] = args
        print(f"Got exists response for track {track_index+1}: {args}")
    

    
    # Register handlers
    disp.map("/live/clip/get/name/return", name_handler)
    disp.map("/live/clip/get/length/return", length_handler) 
    disp.map("/live/clip/get/exists/return", exists_handler)
    
    # Create server
    ip = "127.0.0.1"
    receive_port = 11001
    server = osc_server.ThreadingOSCUDPServer((ip, receive_port), disp)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()
    
    # Send queries for different properties
    client.send_message("/live/clip/get/name", [track_index, clip_slot_index])
    time.sleep(0.1)
    client.send_message("/live/clip/get/length", [track_index, clip_slot_index])
    time.sleep(0.1)
    client.send_message("/live/clip/get/exists", [track_index, clip_slot_index])
    time.sleep(0.1)
    client.send_message("/live/clip/get/color", [track_index, clip_slot_index])
    
    # Wait for responses
    time.sleep(0.5)
    
    # Cleanup
    server.shutdown()
    try:
        server_thread.join(timeout=0.5)
    except:
        pass
    
    return property_results
    
def check_track_has_clip(client, track_index, clip_slot_index):
    """Check if a track has a clip by querying Ableton directly."""
    print(f"Checking if track {track_index+1}, slot {clip_slot_index+1} has a clip...")
    
    # We'll use clip_slot/get/has_clip which directly tells us if a slot has a clip
    has_clip = False
    response_received = False
    
    # Create dispatcher
    disp = dispatcher.Dispatcher()
    
    # Handler for the response
    def has_clip_handler(unused_addr, *args):
        nonlocal has_clip, response_received
        print(f"Response received: {unused_addr} with args: {args}")
        if len(args) >= 3:
            # Format: [track_index, clip_slot_index, has_clip (0/1)]
            if int(args[0]) == track_index and int(args[1]) == clip_slot_index:
                has_clip = bool(int(args[2]))  # Convert to int first, then bool
                response_received = True
                print(f"Response: Track {track_index+1}, slot {clip_slot_index+1} has clip: {has_clip}")
    
    # Register the handler - try multiple possible return paths
    disp.map("/live/clip_slot/get/has_clip/return", has_clip_handler)
    disp.map("/live/clip_slot/get/has_clip", has_clip_handler)
    disp.map("/live/clip/get/exists/return", has_clip_handler)
    
    # Create server
    ip = "127.0.0.1"
    receive_port = 11001
    server = osc_server.ThreadingOSCUDPServer((ip, receive_port), disp)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()
    
    # Send multiple queries to maximize chance of response
    client.send_message("/live/clip_slot/get/has_clip", [track_index, clip_slot_index])
    time.sleep(0.1)
    client.send_message("/live/clip/get/exists", [track_index, clip_slot_index])
    
    # Wait for response with timeout
    timeout = 1.0  # One-second timeout
    start_time = time.time()
    while not response_received and time.time() - start_time < timeout:
        time.sleep(0.01)
    
    # Cleanup
    server.shutdown()
    try:
        server_thread.join(timeout=0.5)
    except:
        pass
    
    if not response_received:
        print(f"No response received for track {track_index+1}, assuming no clip")
        return False
    
    print(f"Track {track_index+1}, slot {clip_slot_index+1} has clip: {has_clip}")
    return has_clip

def find_and_record_in_sequence(client):
    """Record in tracks 1, 3, 5, 7 in sequence, checking all tracks and filling in order."""
    tracks_to_use = [0, 2, 4, 6]  # Corresponds to tracks 1, 3, 5, 7
    clip_slot_index = 0  # We're always using clip slot 1 (index 0)
    
    # Check which tracks have clips
    tracks_with_clips = []
    empty_tracks = []
    
    print("Checking all tracks for clips...")
    
    # Check all tracks, always starting with track 1
    for track_idx in tracks_to_use:
        has_clip = check_track_has_clip(client, track_idx, clip_slot_index)
        if has_clip:
            tracks_with_clips.append(track_idx)
        else:
            empty_tracks.append(track_idx)
    
    # If all tracks have clips, notify the user
    if not empty_tracks:
        print("All tracks (1, 3, 5, 7) are full! Please clear some clips before recording more.")
        return False
    
    # Print status for clarity
    clips_tracks_str = ", ".join([str(t+1) for t in tracks_with_clips])
    empty_tracks_str = ", ".join([str(t+1) for t in empty_tracks])
    print(f"Tracks with clips: {clips_tracks_str if tracks_with_clips else 'none'}")
    print(f"Empty tracks: {empty_tracks_str}")
    
    # Always prioritize tracks in the defined order: 1, 3, 5, 7
    # Take the first empty track in this order
    track_to_use = None
    for track_idx in tracks_to_use:
        if track_idx in empty_tracks:
            track_to_use = track_idx
            break
    
    # Record in this track
    print(f"Recording in empty track {track_to_use+1}, slot {clip_slot_index+1}")
    record_clip(client, track_to_use, clip_slot_index)
    
    print("Next recording will check all tracks again, prioritizing in order: 1, 3, 5, 7")
    return True

def main():
    # Set up the OSC client with default AbletonOSC server address
    ip = "127.0.0.1"  # localhost
    port = 11000      # default AbletonOSC port
    client = udp_client.SimpleUDPClient(ip, port)
    
    print(f"Attempting to connect to AbletonOSC server at {ip}:{port}")
    
    # Verify connection before proceeding
    if not verify_ableton_connection(client):
        input("Press Enter to exit...")
        return
    
    print("Foot controller started.")
    print("- Press ',' to record in the next empty track in sequence (1->3->5->7)")
    print("- Press 'esc' to exit")
    
    # Flag to prevent multiple executions
    is_processing = False
    
    # Create command handler for the comma key
    def handle_comma_press(e):
        nonlocal is_processing
        if is_processing:
            print("Already processing a command. Please wait...")
            return
        
        try:
            is_processing = True
            
            # First disarm all tracks in our sequence (1, 3, 5, 7)
            print("Disarming sequence tracks...")
            for track_idx in [0, 2, 4, 6]:
                print(f"Disarming track {track_idx+1}")
                client.send_message("/live/track/set/arm", [track_idx, 0])
                time.sleep(0.05)
            
            # Then proceed with finding and recording
            find_and_record_in_sequence(client)
        finally:
            is_processing = False
    
    def stop_clips(e):
        print("Stopping clips...")
        client.send_message("/live/track/stop_all_clips", [])
    
    # Set up the keyboard hook for the comma key
    keyboard.on_press_key(',', handle_comma_press)
    keyboard.on_press_key('.', stop_clips)
    
    # Add a way to exit the program
    keyboard.wait('esc')
    print("Exiting foot controller...")

if __name__ == "__main__":
    main()