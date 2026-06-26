# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt
"""Client SOAP du WS Plugin Domibus (eDelivery AS4).

Aligné sur le contrat réel du plugin (`http://eu.domibus.wsplugin/`) :
    - SOAP 1.2 (`application/soap+xml`) ;
    - submitRequest : enveloppe SOAP + fichier en pièce jointe MTOM/XOP
      (`Content-Transfer-Encoding: binary`, Content-ID `<message>`,
      référencé par `cid:message` / `xop:Include`) ;
    - statusRequestWithAccessPointRole / getErrorsRequestWithAccessPointRole
      (suivi d'un message émis, en rôle SENDING) ;
    - listPendingMessagesRequest / retrieveMessageRequest ;
    - l'ID de message Domibus est **généré par le serveur** et extrait de la
      réponse (`<messageID>`).

Responsabilités strictement techniques (transport + (dé)sérialisation). Les
erreurs remontent via `DomibusError` ; l'appelant décide de l'affichage.

Sécurité : valeurs interpolées échappées (anti-injection XML) ; parsing des
réponses durci contre les entités externes (anti-XXE).
"""

import base64
import binascii
import time
import uuid
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

import frappe
import requests
from frappe.utils import now_datetime
from lxml import etree

from paybridge.boa.domibus.exceptions import DomibusConnectionError, DomibusSoapFault

# Namespaces ----------------------------------------------------------------
NS_SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
NS_WSPLUGIN = "http://eu.domibus.wsplugin/"
NS_EBMS = "http://docs.oasis-open.org/ebxml-msg/ebms/v3.0/ns/core/200704/"
NS_XOP = "http://www.w3.org/2004/08/xop/include"

ROLE_SENDER = "http://docs.oasis-open.org/ebxml-msg/ebms/v3.0/ns/core/200704/sender"
ROLE_RECEIVER = "http://docs.oasis-open.org/ebxml-msg/ebms/v3.0/ns/core/200704/receiver"

#: Rôle de notre access point (MSH) pour le message interrogé. Le suivi d'un lot
#: que NOUS émettons se fait toujours en « SENDING » : cela lève l'ambiguïté
#: quand l'AP joue les deux rôles et évite des NOT_FOUND erronés (statusRequest
#: sans rôle peut interroger la mauvaise face). Cf. mshRole du WS plugin.
MSH_ROLE_SENDING = "SENDING"
MSH_ROLE_RECEIVING = "RECEIVING"

#: Content-ID de la pièce jointe (doit correspondre à cid:message).
PAYLOAD_CID = "message"

#: Statuts Domibus connus (utilisés pour extraire le statut d'une réponse).
DOMIBUS_STATUSES = {
	"READY_TO_SEND", "READY_TO_PULL", "BEING_PULLED", "SEND_ENQUEUED",
	"SEND_IN_PROGRESS", "WAITING_FOR_RECEIPT", "ACKNOWLEDGED",
	"ACKNOWLEDGED_WITH_WARNING", "SEND_ATTEMPT_FAILED", "SEND_FAILURE",
	"NOT_FOUND", "WAITING_FOR_RETRY", "RECEIVED", "RECEIVED_WITH_WARNINGS",
	"DELETED", "DOWNLOADED", "SENT", "DELIVERED", "FAILED",
}

#: Codes HTTP transitoires justifiant un retry réseau.
_TRANSIENT_HTTP = {502, 503, 504}

