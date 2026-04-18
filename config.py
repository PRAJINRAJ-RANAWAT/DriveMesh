import os
from cryptography.fernet import Fernet

SCOPES = ["https://www.googleapis.com/auth/drive"]
BLOCK_SIZE = 5 * 1024 * 1024 

KEY_FILE = ".key"
if not os.path.exists(KEY_FILE):
    key = Fernet.generate_key()
    with open(KEY_FILE, 'wb') as f:
        f.write(key)
else:
    with open(KEY_FILE, 'rb') as f:
        key = f.read()

cipher = Fernet(key)
