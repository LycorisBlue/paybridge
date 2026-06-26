# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt
"""Récupération des paiements depuis les documents sources ERPNext.

Pattern Stratégie : chaque type de paiement (Purchase Invoice, Expense Claim,
Salary Slip) implémente `PaymentSource`. Le contrôleur `BOA Payment Batch`
appelle `get_source(payment_type)` puis :

    source = get_source("Purchase Invoice")
    rows   = source.fetch_pending(company, filters)      # liste de candidats
    item   = source.build_item(name, debit_coords)       # -> dict BOA Payment Item

Chaque source sait :
    - quels documents sont éligibles au paiement (soumis, non payés, montant > 0) ;
    - quel est le tiers bénéficiaire et son montant ;
    - comment résoudre ses coordonnées bancaires (via bank_utils).

D'où viennent les données du virement :
    | Champ BOA Payment Item | Purchase Invoice          | Expense Claim                  | Salary Slip                    |
    |------------------------|---------------------------|--------------------------------|--------------------------------|
    | bénéficiaire           | supplier_name             | employee_name                  | employee_name                  |
    | montant                | outstanding_amount        | grand_total - reimbursed       | net_pay (arrondi)              |
    | tiers (party)          | Supplier                  | Employee                       | Employee                       |
    | compte crédit          | Supplier.default_bank_acc | Employee.custom_boa_bank_acc   | Employee.custom_boa_bank_acc   |
    | devise                 | currency                  | currency                       | currency                       |
    | date virement          | jour de transmission (estampillée à l'envoi Domibus, identique pour tout le lot) |
    | libellé                | "FAC <name>"              | "NDF <name>"                   | "SAL <name> <mois>"            |

Le compte à débiter (DEBIT_*) provient toujours de BOA Domibus Settings
(`bank_utils.get_debit_coordinates`), commun à tout le lot.
"""

from abc import ABC, abstractmethod

import frappe
from frappe import _
from frappe.utils import flt, getdate, today

from paybridge.boa.domibus.bank_utils import (
	BankCoordinates,
	get_party_bank_coordinates,
)


def get_already_paid_names(reference_doctype: str) -> list:
	"""Noms des documents déjà payés avec succès via un BOA Payment Batch.

	« Avec succès » = référencé dans un lot soumis (docstatus=1) dont le
	paiement ne s'est PAS soldé par un échec ou une annulation. Ces documents
	ne doivent plus être proposés au paiement.
	"""
	rows = frappe.db.sql(
		"""
		SELECT DISTINCT i.reference_name
		FROM `tabBOA Payment Item` i
		JOIN `tabBOA Payment Batch` b ON b.name = i.parent
		WHERE i.reference_doctype = %s
		  AND COALESCE(i.reference_name, '') != ''
		  AND b.docstatus = 1
		  AND b.status NOT IN ('Failed', 'Cancelled')
		""",
		reference_doctype,
	)
	return [r[0] for r in rows]


class PaymentSource(ABC):
	"""Stratégie d'extraction de paiements pour un doctype source donné."""

	#: Doctype ERPNext source
	doctype: str = ""
	#: Type de tiers bénéficiaire
	party_type: str = ""

	def _exclude_paid(self, filters: dict) -> dict:
		"""Ajoute l'exclusion des documents déjà payés avec succès."""
		paid = get_already_paid_names(self.doctype)
		if paid:
			filters["name"] = ["not in", paid]
		return filters

	@abstractmethod
	def fetch_pending(self, company: str, extra_filters: dict | None = None) -> list[dict]:
		"""Retourne les documents éligibles au paiement (aperçu pour l'utilisateur)."""

	@abstractmethod
	def build_item(self, docname: str, debit: BankCoordinates) -> dict:
		"""Construit un dict prêt à être inséré comme `BOA Payment Item`."""

	# ------------------------------------------------------------------ helpers

	def _credit_coords(self, party: str) -> BankCoordinates:
		"""Coordonnées du bénéficiaire (résilient : ne lève pas si incomplet).

		La complétude est validée plus tard (à l'ajout au lot et à la sauvegarde),
		ce qui permet de pré-remplir partiellement et de laisser l'utilisateur
		corriger les coordonnées manquantes dans le formulaire.
		"""
		return get_party_bank_coordinates(self.party_type, party)

	def _base_item(self, *, party: str, amount, currency, label, debit: BankCoordinates) -> dict:
		credit = self._credit_coords(party)
		return {
			"sens": "DR",  # spec V0 : valeur par défaut « DR »
			"amount": flt(amount),
			"currency": currency or "XOF",
			# Valeur d'amorçage : la date de virement est ré-alignée sur la date
			# de transaction du lot (validate) puis figée au jour de l'envoi.
			"transfer_date": getdate(today()),
			"transfer_label": (label or "")[:140],
			# Débit (compte émetteur, commun au lot) — code banque : override custom_bank_code, sinon 5 premiers car. du RIB
			"debit_bban_code": debit.bank_code,
			"debit_source_account": debit.account,
			# Crédit (bénéficiaire)
			"credit_bban_code": credit.bank_code,
			"credit_swift_code": credit.swift_code,
			"credit_source_account": credit.account,
			"beneficiary_name": credit.account_holder,
			# Traçabilité
			"reference_doctype": self.doctype,
			"reference_name": None,  # rempli par build_item
			"status": "Validated",
		}