# Template de l'enveloppe submitRequest (SOAP 1.2 + ebMS + XOP) -------------
SOAP_SUBMIT_TEMPLATE = """<soap12:Envelope xmlns:soap12="{ns_soap}" xmlns:tns="{ns_tns}" xmlns:eb="{ns_eb}" xmlns:xop="{ns_xop}">
  <soap12:Header>
    <eb:Messaging>
      <eb:UserMessage>
        <eb:MessageInfo>
          <eb:Timestamp>{timestamp}</eb:Timestamp>
        </eb:MessageInfo>
        <eb:PartyInfo>
          <eb:From>
            <eb:PartyId type="urn:oasis:names:tc:ebcore:partyid-type:unregistered">{from_party_id}</eb:PartyId>
            <eb:Role>{from_role}</eb:Role>
          </eb:From>
          <eb:To>
            <eb:PartyId type="urn:oasis:names:tc:ebcore:partyid-type:unregistered">{to_party_id}</eb:PartyId>
            <eb:Role>{to_role}</eb:Role>
          </eb:To>
        </eb:PartyInfo>
        <eb:CollaborationInfo>
          <eb:Service type="{service_type}">{service_value}</eb:Service>
          <eb:Action>{action}</eb:Action>
          <eb:ConversationId>{conversation_id}</eb:ConversationId>
        </eb:CollaborationInfo>
        <eb:MessageProperties>
          <eb:Property name="originalMessageId">{original_message_id}</eb:Property>
          <eb:Property name="bankId">{bank_id}</eb:Property>
          <eb:Property name="fileFormat">{file_format}</eb:Property>
          <eb:Property name="originalSender">{original_sender}</eb:Property>
          <eb:Property name="finalRecipient">{final_recipient}</eb:Property>
          <eb:Property name="processingType">{processing_type}</eb:Property>
          <eb:Property name="initiationDate">{initiation_date}</eb:Property>
          <eb:Property name="contentType">text/csv</eb:Property>
        </eb:MessageProperties>
        <eb:PayloadInfo>
          <eb:PartInfo href="cid:{cid}">
            <eb:PartProperties>
              <eb:Property name="MimeType">text/csv</eb:Property>
            </eb:PartProperties>
          </eb:PartInfo>
        </eb:PayloadInfo>
      </eb:UserMessage>
    </eb:Messaging>
  </soap12:Header>
  <soap12:Body>
    <tns:submitRequest>
      <payload payloadId="cid:{cid}" contentType="text/csv">
        <value>
          <xop:Include href="cid:{cid}"/>
        </value>
      </payload>
    </tns:submitRequest>
  </soap12:Body>
</soap12:Envelope>"""

# Variantes « WithAccessPointRole » : on précise notre rôle (SENDING) pour
# interroger la face émettrice du message — plus fiable que statusRequest /
# getErrorsRequest sans rôle.
SOAP_STATUS_TEMPLATE = """<soap12:Envelope xmlns:soap12="{ns_soap}" xmlns:tns="{ns_tns}">
  <soap12:Body>
    <tns:statusRequestWithAccessPointRole>
      <messageID>{message_id}</messageID>
      <accessPointRole>{access_point_role}</accessPointRole>
    </tns:statusRequestWithAccessPointRole>
  </soap12:Body>
</soap12:Envelope>"""

SOAP_ERRORS_TEMPLATE = """<soap12:Envelope xmlns:soap12="{ns_soap}" xmlns:tns="{ns_tns}">
  <soap12:Body>
    <tns:getErrorsRequestWithAccessPointRole>
      <messageID>{message_id}</messageID>
      <accessPointRole>{access_point_role}</accessPointRole>
    </tns:getErrorsRequestWithAccessPointRole>
  </soap12:Body>
</soap12:Envelope>"""

SOAP_LIST_PENDING_TEMPLATE = """<soap12:Envelope xmlns:soap12="{ns_soap}" xmlns:tns="{ns_tns}">
  <soap12:Body>
    <tns:listPendingMessagesRequest/>
  </soap12:Body>
</soap12:Envelope>"""

SOAP_RETRIEVE_TEMPLATE = """<soap12:Envelope xmlns:soap12="{ns_soap}" xmlns:tns="{ns_tns}">
  <soap12:Body>
    <tns:retrieveMessageRequest>
      <messageID>{message_id}</messageID>
    </tns:retrieveMessageRequest>
  </soap12:Body>
</soap12:Envelope>"""

