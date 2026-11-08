from googleapiclient.discovery import build
from ui_utils import UI

def list_files_selectable(creds, folder_id='root', folder_name='Root'):
    """Lists files from a specific folder on Drive and lets user select one."""
    try:
        service = build("drive", "v3", credentials=creds)
        query = f"'{folder_id}' in parents and trashed = false"
        results = service.files().list(q=query, pageSize=20, fields="files(id, name, mimeType)").execute()
        items = results.get("files", [])

        print(f"\n listing items in {UI.CYAN}/{folder_name}{UI.RESET}")
        display_items = []
        if folder_id != 'root':
            display_items.append({'id': '..', 'name': '..', 'mimeType': 'action'})
        
        display_items.extend(items)
        if not display_items:
            print(f" {UI.DIM}this folder is empty.{UI.RESET}")
            return None

        for i, item in enumerate(display_items):
            if item.get('mimeType') == 'application/vnd.google-apps.folder':
                type_str = f"{UI.CYAN}dir {UI.RESET}"
                name_str = f"{UI.CYAN}{item['name']}{UI.RESET}"
            elif item.get('mimeType') == 'action':
                type_str = f"{UI.DIM}back{UI.RESET}"
                name_str = f"{UI.DIM}{item['name']}{UI.RESET}"
            else:
                type_str = f"{UI.GREEN}file{UI.RESET}"
                name_str = f"{item['name']}"
                
            print(f" {UI.DIM}{i:2}{UI.RESET} {type_str} {name_str}")
        
        choice = input(f"\n{UI.DIM}› index ({UI.RESET}q{UI.DIM}):{UI.RESET} ")
        if choice.lower() == 'q': return None
        try:
            idx = int(choice)
            if 0 <= idx < len(display_items): return display_items[idx]
        except ValueError: pass
        UI.status("invalid selection", success=False)
        return None
    except Exception as error:
        UI.status(f"listing error: {error}", success=False)
        return None

def create_folder(creds, folder_name="script_generated_folder", parent_id=None):
    """Create a folder on Drive."""
    try:
        service = build("drive", "v3", credentials=creds)
        file_metadata = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id: file_metadata["parents"] = [parent_id]
        file = service.files().create(body=file_metadata, fields="id").execute()
        UI.status(f"created directory: {folder_name}")
        return file.get("id")
    except Exception as error:
        UI.status(f"creation failed: {error}", success=False)
        return None
