import os
import time
import tempfile
import shutil
from flask import Flask, render_template, request, jsonify, send_file
from auth_manager import AccountManager
from storage_manager import DistributedStorageManager
from config import BLOCK_SIZE

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Global state — initialized once on startup
# ---------------------------------------------------------------------------
acc_manager = None
dist_manager = None

def init_managers():
    """Initialize the account and storage managers."""
    global acc_manager, dist_manager
    acc_manager = AccountManager()
    dist_manager = DistributedStorageManager()
    # Migration: ensure all registry entries have a parent_path
    updated = False
    for name, info in dist_manager.registry.items():
        if 'parent_path' not in info:
            info['parent_path'] = 'root'
            updated = True
    if updated:
        dist_manager._save_json(dist_manager.registry_path, dist_manager.registry)

# ---------------------------------------------------------------------------
# Page Routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')

# ---------------------------------------------------------------------------
# API: File Listing
# ---------------------------------------------------------------------------
@app.route('/api/files')
def api_list_files():
    """Return files and folders at the given virtual path."""
    path = request.args.get('path', 'root')
    items = []

    # 1. Find subfolders
    for folder_path, ids in dist_manager.folder_registry.items():
        parts = folder_path.split('/')
        parent = '/'.join(parts[:-1])
        name = parts[-1]
        if parent == path:
            items.append({
                'type': 'dir',
                'name': name,
                'path': folder_path,
                'ids': ids,
                'size': None,
                'chunks': None,
                'modified': _format_time(None)
            })

    # 2. Find files
    for name, info in dist_manager.registry.items():
        if info.get('parent_path') == path:
            items.append({
                'type': 'file',
                'name': name,
                'size': info['file_size'],
                'chunks': len(info['chunks']),
                'modified': _format_time(info.get('timestamp'))
            })

    return jsonify({'items': items, 'path': path})

# ---------------------------------------------------------------------------
# API: Upload
# ---------------------------------------------------------------------------
@app.route('/api/upload', methods=['POST'])
def api_upload():
    """Handle file upload — saves temp, then distributes."""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    parent_path = request.form.get('parent_path', 'root')

    # Build parent IDs map from folder registry
    parent_ids_map = {}
    if parent_path != 'root' and parent_path in dist_manager.folder_registry:
        parent_ids_map = dist_manager.folder_registry[parent_path]

    # Save to temp
    temp_dir = os.path.join(os.path.dirname(__file__), 'temp_uploads')
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, file.filename)

    try:
        file.save(temp_path)
        success = dist_manager.upload_distributed(
            temp_path, file.filename, acc_manager,
            parent_ids_map=parent_ids_map,
            parent_path=parent_path
        )
        if success:
            return jsonify({'success': True, 'name': file.filename})
        else:
            return jsonify({'success': False, 'error': 'Upload failed — check server logs'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        # Cleanup temp file
        if os.path.exists(temp_path):
            os.remove(temp_path)

# ---------------------------------------------------------------------------
# API: Download
# ---------------------------------------------------------------------------
@app.route('/api/download/<path:filename>')
def api_download(filename):
    """Reassemble and stream a distributed file to the browser."""
    if filename not in dist_manager.registry:
        return jsonify({'success': False, 'error': f'File "{filename}" not in registry'}), 404

    temp_dir = os.path.join(os.path.dirname(__file__), 'temp_downloads')
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, filename)

    try:
        success = dist_manager.download_distributed(filename, temp_path, acc_manager)
        if success:
            return send_file(temp_path, as_attachment=True, download_name=filename)
        else:
            return jsonify({'success': False, 'error': 'Download reassembly failed'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------------------------------------------------------------------------
# API: Delete
# ---------------------------------------------------------------------------
@app.route('/api/files/<path:filename>', methods=['DELETE'])
def api_delete(filename):
    """Delete a distributed file and all its chunks."""
    try:
        success = dist_manager.delete_distributed(filename, acc_manager)
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Delete failed'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------------------------------------------------------------------------
# API: Create Folder
# ---------------------------------------------------------------------------
@app.route('/api/folders', methods=['POST'])
def api_create_folder():
    """Create a new distributed folder."""
    data = request.get_json()
    name = data.get('name', '').strip()
    parent_path = data.get('parent_path', 'root')

    if not name:
        return jsonify({'success': False, 'error': 'Folder name is required'}), 400

    path_key = f"{parent_path}/{name}"

    # Check if folder already exists
    if path_key in dist_manager.folder_registry:
        return jsonify({'success': False, 'error': 'Folder already exists'}), 409

    parent_ids_map = {}
    if parent_path != 'root' and parent_path in dist_manager.folder_registry:
        parent_ids_map = dist_manager.folder_registry[parent_path]

    result = dist_manager.mkdir_distributed(name, acc_manager, path_key, parent_ids_map=parent_ids_map)
    if result:
        return jsonify({'success': True, 'path': path_key})
    else:
        return jsonify({'success': False, 'error': 'Folder creation failed'}), 500

# ---------------------------------------------------------------------------
# API: Accounts
# ---------------------------------------------------------------------------
@app.route('/api/accounts')
def api_accounts():
    """Return list of connected accounts."""
    accounts = acc_manager.get_accounts_info()
    return jsonify({'accounts': accounts})

@app.route('/api/accounts/add', methods=['POST'])
def api_add_account():
    """Add a new Google Drive account (triggers OAuth flow)."""
    try:
        data = request.get_json() or {}
        account_name = data.get('name', '').strip()
        if not account_name:
            account_name = f"Account {len(acc_manager.accounts) + 1}"
        
        success = acc_manager.add_account_web(account_name)
        if success:
            return jsonify({'success': True, 'name': account_name})
        else:
            return jsonify({'success': False, 'error': 'Authentication failed or was cancelled'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------------------------------------------------------------------------
# API: Storage Stats
# ---------------------------------------------------------------------------
@app.route('/api/storage/stats')
def api_storage_stats():
    """Return aggregated storage statistics."""
    file_count = len(dist_manager.registry)
    folder_count = len(dist_manager.folder_registry)
    
    total_bytes = 0
    total_chunks = 0
    for name, info in dist_manager.registry.items():
        total_bytes += info.get('file_size', 0)
        total_chunks += len(info.get('chunks', []))

    return jsonify({
        'files': file_count,
        'folders': folder_count,
        'total_size': _format_size(total_bytes),
        'total_bytes': total_bytes,
        'chunks': total_chunks
    })

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _format_size(bytes_val):
    """Format bytes into human-readable string."""
    if not bytes_val:
        return '0 B'
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    val = float(bytes_val)
    while val >= 1024 and i < len(units) - 1:
        val /= 1024
        i += 1
    return f"{val:.1f} {units[i]}" if i > 0 else f"{int(val)} B"

def _format_time(timestamp):
    """Format a Unix timestamp into a user-friendly string."""
    if not timestamp:
        return 'Today'
    
    now = time.time()
    diff = now - timestamp
    
    if diff < 60:
        return 'Just now'
    elif diff < 3600:
        mins = int(diff / 60)
        return f'{mins}m ago'
    elif diff < 86400:
        hours = int(diff / 3600)
        return f'{hours}h ago'
    elif diff < 172800:
        return 'Yesterday'
    elif diff < 604800:
        days = int(diff / 86400)
        return f'{days}d ago'
    else:
        return time.strftime('%b %d, %Y', time.localtime(timestamp))

# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    init_managers()
    print("\n  [*] DriveMesh is running at http://localhost:5000\n")
    app.run(debug=True, port=5000, use_reloader=False)