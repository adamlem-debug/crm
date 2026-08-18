import frappe
from frappe.utils import add_to_date, get_datetime


TERMINAL_EVENT_STATUSES = {
    "Cancelled",
    "Closed",
}


def queue_task_calendar_sync(doc, method=None):
    """
    Queue calendar synchronization only after the CRM Task
    transaction has successfully committed.

    This keeps Task saving independent from Google Calendar sync.
    """

    frappe.enqueue(
        "crm.fcrm.task_calendar_sync.sync_task_calendar_event",
        queue="short",
        enqueue_after_commit=True,
        task_name=doc.name,
    )


def cleanup_task_notifications(doc, method=None):
    """
    Workaround for Frappe CRM.

    CRM Notification records can dynamically reference CRM Tasks
    and block Task deletion during Frappe's linked-document checks.

    These deletes happen in the same DB transaction as the Task
    deletion, so they are rolled back if Task deletion fails.
    """

    frappe.db.delete(
        "CRM Notification",
        {
            "reference_doctype": "CRM Task",
            "reference_name": str(doc.name),
        },
    )

    frappe.db.delete(
        "CRM Notification",
        {
            "notification_type_doctype": "CRM Task",
            "notification_type_doc": str(doc.name),
        },
    )


def queue_task_calendar_delete(doc, method=None):
    """
    Runs after CRM Task deletion.

    Delete all Events that historically belonged to the Task
    only after the Task deletion transaction successfully commits.
    """

    frappe.enqueue(
        "crm.fcrm.task_calendar_sync.delete_task_calendar_history",
        queue="short",
        enqueue_after_commit=True,
        task_name=str(doc.name),
        current_event_name=doc.get("custom_calendar_event"),
    )


def get_calendar_settings():
    """
    Read configurable calendar-sync rules from VitalAge CRM Settings.
    """

    settings = frappe.get_single(
        "VitalAge CRM Settings"
    )

    enabled_task_types = {
        row.task_type
        for row in (
            settings.get("calendar_task_types")
            or []
        )
        if row.task_type
    }

    cancellation_statuses = {
        row.task_status
        for row in (
            settings.get(
                "calendar_cancellation_statuses"
            )
            or []
        )
        if row.task_status
    }

    return (
        enabled_task_types,
        cancellation_statuses,
    )


def get_google_calendar(user):
    """
    Find the enabled push-enabled Google Calendar
    belonging to the assigned Frappe user.
    """

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
    Delete one Frappe Event.

    Frappe's normal Event deletion lifecycle handles
    the corresponding Google Calendar cleanup.
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


def delete_task_calendar_history(
    task_name,
    current_event_name=None,
):
    """
    Physically deleting a CRM Task removes all Events
    ever created for it, including old cancelled Events.
    """

    event_names = set()

    if current_event_name:
        event_names.add(
            current_event_name
        )

    historical_events = frappe.get_all(
        "Event",
        filters={
            "custom_crm_task_name": str(
                task_name
            ),
        },
        pluck="name",
    )

    event_names.update(
        historical_events
    )

    for event_name in event_names:
        delete_calendar_event(
            event_name
        )


