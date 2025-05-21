import socket
import os
import threading
import requests
import json
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify, Response, send_file
from urllib.parse import quote
from flask_cors import CORS

# Configuration
app = Flask(__name__)
CORS(app)
CHUNK_SIZE = 1024 * 1024  # 1MB chunks
DOWNLOAD_FOLDER = "downloads"
UPLOAD_FOLDER = "uploads"
SERVER_PORT = 5001
SHARED_FILES_JSON = "shared_files.json"
FILE_METADATA_JSON = "file_metadata.json"
DHT_JSON = "dht.json"
DOWNLOAD_STATE_JSON = "download_state.json"
MAX_RETRIES = 3  # Maximum number of retries per chunk
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("chunks", exist_ok=True)

# Shared state
shared_files = {}  # {filename: filepath}
file_metadata = {}  # {filename: {chunk_size, file_size, file_sha, chunk_shas: []}}
discovered_peers = set()  # IPs of discovered peers
dht = {}  # Simplified DHT: {file_sha: [peer_ips]}
download_state = {}  # {filename: {file_sha, peers, completed_chunks: [indexes], total_chunks, status}}
update_trigger = False


# Load shared_files from JSON at startup
def load_shared_files():
    global shared_files
    if os.path.exists(SHARED_FILES_JSON):
        with open(SHARED_FILES_JSON, 'r') as f:
            shared_files = json.load(f)
        print(f"Loaded shared files from {SHARED_FILES_JSON}: {shared_files}")


# Save shared_files to JSON
def save_shared_files():
    with open(SHARED_FILES_JSON, 'w') as f:
        json.dump(shared_files, f)
    print(f"Saved shared files to {SHARED_FILES_JSON}: {shared_files}")


# Load file_metadata from JSON at startup
def load_file_metadata():
    global file_metadata
    if os.path.exists(FILE_METADATA_JSON):
        with open(FILE_METADATA_JSON, 'r') as f:
            file_metadata = json.load(f)
        print(f"Loaded file metadata from {FILE_METADATA_JSON}: {file_metadata}")


# Save file_metadata to JSON
def save_file_metadata():
    with open(FILE_METADATA_JSON, 'w') as f:
        json.dump(file_metadata, f)
    print(f"Saved file metadata to {FILE_METADATA_JSON}: {file_metadata}")


# Load DHT from JSON at startup
def load_dht():
    global dht
    if os.path.exists(DHT_JSON):
        with open(DHT_JSON, 'r') as f:
            dht = json.load(f)
        print(f"Loaded DHT from {DHT_JSON}: {dht}")


# Save DHT to JSON
def save_dht():
    with open(DHT_JSON, 'w') as f:
        json.dump(dht, f)
    print(f"Saved DHT to {DHT_JSON}: {dht}")


# Load download state at startup
def load_download_state():
    global download_state
    if os.path.exists(DOWNLOAD_STATE_JSON):
        with open(DOWNLOAD_STATE_JSON, 'r') as f:
            download_state = json.load(f)
        print(f"Loaded download state from {DOWNLOAD_STATE_JSON}: {download_state}")


def save_download_state():
    with open(DOWNLOAD_STATE_JSON, 'w') as f:
        json.dump(download_state, f)
    print(f"Saved download state to {DOWNLOAD_STATE_JSON}: {download_state}")


def initialize_download_state(filename, file_sha, peers, num_chunks):
    download_state[filename] = {
        "file_sha": file_sha,
        "peers": peers,
        "completed_chunks": [],
        "total_chunks": num_chunks,
        "status": "downloading"  # downloading, completed, failed, cancelled
    }
    save_download_state()


def update_download_state(filename, chunk_index):
    if filename in download_state:
        if chunk_index not in download_state[filename]["completed_chunks"]:
            download_state[filename]["completed_chunks"].append(chunk_index)
            save_download_state()


def update_download_status(filename, status):
    if filename in download_state:
        download_state[filename]["status"] = status
        save_download_state()


def remove_download_state(filename):
    if filename in download_state:
        # Clean up chunks
        num_chunks = download_state[filename]["total_chunks"]
        for i in range(num_chunks):
            chunk_path = os.path.join("chunks", f"{filename}.chunk{i}")
            if os.path.exists(chunk_path):
                os.remove(chunk_path)
        del download_state[filename]
        save_download_state()


# Calculate SHA-256 hash of a file or chunk
def calculate_sha(data):
    sha = hashlib.sha256()
    sha.update(data)
    return sha.hexdigest()


