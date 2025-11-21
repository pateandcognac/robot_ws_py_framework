document.addEventListener('DOMContentLoaded', () => {
    // --- UI Element References ---
    const headerPanel = document.getElementById('header');
    const ioBufferPanel = document.getElementById('io-buffer');
    const footerPanel = document.getElementById('footer');
    const humanInput = document.getElementById('human-input');
    const typeInput = document.getElementById('type-input');
    const loopCognitionCheckbox = document.getElementById('loop-cognition-checkbox');
    const modeToggle = document.getElementById('mode-toggle');
    const loopCognitionControls = document.getElementById('loop-cognition-controls');
    // NEW: Reference to the container to calculate max height
    const middlePane = document.getElementById('middle-pane');

    // --- Initialize Split.js for resizable panels ---
    Split(['#header', '#middle-pane', '#footer'], {
        sizes: [25, 50, 25],
        minSize: 100,
        gutterSize: 8,
        cursor: 'col-resize'
    });

    // --- Initialize Socket.IO connection ---
    const socket = io();

    socket.on('connect', () => {
        console.log('Connected to server!');
    });

    // --- Socket Event Handlers ---

    // (The socket event handlers 'full_update', 'append_io', 'stream_chunk' remain unchanged)
    socket.on('full_update', (data) => {
        console.log('Received full_update');
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
        console.log('Received append_io:', data);
        const newCell = createIoCell(data.type, data.content);
        ioBufferPanel.appendChild(newCell);
        const codeBlock = newCell.querySelector('code');
        if (codeBlock) {
            hljs.highlightElement(codeBlock);
        }
        scrollToBottom(ioBufferPanel);
    });
    socket.on('stream_chunk', (data) => {
        let streamingCell = ioBufferPanel.querySelector('[data-type="me-streaming"]');
        if (!streamingCell) {
            streamingCell = createIoCell('me', '');
            streamingCell.dataset.type = 'me-streaming';
            ioBufferPanel.appendChild(streamingCell);
        }
        const codeElement = streamingCell.querySelector('code');
        if (codeElement) {
            codeElement.textContent += data.content;
        }
        scrollToBottom(ioBufferPanel);
    });
    setInterval(() => {
        const streamingCell = ioBufferPanel.querySelector('[data-type="me-streaming"] code');
        if (streamingCell) {
            hljs.highlightElement(streamingCell);
        }
    }, 2000);


    // --- Input Handling ---
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

    // NEW: Add event listener for auto-resizing the textarea
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
            // NEW: Reset the textarea size after sending
            autoResizeTextarea();
        }
    }
    
    // --- Helper Functions ---

    /**
     * NEW: Automatically adjusts the height of the textarea based on its content.
     */
    function autoResizeTextarea() {
        // Calculate the maximum height (80% of the middle pane's height)
        const maxHeight = middlePane.clientHeight * 0.8;

        // Reset height to auto to get the natural scrollHeight
        humanInput.style.height = 'auto';
        const scrollHeight = humanInput.scrollHeight;

        // If the natural height exceeds our max height, set the fixed max height and show scrollbar
        if (scrollHeight > maxHeight) {
            humanInput.style.height = `${maxHeight}px`;
            humanInput.style.overflowY = 'auto';
        } else {
            // Otherwise, set the height to its natural scroll height and hide scrollbar
            humanInput.style.height = `${scrollHeight}px`;
            humanInput.style.overflowY = 'hidden';
        }
    }


    function scrollToBottom(element) {
        element.scrollTop = element.scrollHeight;
    }
    
    // (The functions 'renderContentWithImages' and 'createIoCell' remain unchanged)
    function renderContentWithImages(parentElement, contentString) {
        parentElement.innerHTML = '';
        if (typeof contentString !== 'string') return;
        const parts = contentString.split(/(<img src="data:image\/[^"]+">)/g);
        parts.forEach(part => {
            if (!part) return;
            if (part.startsWith('<img')) {
                const tempDiv = document.createElement('div');
                tempDiv.innerHTML = part;
                const imgElement = tempDiv.firstChild;
                if (imgElement) {
                    parentElement.appendChild(imgElement);
                }
            } 
            else {
                const pre = document.createElement('pre');
                const code = document.createElement('code');
                code.textContent = part;
                pre.appendChild(code);
                parentElement.appendChild(pre);
                hljs.highlightElement(code);
            }
        });
    }
    function createIoCell(type, content) {
        const cell = document.createElement('div');
        cell.className = 'io-cell';
        const header = document.createElement('div');
        header.className = 'io-cell-header';
        header.textContent = `${type} (live update)`;
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