def unlink_task_calendar_event(task_name):
    """
    Clear the Task -> current Event relationship
    without triggering another CRM Task on_update.
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


def remove_active_task_event(
    task_name,
    event,
):
    """
    Used when an active Task should no longer have
    a calendar Event.

    Cancelled/Closed Events are retained as history.
    """

    unlink_task_calendar_event(
        task_name
    )

    if (
        event
        and event.status
        not in TERMINAL_EVENT_STATUSES
    ):
        delete_calendar_event(
            event.name
        )


def cancel_task_event(
    task,
    event,
):
    """
    Cancel the current Event but retain it as history.

    The Task continues pointing to the cancelled Event.
    If the Task later becomes active again, that Event
    is detached and a fresh Event is created.
    """

    if not event:
        return

    event.custom_crm_task_name = str(
        task.name
    )

    if (
        event.status
        not in TERMINAL_EVENT_STATUSES
    ):
        event.status = "Cancelled"

        event.save(
            ignore_permissions=True
        )


def create_calendar_event(
    task,
    calendar,
    starts_on,
    ends_on,
):
    """
    Create a fresh Frappe Event and allow Frappe's
    native Google Calendar integration to sync it.
    """

    event = frappe.get_doc(
        {
            "doctype": "Event",

            "subject": task.title,
            "description": (
                task.description or ""
            ),

            "event_type": "Public",
            "event_category": "Event",
            "status": "Open",

            "starts_on": starts_on,
            "ends_on": ends_on,

            "all_day": 0,
            "send_reminder": 0,

            "sync_with_google_calendar": 1,

            "google_calendar": (
                calendar.name
            ),

            "google_calendar_id": (
                calendar.google_calendar_id
            ),

            "custom_crm_task_name": str(
                task.name
            ),
        }
    )

    event.insert(
        ignore_permissions=True
    )

    # Assigned Task user owns the Event.
    # Event remains Public for broader Event visibility.
    frappe.db.set_value(
        "Event",
        event.name,
        "owner",
        task.assigned_to,
        update_modified=False,
    )

    # Store CURRENT Event on Task.
    #
    # Direct DB update intentionally avoids another
    # CRM Task on_update -> calendar sync loop.
    frappe.db.set_value(
        "CRM Task",
        task.name,
        "custom_calendar_event",
        event.name,
        update_modified=False,
    )

    return event


def sync_task_calendar_event(task_name):
    """
    Synchronize the current state of a CRM Task with its
    Frappe Event / Google Calendar event.

    Important:
    This function always reads the Task fresh from the DB.
    The queued job therefore works from the latest committed
    Task state rather than from stale data passed by the UI.
    """

    if not frappe.db.exists(
        "CRM Task",
        task_name,
    ):
        return

    task = frappe.get_doc(
        "CRM Task",
        task_name,
    )

    (
        enabled_task_types,
        cancellation_statuses,
    ) = get_calendar_settings()

    task_type = task.get(
        "custom_task_type"
    )

    event_name = task.get(
        "custom_calendar_event"
    )

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

    # ---------------------------------------------------------
    # STALE TASK -> EVENT LINK
    # ---------------------------------------------------------

    if (
        event_name
        and not existing_event
    ):
        unlink_task_calendar_event(
            task.name
        )

        event_name = None

    # ---------------------------------------------------------
    # TASK CANCELLATION
    # ---------------------------------------------------------

    if (
        task.status
        in cancellation_statuses
    ):
        if existing_event:
            cancel_task_event(
                task,
                existing_event,
            )

        return

    # ---------------------------------------------------------
    # TASK TYPE ELIGIBILITY
    # ---------------------------------------------------------

    if (
        task_type
        not in enabled_task_types
    ):
        if existing_event:
            remove_active_task_event(
                task.name,
                existing_event,
            )

        return

    # ---------------------------------------------------------
    # REACTIVATION AFTER CANCELLATION
    # ---------------------------------------------------------

    if (
        existing_event
        and existing_event.status
        in TERMINAL_EVENT_STATUSES
    ):
        unlink_task_calendar_event(
            task.name
        )

        existing_event = None
        event_name = None

    # ---------------------------------------------------------
    # REQUIRED DATA
    # ---------------------------------------------------------

    duration = task.get(
        "custom_duration"
    )

    if (
        not task.assigned_to
        or not task.due_date
        or not duration
    ):
        frappe.log_error(
            title=(
                "CRM Task Calendar Sync "
                "- Missing Data"
            ),
            message=(
                f"Task: {task.name}\n"
                f"Task Type: {task_type}\n"
                f"Assigned To: "
                f"{task.assigned_to}\n"
                f"Due Date: "
                f"{task.due_date}\n"
                f"Duration: {duration}"
            ),
        )

        if existing_event:
            remove_active_task_event(
                task.name,
                existing_event,
            )

        return

    # ---------------------------------------------------------
    # GOOGLE CALENDAR LOOKUP
    # ---------------------------------------------------------

    calendar = get_google_calendar(
        task.assigned_to
    )

    if not calendar:
        if existing_event:
            remove_active_task_event(
                task.name,
                existing_event,
            )

        return

    # ---------------------------------------------------------
    # START / END
    # ---------------------------------------------------------

    starts_on = get_datetime(
        task.due_date
    )

    ends_on = add_to_date(
        starts_on,
        minutes=int(duration),
    )

    # ---------------------------------------------------------
    # REASSIGNMENT TO ANOTHER CALENDAR
    # ---------------------------------------------------------

    if (
        existing_event
        and existing_event.google_calendar
        != calendar.name
    ):
        unlink_task_calendar_event(
            task.name
        )

        delete_calendar_event(
            existing_event.name
        )

        existing_event = None
        event_name = None

    # ---------------------------------------------------------
    # UPDATE CURRENT EVENT
    # ---------------------------------------------------------

    if existing_event:
        existing_event.subject = (
            task.title
        )

        existing_event.description = (
            task.description or ""
        )

        existing_event.starts_on = (
            starts_on
        )

        existing_event.ends_on = (
            ends_on
        )

        existing_event.all_day = 0
        existing_event.send_reminder = 0

        existing_event.event_type = (
            "Public"
        )

        existing_event.sync_with_google_calendar = 1

        existing_event.google_calendar = (
            calendar.name
        )

        existing_event.google_calendar_id = (
            calendar.google_calendar_id
        )

        existing_event.custom_crm_task_name = str(
            task.name
        )

        existing_event.save(
            ignore_permissions=True
        )

        return

    # ---------------------------------------------------------
    # CREATE FRESH EVENT
    # ---------------------------------------------------------

    create_calendar_event(
        task,
        calendar,
        starts_on,
        ends_on,
    )