# Generate file metadata (chunk SHAs and file SHA)
def generate_file_metadata(file_path, filename):
    file_size = os.path.getsize(file_path)
    chunk_shas = []
    file_sha = hashlib.sha256()

    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            chunk_sha = calculate_sha(chunk)
            chunk_shas.append(chunk_sha)
            file_sha.update(chunk)

    metadata = {
        "chunk_size": CHUNK_SIZE,
        "file_size": file_size,
        "file_sha": file_sha.hexdigest(),
        "chunk_shas": chunk_shas
    }
    file_metadata[filename] = metadata
    save_file_metadata()

    # Update DHT
    my_ip = get_local_ip()
    file_sha = metadata["file_sha"]
    if file_sha not in dht:
        dht[file_sha] = []
    if my_ip not in dht[file_sha]:
        dht[file_sha].append(my_ip)
    save_dht()
    notify_peers_of_dht_update()
    return metadata


# Notify all peers of a DHT update
def notify_peers_of_dht_update():
    my_ip = get_local_ip()
    for peer_ip in discovered_peers:
        if peer_ip == my_ip:
            continue
        url = f"http://{peer_ip}:{SERVER_PORT}/dht_update"
        try:
            response = requests.post(url, json=dht, timeout=5)
            response.raise_for_status()
            print(f"Notified {peer_ip} of DHT update")
        except requests.exceptions.RequestException as e:
            print(f"Error notifying {peer_ip} of DHT update: {e}")


# Flask Server Routes
@app.route('/')
def index():
    return render_template('index.html', files=shared_files.keys(), file_metadata=file_metadata,
                           server_port=SERVER_PORT)


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
    save_shared_files()
    print(f"Uploaded file and added to shared_files: {file.filename} -> {absolute_path}")

    # Generate and store metadata
    generate_file_metadata(absolute_path, file.filename)
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
    save_shared_files()
    print(f"Added file path to shared_files: {filename} -> {absolute_path}")

    # Generate and store metadata
    generate_file_metadata(absolute_path, filename)
    return jsonify({"message": "File path added successfully", "filename": filename})


@app.route('/remove_file', methods=['POST'])
def remove_file():
    filename = request.json.get('filename')
    if filename in shared_files:
        file_sha = file_metadata[filename]["file_sha"]
        del shared_files[filename]
        del file_metadata[filename]
        save_shared_files()
        save_file_metadata()
        # Remove from DHT
        my_ip = get_local_ip()
        if file_sha in dht and my_ip in dht[file_sha]:
            dht[file_sha].remove(my_ip)
            if not dht[file_sha]:
                del dht[file_sha]
            save_dht()
            notify_peers_of_dht_update()
        print(f"Removed file link: {filename}")
        return jsonify({"message": "File link removed successfully"})
    print(f"Failed to remove file: '{filename}' not in shared list")
    return jsonify({"error": "File not found in shared list"}), 404


@app.route('/download/<filename>')
def download(filename):
    if filename not in shared_files:
        return jsonify({"error": "File not found"}), 404
    file_path = shared_files[filename]
    range_header = request.headers.get('Range', None)
    if not range_header:
        return jsonify({"error": "Range header required"}), 400

    file_size = os.path.getsize(file_path)
    byte_range = range_header.replace('bytes=', '').split('-')
    start = int(byte_range[0])
    end = int(byte_range[1]) if byte_range[1] else file_size - 1

    if start >= file_size or end >= file_size or start > end:
        return jsonify({"error": "Invalid range"}), 416

    headers = {
        'Content-Range': f'bytes {start}-{end}/{file_size}',
        'Accept-Ranges': 'bytes'
    }

    with open(file_path, 'rb') as f:
        f.seek(start)
        data = f.read(end - start + 1)

    return Response(data, status=206, headers=headers, mimetype='application/octet-stream')


@app.route('/file_metadata/<filename>')
def get_file_metadata(filename):
    if filename not in file_metadata:
        return jsonify({"error": "File metadata not found"}), 404
    return jsonify(file_metadata[filename])


@app.route('/file_metadata_by_sha/<file_sha>')
def get_file_metadata_by_sha(file_sha):
    for filename, metadata in file_metadata.items():
        if metadata["file_sha"] == file_sha:
            return jsonify({"filename": filename, "metadata": metadata})
    return jsonify({"error": "File metadata not found"}), 404


@app.route('/dht_update', methods=['POST'])
def dht_update():
    global dht
    new_dht = request.json
    dht = new_dht
    save_dht()
    print(f"Updated DHT: {dht}")
    global update_trigger
    update_trigger = True  # Trigger UI update
    return jsonify({"message": "DHT updated"})


