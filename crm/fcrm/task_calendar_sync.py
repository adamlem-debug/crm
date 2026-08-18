import frappe
from frappe.utils import add_to_date, get_datetime


def queue_task_calendar_sync(doc, method=None):
    # Keep this synchronous for now while we finish testing.
    sync_task_calendar_event(doc.name)


def queue_task_calendar_delete(doc, method=None):
    """
    Before CRM Task deletion, remove the Dynamic Link from its Event(s).

    Frappe checks dynamic links after on_trash. If the Event still points
    to the CRM Task at that point, it can prevent the Task from being deleted.

    The Event itself is deleted only after the Task deletion transaction
    successfully commits.
    """
    events = get_task_events(doc.name)

    event_names = [
        event.name
        for event in events
    ]

    if not event_names:
        return

    # Remove the Dynamic Link immediately so it cannot block
    # deletion of the CRM Task.
    #
    # Direct DB updates intentionally do not trigger Event on_update,
    # so Google Calendar is not touched at this stage.
    for event_name in event_names:
        frappe.db.set_value(
            "Event",
            event_name,
            "reference_docname",
            None,
            update_modified=False,
        )

        frappe.db.set_value(
            "Event",
            event_name,
            "reference_doctype",
            None,
            update_modified=False,
        )

    # Delete the Frappe Event only after the CRM Task deletion commits.
    #
    # Deleting the Event normally triggers Frappe's native Google Calendar
    # on_trash hook, which removes/cancels the Google Calendar event.
    frappe.enqueue(
        "crm.fcrm.task_calendar_sync.delete_calendar_events_by_name",
        queue="short",
        enqueue_after_commit=True,
        event_names=event_names,
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


def delete_calendar_events_by_name(event_names):
    """
    Delete Events after their CRM Task has successfully been deleted.

    frappe.delete_doc triggers the native Event on_trash hook,
    which handles Google Calendar deletion/cancellation.
    """
    for event_name in event_names or []:
        delete_event(event_name)


def delete_task_calendar_events(task_name):
    events = get_task_events(task_name)

    for event in events:
        delete_event(event.name)


def sync_task_calendar_event(task_name):
    # Task may already have been deleted.
    if not frappe.db.exists("CRM Task", task_name):
        delete_task_calendar_events(task_name)
        return

    task = frappe.get_doc(
        "CRM Task",
        task_name,
    )

    existing_events = get_task_events(task_name)
    existing_event = existing_events[0] if existing_events else None

    # There should only ever be one Event for a CRM Task.
    # Clean up duplicates if they somehow exist.
    for duplicate in existing_events[1:]:
        delete_event(duplicate.name)

    duration = task.get("custom_duration")

    # Calendar synchronization requires:
    # - Assigned To
    # - Due Date
    # - Duration
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

        # If the Task used to be valid for calendar sync but no longer is,
        # remove the existing Event.
        if existing_event:
            delete_event(existing_event.name)

        return

    calendar = get_google_calendar(
        task.assigned_to,
    )

    # Assigned user has no enabled Google Calendar with Push enabled.
    if not calendar:
        frappe.log_error(
            title="CRM Task Calendar Sync - Google Calendar Missing",
            message=(
                f"Task: {task.name}\n"
                f"Assigned To: {task.assigned_to}\n"
                "No enabled Google Calendar with Push enabled was found."
            ),
        )

        # If this Task previously belonged to somebody with a Google
        # Calendar, remove the old Event.
        if existing_event:
            delete_event(existing_event.name)

        return

    starts_on = get_datetime(
        task.due_date,
    )

    ends_on = add_to_date(
        starts_on,
        minutes=int(duration),
    )

    # If Assigned To changed, the target Google Calendar changed.
    #
    # Delete the old Event from the previous user's calendar and
    # create a fresh one for the new user's calendar.
    if (
        existing_event
        and existing_event.google_calendar != calendar.name
    ):
        delete_event(
            existing_event.name,
        )

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

        # Public means users with Event access can see it in Frappe.
        event.event_type = "Public"

        event.sync_with_google_calendar = 1
        event.google_calendar = calendar.name
        event.google_calendar_id = calendar.google_calendar_id

        event.reference_doctype = "CRM Task"
        event.reference_docname = str(task.name)

        event.save(
            ignore_permissions=True,
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

            "event_type": "Public",
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

    event.insert(
        ignore_permissions=True,
    )

    # Keep the assigned user as the actual Event owner,
    # even though the Event is Public.
    frappe.db.set_value(
        "Event",
        event.name,
        "owner",
        task.assigned_to,
        update_modified=False,
    )