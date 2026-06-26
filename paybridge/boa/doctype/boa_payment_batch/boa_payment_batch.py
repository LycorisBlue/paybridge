# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt
"""Contrôleur du lot de paiement BOA.

Cycle de vie (contexte bancaire — l'accusé transport ≠ exécution du virement) :
    Draft → (récupération des paiements) → submit → Queued
          → send_to_domibus → Sent
          → check_status (transport AS4) → Acknowledged (arrivé à la BOA)
                                         ↘ Failed (échec technique, rejouable)
          → pain.002 (flux retour BOA)   → Confirmed (virement exécuté)
                                         ↘ Rejected (refus banque, à ré-amender)

Le contrôleur orchestre :
    - le calcul/validation des totaux ;
    - la récupération des bénéficiaires depuis les documents sources (PI/EC/SS) ;
    - la génération du CSV et l'envoi via le client Domibus ;
    - la journalisation systématique (BOA Payment Log) de chaque échange.
"""

import hashlib
import json
import time

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, get_datetime, now_datetime, today

from paybridge.boa.domibus.bank_utils import get_debit_coordinates
from paybridge.boa.domibus.exceptions import DomibusError

#: Statuts Domibus = accusé de réception TRANSPORT (le message AS4 est bien
#: parvenu à l'access point de la BOA). N'ATTESTENT PAS l'exécution du
#: virement : seule la pain.002 (flux retour) confirme ou rejette le paiement
#: côté banque. Un lot reste donc « Acknowledged » jusqu'au compte rendu BOA.
_TRANSPORT_ACK_STATUSES = {
	"ACKNOWLEDGED", "ACKNOWLEDGED_WITH_WARNING",
	"RECEIVED", "RECEIVED_WITH_WARNINGS", "DELIVERED", "DOWNLOADED",
}
#: Échec technique de transmission Domibus (rejouable, contrairement à un rejet
#: bancaire qui exige de corriger les données puis de ré-amender le lot).
_TRANSPORT_FAILED_STATUSES = {"SEND_FAILURE", "SEND_ATTEMPT_FAILED", "FAILED"}


