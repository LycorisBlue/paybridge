// Copyright (c) 2026, AMOAMAN and contributors
// For license information, please see license.txt

frappe.ui.form.on("BOA Payment Log", {
    refresh(frm) {
        const color_map = {
            ACKNOWLEDGED: "green",
            DELIVERED: "green",
            RECEIVED: "green",
            SENT: "blue",
            SEND_IN_PROGRESS: "blue",
            SEND_ENQUEUED: "orange",
            WAITING_FOR_RECEIPT: "orange",
            SEND_FAILURE: "red",
            FAILED: "red",
            NOT_FOUND: "orange",
            PARSE_ERROR: "red",
        };
        if (frm.doc.domibus_status) {
            const color = color_map[frm.doc.domibus_status] || "gray";
            frm.page.set_indicator(frm.doc.domibus_status, color);
        }
    },
});
