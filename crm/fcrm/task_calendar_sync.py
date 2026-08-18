import frappe
from frappe.utils import add_to_date, get_datetime


def queue_task_calendar_sync(doc, method=None):
	frappe.enqueue(
		"crm.fcrm.task_calendar_sync.sync_task_calendar_event",
		queue="short",
		enqueue_after_commit=True,
		task_name=doc.name,
	)


def queue_task_calendar_delete(doc, method=None):
	frappe.enqueue(
		"crm.fcrm.task_calendar_sync.delete_task_calendar_events",
		queue="short",
		enqueue_after_commit=True,
		task_name=doc.name,
	)


def get_task_events(task_name):
	return frappe.get_all(
		"Event",
		filters={
			"reference_doctype": "CRM Task",
			"reference_docname": str(task_name),
		},
		fields=["name", "google_calendar"],
		order_by="creation asc",
	)


def get_google_calendar(user):
	calendar_name = frappe.db.get_value(
		"Google Calendar",
		{
			"user": user,
			"enable": 1,
			"push_to_google_calendar": 1,
		},
		"name",
	)

	if not calendar_name:
		return None

	return frappe.db.get_value(
		"Google Calendar",
		calendar_name,
		["name", "google_calendar_id"],
		as_dict=True,
	)


def delete_event(event_name):
	if event_name and frappe.db.exists("Event", event_name):
		frappe.delete_doc(
			"Event",
			event_name,
			ignore_permissions=True,
		)


def delete_task_calendar_events(task_name):
	events = get_task_events(task_name)

	for event in events:
		delete_event(event.name)


def sync_task_calendar_event(task_name):
	# Task may have been deleted before this background job runs.
	if not frappe.db.exists("CRM Task", task_name):
		delete_task_calendar_events(task_name)
		return

	task = frappe.get_doc("CRM Task", task_name)
	existing_events = get_task_events(task_name)

	# There should only ever be one Event.
	existing_event = existing_events[0] if existing_events else None

	# Clean up duplicates if they somehow exist.
	for duplicate in existing_events[1:]:
		delete_event(duplicate.name)

	duration = task.get("custom_duration")

	# Without these values, there should be no calendar Event.
	if not task.assigned_to or not task.due_date or not duration:
		if existing_event:
			delete_event(existing_event.name)
		return

	calendar = get_google_calendar(task.assigned_to)

	# Assigned user has no enabled Google Calendar with push enabled.
	if not calendar:
		if existing_event:
			delete_event(existing_event.name)
		return

	starts_on = get_datetime(task.due_date)
	ends_on = add_to_date(
		starts_on,
		minutes=int(duration),
	)

	# Assigned user changed to another calendar.
	if (
		existing_event
		and existing_event.google_calendar != calendar.name
	):
		delete_event(existing_event.name)
		existing_event = None

	if existing_event:
		event = frappe.get_doc("Event", existing_event.name)

		event.subject = task.title
		event.description = task.description
		event.starts_on = starts_on
		event.ends_on = ends_on

		event.sync_with_google_calendar = 1
		event.google_calendar = calendar.name
		event.google_calendar_id = calendar.google_calendar_id

		event.reference_doctype = "CRM Task"
		event.reference_docname = str(task.name)

		event.save(ignore_permissions=True)

	else:
		event = frappe.get_doc(
			{
				"doctype": "Event",
				"subject": task.title,
				"description": task.description,
				"event_type": "Private",
				"event_category": "Event",
				"starts_on": starts_on,
				"ends_on": ends_on,
				"all_day": 0,

				# Don't send the standard Frappe morning reminder.
				"send_reminder": 0,

				"sync_with_google_calendar": 1,
				"google_calendar": calendar.name,
				"google_calendar_id": calendar.google_calendar_id,

				"reference_doctype": "CRM Task",
				"reference_docname": str(task.name),
			}
		)

		event.insert(ignore_permissions=True)

		# Make the assigned user the owner of the Frappe Event too.
		frappe.db.set_value(
			"Event",
			event.name,
			"owner",
			task.assigned_to,
			update_modified=False,
		)