class BOAPaymentBatch(Document):
	# ------------------------------------------------------------- life cycle

	def before_insert(self):
		if not self.transaction_date:
			self.transaction_date = today()
		self._set_defaults()

	def validate(self):
		self._set_defaults()
		self._sync_item_references()
		self._sync_transfer_dates()
		self._calculate_totals()
		self._validate_items()

	def _sync_item_references(self):
		"""Aligne le type de document source de chaque ligne sur le type du lot.

		Persisté en base : la valeur est donc toujours correcte au rechargement,
		sans dépendre du JS.
		"""
		target = "" if self.payment_type == "Manual" else (self.payment_type or "")
		for item in self.items:
			item.reference_doctype = target

	def _sync_transfer_dates(self):
		"""Invariant : la date de virement de chaque ligne suit la date de
		transaction du lot.

		Géré côté serveur (et non plus seulement en JS) afin que la donnée
		stockée reste cohérente quelle que soit l'origine de la ligne
		(saisie manuelle ou récupération automatique des paiements).
		"""
		if not self.transaction_date:
			return
		for item in self.items:
			item.transfer_date = self.transaction_date

	def _set_defaults(self):
		"""Renseigne dynamiquement les valeurs dérivables d'ERPNext."""
		if not self.company:
			self.company = frappe.defaults.get_user_default("Company")
		# Devise : héritée de la société, jamais codée en dur.
		if self.company and not self.currency:
			self.currency = frappe.get_cached_value("Company", self.company, "default_currency")

	def on_submit(self):
		# Soumis, en file d'attente de transmission (envoi Domibus à suivre).
		self._transition("Queued", source="Système", detail="Lot soumis, en file de transmission")
		# Transmission automatique du CSV à Domibus dès la validation.
		if frappe.db.get_single_value("BOA Domibus Settings", "auto_send_on_submit"):
			frappe.enqueue(
				"paybridge.boa.doctype.boa_payment_batch.boa_payment_batch.send_batch_async",
				queue="short",
				timeout=300,
				enqueue_after_commit=True,
				batch_name=self.name,
			)

	def on_cancel(self):
		self._transition("Cancelled", source="Manuel")

	# --------------------------------------------------------------- internals

	def _calculate_totals(self):
		self.total_amount = sum(flt(item.amount) for item in self.items)
		self.items_count = len(self.items)

	def _validate_items(self):
		seen_refs = set()
		for idx, item in enumerate(self.items, start=1):
			if flt(item.amount) <= 0:
				frappe.throw(_("Ligne {0} : le montant doit être positif.").format(idx))
			if not (item.credit_source_account or "").strip():
				frappe.throw(_("Ligne {0} : compte à créditer (RIB bénéficiaire) requis.").format(idx))
			if not (item.debit_source_account or "").strip():
				frappe.throw(
					_("Ligne {0} : compte à débiter (RIB émetteur) requis. "
					  "Configurez le compte émetteur dans BOA Domibus Settings.").format(idx)
				)
			if not (item.beneficiary_name or "").strip():
				frappe.throw(_("Ligne {0} : nom du bénéficiaire requis.").format(idx))
			# Pas deux fois le même document à payer dans le lot.
			if item.reference_name:
				if item.reference_name in seen_refs:
					frappe.throw(
						_("Ligne {0} : le document {1} est déjà présent dans ce lot.").format(
							idx, item.reference_name
						)
					)
				seen_refs.add(item.reference_name)

	def _new_log(self, action_type, *, direction="Outbound", message_id=None):
		log = frappe.new_doc("BOA Payment Log")
		log.payment_batch = self.name
		log.action_type = action_type
		log.direction = direction
		log.timestamp = now_datetime()
		if message_id:
			log.message_id = message_id
		return log

	def _transition(self, status, *, domibus_status=None, source="Système", detail=None):
		"""Enregistre une transition d'état dans l'historique immuable du lot et
		met à jour le statut courant.

		Contexte bancaire : l'historique est **append-only** et **chaîné par
		hash** (chaque ligne scelle la précédente via SHA-256) afin que toute
		altération a posteriori soit détectable. La ligne enfant est insérée
		directement — sans re-`save()` du parent — pour fonctionner sur un lot
		déjà soumis sans rejouer ses validations.
		"""
		moment = now_datetime()
		last = frappe.get_all(
			"BOA Payment Status History",
			filters={
				"parent": self.name,
				"parenttype": self.doctype,
				"parentfield": "status_history",
			},
			fields=["timestamp", "event_hash", "idx"],
			order_by="idx desc",
			limit=1,
		)
		prev_hash = (last[0].event_hash or "") if last else ""
		elapsed = 0
		if last and last[0].timestamp:
			elapsed = int((moment - get_datetime(last[0].timestamp)).total_seconds())
		next_idx = (last[0].idx if last else 0) + 1

		detail = (detail or "")[:500]
		seal = f"{prev_hash}|{moment}|{status}|{domibus_status or ''}|{source}|{detail}"
		event_hash = hashlib.sha256(seal.encode("utf-8")).hexdigest()

		row = frappe.new_doc("BOA Payment Status History")
		row.update({
			"parent": self.name,
			"parenttype": self.doctype,
			"parentfield": "status_history",
			"idx": next_idx,
			"timestamp": moment,
			"status": status,
			"domibus_status": domibus_status,
			"source": source,
			"detail": detail,
			"elapsed_seconds": elapsed,
			"prev_hash": prev_hash,
			"event_hash": event_hash,
		})
		row.insert(ignore_permissions=True)

		self.db_set("status", status)
		if domibus_status:
			self.db_set("domibus_status", domibus_status)

	# ----------------------------------------------------- payment retrieval

	@frappe.whitelist()
	def get_payments_from_source(self, source_names=None):
		"""Peuple la table `items` depuis les documents sources sélectionnés.

		`source_names` : JSON/list de noms de documents du type `payment_type`.
		Les coordonnées de débit proviennent des BOA Domibus Settings.
		"""
		from paybridge.boa.domibus.source_resolver import get_source

		if self.payment_type == "Manual":
			frappe.throw(_("Récupération indisponible pour le type 'Manual'."))

		if isinstance(source_names, str):
			source_names = json.loads(source_names)
		if not source_names:
			frappe.throw(_("Aucun document source sélectionné."))

		debit = get_debit_coordinates(company=self.company)
		if not debit.is_complete():
			frappe.throw(
				_("Compte émetteur introuvable. Configurez-le dans BOA Domibus Settings "
				  "ou définissez un compte bancaire de société par défaut.")
			)

		source = get_source(self.payment_type)
		existing_refs = {item.reference_name for item in self.items if item.reference_name}

		added, errors = 0, []
		for name in source_names:
			if name in existing_refs:
				continue
			try:
				item = source.build_item(name, debit)
			except DomibusError as exc:
				errors.append(f"{name} : {exc}")
				continue
			if not (item.get("credit_bban_code") or item.get("credit_source_account")):
				errors.append(
					_("{0} : coordonnées bancaires du bénéficiaire manquantes").format(name)
				)
				continue
			self.append("items", item)
			added += 1

		self._calculate_totals()
		self.save()

		if errors:
			frappe.msgprint(
				_("{0} paiement(s) ajouté(s). {1} ignoré(s) :<br>{2}").format(
					added, len(errors), "<br>".join(errors)
				),
				title=_("Récupération partielle"),
				indicator="orange",
			)
		else:
			frappe.msgprint(
				_("{0} paiement(s) ajouté(s) au lot.").format(added),
				indicator="green",
				alert=True,
			)
		return {"added": added, "errors": errors}

	# ------------------------------------------------------------- domibus i/o

	@frappe.whitelist()
	def send_to_domibus(self):
		"""Génère le CSV et le transmet à la BOA via Domibus (submitRequest)."""
		from paybridge.boa.domibus.client import DomibusClient
		from paybridge.boa.domibus.csv_builder import build_csv

		if self.docstatus != 1:
			frappe.throw(_("Le lot doit être soumis avant l'envoi."))
		if self.domibus_message_id and self.domibus_status not in ("", None, *_TRANSPORT_FAILED_STATUSES):
			frappe.throw(
				_("Ce lot a déjà été envoyé (ID : {0}).").format(self.domibus_message_id)
			)

		self._stamp_transmission_date()
		csv_content = build_csv(self)
		client = DomibusClient()
		original_ref = self.original_message_ref or client.generate_original_message_id()

		log = self._new_log("Submit")
		log.request_payload = csv_content[:10000]
		start = time.monotonic()

		try:
			message_id, response, envelope = client.submit_message(csv_content, self.name, original_ref)
			log.message_id = message_id
			log.status_code = str(response.status_code)
			log.duration_ms = int((time.monotonic() - start) * 1000)
			# Audit de non-répudiation : on conserve l'enveloppe SOAP/ebMS
			# RÉELLEMENT transmise (routage : PartyId, Service, Action,
			# MessageProperties…), pas seulement le CSV de payload.
			log.request_envelope = (envelope or "")[:20000]
			log.response_soap = (response.text or "")[:10000]
			log.domibus_status = "SENT"

			self.db_set("original_message_ref", original_ref)
			self.db_set("domibus_message_id", message_id)
			self.db_set("retry_count", cint(self.retry_count) + 1)
			self._attach_csv(csv_content)
			self._transition(
				"Sent", domibus_status="SENT", source="Système",
				detail="Transmis à Domibus (submitRequest)",
			)

			frappe.msgprint(
				_("Lot transmis à Domibus. ID Message : {0}").format(message_id),
				indicator="green",
				alert=True,
			)
		except Exception as exc:
			log.domibus_status = "SEND_FAILURE"
			log.error_detail = str(exc)
			log.duration_ms = int((time.monotonic() - start) * 1000)
			self.db_set("original_message_ref", original_ref)
			self._transition(
				"Failed", domibus_status="SEND_FAILURE", source="Système",
				detail=f"Échec d'envoi : {exc}",
			)
			frappe.log_error(frappe.get_traceback(), "BOA Domibus Submit")
			frappe.msgprint(_("Échec de l'envoi : {0}").format(exc), indicator="red")
		finally:
			log.insert(ignore_permissions=True)

	@frappe.whitelist()
	def check_domibus_status(self, source="Manuel"):
		"""Interroge Domibus sur le statut transport courant du message du lot.

		`source` trace l'origine de l'appel dans l'historique : « Manuel »
		(bouton), « Planifié » (cron) ou « Système » (poll post-envoi).
		"""
		from paybridge.boa.domibus.client import DomibusClient

		if not self.domibus_message_id:
			frappe.throw(_("Aucun ID de message Domibus. Envoyez d'abord le lot."))

		client = DomibusClient()
		previous_status = self.domibus_status
		log = self._new_log("Check Status", message_id=self.domibus_message_id)
		start = time.monotonic()
		status = None

		try:
			status = client.get_message_status(self.domibus_message_id)
			log.domibus_status = status
			log.duration_ms = int((time.monotonic() - start) * 1000)
			self.db_set("last_status_check", now_datetime())

			# La confirmation métier (Confirmed/Rejected) ne vient QUE de la
			# pain.002 (flux retour). Un accusé transport vaut « arrivé à la
			# BOA », pas « virement exécuté » → on ne dépasse pas Acknowledged,
			# et on ne rétrograde jamais un lot déjà tranché par le flux retour.
			if self.status in ("Confirmed", "Rejected"):
				self.db_set("domibus_status", status)
			elif status in _TRANSPORT_ACK_STATUSES:
				self._transition("Acknowledged", domibus_status=status, source=source)
			elif status in _TRANSPORT_FAILED_STATUSES:
				# Échec technique de transmission : on récupère le détail.
				reason = ""
				try:
					reason = client.get_message_errors(self.domibus_message_id) or ""
					log.error_detail = reason[:5000]
					self.db_set("rejection_reason", reason[:1000])
				except Exception:
					pass
				self._transition(
					"Failed", domibus_status=status, source=source,
					detail=reason[:500] or f"Échec transport : {status}",
				)
			else:
				# Statut transport intermédiaire (en cours) : pas de transition
				# métier, on rafraîchit seulement le statut Domibus.
				self.db_set("domibus_status", status)

			if source == "Manuel":
				frappe.msgprint(
					_("Statut Domibus : {0}").format(status), indicator="blue", alert=True
				)
		except Exception as exc:
			log.error_detail = str(exc)
			log.duration_ms = int((time.monotonic() - start) * 1000)
			frappe.log_error(frappe.get_traceback(), "BOA Domibus Check Status")
			if source == "Manuel":
				frappe.msgprint(_("Erreur de vérification : {0}").format(exc), indicator="red")
		finally:
			# On ne journalise qu'un événement utile : changement de statut ou
			# erreur. Les sondages répétés sans changement (planificateur toutes
			# les 10 min) ne créent plus de BOA Payment Log → audit lisible.
			if log.error_detail or status != previous_status:
				log.insert(ignore_permissions=True)

	def _stamp_transmission_date(self):
		"""Fige la date de transmission au jour de l'envoi effectif à la BOA.

		Contexte bancaire : la date d'exécution portée dans le CSV est celle du
		jour où le fichier part réellement vers Domibus, et non la date d'une
		facture source. Posée en base par `db_set` (donc hors contrôle « non
		modifiable après soumission ») au moment de l'envoi, puis figée.
		"""
		send_date = today()
		if str(self.transaction_date) != send_date:
			self.db_set("transaction_date", send_date)
		for item in self.items:
			if str(item.transfer_date) != send_date:
				item.db_set("transfer_date", send_date)

	def _attach_csv(self, csv_content: str):
		"""Attache le CSV envoyé au lot pour archivage/audit."""
		from frappe.utils.file_manager import save_file

		file_doc = save_file(
			f"{self.name}.csv",
			csv_content.encode("utf-8"),
			self.doctype,
			self.name,
			is_private=1,
		)
		self.db_set("csv_file", file_doc.file_url)


