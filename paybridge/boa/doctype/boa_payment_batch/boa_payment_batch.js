// Copyright (c) 2026, AMOAMAN and contributors
// For license information, please see license.txt

frappe.ui.form.on("BOA Payment Batch", {
    refresh(frm) {
        frm.trigger("setup_status_indicator");
        boa_setup_item_query(frm);
        boa_sync_reference_doctype(frm);
        boa_sync_transfer_dates(frm);

        // Contexte bancaire : la date de transaction est un horodatage système,
        // jamais éditable par l'opérateur. Le serveur la ré-estampille au moment
        // de l'envoi Domibus (cf. méthode send_to_domibus côté Python).
        frm.set_df_property("transaction_date", "read_only", 1);

        // --- Récupération des paiements (lot en brouillon) ---
        if (
            frm.doc.docstatus === 0 &&
            frm.doc.payment_type &&
            frm.doc.payment_type !== "Manual" &&
            frm.doc.company
        ) {
            frm.add_custom_button(
                __("Récupérer Paiements"),
                () => boa_fetch_payments_dialog(frm),
                __("Actions BOA")
            );
        }

        // --- Actions Domibus (lot soumis) ---
        // Plus aucune action une fois le virement confirmé (Confirmed) ou le
        // lot annulé. Un rejet bancaire (Rejected) se corrige en ré-amendant.
        if (
            frm.doc.docstatus === 1 &&
            !["Confirmed", "Cancelled"].includes(frm.doc.status)
        ) {
            // Envoi initial (Queued, sans ID) ou réémission après échec
            // technique (Failed). On ne réémet pas un lot déjà en vol (Sent/
            // Acknowledged) ni un lot rejeté par la banque (Rejected).
            const can_send =
                (!frm.doc.domibus_message_id &&
                    frm.doc.status !== "Rejected") ||
                frm.doc.status === "Failed";

            if (can_send) {
                frm.add_custom_button(
                    __("Envoyer via Domibus"),
                    () => {
                        frappe.confirm(
                            __(
                                "Envoyer ce lot de {0} paiements ({1} {2}) à la BOA via Domibus ?",
                                [frm.doc.items_count, frm.doc.total_amount, frm.doc.currency]
                            ),
                            () =>
                                frm
                                    .call("send_to_domibus")
                                    .then(() => frm.reload_doc())
                        );
                    },
                    __("Actions BOA")
                );
            }

            if (frm.doc.domibus_message_id && frm.doc.status !== "Rejected") {
                frm.add_custom_button(
                    __("Vérifier Statut Domibus"),
                    () =>
                        frm
                            .call("check_domibus_status")
                            .then(() => frm.reload_doc()),
                    __("Actions BOA")
                );
            }
        }
    },

    setup_status_indicator(frm) {
        const color_map = {
            Draft: "gray",
            Queued: "yellow",
            Sent: "blue",
            Acknowledged: "cyan",
            Confirmed: "green",
            Rejected: "red",
            Failed: "orange",
            Cancelled: "gray",
        };
        if (frm.doc.status) {
            frm.page.set_indicator(frm.doc.status, color_map[frm.doc.status] || "gray");
        }
    },

    onload(frm) {
        // Société par défaut de l'utilisateur (dynamique)
        if (frm.is_new() && !frm.doc.company) {
            const company = frappe.defaults.get_user_default("Company");
            if (company) frm.set_value("company", company);
        }
        // Date de transaction : valeur initiale = aujourd'hui. La valeur
        // définitive est posée par le serveur lors de l'envoi Domibus.
        if (frm.is_new() && !frm.doc.transaction_date) {
            frm.set_value("transaction_date", frappe.datetime.get_today());
        }
    },

    company(frm) {
        // Devise dérivée de la société sélectionnée (jamais codée en dur)
        if (frm.doc.company) {
            frappe.db.get_value("Company", frm.doc.company, "default_currency").then((r) => {
                if (r.message && r.message.default_currency) {
                    frm.set_value("currency", r.message.default_currency);
                }
            });
        }
    },

    transaction_date(frm) {
        // Filet de sécurité : si la date change (ré-estampillage serveur,
        // reload, etc.), toutes les lignes restent alignées (invariant).
        boa_sync_transfer_dates(frm);
    },

    payment_type(frm) {
        // Le type de document source de chaque ligne suit le type de paiement du lot.
        const has_refs = (frm.doc.items || []).some((i) => i.reference_name);
        if (has_refs) {
            frappe.confirm(
                __("Changer le type de paiement va réinitialiser les documents source déjà liés. Continuer ?"),
                () => {
                    (frm.doc.items || []).forEach((item) => {
                        frappe.model.set_value(item.doctype, item.name, "reference_name", "");
                    });
                    boa_sync_reference_doctype(frm);
                },
                () => frm.set_value("payment_type", frm._previous_payment_type || "")
            );
        } else {
            boa_sync_reference_doctype(frm);
        }
        frm._previous_payment_type = frm.doc.payment_type;
    },

    calculate_totals(frm) {
        let total = 0;
        (frm.doc.items || []).forEach((item) => {
            total += flt(item.amount);
        });
        frm.set_value("total_amount", total);
        frm.set_value("items_count", (frm.doc.items || []).length);
    },
});

