# Copyright (c) 2026, AMOAMAN and contributors
# For license information, please see license.txt
"""Activité Domibus, agrégée par type d'action depuis les BOA Payment Log.

Volumétrie des échanges (envois, vérifications de statut, réceptions), latence
moyenne et taux d'erreur — utile pour superviser la santé de l'intégration.
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
		{"label": _("Type Action"), "fieldname": "action_type", "fieldtype": "Data", "width": 140},
		{"label": _("Total"), "fieldname": "total", "fieldtype": "Int", "width": 90},
		{"label": _("Succès"), "fieldname": "success", "fieldtype": "Int", "width": 90},
		{"label": _("Erreurs"), "fieldname": "errors", "fieldtype": "Int", "width": 90},
		{
			"label": _("Taux Erreur %"),
			"fieldname": "error_rate",
			"fieldtype": "Percent",
			"width": 120,
		},
		{
			"label": _("Latence Moy. (ms)"),
			"fieldname": "avg_duration",
			"fieldtype": "Int",
			"width": 140,
		},
		{
			"label": _("Latence Max (ms)"),
			"fieldname": "max_duration",
			"fieldtype": "Int",
			"width": 140,
		},
	]


def _conditions(filters):
	cond = "1=1"
	values = {}
	if filters.get("from_date"):
		cond += " AND timestamp >= %(from_date)s"
		values["from_date"] = filters["from_date"]
	if filters.get("to_date"):
		cond += " AND timestamp <= %(to_date)s"
		values["to_date"] = filters["to_date"]
	if filters.get("action_type"):
		cond += " AND action_type = %(action_type)s"
		values["action_type"] = filters["action_type"]
	return cond, values


def _fetch(filters):
	cond, values = _conditions(filters)
	rows = frappe.db.sql(
		f"""
		SELECT
			COALESCE(action_type, 'N/A') AS action_type,
			COUNT(*) AS total,
			SUM(error_detail IS NULL OR error_detail = '') AS success,
			SUM(error_detail IS NOT NULL AND error_detail != '') AS errors,
			AVG(duration_ms) AS avg_duration,
			MAX(duration_ms) AS max_duration
		FROM `tabBOA Payment Log`
		WHERE {cond}
		GROUP BY action_type
		ORDER BY total DESC
		""",
		values,
		as_dict=True,
	)
	for r in rows:
		total = int(r.total or 0)
		r.success = int(r.success or 0)
		r.errors = int(r.errors or 0)
		r.error_rate = round(100.0 * r.errors / total, 1) if total else 0.0
		r.avg_duration = int(r.avg_duration or 0)
		r.max_duration = int(r.max_duration or 0)
	return rows


def _chart(data):
	return {
		"data": {
			"labels": [r.action_type for r in data],
			"datasets": [
				{"name": _("Succès"), "values": [r.success for r in data]},
				{"name": _("Erreurs"), "values": [r.errors for r in data]},
			],
		},
		"type": "bar",
		"colors": ["#28a745", "#dc3545"],
		"barOptions": {"stacked": 1},
	}


def _summary(data):
	total = sum(int(r.total or 0) for r in data)
	errors = sum(int(r.errors or 0) for r in data)
	rate = round(100.0 * errors / total, 1) if total else 0.0
	return [
		{"label": _("Échanges"), "value": total, "indicator": "blue"},
		{"label": _("Erreurs"), "value": errors, "indicator": "red" if errors else "green"},
		{
			"label": _("Taux Erreur"),
			"value": f"{rate} %",
			"indicator": "green" if rate < 5 else ("orange" if rate < 20 else "red"),
		},
	]
