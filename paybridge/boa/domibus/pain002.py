# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt
"""Analyse des fichiers retour BOA : ISO 20022 pain.002 (Payment Status Report).

La BOA renvoie, dans le flux entrant, un compte rendu de statut au format
pain.002.001.09 — éventuellement compressé en gzip (cf. propriété ebMS
`CompressionType: application/gzip`). Il corrèle le lot via `OrgnlMsgId`
et porte le statut de groupe (`GrpSts`) ainsi que les motifs de rejet.

Exemple (rejet) :
    <GrpSts>RJCT</GrpSts>
    <StsRsnInf><Rsn><Cd>FF03</Cd></Rsn>
      <AddtlInf>the payload file already exists</AddtlInf></StsRsnInf>
"""

import gzip

from lxml import etree

#: Statuts ISO 20022 acceptés (remise effective).
ACCEPTED_STATUSES = {"ACCP", "ACSP", "ACSC", "ACCC", "ACWC", "ACTC"}
#: Statuts de rejet.
REJECTED_STATUSES = {"RJCT"}
#: Statut partiellement accepté.
PARTIAL_STATUSES = {"PART"}
#: Statuts en attente.
PENDING_STATUSES = {"PDNG", "RCVD"}


def _safe_parser() -> etree.XMLParser:
	return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def maybe_gunzip(data: bytes) -> bytes:
	"""Décompresse si le contenu est gzip (magic bytes 1f 8b)."""
	if data and data[:2] == b"\x1f\x8b":
		try:
			return gzip.decompress(data)
		except Exception:
			return data
	return data


def is_pain002(data: bytes) -> bool:
	return b"CstmrPmtStsRpt" in data or b"pain.002" in data


def parse(xml_bytes: bytes) -> dict:
	"""Extrait statut, motif et corrélation d'un pain.002.

	Retourne un dict :
	    {original_msg_id, group_status, reason_code, additional_info,
	     transactions: [{end_to_end_id, status, reason_code, additional_info}]}
	"""
	xml_bytes = maybe_gunzip(xml_bytes)
	result = {
		"original_msg_id": None,
		"group_status": None,
		"reason_code": None,
		"additional_info": None,
		"transactions": [],
	}
	try:
		root = etree.fromstring(xml_bytes, parser=_safe_parser())
	except etree.XMLSyntaxError:
		return result

	def local(el):
		return etree.QName(el).localname

	def first_text(parent, name):
		for el in parent.iter():
			if local(el) == name and el.text and el.text.strip():
				return el.text.strip()
		return None

	# Niveau groupe
	for grp in root.iter():
		if local(grp) == "OrgnlGrpInfAndSts":
			result["original_msg_id"] = first_text(grp, "OrgnlMsgId")
			result["group_status"] = first_text(grp, "GrpSts")
			result["reason_code"] = first_text(grp, "Cd")
			result["additional_info"] = first_text(grp, "AddtlInf")
			break

	# Fallback : OrgnlMsgId où qu'il soit
	if not result["original_msg_id"]:
		result["original_msg_id"] = first_text(root, "OrgnlMsgId")

	# Niveau transaction (rejets unitaires)
	for tx in root.iter():
		if local(tx) == "TxInfAndSts":
			result["transactions"].append({
				"end_to_end_id": first_text(tx, "OrgnlEndToEndId"),
				"status": first_text(tx, "TxSts"),
				"reason_code": first_text(tx, "Cd"),
				"additional_info": first_text(tx, "AddtlInf"),
			})

	# Statut de groupe déduit du niveau transaction si absent
	if not result["group_status"] and result["transactions"]:
		statuses = {t["status"] for t in result["transactions"] if t["status"]}
		if statuses <= REJECTED_STATUSES:
			result["group_status"] = "RJCT"
		elif statuses & REJECTED_STATUSES:
			result["group_status"] = "PART"

	return result


def map_to_batch_status(group_status: str) -> str | None:
	"""Mappe un statut métier pain.002 vers le statut d'un BOA Payment Batch.

	C'est l'unique source de la confirmation bancaire : `Confirmed` (virement
	exécuté) ou `Rejected` (refusé par la BOA). Les statuts en attente
	(`PDNG`/`RCVD`) ne changent pas le statut (le lot reste « Acknowledged »).
	"""
	if not group_status:
		return None
	if group_status in REJECTED_STATUSES:
		return "Rejected"
	if group_status in PARTIAL_STATUSES:
		# Acceptation partielle : au moins un virement rejeté → à traiter.
		return "Rejected"
	if group_status in ACCEPTED_STATUSES:
		return "Confirmed"
	return None


def format_reason(parsed: dict) -> str:
	"""Construit un message de rejet lisible à partir du pain.002 analysé."""
	parts = []
	if parsed.get("group_status"):
		parts.append(f"Statut: {parsed['group_status']}")
	if parsed.get("reason_code"):
		parts.append(f"Code: {parsed['reason_code']}")
	if parsed.get("additional_info"):
		parts.append(parsed["additional_info"])
	for tx in parsed.get("transactions", []):
		seg = []
		if tx.get("end_to_end_id"):
			seg.append(f"#{tx['end_to_end_id']}")
		if tx.get("status"):
			seg.append(tx["status"])
		if tx.get("reason_code"):
			seg.append(tx["reason_code"])
		if tx.get("additional_info"):
			seg.append(tx["additional_info"])
		if seg:
			parts.append(" ".join(seg))
	return " | ".join(parts)[:1000]
