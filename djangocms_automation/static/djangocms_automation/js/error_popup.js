/**
 * Error Tooltip Handler
 *
 * Shows a tooltip with error messages when clicking on error SVG icons
 */
(function() {
    'use strict';

    class ErrorTooltip {
        constructor() {
            this.tooltip = null;
            this.currentErrorDiv = null;
            this.createTooltipElement();
            this.attachEventListeners();
        }

        createTooltipElement() {
            this.tooltip = document.createElement('div');
            this.tooltip.className = 'error-tooltip';
            this.tooltip.style.display = 'none';

            const arrow = document.createElement('div');
            arrow.className = 'error-tooltip-arrow';

            const content = document.createElement('div');
            content.className = 'error-tooltip-content';

            this.tooltip.appendChild(arrow);
            this.tooltip.appendChild(content);

            // Append to #cms-top if available, otherwise to body
            const cmsTop = document.getElementById('cms-top');
            if (cmsTop) {
                cmsTop.appendChild(this.tooltip);
            } else {
                document.body.appendChild(this.tooltip);
            }
        }

        attachEventListeners() {
            // Toggle tooltip on click
            document.addEventListener('click', (e) => {
                const errorDiv = e.target.closest('.errors');

                if (!errorDiv) {
                    // Click outside - hide tooltip
                    if (this.tooltip.style.display === 'block') {
                        this.hide();
                    }
                    return;
                }

                const svg = errorDiv.querySelector('svg');
                if (!svg) return;

                // Check if click was on the SVG or its children
                if (e.target === svg || svg.contains(e.target)) {
                    e.preventDefault();
                    e.stopPropagation();

                    // Toggle if clicking same error div
                    if (this.currentErrorDiv === errorDiv && this.tooltip.style.display === 'block') {
                        this.hide();
                    } else {
                        this.show(errorDiv, svg);
                    }
                }
            });

            // Hide on scroll
            window.addEventListener('scroll', () => {
                if (this.tooltip.style.display === 'block') {
                    this.hide();
                }
            }, true);

            // Update position on resize
            window.addEventListener('resize', () => {
                if (this.currentErrorDiv && this.tooltip.style.display === 'block') {
                    const svg = this.currentErrorDiv.querySelector('svg');
                    if (svg) {
                        this.positionTooltip(svg);
                    }
                }
            });
        }

        show(errorDiv, svg) {
            const errorList = errorDiv.querySelector('ul');
            if (!errorList) {
                return;
            }

            this.currentErrorDiv = errorDiv;
            const content = this.tooltip.querySelector('.error-tooltip-content');
            content.innerHTML = '';

            // Clone the error list
            const clonedList = errorList.cloneNode(true);
            content.appendChild(clonedList);

            // Position and show
            this.tooltip.style.display = 'block';
            this.positionTooltip(svg);
        }

        positionTooltip(svg) {
            const rect = svg.getBoundingClientRect();
            const tooltipRect = this.tooltip.getBoundingClientRect();
            const arrow = this.tooltip.querySelector('.error-tooltip-arrow');

            // Calculate position (above the SVG by default)
            const scrollY = window.pageYOffset;
            const scrollX = window.pageXOffset;

            let top = rect.top + scrollY - tooltipRect.height - 8;
            let left = rect.left + scrollX + (rect.width / 2) - (tooltipRect.width / 2);

            // Position arrow (pointing down by default)
            arrow.style.left = '50%';
            arrow.style.top = 'auto';
            arrow.style.bottom = '-6px';
            arrow.style.transform = 'translateX(-50%) rotate(225deg)';

            // Check if tooltip goes off-screen vertically (show below instead)
            if (rect.top - tooltipRect.height - 8 < scrollY) {
                top = rect.bottom + scrollY + 8;
                arrow.style.top = '-6px';
                arrow.style.bottom = 'auto';
                arrow.style.transform = 'translateX(-50%) rotate(45deg)';
            }

            // Check if tooltip goes off-screen horizontally
            if (left < scrollX + 10) {
                left = scrollX + 10;
                arrow.style.left = `${rect.left + rect.width / 2 - left + scrollX}px`;
            } else if (left + tooltipRect.width > scrollX + window.innerWidth - 10) {
                left = scrollX + window.innerWidth - tooltipRect.width - 10;
                arrow.style.left = `${rect.left + rect.width / 2 - left + scrollX}px`;
            }

            this.tooltip.style.top = `${top}px`;
            this.tooltip.style.left = `${left}px`;
        }

        hide() {
            this.tooltip.style.display = 'none';
            this.currentErrorDiv = null;
        }
    }

    // Initialize on DOM ready
    function initErrorTooltip() {
        new ErrorTooltip();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initErrorTooltip);
    } else {
        initErrorTooltip();
    }
})();
