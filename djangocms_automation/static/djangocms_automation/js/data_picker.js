"use strict";

document.addEventListener("change", (event) => {
    const selector = event.target.closest(".automation-data-input select");
    if (selector && selector.value) {
        const wrapper = selector.closest(".automation-data-input");
        const input = wrapper.querySelector("input, textarea");
        const expression = "{{ " + selector.value + " }}";
        if (wrapper.dataset.template === "true") {
            const start = input.selectionStart ?? input.value.length;
            const end = input.selectionEnd ?? start;
            input.setRangeText(expression, start, end, "end");
        } else {
            input.value = expression;
        }
        input.dispatchEvent(new Event("change", {bubbles: true}));
        input.focus();
        selector.value = "";
    }
});

document.addEventListener("input", (event) => {
    const wrapper = event.target.closest(".automation-output-picker");
    if (!wrapper || event.target.matches("textarea")) return;
    const mappings = {};
    wrapper.querySelectorAll("[data-result]").forEach((row) => {
        const field = row.querySelector("[data-destination]").value.trim();
        mappings[row.dataset.result] = field ? {field, mode: row.querySelector("[data-mode]").value} : null;
    });
    wrapper.querySelector("textarea").value = JSON.stringify(mappings, null, 2);
});
