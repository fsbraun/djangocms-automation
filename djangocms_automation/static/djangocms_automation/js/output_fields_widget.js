/** Select which catalogue fields form the automation's public result. */
(function() {
    'use strict';

    function isObject(value) {
        return value !== null && typeof value === 'object' && !Array.isArray(value);
    }

    function readCatalogue(raw) {
        try {
            const value = JSON.parse(raw || '{}');
            return isObject(value) ? value : {};
        } catch (error) {
            return {};
        }
    }

    function readSelection(source) {
        try {
            const value = JSON.parse(source.value || '[]');
            return Array.isArray(value) && value.every(item => typeof item === 'string') &&
                new Set(value).size === value.length ? value : null;
        } catch (error) {
            return null;
        }
    }

    function fieldType(entry) {
        if (!isObject(entry) || !isObject(entry.schema) || !entry.schema.type) {
            return '';
        }
        if (entry.schema.type === 'string' && entry.schema.format === 'email') {
            return 'email';
        }
        if (entry.schema.type === 'array') {
            return 'list';
        }
        return entry.schema.type;
    }

    class OutputFieldsEditor {
        constructor(container, source) {
            this.container = container;
            this.source = source;
            this.catalogue = readCatalogue(container.dataset.catalogue);
            this.selected = readSelection(source);
            if (this.selected === null) {
                const note = document.createElement('p');
                note.className = 'output-fields-widget__notice';
                note.textContent = this.label(
                    'invalidLabel',
                    'This value is not a list of unique field names. Correct it as JSON before using the selector.'
                );
                this.container.appendChild(note);
                return;
            }
            this.mode = this.selected.length ? 'selected' : 'complete';
            this.source.classList.add('output-fields-widget-source--hidden');
            this.render();
        }

        label(name, fallback) {
            return this.container.dataset[name] || fallback;
        }

        sync() {
            this.source.value = JSON.stringify(this.mode === 'complete' ? [] : this.selected, null, 2);
        }

        radio(value, text) {
            const label = document.createElement('label');
            label.className = 'output-fields-widget__mode';
            const input = document.createElement('input');
            input.type = 'radio';
            input.name = `${this.source.id || this.source.name}__mode`;
            input.value = value;
            input.checked = this.mode === value;
            input.addEventListener('change', () => {
                this.mode = value;
                this.render();
            });
            label.appendChild(input);
            label.appendChild(document.createTextNode(text));
            return label;
        }

        renderFields(wrapper) {
            const catalogue = this.catalogue;
            const names = Object.keys(catalogue);
            this.selected.forEach(name => {
                if (!names.includes(name)) {
                    names.push(name);
                }
            });
            if (!names.length) {
                const empty = document.createElement('p');
                empty.className = 'output-fields-widget__empty';
                empty.textContent = this.label(
                    'emptyLabel',
                    'Add fields to the data catalogue before selecting them here.'
                );
                wrapper.appendChild(empty);
                return;
            }

            const fields = document.createElement('div');
            fields.className = 'output-fields-widget__fields';
            names.forEach(name => {
                const entry = isObject(catalogue[name]) ? catalogue[name] : null;
                const label = document.createElement('label');
                label.className = 'output-fields-widget__field';
                const input = document.createElement('input');
                input.type = 'checkbox';
                input.value = name;
                input.checked = this.selected.includes(name);
                input.disabled = this.mode !== 'selected';
                input.addEventListener('change', () => {
                    if (input.checked && !this.selected.includes(name)) {
                        this.selected.push(name);
                    } else if (!input.checked) {
                        this.selected = this.selected.filter(selected => selected !== name);
                    }
                    this.sync();
                    this.validate(fields);
                });
                label.appendChild(input);

                const title = document.createElement('span');
                title.textContent = entry && entry.label ? entry.label : name;
                label.appendChild(title);
                if (entry && entry.label && entry.label !== name) {
                    const code = document.createElement('code');
                    code.textContent = name;
                    label.appendChild(code);
                }
                const type = fieldType(entry);
                if (type) {
                    const kind = document.createElement('small');
                    kind.textContent = type;
                    label.appendChild(kind);
                }
                if (!entry) {
                    const missing = document.createElement('small');
                    missing.className = 'output-fields-widget__missing';
                    missing.textContent = this.label('missingLabel', 'not in the catalogue');
                    label.appendChild(missing);
                }
                fields.appendChild(label);
            });
            wrapper.appendChild(fields);
            this.validate(fields);
        }

        validate(fields) {
            const message = this.mode === 'selected' && this.selected.length === 0
                ? this.label('selectionRequired', 'Select at least one field, or produce the complete final item.')
                : '';
            const first = fields.querySelector('input[type="checkbox"]');
            if (first) {
                first.setCustomValidity(message);
            }
            const selectedMode = this.container.querySelector('input[value="selected"]');
            if (selectedMode) {
                selectedMode.setCustomValidity(message);
            }
            fields.classList.toggle('output-fields-widget__fields--invalid', Boolean(message));
        }

        render() {
            this.container.replaceChildren();
            this.container.appendChild(this.radio(
                'complete',
                this.label('completeLabel', 'Produce the complete final item')
            ));
            this.container.appendChild(this.radio(
                'selected',
                this.label('selectedLabel', 'Produce only selected fields')
            ));
            this.renderFields(this.container);
            this.sync();
        }
    }

    function init() {
        document.querySelectorAll('.output-fields-widget').forEach(container => {
            if (container.dataset.initialized === 'true') {
                return;
            }
            const source = container.previousElementSibling;
            if (!source || !source.classList.contains('output-fields-widget-source')) {
                return;
            }
            container.dataset.initialized = 'true';
            new OutputFieldsEditor(container, source);
        });
    }

    if (typeof window !== 'undefined') {
        window.OutputFieldsWidget = { OutputFieldsEditor, fieldType, readCatalogue, readSelection };
    }
    if (typeof document !== 'undefined') {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', init);
        } else {
            init();
        }
    }
})();
