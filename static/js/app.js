/* ============================================
   DriveMesh — Client-Side Application Logic
   ============================================ */

const App = {
  currentPath: 'root',
  pathStack: [],
  files: [],
  accounts: [],
  stats: {},
  selectedFile: null,

  /* ---------- Initialization ---------- */
  async init() {
    this.bindEvents();
    await Promise.all([
      this.loadFiles(),
      this.loadAccounts(),
      this.loadStats()
    ]);
  },

  bindEvents() {
    // New Folder
    document.getElementById('btn-new-folder').addEventListener('click', () => this.openModal('folder-modal'));
    document.getElementById('folder-form').addEventListener('submit', (e) => { e.preventDefault(); this.createFolder(); });
    
    // Upload
    document.getElementById('btn-upload').addEventListener('click', () => this.openModal('upload-modal'));
    document.getElementById('upload-form').addEventListener('submit', (e) => { e.preventDefault(); this.uploadFile(); });

    // Account
    document.getElementById('account-form').addEventListener('submit', (e) => { e.preventDefault(); this.submitAddAccount(); });

    // Upload zone drag/drop
    const zone = document.getElementById('upload-zone');
    const fileInput = document.getElementById('file-input');

    zone.addEventListener('click', () => fileInput.click());
    zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('dragover'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
    zone.addEventListener('drop', (e) => {
      e.preventDefault();
      zone.classList.remove('dragover');
      if (e.dataTransfer.files.length) {
        fileInput.files = e.dataTransfer.files;
        this.showSelectedFile(e.dataTransfer.files[0]);
      }
    });
    fileInput.addEventListener('change', () => {
      if (fileInput.files.length) this.showSelectedFile(fileInput.files[0]);
    });

    // Modal close on overlay click
    document.querySelectorAll('.modal-overlay').forEach(overlay => {
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) this.closeModal(overlay.id);
      });
    });

    // Cancel buttons
    document.querySelectorAll('.btn-cancel').forEach(btn => {
      btn.addEventListener('click', () => {
        const modal = btn.closest('.modal-overlay');
        if (modal) this.closeModal(modal.id);
      });
    });

    // Close context menu on click elsewhere
    document.addEventListener('click', (e) => {
      if (!e.target.closest('.context-menu')) {
        document.getElementById('context-menu').classList.remove('active');
      }
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        document.querySelectorAll('.modal-overlay.active').forEach(m => this.closeModal(m.id));
        document.getElementById('context-menu').classList.remove('active');
      }
    });

    // Add Account button
    document.getElementById('btn-add-account').addEventListener('click', () => this.addAccount());
  },

  /* ---------- API Calls ---------- */
  async apiGet(url) {
    try {
      const res = await fetch(url);
      return await res.json();
    } catch (err) {
      console.error('API GET error:', err);
      return null;
    }
  },

  async apiPost(url, data) {
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      });
      return await res.json();
    } catch (err) {
      console.error('API POST error:', err);
      return null;
    }
  },

  async apiDelete(url) {
    try {
      const res = await fetch(url, { method: 'DELETE' });
      return await res.json();
    } catch (err) {
      console.error('API DELETE error:', err);
      return null;
    }
  },

  /* ---------- File Loading ---------- */
  async loadFiles() {
    const data = await this.apiGet(`/api/files?path=${encodeURIComponent(this.currentPath)}`);
    if (data) {
      this.files = data.items || [];
      this.renderFiles();
    }
  },

  async loadAccounts() {
    const data = await this.apiGet('/api/accounts');
    if (data) {
      this.accounts = data.accounts || [];
      this.renderAccounts();
    }
  },

  async loadStats() {
    const data = await this.apiGet('/api/storage/stats');
    if (data) {
      this.stats = data;
      this.renderStats();
    }
  },

  /* ---------- Rendering ---------- */
  renderFiles() {
    const tbody = document.getElementById('file-tbody');
    const emptyState = document.getElementById('empty-state');
    
    if (!this.files.length) {
      tbody.innerHTML = '';
      emptyState.classList.remove('hidden');
      return;
    }

    emptyState.classList.add('hidden');

    // Sort: folders first, then files
    const sorted = [...this.files].sort((a, b) => {
      if (a.type === 'dir' && b.type !== 'dir') return -1;
      if (a.type !== 'dir' && b.type === 'dir') return 1;
      return a.name.localeCompare(b.name);
    });

    tbody.innerHTML = sorted.map((item, idx) => {
      const isFolder = item.type === 'dir';
      const icon = isFolder ? this.icons.folder : this.icons.file;
      const iconClass = isFolder ? 'folder' : 'file';
      const size = isFolder ? '—' : this.formatSize(item.size);
      const chunks = isFolder ? '—' : (item.chunks || '—');
      const modified = item.modified || '—';

      return `
        <tr data-index="${idx}" data-type="${item.type}" data-name="${this.escapeHtml(item.name)}" 
            ${isFolder ? `data-path="${this.escapeHtml(item.path)}"` : ''}
            ondblclick="App.handleRowDblClick(this)"
            oncontextmenu="App.handleRowContext(event, this)">
          <td>
            <div class="file-name-cell">
              <div class="file-icon ${iconClass}">${icon}</div>
              <span class="file-name">${this.escapeHtml(item.name)}</span>
            </div>
          </td>
          <td>${size}</td>
          <td>${chunks}</td>
          <td>${modified}</td>
          <td class="file-actions-cell">
            ${!isFolder ? `
              <button class="file-action-btn" onclick="event.stopPropagation(); App.downloadFile('${this.escapeHtml(item.name)}')" title="Download">
                ${this.icons.download}
              </button>
              <button class="file-action-btn danger" onclick="event.stopPropagation(); App.deleteFile('${this.escapeHtml(item.name)}')" title="Delete">
                ${this.icons.trash}
              </button>
            ` : `
              <button class="file-action-btn danger" onclick="event.stopPropagation(); App.deleteFolder('${this.escapeHtml(item.path)}', '${this.escapeHtml(item.name)}')" title="Delete Folder">
                ${this.icons.trash}
              </button>
            `}
          </td>
        </tr>
      `;
    }).join('');
  },

  renderAccounts() {
    const list = document.getElementById('account-list');
    if (!this.accounts.length) {
      list.innerHTML = '<li class="account-item" style="color:var(--text-muted);">No accounts connected</li>';
      return;
    }
    list.innerHTML = this.accounts.map((acc, idx) => `
      <li class="account-item">
        <span class="account-dot"></span>
        <span style="flex:1;">${this.escapeHtml(acc.name)}</span>
        <button class="account-remove-btn" onclick="event.stopPropagation(); App.removeAccount(${idx}, '${this.escapeHtml(acc.name)}')" title="Remove account">
          &times;
        </button>
      </li>
    `).join('');
  },

  renderStats() {
    document.getElementById('stat-files').textContent = this.stats.files || 0;
    document.getElementById('stat-folders').textContent = this.stats.folders || 0;
    document.getElementById('stat-size').textContent = this.stats.total_size || '0 B';
    document.getElementById('stat-chunks').textContent = this.stats.chunks || 0;
  },

  renderBreadcrumb() {
    const container = document.getElementById('breadcrumb');
    const parts = this.currentPath === 'root' ? ['root'] : this.currentPath.split('/');
    
    container.innerHTML = parts.map((part, i) => {
      const isLast = i === parts.length - 1;
      const displayName = part === 'root' ? 'Home' : part;
      const pathUpTo = parts.slice(0, i + 1).join('/');
      
      let html = '';
      if (i > 0) html += '<span class="breadcrumb-sep">/</span>';
      
      if (isLast) {
        html += `<span class="breadcrumb-item active">${this.escapeHtml(displayName)}</span>`;
      } else {
        html += `<span class="breadcrumb-item" onclick="App.navigateTo('${this.escapeHtml(pathUpTo)}')">${this.escapeHtml(displayName)}</span>`;
      }
      return html;
    }).join('');
  },

  /* ---------- Navigation ---------- */
  handleRowDblClick(row) {
    const type = row.dataset.type;
    if (type === 'dir') {
      const path = row.dataset.path;
      this.navigateTo(path);
    }
  },

  navigateTo(path) {
    this.currentPath = path;
    this.renderBreadcrumb();
    this.loadFiles();
  },

  /* ---------- Context Menu ---------- */
  handleRowContext(event, row) {
    event.preventDefault();
    const type = row.dataset.type;
    const name = row.dataset.name;
    
    if (type === 'dir') return; // No context menu for folders

    this.selectedFile = name;
    const menu = document.getElementById('context-menu');
    
    // Position
    menu.style.left = event.pageX + 'px';
    menu.style.top = event.pageY + 'px';
    menu.classList.add('active');

    // Adjust if off-screen
    const rect = menu.getBoundingClientRect();
    if (rect.right > window.innerWidth) menu.style.left = (event.pageX - rect.width) + 'px';
    if (rect.bottom > window.innerHeight) menu.style.top = (event.pageY - rect.height) + 'px';
  },

  contextDownload() {
    if (this.selectedFile) this.downloadFile(this.selectedFile);
    document.getElementById('context-menu').classList.remove('active');
  },

  contextDelete() {
    if (this.selectedFile) this.deleteFile(this.selectedFile);
    document.getElementById('context-menu').classList.remove('active');
  },

  /* ---------- File Operations ---------- */
  async createFolder() {
    const input = document.getElementById('folder-name-input');
    const name = input.value.trim();
    if (!name) return;

    const res = await this.apiPost('/api/folders', {
      name: name,
      parent_path: this.currentPath
    });

    if (res && res.success) {
      this.showToast('Folder created', `"${name}" created successfully`, 'success');
      this.closeModal('folder-modal');
      input.value = '';
      await Promise.all([this.loadFiles(), this.loadStats()]);
    } else {
      this.showToast('Error', res?.error || 'Failed to create folder', 'error');
    }
  },

  async uploadFile() {
    const fileInput = document.getElementById('file-input');
    if (!fileInput.files.length) return;

    const file = fileInput.files[0];
    const formData = new FormData();
    formData.append('file', file);
    formData.append('parent_path', this.currentPath);

    this.closeModal('upload-modal');

    // Show progress toast
    const toastId = this.showProgressToast(`Uploading ${file.name}...`);

    try {
      const xhr = new XMLHttpRequest();
      
      xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
          const pct = Math.round((e.loaded / e.total) * 100);
          this.updateProgressToast(toastId, pct);
        }
      });

      xhr.addEventListener('load', async () => {
        if (xhr.status === 200) {
          const res = JSON.parse(xhr.responseText);
          if (res.success) {
            this.completeProgressToast(toastId, 'Upload complete!');
            await Promise.all([this.loadFiles(), this.loadStats()]);
          } else {
            this.errorProgressToast(toastId, res.error || 'Upload failed');
          }
        } else {
          this.errorProgressToast(toastId, 'Upload failed');
        }
      });

      xhr.addEventListener('error', () => {
        this.errorProgressToast(toastId, 'Network error during upload');
      });

      xhr.open('POST', '/api/upload');
      xhr.send(formData);
    } catch (err) {
      this.errorProgressToast(toastId, 'Upload error: ' + err.message);
    }

    // Reset file input
    fileInput.value = '';
    document.getElementById('upload-file-info').classList.remove('visible');
  },

  async downloadFile(name) {
    this.showToast('Downloading', `Starting download of "${name}"...`, 'info');
    
    try {
      const res = await fetch(`/api/download/${encodeURIComponent(name)}`);
      if (res.ok) {
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        this.showToast('Download complete', `"${name}" downloaded successfully`, 'success');
      } else {
        const err = await res.json();
        this.showToast('Download failed', err.error || 'Unknown error', 'error');
      }
    } catch (err) {
      this.showToast('Download error', err.message, 'error');
    }
  },

  async deleteFolder(path, name) {
    if (!confirm(`Delete folder "${name}"? This will remove the folder and ALL its contents from connected drives.`)) return;

    this.showToast('Deleting folder', `Removing "${name}" and its contents...`, 'info');
    const res = await this.apiDelete(`/api/folders/${encodeURIComponent(path)}`);
    if (res && res.success) {
      this.showToast('Folder deleted', `"${name}" and ${res.deleted_files || 0} file(s) removed`, 'success');
      await Promise.all([this.loadFiles(), this.loadStats()]);
    } else {
      this.showToast('Delete failed', res?.error || 'Unknown error', 'error');
    }
  },

  async removeAccount(index, name) {
    if (!confirm(`Remove account "${name}"? The token will be deleted.`)) return;

    const res = await this.apiDelete(`/api/accounts/${index}`);
    if (res && res.success) {
      this.showToast('Account removed', `"${name}" disconnected`, 'success');
      await Promise.all([this.loadAccounts(), this.loadStats()]);
    } else {
      this.showToast('Error', res?.error || 'Failed to remove account', 'error');
    }
  },

  async deleteFile(name) {
    if (!confirm(`Delete "${name}"? This will remove all chunks from connected drives.`)) return;
    
    const res = await this.apiDelete(`/api/files/${encodeURIComponent(name)}`);
    if (res && res.success) {
      this.showToast('Deleted', `"${name}" removed from distributed storage`, 'success');
      await Promise.all([this.loadFiles(), this.loadStats()]);
    } else {
      this.showToast('Delete failed', res?.error || 'Unknown error', 'error');
    }
  },

  async addAccount() {
    this.openModal('account-modal');
  },

  async submitAddAccount() {
    const input = document.getElementById('account-name-input');
    const name = input.value.trim();
    if (!name) return;

    this.closeModal('account-modal');
    input.value = '';

    this.showToast('Adding Account', 'Opening Google authentication — complete sign-in in the popup...', 'info');
    const res = await this.apiPost('/api/accounts/add', { name: name });
    if (res && res.success) {
      this.showToast('Account Added', `"${res.name}" connected successfully`, 'success');
      await Promise.all([this.loadAccounts(), this.loadStats()]);
    } else {
      this.showToast('Error', res?.error || 'Failed to add account', 'error');
    }
  },

  /* ---------- Modal Management ---------- */
  openModal(id) {
    const modal = document.getElementById(id);
    modal.classList.add('active');
    // Focus first input
    setTimeout(() => {
      const input = modal.querySelector('input:not([type=file])');
      if (input) input.focus();
    }, 150);
  },

  closeModal(id) {
    document.getElementById(id).classList.remove('active');
  },

  /* ---------- Upload Zone Helpers ---------- */
  showSelectedFile(file) {
    const info = document.getElementById('upload-file-info');
    document.getElementById('upload-file-name-text').textContent = file.name;
    document.getElementById('upload-file-size-text').textContent = this.formatSize(file.size);
    info.classList.add('visible');
  },

  /* ---------- Toast System ---------- */
  _toastCounter: 0,

  showToast(title, message, type = 'info') {
    const container = document.getElementById('toast-container');
    const id = 'toast-' + (++this._toastCounter);
    
    const toast = document.createElement('div');
    toast.id = id;
    toast.className = `toast ${type}`;
    toast.innerHTML = `
      <div class="toast-header">
        <span class="toast-title">${this.escapeHtml(title)}</span>
      </div>
      <div style="font-size:0.8rem; color:var(--text-secondary); margin-top:2px;">${this.escapeHtml(message)}</div>
    `;
    container.appendChild(toast);

    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(100%)';
      toast.style.transition = 'all 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, 4000);
  },

  showProgressToast(title) {
    const container = document.getElementById('toast-container');
    const id = 'toast-' + (++this._toastCounter);
    
    const toast = document.createElement('div');
    toast.id = id;
    toast.className = 'toast';
    toast.innerHTML = `
      <div class="toast-header">
        <span class="toast-title">${this.escapeHtml(title)}</span>
        <span class="toast-percent">0%</span>
      </div>
      <div class="toast-bar"><div class="toast-bar-fill"></div></div>
    `;
    container.appendChild(toast);
    return id;
  },

  updateProgressToast(id, percent) {
    const toast = document.getElementById(id);
    if (!toast) return;
    toast.querySelector('.toast-percent').textContent = percent + '%';
    toast.querySelector('.toast-bar-fill').style.width = percent + '%';
  },

  completeProgressToast(id, message) {
    const toast = document.getElementById(id);
    if (!toast) return;
    toast.className = 'toast success';
    toast.querySelector('.toast-title').textContent = message;
    toast.querySelector('.toast-percent').textContent = '100%';
    toast.querySelector('.toast-bar-fill').style.width = '100%';
    
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(100%)';
      toast.style.transition = 'all 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, 3000);
  },

  errorProgressToast(id, message) {
    const toast = document.getElementById(id);
    if (!toast) return;
    toast.className = 'toast error';
    toast.querySelector('.toast-title').textContent = message;
    toast.querySelector('.toast-percent').textContent = '✕';
    
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(100%)';
      toast.style.transition = 'all 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, 5000);
  },

  /* ---------- Utility ---------- */
  formatSize(bytes) {
    if (!bytes || bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return (bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
  },

  escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  },

  /* ---------- SVG Icons ---------- */
  icons: {
    folder: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
    </svg>`,
    file: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/>
      <polyline points="13 2 13 9 20 9"/>
    </svg>`,
    download: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
      <polyline points="7 10 12 15 17 10"/>
      <line x1="12" y1="15" x2="12" y2="3"/>
    </svg>`,
    trash: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="3 6 5 6 21 6"/>
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
    </svg>`,
    upload: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
      <polyline points="17 8 12 3 7 8"/>
      <line x1="12" y1="3" x2="12" y2="15"/>
    </svg>`,
    folderPlus: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
      <line x1="12" y1="11" x2="12" y2="17"/>
      <line x1="9" y1="14" x2="15" y2="14"/>
    </svg>`,
    cloud: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/>
    </svg>`,
  }
};

/* ---------- Boot ---------- */
document.addEventListener('DOMContentLoaded', () => {
  App.renderBreadcrumb();
  App.init();
});