class PurchaseInvoiceSource(PaymentSource):
	doctype = "Purchase Invoice"
	party_type = "Supplier"

	def fetch_pending(self, company, extra_filters=None):
		filters = {
			"docstatus": 1,
			"company": company,
			"status": ["in", ["Unpaid", "Overdue", "Partly Paid"]],
			"outstanding_amount": [">", 0],
			"is_return": 0,
		}
		filters.update(extra_filters or {})
		self._exclude_paid(filters)
		return frappe.get_all(
			self.doctype,
			filters=filters,
			fields=[
				"name",
				"supplier as party",
				"supplier_name as party_name",
				"outstanding_amount as amount",
				"currency",
				"due_date as ref_date",
			],
			order_by="due_date asc",
			limit_page_length=0,
		)

	def build_item(self, docname, debit):
		doc = frappe.get_cached_doc(self.doctype, docname)
		item = self._base_item(
			party=doc.supplier,
			amount=doc.outstanding_amount,
			currency=doc.currency,
			label=f"FAC {doc.name}",
			debit=debit,
		)
		item["reference_name"] = doc.name
		item["beneficiary_name"] = item["beneficiary_name"] or doc.supplier_name
		return item


class ExpenseClaimSource(PaymentSource):
	doctype = "Expense Claim"
	party_type = "Employee"

	def fetch_pending(self, company, extra_filters=None):
		filters = {
			"docstatus": 1,
			"company": company,
			"approval_status": "Approved",
			"is_paid": 0,
		}
		filters.update(extra_filters or {})
		self._exclude_paid(filters)
		rows = frappe.get_all(
			self.doctype,
			filters=filters,
			fields=[
				"name",
				"employee as party",
				"employee_name as party_name",
				"grand_total",
				"total_amount_reimbursed",
				"currency",
				"posting_date as ref_date",
			],
			order_by="posting_date asc",
			limit_page_length=0,
		)
		# Montant net restant à rembourser
		result = []
		for r in rows:
			r["amount"] = flt(r.grand_total) - flt(r.total_amount_reimbursed)
			if r["amount"] > 0:
				result.append(r)
		return result

	def build_item(self, docname, debit):
		doc = frappe.get_cached_doc(self.doctype, docname)
		amount = flt(doc.grand_total) - flt(doc.total_amount_reimbursed)
		item = self._base_item(
			party=doc.employee,
			amount=amount,
			currency=doc.currency,
			label=f"NDF {doc.name}",
			debit=debit,
		)
		item["reference_name"] = doc.name
		item["beneficiary_name"] = item["beneficiary_name"] or doc.employee_name
		return item


class SalarySlipSource(PaymentSource):
	doctype = "Salary Slip"
	party_type = "Employee"

	def fetch_pending(self, company, extra_filters=None):
		filters = {
			"docstatus": 1,
			"company": company,
			"status": ["!=", "Withheld"],
			"net_pay": [">", 0],
		}
		filters.update(extra_filters or {})
		self._exclude_paid(filters)
		return frappe.get_all(
			self.doctype,
			filters=filters,
			fields=[
				"name",
				"employee as party",
				"employee_name as party_name",
				"rounded_total as amount",
				"net_pay",
				"currency",
				"end_date as ref_date",
				"posting_date",
			],
			order_by="end_date desc",
			limit_page_length=0,
		)

	def build_item(self, docname, debit):
		doc = frappe.get_cached_doc(self.doctype, docname)
		amount = flt(doc.rounded_total) or flt(doc.net_pay)
		period = getdate(doc.end_date).strftime("%m/%Y") if doc.end_date else ""
		item = self._base_item(
			party=doc.employee,
			amount=amount,
			currency=doc.currency,
			label=f"SAL {doc.employee_name} {period}".strip(),
			debit=debit,
		)
		item["reference_name"] = doc.name
		item["beneficiary_name"] = item["beneficiary_name"] or doc.employee_name
		return item


_REGISTRY: dict[str, type[PaymentSource]] = {
	PurchaseInvoiceSource.doctype: PurchaseInvoiceSource,
	ExpenseClaimSource.doctype: ExpenseClaimSource,
	SalarySlipSource.doctype: SalarySlipSource,
}


def get_source(payment_type: str) -> PaymentSource:
	"""Fabrique la stratégie correspondant au type de paiement."""
	cls = _REGISTRY.get(payment_type)
	if not cls:
		frappe.throw(
			_("Type de paiement non supporté pour la récupération automatique : {0}").format(
				payment_type
			)
		)
	return cls()
