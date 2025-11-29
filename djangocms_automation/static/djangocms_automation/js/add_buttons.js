document.addEventListener('DOMContentLoaded', () => {
    let modal = null;

    const initModal = (plugin) => {
        const placeholder = document.createElement('div');
        placeholder.className = 'cms-add-plugin-placeholder';
        placeholder.textContent = CMS.config.lang.addPluginPlaceholder;

        const dragItem = plugin.ui.dragitem;
        const isPlaceholder = !dragItem;
        let childrenList;

        modal = new CMS.Modal({
            minWidth: 400,
            minHeight: 400
        });
        childrenList = plugin.ui.draggables[0];

        CMS.API.Helpers.addEventListener('modal-loaded', (e, { instance }) => {
            if (instance !== modal) {
                return;
            }

            plugin._setupKeyboardTraversing();
            if (childrenList.classList.contains('cms-hidden') && !isPlaceholder) {
                plugin._toggleCollapsable(dragItem);
            }
            CMS.Plugin._removeAddPluginPlaceholder();
            childrenList.appendChild(placeholder);
            plugin._scrollToElement(placeholder);
        });

        CMS.API.Helpers.addEventListener('modal-closed', (e, { instance }) => {
            if (instance !== modal) {
                return;
            }
            CMS.Plugin._removeAddPluginPlaceholder();
        });

        CMS.API.Helpers.addEventListener('modal-shown', (e, { instance }) => {
            if (modal !== instance) {
                return;
            }
            const dropdown = document.querySelector('.cms-modal-markup .cms-plugin-picker');

            if (!isTouching && dropdown) {
                // only focus the field if using mouse
                // otherwise keyboard pops up
                const input = dropdown.querySelector('input');
                if (input) input.focus();
            }
            isTouching = false;
        });

        plugins = document.querySelector(`#cms-top cms-dragable-${plugin.options.plugin_id} .cms-plugin-picker`);
        plugins ||= document.querySelector(`#cms-top cms-dragbar-${plugin.options.placeholder_id} .cms-plugin-picker`);

        if (plugins) {
            plugin._setupQuickSearch(plugins);
        }
    };

    const getPlugin = (detail) => {
        const placeholderId = detail.placeholder;
        const pluginId = detail.plugin;

        if (pluginId) {
            return CMS._instances.find((instance) => {
                if(instance.options.type === 'plugin' &&
                    instance.options.plugin_id == pluginId
                ) return true;
            });
        }

        return CMS._instances.find((instance) => {
            if(instance.options.type === 'placeholder' &&
                instance.options.placeholder_id == placeholderId
            ) return true ;
        });
    };

    document.querySelectorAll('.js-add-plugin').forEach((btn) => {
        btn.addEventListener('click', (e) => {
            const btn = e.target;
            let plugins;

            if (!btn) return;
            if (btn.disabled) return;

            // Prevent default navigation/submit if it's a link or button
            e.preventDefault();
            e.stopPropagation();

            const detail = { element: btn, ...btn.dataset };
            const plugin = getPlugin(detail);

            if (!plugin) return;

            if (detail.plugin) {
                plugin._setPluginStructureEvents();
            } else {
                plugin._setPlaceholder();
            }
            if (!plugin.ui.submenu) return;

            CMS.Plugin._hideSettingsMenu();

            possibleChildClasses = plugin._getPossibleChildClasses.call(plugin);
            const selectionNeeded = possibleChildClasses.filter(':not(.cms-submenu-item-title)').length !== 1;

            if (selectionNeeded) {
                if (!modal) {
                    modal = initModal(plugin);
                }

                // since we don't know exact plugin parent (because dragndrop)
                // we need to know the parent id by the time we open "add plugin" dialog
                const pluginsCopy = plugin._updateWithMostUsedPlugins(
                    plugins
                        .clone(true, true)
                        .data('parentId', plugin._getId(nav.closest('.cms-draggable')))
                        .append(possibleChildClasses)
                );

                modal.open({
                    title: plugin.options.addPluginHelpTitle,
                    html: pluginsCopy,
                    width: 530,
                    height: 400,
                    position: position,
                });
            } else {
                // only one plugin available, no need to show the modal
                // instead directly add the single plugin
                const el = possibleChildClasses.find('a'); // only one result
                const pluginType = el.attr('href').replace('#', '');
                const showAddForm = el.data('addForm');
                const parentId = that._getId(nav.closest('.cms-draggable'));

                that.addPlugin(pluginType, el.text(), parentId, showAddForm);
            }



        });

	});
});