SOAP_MARK_DOWNLOADED_TEMPLATE = """<soap12:Envelope xmlns:soap12="{ns_soap}" xmlns:tns="{ns_tns}">
  <soap12:Body>
    <tns:markMessageAsDownloadedRequest>
      <messageID>{message_id}</messageID>
    </tns:markMessageAsDownloadedRequest>
  </soap12:Body>
</soap12:Envelope>"""

#: Content-Type des appels SOAP simples (sans pièce jointe).
SOAP12_CONTENT_TYPE = "application/soap+xml; charset=UTF-8"


def _safe_parser() -> etree.XMLParser:
	"""Parser lxml durci contre les attaques XXE / entités externes."""
	return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def _xe(value) -> str:
	"""Échappe une valeur pour insertion sûre dans le XML."""
	return xml_escape("" if value is None else str(value))


def _iso8601_nanos() -> str:
	"""Horodatage UTC ISO 8601 à précision nanoseconde, suffixe Z.

	Format attendu par Domibus/BOA pour `initiationDate`
	(ex. ``2026-05-18T10:12:46.116958399Z``). Python n'a qu'une précision
	microseconde ; on complète à 9 chiffres à partir de `time.time_ns()`.
	"""
	seconds, nanos = divmod(time.time_ns(), 1_000_000_000)
	base = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
	return f"{base}.{nanos:09d}Z"


