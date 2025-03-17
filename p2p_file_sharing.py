import socket
import os
import threading
import requests
import json
import time
from flask import Flask, render_template, request, jsonify, Response
from urllib.parse import quote
from flask_cors import CORS  # Import CORS

# Configuration
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes, allowing all origins
CHUNK_SIZE = 1024 * 1024  # 1MB chunks
DOWNLOAD_FOLDER = "downloads"
UPLOAD_FOLDER = "uploads"  # Folder for uploaded files
SERVER_PORT = 5001
SHARED_FILES_JSON = "shared_files.json"  # File to persist shared_files
# TRACKER_URL = "http://<tracker-ip>:6000"  # Disabled for local testing
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Shared state
shared_files = {}
discovered_peers = set()


# Load shared_files from JSON at startup
def load_shared_files():
    global shared_files
    if os.path.exists(SHARED_FILES_JSON):
        with open(SHARED_FILES_JSON, 'r') as f:
            shared_files = json.load(f)
        print(f"Loaded shared files from {SHARED_FILES_JSON}: {shared_files}")
    else:
        print(f"No {SHARED_FILES_JSON} found, starting with empty shared_files")


# Save shared_files to JSON
def save_shared_files():
    with open(SHARED_FILES_JSON, 'w') as f:
        json.dump(shared_files, f)
    print(f"Saved shared files to {SHARED_FILES_JSON}: {shared_files}")


# Flask Server Routes
@app.route('/')
def index():
    return render_template('index.html', files=shared_files.keys(), server_port=SERVER_PORT)


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        print("Upload failed: No file part in request")
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        print("Upload failed: No selected file")
        return jsonify({"error": "No selected file"}), 400
    file_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(file_path)
    absolute_path = os.path.abspath(file_path)
    shared_files[file.filename] = absolute_path
    save_shared_files()  # Persist changes
    print(f"Uploaded file and added to shared_files: {file.filename} -> {absolute_path}")
    print(f"Current shared files: {shared_files}")
    return jsonify({"message": "File uploaded successfully", "filename": file.filename})


@app.route('/add_file', methods=['POST'])
def add_file():
    file_path = request.json.get('file_path')
    if not file_path or not os.path.exists(file_path):
        print(f"Failed to add file: Invalid or non-existent path '{file_path}'")
        return jsonify({"error": "Invalid or non-existent file path"}), 400
    filename = os.path.basename(file_path)
    absolute_path = os.path.abspath(file_path)
    shared_files[filename] = absolute_path
    save_shared_files()  # Persist changes
    print(f"Added file path to shared_files: {filename} -> {absolute_path}")
    print(f"Current shared files: {shared_files}")
    return jsonify({"message": "File path added successfully", "filename": filename})


@app.route('/remove_file', methods=['POST'])
def remove_file():
    filename = request.json.get('filename')
    if filename in shared_files:
        del shared_files[filename]
        save_shared_files()  # Persist changes
        print(f"Removed file link: {filename}")
        return jsonify({"message": "File link removed successfully"})
    print(f"Failed to remove file: '{filename}' not in shared list")
    return jsonify({"error": "File not found in shared list"}), 404


@app.route('/download/<filename>')
def download_file(filename):
    if filename not in shared_files:
        print(f"Download failed: '{filename}' not in shared files")
        return jsonify({"error": "File not found"}), 404
    file_path = shared_files[filename]
    if not os.access(file_path, os.R_OK):
        print(f"Download failed: Permission denied for '{file_path}'")
        return jsonify({"error": "Permission denied"}), 403

    def generate_chunks():
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    print(f"Serving file: {file_path}")
    headers = {'Content-Disposition': f'attachment; filename="{filename}"'}
    return Response(generate_chunks(), mimetype='application/octet-stream', headers=headers)


@app.route('/list_files')
def list_files():
    print(f"Current shared files: {shared_files}")
    return jsonify({"files": list(shared_files.keys())})


@app.route('/peers')
def get_peers():
    print(f"Returning peers: {list(discovered_peers)}")
    return jsonify({"peers": list(discovered_peers)})


@app.route('/peer_updates')
def peer_updates():
    def stream():
        print("SSE connection established")
        while True:
            peers = list(discovered_peers)
            print(f"Sending peer update via SSE: {peers}")
            yield f"data: {json.dumps({'peers': peers})}\n\n"
            time.sleep(5)  # Send update every 5 seconds

    return Response(stream(), mimetype='text/event-stream')


