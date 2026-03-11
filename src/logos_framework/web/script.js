document.addEventListener('DOMContentLoaded', () => {
    const headerPanel = document.getElementById('header');
    const ioBufferPanel = document.getElementById('io-buffer');
    const footerPanel = document.getElementById('footer');
    const humanInput = document.getElementById('human-input');
    const typeInput = document.getElementById('type-input');
    const loopCognitionCheckbox = document.getElementById('loop-cognition-checkbox');
    const modeToggle = document.getElementById('mode-toggle');
    const loopCognitionControls = document.getElementById('loop-cognition-controls');
    const middlePane = document.getElementById('middle-pane');
    const statusBar = document.getElementById('status-bar'); // NEW

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
            typeInput.value = 'ai';
        } else {
            loopCognitionControls.style.display = 'inline';
            typeInput.value = 'human';
        }
    });

    humanInput.addEventListener('input', autoResizeTextarea);

    function sendMessage() {
        const content = humanInput.value.trim();
        if (content) {
            const message = {
                content: content,
                type: typeInput.value.trim() || 'human',
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
});