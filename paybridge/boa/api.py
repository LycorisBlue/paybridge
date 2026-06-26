"""API publique du module BOA."""
import frappe
from frappe import _
from frappe.utils import now_datetime


@frappe.whitelist()
def list_pending_messages():
    """Liste les messages Domibus en attente."""
    from paybridge.boa.domibus.client import DomibusClient

    client = DomibusClient()
    return client.list_pending_messages()


@frappe.whitelist()
def get_payable_documents(payment_type, company, extra_filters=None):
    """Documents sources éligibles au paiement, pour le dialogue de sélection.

    Utilisé par le formulaire BOA Payment Batch pour proposer les
    Purchase Invoice / Expense Claim / Salary Slip à payer.
    """
    import json

    from paybridge.boa.domibus.source_resolver import get_source

    frappe.has_permission("BOA Payment Batch", ptype="write", throw=True)

    if isinstance(extra_filters, str):
        extra_filters = json.loads(extra_filters) if extra_filters else None

    source = get_source(payment_type)
    return source.fetch_pending(company, extra_filters)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def payable_documents_query(doctype, txt, searchfield, start, page_len, filters):
    """Requête de lien pour « Document à Payer » dans la grille.

    Ne propose que les documents soumis, réellement payables, et **non déjà
    payés avec succès** via un BOA Payment Batch.
    """
    from paybridge.boa.domibus.source_resolver import get_already_paid_names

    allowed = {"Purchase Invoice", "Expense Claim", "Salary Slip"}
    if doctype not in allowed:
        return []

    filters = filters or {}
    company = filters.get("company")
    conditions = ["docstatus = 1"]
    values = {"txt": f"%{txt}%", "start": start, "page_len": page_len}
    if company:
        conditions.append("company = %(company)s")
        values["company"] = company

    if doctype == "Purchase Invoice":
        conditions += ["status in ('Unpaid','Overdue','Partly Paid')", "outstanding_amount > 0", "is_return = 0"]
        title = "supplier_name"
    elif doctype == "Expense Claim":
        conditions += ["approval_status = 'Approved'", "is_paid = 0"]
        title = "employee_name"
    else:  # Salary Slip — aligné sur SalarySlipSource.fetch_pending
        conditions += ["status != 'Withheld'", "net_pay > 0"]
        title = "employee_name"

    paid = get_already_paid_names(doctype)
    if paid:
        conditions.append("name not in %(paid)s")
        values["paid"] = tuple(paid)

    conditions.append(f"(name like %(txt)s or {title} like %(txt)s)")
    where = " and ".join(conditions)
    return frappe.db.sql(
        f"""
        select name, {title}
        from `tab{doctype}`
        where {where}
        order by modified desc
        limit %(start)s, %(page_len)s
        """,
        values,
    )


@frappe.whitelist()
def get_item_from_source(payment_type, source_name, company=None):
    """Résout une ligne de paiement à partir d'un document source unique.

    Appelé lors de la sélection du « Document Source » dans la grille pour
    auto-remplir bénéficiaire, montant, devise, date et coordonnées bancaires.
    """
    from paybridge.boa.domibus.bank_utils import get_debit_coordinates
    from paybridge.boa.domibus.source_resolver import get_source

    frappe.has_permission("BOA Payment Batch", ptype="write", throw=True)

    debit = get_debit_coordinates(company=company)
    source = get_source(payment_type)
    item = source.build_item(source_name, debit)

    # Sérialise la date pour le client (JSON)
    if item.get("transfer_date"):
        item["transfer_date"] = str(item["transfer_date"])
    return item


@frappe.whitelist()
def check_all_pending_status():
    """Vérifie le statut de tous les lots envoyés non confirmés."""
    batches = frappe.get_all(
        "BOA Payment Batch",
        filters={
            "docstatus": 1,
            # Lots transmis dont on attend encore l'accusé transport AS4.
            # Une fois « Acknowledged », la confirmation viendra du flux retour
            # (pain.002), pas d'un nouveau sondage de statut transport.
            "status": "Sent",
            "domibus_message_id": ["!=", ""],
        },
        fields=["name", "domibus_message_id"],
    )
    results = []
    for batch in batches:
        doc = frappe.get_doc("BOA Payment Batch", batch.name)
        try:
            doc.check_domibus_status(source="Planifié")
            results.append({"batch": batch.name, "success": True})
        except Exception as e:
            results.append({"batch": batch.name, "success": False, "error": str(e)})
    return results


