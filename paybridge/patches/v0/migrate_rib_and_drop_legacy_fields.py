# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt
"""Bascule des coordonnées bancaires BOA vers la source unique `Bank Account`.

Contexte : le RIB se saisissait dans `Bank Account.custom_bban_code` (champ mal
nommé/ambigu, rempli de valeurs factices). Il est remplacé par un champ dédié
**`custom_rib`**, et le code banque est désormais dérivé du RIB (plus de valeur
littérale « BBAN »). `custom_swift_code` est **conservé** (override du BIC par
compte). Seul `custom_bban_code` est supprimé.

Données : les valeurs existantes (de test, format incertain) ne sont **pas**
recopiées — elles seront ressaisies par l'opérateur dans le champ « RIB ». Les
Custom Fields livrés en fixtures n'étant pas supprimés en base par une simple
migration, on retire ici explicitement `custom_bban_code`.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	# 1. S'assurer que `custom_rib` existe (le fixture le re-synchronisera ensuite).
	create_custom_fields(
		{
			"Bank Account": [
				{
					"fieldname": "custom_rib",
					"label": "RIB",
					"fieldtype": "Data",
					"insert_after": "custom_boa_section",
					"translatable": 0,
					"module": "BOA",
				}
			]
		},
		ignore_validate=True,
	)

	# 2. Supprimer le Custom Field obsolète (sa colonne est retirée avec).
	#    `custom_swift_code` est CONSERVÉ : sur le terrain le BIC se saisit par
	#    compte (banque liée générique, sans swift_number).
	frappe.delete_doc_if_exists("Custom Field", "Bank Account-custom_bban_code")

	frappe.clear_cache(doctype="Bank Account")