@app.route('/dht')
def get_dht():
    print(f"Returning DHT: {dht}")
    return jsonify(dht)


@app.route('/peer_updates')
def peer_updates():
    def stream():
        print("SSE connection established")
        global update_trigger
        while True:
            if update_trigger:
                print("Sending update trigger to web UI")
                yield "data: update\n\n"
                update_trigger = False
            time.sleep(1)

    return Response(stream(), mimetype='text/event-stream')


# New Endpoints for Download Management
@app.route('/start_download', methods=['POST'])
def start_download():
    file_sha = request.json.get('file_sha')
    if not file_sha:
        return jsonify({"error": "File SHA required"}), 400
    if file_sha not in dht:
        return jsonify({"error": "File not found in DHT"}), 404

    peers = dht[file_sha]
    if not peers:
        return jsonify({"error": "No peers found with this file"}), 404

    # Check if download is already in progress or completed
    for filename, state in download_state.items():
        if state["file_sha"] == file_sha:
            if state["status"] in ["downloading", "completed"]:
                return jsonify({
                    "message": "Download already in progress or completed",
                    "filename": filename,
                    "status": state["status"]
                })
            else:
                # If failed or cancelled, remove the state and restart
                remove_download_state(filename)
                break

    # Fetch filename from a peer
    filename = None
    for peer_ip in peers:
        url = f"http://{peer_ip}:{SERVER_PORT}/file_metadata_by_sha/{file_sha}"
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            filename = data["filename"]
            break
        except requests.exceptions.RequestException as e:
            print(f"Error fetching metadata from {peer_ip}: {e}")
            continue

    if not filename:
        return jsonify({"error": "Could not retrieve filename for this file SHA"}), 404

    # Start the download in a background thread
    threading.Thread(target=download_file_parallel, args=(filename, peers, False), daemon=True).start()
    return jsonify({"message": "Download started", "filename": filename})


@app.route('/download_status/<file_sha>')
def download_status(file_sha):
    for filename, state in download_state.items():
        if state["file_sha"] == file_sha:
            return jsonify({
                "filename": filename,
                "status": state["status"],
                "completed_chunks": len(state["completed_chunks"]),
                "total_chunks": state["total_chunks"]
            })
    return jsonify({"error": "Download not found"}), 404


@app.route('/download_file/<file_sha>')
def download_file(file_sha):
    for filename, state in download_state.items():
        if state["file_sha"] == file_sha and state["status"] == "completed":
            file_path = os.path.join(DOWNLOAD_FOLDER, filename)
            if os.path.exists(file_path):
                return send_file(file_path, as_attachment=True, download_name=filename)
            else:
                return jsonify({"error": "File not found on disk"}), 404
    return jsonify({"error": "Download not completed or not found"}), 404


@app.route('/cancel_download/<file_sha>', methods=['POST'])
def cancel_download(file_sha):
    for filename, state in download_state.items():
        if state["file_sha"] == file_sha:
            if state["status"] == "completed":
                return jsonify({"error": "Download already completed"}), 400
            remove_download_state(filename)
            return jsonify({"message": "Download cancelled"})
    return jsonify({"error": "Download not found"}), 404


