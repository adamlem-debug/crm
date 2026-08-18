import frappe
from frappe.utils import add_to_date, get_datetime


def queue_task_calendar_sync(doc, method=None):
    # Run synchronously temporarily while debugging.
    # Once everything works, we will move this back to a background job.
    sync_task_calendar_event(doc.name)


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
        fields=[
            "name",
            "google_calendar",
            "google_calendar_event_id",
        ],
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
        [
            "name",
            "google_calendar_id",
        ],
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
    # Task may already have been deleted.
    if not frappe.db.exists("CRM Task", task_name):
        delete_task_calendar_events(task_name)
        return

    task = frappe.get_doc("CRM Task", task_name)

    existing_events = get_task_events(task_name)
    existing_event = existing_events[0] if existing_events else None

    # Remove duplicates if they somehow exist.
    for duplicate in existing_events[1:]:
        delete_event(duplicate.name)

    duration = task.get("custom_duration")

    # Required information for calendar sync.
    if not task.assigned_to or not task.due_date or not duration:
        frappe.log_error(
            title="CRM Task Calendar Sync - Missing Data",
            message=(
                f"Task: {task.name}\n"
                f"Assigned To: {task.assigned_to}\n"
                f"Due Date: {task.due_date}\n"
                f"Duration: {duration}"
            ),
        )

        if existing_event:
            delete_event(existing_event.name)

        return

    calendar = get_google_calendar(task.assigned_to)

    # Assigned user has no enabled Google Calendar with push enabled.
    if not calendar:
        frappe.log_error(
            title="CRM Task Calendar Sync - Google Calendar Missing",
            message=(
                f"Task: {task.name}\n"
                f"Assigned To: {task.assigned_to}\n"
                "No enabled Google Calendar with Push enabled was found."
            ),
        )

        if existing_event:
            delete_event(existing_event.name)

        return

    starts_on = get_datetime(task.due_date)

    ends_on = add_to_date(
        starts_on,
        minutes=int(duration),
    )

    # If Assigned To changed and therefore the target Google Calendar
    # changed, remove the old Event and create a new one.
    if (
        existing_event
        and existing_event.google_calendar != calendar.name
    ):
        delete_event(existing_event.name)
        existing_event = None

    # ---------------------------------------------------------
    # UPDATE EXISTING EVENT
    # ---------------------------------------------------------
    if existing_event:
        event = frappe.get_doc(
            "Event",
            existing_event.name,
        )

        event.subject = task.title
        event.description = task.description or ""

        event.starts_on = starts_on
        event.ends_on = ends_on

        event.all_day = 0
        event.send_reminder = 0

        event.sync_with_google_calendar = 1
        event.google_calendar = calendar.name
        event.google_calendar_id = calendar.google_calendar_id

        event.reference_doctype = "CRM Task"
        event.reference_docname = str(task.name)

        event.save(ignore_permissions=True)

        # Reload because Frappe's Google hook may have updated
        # google_calendar_event_id directly in the database.
        event.reload()

        frappe.msgprint(
            (
                f"CRM Task calendar sync successful.<br><br>"
                f"Frappe Event: <b>{event.name}</b><br>"
                f"Google Calendar: <b>{calendar.name}</b><br>"
                f"Google Event ID: "
                f"<b>{event.google_calendar_event_id or 'Not set'}</b><br>"
                f"Start: <b>{event.starts_on}</b><br>"
                f"End: <b>{event.ends_on}</b>"
            ),
            title="Calendar Sync Debug",
            indicator="green",
        )

        return

    # ---------------------------------------------------------
    # CREATE NEW EVENT
    # ---------------------------------------------------------
    event = frappe.get_doc(
        {
            "doctype": "Event",

            "subject": task.title,
            "description": task.description or "",

            "event_type": "Private",
            "event_category": "Event",

            "starts_on": starts_on,
            "ends_on": ends_on,

            "all_day": 0,
            "send_reminder": 0,

            "sync_with_google_calendar": 1,

            "google_calendar": calendar.name,
            "google_calendar_id": calendar.google_calendar_id,

            "reference_doctype": "CRM Task",
            "reference_docname": str(task.name),
        }
    )

    # This should also trigger Frappe's native
    # Event -> Google Calendar after_insert hook.
    event.insert(ignore_permissions=True)

    # Make the assigned CRM user the owner of the Frappe Event.
    frappe.db.set_value(
        "Event",
        event.name,
        "owner",
        task.assigned_to,
        update_modified=False,
    )

    # Frappe's Google hook stores google_calendar_event_id
    # directly in the DB, so reload before showing debug info.
    event.reload()

    frappe.msgprint(
        (
            f"CRM Task calendar sync successful.<br><br>"
            f"Frappe Event: <b>{event.name}</b><br>"
            f"Google Calendar: <b>{calendar.name}</b><br>"
            f"Google Event ID: "
            f"<b>{event.google_calendar_event_id or 'Not set'}</b><br>"
            f"Start: <b>{event.starts_on}</b><br>"
            f"End: <b>{event.ends_on}</b>"
        ),
        title="Calendar Sync Debug",
        indicator="green",
    )