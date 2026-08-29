/**
 * Automation Flow Connectors
 *
 * Draws SVG elbow connectors between automation items, handling:
 * - Sequential flow between items
 * - Conditional branches (then/else)
 * - Nested conditions
 * - Branch merging after conditionals
 */
(function() {
    'use strict';

    const SVG_NS = 'http://www.w3.org/2000/svg';
    const CONNECTOR_COLOR = '#0066cc';
    const CONNECTOR_WIDTH = 2;
    const CORNER_RADIUS = 5;
    const VERTICAL_OFFSET = 20;
    // How far outside a loop's body its return edge is routed.
    const LOOP_CHANNEL = 40;

    class AutomationConnectors {
        constructor(container) {
            this.container = container;
            this.svg = null;
            this.connectors = [];
        }

        init() {
            this.createSVGLayer();
            this.drawConnectors();
            this.setupResizeObserver();
        }

        createSVGLayer() {
            if (this.svg) {
                this.svg.parentNode.removeChild(this.svg);
            }
            // Create SVG overlay that covers the entire container
            this.svg = document.createElementNS(SVG_NS, 'svg');
            this.svg.classList.add('automation-connectors');

            // Ensure container is positioned
            if (getComputedStyle(this.container).position === 'static') {
                this.container.style.position = 'relative';
            }

            this.container.insertBefore(this.svg, this.container.firstChild);
        }

        createArrowMarker() {
            // Create arrow marker definition
            const defs = document.createElementNS(SVG_NS, 'defs');
            const marker = document.createElementNS(SVG_NS, 'marker');
            marker.setAttribute('id', 'arrowhead');
            marker.setAttribute('markerWidth', '10');
            marker.setAttribute('markerHeight', '10');
            marker.setAttribute('refX', '9');
            marker.setAttribute('refY', '3');
            marker.setAttribute('orient', 'auto');
            marker.setAttribute('markerUnits', 'userSpaceOnUse');

            const polygon = document.createElementNS(SVG_NS, 'polygon');
            polygon.setAttribute('points', '0 0, 10 3, 0 6');
            polygon.setAttribute('fill', '#000000');

            marker.appendChild(polygon);
            defs.appendChild(marker);
            this.svg.appendChild(defs);
        }

        updateViewBox() {
            // Update viewBox to show the scrollable area
            const viewBox = `${this.container.scrollLeft} ${this.container.scrollTop} ${this.container.clientWidth} ${this.container.clientHeight}`;
            this.svg.setAttribute('viewBox', viewBox);
            this.svg.setAttribute('preserveAspectRatio', 'xMinYMin meet');
        }

        drawConnectors() {
            // Clear existing connectors
            while (this.svg.firstChild) {
                this.svg.removeChild(this.svg.firstChild);
            }

            // Update viewBox for current scroll position
            this.updateViewBox();

            // Re-create arrow marker definition after clearing
            this.createArrowMarker();
            this.processFlow(this.container);
        }

        processFlow(container, reconnect = undefined) {
            // Get all automation items in DOM order
            const nodeList = Array.from(container.querySelectorAll('& > .automation-step, & > .automation-group'));
            // For each node, find its immediate successor (no other nodes in between)
            let currentItem = null;
            let currentNode = null;
            for (currentNode of nodeList) {
                // Get next sibling, skipping text nodes and comments
                let nextNode = currentNode.nextElementSibling;

                // Check if next sibling is in our nodeList (is a step or group)
                while (nextNode && !nodeList.includes(nextNode)) {
                    nextNode = nextNode.nextElementSibling;
                }

                currentItem = currentNode.querySelector('.automation-item');
                const nextItem = nextNode ? nextNode.querySelector('.automation-item') : undefined;
                if (currentNode.classList.contains('automation-group')) {
                    const branches = currentNode.querySelectorAll('& > .automation-branches > .automation-branch');
                    for (let branch of branches) {
                        // A loop's body returns to its own test rather than
                        // continuing past it; every other branch rejoins the
                        // flow after the group.
                        const returnsToTest = branch.classList.contains('loop-body');
                        if (returnsToTest) {
                            // The body sits directly under the test, so the
                            // repeat edge drops out of its bottom rather than
                            // leaving sideways as a conditional's branches do.
                            this.drawElbowConnector(currentItem, branch.querySelector('.automation-item'));
                        } else {
                            this.drawBranchConnector(currentItem, branch.querySelector('.automation-item'), branch.dataset.branchType, {branch: true});
                        }
                        this.processFlow(branch, returnsToTest ? currentItem : (nextItem || reconnect));
                    }
                    if (currentNode.classList.contains('automation-loop-group')) {
                        // The exit is drawn rather than given a lane of its own:
                        // an empty lane cannot be narrower than the margins of
                        // the node inside it, and that width pushes the body off
                        // centre under the test.
                        const target = nextItem || reconnect;
                        if (target) {
                            this.drawLoopExitConnector(currentItem, target, currentNode.querySelector('.loop-body'));
                        }
                    }
                } else if (nextItem && !currentNode.classList.contains('end')) {
                    this.drawElbowConnector(currentItem, nextItem);
                }
            }
            if (reconnect && currentNode?.classList.contains('automation-step')) {
                if (!currentNode?.classList.contains('end')) {
                    if (container.classList.contains('loop-body')) {
                        this.drawLoopBackConnector(currentItem, reconnect);
                    } else {
                        this.drawElbowConnector(currentItem, reconnect);
                    }
                }
            }
        }

        drawElbowConnector(fromElement, toElement, options = {}) {
            const from = this.getConnectionPoint(fromElement, 'bottom');
            const to = this.getConnectionPoint(toElement, 'top');

            const path = this.createElbowPath(from, to);
            this.addPath(path, options);
        }

        // The two edges that run down the outside of a loop's body: the return
        // to the test on the left, and the exit past the loop on the right.
        // Both need the body's real extent, which is the extent of the items in
        // it — the branch box is a flex lane and can be far wider.
        bodyEdge(body, side) {
            const containerRect = this.container.getBoundingClientRect();
            const items = body ? body.querySelectorAll('.automation-item') : [];
            let edge = null;
            for (const item of items) {
                const rect = item.getBoundingClientRect();
                const value = side === 'left' ? rect.left - containerRect.left : rect.right - containerRect.left;
                if (edge === null) {
                    edge = value;
                } else {
                    edge = side === 'left' ? Math.min(edge, value) : Math.max(edge, value);
                }
            }
            return edge;
        }

        drawLoopExitConnector(fromElement, toElement, body, options = {}) {
            if (!fromElement || !toElement) return;

            const from = this.getConnectionPoint(fromElement, 'right');
            const to = this.getConnectionPoint(toElement, 'top');
            const bodyRight = this.bodyEdge(body, 'right');
            const channelX = Math.max(from.x, bodyRight === null ? from.x : bodyRight) + LOOP_CHANNEL;
            const turnY = to.y - VERTICAL_OFFSET;
            const r = CORNER_RADIUS;

            let path = `M ${from.x},${from.y} L ${channelX - r},${from.y}`;
            path += ` Q ${channelX},${from.y} ${channelX},${from.y + r}`;
            path += ` L ${channelX},${turnY - r}`;
            path += ` Q ${channelX},${turnY} ${channelX - r},${turnY}`;
            path += ` L ${to.x + r},${turnY}`;
            path += ` Q ${to.x},${turnY} ${to.x},${turnY + r}`;
            path += ` L ${to.x},${to.y}`;

            this.addPath(path, options);
        }

        drawLoopBackConnector(fromElement, toElement, options = {}) {
            // The return edge of a loop: down out of the last repeated step,
            // around the outside of the body, and back up into the side of the
            // test. Routed outside rather than straight up so it never crosses
            // the steps it is returning past.
            if (!fromElement || !toElement) return;

            const from = this.getConnectionPoint(fromElement, 'bottom');
            const to = this.getConnectionPoint(toElement, 'left');

            // Hug the repeated steps rather than the branch box: the branch is a
            // flex lane that can be far wider than its contents, so measuring it
            // would fling the return path across the diagram.
            const bodyLeft = this.bodyEdge(fromElement.closest('.loop-body'), 'left');
            const channelX = Math.min(to.x, bodyLeft === null ? to.x : bodyLeft) - LOOP_CHANNEL;
            const r = CORNER_RADIUS;

            let path = `M ${from.x},${from.y} L ${from.x},${from.y + VERTICAL_OFFSET - r}`;
            path += ` Q ${from.x},${from.y + VERTICAL_OFFSET} ${from.x - r},${from.y + VERTICAL_OFFSET}`;
            path += ` L ${channelX + r},${from.y + VERTICAL_OFFSET}`;
            path += ` Q ${channelX},${from.y + VERTICAL_OFFSET} ${channelX},${from.y + VERTICAL_OFFSET - r}`;
            path += ` L ${channelX},${to.y + r}`;
            path += ` Q ${channelX},${to.y} ${channelX + r},${to.y}`;
            path += ` L ${to.x},${to.y}`;

            this.addPath(path, options);
        }

        drawBranchConnector(fromElement, toElement, options = {}) {
            if (!fromElement || !toElement) return;

            const from = this.getConnectionPoint(fromElement, 'middle');
            const to = this.getConnectionPoint(toElement, 'top');

            const path = this.createBranchPath(from, to);
            this.addPath(path, options);
        }

        createElbowPath(from, to) {
            const elbowY = to.y - VERTICAL_OFFSET;

            // Vertical line down from source to elbow
            let path = `M ${from.x},${from.y} L ${from.x},${elbowY - CORNER_RADIUS}`;

            if (Math.abs(to.x - from.x) > 0.1) {
                // Horizontal offset - draw rounded corner
                const direction = to.x > from.x ? 1 : -1;
                path += ` Q ${from.x},${elbowY} ${from.x + direction * CORNER_RADIUS},${elbowY}`;
                path += ` L ${to.x - direction * CORNER_RADIUS},${elbowY}`;
                path += ` Q ${to.x},${elbowY} ${to.x},${elbowY + CORNER_RADIUS}`;
            }

            // Vertical line to target
            path += ` L ${to.x},${to.y}`;

            return path;
        }

        createBranchPath(from, to) {
            // Determine direction (left or right)
            const direction = to.x > from.x ? 1 : -1;

            // Start with horizontal line from source
            let path = `M ${from.x},${from.y} L ${to.x - direction * CORNER_RADIUS},${from.y}`;

            // Single corner - turn down and head directly to target
            path += ` Q ${to.x},${from.y} ${to.x},${from.y + CORNER_RADIUS}`;

            // Vertical line straight to target
            path += ` L ${to.x},${to.y}`;

            return path;
        }

        addPath(pathData, options = {}) {
            const path = document.createElementNS(SVG_NS, 'path');
            path.setAttribute('d', pathData);
            path.setAttribute('stroke', CONNECTOR_COLOR);
            path.setAttribute('stroke-width', CONNECTOR_WIDTH);
            path.setAttribute('fill', 'none');
            path.setAttribute('stroke-linecap', 'round');
            path.setAttribute('marker-end', 'url(#arrowhead)');

            if (options.branch) {
                path.classList.add(`connector-${options.branch}`);
            }
            if (options.merge) {
                path.classList.add('connector-merge');
            }

            this.svg.appendChild(path);
            this.connectors.push(path);
        }

        getConnectionPoint(element, side) {
            const rect = element.getBoundingClientRect();
            const containerRect = this.container.getBoundingClientRect();

            const relativeRect = {
                left: rect.left - containerRect.left,
                top: rect.top - containerRect.top,
                width: rect.width,
                height: rect.height
            };

            switch (side) {
                case 'top':
                    return {
                        x: relativeRect.left + relativeRect.width / 2,
                        y: relativeRect.top
                    };
                case 'bottom':
                    return {
                        x: relativeRect.left + relativeRect.width / 2,
                        y: relativeRect.top + relativeRect.height
                    };
                case 'left':
                    return {
                        x: relativeRect.left,
                        y: relativeRect.top + relativeRect.height / 2
                    };
                case 'right':
                    return {
                        x: relativeRect.left + relativeRect.width,
                        y: relativeRect.top + relativeRect.height / 2
                    };
                default:
                    return {
                        x: relativeRect.left + relativeRect.width / 2,
                        y: relativeRect.top + relativeRect.height / 2
                    };
            }
        }

        setupResizeObserver() {
            if (typeof ResizeObserver === 'undefined') {
                // Fallback for older browsers
                window.addEventListener('resize', () => this.drawConnectors());
                return;
            }

            const observer = new ResizeObserver(() => {
                this.drawConnectors();
            });

            observer.observe(this.container);

            // Also observe individual items
            const items = this.container.querySelectorAll('.automation-item');
            items.forEach(item => observer.observe(item));
        }

        destroy() {
            if (this.svg && this.svg.parentNode) {
                this.svg.parentNode.removeChild(this.svg);
            }
        }
    }

    function initWidthStyles(containers) {
        const recurseBranches = (branch) => {
            const branches = branch.querySelectorAll('& > .automation-group > .automation-branches > .automation-branch');
            if (!branches.length) {
                if (branch.classList.contains('empty')) return 1;
                return branch.querySelector('& > .automation-step') ? 10 : 0;
            }
            let sum = 0;
            branches.forEach(br => {
                const width = recurseBranches(br);
                if (width) br.style.setProperty('--branch-width', width);
                sum += width;
            });
            return sum;
        };

        containers.forEach(recurseBranches);
    }

    function initAddTriggerButton() {
        const btn = document.getElementById('js-add-automation-trigger');
        if (!btn) return;

        btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();

            const url = btn.getAttribute('href');
            if (!url) return;
            const toobarButton = document.getElementById('cms-top')?.querySelector(`a[href="${url}"]`);
            if (toobarButton) {
                toobarButton.click();
            }
        });
    }

    // Auto-initialize on DOM ready
    function initAutomationConnectors() {
        const containers = document.querySelectorAll('.automation-graph');
        initWidthStyles(containers);
        initAddTriggerButton();
        setTimeout(() => {
            containers.forEach(container => {
                if (!container._automationConnector) {
                    container._automationConnector = new AutomationConnectors(container);
                }
                container._automationConnector.init();
            });
        }, 0);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAutomationConnectors);
    } else {
        initAutomationConnectors();
    }
})();
