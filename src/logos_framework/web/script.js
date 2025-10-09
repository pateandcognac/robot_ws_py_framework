document.addEventListener('DOMContentLoaded', () => {
    // --- UI Element References ---
    const headerPanel = document.getElementById('header');
    const ioBufferPanel = document.getElementById('io-buffer');
    const footerPanel = document.getElementById('footer');
    const humanInput = document.getElementById('human-input');
    const typeInput = document.getElementById('type-input');
    const loopCognitionCheckbox = document.getElementById('loop-cognition-checkbox');

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

    // This uses our smart rendering function to build the UI,
    // which allows images to be displayed correctly alongside formatted text.
    socket.on('full_update', (data) => {
        console.log('Received full_update');
        
        renderContentWithImages(headerPanel, data.header);
        
        // For the IO buffer, we still need our base div container
        const ioBufferBase = document.createElement('div');
        ioBufferBase.id = 'io-buffer-base';
        renderContentWithImages(ioBufferBase, data.io_buffer);
        ioBufferPanel.innerHTML = ''; // Clear the panel first
        ioBufferPanel.appendChild(ioBufferBase);

        renderContentWithImages(footerPanel, data.footer);
        
        // No longer need hljs.highlightAll() as the new function does it piece by piece.
        scrollToBottom(ioBufferPanel);
    });

    /**
     * Appends a new, styled cell for any incoming input message.
     */
    socket.on('append_io', (data) => {
        console.log('Received append_io:', data);
        const newCell = createIoCell(data.type, data.content);
        ioBufferPanel.appendChild(newCell);
        
        // Highlight only the new cell for performance
        const codeBlock = newCell.querySelector('code');
        if (codeBlock) {
            hljs.highlightElement(codeBlock);
        }
        scrollToBottom(ioBufferPanel);
    });

    /**
     * Appends or adds to a distinct "streaming" cell for LLM output.
     */
    socket.on('stream_chunk', (data) => {
        let streamingCell = ioBufferPanel.querySelector('[data-type="me-streaming"]');
        
        // If the streaming cell doesn't exist, create it.
        if (!streamingCell) {
            streamingCell = createIoCell('me', ''); // Content starts empty
            streamingCell.dataset.type = 'me-streaming'; // Mark it for easy selection
            ioBufferPanel.appendChild(streamingCell);
        }

        const codeElement = streamingCell.querySelector('code');
        if (codeElement) {
            // Append the new text chunk
            codeElement.textContent += data.content;
        }
        scrollToBottom(ioBufferPanel);
    });
    
    // After a stream is finished, we can re-highlight the final block
    // This is a placeholder; a more robust solution would be an 'end_stream' event.
    // For now, we periodically re-highlight the streaming block.
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

    function sendMessage() {
        const content = humanInput.value.trim();
        if (content) {
            const message = {
                content: content,
                type: typeInput.value.trim() || 'human',
                loop_cognition: loopCognitionCheckbox.checked
            };
            socket.emit('human_input', message);
            humanInput.value = '';
        }
    }

    // --- Helper Functions ---
    function scrollToBottom(element) {
        element.scrollTop = element.scrollHeight;
    }

    /**
     * This function intelligently renders content. Text is wrapped in <pre><code>
     * for formatting, while <img> tags are rendered as actual images.
     * @param {HTMLElement} parentElement The container element to append content to.
     * @param {string} contentString The raw string from the backend.
     */
    function renderContentWithImages(parentElement, contentString) {
        // Clear any previous content
        parentElement.innerHTML = '';

        if (typeof contentString !== 'string') return;

        // This regex splits the string by our specific img tags, but KEEPS the tags in the resulting array.
        const parts = contentString.split(/(<img src="data:image\/[^"]+">)/g);

        parts.forEach(part => {
            if (!part) return; // Skip any empty strings from the split.

            // If the part is an image tag, create an img element.
            if (part.startsWith('<img')) {
                // To "un-escape" it, we create a temporary element. This is a safe way
                // to parse an HTML string into a DOM node.
                const tempDiv = document.createElement('div');
                tempDiv.innerHTML = part;
                const imgElement = tempDiv.firstChild;
                if (imgElement) {
                    parentElement.appendChild(imgElement);
                }
            } 
            // Otherwise, it's a text part. Wrap it in <pre><code>.
            else {
                const pre = document.createElement('pre');
                const code = document.createElement('code');
                // Use textContent to safely insert the text, preventing any other HTML interpretation.
                code.textContent = part;
                pre.appendChild(code);
                parentElement.appendChild(pre);
                // Apply syntax highlighting to this new code block
                hljs.highlightElement(code);
            }
        });
    }



    /**
     * Creates a DOM element for a single IO cell for live updates.
     * This function safely handles text-only content.
     * @param {string} type The type of the message (e.g., 'human', 'me').
     * @param {string} content The text content of the message.
     * @returns {HTMLElement} The complete cell element.
     */
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
        // Use textContent to safely insert the text as-is, preventing HTML rendering.
        code.textContent = content;
        
        pre.appendChild(code);
        contentDiv.appendChild(pre);
        
        cell.appendChild(header);
        cell.appendChild(contentDiv);
        return cell;
    }
}
);