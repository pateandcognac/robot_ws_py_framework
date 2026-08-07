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
        copyVisibleButton.addEventListener('click', () => {
            const entries = visibleEntries(panel)
                .map((entry) => entry.dataset.copyMarkdown || '')
                .filter(Boolean)
                .join('\n\n---\n\n');
            const panelTitle = panel.querySelector('h2').textContent.trim();
            const text = entries ? `# ${panelTitle}\n\n${entries}` : '';
            copyText(text, copyVisibleButton);
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
        card.dataset.searchText = copyPayload.toLowerCase();
        card.dataset.copyText = copyPayload;
        card.dataset.copyMarkdown = markdownEntryText(entry);

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
        const type = data.type || data.role || data.kind || 'entry';
        const id = data.id || `line ${entry.line}`;
        return `${type} - ${id}`;
    }

    function metaForEntry(entry) {
        const data = entry.data || {};
        const bits = [`line ${entry.line}`];
        if (data.timestamp) bits.push(formatTimestamp(data.timestamp));
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

    function flashButton(button) {
        if (!button) return;
        const previous = button.textContent;
        button.textContent = 'Copied';
        setTimeout(() => {
            button.textContent = previous;
        }, 900);
    }
});
