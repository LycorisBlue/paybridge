# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class BOADomibusSettings(Document):

	def validate(self):
		self._require_endpoint()

	def _require_endpoint(self):
		"""L'URL de l'environnement actif doit être renseignée."""
		if self.environment == "PROD" and not self.prod_endpoint_url:
			frappe.throw(_("URL Endpoint (PROD) requise en environnement PROD."))
		if self.environment == "TEST" and not self.endpoint_url:
			frappe.throw(_("URL Endpoint (TEST) requise en environnement TEST."))

	@frappe.whitelist()
	def test_connection(self):
		"""Teste la connexion à l'access point Domibus (ping fonctionnel)."""
		from paybridge.boa.domibus.client import DomibusClient

		try:
			pending = DomibusClient(self).test_connection()
		except Exception as exc:
			frappe.throw(_("Échec de connexion : {0}").format(exc))

		frappe.msgprint(
			_("Connexion réussie. {0} message(s) en attente.").format(pending),
			indicator="green",
			title=_("Connexion Domibus OK"),
		)
