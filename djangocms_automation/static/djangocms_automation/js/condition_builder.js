/**
 * Condition Builder Widget
 *
 * Allows users to build complex conditions with:
 * - Field selection
 * - Operator selection (==, !=, <, >, <=, >=, contains, etc.)
 * - Value input
 * - AND/OR logic
 * - Add/remove conditions
 */
(function() {
    'use strict';

    const OPERATORS = [
        { value: '==', label: 'equals' },
        { value: '!=', label: 'not equals' },
    ];

    class ConditionBuilder {
        constructor(container, hiddenInput, initialValue = null) {
            this.container = container;
            this.hiddenInput = hiddenInput;
            this.conditions = [];

            if (initialValue) {
                this.loadValue(initialValue);
            } else {
                this.addCondition();
            }

            this.render();
        }

        loadValue(value) {
            try {
                const data = typeof value === 'string' ? JSON.parse(value) : value;
                if (data.conditions && Array.isArray(data.conditions)) {
                    this.conditions = data.conditions;
                    this.logicOperator = data.logic || 'and';
                }
            } catch (e) {
                console.error('Failed to parse condition value:', e);
                this.addCondition();
            }
        }

        addCondition(field = '', operator = '==', value = '') {
            this.conditions.push({ field, operator, value });
        }

        removeCondition(index) {
            this.conditions.splice(index, 1);
            if (this.conditions.length === 0) {
                this.addCondition();
            }
            this.render();
        }

        updateCondition(index, key, value) {
            if (this.conditions[index]) {
                this.conditions[index][key] = value;
                this.updateHiddenInput();
            }
        }

        updateLogicOperator(operator) {
            this.logicOperator = operator;
            this.updateHiddenInput();
        }

        updateHiddenInput() {
            const value = {
                logic: this.logicOperator || 'and',
                conditions: this.conditions
            };
            this.hiddenInput.value = JSON.stringify(value);
        }

        render() {
            this.container.innerHTML = '';

            // Track if we should focus the last field (when adding new condition)
            const shouldFocusLast = this.focusLastField;
            this.focusLastField = false;

            // Logic operator selector (shown if more than 1 condition)
            if (this.conditions.length > 1) {
                const logicDiv = document.createElement('div');
                logicDiv.className = 'condition-logic';

                const logicSelect = document.createElement('select');
                logicSelect.className = 'condition-logic-select';

                const andOption = document.createElement('option');
                andOption.value = 'and';
                andOption.textContent = this.container.dataset.andLabel || 'All conditions (AND)';
                andOption.selected = (this.logicOperator || 'and') === 'and';

                const orOption = document.createElement('option');
                orOption.value = 'or';
                orOption.textContent = this.container.dataset.orLabel || 'Any condition (OR)';
                orOption.selected = this.logicOperator === 'or';

                logicSelect.appendChild(andOption);
                logicSelect.appendChild(orOption);

                logicSelect.addEventListener('change', (e) => {
                    this.updateLogicOperator(e.target.value);
                });

                logicDiv.appendChild(logicSelect);

                this.container.appendChild(logicDiv);
            }

            // Render each condition
            const fieldInputs = [];
            this.conditions.forEach((condition, index) => {
                const conditionDiv = document.createElement('div');
                conditionDiv.className = 'condition-row';

                // Field input
                const fieldInput = document.createElement('input');
                fieldInput.type = 'text';
                fieldInput.setAttribute('code', '');
                fieldInput.value = condition.field;
                fieldInput.placeholder = 'Field name';
                fieldInput.addEventListener('input', (e) => {
                    this.updateCondition(index, 'field', e.target.value);
                });
                fieldInputs.push(fieldInput);

                // Operator select
                const operatorSelect = document.createElement('select');
                let operators = JSON.parse(this.container.dataset.operators) || [];

                if (operators.length) {
                    operators = operators.map(op => ({ value: op[0], label: op[1] }));
                } else {
                    operators = OPERATORS;
                }
                operators.forEach(op => {
                    const option = document.createElement('option');
                    option.value = op.value;
                    option.textContent = op.label;
                    option.selected = condition.operator === op.value;
                    operatorSelect.appendChild(option);
                });
                operatorSelect.addEventListener('change', (e) => {
                    this.updateCondition(index, 'operator', e.target.value);
                });

                // Value input
                const valueInput = document.createElement('input');
                valueInput.type = 'text';
                valueInput.setAttribute('code', '');
                valueInput.value = condition.value;
                valueInput.placeholder = 'Value';
                valueInput.addEventListener('input', (e) => {
                    this.updateCondition(index, 'value', e.target.value);
                });

                // Remove button
                const removeBtn = document.createElement('a');
                removeBtn.className = 'deletelink';
                removeBtn.title = 'Remove condition';
                removeBtn.addEventListener('click', () => this.removeCondition(index));

                conditionDiv.appendChild(fieldInput);
                conditionDiv.appendChild(operatorSelect);
                conditionDiv.appendChild(valueInput);
                conditionDiv.appendChild(removeBtn);

                this.container.appendChild(conditionDiv);
            });

            // Add condition button
            const addBtn = document.createElement('button');
            addBtn.type = 'button';
            addBtn.className = 'btn button';
            addBtn.textContent = this.container.dataset.addLabel ||'Add Condition';
            addBtn.addEventListener('click', () => {
                this.addCondition();
                this.focusLastField = true;
                this.render();
            });

            this.container.appendChild(addBtn);

            this.updateHiddenInput();

            // Focus the last field input if we just added a condition
            if (shouldFocusLast && fieldInputs.length > 0) {
                fieldInputs[fieldInputs.length - 1].focus();
            }
        }
    }

    // Auto-initialize on DOM ready
    function initConditionBuilders() {
        document.querySelectorAll('.condition-builder-widget').forEach(container => {
            const hiddenInput = container.previousElementSibling;
            if (hiddenInput && hiddenInput.type === 'hidden') {
                const initialValue = hiddenInput.value || null;
                new ConditionBuilder(container, hiddenInput, initialValue);
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initConditionBuilders);
    } else {
        initConditionBuilders();
    }

    // Export for manual initialization if needed
    window.ConditionBuilder = ConditionBuilder;
})();
