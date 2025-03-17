import socket
import requests
import os
import threading

# Configuration
DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
CHUNK_SIZE = 1024 * 1024  # 1MB chunks
SERVER_PORT = 5000  # Match the server's port


# Discover peers on the local network
def discover_peers():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('', 5000))
    peers = set()
    print("Listening for peers...")
    while True:
        data, addr = sock.recvfrom(1024)
        if data == b"P2P_FILE_SERVER":
            peers.add(addr[0])
            print(f"Discovered peer: {addr[0]}")
    return peers


# Download file from a peer
def download_file(peer_ip, filename):
    url = f"http://{peer_ip}:{SERVER_PORT}/download/{filename}"
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)

    print(f"Attempting to download from {url}")
    try:
        with open(file_path, 'wb') as f:
            response = requests.get(url, stream=True, timeout=10)
            response.raise_for_status()  # Raise an error for bad responses

            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0

            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:  # Only process if there's data
                    downloaded += len(chunk)
                    f.write(chunk)
                    percentage = (downloaded / total_size) * 100 if total_size > 0 else 100
                    print(f"Downloading: {percentage:.2f}%")

            # Final check to ensure 100% is printed only once
            if total_size > 0 and downloaded >= total_size:
                print("Downloading: 100.00%")
            elif total_size == 0:
                print("Downloading: 100.00% (Size unknown)")

        print(f"File downloaded to {file_path}")
    except requests.exceptions.RequestException as e:
        print(f"Error downloading file: {e}")


if __name__ == "__main__":
    # Start discovery in a thread
    threading.Thread(target=discover_peers, daemon=True).start()
    # Example: Download a file from a known peer
    peer_ip = input("Enter peer IP (e.g., 192.168.1.x): ")
    filename = input("Enter filename to download: ")
    download_file(peer_ip, filename)

# 10.21.233.106