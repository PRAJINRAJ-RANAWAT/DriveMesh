import os
import json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from config import SCOPES

def auth(token_file="token.json"):
    """Handles Google Drive API authentication for existing tokens."""
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
                creds = flow.run_local_server(port=0)
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open(token_file, "w") as token:
            token.write(creds.to_json())

    return creds

def auth_new_account(token_file):
    """Forces Google account picker + consent screen for adding a NEW account.
    Always shows the login page so the user can pick a different Google account."""
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(
        port=0,
        prompt='select_account'  # Force account picker every time
    )
    
    with open(token_file, "w") as token:
        token.write(creds.to_json())
    
    return creds

class AccountManager:
    """Manages multiple Google Drive accounts."""
    def __init__(self, config_path="accounts_config.json"):
        self.config_path = config_path
        self.accounts = self._load_config()
        self.creds_list = []
        self._refresh_all_creds()

    def _load_config(self):
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as f:
                return json.load(f)
        return []

    def _save_config(self):
        with open(self.config_path, 'w') as f:
            json.dump(self.accounts, f)

    def _refresh_all_creds(self):
        self.creds_list = []
        for acc in self.accounts:
            creds = auth(acc['token_file'])
            if creds:
                self.creds_list.append(creds)

    def add_account(self):
        print("\n--- Adding New Google Account ---")
        account_index = len(self.accounts)
        token_file = f"token_{account_index}.json"
        creds = auth(token_file)
        if creds:
            account_name = input("Enter a name/nickname for this account: ")
            self.accounts.append({
                'name': account_name,
                'token_file': token_file,
                'id': account_index
            })
            self._save_config()
            self.creds_list.append(creds)
            print(f"Account '{account_name}' added successfully.")
            return True
        return False

    def add_account_web(self, account_name="Account"):
        """Web-friendly version: accepts name as parameter instead of input()."""
        # Find next unused token index — skip orphaned token files from failed attempts
        account_index = len(self.accounts)
        token_file = f"token_{account_index}.json"
        while os.path.exists(token_file):
            account_index += 1
            token_file = f"token_{account_index}.json"
        
        creds = auth_new_account(token_file)
        if creds:
            self.accounts.append({
                'name': account_name,
                'token_file': token_file,
                'id': account_index
            })
            self._save_config()
            self.creds_list.append(creds)
            return True
        return False

    def get_accounts_info(self):
        return self.accounts
