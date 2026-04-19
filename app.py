import os
import time
from auth_manager import AccountManager
from storage_manager import DistributedStorageManager
from drive_helpers import list_files_selectable, create_folder
from ui_utils import UI

def migrate_registry(dist_manager):
    """Ensures all registry entries have a parent_path field."""
    updated = False
    for name, info in dist_manager.registry.items():
        if 'parent_path' not in info:
            info['parent_path'] = 'root'
            updated = True
    if updated:
        dist_manager._save_json(dist_manager.registry_path, dist_manager.registry)

def main():
    UI.clear()
    UI.header()
    
    acc_manager = AccountManager()
    dist_manager = DistributedStorageManager()
    
    migrate_registry(dist_manager)
    
    if not acc_manager.creds_list:
        UI.status("no accounts found. please add one.", success=False)
        if not acc_manager.add_account():
            UI.status("auth failed. exiting.", success=False)
            return

    # Virtual Folder context
    current_folder_name = 'root'
    # IDs mapped by account index (as strings)
    current_ids_map = {} 
    folder_stack = [] # (name, ids_map)
    
    while True:
        UI.location(len(acc_manager.creds_list), current_folder_name)
        
        print(f"\n  {UI.DIM}[1]{UI.RESET} explore   {UI.DIM}[2]{UI.RESET} upload    {UI.DIM}[3]{UI.RESET} pull")
        print(f"  {UI.DIM}[4]{UI.RESET} move      {UI.DIM}[5]{UI.RESET} delete    {UI.DIM}[6]{UI.RESET} registry")
        print(f"  {UI.DIM}[7]{UI.RESET} mkdir     {UI.DIM}[8]{UI.RESET} accounts  {UI.DIM}[9]{UI.RESET} exit")
        
        choice = input(f"\n{UI.DIM}› mode:{UI.RESET} ")
        
        if choice == '1': # Virtual Explorer
            while True:
                UI.clear()
                UI.header()
                UI.location(len(acc_manager.creds_list), current_folder_name)
                print(f"\n {UI.DIM}browsing virtual distributed registry{UI.RESET}")
                
                display_items = []
                if current_folder_name != 'root':
                    display_items.append({'type': 'back', 'name': '..'})
                
                # 1. Find virtual subfolders
                # folders_registry keys are paths like "root/abc"
                for path, ids in dist_manager.folder_registry.items():
                    parent = "/".join(path.split("/")[:-1])
                    name = path.split("/")[-1]
                    if parent == current_folder_name:
                        display_items.append({'type': 'dir', 'name': name, 'ids': ids, 'path': path})
                
                # 2. Find virtual files
                for name, info in dist_manager.registry.items():
                    if info.get('parent_path') == current_folder_name:
                        display_items.append({'type': 'file', 'name': name, 'size': info['file_size']})

                if not display_items:
                    print(f"\n {UI.DIM}this virtual folder is empty.{UI.RESET}")
                else:
                    for i, item in enumerate(display_items):
                        if item['type'] == 'dir':
                            color = UI.CYAN
                            label = "dir "
                        elif item['type'] == 'file':
                            color = UI.GREEN
                            label = "file"
                        else:
                            color = UI.DIM
                            label = "back"
                        
                        size_str = f" ({item['size']/(1024*1024):.1f} MB)" if item['type'] == 'file' else ""
                        print(f" {UI.DIM}{i:2}{UI.RESET} {color}{label}{UI.RESET} {item['name']}{UI.DIM}{size_str}{UI.RESET}")
                
                v_choice = input(f"\n{UI.DIM}› select index (q):{UI.RESET} ")
                if v_choice.lower() == 'q': break
                
                try:
                    idx = int(v_choice)
                    if 0 <= idx < len(display_items):
                        selected = display_items[idx]
                        if selected['type'] == 'back':
                            if folder_stack:
                                current_folder_name, current_ids_map = folder_stack.pop()
                            else:
                                current_folder_name, current_ids_map = 'root', {}
                        elif selected['type'] == 'dir':
                            folder_stack.append((current_folder_name, current_ids_map.copy()))
                            current_folder_name = selected['path']
                            current_ids_map = selected['ids']
                        else:
                            UI.info(f"selected file: {selected['name']}")
                            print(f"  {UI.DIM}[1]{UI.RESET} pull  {UI.DIM}[2]{UI.RESET} move  {UI.DIM}[3]{UI.RESET} delete  {UI.DIM}[4]{UI.RESET} back")
                            op = input(f"\n{UI.DIM}› action:{UI.RESET} ")
                            if op == '1' or op == '2':
                                local_p = input(f"{UI.DIM}› save as (default: downloaded_{selected['name']}):{UI.RESET} ")
                                if not local_p: local_p = f"downloaded_{selected['name']}"
                                if dist_manager.download_distributed(selected['name'], local_p, acc_manager):
                                    if op == '2': dist_manager.delete_distributed(selected['name'], acc_manager)
                            elif op == '3':
                                dist_manager.delete_distributed(selected['name'], acc_manager)
                    else: UI.status("invalid index", success=False)
                except ValueError: pass
            UI.clear()
            UI.header()

        elif choice == '2': # Upload
            local_path = input(f"{UI.DIM}› path:{UI.RESET} ").strip('"')
            if not os.path.exists(local_path):
                UI.status("file not found", success=False)
                continue
            remote_name = input(f"{UI.DIM}› name (default: {os.path.basename(local_path)}):{UI.RESET} ")
            if not remote_name: remote_name = os.path.basename(local_path)
            
            # Pass the current IDs map and the virtual path
            dist_manager.upload_distributed(local_path, remote_name, acc_manager, 
                                           parent_ids_map=current_ids_map, 
                                           parent_path=current_folder_name)
            
        elif choice == '3' or choice == '4': # Pull (3) or Move (4)
            files = dist_manager.list_distributed_files()
            if files:
                idx_choice = input(f"\n{UI.DIM}› select index:{UI.RESET} ")
                try:
                    idx = int(idx_choice)
                    if 0 <= idx < len(files):
                        remote_name = files[idx]
                        local_path = input(f"{UI.DIM}› save as (default: downloaded_{remote_name}):{UI.RESET} ")
                        if not local_path: local_path = f"downloaded_{remote_name}"
                        
                        if dist_manager.download_distributed(remote_name, local_path, acc_manager):
                            if choice == '4': # Move
                                UI.info(f"move: purging cloud storage for {remote_name}...")
                                dist_manager.delete_distributed(remote_name, acc_manager)
                except ValueError: UI.status("numeric input required", success=False)
                
        elif choice == '5': # Delete
            files = dist_manager.list_distributed_files()
            if files:
                idx_choice = input(f"\n{UI.DIM}› select index to delete:{UI.RESET} ")
                try:
                    idx = int(idx_choice)
                    if 0 <= idx < len(files):
                        dist_manager.delete_distributed(files[idx], acc_manager)
                except ValueError: UI.status("numeric input required", success=False)

        elif choice == '6':
            dist_manager.list_distributed_files()
            
        elif choice == '7': # Mkdir (Distributed)
            folder_name = input(f"{UI.DIM}› folder name:{UI.RESET} ")
            path_key = f"{current_folder_name}/{folder_name}"
            dist_manager.mkdir_distributed(folder_name, acc_manager, path_key, parent_ids_map=current_ids_map)
            
        elif choice == '8': # Accounts
            while True:
                UI.info("account management")
                accounts = acc_manager.get_accounts_info()
                for i, acc in enumerate(accounts):
                    print(f"  {UI.DIM}[{i}]{UI.RESET} {acc['name']} {UI.DIM}({acc['token_file']}){UI.RESET}")
                print(f"\n  {UI.DIM}[1]{UI.RESET} add account  {UI.DIM}[2]{UI.RESET} back")
                subchoice = input(f"\n{UI.DIM}› action:{UI.RESET} ")
                if subchoice == '1': acc_manager.add_account()
                elif subchoice == '2': break

        elif choice == '9' or choice == 'exit':
            UI.status("session ended")
            break
        else: UI.status("unknown command", success=False)

if __name__ == "__main__":
    main()