# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt
"""Exceptions du domaine BOA / Domibus.

Séparer les erreurs métier des erreurs d'infrastructure permet au contrôleur
de décider de la présentation (msgprint, throw, log) sans coupler la couche
client SOAP à l'UI Frappe.
"""


class DomibusError(Exception):
	"""Erreur générique de l'intégration Domibus."""


class DomibusConnectionError(DomibusError):
	"""Connexion/timeout réseau vers l'access point Domibus."""


class DomibusSoapFault(DomibusError):
	"""Le serveur Domibus a renvoyé un SOAP Fault (réponse non 2xx ou Fault XML)."""

	def __init__(self, message, *, status_code=None, fault_code=None, raw_response=None):
		super().__init__(message)
		self.status_code = status_code
		self.fault_code = fault_code
		self.raw_response = raw_response


class BankDetailsError(DomibusError):
	"""Coordonnées bancaires manquantes ou incomplètes pour un tiers."""