def send_batch_async(batch_name: str):
	"""Tâche de fond : transmet un lot à Domibus juste après sa validation,
	puis relève une première fois le statut transport.

	`submitMessage` ne fait que mettre le message en file ; l'accusé AS4
	(« ACKNOWLEDGED » = bien reçu par l'access point de la BOA) arrive de façon
	asynchrone. Ce premier relevé confirme la prise en charge par Domibus et
	fait remonter immédiatement un éventuel échec dur ; le planificateur
	poursuit ensuite jusqu'à l'accusé. NB : il n'atteste PAS l'exécution du
	virement — seule la pain.002 (flux retour) la confirme (→ Confirmed).
	"""
	doc = frappe.get_doc("BOA Payment Batch", batch_name)
	if doc.docstatus != 1:
		return
	doc.send_to_domibus()
	if doc.domibus_message_id and doc.status == "Sent":
		doc.check_domibus_status(source="Système")
		# Pas encore d'accusé ? On lance un sondage borné en tâche de fond pour
		# atteindre Acknowledged/Failed sans clic ; le cron */10 prend le relais.
		doc.reload()
		if doc.status == "Sent":
			frappe.enqueue(
				"paybridge.boa.doctype.boa_payment_batch.boa_payment_batch.poll_status_until_ack",
				queue="long",
				timeout=600,
				batch_name=batch_name,
			)


#: Sondage post-envoi : délais (s) entre tentatives, backoff borné (~5 min au
#: total). Volontairement fini — jamais de boucle infinie ni de worker web
#: bloqué (le job tourne sur la file « long »).
_POLL_BACKOFF = (15, 30, 60, 90, 120)


def poll_status_until_ack(batch_name: str):
	"""Sonde `getStatus` en tâche de fond jusqu'à un statut transport terminal.

	Transpose la logique « attendre l'accusé » d'un script de test, mais bornée
	et asynchrone : le lot atteint Acknowledged/Failed tout seul. Si le job
	expire avant l'accusé, le cron `scheduled_check_status` (*/10) poursuit.
	"""
	for delay in _POLL_BACKOFF:
		time.sleep(delay)
		doc = frappe.get_doc("BOA Payment Batch", batch_name)
		if doc.docstatus != 1 or not doc.domibus_message_id or doc.status != "Sent":
			return
		doc.check_domibus_status(source="Système")
		doc.reload()
		if doc.status != "Sent":
			return
