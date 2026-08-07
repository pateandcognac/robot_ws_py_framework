document.addEventListener('DOMContentLoaded', () => {
    const workspacePath = document.getElementById('workspace-path');
    const searchInput = document.getElementById('search-input');
    const refreshButton = document.getElementById('refresh-button');
    const panels = Array.from(document.querySelectorAll('.log-panel'));
    let latestFiles = {};

    refreshButton.addEventListener('click', loadLogs);
    searchInput.addEventListener('input', applySearch);

    panels.forEach((panel) => {
        const copyVisibleButton = panel.querySelector('.copy-visible-button');
        const copyVisibleHtmlButton = panel.querySelector('.copy-visible-html-button');
        copyVisibleButton.addEventListener('click', () => {
            const entries = visibleEntries(panel)
                .map((entry) => entry.dataset.copyMarkdown || '')
                .filter(Boolean)
                .join('\n\n---\n\n');
            const panelTitle = panel.querySelector('h2').textContent.trim();
            const text = entries ? `# ${panelTitle}\n\n${entries}` : '';
            copyText(text, copyVisibleButton);
        });
        copyVisibleHtmlButton.addEventListener('click', () => {
            const entries = visibleEntries(panel)
                .map((entry) => entry.dataset.copyHtml || '')
                .filter(Boolean);
            const panelTitle = panel.querySelector('h2').textContent.trim();
            const html = entries.length ? htmlExportDocument(panelTitle, entries) : '';
            copyHtml(html, copyVisibleHtmlButton);
        });
    });

    loadLogs();

    async function loadLogs() {
        refreshButton.disabled = true;
        refreshButton.textContent = 'Refreshing';
        try {
            const response = await fetch('/api/state-jsonl', {cache: 'no-store'});
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            const data = await response.json();
            workspacePath.textContent = data.workspace_path || 'Workspace path unavailable';
            latestFiles = data.files || {};
            renderAll();
            applySearch();
        } catch (error) {
            panels.forEach((panel) => {
                panel.querySelector('.log-panel-status').textContent = `Could not load logs: ${error.message}`;
                panel.querySelector('.log-list').innerHTML = '';
            });
        } finally {
            refreshButton.disabled = false;
            refreshButton.textContent = 'Refresh';
        }
    }

    function renderAll() {
        panels.forEach((panel) => {
            const key = panel.dataset.logPanel;
            renderPanel(panel, latestFiles[key]);
        });
    }

    function renderPanel(panel, fileInfo) {
        const status = panel.querySelector('.log-panel-status');
        const list = panel.querySelector('.log-list');
        list.innerHTML = '';

        if (!fileInfo || !fileInfo.available) {
            status.textContent = fileInfo && fileInfo.error ? fileInfo.error : 'No data available.';
            const empty = document.createElement('div');
            empty.className = 'log-empty';
            empty.textContent = 'Nothing to show yet.';
            list.appendChild(empty);
            return;
        }

        const entries = fileInfo.entries || [];
        status.textContent = `${entries.length} entries`;
        if (!entries.length) {
            const empty = document.createElement('div');
            empty.className = 'log-empty';
            empty.textContent = 'File exists, but it is empty.';
            list.appendChild(empty);
            return;
        }

        entries.forEach((entry) => {
            list.appendChild(createEntry(entry));
        });
    }

    function createEntry(entry) {
        const data = entry.data || {};
        const title = titleForEntry(entry);
        const copyPayload = prettyEntryText(entry);

        const card = document.createElement('article');
        card.className = 'log-entry';
        card.dataset.messageType = canonicalType(data.type || data.role || data.kind);
        card.dataset.searchText = copyPayload.toLowerCase();
        card.dataset.copyText = copyPayload;
        card.dataset.copyMarkdown = markdownEntryText(entry);
        card.dataset.copyHtml = htmlEntryText(entry);

        const header = document.createElement('div');
        header.className = 'log-entry-header';

        const titleElement = document.createElement('div');
        titleElement.className = 'log-entry-title';
        titleElement.textContent = title;

        const copyButton = document.createElement('button');
        copyButton.className = 'copy-entry-button';
        copyButton.type = 'button';
        copyButton.textContent = 'Copy';
        copyButton.addEventListener('click', () => copyText(copyPayload, copyButton));

        header.appendChild(titleElement);
        header.appendChild(copyButton);

        const meta = document.createElement('div');
        meta.className = 'log-entry-meta';
        meta.textContent = metaForEntry(entry);

        const content = document.createElement('pre');
        content.className = 'log-entry-content';
        if (entry.parse_error) {
            content.classList.add('log-entry-error');
            content.textContent = `JSON parse error: ${entry.parse_error}\n\n${entry.raw}`;
        } else {
            content.textContent = contentForEntry(data);
        }

        card.appendChild(header);
        card.appendChild(meta);
        card.appendChild(content);
        return card;
    }

    function titleForEntry(entry) {
        const data = entry.data || {};
        const presentation = typePresentation(data.type || data.role || data.kind);
        return `${presentation.icon} ${presentation.label}`;
    }

    function metaForEntry(entry) {
        const data = entry.data || {};
        const bits = [`line ${entry.line}`];
        if (data.timestamp) bits.push(formatTimestamp(data.timestamp));
        if (data.id) bits.push(`id ${data.id}`);
        if (data.filename) bits.push(`file ${data.filename}`);
        if (data.token_count !== undefined) bits.push(`${data.token_count} tokens`);
        return bits.join(' | ');
    }

    function contentForEntry(data) {
        if (typeof data.content === 'string') {
            return data.content;
        }
        if (typeof data.summary === 'string') {
            return data.summary;
        }
        if (typeof data.text === 'string') {
            return data.text;
        }
        return JSON.stringify(data, null, 2);
    }

    function prettyEntryText(entry) {
        if (entry.parse_error) {
            return `line ${entry.line}\nJSON parse error: ${entry.parse_error}\n${entry.raw}`;
        }
        return [
            titleForEntry(entry),
            metaForEntry(entry),
            '',
            contentForEntry(entry.data || {})
        ].join('\n');
    }

    function markdownEntryText(entry) {
        if (entry.parse_error) {
            return [
                `## Line ${entry.line}: JSON parse error`,
                '',
                `**Error:** ${entry.parse_error}`,
                '',
                fencedCode(entry.raw || '', 'text')
            ].join('\n');
        }

        const data = entry.data || {};
        let content;
        if (typeof data.content === 'string') {
            content = markdownContent(data, data.content);
        } else if (typeof data.summary === 'string') {
            content = markdownContent(data, data.summary);
        } else if (typeof data.text === 'string') {
            content = markdownContent(data, data.text);
        } else {
            content = fencedCode(JSON.stringify(data, null, 2), 'json');
        }

        return [
            `## ${titleForEntry(entry)}`,
            '',
            `*${metaForEntry(entry)}*`,
            '',
            content
        ].join('\n');
    }

    function markdownContent(data, content) {
        if (data.type !== 'me') return content;
        return content.replace(/<py(?:\s[^>]*)?>([\s\S]*?)<\/py>/gi, (match, code) => {
            const unwrappedCode = code.replace(/^\r?\n/, '').replace(/\r?\n$/, '');
            return fencedCode(unwrappedCode, 'py');
        });
    }

    function htmlEntryText(entry) {
        if (entry.parse_error) {
            return [
                '<details class="entry type-error" data-message-type="error" open>',
                '<summary>',
                '<span class="entry-title"><span aria-hidden="true">⚠️</span> Parse error</span>',
                `<span class="entry-meta">line ${entry.line}</span>`,
                '</summary>',
                '<div class="entry-body">',
                `<p class="error"><strong>Error:</strong> ${escapeHtml(entry.parse_error)}</p>`,
                `<pre><code>${escapeHtml(entry.raw || '')}</code></pre>`,
                '</div>',
                '</details>'
            ].join('\n');
        }

        const data = entry.data || {};
        const type = canonicalType(data.type || data.role || data.kind);
        const presentation = typePresentation(type);
        let content;
        if (typeof data.content === 'string') {
            content = htmlContent(data, data.content);
        } else if (typeof data.summary === 'string') {
            content = htmlContent(data, data.summary);
        } else if (typeof data.text === 'string') {
            content = htmlContent(data, data.text);
        } else {
            content = `<pre><code class="language-json">${escapeHtml(JSON.stringify(data, null, 2))}</code></pre>`;
        }

        return [
            `<details class="entry type-${escapeHtml(type)}" data-message-type="${escapeHtml(type)}" open>`,
            '<summary>',
            `<span class="entry-title"><span aria-hidden="true">${presentation.icon}</span> ${escapeHtml(presentation.label)}</span>`,
            `<span class="entry-meta">${escapeHtml(metaForEntry(entry))}</span>`,
            '</summary>',
            `<div class="entry-body">${content}</div>`,
            '</details>'
        ].join('\n');
    }

    function htmlContent(data, content) {
        if (data.type !== 'me') {
            return `<div class="message-text">${escapeHtml(content)}</div>`;
        }

        const blocks = [];
        const pyPattern = /<py(?:\s[^>]*)?>([\s\S]*?)<\/py>/gi;
        let lastIndex = 0;
        let match;
        while ((match = pyPattern.exec(content)) !== null) {
            if (match.index > lastIndex) {
                blocks.push(`<div class="message-text">${escapeHtml(content.slice(lastIndex, match.index))}</div>`);
            }
            const code = match[1].replace(/^\r?\n/, '').replace(/\r?\n$/, '');
            blocks.push(`<pre class="python"><code class="language-python">${escapeHtml(code)}</code></pre>`);
            lastIndex = pyPattern.lastIndex;
        }
        if (lastIndex < content.length) {
            blocks.push(`<div class="message-text">${escapeHtml(content.slice(lastIndex))}</div>`);
        }
        return blocks.length ? blocks.join('\n') : `<div class="message-text">${escapeHtml(content)}</div>`;
    }

    function canonicalType(value) {
        const type = String(value || 'entry').trim().toLowerCase();
        if (type === 'async_py') return 'py_async';
        if (type === 'synospsis') return 'synopsis';
        return type.replace(/[^a-z0-9_-]/g, '-') || 'entry';
    }

    function typePresentation(value) {
        const type = canonicalType(value);
        const presentations = {
            me: {icon: '🤖', label: 'Robot'},
            human: {icon: '👤', label: 'Human'},
            human_stt: {icon: '🎙️', label: 'Human · speech'},
            py_result: {icon: '🐍', label: 'Python result'},
            py_async: {icon: '⚡', label: 'Async Python'},
            system: {icon: '⚙️', label: 'System'},
            synopsis: {icon: '📝', label: 'Synopsis'},
            entry: {icon: '💬', label: 'Entry'}
        };
        return presentations[type] || {icon: '💬', label: type.replace(/_/g, ' ')};
    }

    function htmlExportDocument(panelTitle, entries) {
        const title = `Logos · ${panelTitle}`;
        return `<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>
:root{color-scheme:dark;--bg:#0c111b;--surface:#131b29;--raised:#1a2536;--text:#e8edf5;--muted:#91a0b5;--line:#2a3a50;--accent:#75baff;--code:#080d15;--shadow:0 12px 34px rgba(0,0,0,.24)}
:root[data-theme="light"]{color-scheme:light;--bg:#f3f6fa;--surface:#fff;--raised:#edf2f8;--text:#172033;--muted:#627086;--line:#d7e0eb;--accent:#0868c9;--code:#f5f7fa;--shadow:0 12px 34px rgba(31,49,75,.1)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button{font:inherit}.shell{width:min(1100px,calc(100% - 32px));margin:0 auto;padding:38px 0 64px}.topbar{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin-bottom:22px}.eyebrow{margin:0 0 4px;color:var(--accent);font-size:.72rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase}h1{margin:0;font-size:clamp(1.55rem,4vw,2.35rem);line-height:1.15}.count{margin:8px 0 0;color:var(--muted);font-size:.88rem}.controls{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px}.controls button{border:1px solid var(--line);border-radius:999px;background:var(--surface);color:var(--text);padding:7px 12px;cursor:pointer}.controls button:hover{border-color:var(--accent);color:var(--accent)}.controls button[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:var(--bg)}.entries{display:grid;gap:12px}.entry{overflow:hidden;border:1px solid var(--line);border-left:4px solid var(--type,#77869a);border-radius:12px;background:var(--surface);box-shadow:var(--shadow)}.entry summary{display:grid;grid-template-columns:max-content minmax(0,1fr);align-items:center;gap:16px;padding:13px 16px;background:var(--raised);cursor:pointer;list-style:none}.entry summary::-webkit-details-marker{display:none}.entry summary::after{content:"▾";grid-column:3;color:var(--muted);transition:transform .15s}.entry:not([open]) summary::after{transform:rotate(-90deg)}.entry-title{font-weight:760}.entry-meta{min-width:0;color:var(--muted);font:clamp(.68rem,1.6vw,.78rem)/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}.entry-body{padding:17px 18px}.message-text{white-space:pre-wrap;overflow-wrap:anywhere}.message-text:empty{display:none}pre{overflow:auto;margin:0;border:1px solid var(--line);border-radius:8px;background:var(--code);padding:14px;white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}pre.python{border-left:3px solid #ffd43b}.message-text+pre,pre+.message-text,.message-text+.message-text{margin-top:12px}.error{color:#ff8e8e}.type-me{--type:#61dafb}.type-human{--type:#8bd17c}.type-human_stt{--type:#d7a7ff}.type-py_result{--type:#ffd43b}.type-py_async{--type:#ff9f6e}.type-system{--type:#99a8ba}.type-synopsis{--type:#f28db2}.type-error{--type:#ff6969}
@media(max-width:720px){.shell{width:min(100% - 20px,1100px);padding-top:20px}.topbar{align-items:stretch;flex-direction:column}.controls{justify-content:flex-start}.entry summary{grid-template-columns:1fr auto;gap:4px 10px}.entry-meta{grid-column:1/-1;grid-row:2}.entry summary::after{grid-column:2;grid-row:1}}
@media print{.controls{display:none}.shell{width:100%;padding:0}.entry{break-inside:avoid;box-shadow:none}}
</style>
</head>
<body>
<main class="shell">
<header class="topbar">
<div><p class="eyebrow">Logos conversation log</p><h1>${escapeHtml(panelTitle)}</h1><p class="count">${entries.length} visible ${entries.length === 1 ? 'entry' : 'entries'} · exported ${escapeHtml(new Date().toLocaleString())}</p></div>
<nav class="controls" aria-label="Log controls">
<button id="theme-toggle" type="button">☀️ Light mode</button>
<button type="button" data-collapse-type="py_result" aria-pressed="false">Collapse Python results</button>
<button type="button" data-collapse-type="py_async" aria-pressed="false">Collapse async Python</button>
</nav>
</header>
<section class="entries" aria-label="Log entries">${entries.join('\n')}</section>
</main>
<script>
(()=>{const root=document.documentElement;const theme=document.getElementById('theme-toggle');theme.addEventListener('click',()=>{const light=root.dataset.theme!=='light';root.dataset.theme=light?'light':'dark';theme.textContent=light?'🌙 Dark mode':'☀️ Light mode'});document.querySelectorAll('[data-collapse-type]').forEach(button=>{button.addEventListener('click',()=>{const collapse=button.getAttribute('aria-pressed')!=='true';button.setAttribute('aria-pressed',String(collapse));document.querySelectorAll('[data-message-type="'+button.dataset.collapseType+'"]').forEach(entry=>entry.open=!collapse);button.textContent=(collapse?'Expand ':'Collapse ')+(button.dataset.collapseType==='py_result'?'Python results':'async Python')})})})();
</script>
</body>
</html>`;
    }

    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function fencedCode(value, language) {
        const text = String(value);
        const backtickRuns = text.match(/`+/g) || [];
        const longestRun = backtickRuns.reduce((longest, run) => Math.max(longest, run.length), 0);
        const fence = '`'.repeat(Math.max(3, longestRun + 1));
        return `${fence}${language}\n${text}\n${fence}`;
    }

    function formatTimestamp(value) {
        if (typeof value === 'number') {
            return new Date(value * 1000).toLocaleString();
        }
        const parsed = Date.parse(value);
        if (!Number.isNaN(parsed)) {
            return new Date(parsed).toLocaleString();
        }
        return String(value);
    }

    function applySearch() {
        const query = searchInput.value.trim().toLowerCase();
        panels.forEach((panel) => {
            const entries = Array.from(panel.querySelectorAll('.log-entry'));
            let visibleCount = 0;
            entries.forEach((entry) => {
                const matches = !query || entry.dataset.searchText.includes(query);
                entry.classList.toggle('hidden-by-search', !matches);
                if (matches) visibleCount += 1;
            });
            const status = panel.querySelector('.log-panel-status');
            const fileInfo = latestFiles[panel.dataset.logPanel];
            if (fileInfo && fileInfo.available) {
                const total = fileInfo.entries ? fileInfo.entries.length : 0;
                status.textContent = query ? `${visibleCount} of ${total} entries` : `${total} entries`;
            }
        });
    }

    function visibleEntries(panel) {
        return Array.from(panel.querySelectorAll('.log-entry:not(.hidden-by-search)'));
    }

    async function copyText(text, button) {
        if (!text) return;
        try {
            await navigator.clipboard.writeText(text);
            flashButton(button);
        } catch (error) {
            const textArea = document.createElement('textarea');
            textArea.value = text;
            textArea.style.position = 'fixed';
            textArea.style.left = '-9999px';
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            document.execCommand('copy');
            document.body.removeChild(textArea);
            flashButton(button);
        }
    }

    async function copyHtml(html, button) {
        if (!html) return;
        try {
            if (!navigator.clipboard.write || typeof ClipboardItem === 'undefined') {
                throw new Error('Rich clipboard writes are unavailable');
            }
            const item = new ClipboardItem({
                'text/html': new Blob([html], {type: 'text/html'}),
                'text/plain': new Blob([html], {type: 'text/plain'})
            });
            await navigator.clipboard.write([item]);
            flashButton(button);
        } catch (error) {
            await copyText(html, button);
        }
    }

    function flashButton(button) {
        if (!button) return;
        const previous = button.textContent;
        button.textContent = 'Copied';
        setTimeout(() => {
            button.textContent = previous;
        }, 900);
    }
});