frappe.ui.form.on("BOA Payment Item", {
    items_add(frm, cdt, cdn) {
        frm.trigger("calculate_totals");
        // Le type de document source de la ligne DOIT être posé pour que le
        // lien dynamique « Document Source » soit sélectionnable.
        if (!frm.doc.payment_type) {
            frappe.show_alert({
                message: __("Choisissez d'abord le « Type de Paiement » du lot."),
                indicator: "orange",
            });
        } else if (frm.doc.payment_type !== "Manual") {
            frappe.model.set_value(cdt, cdn, "reference_doctype", frm.doc.payment_type);
        }
        // Invariant : transfer_date == transaction_date (lot).
        if (frm.doc.transaction_date) {
            frappe.model.set_value(cdt, cdn, "transfer_date", frm.doc.transaction_date);
        }
        if (frm.doc.currency) {
            frappe.model.set_value(cdt, cdn, "currency", frm.doc.currency);
        }
    },

    items_remove(frm) {
        frm.trigger("calculate_totals");
    },

    amount(frm) {
        frm.trigger("calculate_totals");
    },

    // Champs entièrement dérivés du Document Source.
    // NB : transfer_date est volontairement exclu — il reste piloté par
    // transaction_date du lot (invariant).
    reference_name(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        const derived = [
            "beneficiary_name", "amount", "currency", "sens",
            "transfer_label",
            "credit_bban_code", "credit_swift_code", "credit_source_account",
            "debit_bban_code", "debit_source_account",
            "status",
        ];

        // Document Source effacé -> repasse en saisie manuelle (champs vidés).
        if (!row.reference_name) {
            derived.forEach((f) => frappe.model.set_value(cdt, cdn, f, null));
            // On conserve l'invariant même après effacement.
            if (frm.doc.transaction_date) {
                frappe.model.set_value(cdt, cdn, "transfer_date", frm.doc.transaction_date);
            }
            frm.trigger("calculate_totals");
            return;
        }
        if (!row.reference_doctype) return;

        frappe.call({
            method: "paybridge.boa.api.get_item_from_source",
            args: {
                payment_type: row.reference_doctype,
                source_name: row.reference_name,
                company: frm.doc.company,
            },
            freeze: true,
            freeze_message: __("Récupération des informations du bénéficiaire…"),
            callback(r) {
                if (!r.message) return;
                const d = r.message;
                derived.forEach((f) => {
                    if (d[f] !== undefined && d[f] !== null && d[f] !== "") {
                        frappe.model.set_value(cdt, cdn, f, d[f]);
                    }
                });
                // Invariant : la date de transfert reste celle du lot,
                // quelle que soit la valeur retournée par la source.
                if (frm.doc.transaction_date) {
                    frappe.model.set_value(
                        cdt,
                        cdn,
                        "transfer_date",
                        frm.doc.transaction_date
                    );
                }
                if (!d.credit_bban_code && !d.credit_source_account) {
                    frappe.show_alert({
                        message: __("Coordonnées bancaires manquantes pour {0}. Renseignez le compte bénéficiaire.", [
                            d.beneficiary_name || row.reference_name,
                        ]),
                        indicator: "orange",
                    });
                }
                frm.trigger("calculate_totals");
            },
        });
    },
});

