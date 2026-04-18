from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from config import cipher, BLOCK_SIZE
from ui_utils import UI
import os
import io
import json
import concurrent.futures
import time
import shutil

class DistributedStorageManager:
    """Manages chunking, encryption, and parallel distribution of files/folders."""
    def __init__(self, registry_path="registry.json", folder_registry_path="folders_registry.json"):
        self.registry_path = registry_path
        self.folder_registry_path = folder_registry_path
        self.registry = self._load_json(self.registry_path)
        self.folder_registry = self._load_json(self.folder_registry_path)

    def _load_json(self, path):
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
        return {}

    def _save_json(self, path, data):
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

    def _upload_chunk_task(self, creds, local_path, byte_offset, size, chunk_name, chunk_idx, acc_idx, parent_id=None):
        """Worker task: reads, encrypts, and uploads a chunk directly from memory."""
        try:
            # 1. Read & Encrypt in-memory
            with open(local_path, 'rb') as f:
                f.seek(byte_offset)
                chunk_data = f.read(size)
            
            encrypted_chunk = cipher.encrypt(chunk_data)
            
            # 2. Upload to Drive
            service = build("drive", "v3", credentials=creds)
            file_metadata = {"name": chunk_name, "appProperties": {"type": "chunk", "order": str(chunk_idx)}}
            if parent_id:
                file_metadata["parents"] = [parent_id]
            
            media = MediaIoBaseUpload(io.BytesIO(encrypted_chunk), mimetype="application/octet-stream", resumable=False)
            drive_file = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
                
            return {
                "account_id": acc_idx,
                "drive_id": drive_file.get("id"),
                "order": chunk_idx
            }
        except Exception as e:
            UI.status(f"fail: chunk {chunk_idx}: {e}", success=False)
            return None

    def upload_distributed(self, local_path, remote_name, acc_manager, parent_ids_map=None, parent_path='root'):
        if not acc_manager.creds_list:
            UI.status("no authorized accounts found", success=False)
            return False

        num_accounts = len(acc_manager.creds_list)
        file_size = os.path.getsize(local_path)
        total_chunks = (file_size + BLOCK_SIZE - 1) // BLOCK_SIZE
        
        UI.info(f"{remote_name} -> {total_chunks} chunks (Turbo Mode)")
        chunks_info = [None] * total_chunks
        
        # High concurrency: 5 threads per account
        workers = num_accounts * 5
        completed = 0
        
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                UI.progress_bar(0, total_chunks, prefix="uploading")
                upload_tasks = []
                
                for c_idx in range(total_chunks):
                    acc_idx = c_idx % num_accounts # Distribute round-robin for better balance
                    creds = acc_manager.creds_list[acc_idx]
                    
                    byte_offset = c_idx * BLOCK_SIZE
                    size = min(BLOCK_SIZE, file_size - byte_offset)
                    chunk_name = f"{remote_name}.chunk{c_idx}"
                    
                    current_parent_id = None
                    if parent_ids_map and str(acc_idx) in parent_ids_map:
                        current_parent_id = parent_ids_map[str(acc_idx)]
                    
                    upload_tasks.append(executor.submit(
                        self._upload_chunk_task, creds, local_path, byte_offset, size, chunk_name, c_idx, acc_idx, current_parent_id
                    ))
                
                for future in concurrent.futures.as_completed(upload_tasks):
                    res = future.result()
                    if res:
                        chunks_info[res['order']] = res
                        completed += 1
                        UI.progress_bar(completed, total_chunks, prefix="uploading")

            if None in chunks_info:
                UI.status("some chunks failed to upload", success=False)
                return False

            self.registry[remote_name] = {
                "file_size": file_size,
                "chunks": chunks_info,
                "parent_path": parent_path,
                "timestamp": time.time()
            }
            self._save_json(self.registry_path, self.registry)
            UI.status(f"distributed upload complete: {remote_name}")
            return True

        except Exception as e:
            UI.status(f"critical upload error: {e}", success=False)
            return False

    def mkdir_distributed(self, folder_name, acc_manager, parent_ids_map=None):
        """Creates a folder in all accounts and tracks them together."""
        if not acc_manager.creds_list: return None
        
        UI.info(f"creating distributed folder: {folder_name}...")
        new_ids_map = {}
        
        def _mkdir_task(creds, acc_idx, p_id):
            try:
                service = build("drive", "v3", credentials=creds)
                file_metadata = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
                if p_id: file_metadata["parents"] = [p_id]
                folder = service.files().create(body=file_metadata, fields="id").execute()
                return acc_idx, folder.get("id")
            except Exception as e:
                print(f"Error in acc {acc_idx}: {e}")
                return acc_idx, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(acc_manager.creds_list)) as executor:
            tasks = []
            for i, creds in enumerate(acc_manager.creds_list):
                p_id = parent_ids_map.get(str(i)) if parent_ids_map else None
                tasks.append(executor.submit(_mkdir_task, creds, i, p_id))
            
            for future in concurrent.futures.as_completed(tasks):
                idx, f_id = future.result()
                if f_id: new_ids_map[str(idx)] = f_id
        
        # Save to registry (logical path or just the map)
        # For simplicity, we'll let app.py handle the path-to-map logic if needed
        UI.status(f"distributed folder '{folder_name}' synced across {len(new_ids_map)} accounts")
        return new_ids_map

    def _download_chunk_task(self, creds, chunk_id, order, temp_dir):
        """Worker task for parallel chunk download and decryption."""
        try:
            service = build("drive", "v3", credentials=creds)
            request = service.files().get_media(fileId=chunk_id)
            
            enc_path = os.path.join(temp_dir, f"chunk_{order}.enc")
            dec_path = os.path.join(temp_dir, f"chunk_{order}.dec")
            
            with open(enc_path, 'wb') as f:
                downloader = MediaIoBaseDownload(f, request)
                done = False
                while done is False:
                    _, done = downloader.next_chunk()
            
            with open(enc_path, 'rb') as f_in:
                decrypted_data = cipher.decrypt(f_in.read())
                with open(dec_path, 'wb') as f_out:
                    f_out.write(decrypted_data)
            
            os.remove(enc_path)
            return {"order": order, "path": dec_path}
        except Exception as e:
            UI.status(f"fail: chunk {order}: {e}", success=False)
            return None

    def download_distributed(self, remote_name, local_path, acc_manager):
        if remote_name not in self.registry:
            UI.status(f"file '{remote_name}' not in registry", success=False)
            return False
        
        file_info = self.registry[remote_name]
        chunks = sorted(file_info['chunks'], key=lambda x: x['order'])
        num_chunks = len(chunks)
        num_accounts = len(acc_manager.creds_list)
        
        UI.info(f"fetching {num_chunks} chunks...")
        
        temp_dir = tempfile.mkdtemp()
        chunk_paths = [None] * num_chunks
        completed = 0
        
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                download_tasks = []
                UI.progress_bar(0, num_chunks, prefix="downloading")
                for chunk in chunks:
                    acc_idx = chunk['account_id']
                    creds = acc_manager.creds_list[acc_idx]
                    download_tasks.append(executor.submit(self._download_chunk_task, creds, chunk['drive_id'], chunk['order'], temp_dir))
                
                for future in concurrent.futures.as_completed(download_tasks):
                    res = future.result()
                    if res:
                        chunk_paths[res['order']] = res['path']
                        completed += 1
                        UI.progress_bar(completed, num_chunks, prefix="downloading")

            if None in chunk_paths:
                UI.status("failed to retrieve all chunks", success=False)
                return False

            UI.info("assembling stream...")
            with open(local_path, 'wb') as output_f:
                for path in chunk_paths:
                    with open(path, 'rb') as cf:
                        shutil.copyfileobj(cf, output_f)
                
            UI.status("reassembled successfully")
            return True

        finally:
            shutil.rmtree(temp_dir)

    def delete_distributed(self, remote_name, acc_manager):
        """Permanently deletes a distributed file and all its chunks from the cloud."""
        if remote_name not in self.registry:
            UI.status(f"file '{remote_name}' not in registry", success=False)
            return False
        
        file_info = self.registry[remote_name]
        chunks = file_info['chunks']
        num_chunks = len(chunks)
        num_accounts = len(acc_manager.creds_list)
        
        UI.info(f"deleting {num_chunks} chunks from drive...")
        completed = 0
        
        def _delete_chunk_task(creds, drive_id, order):
            try:
                service = build("drive", "v3", credentials=creds)
                service.files().delete(fileId=drive_id).execute()
                return order
            except Exception as e:
                # If chunk already deleted or account missing permissions
                return order # Still return order to show progress/account for it

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            delete_tasks = []
            UI.progress_bar(0, num_chunks, prefix="purging")
            for chunk in chunks:
                acc_idx = chunk['account_id']
                if acc_idx < len(acc_manager.creds_list):
                    creds = acc_manager.creds_list[acc_idx]
                    delete_tasks.append(executor.submit(_delete_chunk_task, creds, chunk['drive_id'], chunk['order']))
            
            for future in concurrent.futures.as_completed(delete_tasks):
                res = future.result()
                if res is not None:
                    completed += 1
                    UI.progress_bar(completed, num_chunks, prefix="purging")

        del self.registry[remote_name]
        self._save_json(self.registry_path, self.registry)
        UI.status(f"removed {remote_name} from distributed storage")
        return True

    def list_distributed_files(self):
        if not self.registry:
            print("No distributed files found in registry.")
            return None
        
        print("\n--- Distributed Files (Registry) ---")
        files = list(self.registry.keys())
        for i, name in enumerate(files):
            info = self.registry[name]
            size_mb = info['file_size'] / (1024 * 1024)
            print(f"[{i}] {name} ({size_mb:.2f} MB, {len(info['chunks'])} chunks)")
        
        return files