@frappe.whitelist()
def receive_pending_messages():
    """Traite les messages entrants Domibus (flux retour BOA → AMOAMAN).

    Flux prescrit par la BOA (plugin WS) :
        1. listPendingMessages   -> liste des IDs de messages reçus
        2. pour chaque ID :
           a. retrieveMessage    -> détail du message + fichier joint
           b. traitement ERP     -> corrélation au lot via originalMessageId
           c. markMessageAsDownloaded -> retire le message de la file d'attente
    """
    from paybridge.boa.domibus.client import DomibusClient

    client = DomibusClient()
    pending_ids = client.list_pending_messages()
    received = []
    for msg_id in pending_ids:
        log = _new_inbound_log(msg_id)
        try:
            response = client.retrieve_message(msg_id)
            log.status_code = str(response.status_code)
            log.response_soap = (response.text or "")[:10000]
            _process_inbound_message(client, msg_id, response)
            # Marque comme téléchargé pour qu'il ne réapparaisse plus.
            client.mark_message_as_downloaded(msg_id)
            log.domibus_status = "DOWNLOADED"
            received.append(msg_id)
        except Exception as e:
            log.error_detail = str(e)
            frappe.log_error(frappe.get_traceback(), "BOA Domibus Receive")
            # Dead-letter : sans cette garde, un message « poison » (illisible)
            # n'est jamais marqué téléchargé et réapparaît à chaque cycle (5 min),
            # bloquant la file. Au-delà de N échecs, on le retire et on le
            # consigne pour traitement manuel.
            if _inbound_failure_count(msg_id) + 1 >= MAX_INBOUND_ATTEMPTS:
                try:
                    client.mark_message_as_downloaded(msg_id)
                    log.domibus_status = "FAILED"
                    log.error_detail = (str(e)[:4500]
                                        + " | DEAD-LETTER : retiré de la file après échecs répétés")
                except Exception:
                    frappe.log_error(frappe.get_traceback(), "BOA Domibus Dead-letter")
        finally:
            log.insert(ignore_permissions=True)
    frappe.db.commit()
    return received


#: Nb max de tentatives de récupération d'un message entrant avant dead-letter.
MAX_INBOUND_ATTEMPTS = 5


def _inbound_failure_count(message_id):
    """Nombre d'échecs de récupération déjà journalisés pour ce message."""
    return frappe.db.count(
        "BOA Payment Log",
        {
            "message_id": message_id,
            "direction": "Inbound",
            "action_type": "Retrieve",
            "error_detail": ["is", "set"],
        },
    )


def _new_inbound_log(message_id):
    log = frappe.new_doc("BOA Payment Log")
    log.action_type = "Retrieve"
    log.direction = "Inbound"
    log.timestamp = now_datetime()
    log.message_id = message_id
    return log