class DomibusClient:
	def __init__(self, settings=None):
		if settings is None:
			settings = frappe.get_cached_doc("BOA Domibus Settings")
		self.settings = settings
		self.endpoint = (
			settings.prod_endpoint_url if settings.environment == "PROD" else settings.endpoint_url
		)
		if not self.endpoint:
			raise DomibusConnectionError(
				"URL d'endpoint Domibus non configurée pour l'environnement "
				+ (settings.environment or "TEST")
			)
		self.timeout = settings.request_timeout or 30
		self.max_retries = max(0, int(settings.get("max_retry_count") or 0))

	# --------------------------------------------------------------- transport

	def _build_session(self) -> requests.Session:
		session = requests.Session()
		if self.settings.use_authentication:
			session.auth = (self.settings.username, self.settings.get_password("password"))
		if self.settings.use_tls and self.settings.client_certificate:
			session.cert = self._site_file_path(self.settings.client_certificate)
			session.verify = (
				self._site_file_path(self.settings.ca_certificate)
				if self.settings.ca_certificate
				else True
			)
		return session

	@staticmethod
	def _site_file_path(file_url: str) -> str:
		return frappe.get_site_path("private", "files", file_url.split("/")[-1])

	def _post(self, body: bytes, *, content_type: str) -> requests.Response:
		"""POST HTTP avec retry exponentiel sur erreurs transitoires uniquement."""
		headers = {"Content-Type": content_type}
		session = self._build_session()
		attempt = 0
		while True:
			try:
				response = session.post(
					self.endpoint, data=body, headers=headers, timeout=self.timeout
				)
			except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
				if attempt < self.max_retries:
					time.sleep(2**attempt)
					attempt += 1
					continue
				raise DomibusConnectionError(
					f"Connexion Domibus impossible après {attempt + 1} tentative(s) : {exc}"
				) from exc

			if response.status_code in _TRANSIENT_HTTP and attempt < self.max_retries:
				time.sleep(2**attempt)
				attempt += 1
				continue
			return response

	def _post_soap(self, soap_body: str) -> requests.Response:
		return self._post(soap_body.encode("utf-8"), content_type=SOAP12_CONTENT_TYPE)

	# ----------------------------------------------------------- id generators

	def generate_original_message_id(self) -> str:
		"""Référence métier corrélant requête/réponse (originalMessageId)."""
		prefix = self.settings.message_id_prefix or "AMOAMAN"
		stamp = now_datetime().strftime("%Y%m%d%H%M%S")
		return f"{prefix}-CSV-{stamp}-{uuid.uuid4().hex[:12].upper()}"

	def generate_conversation_id(self) -> str:
		prefix = self.settings.conversation_id_prefix or "conv"
		return f"{prefix}-{now_datetime().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

	# ----------------------------------------------------------------- parsing

	@staticmethod
	def _parse(response: requests.Response):
		"""Parse la réponse en élément lxml ; lève DomibusSoapFault sur Fault."""
		try:
			root = etree.fromstring(response.content, parser=_safe_parser())
		except etree.XMLSyntaxError as exc:
			raise DomibusSoapFault(
				f"Réponse Domibus illisible : {exc}",
				status_code=response.status_code,
				raw_response=response.text[:2000],
			) from exc

		fault = root.find(f".//{{{NS_SOAP12}}}Fault")
		if fault is not None:
			reason = "".join(fault.itertext()).strip()
			raise DomibusSoapFault(
				f"SOAP Fault Domibus : {reason or 'erreur inconnue'}",
				status_code=response.status_code,
				raw_response=response.text[:2000],
			)
		return root

	@staticmethod
	def _find_text(root, localname: str):
		for el in root.iter():
			if etree.QName(el).localname == localname and el.text and el.text.strip():
				return el.text.strip()
		return None

	@classmethod
	def _extract_message_id(cls, root):
		return cls._find_text(root, "messageID")

	@classmethod
	def _extract_status(cls, root):
		for el in root.iter():
			if el.text and el.text.strip() in DOMIBUS_STATUSES:
				return el.text.strip()
		return "UNKNOWN"

	# ----------------------------------------------------------------- actions

	def submit_message(self, csv_content: str, batch_name: str, original_message_id: str):
		"""Envoie un CSV via submitRequest (MTOM/XOP) ; retourne (message_id, response).

		`message_id` est l'identifiant **Domibus** extrait de la réponse.
		"""
		s = self.settings
		conversation_id = self.generate_conversation_id()
		timestamp = now_datetime().strftime("%Y-%m-%dT%H:%M:%SZ")

		envelope = SOAP_SUBMIT_TEMPLATE.format(
			ns_soap=NS_SOAP12, ns_tns=NS_WSPLUGIN, ns_eb=NS_EBMS, ns_xop=NS_XOP,
			cid=PAYLOAD_CID,
			timestamp=_xe(timestamp),
			from_party_id=_xe(s.company_party_id),
			from_role=_xe(s.initiator_role or ROLE_SENDER),
			to_party_id=_xe(s.receiver_party_id),
			to_role=_xe(s.responder_role or ROLE_RECEIVER),
			service_type=_xe(s.service_type or "tc1"),
			service_value=_xe(s.service_value or "PAYMENTS"),
			action=_xe(s.action or "DomesticCreditTransfer"),
			conversation_id=_xe(conversation_id),
			original_message_id=_xe(original_message_id),
			bank_id=_xe(s.bank_id or ""),
			file_format=_xe(s.get("file_format") or "STANDARD_CSV"),
			original_sender=_xe(s.original_sender or s.company_party_id),
			final_recipient=_xe(s.final_recipient or s.receiver_party_id),
			processing_type=_xe(s.get("processing_type") or "BATCH"),
			initiation_date=_xe(_iso8601_nanos()),
		)

		body, content_type = self._build_mtom(envelope, csv_content)
		response = self._post(body, content_type=content_type)

		if response.status_code not in (200, 202):
			try:
				self._parse(response)  # tente d'extraire un Fault exploitable
			except DomibusSoapFault:
				raise
			raise DomibusSoapFault(
				f"Envoi refusé par Domibus (HTTP {response.status_code})",
				status_code=response.status_code,
				raw_response=response.text[:2000],
			)

		root = self._parse(response)
		message_id = self._extract_message_id(root)
		if not message_id:
			raise DomibusSoapFault(
				"Réponse submitRequest sans messageID",
				status_code=response.status_code,
				raw_response=response.text[:2000],
			)
		# On retourne aussi l'enveloppe SOAP/ebMS transmise (audit de
		# non-répudiation côté appelant).
		return message_id, response, envelope

	@staticmethod
	def _build_mtom(envelope: str, csv_content: str):
		"""Assemble le colis MIME multipart/related (XOP) : enveloppe + CSV binaire."""
		boundary = "----=_Part_" + uuid.uuid4().hex
		root_cid = "root.message@amoaman.com"
		crlf = b"\r\n"
		b = boundary.encode()

		part_soap = (
			b"--" + b + crlf
			+ b'Content-Type: application/xop+xml; charset=UTF-8; type="application/soap+xml"' + crlf
			+ b"Content-Transfer-Encoding: binary" + crlf
			+ b"Content-ID: <" + root_cid.encode() + b">" + crlf
			+ crlf + envelope.encode("utf-8") + crlf
		)
		part_file = (
			b"--" + b + crlf
			+ b"Content-Type: application/octet-stream" + crlf
			+ b"Content-Transfer-Encoding: binary" + crlf
			+ b"Content-ID: <" + PAYLOAD_CID.encode() + b">" + crlf
			+ crlf + csv_content.encode("utf-8") + crlf
		)
		closing = b"--" + b + b"--" + crlf
		body = part_soap + part_file + closing

		content_type = (
			'multipart/related; type="application/xop+xml"; '
			f'boundary="{boundary}"; start="<{root_cid}>"; '
			'start-info="application/soap+xml"'
		)
		return body, content_type

	def get_message_status(self, message_id: str) -> str:
		"""Statut transport courant (enum Domibus) ; 'UNKNOWN' si introuvable.

		Interrogé en rôle « SENDING » (on est l'émetteur du lot).
		"""
		body = SOAP_STATUS_TEMPLATE.format(
			ns_soap=NS_SOAP12, ns_tns=NS_WSPLUGIN,
			message_id=_xe(message_id), access_point_role=MSH_ROLE_SENDING,
		)
		root = self._parse(self._post_soap(body))
		return self._extract_status(root)

	def get_message_errors(self, message_id: str) -> str:
		"""Détail lisible des erreurs transport d'un message (rôle SENDING).

		Le WS plugin renvoie un `errorResultImplArray` (liste de `item` avec
		`domibusErrorCode`/`errorDetail`). On en extrait un résumé « code : détail »
		par erreur ; à défaut de structure exploitable, on retombe sur le texte brut.
		"""
		body = SOAP_ERRORS_TEMPLATE.format(
			ns_soap=NS_SOAP12, ns_tns=NS_WSPLUGIN,
			message_id=_xe(message_id), access_point_role=MSH_ROLE_SENDING,
		)
		response = self._post_soap(body)
		try:
			root = etree.fromstring(response.content, parser=_safe_parser())
		except etree.XMLSyntaxError:
			return (response.text or "").strip()

		errors = []
		for item in root.iter():
			if etree.QName(item).localname != "item":
				continue
			code = detail = None
			for el in item.iter():
				ln = etree.QName(el).localname
				if ln == "domibusErrorCode" and el.text and el.text.strip():
					code = el.text.strip()
				elif ln == "errorDetail" and el.text and el.text.strip():
					detail = el.text.strip()
			segment = " : ".join(part for part in (code, detail) if part)
			if segment:
				errors.append(segment)
		return " | ".join(errors) if errors else (response.text or "").strip()

	def list_pending_messages(self) -> list:
		"""IDs des messages entrants en attente de récupération."""
		body = SOAP_LIST_PENDING_TEMPLATE.format(ns_soap=NS_SOAP12, ns_tns=NS_WSPLUGIN)
		root = self._parse(self._post_soap(body))
		return [
			el.text.strip()
			for el in root.iter()
			if etree.QName(el).localname == "messageID" and el.text and el.text.strip()
		]

	def retrieve_message(self, message_id: str) -> requests.Response:
		"""Récupère le contenu (payload) d'un message entrant (réponse MTOM)."""
		body = SOAP_RETRIEVE_TEMPLATE.format(
			ns_soap=NS_SOAP12, ns_tns=NS_WSPLUGIN, message_id=_xe(message_id)
		)
		response = self._post_soap(body)
		if response.status_code not in (200, 202):
			raise DomibusSoapFault(
				f"Récupération du message {message_id} échouée (HTTP {response.status_code})",
				status_code=response.status_code,
				raw_response=response.text[:2000],
			)
		return response

	def mark_message_as_downloaded(self, message_id: str) -> requests.Response:
		"""Marque un message comme téléchargé (sort de listPendingMessages).

		Indispensable dans le flux entrant : sans cet appel, le message
		réapparaît indéfiniment dans la liste des messages en attente.
		"""
		body = SOAP_MARK_DOWNLOADED_TEMPLATE.format(
			ns_soap=NS_SOAP12, ns_tns=NS_WSPLUGIN, message_id=_xe(message_id)
		)
		return self._post_soap(body)

	@staticmethod
	def parse_mtom_response(response: requests.Response):
		"""Décompose une réponse MTOM en (propriétés ebMS, pièces jointes).

		Retourne (properties: dict, attachments: dict[content_id -> bytes]).
		Gère aussi bien une réponse multipart/related qu'une réponse SOAP simple.
		"""
		from email import message_from_bytes

		content_type = response.headers.get("Content-Type", "")
		raw = b"Content-Type: " + content_type.encode("utf-8", "ignore") + b"\r\n\r\n" + response.content
		msg = message_from_bytes(raw)

		properties: dict = {}
		attachments: dict = {}
		soap_bytes = None

		parts = msg.walk() if msg.is_multipart() else [msg]
		for part in parts:
			if part.get_content_maintype() == "multipart":
				continue
			payload = part.get_payload(decode=True)
			if payload is None:
				continue
			ctype = (part.get_content_type() or "").lower()
			cid = (part.get("Content-ID") or "").strip("<>")
			if "xml" in ctype or "soap" in ctype:
				soap_bytes = payload
			else:
				attachments[cid or f"part{len(attachments)}"] = payload

		# Si non multipart, le corps est l'enveloppe SOAP
		if soap_bytes is None and not msg.is_multipart():
			soap_bytes = response.content

		if soap_bytes:
			try:
				root = etree.fromstring(soap_bytes, parser=_safe_parser())
				for el in root.iter():
					ln = etree.QName(el).localname
					if ln == "Property":
						name = el.get("name")
						if name and el.text:
							properties[name] = el.text.strip()
					elif ln == "value" and el.text and el.text.strip():
						# Réponse SOAP simple (sans MIME) : le payload métier
						# (pain.002) est encodé en base64 dans
						# <payload>/<bodyload><value>. On le décode pour le rendre
						# disponible comme pièce jointe (sinon l'erreur exacte BOA
						# serait perdue). Cf. retrieveMessageResponse.
						parent = el.getparent()
						pln = etree.QName(parent).localname if parent is not None else ""
						if pln in ("payload", "bodyload"):
							try:
								decoded = base64.b64decode(el.text.strip())
								cid = (parent.get("payloadId") or f"value{len(attachments)}").replace("cid:", "")
								attachments[cid] = decoded
							except (ValueError, binascii.Error):
								pass
				mid = next(
					(e.text.strip() for e in root.iter()
					 if etree.QName(e).localname == "messageID" and e.text), None
				)
				if mid:
					properties.setdefault("_messageID", mid)
			except etree.XMLSyntaxError:
				pass

		return properties, attachments

	def test_connection(self) -> int:
		"""Ping fonctionnel : retourne le nombre de messages en attente."""
		return len(self.list_pending_messages())
