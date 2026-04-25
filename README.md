# Distributed Google Drive Storage

This project is a Python CLI app that splits files into chunks, encrypts each chunk, and distributes those chunks across multiple Google Drive accounts.

## What The App Does

- Splits a local file into 5 MB chunks.
- Encrypts each chunk with a local Fernet key stored in `.key`.
- Uploads chunks across multiple Google Drive accounts in parallel.
- Keeps local registries so files can be downloaded, moved, or deleted later.
- Supports distributed folder creation across all connected Drive accounts.

## Project Files

- `app.py`: entry point for the CLI.
- `auth_manager.py`: handles OAuth login and multiple Drive accounts.
- `storage_manager.py`: upload, download, delete, and folder logic.
- `config.py`: Drive scope, block size, and encryption key setup.
- `credentials.json`: Google OAuth client credentials downloaded from Google Cloud.
- `accounts_config.json`: saved list of connected Google accounts.
- `token_*.json`: OAuth tokens for each signed-in account.
- `registry.json`: metadata for uploaded distributed files.
- `folders_registry.json`: metadata for distributed folders.
- `.key`: Fernet encryption key used for file chunk encryption and decryption.

## Requirements

- Python 3.10 or newer recommended
- A Google Cloud project
- Google Drive API enabled in that project
- An OAuth client configured for a desktop app

## 1. Create A Google Cloud Project

1. Open the Google Cloud Console: https://console.cloud.google.com/
2. Create a new project, or select an existing one.
3. Make sure billing is not required for basic Drive API testing, but your Google account must be in good standing.

## 2. Enable The Google Drive API

1. In Google Cloud Console, open `APIs & Services`.
2. Click `Enable APIs and Services`.
3. Search for `Google Drive API`.
4. Open it and click `Enable`.

Official reference:
- https://developers.google.com/workspace/drive/api/quickstart/python

## 3. Configure OAuth Consent Screen

1. In Google Cloud Console, go to `Google Auth Platform` or `APIs & Services` > `OAuth consent screen`.
2. Choose `External` if you are using personal Gmail accounts.
3. Fill in the required app details such as:
   - App name
   - User support email
   - Developer contact email
4. Add yourself as a test user if the app is in testing mode.
5. Save the consent screen configuration.

Important:
- This app requests full Drive access with the scope `https://www.googleapis.com/auth/drive`.
- If your app is still in testing mode, only listed test users can sign in.

Official references:
- https://developers.google.com/workspace/drive/api/guides/api-specific-auth
- https://developers.google.com/workspace/guides/configure-oauth-consent

## 4. Create OAuth Credentials For A Desktop App

1. In Google Cloud Console, open `APIs & Services` > `Credentials`.
2. Click `Create Credentials`.
3. Choose `OAuth client ID`.
4. For application type, choose `Desktop app`.
5. Give it a name and create it.
6. Download the JSON file.
7. Place that file in the project folder and rename it to `credentials.json`.

Expected location:

```text
bda-poject/credentials.json
```

## 5. Set Up Python Locally

From the project directory:

```powershell
cd C:\Users\veera\Documents\projects\bda-poject
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks activation, you may need:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Official Python virtual environment documentation:
- https://docs.python.org/3/library/venv.html

## 6. Run The App

```powershell
python app.py
```

On first launch:

1. The app loads saved account tokens if they already exist.
2. If no accounts are configured, it asks you to add one.
3. A browser window opens for Google sign-in and consent.
4. After approval, the app stores:
   - `token_0.json` for the first account
   - `accounts_config.json` with account metadata
5. If `.key` does not exist, the app creates it automatically.

## 7. Add More Google Accounts

From the app menu:

1. Open `accounts`
2. Choose `add account`
3. Sign in with another Google account
4. Repeat as needed

Each added account gets its own token file like:

- `token_0.json`
- `token_1.json`
- `token_2.json`

The upload logic distributes chunks across available accounts in round-robin order.

## How Encryption Works

- The app uses `cryptography.fernet.Fernet`.
- The encryption key is stored in `.key`.
- Every uploaded chunk is encrypted before being sent to Drive.
- Downloaded chunks are decrypted locally before reassembly.

Important:
- If you delete or replace `.key`, previously uploaded files will no longer decrypt correctly.
- Back up `.key` safely if you need long-term access to uploaded data.

## Generated Files

These files are created automatically during use:

- `.key`
- `accounts_config.json`
- `token_*.json`
- `registry.json`
- `folders_registry.json`

These are already ignored by `.gitignore` and should usually not be committed.

## Typical Workflow

1. Start the app with `python app.py`
2. Add one or more Google accounts
3. Upload a file
4. The app:
   - splits it into chunks
   - encrypts each chunk
   - uploads chunks across accounts
   - records metadata in `registry.json`
5. Later, use `pull` to reconstruct the original file locally
6. Use `delete` to remove cloud chunks and clean the registry

## Troubleshooting

### `credentials.json` missing

The OAuth flow will fail if `credentials.json` is not present in the project directory.

### `.key` changed or deleted

Previously uploaded files may become unreadable because decryption depends on the original key.

### Browser sign-in fails

Check:

- Google Drive API is enabled
- OAuth client type is `Desktop app`
- your Google account is listed as a test user if the app is still in testing mode

### Tokens seem stale

Delete the affected `token_*.json` file and sign in again through the app.

### `no authorized accounts found`

Use the `accounts` menu and add at least one Google account.

## Security Notes

- `credentials.json` and `token_*.json` are sensitive.
- `.key` is extremely sensitive because it controls decryption of your stored files.
- Do not commit any of those files to a public repository.

## References

- Google Drive API Python quickstart: https://developers.google.com/workspace/drive/api/quickstart/python
- Drive API auth scopes: https://developers.google.com/workspace/drive/api/guides/api-specific-auth
- OAuth consent screen setup: https://developers.google.com/workspace/guides/configure-oauth-consent
- Python `venv` docs: https://docs.python.org/3/library/venv.html
