/**
 * Trigger Type Change Handler
 *
 * Handles dynamic reloading of the AutomationTrigger admin form when the
 * trigger type changes. Validates the form, saves if valid, or discards
 * changes if invalid, then reloads the page to show new type-specific fields.
 */

(function() {
    'use strict';

    /**
     * Initialize trigger type change handler
     */
    function init() {
        const typeSelect = document.getElementById('id_type');
        if (!typeSelect) return;

        let initialType = typeSelect.value;
        let isChanging = false;

        // Store initial form values
        const form = typeSelect.closest('form');
        const initialValues = new Map();
        if (form) {
            const elements = form.querySelectorAll('input, select, textarea');
            elements.forEach(el => {
                if (el.type === 'checkbox' || el.type === 'radio') {
                    initialValues.set(el, el.checked);
                } else {
                    initialValues.set(el, el.value);
                }
            });
        }

        typeSelect.addEventListener('change', function(e) {
            const newType = e.target.value;

            // Ignore if type hasn't really changed
            if (newType === initialType || isChanging) {
                return;
            }

            isChanging = true;

            // Check if this is a new object (no pk in URL)
            const urlParams = new URLSearchParams(window.location.search);
            const isAddForm = window.location.pathname.includes('/add/');

            if (isAddForm) {
                // For add form, just reload to show new fields
                reloadWithType(newType);
                return;
            }

            // For change form, check if any fields were modified
            let hasChanges = false;

            if (form) {
                // Check if form is dirty (any input/select/textarea changed, excluding type field)
                const elements = form.querySelectorAll('input, select, textarea');
                for (const el of elements) {
                    // Skip type field itself, hidden fields, and disabled fields
                    if (el === typeSelect || el.type === 'hidden' || el.disabled) continue;

                    const currentValue = (el.type === 'checkbox' || el.type === 'radio') ? el.checked : el.value;
                    const initialValue = initialValues.get(el);

                    if (currentValue !== initialValue) {
                        hasChanges = true;
                        break;
                    }
                }
            }

            if (hasChanges) {
                // Get localized message from data attribute or use fallback
                const confirmMessage = typeSelect.dataset.confirmMessage ||
                    'Changing the trigger type will reload the form with different configuration fields. ' +
                    'Current configuration will not be saved. Continue?';
                if (!confirm(confirmMessage)) {
                    // Revert to original type
                    typeSelect.value = initialType;
                    isChanging = false;
                    return;
                }
            }
            // Try to validate the form
            if (!form) {
                reloadWithType(newType);
                return;
            }

            // Submit the form with a special flag to indicate type change
            const input = document.createElement('input');
            input.type = 'hidden';
            input.name = '_trigger_type_change';
            input.value = newType;
            form.appendChild(input);

            // Submit the form
            form.submit();
        });
    }

    /**
     * Reload the page with the new trigger type pre-selected
     */
    function reloadWithType(newType) {
        const url = new URL(window.location.href);
        url.searchParams.set('type', newType);
        window.location.href = url.toString();
    }

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
