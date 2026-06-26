# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt
"""Synthèse des lots de paiement BOA, jour par jour.

Pour chaque date de transaction : nombre de lots, paiements, montant total,
lots livrés / en échec, et taux de livraison. Tout est dérivé dynamiquement
des BOA Payment Batch soumis (aucune valeur figée).
"""

import frappe
from frappe import _


def execute(filters=None):
	filters = filters or {}
	columns = _columns()
	data = _fetch(filters)
	chart = _chart(data)
	summary = _summary(data)
	return columns, data, None, chart, summary


def _columns():
	return [
		{"label": _("Date"), "fieldname": "day", "fieldtype": "Date", "width": 110},
		{"label": _("Lots"), "fieldname": "batches", "fieldtype": "Int", "width": 80},
		{"label": _("Paiements"), "fieldname": "payments", "fieldtype": "Int", "width": 100},
		{
			"label": _("Montant Total"),
			"fieldname": "total_amount",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 150,
		},
		{"label": _("Confirmés"), "fieldname": "confirmed", "fieldtype": "Int", "width": 100},
		{"label": _("En cours"), "fieldname": "in_progress", "fieldtype": "Int", "width": 90},
		{"label": _("Rejetés"), "fieldname": "rejected", "fieldtype": "Int", "width": 90},
		{"label": _("Échecs"), "fieldname": "failed", "fieldtype": "Int", "width": 90},
		{
			"label": _("Taux Confirmation %"),
			"fieldname": "confirmation_rate",
			"fieldtype": "Percent",
			"width": 150,
		},
		{
			"label": _("Devise"),
			"fieldname": "currency",
			"fieldtype": "Link",
			"options": "Currency",
			"width": 80,
		},
	]


def _conditions(filters):
	cond = "docstatus = 1"
	values = {}
	if filters.get("company"):
		cond += " AND company = %(company)s"
		values["company"] = filters["company"]
	if filters.get("from_date"):
		cond += " AND transaction_date >= %(from_date)s"
		values["from_date"] = filters["from_date"]
	if filters.get("to_date"):
		cond += " AND transaction_date <= %(to_date)s"
		values["to_date"] = filters["to_date"]
	if filters.get("payment_type"):
		cond += " AND payment_type = %(payment_type)s"
		values["payment_type"] = filters["payment_type"]
	return cond, values


def _fetch(filters):
	cond, values = _conditions(filters)
	default_currency = frappe.defaults.get_global_default("currency")

	rows = frappe.db.sql(
		f"""
		SELECT
			transaction_date AS day,
			COUNT(*) AS batches,
			SUM(items_count) AS payments,
			SUM(total_amount) AS total_amount,
			SUM(status = 'Confirmed') AS confirmed,
			SUM(status IN ('Queued', 'Sent', 'Acknowledged')) AS in_progress,
			SUM(status = 'Rejected') AS rejected,
			SUM(status = 'Failed') AS failed,
			MAX(currency) AS currency
		FROM `tabBOA Payment Batch`
		WHERE {cond}
		GROUP BY transaction_date
		ORDER BY transaction_date ASC
		""",
		values,
		as_dict=True,
	)

	for r in rows:
		batches = int(r.batches or 0)
		r.payments = int(r.payments or 0)
		r.confirmed = int(r.confirmed or 0)
		r.in_progress = int(r.in_progress or 0)
		r.rejected = int(r.rejected or 0)
		r.failed = int(r.failed or 0)
		r.confirmation_rate = round(100.0 * r.confirmed / batches, 1) if batches else 0.0
		r.currency = r.currency or default_currency
	return rows


def _chart(data):
	labels = [str(r.day) for r in data]
	return {
		"data": {
			"labels": labels,
			"datasets": [
				{"name": _("Confirmés"), "values": [r.confirmed for r in data]},
				{"name": _("Rejetés"), "values": [r.rejected for r in data]},
				{"name": _("Échecs"), "values": [r.failed for r in data]},
			],
		},
		"type": "bar",
		"colors": ["#28a745", "#dc3545", "#fd7e14"],
		"axisOptions": {"xIsSeries": 1},
		"barOptions": {"stacked": 1},
	}


def _summary(data):
	total_batches = sum(int(r.batches or 0) for r in data)
	total_payments = sum(int(r.payments or 0) for r in data)
	total_amount = sum(float(r.total_amount or 0) for r in data)
	confirmed = sum(int(r.confirmed or 0) for r in data)
	rejected = sum(int(r.rejected or 0) for r in data)
	failed = sum(int(r.failed or 0) for r in data)
	rate = round(100.0 * confirmed / total_batches, 1) if total_batches else 0.0
	currency = data[0].currency if data else frappe.defaults.get_global_default("currency")

	return [
		{"label": _("Lots"), "value": total_batches, "indicator": "blue"},
		{"label": _("Paiements"), "value": total_payments, "indicator": "blue"},
		{
			"label": _("Montant Total"),
			"value": total_amount,
			"datatype": "Currency",
			"currency": currency,
			"indicator": "green",
		},
		{"label": _("Confirmés"), "value": confirmed, "indicator": "green"},
		{"label": _("Rejetés"), "value": rejected, "indicator": "red" if rejected else "green"},
		{"label": _("Échecs"), "value": failed, "indicator": "orange" if failed else "green"},
		{
			"label": _("Taux Confirmation"),
			"value": f"{rate} %",
			"indicator": "green" if rate >= 90 else ("orange" if rate >= 70 else "red"),
		},
	]