# Client Functionality
def download_chunk(peer_ip, filename, chunk_index, start, end, expected_sha, peers, current_try=1):
    if current_try > MAX_RETRIES:
        print(f"Chunk {chunk_index} failed after {MAX_RETRIES} retries")
        return None, None

    url = f"http://{peer_ip}:{SERVER_PORT}/download/{quote(filename)}"
    chunk_path = os.path.join("chunks", f"{filename}.chunk{chunk_index}")
    headers = {'Range': f'bytes={start}-{end}'}
    try:
        response = requests.get(url, headers=headers, stream=True, timeout=10)
        response.raise_for_status()
        with open(chunk_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
        # Verify chunk SHA
        with open(chunk_path, 'rb') as f:
            chunk_data = f.read()
            chunk_sha = calculate_sha(chunk_data)
        if chunk_sha != expected_sha:
            print(
                f"Chunk {chunk_index} SHA verification failed on try {current_try} from {peer_ip}: expected {expected_sha}, got {chunk_sha}")
            other_peers = [p for p in peers if p != peer_ip]
            if not other_peers:
                print(f"No other peers available to retry chunk {chunk_index}")
                return None, None
            next_peer = other_peers[chunk_index % len(other_peers)]
            print(f"Retrying chunk {chunk_index} from {next_peer} (try {current_try + 1})")
            return download_chunk(next_peer, filename, chunk_index, start, end, expected_sha, peers, current_try + 1)
        return chunk_sha, chunk_path
    except requests.exceptions.RequestException as e:
        print(f"Error downloading chunk {chunk_index} from {peer_ip} on try {current_try}: {e}")
        other_peers = [p for p in peers if p != peer_ip]
        if not other_peers:
            print(f"No other peers available to retry chunk {chunk_index}")
            return None, None
        next_peer = other_peers[chunk_index % len(other_peers)]
        print(f"Retrying chunk {chunk_index} from {next_peer} (try {current_try + 1})")
        return download_chunk(next_peer, filename, chunk_index, start, end, expected_sha, peers, current_try + 1)


def download_file_parallel(filename, peers, resume=False):
    # Step 1: Get metadata from one of the peers
    metadata = None
    for peer_ip in peers:
        url = f"http://{peer_ip}:{SERVER_PORT}/file_metadata/{quote(filename)}"
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            metadata = response.json()
            break
        except requests.exceptions.RequestException as e:
            print(f"Error fetching metadata from {peer_ip}: {e}")
            continue

    if not metadata:
        print(f"Could not retrieve metadata for {filename}")
        update_download_status(filename, "failed")
        return

    chunk_size = metadata['chunk_size']
    file_size = metadata['file_size']
    file_sha = metadata['file_sha']
    chunk_shas = metadata['chunk_shas']
    num_chunks = len(chunk_shas)

    # Step 2: Check if we're resuming a download
    completed_chunks = []
    if resume and filename in download_state:
        state = download_state[filename]
        if state["file_sha"] == file_sha and state["total_chunks"] == num_chunks:
            completed_chunks = state["completed_chunks"]
            peers = state["peers"]
            print(f"Resuming download of {filename}: {len(completed_chunks)}/{num_chunks} chunks completed")
        else:
            print(f"Download state for {filename} is invalid, starting fresh")
            initialize_download_state(filename, file_sha, peers, num_chunks)
    else:
        initialize_download_state(filename, file_sha, peers, num_chunks)

    # Step 3: Download remaining chunks in parallel
    chunk_paths = [None] * num_chunks
    with ThreadPoolExecutor(max_workers=min(len(peers) * 2, num_chunks)) as executor:
        futures = []
        for chunk_index in range(num_chunks):
            if chunk_index in completed_chunks:
                chunk_path = os.path.join("chunks", f"{filename}.chunk{chunk_index}")
                if os.path.exists(chunk_path):
                    with open(chunk_path, 'rb') as f:
                        existing_sha = calculate_sha(f.read())
                    if existing_sha == chunk_shas[chunk_index]:
                        chunk_paths[chunk_index] = chunk_path
                        print(f"Chunk {chunk_index} already downloaded and verified")
                        continue
                else:
                    print(f"Chunk {chunk_index} marked as completed but file missing, re-downloading")
                    completed_chunks.remove(chunk_index)

            start = chunk_index * chunk_size
            end = min(start + chunk_size - 1, file_size - 1)
            peer_ip = peers[chunk_index % len(peers)]
            futures.append(
                executor.submit(download_chunk, peer_ip, filename, chunk_index, start, end, chunk_shas[chunk_index],
                                peers))

        # Collect results
        for i, future in enumerate(futures):
            chunk_index = i + len(completed_chunks)
            chunk_sha, chunk_path = future.result()
            if chunk_sha and chunk_path:
                chunk_paths[chunk_index] = chunk_path
                update_download_state(filename, chunk_index)
                print(f"Chunk {chunk_index} downloaded and verified")
            else:
                print(f"Failed to download chunk {chunk_index} after retries")
                update_download_status(filename, "failed")
                return

    # Step 4: Verify all chunks
    for i, (chunk_path, expected_sha) in enumerate(zip(chunk_paths, chunk_shas)):
        if not chunk_path:
            print(f"Missing chunk {i}")
            update_download_status(filename, "failed")
            return
        with open(chunk_path, 'rb') as f:
            chunk_data = f.read()
            chunk_sha = calculate_sha(chunk_data)
            if chunk_sha != expected_sha:
                print(f"Chunk {i} SHA verification failed")
                update_download_status(filename, "failed")
                return

    # Step 5: Assemble the file
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)
    with open(file_path, 'wb') as f:
        for chunk_path in chunk_paths:
            with open(chunk_path, 'rb') as cf:
                f.write(cf.read())

    # Step 6: Verify the entire file
    with open(file_path, 'rb') as f:
        file_data = f.read()
        computed_file_sha = calculate_sha(file_data)
    if computed_file_sha != file_sha:
        print(f"File SHA verification failed: expected {file_sha}, got {computed_file_sha}")
        os.remove(file_path)
        update_download_status(filename, "failed")
        return

    print(f"File downloaded and verified: {file_path}")
    update_download_status(filename, "completed")

    # Step 7: Clean up
    for chunk_path in chunk_paths:
        os.remove(chunk_path)
    absolute_path = os.path.abspath(file_path)
    shared_files[filename] = absolute_path
    save_shared_files()
    generate_file_metadata(absolute_path, filename)


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
    my_ip = get_local_ip()
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            if data == b"P2P_FILE_SERVER":
                peer_ip = addr[0]
                if peer_ip not in discovered_peers and peer_ip != my_ip:
                    discovered_peers.add(peer_ip)
                    print(f"Discovered peer via broadcast: {peer_ip}")
                    url = f"http://{peer_ip}:{SERVER_PORT}/dht"
                    try:
                        response = requests.get(url, timeout=5)
                        response.raise_for_status()
                        peer_dht = response.json()
                        for file_sha, peer_ips in peer_dht.items():
                            if file_sha not in dht:
                                dht[file_sha] = []
                            for ip in peer_ips:
                                if ip not in dht[file_sha]:
                                    dht[file_sha].append(ip)
                        save_dht()
                        print(f"Synchronized DHT with {peer_ip}: {dht}")
                    except requests.exceptions.RequestException as e:
                        print(f"Error syncing DHT with {peer_ip}: {e}")
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
    load_shared_files()
    load_file_metadata()
    load_dht()
    load_download_state()

    # Start Flask server in a thread
    server_thread = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=SERVER_PORT, debug=False), daemon=True)
    server_thread.start()

    # Start broadcast discovery
    threading.Thread(target=discover_peers, daemon=True).start()
    threading.Thread(target=announce_presence, daemon=True).start()

    print(f"P2P node started. Access UI at http://{my_ip}:{SERVER_PORT}")

    while True:
        action = input("Enter 'download', 'resume', 'add', 'remove', or 'exit': ").strip().lower()
        if action == 'exit':
            break
        elif action == 'download' or action == 'resume':
            file_sha = input("Enter the file SHA to download: ").strip()
            if not file_sha:
                print("File SHA required")
                continue
            if file_sha in dht:
                peers = dht[file_sha]
                if not peers:
                    print("No peers found with this file")
                    continue
                print(f"Found peers with file: {peers}")
                filename = None
                for peer_ip in peers:
                    url = f"http://{peer_ip}:{SERVER_PORT}/file_metadata_by_sha/{file_sha}"
                    try:
                        response = requests.get(url, timeout=5)
                        response.raise_for_status()
                        data = response.json()
                        filename = data["filename"]
                        break
                    except requests.exceptions.RequestException as e:
                        print(f"Error fetching metadata from {peer_ip}: {e}")
                        continue
                if not filename:
                    print("Could not retrieve filename for this file SHA")
                    continue
                resume = (action == 'resume' or filename in download_state)
                threading.Thread(target=download_file_parallel, args=(filename, peers, resume), daemon=True).start()
            else:
                print("File not found in DHT")
        elif action == 'add':
            file_path = input("Enter absolute file path to share: ")
            if os.path.exists(file_path):
                filename = os.path.basename(file_path)
                shared_files[filename] = os.path.abspath(file_path)
                save_shared_files()
                generate_file_metadata(shared_files[filename], filename)
                print(f"Added file link: {filename} -> {shared_files[filename]}")
            else:
                print("Error: File path does not exist")
        elif action == 'remove':
            filename = input("Enter filename to remove from shared list: ")
            if filename in shared_files:
                file_sha = file_metadata[filename]["file_sha"]
                del shared_files[filename]
                del file_metadata[filename]
                save_shared_files()
                save_file_metadata()
                my_ip = get_local_ip()
                if file_sha in dht and my_ip in dht[file_sha]:
                    dht[file_sha].remove(my_ip)
                    if not dht[file_sha]:
                        del dht[file_sha]
                    save_dht()
                    notify_peers_of_dht_update()
                print(f"Removed file link: {filename}")
            else:
                print("Error: File not in shared list")
        else:
            print("Unknown command")


if __name__ == "__main__":
    start_p2p()