// Aligne la « Date de transfert » de chaque ligne sur la date de transaction
// du lot. Invariant absolu : transfer_date == transaction_date.
function boa_sync_transfer_dates(frm) {
    // Jamais muter un lot soumis : ses dates sont figées côté serveur, sinon
    // l'enregistrement déclenche « Not allowed to change ... after submission ».
    if (frm.doc.docstatus !== 0) return;
    if (!frm.doc.transaction_date) return;
    (frm.doc.items || []).forEach((item) => {
        if (item.transfer_date !== frm.doc.transaction_date) {
            frappe.model.set_value(
                item.doctype,
                item.name,
                "transfer_date",
                frm.doc.transaction_date
            );
        }
    });
}

// Aligne le « Type de Document » de chaque ligne sur le type de paiement du lot.
// set_value garantit l'affichage fiable de la cellule dans la grille ; la valeur
// est par ailleurs persistée côté serveur (validate).
function boa_sync_reference_doctype(frm) {
    const target = frm.doc.payment_type === "Manual" ? "" : (frm.doc.payment_type || "");
    if (!target) return;
    (frm.doc.items || []).forEach((item) => {
        if (item.reference_doctype !== target) {
            frappe.model.set_value(item.doctype, item.name, "reference_doctype", target);
        }
    });
}

// Filtre le « Document à Payer » : documents payables non encore réglés avec
// succès via un BOA Payment Batch (requête serveur dédiée).
function boa_setup_item_query(frm) {
    frm.set_query("reference_name", "items", (doc, cdt, cdn) => {
        const row = locals[cdt][cdn];
        if (!row.reference_doctype) return {};
        return {
            query: "paybridge.boa.api.payable_documents_query",
            filters: { company: doc.company },
        };
    });
}

// Dialogue de sélection des documents sources à payer.
function boa_fetch_payments_dialog(frm) {
    frappe.call({
        method: "paybridge.boa.api.get_payable_documents",
        args: { payment_type: frm.doc.payment_type, company: frm.doc.company },
        freeze: true,
        freeze_message: __("Recherche des paiements en attente…"),
        callback(r) {
            const rows = r.message || [];
            if (!rows.length) {
                frappe.msgprint({
                    title: __("Aucun paiement"),
                    message: __("Aucun {0} en attente de paiement pour {1}.", [
                        frm.doc.payment_type,
                        frm.doc.company,
                    ]),
                    indicator: "blue",
                });
                return;
            }

            const dialog = new frappe.ui.Dialog({
                title: __("Sélectionner les paiements ({0})", [frm.doc.payment_type]),
                size: "large",
                fields: [
                    {
                        fieldname: "docs",
                        fieldtype: "Table",
                        cannot_add_rows: true,
                        cannot_delete_rows: true,
                        in_place_edit: true,
                        data: rows.map((d) => ({ ...d, __checked: 1 })),
                        get_data: () => rows,
                        fields: [
                            {
                                fieldname: "name",
                                label: __("Document"),
                                fieldtype: "Data",
                                in_list_view: 1,
                                read_only: 1,
                                columns: 4,
                            },
                            {
                                fieldname: "party_name",
                                label: __("Bénéficiaire"),
                                fieldtype: "Data",
                                in_list_view: 1,
                                read_only: 1,
                                columns: 4,
                            },
                            {
                                fieldname: "amount",
                                label: __("Montant"),
                                fieldtype: "Currency",
                                in_list_view: 1,
                                read_only: 1,
                                columns: 3,
                            },
                        ],
                    },
                ],
                primary_action_label: __("Ajouter au lot"),
                primary_action() {
                    const selected = (dialog.get_value("docs") || [])
                        .filter((d) => d.__checked)
                        .map((d) => d.name);
                    if (!selected.length) {
                        frappe.msgprint(__("Sélectionnez au moins un document."));
                        return;
                    }
                    dialog.hide();
                    frm.call("get_payments_from_source", { source_names: selected }).then(
                        () => frm.reload_doc()
                    );
                },
            });
            dialog.show();
        },
    });
}