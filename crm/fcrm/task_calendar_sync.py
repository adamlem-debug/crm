import frappe
from frappe.utils import add_to_date, get_datetime


def queue_task_calendar_sync(doc, method=None):
    # Keep synchronous while we finish testing.
    sync_task_calendar_event(doc.name)


def queue_task_calendar_delete(doc, method=None):
    """
    Runs AFTER the CRM Task has been deleted.

    The deleted document object still contains the value of
    custom_calendar_event, so we can use it to remove the linked Event.

    Event deletion is queued after commit so Google Calendar is only
    touched if the CRM Task deletion transaction succeeds.
    """
    event_name = doc.get("custom_calendar_event")

    if not event_name:
        return

    frappe.enqueue(
        "crm.fcrm.task_calendar_sync.delete_calendar_event",
        queue="short",
        enqueue_after_commit=True,
        event_name=event_name,
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


def delete_calendar_event(event_name):
    """
    Delete the Frappe Event.

    Frappe's native Event deletion hook then handles removal of the
    corresponding Google Calendar event.
    """
    if not event_name:
        return

    if frappe.db.exists("Event", event_name):
        frappe.delete_doc(
            "Event",
            event_name,
            ignore_permissions=True,
        )


def unlink_task_calendar_event(task_name):
    """
    Clear the CRM Task -> Event link without triggering another
    CRM Task on_update lifecycle.
    """
    if frappe.db.exists("CRM Task", task_name):
        frappe.db.set_value(
            "CRM Task",
            task_name,
            "custom_calendar_event",
            None,
            update_modified=False,
        )


def remove_task_event(task_name, event_name):
    """
    Used while the Task still exists, for example:
    - Assigned To changes to user without calendar
    - Required calendar data becomes unavailable
    - Assigned user changes to another Google Calendar
    """
    if not event_name:
        return

    unlink_task_calendar_event(task_name)

    delete_calendar_event(event_name)


def create_calendar_event(task, calendar, starts_on, ends_on):
    event = frappe.get_doc(
        {
            "doctype": "Event",

            "subject": task.title,
            "description": task.description or "",

            "event_type": "Public",
            "event_category": "Event",

            "starts_on": starts_on,
            "ends_on": ends_on,

            "all_day": 0,
            "send_reminder": 0,

            "sync_with_google_calendar": 1,

            "google_calendar": calendar.name,
            "google_calendar_id": calendar.google_calendar_id,
        }
    )

    event.insert(
        ignore_permissions=True,
    )

    # Assigned CRM user remains the Event owner.
    frappe.db.set_value(
        "Event",
        event.name,
        "owner",
        task.assigned_to,
        update_modified=False,
    )

    # Store the relationship on CRM Task.
    # Direct DB update avoids triggering another on_update loop.
    frappe.db.set_value(
        "CRM Task",
        task.name,
        "custom_calendar_event",
        event.name,
        update_modified=False,
    )

    return event


def sync_task_calendar_event(task_name):
    if not frappe.db.exists("CRM Task", task_name):
        return

    task = frappe.get_doc(
        "CRM Task",
        task_name,
    )

    duration = task.get("custom_duration")
    event_name = task.get("custom_calendar_event")

    # ---------------------------------------------------------
    # REQUIRED TASK DATA
    # ---------------------------------------------------------
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

        if event_name:
            remove_task_event(
                task.name,
                event_name,
            )

        return

    # ---------------------------------------------------------
    # GOOGLE CALENDAR LOOKUP
    # ---------------------------------------------------------
    calendar = get_google_calendar(
        task.assigned_to,
    )

    if not calendar:
        # Assigned user has no configured Google Calendar.
        # Remove an Event that may have existed previously.
        if event_name:
            remove_task_event(
                task.name,
                event_name,
            )

        return

    starts_on = get_datetime(
        task.due_date,
    )

    ends_on = add_to_date(
        starts_on,
        minutes=int(duration),
    )

    # ---------------------------------------------------------
    # EXISTING EVENT
    # ---------------------------------------------------------
    existing_event = None

    if event_name and frappe.db.exists(
        "Event",
        event_name,
    ):
        existing_event = frappe.get_doc(
            "Event",
            event_name,
        )

    # Task points to Event that no longer exists.
    if event_name and not existing_event:
        unlink_task_calendar_event(
            task.name,
        )

        event_name = None

    # ---------------------------------------------------------
    # REASSIGNMENT
    # ---------------------------------------------------------
    if (
        existing_event
        and existing_event.google_calendar != calendar.name
    ):
        # Remove link from Task first.
        unlink_task_calendar_event(
            task.name,
        )

        # Delete old Event / old Google Calendar event.
        delete_calendar_event(
            existing_event.name,
        )

        existing_event = None
        event_name = None

    # ---------------------------------------------------------
    # UPDATE EXISTING EVENT
    # ---------------------------------------------------------
    if existing_event:
        existing_event.subject = task.title
        existing_event.description = task.description or ""

        existing_event.starts_on = starts_on
        existing_event.ends_on = ends_on

        existing_event.all_day = 0
        existing_event.send_reminder = 0

        # Public = visible to users with Event access.
        existing_event.event_type = "Public"

        existing_event.sync_with_google_calendar = 1
        existing_event.google_calendar = calendar.name
        existing_event.google_calendar_id = calendar.google_calendar_id

        existing_event.save(
            ignore_permissions=True,
        )

        return

    # ---------------------------------------------------------
    # CREATE NEW EVENT
    # ---------------------------------------------------------
    create_calendar_event(
        task,
        calendar,
        starts_on,
        ends_on,
    )