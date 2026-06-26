# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt
"""Génération du fichier des virements de masse BOA H2H (spec V0).

Le CSV est le *payload* transporté par Domibus (eDelivery / AS4) vers la BOA.
Séparateur ";", une ligne d'en-tête puis une ligne par ordre de virement.

Structure officielle (réf. « Fichier des virements de masse H2H V0 »), 12 colonnes :

    | # | Champ (FR)          | Type     | Obl. | Provenance                                   |
    |---|---------------------|----------|------|----------------------------------------------|
    | 1 | Numéro de séquence  | Integer  | Oui  | rang de la ligne (1, 2, ...)                  |
    | 2 | Sens                | String   | —    | item.sens (défaut « DR »)                     |
    | 3 | Code bban           | String   | Oui  | code banque émetteur (5 prem. car. du RIB, dérivé) |
    | 4 | Compte à débiter    | RIB      | Oui  | RIB émetteur (Bank Account lié, BOA Domibus Settings) |
    | 5 | Montant             | Amount   | Oui  | item.amount (> 0, précision selon devise)     |
    | 6 | Devise              | Currency | Oui  | item.currency                                 |
    | 7 | Code Swift          | String   | Oui  | BIC de la banque du bénéficiaire (Bank.swift_number) |
    | 8 | Code bban           | String   | Oui  | code banque bénéficiaire (5 prem. car. du RIB, dérivé) |
    | 9 | Compte à créditer   | RIB      | Oui  | RIB bénéficiaire (custom_rib du Bank Account du tiers) |
    | 10| Date d'exécution    | Date     | Oui  | item.transfer_date (J/M/AAAA)                 |
    | 11| Libelle d'opération | String   | Non  | item.transfer_label                           |
    | 12| Bénéficiaire        | String   | Oui  | nom / raison sociale du bénéficiaire          |

Bonnes pratiques :
    - séparateur ";" et terminateur CRLF (automates UEMOA) ;
    - encodage UTF-8, montants sans séparateur de milliers ;
    - libellés nettoyés (ni ";" ni retour ligne).
"""

import csv
import io

import frappe
from frappe import _
from frappe.utils import flt, getdate

#: Devises sans sous-unité (le franc CFA se libelle en unités entières).
ZERO_DECIMAL_CURRENCIES = {"XOF", "XAF", "JPY", "KRW", "VND", "CLP", "ISK", "GNF"}

#: Sens par défaut (spec V0).
DEFAULT_SENS = "DR"

#: En-tête officiel (12 colonnes).
CSV_HEADER = [
	"Numéro de séquence",
	"Sens",
	"Code bban",
	"Compte à débiter",
	"Montant",
	"Devise",
	"Code Swift",
	"Code bban",
	"Compte à créditer",
	"Date d'exécution",
	"Libelle d'opération",
	"Bénéficiaire",
]


def _currency_decimals(currency: str) -> int:
	"""Nombre de décimales d'une devise (0 pour XOF & assimilées)."""
	if not currency:
		return 0
	if currency.upper() in ZERO_DECIMAL_CURRENCIES:
		return 0
	smallest = frappe.db.get_value("Currency", currency, "smallest_currency_fraction_value")
	if smallest and flt(smallest) >= 1:
		return 0
	return 2


def _format_amount(amount, currency: str) -> str:
	decimals = _currency_decimals(currency)
	value = flt(amount, decimals)
	if decimals == 0:
		return str(int(round(value)))
	return f"{value:.{decimals}f}"


def _format_date(value) -> str:
	"""Date au format J/M/AAAA sans zéro initial (ex. 6/5/2026)."""
	if not value:
		return ""
	d = getdate(value)
	return f"{d.day}/{d.month}/{d.year}"


def _clean(text) -> str:
	"""Nettoie une valeur texte : pas de ";" ni de retour ligne, espaces compactés."""
	if not text:
		return ""
	return " ".join(str(text).replace(";", " ").split())


def build_csv(batch_doc) -> str:
	"""Génère le contenu CSV (str) d'un lot de paiement BOA, format H2H V0."""
	if not batch_doc.items:
		frappe.throw(_("Le lot ne contient aucun paiement à exporter."))

	output = io.StringIO()
	writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
	for seq, item in enumerate(batch_doc.items, start=1):
		if flt(item.amount) <= 0:
			frappe.throw(_("Ligne {0} : montant invalide ({1}).").format(seq, item.amount))
		if not (item.credit_source_account or "").strip():
			frappe.throw(_("Ligne {0} : compte à créditer (RIB) manquant.").format(seq))
		if not (item.debit_source_account or "").strip():
			frappe.throw(_("Ligne {0} : compte à débiter (RIB) manquant.").format(seq))

		writer.writerow(
			[
				seq,
				item.sens or DEFAULT_SENS,
				_clean(item.debit_bban_code),
				_clean(item.debit_source_account),
				_format_amount(item.amount, item.currency),
				(item.currency or batch_doc.currency or "XOF").upper(),
				_clean(item.credit_swift_code),
				_clean(item.credit_bban_code),
				_clean(item.credit_source_account),
				_format_date(item.transfer_date),
				_clean(item.transfer_label),
				_clean(item.get("beneficiary_name")),
			]
		)

	return output.getvalue()
