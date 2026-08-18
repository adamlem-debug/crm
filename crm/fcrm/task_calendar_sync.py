import frappe
from frappe.utils import add_to_date, get_datetime


def queue_task_calendar_sync(doc, method=None):
    # Keep synchronous while we finish testing.
    sync_task_calendar_event(doc.name)


def cleanup_task_notifications(doc, method=None):
    """
    Runs during CRM Task on_trash.

    CRM Notification contains Dynamic Links to CRM Task records.
    Those links can prevent Frappe from deleting the Task.

    Delete Task-related CRM Notifications before Frappe performs
    its linked-document validation.

    These DB deletions are part of the same transaction, so if
    Task deletion fails later, they are rolled back as well.
    """

    # CRM Notification:
    # reference_doctype -> reference_name
    frappe.db.delete(
        "CRM Notification",
        {
            "reference_doctype": "CRM Task",
            "reference_name": str(doc.name),
        },
    )

    # CRM Notification:
    # notification_type_doctype -> notification_type_doc
    frappe.db.delete(
        "CRM Notification",
        {
            "notification_type_doctype": "CRM Task",
            "notification_type_doc": str(doc.name),
        },
    )


def queue_task_calendar_delete(doc, method=None):
    """
    Runs after the CRM Task has successfully been deleted.

    The deleted document object still contains custom_calendar_event.

    Delete the corresponding Frappe Event after commit.
    Frappe's native Event hook then removes the Google Calendar event.
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
    Delete a Frappe Event.

    Normal Event deletion triggers Frappe's native
    Google Calendar cleanup.
    """

    if not event_name:
        return

    if frappe.db.exists(
        "Event",
        event_name,
    ):
        frappe.delete_doc(
            "Event",
            event_name,
            ignore_permissions=True,
        )


def unlink_task_calendar_event(task_name):
    """
    Remove CRM Task -> Event link without triggering
    another CRM Task on_update cycle.
    """

    if frappe.db.exists(
        "CRM Task",
        task_name,
    ):
        frappe.db.set_value(
            "CRM Task",
            task_name,
            "custom_calendar_event",
            None,
            update_modified=False,
        )


def remove_task_event(task_name, event_name):
    """
    Used when Task still exists but should no longer
    have a calendar Event.
    """

    if not event_name:
        return

    unlink_task_calendar_event(
        task_name,
    )

    delete_calendar_event(
        event_name,
    )


def create_calendar_event(
    task,
    calendar,
    starts_on,
    ends_on,
):
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

    # Store Event relationship on CRM Task.
    # Direct DB update prevents another Task on_update loop.
    frappe.db.set_value(
        "CRM Task",
        task.name,
        "custom_calendar_event",
        event.name,
        update_modified=False,
    )

    return event


def sync_task_calendar_event(task_name):
    if not frappe.db.exists(
        "CRM Task",
        task_name,
    ):
        return

    task = frappe.get_doc(
        "CRM Task",
        task_name,
    )

    duration = task.get(
        "custom_duration",
    )

    event_name = task.get(
        "custom_calendar_event",
    )

    # ---------------------------------------------------------
    # REQUIRED TASK DATA
    # ---------------------------------------------------------

    if (
        not task.assigned_to
        or not task.due_date
        or not duration
    ):
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
        # User has no enabled Google Calendar.
        # Remove Event if one previously existed.
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

    if (
        event_name
        and frappe.db.exists(
            "Event",
            event_name,
        )
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
        unlink_task_calendar_event(
            task.name,
        )

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

        # Public so admins/users with Event access can see it.
        existing_event.event_type = "Public"

        existing_event.sync_with_google_calendar = 1
        existing_event.google_calendar = calendar.name
        existing_event.google_calendar_id = (
            calendar.google_calendar_id
        )

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