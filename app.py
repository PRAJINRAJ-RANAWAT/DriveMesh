import os.path, io, time
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from cryptography.fernet import Fernet


SCOPES = ["https://www.googleapis.com/auth/drive"]
cs = Fernet(open(".key", 'rb').read())


def create_folder(creds, folder_name = "script_generated_folder"):
	"""Create a folder and prints the folder ID
	Returns : Folder Id

	Load pre-authorized user credentials from the environment.
	TODO(developer) - See https://developers.google.com/identity
	for guides on implementing OAuth2 for the application.
	"""

	try:
		# create drive api client
		service = build("drive", "v3", credentials=creds)
		file_metadata = {
				"name": folder_name,
				"mimeType": "application/vnd.google-apps.folder",
		}

		# pylint: disable=maybe-no-member
		file = service.files().create(body=file_metadata, fields="id").execute()
		print(f'Folder ID: "{file.get("id")}".')
		return file.get("id")

	except HttpError as error:
		print(f"An error occurred: {error}")
		return None
	


def auth():
	# The file token.json stores the user's access and refresh tokens, and is
	# created automatically when the authorization flow completes for the first
	# time.
	creds = None
	if os.path.exists("token.json"):
		creds = Credentials.from_authorized_user_file("token.json", SCOPES)
	# If there are no (valid) credentials available, let the user log in.
	if not creds or not creds.valid:
		if creds and creds.expired and creds.refresh_token:
			creds.refresh(Request())
		else:
			flow = InstalledAppFlow.from_client_secrets_file(
					"credentials.json", SCOPES
			)
			creds = flow.run_local_server(port=0)
		# Save the credentials for the next run
		with open("token.json", "w") as token:
			token.write(creds.to_json())

	return creds


def list_10_files(creds):
	try:
		service = build("drive", "v3", credentials=creds)

		# Call the Drive v3 API
		results = (
				service.files()
				.list(pageSize=10, fields="nextPageToken, files(id, name)")
				.execute()
		)
		items = results.get("files", [])

		if not items:
			print("No files found.")
			return
		print("Files:")
		for item in items:
			print(f"{item['name']} ({item['id']})")
	except HttpError as error:
		# TODO(developer) - Handle errors from drive API.
		print(f"An error occurred: {error}")
	

def upload_encrypt_file(creds, folder_id, file_name):
	"""Upload a file to the specified folder and prints file ID, folder ID
	Args: Id of the folder
	Returns: ID of the file uploaded

	Load pre-authorized user credentials from the environment.
	TODO(developer) - See https://developers.google.com/identity
	for guides on implementing OAuth2 for the application.
	"""

	try:
		# create drive api client
		service = build("drive", "v3", credentials=creds)

		fs = io.BytesIO(cs.encrypt(open("app.py", 'rb').read()))

		file_metadata = {"name": file_name, "parents": [folder_id]}
		media = MediaIoBaseUpload(
				fs, mimetype="document/text", resumable=True
		)
		# pylint: disable=maybe-no-member
		file = (
				service.files()
				.create(body=file_metadata, media_body=media, fields="id")
				.execute()
		)
		print(f'File ID: "{file.get("id")}".')
		return file.get("id")

	except HttpError as error:
		print(f"An error occurred: {error}")
		return None


def download_decrypt_file(creds, real_file_id, file_name):
	"""Downloads a file
	Args:
			real_file_id: ID of the file to download
	Returns : IO object with location.

	Load pre-authorized user credentials from the environment.
	TODO(developer) - See https://developers.google.com/identity
	for guides on implementing OAuth2 for the application.
	"""

	try:
		# create drive api client
		service = build("drive", "v3", credentials=creds)

		file_id = real_file_id

		# pylint: disable=maybe-no-member
		request = service.files().get_media(fileId=file_id)
		file = io.BytesIO()
		downloader = MediaIoBaseDownload(file, request)
		done = False
		while done is False:
			status, done = downloader.next_chunk()
			print(f"Download {int(status.progress() * 100)}.")
		
		fdc = cs.decrypt(file.getvalue())
		open(file_name, 'wb').write(fdc)

	except HttpError as error:
		print(f"An error occurred: {error}")
		file = None
	
	return fdc.decode()


def main():
	"""Shows basic usage of the Drive v3 API.
	Prints the names and ids of the first 10 files the user has access to.
	"""
	creds = auth()

	## READ

	# list_10_files(creds)

	folder_id = create_folder(creds, "script_generated_folder")

	file_id = upload_encrypt_file(creds, folder_id, "script_generated_file.py")

	time.sleep(20)

	print(download_decrypt_file(creds, file_id, "decrypted_file.py"))

	

if __name__ == "__main__":
	main()