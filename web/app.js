// Function-hosted dashboard requests stay same-origin. A static-site deployment
// can pass ?apiBase=...&code=... or set these window variables before this script.
const params = new URLSearchParams(window.location.search);
const sameOriginApiBase = window.location.pathname.startsWith('/api') ? '/api' : '';
const API_BASE = (params.get('apiBase') || window.STOCKLENS_API_BASE || sameOriginApiBase).replace(/\/$/, '');
const functionCode = params.get('code') || window.STOCKLENS_FUNCTION_KEY || '';
const api = path => `${API_BASE}${path}${functionCode ? `?code=${encodeURIComponent(functionCode)}` : ''}`;
const notice = document.querySelector('#notice');
const documents = document.querySelector('#documents');
const escapeHtml = value => String(value || '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));

async function loadDocuments() {
  const response = await fetch(api('/documents'));
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || data.error || 'Could not load documents');
  documents.innerHTML = data.documents.length
    ? data.documents.map(d => `<article><div><span class="status ${d.status.toLowerCase()}">${escapeHtml(d.status)}</span><h3>${escapeHtml(d.fileName)}</h3><small>${new Date(d.uploadedAt).toLocaleString()} · ${escapeHtml(d.fileType)}</small></div><a href="${api(`/documents/${encodeURIComponent(d.documentId)}/download`)}">Download original</a>${d.extractedText ? `<details><summary>Extracted text</summary><pre>${escapeHtml(d.extractedText)}</pre></details>` : ''}${d.errorMessage ? `<p class="error">${escapeHtml(d.errorMessage)}</p>` : ''}</article>`).join('')
    : '<p class="hint">No documents yet. Upload a PDF or Word document to start the flow.</p>';
}

document.querySelector('#upload-form').addEventListener('submit', async event => {
  event.preventDefault();
  const file = document.querySelector('#file').files[0];
  if (!file) return;
  notice.textContent = 'Uploading…';
  try {
    const formData = new FormData();
    formData.append('file', file);
    const response = await fetch(api('/documents'), { method: 'POST', body: formData });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || 'Upload failed');
    notice.textContent = 'Uploaded. Processing usually takes a few seconds—refresh to view the result.';
    event.target.reset();
    await loadDocuments();
  } catch (error) {
    notice.textContent = error.message;
  }
});

document.querySelector('#refresh').onclick = () => loadDocuments().catch(error => { notice.textContent = error.message; });
loadDocuments().catch(error => { notice.textContent = error.message; });
