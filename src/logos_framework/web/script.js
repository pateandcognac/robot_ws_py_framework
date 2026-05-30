document.addEventListener('DOMContentLoaded', () => {
    const headerPanel = document.getElementById('header');
    const ioBufferPanel = document.getElementById('io-buffer');
    const chatTab = document.getElementById('chat-tab');
    const debugVisionTab = document.getElementById('debug-vision-tab');
    const debugVisionView = document.getElementById('debug-vision-view');
    const debugVisionCount = document.getElementById('debug-vision-count');
    const debugVisionTopicFilter = document.getElementById('debug-vision-topic-filter');
    const debugVisionPause = document.getElementById('debug-vision-pause');
    const debugVisionStatus = document.getElementById('debug-vision-status');
    const debugVisionGrid = document.getElementById('debug-vision-grid');
    const debugVisionModal = document.getElementById('debug-vision-modal');
    const debugVisionModalClose = document.getElementById('debug-vision-modal-close');
    const debugVisionModalImage = document.getElementById('debug-vision-modal-image');
    const debugVisionModalMeta = document.getElementById('debug-vision-modal-meta');
    const footerPanel = document.getElementById('footer');
    const humanInput = document.getElementById('human-input');
    const typeInput = document.getElementById('type-input');
    const loopCognitionCheckbox = document.getElementById('loop-cognition-checkbox');
    const modeToggle = document.getElementById('mode-toggle');
    const loopCognitionControls = document.getElementById('loop-cognition-controls');
    const middlePane = document.getElementById('middle-pane');
    const statusBar = document.getElementById('status-bar'); // NEW
    const runtimeConfigToggle = document.getElementById('runtime-config-toggle');
    const jsonlViewerButton = document.getElementById('jsonl-viewer-button');
    const hookRefreshButton = document.getElementById('hook-refresh-button');
    const runtimeConfigPopover = document.getElementById('runtime-config-popover');
    const apiProfileSelect = document.getElementById('api-profile-select');
    const modelPresetSelect = document.getElementById('model-preset-select');
    const modelInput = document.getElementById('model-input');
    const thinkingLevelSelect = document.getElementById('thinking-level-select');
    const mediaResolutionSelect = document.getElementById('media-resolution-select');
    const filesApiToggle = document.getElementById('files-api-toggle');
    const keyFailoverToggle = document.getElementById('key-failover-toggle');
    const throttlingToggle = document.getElementById('throttling-toggle');
    const runtimeConfigStatus = document.getElementById('runtime-config-status');
    const chromaUiButton = document.getElementById('chroma-ui-button');
    const paidKeyAlert = document.getElementById('paid-key-alert');
    const paidKeyAlertBody = document.getElementById('paid-key-alert-body');
    const paidKeyAlertClose = document.getElementById('paid-key-alert-close');
    let previousApiProfile = null;
    let latestDebugVision = {frames: [], topics: [], prefix: '/logos/debug_vision/'};
    let pausedDebugVision = null;

    Split(['#header', '#middle-pane', '#footer'], {
        sizes: [25, 50, 25],
        minSize: 100,
        gutterSize: 8,
        cursor: 'col-resize'
    });

    const socket = io();

    socket.on('connect', () => {
        console.log('Connected to server!');
    });

    socket.on('runtime_config_state', (data) => {
        maybeShowPaidKeyAlert(data);
        renderRuntimeConfig(data);
    });

    socket.on('full_update', (data) => {
        renderContentWithImages(headerPanel, data.header);
        const ioBufferBase = document.createElement('div');
        ioBufferBase.id = 'io-buffer-base';
        renderContentWithImages(ioBufferBase, data.io_buffer);
        ioBufferPanel.innerHTML = '';
        ioBufferPanel.appendChild(ioBufferBase);
        renderContentWithImages(footerPanel, data.footer);
        scrollToBottom(ioBufferPanel);
    });

    socket.on('debug_vision_update', (data) => {
        if (!debugVisionPause.checked) {
            latestDebugVision = data || {frames: [], topics: []};
            renderDebugVision();
        }
    });

    socket.on('append_io', (data) => {
        clearStatusBar(); // Hide spinner and finalize previous stream when new data arrives
        const newCell = createIoCell(data.type, data.content);
        ioBufferPanel.appendChild(newCell);
        scrollToBottom(ioBufferPanel);
    });

    socket.on('stream_chunk', (data) => {
        // Hide the "thinking..." status bar now that the response is streaming
        statusBar.style.display = 'none';
        statusBar.textContent = '';

        let streamingCell = ioBufferPanel.querySelector('[data-type="me-streaming"]');
        if (!streamingCell) {
            // This is the first chunk, so create a new cell for this response
            streamingCell = createIoCell('me', '');
            streamingCell.dataset.type = 'me-streaming';
            ioBufferPanel.appendChild(streamingCell);
        }
        
        // Append new content to the existing streaming cell
        const codeElement = streamingCell.querySelector('code');
        if (codeElement) {
            codeElement.textContent += data.content;
        }
        scrollToBottom(ioBufferPanel);
    });

    // Handle the thoughts/spinner
    socket.on('thought_update', (data) => {
        // A new thought means a new response cycle is starting.
        // Finalize the previous streaming cell before showing the new status.
        clearStatusBar(); 
        
        statusBar.style.display = 'block';
        statusBar.textContent = "Logos is thinking: " + data.content;
    });

    function clearStatusBar() {
        statusBar.style.display = 'none';
        statusBar.textContent = '';
        
        // Finalize the streaming cell by removing its special data-type.
        // This ensures the next stream will create a new cell.
        const streamingCell = ioBufferPanel.querySelector('[data-type="me-streaming"]');
        if (streamingCell) {
            delete streamingCell.dataset.type; 
        }
    }

    humanInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && event.ctrlKey) {
            event.preventDefault();
            sendMessage();
        }
    });

    modeToggle.addEventListener('change', () => {
        if (modeToggle.checked) {
            loopCognitionControls.style.display = 'none';
            typeInput.value = 'debug';
        } else {
            loopCognitionControls.style.display = 'inline';
            typeInput.value = 'human';
        }
    });

    humanInput.addEventListener('input', autoResizeTextarea);

    chatTab.addEventListener('click', () => {
        setMiddleView('chat');
    });

    debugVisionTab.addEventListener('click', () => {
        setMiddleView('debug-vision');
        renderDebugVision();
    });

    debugVisionCount.addEventListener('change', renderDebugVision);
    debugVisionTopicFilter.addEventListener('change', renderDebugVision);
    window.addEventListener('resize', renderDebugVision);
    debugVisionPause.addEventListener('change', () => {
        if (debugVisionPause.checked) {
            pausedDebugVision = JSON.parse(JSON.stringify(latestDebugVision));
        } else if (pausedDebugVision) {
            pausedDebugVision = null;
            fetchDebugVisionSnapshot();
        }
        renderDebugVision();
    });

    debugVisionModalClose.addEventListener('click', closeDebugVisionModal);
    debugVisionModal.addEventListener('click', (event) => {
        if (event.target === debugVisionModal) {
            closeDebugVisionModal();
        }
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !debugVisionModal.hidden) {
            closeDebugVisionModal();
        }
    });

    runtimeConfigToggle.addEventListener('click', (event) => {
        event.stopPropagation();
        runtimeConfigPopover.hidden = !runtimeConfigPopover.hidden;
    });

    jsonlViewerButton.addEventListener('click', () => {
        window.open('/logs', '_blank', 'noopener');
    });

    hookRefreshButton.addEventListener('click', () => {
        hookRefreshButton.disabled = true;
        hookRefreshButton.textContent = 'Running';
        socket.emit('hook_refresh');
        window.setTimeout(() => {
            hookRefreshButton.disabled = false;
            hookRefreshButton.textContent = 'Run Hooks';
        }, 1500);
    });

    paidKeyAlertClose.addEventListener('click', () => {
        paidKeyAlert.hidden = true;
    });

    paidKeyAlert.addEventListener('click', (event) => {
        if (event.target === paidKeyAlert) {
            paidKeyAlert.hidden = true;
        }
    });

    document.addEventListener('click', (event) => {
        if (
            !runtimeConfigPopover.hidden
            && !runtimeConfigPopover.contains(event.target)
            && event.target !== runtimeConfigToggle
        ) {
            runtimeConfigPopover.hidden = true;
        }
    });

    runtimeConfigPopover.addEventListener('click', (event) => {
        event.stopPropagation();
    });

    apiProfileSelect.addEventListener('change', () => {
        emitRuntimeConfig({api_profile: apiProfileSelect.value});
    });

    modelPresetSelect.addEventListener('change', () => {
        if (modelPresetSelect.value) {
            modelInput.value = modelPresetSelect.value;
            emitRuntimeConfig({model: modelPresetSelect.value});
        }
    });

    modelInput.addEventListener('change', () => {
        const model = modelInput.value.trim();
        if (model) {
            emitRuntimeConfig({model});
        }
    });

    thinkingLevelSelect.addEventListener('change', () => {
        emitRuntimeConfig({thinking_level: thinkingLevelSelect.value});
    });

    mediaResolutionSelect.addEventListener('change', () => {
        emitRuntimeConfig({media_resolution: mediaResolutionSelect.value});
    });

    filesApiToggle.addEventListener('change', () => {
        emitRuntimeConfig({use_files_api: filesApiToggle.checked});
    });

    keyFailoverToggle.addEventListener('change', () => {
        emitRuntimeConfig({key_failover: keyFailoverToggle.checked});
    });

    throttlingToggle.addEventListener('change', () => {
        emitRuntimeConfig({api_throttling_enabled: throttlingToggle.checked});
    });

    chromaUiButton.addEventListener('click', () => {
        window.open('http://127.0.0.1:8123/static/ui.html', '_blank', 'noopener');
    });

    function emitRuntimeConfig(update) {
        socket.emit('runtime_config_set', update);
    }

    function setMiddleView(viewName) {
        const showDebugVision = viewName === 'debug-vision';
        ioBufferPanel.classList.toggle('active', !showDebugVision);
        debugVisionView.classList.toggle('active', showDebugVision);
        chatTab.classList.toggle('active', !showDebugVision);
        debugVisionTab.classList.toggle('active', showDebugVision);
        chatTab.setAttribute('aria-selected', showDebugVision ? 'false' : 'true');
        debugVisionTab.setAttribute('aria-selected', showDebugVision ? 'true' : 'false');
    }

    function setSelectOptions(select, values, labelsByValue = {}) {
        const previousValue = select.value;
        select.innerHTML = '';
        values.forEach((value) => {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = labelsByValue[value] || value;
            select.appendChild(option);
        });
        if (values.includes(previousValue)) {
            select.value = previousValue;
        }
    }

    function renderRuntimeConfig(config) {
        const profiles = config.api_profiles || ['free', 'paid'];
        setSelectOptions(apiProfileSelect, profiles);
        apiProfileSelect.value = config.api_profile || profiles[0];

        const presets = config.model_presets || [];
        const presetValues = presets.map((preset) => preset.model);
        const presetLabels = {};
        presets.forEach((preset) => {
            presetLabels[preset.model] = preset.label;
        });
        setSelectOptions(modelPresetSelect, ['', ...presetValues], {'': 'Custom', ...presetLabels});
        modelPresetSelect.value = presetValues.includes(config.model) ? config.model : '';
        modelInput.value = config.model || '';

        const thinkingLevels = config.thinking_levels || ['minimal', 'low', 'medium', 'high'];
        setSelectOptions(thinkingLevelSelect, thinkingLevels);
        thinkingLevelSelect.value = config.thinking_level || 'low';

        const mediaResolutions = config.media_resolutions || [
            'MEDIA_RESOLUTION_UNSPECIFIED',
            'MEDIA_RESOLUTION_LOW',
            'MEDIA_RESOLUTION_MEDIUM',
            'MEDIA_RESOLUTION_HIGH'
        ];
        setSelectOptions(mediaResolutionSelect, mediaResolutions);
        mediaResolutionSelect.value = config.media_resolution || 'MEDIA_RESOLUTION_MEDIUM';

        filesApiToggle.checked = Boolean(config.use_files_api);
        keyFailoverToggle.checked = Boolean(config.key_failover);
        throttlingToggle.checked = Boolean(config.api_throttling_enabled);
        renderRuntimeStatus(config);
    }

    function maybeShowPaidKeyAlert(config) {
        const currentProfile = config.api_profile || null;
        if (previousApiProfile && previousApiProfile !== 'paid' && currentProfile === 'paid') {
            const status = config.status || {};
            paidKeyAlertBody.textContent = status.last_paid_key_notice || 'Logos switched to the paid Gemini API key.';
            paidKeyAlert.hidden = false;
        }
        previousApiProfile = currentProfile;
    }

    function renderRuntimeStatus(config) {
        const available = config.api_key_available || {};
        const keyState = Object.keys(available)
            .map((profile) => `${profile}:${available[profile] ? 'ready' : 'missing'}`)
            .join(' ');
        const status = config.status || {};
        const bits = [
            `active:${config.api_profile || 'unknown'}`,
            keyState,
            `files:${config.files_cache_entries || 0}`
        ];
        if (status.last_failover) bits.push(status.last_failover);
        if (status.files_api_last_event) bits.push(status.files_api_last_event);
        if (status.last_error) bits.push(status.last_error);
        runtimeConfigStatus.textContent = bits.filter(Boolean).join(' | ');
    }

    function sendMessage() {
        const content = humanInput.value.trim();
        if (content) {
            const message = {
                content: content,
                type: modeToggle.checked ? 'debug' : (typeInput.value.trim() || 'human'),
                loop_cognition: loopCognitionCheckbox.checked,
                mode: modeToggle.checked ? 'output' : 'input'
            };
            socket.emit('human_input', message);
            humanInput.value = '';
            autoResizeTextarea();
        }
    }
    
    function autoResizeTextarea() {
        const maxHeight = middlePane.clientHeight * 0.8;
        humanInput.style.height = 'auto';
        const scrollHeight = humanInput.scrollHeight;
        if (scrollHeight > maxHeight) {
            humanInput.style.height = `${maxHeight}px`;
            humanInput.style.overflowY = 'auto';
        } else {
            humanInput.style.height = `${scrollHeight}px`;
            humanInput.style.overflowY = 'hidden';
        }
    }

    function scrollToBottom(element) {
        element.scrollTop = element.scrollHeight;
    }

    async function fetchDebugVisionSnapshot() {
        try {
            const response = await fetch('/api/debug-vision', {cache: 'no-store'});
            latestDebugVision = await response.json();
            renderDebugVision();
        } catch (error) {
            debugVisionStatus.textContent = `debug vision unavailable: ${error}`;
        }
    }

    function renderDebugVision() {
        const data = debugVisionPause.checked && pausedDebugVision ? pausedDebugVision : latestDebugVision;
        const topics = data.topics || [];
        const previousTopic = debugVisionTopicFilter.value;
        debugVisionTopicFilter.innerHTML = '';
        const allOption = document.createElement('option');
        allOption.value = '';
        allOption.textContent = 'All';
        debugVisionTopicFilter.appendChild(allOption);
        topics.forEach((topic) => {
            const option = document.createElement('option');
            option.value = topic;
            option.textContent = topic.replace(data.prefix || '/logos/debug_vision/', '');
            debugVisionTopicFilter.appendChild(option);
        });
        if (topics.includes(previousTopic)) {
            debugVisionTopicFilter.value = previousTopic;
        }

        const topicFilter = debugVisionTopicFilter.value;
        const limit = parseInt(debugVisionCount.value, 10) || 4;
        const frames = (data.frames || [])
            .filter((frame) => !topicFilter || frame.topic === topicFilter)
            .slice(-limit)
            .reverse();
        setDebugVisionGridShape(frames.length || limit);

        debugVisionGrid.innerHTML = '';
        frames.forEach((frame) => {
            debugVisionGrid.appendChild(createDebugVisionTile(frame));
        });

        const pauseText = debugVisionPause.checked ? 'paused | ' : '';
        debugVisionStatus.textContent = `${pauseText}${frames.length}/${data.frames ? data.frames.length : 0} shown | ${topics.length} topics`;
        if (!frames.length) {
            const empty = document.createElement('div');
            empty.className = 'debug-vision-empty';
            empty.textContent = 'No debug vision frames yet.';
            debugVisionGrid.appendChild(empty);
        }
    }

    function createDebugVisionTile(frame) {
        const tile = document.createElement('button');
        tile.className = 'debug-vision-tile';
        tile.type = 'button';
        tile.title = frame.topic;
        tile.addEventListener('click', () => {
            openDebugVisionModal(frame);
        });

        const img = document.createElement('img');
        img.src = frame.src;
        img.alt = frame.name || frame.topic;
        tile.appendChild(img);

        const meta = document.createElement('div');
        meta.className = 'debug-vision-meta';

        const name = document.createElement('span');
        name.className = 'debug-vision-name';
        name.textContent = frame.name || frame.topic;
        meta.appendChild(name);

        const age = document.createElement('span');
        age.className = 'debug-vision-age';
        age.textContent = formatFrameAge(frame.received_time);
        meta.appendChild(age);

        tile.appendChild(meta);
        return tile;
    }

    function setDebugVisionGridShape(count) {
        const boundedCount = Math.max(1, Math.min(8, count));
        let columns = 2;
        if (boundedCount > 4 && debugVisionGrid.clientWidth >= 760) columns = 4;
        const rows = Math.ceil(boundedCount / columns);
        debugVisionGrid.style.setProperty('--debug-vision-columns', String(columns));
        debugVisionGrid.style.setProperty('--debug-vision-rows', String(rows));
    }

    function openDebugVisionModal(frame) {
        debugVisionModalImage.src = frame.src;
        debugVisionModalImage.alt = frame.name || frame.topic;
        const pieces = [frame.topic];
        if (frame.width && frame.height) pieces.push(`${frame.width}x${frame.height}`);
        if (frame.header_stamp) pieces.push(`stamp ${frame.header_stamp}`);
        debugVisionModalMeta.textContent = pieces.join(' | ');
        debugVisionModal.hidden = false;
    }

    function closeDebugVisionModal() {
        debugVisionModal.hidden = true;
        debugVisionModalImage.removeAttribute('src');
        debugVisionModalMeta.textContent = '';
    }

    function formatFrameAge(receivedTime) {
        if (!receivedTime) return '';
        const ageSeconds = Math.max(0, (Date.now() / 1000) - receivedTime);
        if (ageSeconds < 60) return `${ageSeconds.toFixed(1)}s`;
        if (ageSeconds < 3600) return `${Math.floor(ageSeconds / 60)}m`;
        return `${Math.floor(ageSeconds / 3600)}h`;
    }
    
    function renderContentWithImages(parentElement, contentString) {
        parentElement.innerHTML = '';
        if (typeof contentString !== 'string') return;
        
        // Split by the actual image tags we just created in python
        const parts = contentString.split(/(<img src="[^"]+">)/g);
        
        parts.forEach(part => {
            if (!part) return;
            if (part.startsWith('<img')) {
                const tempDiv = document.createElement('div');
                tempDiv.innerHTML = part;
                const imgElement = tempDiv.firstChild;
                if (imgElement) {
                    parentElement.appendChild(imgElement);
                }
            } else {
                const pre = document.createElement('pre');
                const code = document.createElement('code');
                code.textContent = part; // Use textContent to safely render <py> tags as plain text!
                pre.appendChild(code);
                parentElement.appendChild(pre);
            }
        });
    }

    function createIoCell(type, content) {
        const cell = document.createElement('div');
        cell.className = 'io-cell';
        const header = document.createElement('div');
        header.className = 'io-cell-header';
        header.textContent = type;
        const contentDiv = document.createElement('div');
        contentDiv.className = 'io-cell-content';
        const pre = document.createElement('pre');
        const code = document.createElement('code');
        code.textContent = content;
        pre.appendChild(code);
        contentDiv.appendChild(pre);
        cell.appendChild(header);
        cell.appendChild(contentDiv);
        return cell;
    }

    fetchDebugVisionSnapshot();
});
