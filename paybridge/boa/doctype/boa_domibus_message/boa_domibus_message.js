frappe.ui.form.on("BOA Domibus Message", {
    refresh(frm) {
        const color_map = {
            "ACKNOWLEDGED": "green",
            "DELIVERED": "green",
            "RECEIVED": "green",
            "SENT": "blue",
            "SEND_IN_PROGRESS": "blue",
            "SEND_ENQUEUED": "orange",
            "WAITING_FOR_RECEIPT": "orange",
            "SEND_FAILURE": "red",
            "FAILED": "red",
            "NOT_FOUND": "gray"
        };
        if (frm.doc.status) {
            const color = color_map[frm.doc.status] || "gray";
            frm.page.set_indicator(frm.doc.status, color);
        }
    }
});
