app_name = "paybridge"
app_title = "PayBridge"
app_publisher = "AMOAMAN"
app_description = "Passerelle de paiement"
app_email = "developers@amoaman.com"
app_license = "mit"

# Fixtures
fixtures = [
	"Role",
	{"dt": "Custom Field", "filters": [["module", "=", "BOA"]]},
]

# Apps
# ------------------

# required_apps = []

add_to_apps_screen = [
	{
		"name": "paybridge",
		"logo": "/assets/paybridge/images/bank_of_africa_cte_d_ivoire_logo.jpeg",
		"title": "BOA",
		"route": "/paybridge/BOA",
	}
]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/paybridge/css/paybridge.css"
# app_include_js = "/assets/paybridge/js/paybridge.js"

# include js, css files in header of web template
# web_include_css = "/assets/paybridge/css/paybridge.css"
# web_include_js = "/assets/paybridge/js/paybridge.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "paybridge/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "paybridge/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "paybridge.utils.jinja_methods",
# 	"filters": "paybridge.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "paybridge.install.before_install"
# after_install = "paybridge.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "paybridge.uninstall.before_uninstall"
# after_uninstall = "paybridge.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "paybridge.utils.before_app_install"
# after_app_install = "paybridge.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "paybridge.utils.before_app_uninstall"
# after_app_uninstall = "paybridge.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "paybridge.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	# Le Bank Account est la source unique des coordonnées BOA d'un tiers :
	# on y valide le RIB (custom_rib) et le code banque (custom_bank_code) à l'enregistrement.
	"Bank Account": {
		"validate": [
			"paybridge.boa.bank_account.validate_rib",
			"paybridge.boa.bank_account.validate_bank_code",
		],
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
    "cron": {
        # Vérification statut Domibus toutes les 10 minutes
        "*/10 * * * *": [
            "paybridge.boa.api.scheduled_check_status",
        ],
        # Récupération messages entrants toutes les 5 minutes
        "*/5 * * * *": [
            "paybridge.boa.api.scheduled_receive_messages",
        ],
    },
}

# Testing
# -------

# before_tests = "paybridge.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "paybridge.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "paybridge.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["paybridge.utils.before_request"]
# after_request = ["paybridge.utils.after_request"]

# Job Events
# ----------
# before_job = ["paybridge.utils.before_job"]
# after_job = ["paybridge.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"paybridge.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