# Client Functionality
def download_from_peer(peer_ip, filename):
    url = f"http://{peer_ip}:{SERVER_PORT}/download/{quote(filename)}"
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)
    print(f"Attempting to download from {url}")
    try:
        with open(file_path, 'wb') as f:
            response = requests.get(url, stream=True, timeout=10)
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    downloaded += len(chunk)
                    f.write(chunk)
                    percentage = (downloaded / total_size) * 100 if total_size > 0 else 100
                    print(f"Downloading {filename}: {percentage:.2f}%")
            if total_size > 0 and downloaded >= total_size:
                print(f"Downloading {filename}: 100.00%")
        print(f"File downloaded to {file_path}")
        if os.path.exists(file_path):
            absolute_path = os.path.abspath(file_path)
            shared_files[filename] = absolute_path
            save_shared_files()  # Persist changes
            print(f"Automatically added downloaded file to shared list: {filename} -> {absolute_path}")
            print(f"Updated shared files: {shared_files}")
        else:
            print(f"Error: Downloaded file not found at {file_path}")
    except requests.exceptions.RequestException as e:
        print(f"Error downloading {filename}: {e}")


def get_available_files(peer_ip):
    url = f"http://{peer_ip}:{SERVER_PORT}/list_files"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        return response.json().get("files", [])
    except requests.exceptions.RequestException as e:
        print(f"Error fetching file list from {peer_ip}: {e}")
        return []


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        print(f"Error detecting IP automatically: {e}")
        return input("Enter your local IP manually (e.g., 192.168.1.x): ").strip()


# Broadcast-Based Peer Discovery
def discover_peers():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(('', SERVER_PORT))
    except OSError:
        print(f"Port {SERVER_PORT} already in use for discovery, skipping broadcast bind")
        return
    print("Listening for peers via broadcast...")
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            if data == b"P2P_FILE_SERVER":
                if addr[0] not in discovered_peers:
                    discovered_peers.add(addr[0])
                    print(f"Discovered peer via broadcast: {addr[0]}")
        except OSError:
            pass


def announce_presence():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    message = b"P2P_FILE_SERVER"
    while True:
        sock.sendto(message, ('255.255.255.255', SERVER_PORT))
        threading.Event().wait(5)


# Start both server and client components
def start_p2p():
    my_ip = get_local_ip()
    print(f"My IP: {my_ip}")

    # Load shared_files at startup
    load_shared_files()

    # Start Flask server in a thread
    server_thread = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=SERVER_PORT, debug=False), daemon=True)
    server_thread.start()

    # Start broadcast discovery
    threading.Thread(target=discover_peers, daemon=True).start()
    threading.Thread(target=announce_presence, daemon=True).start()

    print("Tracker registration disabled for now")
    print(f"P2P node started. Access UI at http://{my_ip}:{SERVER_PORT}")

    # Interactive client mode
    while True:
        action = input("Enter 'download', 'add', 'remove', or 'exit': ").strip().lower()
        if action == 'exit':
            break
        elif action == 'download':
            peers = list(discovered_peers)
            if not peers:
                print("No peers available")
                continue
            print("Available peers:")
            for i, peer in enumerate(peers, 1):
                print(f"{i}. {peer}")
            peer_choice = input("Enter the number of the peer to download from (or type the IP): ").strip()
            try:
                peer_index = int(peer_choice) - 1
                if 0 <= peer_index < len(peers):
                    peer_ip = peers[peer_index]
                else:
                    print(f"Error: Invalid peer number. Choose between 1 and {len(peers)}")
                    continue
            except ValueError:
                peer_ip = peer_choice
                if peer_ip not in peers:
                    print("Error: Invalid peer IP")
                    continue

            available_files = get_available_files(peer_ip)
            if available_files:
                print(f"Files available for download from {peer_ip}:")
                for i, file in enumerate(available_files, 1):
                    print(f"{i}. {file}")
                file_choice = input("Enter the number of the file to download (or type the filename): ").strip()
                try:
                    file_index = int(file_choice) - 1
                    if 0 <= file_index < len(available_files):
                        filename = available_files[file_index]
                    else:
                        print(f"Error: Invalid file number. Choose between 1 and {len(available_files)}")
                        continue
                except ValueError:
                    filename = file_choice
                    if filename not in available_files:
                        print("Error: File not available on this peer")
                        continue

                threading.Thread(target=download_from_peer, args=(peer_ip, filename), daemon=True).start()
            else:
                print("No files available or peer unreachable")
        elif action == 'add':
            file_path = input("Enter absolute file path to share: ")
            if os.path.exists(file_path):
                filename = os.path.basename(file_path)
                shared_files[filename] = os.path.abspath(file_path)
                save_shared_files()  # Persist changes
                print(f"Added file link: {filename} -> {shared_files[filename]}")
            else:
                print("Error: File path does not exist")
        elif action == 'remove':
            filename = input("Enter filename to remove from shared list: ")
            if filename in shared_files:
                del shared_files[filename]
                save_shared_files()  # Persist changes
                print(f"Removed file link: {filename}")
            else:
                print("Error: File not in shared list")
        else:
            print("Unknown command")


if __name__ == "__main__":
    start_p2p()