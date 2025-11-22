/**
 * Trigger Select Widget Dynamic Update
 *
 * Updates the trigger description and schema display when the user
 * changes the selected trigger in the dropdown.
 */
(function() {
    'use strict';

    function initTriggerSelect() {
        const select = document.querySelector('[data-trigger-registry]');
        console.log('Trigger Select Element:', select);
        if (!select) return;

        const schemaEl = document.getElementById('trigger-schema');
        const descEl = document.getElementById('trigger-description');

        // Registry is injected via data attribute
        const registryData = select.dataset.triggerRegistry;
        console.log('Registry Data:', registryData);
        if (!registryData) return;

        let registry;
        try {
            registry = JSON.parse(registryData);
        } catch (e) {
            console.error('Failed to parse trigger registry:', e);
            return;
        }

        select.addEventListener('change', function() {
            const trig = registry[this.value];
            if (trig) {
                descEl.textContent = trig.description;
                schemaEl.textContent = trig.schema;
            } else {
                descEl.textContent = 'No trigger selected.';
                schemaEl.textContent = '{}';
            }
        });
    }

    // Initialize on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initTriggerSelect);
    } else {
        initTriggerSelect();
    }
})();