def _process_inbound_message(client, message_id, response):
    """Crée le BOA Domibus Message, attache le fichier retour et corrèle le lot."""
    if frappe.db.exists("BOA Domibus Message", {"message_id": message_id}):
        return

    properties, attachments = client.parse_mtom_response(response)
    original_ref = properties.get("originalMessageId") or properties.get("refToMessageId")

    msg_doc = frappe.new_doc("BOA Domibus Message")
    msg_doc.message_id = message_id
    msg_doc.direction = "RECEIVE"
    msg_doc.received_at = now_datetime()
    msg_doc.status = "RECEIVED"
    msg_doc.original_message_id = original_ref
    msg_doc.from_party_id = properties.get("originalSender")
    msg_doc.to_party_id = properties.get("finalRecipient")

    # Corrélation au lot émis (via originalMessageId stocké à l'envoi).
    batch_name = None
    if original_ref:
        batch_name = frappe.db.get_value(
            "BOA Payment Batch", {"original_message_ref": original_ref}, "name"
        )
        if batch_name:
            msg_doc.payment_batch = batch_name

    msg_doc.insert(ignore_permissions=True)

    # Attache le(s) fichier(s) retour (décompressés si gzip) et garde le pain.002.
    from paybridge.boa.domibus import pain002

    return_xml = b""
    for _cid, payload in (attachments or {}).items():
        decoded = pain002.maybe_gunzip(payload)
        if pain002.is_pain002(decoded):
            return_xml = decoded
        ext = "xml" if decoded[:5] == b"<?xml" or b"<" in decoded[:64] else "bin"
        _attach_to(msg_doc.doctype, msg_doc.name, f"retour_{message_id}.{ext}", decoded)
    if not attachments and response.content:
        _attach_to(msg_doc.doctype, msg_doc.name, f"retour_{message_id}.xml", response.content)
        return_xml = pain002.maybe_gunzip(response.content)

    # Analyse le pain.002 et met à jour le lot corrélé.
    parsed = pain002.parse(return_xml) if return_xml else {}
    if parsed.get("group_status"):
        # Erreur métier exacte décodée du compte rendu BOA (GrpSts / Cd /
        # AddtlInf), stockée en clair ET en champs structurés exploitables.
        msg_doc.db_set("business_status", parsed.get("group_status"))
        msg_doc.db_set("reason_code", parsed.get("reason_code"))
        msg_doc.db_set("reason_detail", parsed.get("additional_info"))
        msg_doc.db_set("error_detail", pain002.format_reason(parsed))

    # Corrélation de secours via OrgnlMsgId (= ID Domibus du message émis).
    if not batch_name and parsed.get("original_msg_id"):
        batch_name = frappe.db.get_value(
            "BOA Payment Batch",
            {"domibus_message_id": parsed["original_msg_id"]},
            "name",
        ) or frappe.db.get_value(
            "BOA Payment Batch",
            {"original_message_ref": parsed["original_msg_id"]},
            "name",
        )
        if batch_name:
            msg_doc.db_set("payment_batch", batch_name)

    if batch_name:
        _apply_return_to_batch(batch_name, message_id, parsed)

    frappe.db.commit()


def _attach_to(doctype, name, filename, content_bytes):
    try:
        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": filename,
            "attached_to_doctype": doctype,
            "attached_to_name": name,
            "is_private": 1,
            "content": content_bytes,
        })
        file_doc.insert(ignore_permissions=True)
        if doctype == "BOA Domibus Message":
            frappe.db.set_value(doctype, name, "payload_file", file_doc.file_url)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "BOA Domibus Attach")


def _apply_return_to_batch(batch_name, message_id, parsed):
    """Applique un fichier retour BOA (pain.002 analysé) au lot corrélé."""
    from paybridge.boa.domibus import pain002

    group_status = parsed.get("group_status") if parsed else None
    reason = pain002.format_reason(parsed) if parsed else ""
    batch_status = pain002.map_to_batch_status(group_status)

    batch = frappe.get_doc("BOA Payment Batch", batch_name)
    batch.db_set("last_status_check", now_datetime())
    # Statut métier confirmé par la BOA (pain.002) : prioritaire sur le statut
    # transport. Tracé dans l'historique immuable avec la source « Flux retour ».
    # La « Raison de Rejet » n'est renseignée qu'en cas de rejet.
    if batch_status == "Rejected" and reason:
        batch.db_set("rejection_reason", reason)
    if batch_status:
        batch._transition(
            batch_status, domibus_status=group_status, source="Flux retour",
            detail=reason[:500] if batch_status == "Rejected" else None,
        )

    log = frappe.new_doc("BOA Payment Log")
    log.payment_batch = batch_name
    log.action_type = "Retrieve"
    log.direction = "Inbound"
    log.timestamp = now_datetime()
    log.message_id = message_id
    log.domibus_status = group_status or "RECEIVED"
    log.error_detail = reason
    log.insert(ignore_permissions=True)


def scheduled_check_status():
    """Tâche planifiée: vérifie statut des messages envoyés."""
    if not frappe.db.get_single_value("BOA Domibus Settings", "auto_check_status"):
        return
    check_all_pending_status()


def scheduled_receive_messages():
    """Tâche planifiée: récupère les messages entrants (flux retour)."""
    receive_pending_messages()
