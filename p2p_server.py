import socket
import os
import threading
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)

# Configuration
CHUNK_SIZE = 1024 * 1024  # 1MB chunks
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Store shared file info
shared_file = None


# Serve the UI
@app.route('/')
def index():
    return render_template('index.html')


# Handle file upload
@app.route('/upload', methods=['POST'])
def upload_file():
    global shared_file
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    file_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(file_path)
    shared_file = file_path
    return jsonify({"message": "File uploaded successfully", "filename": file.filename})


# Serve file chunks
@app.route('/download/<filename>')
def download_file(filename):
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404

    def generate_chunks():
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    return app.response_class(generate_chunks(), mimetype='application/octet-stream')


# Background server to announce availability (simplified)
def announce_presence():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    message = b"P2P_FILE_SERVER"
    while True:
        sock.sendto(message, ('255.255.255.255', 5001))
        threading.Event().wait(5)  # Announce every 5 seconds


if __name__ == "__main__":
    threading.Thread(target=announce_presence, daemon=True).start()
    app.run(host='0.0.0.0', port=5001, debug=True)