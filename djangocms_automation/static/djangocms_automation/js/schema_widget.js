/**
 * Flat JSON Schema editor.
 *
 * The textarea is the sole submitted value. This script is only a view over
 * it: supported object schemas become rows, while anything richer stays in
 * the lossless JSON editor.
 */
(function() {
    'use strict';

    const ROOT_KEYS = new Set(['type', 'properties', 'required', 'additionalProperties']);

    function isObject(value) {
        return value !== null && typeof value === 'object' && !Array.isArray(value);
    }

    function hasOnlyKeys(value, allowed) {
        return Object.keys(value).every(key => allowed.has(key));
    }

    function sameStringItemsSchema(value) {
        return isObject(value) && Object.keys(value).length === 1 && value.type === 'string';
    }

    function decomposeSchema(schema) {
        if (!isObject(schema) || !hasOnlyKeys(schema, ROOT_KEYS)) {
            return null;
        }
        if (schema.type !== 'object' || schema.additionalProperties !== false) {
            return null;
        }

        const propertiesPresent = Object.hasOwn(schema, 'properties');
        const requiredPresent = Object.hasOwn(schema, 'required');
        const properties = propertiesPresent ? schema.properties : {};
        const required = requiredPresent ? schema.required : [];
        if (!isObject(properties) || !Array.isArray(required)) {
            return null;
        }
        if (!required.every(name => typeof name === 'string') || new Set(required).size !== required.length) {
            return null;
        }
        if (required.some(name => !Object.hasOwn(properties, name))) {
            return null;
        }

        const requiredNames = new Set(required);
        const rows = [];
        for (const [name, declared] of Object.entries(properties)) {
            let definition = declared;
            if (name === '' || name.includes('\n') || name.includes('\r')) {
                return null;
            }
            if (!isObject(definition)) {
                return null;
            }
            // ``["string", "null"]`` is how an optional field is written, so
            // the table reads it as one: same type, box unticked. Anything
            // else in the union is beyond what the table can show.
            let optional = false;
            if (Array.isArray(definition.type)) {
                const [first, ...rest] = definition.type;
                if (typeof first !== 'string' || rest.length !== 1 || rest[0] !== 'null') {
                    return null;
                }
                optional = true;
                definition = { ...definition, type: first };
            }
            if (typeof definition.type !== 'string') {
                return null;
            }
            if (Object.hasOwn(definition, 'description') && typeof definition.description !== 'string') {
                return null;
            }

            let type = definition.type;
            let choices = [];
            let enumPresent = false;
            let allowed = new Set(['type', 'description']);

            if (type === 'string') {
                allowed.add('enum');
                // `email` is a format on a string rather than a type of its
                // own — the same trick `string_array` uses, so the JSON stays
                // canonical while the editor offers one choice.
                if (Object.hasOwn(definition, 'format')) {
                    // Both at once has no row to be: an email row carries no
                    // choices, so recomposing would drop the enum. Refusing
                    // sends it to the JSON editor with everything intact.
                    if (definition.format !== 'email' || Object.hasOwn(definition, 'enum')) {
                        return null;
                    }
                    allowed.add('format');
                    type = 'email';
                }
                if (Object.hasOwn(definition, 'enum')) {
                    if (Array.isArray(definition.enum) && optional) {
                        // The null that goes with the type union is not a
                        // choice anyone typed, so it is not shown as one.
                        const trimmed = definition.enum.filter(choice => choice !== null);
                        if (trimmed.length === definition.enum.length) {
                            return null;  // optional, yet no null among the choices
                        }
                        definition = { ...definition, enum: trimmed };
                    }
                    if (
                        !Array.isArray(definition.enum) ||
                        definition.enum.length === 0 ||
                        !definition.enum.every(choice =>
                            typeof choice === 'string' && choice !== '' && !choice.includes('\n') && !choice.includes('\r')
                        )
                    ) {
                        return null;
                    }
                    choices = definition.enum.slice();
                    enumPresent = true;
                }
            } else if (type === 'array') {
                allowed.add('items');
                if (!sameStringItemsSchema(definition.items)) {
                    return null;
                }
                type = 'string_array';
            } else if (!['number', 'integer', 'boolean'].includes(type)) {
                return null;
            }

            if (!hasOnlyKeys(definition, allowed)) {
                return null;
            }
            rows.push({
                name,
                type,
                required: requiredNames.has(name) && !optional,
                description: definition.description || '',
                descriptionPresent: Object.hasOwn(definition, 'description'),
                choices,
                enumPresent,
            });
        }

        return {
            hasSchema: true,
            propertiesPresent,
            requiredPresent,
            requiredOrder: required.slice(),
            rows,
        };
    }

    function composeSchema(state) {
        if (!state.hasSchema) {
            return null;
        }

        const schema = { type: 'object' };
        if (state.propertiesPresent || state.rows.length) {
            schema.properties = {};
            state.rows.forEach(row => {
                if (!row.name) {
                    return;
                }
                // A provider enforcing a schema insists that every field is
                // listed as required, so "not required" cannot be said by
                // leaving one out. It is said by allowing null instead, which
                // is the same statement and one every provider accepts.
                const base = row.type === 'string_array' ? 'array' : row.type === 'email' ? 'string' : row.type;
                const type = row.required ? base : [base, 'null'];
                let definition;
                if (row.type === 'string_array') {
                    definition = { type, items: { type: 'string' } };
                } else if (row.type === 'email') {
                    definition = { type, format: 'email' };
                } else {
                    definition = { type };
                }
                if (row.descriptionPresent || row.description !== '') {
                    definition.description = row.description;
                }
                if (row.type === 'string' && (row.enumPresent || row.choices.length)) {
                    definition.enum = row.required ? row.choices.slice() : [...row.choices, null];
                }
                // Assignment to a key named "__proto__" mutates a normal
                // object's prototype instead of creating a JSON property.
                Object.defineProperty(schema.properties, row.name, {
                    value: definition,
                    enumerable: true,
                    configurable: true,
                    writable: true,
                });
            });
        }

        // Every named field, in the order they were first seen. What the
        // checkbox decides is whether the field may be null, not whether it
        // appears here.
        const present = new Set(state.rows.filter(row => row.name).map(row => row.name));
        const required = state.requiredOrder.filter(name => present.has(name));
        state.rows.forEach(row => {
            if (row.name && !required.includes(row.name)) {
                required.push(row.name);
            }
        });
        if (state.requiredPresent || required.length) {
            schema.required = required;
        }
        schema.additionalProperties = false;
        return schema;
    }

    function emptyState() {
        return {
            hasSchema: false,
            propertiesPresent: false,
            requiredPresent: false,
            requiredOrder: [],
            rows: [],
        };
    }

    function choicesFromInput(value) {
        return value.split(/\r?\n/).filter(choice => choice !== '');
    }

    class SchemaEditor {
        constructor(container, source) {
            this.container = container;
            this.source = source;
            this.types = JSON.parse(container.dataset.schemaTypes || '[]');
            this.state = emptyState();
            this.mode = 'builder';
            this.unsupported = false;

            const raw = source.value.trim();
            if (raw) {
                try {
                    const parsed = JSON.parse(raw);
                    // `null` is how "nothing configured" arrives from a
                    // JSONField. An empty table, not an unreadable schema.
                    if (parsed !== null) {
                        const decomposed = decomposeSchema(parsed);
                        if (decomposed) {
                            this.state = decomposed;
                        } else {
                            this.mode = 'json';
                            this.unsupported = true;
                        }
                    }
                } catch (error) {
                    this.mode = 'json';
                    this.unsupported = true;
                }
            }
            this.render();
        }

        label(name, fallback) {
            return this.container.dataset[name] || fallback;
        }

        syncSource() {
            const schema = composeSchema(this.state);
            this.source.value = schema === null ? '' : JSON.stringify(schema, null, 2);
        }

        setSourceVisible(visible) {
            this.source.classList.toggle('schema-widget-source--hidden', !visible);
        }

        addRow() {
            this.state.hasSchema = true;
            this.state.propertiesPresent = true;
            this.state.rows.push({
                name: '',
                type: 'string',
                required: false,
                description: '',
                descriptionPresent: false,
                choices: [],
                enumPresent: false,
            });
            this.render();
            const inputs = this.container.querySelectorAll('.schema-widget__name');
            if (inputs.length) {
                inputs[inputs.length - 1].focus();
            }
        }

        removeRow(index) {
            const [removed] = this.state.rows.splice(index, 1);
            if (removed) {
                this.state.requiredOrder = this.state.requiredOrder.filter(name => name !== removed.name);
            }
            this.state.hasSchema = true;
            this.state.propertiesPresent = true;
            this.render();
        }

        validateNames() {
            const counts = new Map();
            this.state.rows.forEach(row => {
                if (row.name) {
                    counts.set(row.name, (counts.get(row.name) || 0) + 1);
                }
            });
            this.container.querySelectorAll('.schema-widget__name').forEach((input, index) => {
                const name = this.state.rows[index].name;
                let message = '';
                if (!name) {
                    message = this.label('nameRequired', 'Enter a field name.');
                } else if (counts.get(name) > 1) {
                    message = this.label('nameDuplicate', 'Field names must be unique.');
                }
                input.setCustomValidity(message);
                input.classList.toggle('schema-widget__input--invalid', Boolean(message));
            });
        }

        showJson() {
            this.syncSource();
            this.mode = 'json';
            this.unsupported = false;
            this.render();
            this.source.focus();
        }

        tryBuilder() {
            const raw = this.source.value.trim();
            // `null` counts as empty, the same way it does on first load: a
            // JSONField with nothing in it says `null`, and going back to the
            // table should give an empty one rather than a warning.
            if (!raw || raw === 'null') {
                this.state = emptyState();
                this.mode = 'builder';
                this.unsupported = false;
                this.render();
                return;
            }
            try {
                const decomposed = decomposeSchema(JSON.parse(raw));
                if (!decomposed) {
                    this.unsupported = true;
                    this.render();
                    return;
                }
                this.state = decomposed;
                this.mode = 'builder';
                this.unsupported = false;
                this.render();
            } catch (error) {
                this.unsupported = true;
                this.render();
            }
        }

        makeButton(label, className, handler) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = className;
            button.textContent = label;
            button.addEventListener('click', handler);
            return button;
        }

        renderJsonMode() {
            this.setSourceVisible(true);
            if (this.unsupported) {
                const note = document.createElement('p');
                note.className = 'schema-widget__notice';
                note.textContent = this.label(
                    'unsupported',
                    'This schema uses features the editor cannot show. Edit it as JSON, or simplify it.'
                );
                this.container.appendChild(note);
            }
            const footer = document.createElement('div');
            footer.className = 'schema-widget__footer';
            footer.appendChild(this.makeButton(
                this.label('builderLabel', 'Use field editor'),
                'button schema-widget__toggle',
                () => this.tryBuilder()
            ));
            this.container.appendChild(footer);
        }

        renderBuilderMode() {
            this.setSourceVisible(false);

            const tableWrap = document.createElement('div');
            tableWrap.className = 'schema-widget__table-wrap';
            const table = document.createElement('table');
            table.className = 'schema-widget__table';
            const head = document.createElement('thead');
            const headerRow = document.createElement('tr');
            [
                this.label('fieldLabel', 'Field'),
                this.label('typeLabel', 'Type'),
                this.label('requiredLabel', 'Required'),
                this.label('descriptionLabel', 'Description'),
                this.label('choicesLabel', 'Choices'),
                '',
            ].forEach(label => {
                const cell = document.createElement('th');
                cell.scope = 'col';
                cell.textContent = label;
                headerRow.appendChild(cell);
            });
            head.appendChild(headerRow);
            table.appendChild(head);

            const body = document.createElement('tbody');
            this.state.rows.forEach((row, index) => body.appendChild(this.renderRow(row, index)));
            table.appendChild(body);
            tableWrap.appendChild(table);
            this.container.appendChild(tableWrap);

            const controls = document.createElement('div');
            controls.className = 'schema-widget__controls';
            controls.appendChild(this.makeButton(
                `+ ${this.label('addLabel', 'Add a field')}`,
                'button schema-widget__add',
                () => this.addRow()
            ));
            this.container.appendChild(controls);

            const footer = document.createElement('div');
            footer.className = 'schema-widget__footer';
            const consequence = document.createElement('p');
            consequence.className = 'schema-widget__consequence';
            consequence.textContent = this.label('consequence', 'Anything not listed here is refused.');
            footer.appendChild(consequence);
            footer.appendChild(this.makeButton(
                this.label('jsonLabel', 'Edit as JSON'),
                'button schema-widget__toggle',
                () => this.showJson()
            ));
            this.container.appendChild(footer);

            this.syncSource();
            this.validateNames();
        }

        renderRow(row, index) {
            const tr = document.createElement('tr');

            const nameCell = document.createElement('td');
            const nameInput = document.createElement('input');
            nameInput.type = 'text';
            nameInput.required = true;
            nameInput.value = row.name;
            nameInput.className = 'schema-widget__name';
            nameInput.setAttribute('code', '');
            nameInput.addEventListener('input', event => {
                const previous = row.name;
                row.name = event.target.value;
                this.state.requiredOrder = this.state.requiredOrder.map(name => name === previous ? row.name : name);
                this.syncSource();
                this.validateNames();
            });
            nameCell.appendChild(nameInput);
            tr.appendChild(nameCell);

            const typeCell = document.createElement('td');
            const typeSelect = document.createElement('select');
            this.types.forEach(type => {
                const option = document.createElement('option');
                option.value = type.value;
                option.textContent = type.label;
                option.selected = row.type === type.value;
                typeSelect.appendChild(option);
            });
            typeSelect.addEventListener('change', event => {
                row.type = event.target.value;
                if (row.type !== 'string') {
                    row.choices = [];
                    row.enumPresent = false;
                }
                this.render();
            });
            typeCell.appendChild(typeSelect);
            tr.appendChild(typeCell);

            const requiredCell = document.createElement('td');
            requiredCell.className = 'schema-widget__required';
            const requiredInput = document.createElement('input');
            requiredInput.type = 'checkbox';
            requiredInput.checked = row.required;
            requiredInput.setAttribute('aria-label', this.label('requiredLabel', 'Required'));
            requiredInput.addEventListener('change', event => {
                row.required = event.target.checked;
                if (row.required && row.name && !this.state.requiredOrder.includes(row.name)) {
                    this.state.requiredOrder.push(row.name);
                }
                if (!row.required) {
                    this.state.requiredOrder = this.state.requiredOrder.filter(name => name !== row.name);
                }
                this.syncSource();
            });
            requiredCell.appendChild(requiredInput);
            tr.appendChild(requiredCell);

            const descriptionCell = document.createElement('td');
            const descriptionInput = document.createElement('input');
            descriptionInput.type = 'text';
            descriptionInput.value = row.description;
            descriptionInput.addEventListener('input', event => {
                row.description = event.target.value;
                row.descriptionPresent = row.description !== '';
                this.syncSource();
            });
            descriptionCell.appendChild(descriptionInput);
            tr.appendChild(descriptionCell);

            const choicesCell = document.createElement('td');
            if (row.type === 'string') {
                const choicesInput = document.createElement('textarea');
                choicesInput.rows = 1;
                choicesInput.value = row.choices.join('\n');
                choicesInput.placeholder = this.label('choicesHelp', 'One choice per line');
                choicesInput.setAttribute('aria-label', this.label('choicesLabel', 'Choices'));
                choicesInput.addEventListener('input', event => {
                    row.choices = choicesFromInput(event.target.value);
                    row.enumPresent = row.choices.length > 0;
                    this.syncSource();
                });
                choicesCell.appendChild(choicesInput);
            } else {
                choicesCell.className = 'schema-widget__not-applicable';
                choicesCell.textContent = '—';
            }
            tr.appendChild(choicesCell);

            const removeCell = document.createElement('td');
            removeCell.className = 'schema-widget__remove-cell';
            const remove = this.makeButton('', 'schema-widget__remove deletelink', () => this.removeRow(index));
            remove.title = this.label('removeLabel', 'Remove field');
            remove.setAttribute('aria-label', remove.title);
            removeCell.appendChild(remove);
            tr.appendChild(removeCell);
            return tr;
        }

        render() {
            this.container.replaceChildren();
            if (this.mode === 'json') {
                this.renderJsonMode();
            } else {
                this.renderBuilderMode();
            }
        }
    }

    function initSchemaWidgets() {
        document.querySelectorAll('.schema-widget').forEach(container => {
            if (container.dataset.initialized === 'true') {
                return;
            }
            const source = container.previousElementSibling;
            if (!source || !source.classList.contains('schema-widget-source')) {
                return;
            }
            container.dataset.initialized = 'true';
            new SchemaEditor(container, source);
        });
    }

    const api = { SchemaEditor, composeSchema, decomposeSchema };
    if (typeof window !== 'undefined') {
        window.SchemaWidget = api;
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
    if (typeof document !== 'undefined') {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', initSchemaWidgets);
        } else {
            initSchemaWidgets();
        }
    }
})();
