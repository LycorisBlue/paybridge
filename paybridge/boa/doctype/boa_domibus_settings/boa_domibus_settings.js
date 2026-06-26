// Copyright (c) 2026, AMOAMAN and contributors
// For license information, please see license.txt

frappe.ui.form.on("BOA Domibus Settings", {
    refresh(frm) {
        frm.add_custom_button(
            __("Tester la Connexion"),
            () => {
                frm.call("test_connection");
            },
            __("Outils")
        );

        frm.add_custom_button(
            __("Vérifier Messages en Attente"),
            () => {
                frappe.call({
                    method: "paybridge.boa.api.list_pending_messages",
                    callback(r) {
                        if (r.message !== undefined) {
                            frappe.msgprint({
                                title: __("Messages en Attente"),
                                message: r.message.length
                                    ? "<ul>" +
                                      r.message
                                          .map((id) => `<li>${id}</li>`)
                                          .join("") +
                                      "</ul>"
                                    : __("Aucun message en attente."),
                                indicator: "blue",
                            });
                        }
                    },
                });
            },
            __("Outils")
        );
    },
    use_authentication(frm) {
        frm.toggle_reqd("username", frm.doc.use_authentication);
        frm.toggle_reqd("password", frm.doc.use_authentication);
    },
});
