# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt
"""Résolution des coordonnées bancaires d'un tiers (RIB / code banque / SWIFT).

Toutes les sources de paiement (Purchase Invoice, Expense Claim, Salary Slip)
convergent vers ce module pour obtenir un jeu normalisé de coordonnées,
quel que soit le doctype d'origine. Cela garantit un format CSV homogène.

Principe : **source unique = l'enregistrement `Bank Account` ERPNext** du tiers.
Toutes les coordonnées d'un tiers se saisissent à un seul endroit, son compte
bancaire ; on n'en duplique aucune ailleurs.

Hiérarchie de résolution d'un compte bénéficiaire :
    1. Un enregistrement `Bank Account` lié (Supplier.default_bank_account,
       Employee.custom_boa_bank_account, ou Bank Account lié par party).

Sur le `Bank Account` :
    - RIB        : `custom_rib` (RIB/BBAN UEMOA complet, 24 car.), sinon dérivé
                   de l'IBAN (IBAN sans le code pays ni la clé = `iban[4:]`).
    - Code banque: `custom_bank_code` (override par compte) sinon dérivé des
                   5 premiers caractères du RIB (UEMOA).
    - SWIFT      : `custom_swift_code` (override par compte), sinon
                   `Bank.swift_number` de la banque liée.
    - Titulaire  : `account_name`.

Le champ standard `bank_account_no` reste le **numéro de compte** générique du
cœur ERPNext ; il est volontairement distinct du RIB et n'est pas lu ici.
"""

from dataclasses import dataclass, field

import frappe
from frappe import _

from paybridge.boa.domibus.exceptions import BankDetailsError


def normalize_rib(rib: str) -> str:
	"""Normalise un RIB : retire les espaces, met en majuscules."""
	return (rib or "").replace(" ", "").upper()


@dataclass
class BankCoordinates:
	"""Coordonnées bancaires normalisées d'une partie (débit ou crédit)."""

	account_holder: str = ""
	account_number: str = ""  # RIB / BBAN UEMOA complet (24 car.)
	swift_code: str = ""
	bank_account: str = ""  # nom du doctype Bank Account source (traçabilité)
	bank_code_override: str = ""  # code banque saisi explicitement (Bank Account.custom_bank_code)

	@property
	def account(self) -> str:
		"""Compte à porter dans le CSV (col. « Compte à débiter/créditer ») = le RIB."""
		return self.account_number

	@property
	def bank_code(self) -> str:
		"""Code banque (col. « Code bban »).

		Saisi explicitement sur le compte (`custom_bank_code`) s'il est présent,
		sinon dérivé des 5 premiers caractères du RIB (standard UEMOA). L'override
		couvre les cas où le code banque ne correspond pas aux 5 premiers car. du RIB.
		"""
		return self.bank_code_override or (self.account_number or "")[:5]

	def is_complete(self) -> bool:
		"""Le RIB est présent."""
		return bool(self.account_number)

	def missing_fields(self) -> list:
		missing = []
		if not self.account_number:
			missing.append(_("RIB du compte"))
		return missing


def _derive_rib_from_iban(iban: str) -> str:
	"""RIB = IBAN sans le code pays (2) ni la clé de contrôle (2).

	Format UEMOA : CIxx + 24 caractères de RIB/BBAN. On retourne la partie RIB.
	"""
	iban = normalize_rib(iban)
	return iban[4:] if len(iban) > 4 else iban


def get_bank_coordinates_from_account(bank_account: str) -> BankCoordinates:
	"""Construit les coordonnées à partir d'un doctype Bank Account (source unique)."""
	if not bank_account:
		return BankCoordinates()

	ba = frappe.get_cached_doc("Bank Account", bank_account)

	# BIC : override par compte (custom_swift_code) sinon banque liée. Sur le
	# terrain, on paie des bénéficiaires de banques variées sans tenir un Bank
	# par établissement : le BIC se saisit donc sur le compte.
	swift = ba.get("custom_swift_code") or ""
	if not swift and ba.bank:
		swift = frappe.get_cached_value("Bank", ba.bank, "swift_number") or ""

	rib = normalize_rib(ba.get("custom_rib")) or _derive_rib_from_iban(ba.get("iban"))

	# Code banque : override saisi sur le compte, sinon dérivé du RIB (cf. property bank_code).
	bank_code = normalize_rib(ba.get("custom_bank_code"))

	return BankCoordinates(
		account_holder=ba.account_name or "",
		account_number=rib,
		swift_code=(swift or "").strip(),
		bank_account=ba.name,
		bank_code_override=bank_code,
	)


def get_party_bank_coordinates(party_type: str, party: str) -> BankCoordinates:
	"""Coordonnées bancaires d'un tiers (Supplier / Employee...).

	Source unique : le `Bank Account` lié au tiers. Aucun repli sur des champs
	scalaires natifs — tout se saisit dans le compte bancaire.
	"""
	bank_account = None

	if party_type == "Supplier":
		bank_account = frappe.db.get_value("Supplier", party, "default_bank_account")

	elif party_type == "Employee":
		bank_account = frappe.db.get_value("Employee", party, "custom_boa_bank_account")

	# Bank Account explicitement lié au tiers (party_type/party, is_default)
	if not bank_account:
		bank_account = frappe.db.get_value(
			"Bank Account",
			{"party_type": party_type, "party": party, "is_default": 1, "disabled": 0},
			"name",
		)

	return get_bank_coordinates_from_account(bank_account)


def get_company_default_bank_account(company: str) -> str | None:
	"""Compte bancaire de société par défaut (ERPNext), s'il existe."""
	if not company:
		return None
	return frappe.db.get_value(
		"Bank Account",
		{"company": company, "is_company_account": 1, "is_default": 1, "disabled": 0},
		"name",
	)


def get_debit_coordinates(settings=None, company: str | None = None) -> BankCoordinates:
	"""Coordonnées du compte émetteur (à débiter).

	Source unique : un `Bank Account` ERPNext, comme pour les bénéficiaires.
	Cascade de résolution :
	    1. `debit_bank_account` configuré dans BOA Domibus Settings ;
	    2. compte bancaire de société par défaut (ERPNext) si `company` fourni.
	"""
	if settings is None:
		settings = frappe.get_cached_doc("BOA Domibus Settings")

	# 1. Bank Account lié dans les Settings
	if settings.get("debit_bank_account"):
		coords = get_bank_coordinates_from_account(settings.debit_bank_account)
		if coords.is_complete():
			return coords

	# 2. Compte de société ERPNext
	company_account = get_company_default_bank_account(company)
	if company_account:
		coords = get_bank_coordinates_from_account(company_account)
		if coords.is_complete():
			return coords

	return BankCoordinates()


def require_complete(coords: BankCoordinates, label: str) -> BankCoordinates:
	"""Lève BankDetailsError si les coordonnées sont incomplètes."""
	if not coords.is_complete():
		raise BankDetailsError(
			_("Coordonnées bancaires incomplètes pour {0} : {1}").format(
				label, ", ".join(coords.missing_fields())
			)
		)
	return coords
