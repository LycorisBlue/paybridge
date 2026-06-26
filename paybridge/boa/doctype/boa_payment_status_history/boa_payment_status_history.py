# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt
"""Historique de statut d'un lot de paiement BOA (child table immuable).

Une ligne par transition d'état (Queued → Sent → Acknowledged →
Confirmed/Rejected/Failed). Append-only : alimentée uniquement par
``BOAPaymentBatch._transition`` ; chaque ligne porte un ``event_hash`` chaîné
au ``prev_hash`` (SHA-256) pour rendre toute altération détectable — exigence
d'audit en contexte bancaire.
"""

from frappe.model.document import Document


class BOAPaymentStatusHistory(Document):
	pass
