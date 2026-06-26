# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt
"""Contrôles BOA sur le DocType standard `Bank Account`.

Câblé via `doc_events` (hooks.py). Le `Bank Account` est la source unique des
coordonnées bancaires d'un tiers pour les virements BOA : on y valide donc le
RIB (`custom_rib`), identifiant le plus critique (c'est là que va l'argent).
"""

import frappe
from frappe import _

from paybridge.boa.domibus.bank_utils import normalize_rib

#: Longueur standard d'un RIB / BBAN UEMOA (banque 5 + guichet 5 + compte 12 + clé 2).
UEMOA_RIB_LENGTH = 24

#: Longueur standard d'un code banque UEMOA (5 premiers caractères du RIB).
UEMOA_BANK_CODE_LENGTH = 5


def validate_rib(doc, method=None):
	"""Normalise et contrôle le RIB saisi sur un Bank Account.

	- Normalise (retire les espaces, met en majuscules) pour un stockage homogène.
	- **Bloque** si le RIB contient des caractères non alphanumériques.
	- **Avertit** (non bloquant) si la longueur diffère du standard UEMOA (24) :
	  le code banque étant dérivé des 5 premiers caractères, un RIB mal formé
	  produirait un fichier de virement incorrect.

	NB : la validation de la clé RIB (mod-97) n'est volontairement pas bloquante
	pour l'instant — à activer une fois la convention exacte confirmée côté BOA,
	afin de ne jamais bloquer un paiement sur un faux positif.
	"""
	rib = normalize_rib(doc.get("custom_rib"))
	if not rib:
		return

	doc.custom_rib = rib

	if not rib.isalnum():
		frappe.throw(
			_("RIB invalide : seuls les caractères alphanumériques sont autorisés (reçu : {0}).").format(rib)
		)

	if len(rib) != UEMOA_RIB_LENGTH:
		frappe.msgprint(
			_("Le RIB saisi fait {0} caractères ; le standard UEMOA en attend {1}. "
			  "Vérifiez la saisie : le code banque est dérivé des 5 premiers caractères.").format(
				len(rib), UEMOA_RIB_LENGTH
			),
			title=_("RIB de longueur inhabituelle"),
			indicator="orange",
		)


def validate_bank_code(doc, method=None):
	"""Normalise et contrôle le code banque saisi (`custom_bank_code`).

	Ce champ est un **override** facultatif de la colonne « Code bban » du fichier
	H2H : laissé vide, le code banque est dérivé des 5 premiers caractères du RIB
	(cf. `bank_utils.BankCoordinates.bank_code`). On ne le renseigne que lorsqu'il
	diffère de ces 5 premiers caractères.

	- Normalise (retire les espaces, met en majuscules) pour un stockage homogène.
	- **Bloque** si la valeur contient des caractères non alphanumériques.
	- **Avertit** (non bloquant) si la longueur diffère du standard UEMOA (5).
	"""
	bank_code = normalize_rib(doc.get("custom_bank_code"))
	if not bank_code:
		return

	doc.custom_bank_code = bank_code

	if not bank_code.isalnum():
		frappe.throw(
			_("Code banque invalide : seuls les caractères alphanumériques sont autorisés (reçu : {0}).").format(
				bank_code
			)
		)

	if len(bank_code) != UEMOA_BANK_CODE_LENGTH:
		frappe.msgprint(
			_("Le code banque saisi fait {0} caractères ; le standard UEMOA en attend {1}. "
			  "Laissez le champ vide pour le dériver automatiquement du RIB.").format(
				len(bank_code), UEMOA_BANK_CODE_LENGTH
			),
			title=_("Code banque de longueur inhabituelle"),
			indicator="orange",
		)
